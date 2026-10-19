"""AutoTrader: the decision loop (docs/ARCHITECTURE.md §6.3).

manage exits → generate ideas → rank → LLM gate (veto/shrink only) → risk sizing → execute.
Every step emits a Decision. Entries require ``armed``; exits of positions the auto-trader opened
run whenever the loop runs (risk-reducing). Day trades are unlimited (PDT rule eliminated 2026-06-04) unless
the user disables them or the broker rejects an order under legacy PDT protection.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import date
from typing import Any

from vantage.core import clock
from vantage.core.bus import Bus
from vantage.core.store import Store
from vantage.core.types import Decision, ManagedPosition, TradeIdea, parse_occ
from vantage.core.util import dumps, loads, new_id, now_ms, sanitize
from vantage.execution.engine import ExecutionEngine, WorkingOrder
from vantage.execution.risk import RISK_SCHEMA, RiskManager, can_close_same_day, is_pdt_rejection
from vantage.llm import prompts
from vantage.strategies.base import Strategy, StrategyContext, dossier
from vantage.strategies.library import REGISTRY
from vantage.strategies.presets import TIER_BY_ID, presets_wire, tier_for_equity

log = logging.getLogger(__name__)


class AutoTrader:
    def __init__(self, c: Any):
        """``c`` is the service Container (broker, market, options, signals, scanner, news, flow, calendar, llm)."""
        self.c = c
        self.mode: str = c.mode
        self.store: Store = c.store
        self.bus: Bus = c.bus
        self.exec: ExecutionEngine = c.execution
        saved_risk = self.store.kv_get(self.mode, "risk_config")
        # until the user customises anything, adopt the preset of the tier detected from live equity
        self._adopt_tier_preset = saved_risk is None and not self.store.kv_get(self.mode, "strategies")
        self.risk = RiskManager(saved_risk, self.mode)
        self.tier: str = self.store.kv_get(self.mode, "tier") or "small"
        self.tier_locked: bool = bool(self.store.kv_get(self.mode, "tier_locked", False))
        self.strategies: dict[str, Strategy] = {}
        self.enabled: dict[str, bool] = {}
        self._load_strategies()
        self.armed = False  # ALWAYS start disarmed
        self.preview = True
        self.manage_exits_when_disarmed = True
        self.running = False
        self.last_cycle: int | None = None
        self.cycle_ms: int | None = None
        self.decisions: list[Decision] = []
        self.peak_equity: float = float(self.store.kv_get(self.mode, "peak_equity", 0.0) or 0.0)
        self._task: asyncio.Task[None] | None = None
        self._cycle_lock = asyncio.Lock()
        self._exit_pending: set[str] = set()
        self._last_preview: dict[str, int] = {}
        self.exec.on_fill(self._on_fill)

    # ------------------------------------------------------------------ config
    def _load_strategies(self) -> None:
        saved = self.store.kv_get(self.mode, "strategies", {}) or {}
        tier_defaults = set(TIER_BY_ID.get(self.tier, TIER_BY_ID["small"])["strategies"])
        for sid, cls in REGISTRY.items():
            cfg = saved.get(sid, {})
            s = cls(cfg.get("params"))
            s.use_llm_gate = bool(cfg.get("use_llm_gate", cls.default_llm_gate))
            self.strategies[sid] = s
            self.enabled[sid] = bool(cfg.get("enabled", sid in tier_defaults))

    def _save_strategies(self) -> None:
        self.store.kv_set(self.mode, "strategies", {
            sid: {"enabled": self.enabled[sid], "params": s.params, "use_llm_gate": s.use_llm_gate}
            for sid, s in self.strategies.items()
        })

    def update_strategy(self, sid: str, enabled: bool | None, params: dict[str, Any] | None, use_llm_gate: bool | None) -> None:
        if sid not in self.strategies:
            raise KeyError(sid)
        s = self.strategies[sid]
        if params:
            new = type(s)({**s.params, **params})
            new.use_llm_gate = s.use_llm_gate
            if isinstance(s, type(s)) and hasattr(s, "_reviewed"):
                new._reviewed = s._reviewed  # type: ignore[attr-defined]
            self.strategies[sid] = s = new
        if use_llm_gate is not None:
            s.use_llm_gate = bool(use_llm_gate)
        if enabled is not None:
            self.enabled[sid] = bool(enabled)
        self._save_strategies()

    def update_risk(self, values: dict[str, Any]) -> None:
        self.risk.update(values)
        self.store.kv_set(self.mode, "risk_config", self.risk.cfg)

    def apply_preset(self, tier: str) -> None:
        t = TIER_BY_ID[tier]
        self.tier = tier
        self.tier_locked = True
        self.store.kv_set(self.mode, "tier", tier)
        self.store.kv_set(self.mode, "tier_locked", True)
        self.update_risk(t["risk"])
        for sid in self.strategies:
            self.enabled[sid] = sid in t["strategies"]
        self._save_strategies()

    async def set_armed(self, armed: bool) -> None:
        if armed and self.risk.killed:
            raise PermissionError(f"kill switch active: {self.risk.kill_reason}")
        self.armed = armed
        self._decide("system", "-", "skip", "auto-trader ARMED" if armed else "auto-trader disarmed")
        if not armed:
            await self.exec.cancel_all("entry")

    async def kill(self, reason: str, flatten: bool = False) -> None:
        self.risk.kill(reason)
        self.armed = False
        await self.exec.cancel_all()
        self._decide("system", "-", "error", f"KILL SWITCH: {reason}")
        if flatten:
            for p in await self.c.broker.get_positions():
                try:
                    await self.exec.submit(p["symbol"], "sell", p["qty"], p["asset_class"] == "option", "exit",
                                           p.get("strategy_id"), urgent=True)
                except Exception as e:  # noqa: BLE001
                    self._decide("system", p["symbol"], "error", f"flatten failed: {e}")

    # ------------------------------------------------------------------ loop
    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="autotrader")

    async def stop(self) -> None:
        self.armed = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self.running = False

    async def _loop(self) -> None:
        self.running = True
        await asyncio.sleep(12)  # let signals warm up
        while True:
            t0 = time.monotonic()
            try:
                await self.cycle()
            except Exception as e:  # noqa: BLE001
                log.exception("autotrader cycle failed")
                self._decide("system", "-", "error", f"cycle failed: {e}")
            await asyncio.sleep(max(2.0, self.risk.cfg["cycle_seconds"] - (time.monotonic() - t0)))

    async def cycle(self) -> None:
        async with self._cycle_lock:
            t0 = time.perf_counter()
            await self._cycle()
            self.cycle_ms = int((time.perf_counter() - t0) * 1000)
            self.last_cycle = now_ms()

    async def _cycle(self) -> None:
        c = self.c
        account = await c.broker.get_account()
        positions = await c.broker.get_positions()
        ms = c.market_state()
        today = clock.today_et()
        self.risk.roll_day(account, today)
        eq = account["equity"]
        if eq > self.peak_equity:
            self.peak_equity = eq
            self.store.kv_set(self.mode, "peak_equity", eq)
        if not self.tier_locked:
            self.tier = tier_for_equity(eq)
        if self._adopt_tier_preset:
            self._adopt_tier_preset = False
            self.apply_preset(self.tier)
            self.tier_locked = False
            self.store.kv_set(self.mode, "tier_locked", False)
            self._decide("system", "-", "skip", f"adopted '{self.tier}' tier preset for equity ${eq:,.0f}")
        account["tier"] = self.tier
        if not self.risk.killed:
            reason = self.risk.check_kill(account, self.peak_equity)
            if reason:
                await self.kill(reason)
        ctx = StrategyContext(
            mode=self.mode, market=c.market, options=c.options, signals=c.signals, scanner=c.scanner, news=c.news,
            flow=c.flow, calendar=c.calendar, llm=c.llm, account=account, positions=positions, tier=self.tier,
            risk=self.risk.cfg, universe=c.universe(), market_state=ms,
        )
        if ms.is_open or self.mode == "sim":
            if self.armed or self.manage_exits_when_disarmed:
                await self._manage(ctx, positions, account)
        if self.risk.killed or not (ms.is_open or self.mode == "sim"):
            return
        if not self.armed and not self.preview:
            return
        await self._entries(ctx, account, positions, ms)

    # ------------------------------------------------------------------ exits
    async def _metas(self) -> dict[str, dict[str, Any]]:
        rows = await self.store.query("SELECT symbol, strategy_id, entry_t, data FROM positions_meta WHERE mode=?", (self.mode,))
        return {r["symbol"]: {"strategy_id": r["strategy_id"], "entry_t": r["entry_t"], **loads(r["data"] or "{}")} for r in rows}

    async def _manage(self, ctx: StrategyContext, positions: list[dict[str, Any]], account: dict[str, Any]) -> None:
        metas = await self._metas()
        held = {p["symbol"] for p in positions}
        for sym in list(metas):
            if sym not in held and not self.exec.is_working(sym):
                self.store.write("DELETE FROM positions_meta WHERE mode=? AND symbol=?", (self.mode, sym))
        for p in positions:
            m = metas.get(p["symbol"])
            if not m or not m.get("strategy_id") or m["strategy_id"] not in self.strategies:
                continue
            if p["symbol"] in self._exit_pending or self.exec.is_working(p["symbol"]):
                continue
            peak = max(float(m.get("peak_plpc", 0.0)), p["unrealized_plpc"])
            if peak != m.get("peak_plpc"):
                m["peak_plpc"] = peak
                self._save_meta(p["symbol"], m)
            occ = parse_occ(p["symbol"])
            mp = ManagedPosition(
                symbol=p["symbol"], underlying=p["underlying"], asset_class=p["asset_class"], qty=p["qty"],
                avg_entry_price=p["avg_entry_price"], current_price=p["current_price"], market_value=p["market_value"],
                unrealized_plpc=p["unrealized_plpc"], strategy_id=m["strategy_id"], direction=int(m.get("direction", 1)),
                entry_t=m.get("entry_t"), peak_plpc=peak, stop_pct=m.get("stop_pct"), target_pct=m.get("target_pct"),
                trail_pct=m.get("trail_pct"), trail_activate_pct=m.get("trail_activate_pct"),
                time_stop_days=m.get("time_stop_days"), min_dte_exit=m.get("min_dte_exit"),
                expiration=occ[1] if occ else None, meta=m,
            )
            strat = self.strategies[m["strategy_id"]]
            try:
                sig = await strat.manage(ctx, mp)
            except Exception as e:  # noqa: BLE001
                self._decide(strat.id, p["symbol"], "error", f"manage failed: {e}")
                continue
            if sig is None:
                continue
            opened_today = m.get("entry_t") and _et_date(m["entry_t"]) == clock.today_et()
            if opened_today and self.mode != "sim" and not sig.urgent and not can_close_same_day(
                    account, self.risk.cfg["allow_daytrades"], self.risk.broker_pdt):
                why = ("day trades disabled in risk settings" if self.risk.cfg["allow_daytrades"] == "never"
                       else "broker enforcing legacy PDT protection")
                self._decide(strat.id, p["symbol"], "skip", f"exit deferred to next session ({why}): {sig.reason}")
                continue
            if not self.armed and not self.manage_exits_when_disarmed:
                continue
            qty = p["qty"] * sig.pct
            if p["asset_class"] == "option":
                qty = max(1, int(round(qty)))
            self._exit_pending.add(p["symbol"])
            try:
                w = await self.exec.submit(
                    p["symbol"], "sell", qty, p["asset_class"] == "option", "exit", strat.id,
                    aggression=0.5 if not sig.urgent else 1.0, max_aggression=1.0,
                    timeout_s=self.risk.cfg["order_timeout_s"], urgent=sig.urgent,
                    meta={"reason": sig.reason}, ref_price=p["current_price"],
                )
                self._decide(strat.id, p["symbol"], "exit", sig.reason, order_id=w.client_id)
                asyncio.create_task(self._clear_exit_pending(p["symbol"], w))
            except Exception as e:  # noqa: BLE001
                self._exit_pending.discard(p["symbol"])
                self._note_pdt_rejection(e)
                self._decide(strat.id, p["symbol"], "error", f"exit order failed: {e}")

    def _note_pdt_rejection(self, e: Exception) -> None:
        """PDT was eliminated (2026-06-04) but brokers may enforce legacy protection until 2027-10-20. If ours
        rejects an order for that reason, fall back to the legacy day-trade allowance for this session."""
        if not self.risk.broker_pdt and is_pdt_rejection(str(e)):
            self.risk.broker_pdt = True
            self._decide("system", "-", "error", "broker rejected an order under legacy PDT protection — "
                         "intraday strategies paused and same-day exits limited to the broker allowance this session")

    async def _clear_exit_pending(self, sym: str, w: WorkingOrder) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(w.done.wait(), w.timeout_s + 30)
        self._exit_pending.discard(sym)

    def _save_meta(self, symbol: str, m: dict[str, Any]) -> None:
        data = {k: v for k, v in m.items() if k not in ("strategy_id", "entry_t")}
        self.store.write(
            "INSERT INTO positions_meta(mode,symbol,strategy_id,entry_t,data) VALUES(?,?,?,?,?) "
            "ON CONFLICT(mode,symbol) DO UPDATE SET data=excluded.data",
            (self.mode, symbol, m.get("strategy_id"), m.get("entry_t"), dumps(sanitize(data)).decode()),
        )

    # ------------------------------------------------------------------ entries
    async def _entries(self, ctx: StrategyContext, account: dict[str, Any], positions: list[dict[str, Any]],
                       ms: clock.MarketState) -> None:
        ideas: list[TradeIdea] = []
        for sid, strat in self.strategies.items():
            if not self.enabled.get(sid):
                continue
            try:
                got = await strat.generate(ctx)
            except Exception as e:  # noqa: BLE001
                log.exception("strategy %s failed", sid)
                self._decide(sid, "-", "error", f"generate failed: {e}")
                continue
            ideas.extend(got)
        if not ideas:
            return
        # de-duplicate by instrument & underlying (keep best edge)
        best: dict[str, TradeIdea] = {}
        for i in sorted(ideas, key=lambda i: -i.edge_score):
            if i.underlying not in best:
                best[i.underlying] = i
        ranked = sorted(best.values(), key=lambda i: -i.edge_score)
        held_und = {p["underlying"] for p in positions}
        new_count = 0
        tier = TIER_BY_ID.get(self.tier, TIER_BY_ID["small"])
        for idea in ranked:
            if new_count >= self.risk.cfg["max_new_per_cycle"]:
                break
            if not self.armed:
                key = f"{idea.strategy_id}:{idea.symbol}"
                if now_ms() - self._last_preview.get(key, 0) > 15 * 60_000:
                    self._last_preview[key] = now_ms()
                    self._decide(idea.strategy_id, idea.symbol, "skip", f"preview (disarmed): {idea.rationale}",
                                 idea=idea)
                continue
            if idea.underlying in held_und and self.risk.cfg["max_per_underlying"] <= 1:
                continue
            intraday = self.strategies[idea.strategy_id].style == "intraday"
            dte = ctx.calendar.days_to_earnings(idea.underlying) if ctx.calendar else None
            fomc = any(d == clock.today_et() for d, _ in _fomc())
            # cheap pre-check before spending LLM time
            plan, why = self.risk.size_and_check(idea, account, positions, self.exec.pending_symbols(), ms, dte, fomc,
                                                 intraday, tier["fractional"])
            if plan is None:
                self._decide(idea.strategy_id, idea.symbol, "reject", why, idea=idea)
                continue
            llm_info = None
            if idea.use_llm_gate and self.risk.cfg["llm_enabled"] and self.c.llm is not None:
                llm_info = await self._gate(ctx, idea)
                if llm_info["decision"] == "reject":
                    self._decide(idea.strategy_id, idea.symbol, "reject", f"LLM veto: {llm_info['rationale']}",
                                 idea=idea, llm=llm_info)
                    continue
                if llm_info["decision"] == "error":
                    if self.risk.cfg["on_llm_failure"] == "skip":
                        self._decide(idea.strategy_id, idea.symbol, "skip", f"LLM unavailable ({llm_info['rationale']})",
                                     idea=idea, llm=llm_info)
                        continue
                    idea.size_multiplier = min(idea.size_multiplier, 0.5)
                else:
                    idea.size_multiplier = min(idea.size_multiplier, float(llm_info["size_multiplier"]))
                plan, why = self.risk.size_and_check(idea, account, positions, self.exec.pending_symbols(), ms, dte,
                                                     fomc, intraday, tier["fractional"])
                if plan is None:
                    self._decide(idea.strategy_id, idea.symbol, "reject", f"after LLM sizing: {why}", idea=idea, llm=llm_info)
                    continue
            try:
                meta = {
                    "direction": idea.direction, "stop_pct": idea.stop_pct, "target_pct": idea.target_pct,
                    "trail_pct": idea.trail_pct, "trail_activate_pct": idea.trail_activate_pct,
                    "time_stop_days": idea.time_stop_days, "min_dte_exit": idea.min_dte_exit,
                    "event_ok": bool(idea.signals.get("event_ok")), "underlying": idea.underlying,
                    "rationale": idea.rationale, "edge_score": idea.edge_score, "sizing": plan.sizing,
                }
                w = await self.exec.submit(
                    plan.symbol, "buy", plan.qty, plan.asset_class == "option", "entry", idea.strategy_id,
                    aggression=self.risk.cfg["aggression"], max_aggression=self.risk.cfg["max_aggression"],
                    timeout_s=self.risk.cfg["order_timeout_s"], meta=meta, ref_price=plan.limit_price,
                )
            except Exception as e:  # noqa: BLE001
                self._note_pdt_rejection(e)
                self._decide(idea.strategy_id, idea.symbol, "error", f"order failed: {e}", idea=idea, llm=llm_info)
                continue
            self.risk.record_entry()
            new_count += 1
            held_und.add(idea.underlying)
            self._decide(idea.strategy_id, idea.symbol, "enter", f"{why}. {idea.rationale}", idea=idea, llm=llm_info,
                         order_id=w.client_id)

    async def _gate(self, ctx: StrategyContext, idea: TradeIdea) -> dict[str, Any]:
        d = dossier(ctx, idea.underlying, idea)
        res = await self.c.llm.complete_json("gate", prompts.GATE_SYSTEM, prompts.gate_user(sanitize(d)),
                                             prompts.GATE_SCHEMA, cache_ttl_s=300)
        if not res.ok or not res.data:
            return {"decision": "error", "confidence": 0.0, "size_multiplier": 0.0, "rationale": res.error or "no reply",
                    "latency_ms": res.latency_ms}
        dec = res.data["decision"]
        mult = max(0.0, min(1.0, float(res.data.get("size_multiplier", 1.0))))
        if dec == "approve":
            mult = 1.0 if mult == 0 else mult
        elif dec == "reduce":
            mult = max(0.1, min(0.9, mult))
        return {"decision": dec, "confidence": float(res.data.get("confidence", 0.5)), "size_multiplier": mult,
                "rationale": str(res.data.get("rationale", ""))[:600], "key_risks": res.data.get("key_risks", []),
                "latency_ms": res.latency_ms}

    # ------------------------------------------------------------------ fills → metadata / journal
    async def _on_fill(self, o: dict[str, Any], w: WorkingOrder) -> None:
        if w.purpose not in ("entry", "exit"):
            return
        px = float(w.avg_price or o.get("filled_avg_price") or 0.0)
        qty = float(w.filled_qty or o.get("filled_qty") or 0.0)
        if w.purpose == "entry":
            m = dict(w.meta)
            m.update(strategy_id=w.strategy_id, entry_t=now_ms(), entry_price=px, peak_plpc=0.0)
            self._save_meta(w.symbol, m)
            occ = parse_occ(w.symbol)
            self.store.write(
                "INSERT INTO journal(id,mode,symbol,underlying,strategy_id,asset_class,direction,entry_t,entry_price,qty,data) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (w.client_id, self.mode, w.symbol, occ[0] if occ else w.symbol, w.strategy_id,
                 "option" if occ else "equity", int(w.meta.get("direction", 1)), now_ms(), px, qty,
                 dumps(sanitize(w.meta)).decode()),
            )
        else:
            rows = await self.store.query(
                "SELECT id, entry_price, qty, asset_class FROM journal WHERE mode=? AND symbol=? AND exit_t IS NULL "
                "ORDER BY entry_t DESC LIMIT 1", (self.mode, w.symbol))
            if rows:
                r = rows[0]
                mult = 100 if r["asset_class"] == "option" else 1
                pnl = (px - r["entry_price"]) * qty * mult
                pnl_pct = px / r["entry_price"] - 1 if r["entry_price"] else None
                self.store.write(
                    "UPDATE journal SET exit_t=?, exit_price=?, pnl=?, pnl_pct=?, reason_exit=? WHERE id=?",
                    (now_ms(), px, pnl, pnl_pct, w.meta.get("reason"), r["id"]),
                )

    # ------------------------------------------------------------------ decisions / state
    def _decide(self, strategy_id: str, symbol: str, action: str, reason: str, idea: TradeIdea | None = None,
                llm: dict[str, Any] | None = None, order_id: str | None = None) -> Decision:
        d = Decision(new_id("d-"), now_ms(), strategy_id, symbol, action, reason,  # type: ignore[arg-type]
                     idea.edge_score if idea else None, llm, order_id, sanitize(idea.to_wire()) if idea else None)
        self.decisions.append(d)
        if len(self.decisions) > 1000:
            del self.decisions[:-800]
        self.store.write(
            "INSERT INTO decisions(id,mode,t,strategy_id,symbol,action,reason,edge_score,llm,order_id,idea) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (d.id, self.mode, d.t, strategy_id, symbol, action, reason, d.edge_score,
             dumps(sanitize(llm)).decode() if llm else None, order_id, dumps(d.idea).decode() if d.idea else None),
        )
        self.bus.publish("decision", d)
        return d

    async def recent_decisions(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self.store.query(
            "SELECT id,t,strategy_id,symbol,action,reason,edge_score,llm,order_id FROM decisions WHERE mode=? "
            "ORDER BY t DESC LIMIT ?", (self.mode, limit))
        for r in rows:
            r["llm"] = loads(r["llm"]) if r["llm"] else None
        return rows

    async def strategy_stats(self) -> dict[str, dict[str, Any]]:
        rows = await self.store.query(
            "SELECT strategy_id, COUNT(*) n, SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins, "
            "SUM(CASE WHEN exit_t IS NOT NULL THEN 1 ELSE 0 END) closed, COALESCE(SUM(pnl),0) pnl "
            "FROM journal WHERE mode=? GROUP BY strategy_id", (self.mode,))
        return {r["strategy_id"]: {"trades": r["n"], "win_rate": (r["wins"] / r["closed"]) if r["closed"] else None,
                                   "pnl": r["pnl"]} for r in rows}

    async def today_stats(self) -> dict[str, Any]:
        start = clock.to_ms(clock.et_dt(clock.today_et(), clock.time(0, 0)))
        rows = await self.store.query(
            "SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl FROM journal WHERE mode=? AND entry_t>=?", (self.mode, start))
        return {"trades": rows[0]["n"] if rows else 0, "pnl": rows[0]["pnl"] if rows else 0.0}

    async def state(self) -> dict[str, Any]:
        return {
            "armed": self.armed,
            "running": self.running,
            "killed": self.risk.killed,
            "kill_reason": self.risk.kill_reason,
            "tier": self.tier,
            "tier_locked": self.tier_locked,
            "risk": self.risk.cfg,
            "risk_schema": [p.to_wire() for p in RISK_SCHEMA],
            "last_cycle": self.last_cycle,
            "cycle_ms": self.cycle_ms,
            "today": await self.today_stats(),
            "presets": presets_wire(),
            "preview": self.preview,
            "manage_exits_when_disarmed": self.manage_exits_when_disarmed,
        }

    async def strategies_wire(self) -> list[dict[str, Any]]:
        stats = await self.strategy_stats()
        out = []
        for sid, s in self.strategies.items():
            out.append({
                "id": sid, "name": s.name, "description": s.description, "asset_class": s.asset_class, "style": s.style,
                "enabled": self.enabled[sid], "use_llm_gate": s.use_llm_gate, "params": s.params,
                "schema": [p.to_wire() for p in s.schema()],
                "tiers": [t for t, cfg in TIER_BY_ID.items() if sid in cfg["strategies"]],
                "stats": stats.get(sid, {"trades": 0, "win_rate": None, "pnl": 0.0}),
                "backtest": {"support": s.backtest_support, "note": s.backtest_note},
            })
        return out


def _et_date(ms: int) -> date:
    from datetime import datetime

    return datetime.fromtimestamp(ms / 1000, tz=clock.ET).date()


def _fomc() -> list[tuple[date, bool]]:
    from vantage.data.calendar import FOMC

    return FOMC

"""REST API (docs/API.md is the contract)."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import Response

from vantage.broker.base import BrokerError
from vantage.container import Container
from vantage.core import clock
from vantage.core.types import parse_occ
from vantage.core.util import dumps, loads, now_ms
from vantage.data.market import RANGE_SPEC
from vantage.scanner.options_scanner import SCAN_SCHEMA
from vantage.strategies.presets import TIER_BY_ID

router = APIRouter(prefix="/api")


class JSON(Response):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return dumps(content)


def J(x: Any, status: int = 200) -> JSON:
    return JSON(x, status_code=status)


def C(request: Request) -> Container:
    c = request.app.state.vs.container
    if c is None:
        raise HTTPException(503, "starting up")
    return c


def err(code: str, detail: str, status: int = 400) -> JSON:
    return J({"error": code, "detail": detail}, status)


# ============================================================================ system
@router.get("/status")
async def status(request: Request) -> JSON:
    c = C(request)
    s = c.status()
    s["mode_error"] = request.app.state.vs.mode_error
    return J(s)


@router.post("/mode")
async def set_mode(request: Request, body: dict[str, Any] = Body(...)) -> JSON:
    vs = request.app.state.vs
    mode = body.get("mode")
    if mode not in ("sim", "paper", "live"):
        return err("bad_mode", "mode must be sim, paper or live")
    if mode == "live":
        if not vs.settings.live_allowed:
            return err("live_not_allowed", "Set ALLOW_LIVE_TRADING=true and live API keys in .env, then restart.", 403)
        if body.get("confirm") != "LIVE":
            return err("confirm_required", 'Type LIVE to confirm switching to real-money trading.', 400)
    if mode == "paper" and not vs.settings.paper_keys:
        return err("no_keys", "Paper API keys are not configured in .env", 400)
    c = await vs.switch_mode(mode)
    s = c.status()
    s["mode_error"] = vs.mode_error
    return J(s)


@router.get("/settings")
async def get_settings(request: Request) -> JSON:
    c = C(request)
    return J({"stock_feed": c.stock_feed, "options_feed": c.options_feed, "universe": c.universe(),
              "watchlist": c.watchlist(), "ws_flush_ms": c.ws_flush_ms})


@router.put("/settings")
async def put_settings(request: Request, body: dict[str, Any] = Body(...)) -> JSON:
    vs = request.app.state.vs
    c = C(request)
    feeds_changed = False
    if body.get("stock_feed") in ("iex", "sip") and body["stock_feed"] != c.stock_feed:
        c.stock_feed = body["stock_feed"]
        feeds_changed = True
    if body.get("options_feed") in ("indicative", "opra") and body["options_feed"] != c.options_feed:
        c.options_feed = body["options_feed"]
        feeds_changed = True
    if isinstance(body.get("ws_flush_ms"), (int, float)):
        c.ws_flush_ms = int(max(16, min(1000, body["ws_flush_ms"])))
    await c.set_lists(body.get("universe"), body.get("watchlist"))
    if feeds_changed and c.mode != "sim":
        mode = c.mode
        c._persist_settings()
        vs.container = None
        await c.stop()
        c = await vs.switch_mode(mode)
    return await get_settings(request)


@router.post("/sim/reset")
async def sim_reset(request: Request, body: dict[str, Any] = Body(default={})) -> JSON:
    c = C(request)
    if c.mode != "sim":
        return err("not_sim", "only available in SIM mode")
    eq = float(body.get("equity", 10_000))
    if not 100 <= eq <= 10_000_000:
        return err("bad_equity", "equity must be between $100 and $10M")
    c.store.kv_set("sim", "initial_equity", eq)
    c.store.write("DELETE FROM positions_meta WHERE mode='sim'")
    c.store.write("DELETE FROM journal WHERE mode='sim'")
    c.broker.reset(eq)
    c.autotrader.peak_equity = eq
    c.store.kv_set("sim", "peak_equity", eq)
    c.autotrader.risk.reset_kill()
    return J(await c.broker.get_account())


# ============================================================================ account
@router.get("/account")
async def account(request: Request) -> JSON:
    c = C(request)
    a = await c.broker.get_account()
    a["tier"] = c.autotrader.tier
    return J(a)


@router.get("/positions")
async def positions(request: Request) -> JSON:
    c = C(request)
    if c.positions:
        return J(c.positions)
    return J(await c.broker.get_positions())


@router.post("/positions/{symbol}/close")
async def close_position(request: Request, symbol: str, body: dict[str, Any] = Body(default={})) -> JSON:
    c = C(request)
    pct = float(body.get("pct", 1.0))
    if not 0 < pct <= 1:
        return err("bad_pct", "pct must be in (0, 1]")
    pos = next((p for p in await c.broker.get_positions() if p["symbol"] == symbol.upper()), None)
    if pos is None:
        return err("not_found", f"no position in {symbol}", 404)
    qty = pos["qty"] * pct
    is_opt = pos["asset_class"] == "option"
    if is_opt:
        qty = max(1, int(round(qty)))
    w = await c.execution.submit(pos["symbol"], "sell", qty, is_opt, "exit", pos.get("strategy_id"), aggression=0.5,
                                 max_aggression=1.0, timeout_s=120, meta={"reason": "manual close"},
                                 ref_price=pos["current_price"])
    await asyncio.sleep(0.05)
    return J({"id": w.broker_id, "client_order_id": w.client_id, "symbol": w.symbol, "status": w.status,
              "side": "sell", "qty": qty, "limit_price": w.limit})


@router.get("/orders")
async def orders(request: Request, status: str = "open", limit: int = 100) -> JSON:
    c = C(request)
    rows = await c.broker.get_orders(status, limit)
    meta = await c.store.query("SELECT client_order_id, strategy_id FROM orders_meta WHERE mode=?", (c.mode,))
    sm = {m["client_order_id"]: m["strategy_id"] for m in meta}
    for o in rows:
        o["strategy_id"] = sm.get(o.get("client_order_id"))
    return J(rows)


@router.post("/orders")
async def submit_order(request: Request, body: dict[str, Any] = Body(...)) -> JSON:
    """Manual order ticket. Uses the broker directly (no auto-trader risk sizing) but with sanity checks."""
    c = C(request)
    sym = str(body.get("symbol", "")).upper().strip()
    side = body.get("side")
    typ = body.get("type", "limit")
    if not sym or side not in ("buy", "sell") or typ not in ("market", "limit"):
        return err("bad_order", "symbol, side (buy/sell) and type (market/limit) are required")
    is_opt = parse_occ(sym) is not None
    qty, notional = body.get("qty"), body.get("notional")
    if (qty is None) == (notional is None):
        return err("bad_order", "provide exactly one of qty or notional")
    if is_opt and (notional is not None or float(qty) != int(float(qty))):
        return err("bad_order", "options orders need a whole-number qty")
    if is_opt and typ == "market":
        return err("bad_order", "use a limit order for options (market orders on options are unsafe)")
    if typ == "limit" and not body.get("limit_price"):
        return err("bad_order", "limit_price required for limit orders")
    acct = await c.broker.get_account()
    if side == "buy":
        px = float(body.get("limit_price") or (c.market.price(sym) or 0))
        cost = (float(notional) if notional is not None else float(qty) * px * (100 if is_opt else 1))
        if cost > acct["buying_power"] * 1.001:
            return err("insufficient_bp", f"order cost ${cost:,.2f} exceeds buying power ${acct['buying_power']:,.2f}")
    req = {"symbol": sym, "side": side, "type": typ, "time_in_force": body.get("time_in_force", "day"),
           "limit_price": body.get("limit_price"), "extended_hours": bool(body.get("extended_hours")),
           "asset_class": "option" if is_opt else "equity"}
    if qty is not None:
        req["qty"] = float(qty)
    else:
        req["notional"] = float(notional)
    o = await c.broker.submit_order(req)
    c.store.write(
        "INSERT INTO orders_meta(client_order_id,mode,t,broker_id,symbol,strategy_id,purpose,data) VALUES(?,?,?,?,?,?,?,?)",
        (o.get("client_order_id"), c.mode, now_ms(), o.get("id"), sym, None, "manual", "{}"))
    return J(o)


@router.delete("/orders/{order_id}")
async def cancel_order(request: Request, order_id: str) -> JSON:
    await C(request).broker.cancel_order(order_id)
    return J({"ok": True})


@router.get("/portfolio/history")
async def portfolio_history(request: Request, range: str = Query("1D")) -> JSON:  # noqa: A002
    c = C(request)
    if range not in ("1D", "1W", "1M", "3M", "YTD", "1Y", "ALL"):
        return err("bad_range", "range must be one of 1D 1W 1M 3M YTD 1Y ALL")
    h = await c.broker.get_portfolio_history(range)
    if range == "1D":
        if c.mode == "sim":
            from vantage.broker.sim import SimMarket

            start = SimMarket._day_start(now_ms())
            h["session_bounds"] = {"open": start + int(5.5 * 3600_000), "close": start + 12 * 3600_000}
            h["day_bounds"] = {"start": start, "end": start + 16 * 3600_000}
        else:
            d = clock.last_session_date()
            o, cl = clock.session_bounds_ms(d)
            h["session_bounds"] = {"open": o, "close": cl}
            h["day_bounds"] = {"start": clock.to_ms(clock.et_dt(d, clock.PRE_OPEN)),
                               "end": clock.to_ms(clock.et_dt(d, clock.POST_CLOSE))}
    if c.mode != "sim":
        _append_live_equity(h, c.account or await c.broker.get_account(), range)
    return J(h)


def _append_live_equity(h: dict[str, Any], acct: dict[str, Any], rng: str) -> None:
    """End every range at live equity (overnight/extended sessions move it past the broker's last bar).

    For 1D, if the broker's history is from an earlier ET date (weekend/overnight), the baseline becomes the
    previous close (`last_equity`) so the chart's "Today" change matches the account's day P&L.
    """
    from datetime import datetime

    pts = h.get("points") or []
    now = now_ms()
    eq = float(acct.get("equity") or 0.0)
    if eq <= 0:
        return
    if rng == "1D" and pts:
        last_d = datetime.fromtimestamp(pts[-1]["t"] / 1000, tz=clock.ET).date()
        if last_d < clock.today_et() and acct.get("last_equity"):
            h["base_value"] = float(acct["last_equity"])
            if h.get("day_bounds"):
                h["day_bounds"]["end"] = max(h["day_bounds"]["end"], now)
    if not pts or now - pts[-1]["t"] > 60_000:
        pts.append({"t": now, "v": round(eq, 2)})
    else:
        pts[-1]["v"] = round(eq, 2)
    h["points"] = pts


@router.get("/journal")
async def journal(request: Request, limit: int = 200) -> JSON:
    c = C(request)
    rows = await c.store.query(
        "SELECT id,symbol,strategy_id,asset_class,direction,entry_t,exit_t,entry_price,exit_price,qty,pnl,pnl_pct,"
        "reason_exit FROM journal WHERE mode=? ORDER BY entry_t DESC LIMIT ?", (c.mode, min(limit, 2000)))
    return J(rows)


# ============================================================================ market data
@router.get("/quote/{symbol}")
async def quote(request: Request, symbol: str) -> JSON:
    return J(await C(request).market.quote_snap(symbol))


@router.get("/bars")
async def bars(request: Request, symbol: str, range: str = "1D") -> JSON:  # noqa: A002
    if range not in RANGE_SPEC:
        return err("bad_range", f"range must be one of {', '.join(RANGE_SPEC)}")
    return J(await C(request).market.bars_for_range(symbol, range))


@router.get("/search")
async def search(request: Request, q: str = "") -> JSON:
    if not q.strip():
        return J([])
    return J(await C(request).broker.search_assets(q))


@router.get("/options/expirations")
async def expirations(request: Request, underlying: str) -> JSON:
    exps = await C(request).options.expirations(underlying)
    return J([e.isoformat() for e in exps])


@router.get("/options/chain")
async def chain(request: Request, underlying: str, expiration: str) -> JSON:
    c = C(request)
    try:
        exp = date.fromisoformat(expiration)
    except ValueError:
        return err("bad_date", "expiration must be YYYY-MM-DD")
    spot = await c.market.ensure_price(underlying.upper()) or 0.0
    return J(await c.options.chain_for_expiration(underlying.upper(), exp, spot))


@router.get("/options/bars")
async def option_bars(request: Request, symbol: str, range: str = "1D") -> JSON:  # noqa: A002
    c = C(request)
    if range not in ("1D", "1W", "1M", "3M"):
        return err("bad_range", "range must be 1D, 1W, 1M or 3M")
    tf, days = {"1D": ("1Min", 1), "1W": ("15Min", 7), "1M": ("1Hour", 31), "3M": ("1Day", 92)}[range]
    if range == "1D":
        if c.mode == "sim":
            from vantage.broker.sim import SimMarket

            start = SimMarket._day_start(now_ms())
        else:
            start = clock.to_ms(clock.et_dt(clock.last_session_date(), clock.OPEN))
    else:
        start = now_ms() - days * 86_400_000
    b = await c.broker.get_option_bars(symbol.upper(), tf, start)
    return J({"symbol": symbol.upper(), "range": range, "timeframe": tf, "prev_close": b[0].o if b else None,
              "bars": [x.to_wire() for x in b]})


# ============================================================================ scanner
@router.get("/scanner")
async def scanner_last(request: Request) -> JSON:
    return J(C(request).scanner.last)


@router.get("/scanner/params")
async def scanner_params(request: Request) -> JSON:
    c = C(request)
    out = []
    for p in SCAN_SCHEMA:
        w = p.to_wire()
        w["default"] = c.scanner.params.get(p.name, p.default)
        out.append(w)
    return J(out)


@router.post("/scanner/run")
async def scanner_run(request: Request, body: dict[str, Any] = Body(default={})) -> JSON:
    return J(await C(request).scanner.run(body or None))


# ============================================================================ signals / news / flow
@router.get("/signals")
async def signals(request: Request, symbol: str | None = None) -> JSON:
    return J(C(request).signals.signals_for(symbol.upper() if symbol else None))


@router.get("/signals/models")
async def signal_models(request: Request) -> JSON:
    return J(C(request).signals.model_table())


@router.get("/signals/forecast/{symbol}")
async def forecast(request: Request, symbol: str, horizon: float = 10.0) -> JSON:
    c = C(request)
    s = symbol.upper()
    if not any(k[0] == s for k in c.signals.latest):
        await c.signals.refresh([s], full=True)
    return J(c.signals.forecast(s, max(0.5, min(60.0, horizon))))


@router.get("/news")
async def news(request: Request, symbol: str | None = None, limit: int = 100) -> JSON:
    c = C(request)
    items = c.news.recent(symbol.upper() if symbol else None, min(limit, 200))
    if symbol and len(items) < 5 and c.mode != "sim":
        try:
            fresh = await c.broker.get_news([symbol.upper()], 30)
            for n in fresh:
                c.news._accept(n, publish=False)
            items = c.news.recent(symbol.upper(), min(limit, 200))
        except BrokerError:
            pass
    return J([n.to_wire() for n in items])


@router.get("/flow")
async def flow(request: Request, underlying: str | None = None, limit: int = 200) -> JSON:
    return J([e.to_wire() for e in C(request).flow.recent(underlying.upper() if underlying else None, min(limit, 1000))])


# ============================================================================ calendar
@router.get("/calendar")
async def calendar(request: Request, start: str | None = None, end: str | None = None) -> JSON:
    c = C(request)
    s = date.fromisoformat(start) if start else clock.today_et().replace(day=1)
    e = date.fromisoformat(end) if end else s + timedelta(days=42)
    if (e - s).days > 400:
        return err("range_too_long", "max 400 days")
    focus = set(c.all_symbols()) | {p["underlying"] for p in c.positions}
    return J(await c.calendar.events(s, e, focus))


@router.post("/calendar")
async def calendar_add(request: Request, body: dict[str, Any] = Body(...)) -> JSON:
    if not body.get("date") or not body.get("title"):
        return err("bad_event", "date and title required")
    try:
        date.fromisoformat(body["date"])
    except ValueError:
        return err("bad_date", "date must be YYYY-MM-DD")
    return J(await C(request).calendar.add_user_event(body))


@router.delete("/calendar/{eid}")
async def calendar_delete(request: Request, eid: str) -> JSON:
    if not eid.startswith("user-"):
        return err("not_user_event", "only user events can be deleted", 400)
    await C(request).calendar.delete_user_event(eid)
    return J({"ok": True})


# ============================================================================ strategies & auto-trader
@router.get("/strategies")
async def strategies(request: Request) -> JSON:
    return J(await C(request).autotrader.strategies_wire())


@router.put("/strategies/{sid}")
async def strategy_update(request: Request, sid: str, body: dict[str, Any] = Body(...)) -> JSON:
    at = C(request).autotrader
    try:
        at.update_strategy(sid, body.get("enabled"), body.get("params"), body.get("use_llm_gate"))
    except KeyError:
        return err("not_found", f"unknown strategy {sid}", 404)
    except (ValueError, TypeError) as e:
        return err("bad_params", str(e))
    return J(next(s for s in await at.strategies_wire() if s["id"] == sid))


@router.get("/autotrader")
async def autotrader(request: Request) -> JSON:
    return J(await C(request).autotrader.state())


@router.put("/autotrader/risk")
async def autotrader_risk(request: Request, body: dict[str, Any] = Body(...)) -> JSON:
    at = C(request).autotrader
    try:
        at.update_risk(body)
    except (ValueError, TypeError) as e:
        return err("bad_params", str(e))
    if "preview" in body:
        at.preview = bool(body["preview"])
    if "manage_exits_when_disarmed" in body:
        at.manage_exits_when_disarmed = bool(body["manage_exits_when_disarmed"])
    return J(await at.state())


@router.post("/autotrader/preset")
async def autotrader_preset(request: Request, body: dict[str, Any] = Body(...)) -> JSON:
    tier = body.get("tier")
    at = C(request).autotrader
    if tier == "auto":
        at.tier_locked = False
        at.store.kv_set(at.mode, "tier_locked", False)
        return J(await at.state())
    if tier not in TIER_BY_ID:
        return err("bad_tier", f"tier must be one of {', '.join(TIER_BY_ID)} or auto")
    at.apply_preset(tier)
    return J(await at.state())


@router.post("/autotrader/arm")
async def autotrader_arm(request: Request, body: dict[str, Any] = Body(...)) -> JSON:
    c = C(request)
    armed = bool(body.get("armed"))
    if armed and c.mode == "live" and body.get("confirm") != "LIVE":
        return err("confirm_required", "Type LIVE to arm the auto-trader with real money.")
    try:
        await c.autotrader.set_armed(armed)
    except PermissionError as e:
        return err("killed", str(e), 409)
    return J(await c.autotrader.state())


@router.post("/autotrader/kill")
async def autotrader_kill(request: Request, body: dict[str, Any] = Body(default={})) -> JSON:
    c = C(request)
    await c.autotrader.kill("manual kill switch", flatten=bool(body.get("flatten")))
    return J(await c.autotrader.state())


@router.post("/autotrader/reset-kill")
async def autotrader_reset_kill(request: Request) -> JSON:
    c = C(request)
    c.autotrader.risk.reset_kill()
    acct = await c.broker.get_account()
    c.autotrader.peak_equity = acct["equity"]
    c.store.kv_set(c.mode, "peak_equity", acct["equity"])
    return J(await c.autotrader.state())


@router.post("/autotrader/cycle")
async def autotrader_cycle(request: Request) -> JSON:
    at = C(request).autotrader
    await at.cycle()
    return J(await at.state())


@router.get("/autotrader/decisions")
async def decisions(request: Request, limit: int = 200) -> JSON:
    return J(await C(request).autotrader.recent_decisions(min(limit, 1000)))


# ============================================================================ LLM
@router.get("/llm")
async def llm_info(request: Request) -> JSON:
    llm = C(request).llm
    await llm.probe()
    return J(llm.info())


@router.put("/llm")
async def llm_update(request: Request, body: dict[str, Any] = Body(...)) -> JSON:
    c = C(request)
    c.llm.configure(**{k: body.get(k) for k in ("enabled", "model", "base_url", "temperature")})
    c.store.kv_set("global", "llm", {"enabled": c.llm.enabled, "model": c.llm.model, "base_url": c.llm.base_url,
                                     "temperature": c.llm.temperature})
    await c.llm.probe(force=True)
    return J(c.llm.info())


@router.get("/llm/calls")
async def llm_calls(request: Request, limit: int = 200, purpose: str | None = None, status: str | None = None,
                    q: str | None = None, before: int | None = None) -> JSON:
    """Summaries of logged LLM exchanges (newest first) for the LLM Log page."""
    from vantage.llm.gateway import call_preview

    c = C(request)
    where, args = ["mode=?"], [c.mode]
    if purpose:
        where.append("purpose=?")
        args.append(purpose)
    if status == "ok":
        where.append("ok=1")
    elif status == "fail":
        where.append("ok=0")
    elif status == "cached":
        where.append("cached=1")
    if before:
        where.append("t<?")
        args.append(int(before))
    if q and q.strip():
        like = f"%{q.strip()[:100]}%"
        where.append("(request LIKE ? OR response LIKE ? OR error LIKE ? OR parsed LIKE ?)")
        args += [like, like, like, like]
    rows = await c.store.query(
        "SELECT id,t,purpose,model,latency_ms,ok,cached,prompt_tokens,completion_tokens,error,parsed,"
        f"substr(response,1,600) AS head FROM llm_calls WHERE {' AND '.join(where)} ORDER BY t DESC LIMIT ?",
        (*args, max(1, min(limit, 1000))),
    )
    out = []
    for r in rows:
        parsed = loads(r["parsed"]) if r["parsed"] else None
        out.append({
            "id": r["id"], "t": r["t"], "purpose": r["purpose"], "model": r["model"], "latency_ms": r["latency_ms"],
            "ok": bool(r["ok"]), "cached": bool(r["cached"]), "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"], "error": r["error"],
            "preview": call_preview(parsed, r["head"] or "", r["error"]),
        })
    counts = await c.store.query(
        "SELECT purpose, COUNT(*) n, SUM(ok=0) fails, AVG(CASE WHEN ok=1 AND cached=0 THEN latency_ms END) avg_ms "
        "FROM llm_calls WHERE mode=? GROUP BY purpose", (c.mode,))
    return J({"calls": out, "by_purpose": counts})


@router.get("/llm/calls/{call_id}")
async def llm_call_detail(request: Request, call_id: str) -> JSON:
    from vantage.llm.gateway import _extract_json

    c = C(request)
    rows = await c.store.query(
        "SELECT id,t,purpose,model,latency_ms,ok,cached,prompt_tokens,completion_tokens,error,parsed,system,"
        "request,response FROM llm_calls WHERE id=? AND mode=?", (call_id, c.mode))
    if not rows:
        return err("not_found", "no such LLM call", 404)
    r = rows[0]
    return J({
        "id": r["id"], "t": r["t"], "purpose": r["purpose"], "model": r["model"], "latency_ms": r["latency_ms"],
        "ok": bool(r["ok"]), "cached": bool(r["cached"]), "prompt_tokens": r["prompt_tokens"],
        "completion_tokens": r["completion_tokens"], "error": r["error"],
        "system": r["system"], "user": r["request"], "response": r["response"],
        "parsed": loads(r["parsed"]) if r["parsed"] else (_extract_json(r["response"] or "") if r["ok"] else None),
    })


@router.post("/llm/test")
async def llm_test(request: Request, body: dict[str, Any] = Body(default={})) -> JSON:
    return J(await C(request).llm.test(body.get("prompt")))


# ============================================================================ backtest
@router.post("/backtest")
async def backtest(request: Request, body: dict[str, Any] = Body(...)) -> JSON:
    from vantage.backtest.engine import run_backtest

    c = C(request)
    sid = body.get("strategy_id")
    syms = [s.upper() for s in body.get("symbols", []) if isinstance(s, str) and s.strip()][:30]
    if not sid or not syms:
        return err("bad_request", "strategy_id and symbols required")
    try:
        start = date.fromisoformat(body.get("start") or (clock.today_et() - timedelta(days=730)).isoformat())
        end = date.fromisoformat(body.get("end") or clock.today_et().isoformat())
    except ValueError:
        return err("bad_date", "dates must be YYYY-MM-DD")
    if end <= start:
        return err("bad_date", "end must be after start")
    trials_key = f"bt_trials:{sid}"
    seen = set(c.store.kv_get(c.mode, trials_key, []) or [])
    seen.add(dumps({"p": body.get("params") or {}, "s": sorted(syms)}).decode())
    c.store.kv_set(c.mode, trials_key, sorted(seen)[-500:])
    try:
        res = await run_backtest(c, sid, syms, start, end, body.get("params") or {},
                                 float(body.get("initial_equity") or 10_000), n_trials=len(seen),
                                 iv_rv=max(0.5, min(2.0, float(body.get("iv_rv") or 1.10))),
                                 sizing=str(body.get("sizing") or "equal_weight"))
    except KeyError:
        return err("not_found", f"unknown strategy {sid}", 404)
    except ValueError as e:
        return err("bad_request", str(e))
    return J(res)


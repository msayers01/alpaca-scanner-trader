import { memo, useCallback, useEffect, useMemo, useState } from "react";
import { api, errorMessage } from "../lib/api";
import type { RiskConfig, RiskUpdate, StrategyInfo, TierPreset } from "../lib/types";
import { fmtAgo, fmtPct, fmtUsd } from "../lib/format";
import { useAsync } from "../hooks/useAsync";
import { useNow } from "../hooks/useNow";
import { useOnReconnect } from "../hooks/useOnReconnect";
import { useAutoTraderStore } from "../store/autotrader";
import { useAccountStore } from "../store/account";
import { useFeedsStore } from "../store/feeds";
import { useStatusStore } from "../store/status";
import { ArmButton, AutoTraderStatusLine, KillButton, RunCycleButton } from "../components/AutoTraderControls";
import { DecisionItem } from "../components/DecisionItem";
import { ModeBadge } from "../components/StatusBits";
import { ConfirmDialog } from "../components/ui/Modal";
import { diffParams, ParamForm, type ParamValues } from "../components/ui/ParamForm";
import { Empty, ErrorBox, Loading, Switch, Tip } from "../components/ui/States";
import { Icon } from "../components/ui/Icon";
import { toast } from "../components/ui/Toast";
import { RagsPanel } from "../components/RagsPanel";

const AUTO_PRESET: TierPreset = {
  tier: "auto",
  label: "Automatic",
  description: "Follow account equity: the tier (and its strategy/risk preset) switches automatically as equity crosses the tier boundaries.",
  min_equity: 0,
  max_equity: null,
};

function PresetPicker({ presets, current, tierLocked }: { presets: TierPreset[]; current: string; tierLocked?: boolean }) {
  const setAt = useAutoTraderStore((s) => s.set);
  const equity = useAccountStore((s) => s.account?.equity ?? null);
  const [pending, setPending] = useState<TierPreset | null>(null);
  if (!presets.length) return <Empty>No presets.</Empty>;
  const autoActive = tierLocked === false;
  return (
    <>
      {tierLocked !== undefined && (
        <div className="row small muted" style={{ marginBottom: 10 }}>
          {autoActive ? (
            <span>
              Tier follows equity automatically (currently <strong style={{ color: "var(--text)" }}>{current}</strong>).
            </span>
          ) : (
            <>
              <span>
                Tier locked to <strong style={{ color: "var(--text)" }}>{current}</strong>.
              </span>
              <button className="btn btn-xs" onClick={() => setPending(AUTO_PRESET)}>
                Follow equity automatically
              </button>
            </>
          )}
        </div>
      )}
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))" }}>
        {presets.map((p) => {
          const fits = equity !== null && equity >= p.min_equity && (p.max_equity === null || equity < p.max_equity);
          return (
            <button
              key={p.tier}
              className={`preset${p.tier === current ? " active" : ""}`}
              onClick={() => (p.tier !== current || autoActive) && setPending(p)}
              aria-pressed={p.tier === current}
            >
              <div className="title">
                {p.label}
                {p.tier === current && <span className="badge accent">{autoActive ? "auto" : "active"}</span>}
                {p.manual ? <span className="badge danger">high risk</span> : fits && p.tier !== current && <span className="badge outline">fits equity</span>}
              </div>
              <div className="xsmall faint num">
                {p.manual ? "Manual · any balance" : `${fmtUsd(p.min_equity, { decimals: 0 })} – ${p.max_equity === null ? "∞" : fmtUsd(p.max_equity, { decimals: 0 })}`}
              </div>
              <div className="desc">{p.description}</div>
            </button>
          );
        })}
      </div>
      {pending && (
        <ConfirmDialog
          title={`Apply the ${pending.label} preset?`}
          confirmLabel="Apply preset"
          typeToConfirm={pending.manual ? "RISK" : undefined}
          onClose={() => setPending(null)}
          onConfirm={async () => {
            try {
              setAt(await api.applyPreset(pending.tier));
              toast.success(`${pending.label} preset applied`);
            } catch (e) {
              throw new Error(errorMessage(e));
            }
          }}
        >
          <p className="muted">{pending.description}</p>
          <p className="muted">
            {pending.tier === "auto"
              ? "The preset will be re-applied whenever equity moves into a different tier."
              : "This overwrites the global risk parameters and the default strategy selection, and locks the tier. You can fine-tune afterwards."}
          </p>
        </ConfirmDialog>
      )}
    </>
  );
}

/** Addition toggles: preview decisions while disarmed, manage exits while disarmed. */
function EngineToggles() {
  const state = useAutoTraderStore((s) => s.state);
  const setAt = useAutoTraderStore((s) => s.set);
  const [busy, setBusy] = useState(false);
  if (!state) return null;
  const put = async (body: RiskUpdate) => {
    setBusy(true);
    try {
      setAt(await api.updateRisk(body));
    } catch (e) {
      toast.error(`Update failed: ${errorMessage(e)}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="row-wrap" style={{ marginTop: 12, gap: 24 }}>
      {state.preview !== undefined && (
        <Switch
          checked={state.preview}
          disabled={busy}
          onChange={(v) => void put({ preview: v })}
          label={
            <span className="small">
              Preview while disarmed <Tip text="When disarmed, run the pipeline anyway and log would-be entries as 'skip' decisions prefixed 'preview (disarmed):'. No orders are sent." />
            </span>
          }
        />
      )}
      {state.manage_exits_when_disarmed !== undefined && (
        <Switch
          checked={state.manage_exits_when_disarmed}
          disabled={busy}
          onChange={(v) => void put({ manage_exits_when_disarmed: v })}
          label={
            <span className="small">
              Manage exits while disarmed <Tip text="Keep applying stops, targets and time stops to positions the auto-trader opened, even when it is disarmed (no new entries)." />
            </span>
          }
        />
      )}
    </div>
  );
}

function RiskForm() {
  const state = useAutoTraderStore((s) => s.state);
  const setAt = useAutoTraderStore((s) => s.set);
  const [draft, setDraft] = useState<ParamValues>({});
  const [saving, setSaving] = useState(false);
  const base = useMemo(() => (state?.risk ?? {}) as unknown as ParamValues, [state?.risk]);
  useEffect(() => setDraft(base), [base]);
  const changes = useMemo(() => diffParams(draft, base), [draft, base]);
  const onChange = useCallback((n: string, v: unknown) => setDraft((d) => ({ ...d, [n]: v })), []);
  if (!state) return null;
  const n = Object.keys(changes).length;
  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Global risk parameters</span>
        <Tip text="Hard limits enforced by the risk manager on every order, regardless of strategy or LLM. See Help → Risk parameters." />
        <span className="spacer" />
        {n > 0 && <span className="small muted">{n} unsaved</span>}
        <button className="btn btn-sm btn-ghost" disabled={!n || saving} onClick={() => setDraft(base)}>
          Revert
        </button>
        <button
          className="btn btn-sm btn-primary"
          disabled={!n || saving}
          onClick={async () => {
            setSaving(true);
            try {
              setAt(await api.updateRisk(changes as Partial<RiskConfig>));
              toast.success("Risk parameters saved");
            } catch (e) {
              toast.error(`Save failed: ${errorMessage(e)}`);
            } finally {
              setSaving(false);
            }
          }}
        >
          {saving && <span className="spinner" />}
          Save
        </button>
      </div>
      <div className="card-body">
        <ParamForm schema={state.risk_schema} values={draft} onChange={onChange} baseline={base} columns />
      </div>
    </div>
  );
}

const StrategyCard = memo(function StrategyCard({ s, onUpdated }: { s: StrategyInfo; onUpdated: (s: StrategyInfo) => void }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<ParamValues>(s.params);
  const [busy, setBusy] = useState(false);
  useEffect(() => setDraft(s.params), [s.params]);
  const changes = useMemo(() => diffParams(draft, s.params), [draft, s.params]);
  const onChange = useCallback((n: string, v: unknown) => setDraft((d) => ({ ...d, [n]: v })), []);
  const put = async (body: Parameters<typeof api.updateStrategy>[1], msg: string) => {
    setBusy(true);
    try {
      onUpdated(await api.updateStrategy(s.id, body));
      toast.success(msg);
    } catch (e) {
      toast.error(`${s.name}: ${errorMessage(e)}`);
    } finally {
      setBusy(false);
    }
  };
  const n = Object.keys(changes).length;
  return (
    <div className="card strategy-card">
      <div className="card-body">
        <div className="row" style={{ alignItems: "flex-start" }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="row">
              <h3>{s.name}</h3>
              <span className="faint small mono">{s.id}</span>
            </div>
            <div className="meta">
              <span className="badge outline">{s.asset_class}</span>
              <span className="badge outline">{s.style}</span>
              {s.tiers.map((t) => (
                <span key={t} className="badge">
                  {t}
                </span>
              ))}
            </div>
          </div>
          <Switch checked={s.enabled} disabled={busy} onChange={(v) => void put({ enabled: v }, `${s.name} ${v ? "enabled" : "disabled"}`)} label={s.enabled ? "On" : "Off"} />
        </div>
        <div className="desc">{s.description}</div>
        {s.id === "rags_to_riches" && <RagsPanel s={s} />}
        <div className="row-wrap" style={{ marginTop: 12, gap: 16 }}>
          <Switch
            checked={s.use_llm_gate}
            disabled={busy}
            onChange={(v) => void put({ use_llm_gate: v }, `LLM gate ${v ? "on" : "off"} for ${s.name}`)}
            label={
              <span className="small">
                LLM gate <Tip text="Send each idea to the local LLM for review. The LLM can only approve, reject, or shrink size (multiplier ≤ 1) — never add trades or bypass risk." />
              </span>
            }
          />
          <span className="spacer" />
          <span className="small muted num">
            {s.stats.trades} trades · win {s.stats.win_rate === null ? "—" : fmtPct(s.stats.win_rate, { decimals: 0 })} ·{" "}
            <span className={s.stats.pnl >= 0 ? "up" : "down"}>{fmtUsd(s.stats.pnl, { signed: true })}</span>
          </span>
        </div>
        <div className="row" style={{ marginTop: 12 }}>
          <button className="btn btn-sm btn-ghost" onClick={() => setOpen((o) => !o)} aria-expanded={open} disabled={!s.schema.length}>
            <Icon name={open ? "chevron-down" : "chevron-right"} size={12} /> Parameters ({s.schema.length})
          </button>
          <span className="spacer" />
          {open && n > 0 && (
            <>
              <button className="btn btn-sm btn-ghost" onClick={() => setDraft(s.params)} disabled={busy}>
                Revert
              </button>
              <button className="btn btn-sm btn-primary" onClick={() => void put({ params: changes }, `${s.name} parameters saved`)} disabled={busy}>
                Save {n}
              </button>
            </>
          )}
        </div>
        {open && (
          <div style={{ marginTop: 8 }}>
            <ParamForm schema={s.schema} values={draft} onChange={onChange} baseline={s.params} />
          </div>
        )}
      </div>
    </div>
  );
});

function DecisionLog() {
  const decisions = useFeedsStore((s) => s.decisions);
  const loaded = useFeedsStore((s) => s.loaded.decisions);
  const [strategy, setStrategy] = useState("");
  const [action, setAction] = useState("");
  useEffect(() => {
    void useFeedsStore.getState().ensureLoaded("decisions");
  }, []);
  const strategies = useMemo(() => [...new Set(decisions.map((d) => d.strategy_id))].sort(), [decisions]);
  const list = useMemo(() => decisions.filter((d) => (!strategy || d.strategy_id === strategy) && (!action || d.action === action)).slice(0, 100), [decisions, strategy, action]);
  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Decision log</span>
        <span className="spacer" />
        <select className="select input-sm" style={{ width: 160 }} value={strategy} onChange={(e) => setStrategy(e.target.value)} aria-label="Strategy filter">
          <option value="">All strategies</option>
          {strategies.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select className="select input-sm" style={{ width: 120 }} value={action} onChange={(e) => setAction(e.target.value)} aria-label="Action filter">
          <option value="">All actions</option>
          {["enter", "exit", "skip", "reject", "error"].map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
      </div>
      <div style={{ maxHeight: 640, overflowY: "auto" }}>
        {!loaded && !list.length ? <Loading /> : list.length ? list.map((d) => <DecisionItem key={d.id} d={d} />) : <Empty>No decisions match.</Empty>}
      </div>
    </div>
  );
}

export default function AutoTrader() {
  const state = useAutoTraderStore((s) => s.state);
  const err = useAutoTraderStore((s) => s.error);
  const mode = useStatusStore((s) => s.status?.mode);
  const now = useNow(5000);
  const strategies = useAsync((signal) => api.strategies({ signal }), [mode]);
  useEffect(() => {
    void useAutoTraderStore.getState().load();
  }, [mode]);
  useOnReconnect(() => {
    void useAutoTraderStore.getState().load();
    strategies.reload();
  });
  const setStrategies = strategies.setData;
  const onUpdated = useCallback((s: StrategyInfo) => setStrategies((list) => (list ?? []).map((x) => (x.id === s.id ? s : x))), [setStrategies]);

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1 className="row">
            Auto-trader <ModeBadge mode={mode} large />
          </h1>
          <div className="sub">Universe → strategy ideas → LLM gate → risk manager → smart limit execution. Starts disarmed after every restart or mode switch.</div>
        </div>
      </div>

      {err && !state && <ErrorBox error={err} onRetry={() => void useAutoTraderStore.getState().load()} />}

      <div className="card" style={{ marginBottom: 20 }}>
        <div className="card-body">
          <div className="row-wrap" style={{ gap: 16 }}>
            <AutoTraderStatusLine />
            <span className="spacer" />
            <ArmButton />
            <RunCycleButton />
            <KillButton />
          </div>
          {state && (
            <div className="row-wrap small muted num" style={{ marginTop: 12, gap: 20 }}>
              <span>Engine {state.running ? "running" : "idle"}</span>
              <span>Last cycle {state.last_cycle ? fmtAgo(state.last_cycle, now) : "never"}</span>
              <span>Cycle time {state.cycle_ms !== null ? `${state.cycle_ms} ms` : "—"}</span>
              <span>Every {state.risk.cycle_seconds}s</span>
              <span>Max {state.risk.max_trades_per_day} trades/day</span>
              <span>Daily loss stop {fmtPct(state.risk.max_daily_loss, { decimals: 1 })}</span>
            </div>
          )}
          {state && (state.preview !== undefined || state.manage_exits_when_disarmed !== undefined) && <EngineToggles />}
          {mode === "live" && (
            <div className="notice danger" style={{ marginTop: 12 }}>
              LIVE mode: arming requires typing LIVE and every order uses real money.
            </div>
          )}
        </div>
      </div>

      <div className="stack-lg">
        <section>
          <div className="section-title">Account tier preset</div>
          {state ? <PresetPicker presets={state.presets} current={state.tier} tierLocked={state.tier_locked} /> : <Loading />}
        </section>

        <RiskForm />

        <section>
          <div className="section-title">Strategies</div>
          {strategies.error ? (
            <ErrorBox error={strategies.error} onRetry={strategies.reload} />
          ) : !strategies.data ? (
            <Loading />
          ) : strategies.data.length === 0 ? (
            <Empty>No strategies registered.</Empty>
          ) : (
            <div className="grid-2">
              {strategies.data.map((s) => (
                <StrategyCard key={s.id} s={s} onUpdated={onUpdated} />
              ))}
            </div>
          )}
        </section>

        <DecisionLog />
      </div>
    </div>
  );
}

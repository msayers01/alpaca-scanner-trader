import { useEffect, useMemo, useState } from "react";
import { api, errorMessage } from "../lib/api";
import type { LlmInfo, LlmTestResult, Mode, Settings as SettingsT } from "../lib/types";
import { useAsync } from "../hooks/useAsync";
import { useAccountStore } from "../store/account";
import { useAutoTraderStore } from "../store/autotrader";
import { useStatusStore } from "../store/status";
import { useUiStore } from "../store/ui";
import { ModeBadge } from "../components/StatusBits";
import { ConfirmDialog } from "../components/ui/Modal";
import { NumberInput } from "../components/ui/ParamForm";
import { Empty, ErrorBox, Loading, Switch, Tip } from "../components/ui/States";
import { Icon } from "../components/ui/Icon";
import { toast } from "../components/ui/Toast";

const MODES: { id: Mode; title: string; desc: string }[] = [
  { id: "sim", title: "Simulation", desc: "Synthetic market and broker. No keys, no network. For development and learning the UI — never judge strategy profitability here." },
  { id: "paper", title: "Paper", desc: "Alpaca paper account: real market data, fake money. Use for forward-testing strategies." },
  { id: "live", title: "Live", desc: "Alpaca live account: real money. Requires ALLOW_LIVE_TRADING=true and live keys in the backend .env." },
];

function ModeSwitcher() {
  const status = useStatusStore((s) => s.status);
  const [target, setTarget] = useState<Mode | null>(null);
  if (!status) return <Loading label="Waiting for backend" />;
  const modeError = status.mode_error;
  const disabledReason = (m: Mode): string | null => {
    if (m === "paper" && !status.paper_keys) return "Set ALPACA_PAPER_KEY / ALPACA_PAPER_SECRET in .env and restart the backend.";
    if (m === "live" && !status.live_keys) return "Set ALPACA_LIVE_KEY / ALPACA_LIVE_SECRET in .env and restart the backend.";
    if (m === "live" && !status.live_allowed) return "Live trading is locked: set ALLOW_LIVE_TRADING=true in .env and restart the backend.";
    return null;
  };
  return (
    <>
      {modeError && (
        <div className="notice danger" style={{ marginBottom: 12 }}>
          <strong>Mode switch failed:</strong> {modeError} — the backend fell back to SIM.
        </div>
      )}
      <div className="grid-3">
        {MODES.map((m) => {
          const reason = disabledReason(m.id);
          const active = status.mode === m.id;
          return (
            <button key={m.id} className={`preset${active ? " active" : ""}`} disabled={!!reason || active} onClick={() => setTarget(m.id)} aria-pressed={active} style={reason ? { opacity: 0.55, cursor: "not-allowed" } : undefined}>
              <div className="title">
                <ModeBadge mode={m.id} /> {m.title}
                {active && <span className="badge accent">current</span>}
              </div>
              <div className="desc">{m.desc}</div>
              {reason && (
                <div className="desc warn-text" style={{ marginTop: 6 }}>
                  <Icon name="shield" size={12} /> {reason}
                </div>
              )}
            </button>
          );
        })}
      </div>
      {target && (
        <ConfirmDialog
          title={
            <span className="row">
              Switch to <ModeBadge mode={target} />
            </span>
          }
          confirmLabel={target === "live" ? "Switch to LIVE" : `Switch to ${target}`}
          tone={target === "live" ? "danger" : "primary"}
          typeToConfirm={target === "live" ? "LIVE" : undefined}
          onClose={() => setTarget(null)}
          onConfirm={async () => {
            try {
              const s = await api.setMode(target === "live" ? { mode: "live", confirm: "LIVE" } : { mode: target });
              useStatusStore.getState().setStatus(s);
              void useAccountStore.getState().loadAll();
              void useAutoTraderStore.getState().load();
              toast.success(`Now in ${s.mode.toUpperCase()} mode`);
            } catch (e) {
              throw new Error(errorMessage(e));
            }
          }}
        >
          <p className="muted">
            Switching mode stops and disarms the auto-trader, closes all streams and rebuilds the broker and data services. Positions, orders and history shown afterwards
            belong to the new mode only.
          </p>
          {target === "live" && (
            <div className="notice danger">
              <strong>Real money.</strong> Manual orders and (once armed) the auto-trader will trade your live Alpaca account.
            </div>
          )}
        </ConfirmDialog>
      )}
    </>
  );
}

function SimReset() {
  const mode = useStatusStore((s) => s.status?.mode);
  const [equity, setEquity] = useState(10_000);
  const [open, setOpen] = useState(false);
  if (mode !== "sim") return null;
  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Simulator</span>
        <ModeBadge mode="sim" />
      </div>
      <div className="card-body row-wrap" style={{ gap: 16 }}>
        <span className="small muted" style={{ flex: 1, minWidth: 260 }}>
          Reset the simulated account: cash is set to the starting equity and all SIM positions, orders and journal entries are cleared. Paper and live data are not
          touched.
        </span>
        <div className="input-group" style={{ width: 160 }}>
          <span className="addon">$</span>
          <NumberInput value={equity} onChange={setEquity} min={100} max={10_000_000} className="input input-num" ariaLabel="Starting equity" />
        </div>
        <button className="btn" onClick={() => setOpen(true)} disabled={!(equity >= 100 && equity <= 10_000_000)}>
          Reset simulator
        </button>
      </div>
      {open && (
        <ConfirmDialog
          title="Reset the simulator?"
          confirmLabel="Reset"
          tone="danger"
          onClose={() => setOpen(false)}
          onConfirm={async () => {
            try {
              const a = await api.simReset(equity);
              useAccountStore.getState().setAccount(a);
              void useAccountStore.getState().loadAll();
              toast.success(`Simulator reset to $${equity.toLocaleString("en-US")}`);
            } catch (e) {
              throw new Error(errorMessage(e));
            }
          }}
        >
          <p className="muted">All SIM positions, orders and the SIM trade journal will be cleared.</p>
        </ConfirmDialog>
      )}
    </div>
  );
}

function SymbolListEditor({ label, value, onChange, help }: { label: string; value: string[]; onChange: (v: string[]) => void; help?: string }) {
  const [text, setText] = useState("");
  const add = () => {
    const syms = text
      .split(/[\s,;]+/)
      .map((s) => s.trim().toUpperCase())
      .filter((s) => s && /^[A-Z0-9.\-/]{1,12}$/.test(s));
    if (!syms.length) return;
    onChange([...value, ...syms.filter((s) => !value.includes(s))]);
    setText("");
  };
  return (
    <div className="field">
      <span className="label">
        {label} ({value.length}) {help && <Tip text={help} />}
      </span>
      <div className="row-wrap" style={{ gap: 6, minHeight: 28 }}>
        {value.length === 0 && <span className="faint small">Empty</span>}
        {value.map((s) => (
          <span key={s} className="chip">
            {s}
            <button aria-label={`Remove ${s}`} onClick={() => onChange(value.filter((x) => x !== s))}>
              <Icon name="close" size={10} />
            </button>
          </span>
        ))}
      </div>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          add();
        }}
      >
        <input className="input input-sm" placeholder="Add symbols (comma or space separated)" value={text} onChange={(e) => setText(e.target.value)} aria-label={`Add to ${label}`} />
        <button className="btn btn-sm" type="submit" disabled={!text.trim()}>
          Add
        </button>
        {value.length > 0 && (
          <button className="btn btn-sm btn-ghost" type="button" onClick={() => onChange([])}>
            Clear
          </button>
        )}
      </form>
    </div>
  );
}

function DataSettings() {
  const settings = useStatusStore((s) => s.settings);
  const err = useStatusStore((s) => s.settingsError);
  const save = useStatusStore((s) => s.saveSettings);
  const [draft, setDraft] = useState<SettingsT | null>(settings);
  const [saving, setSaving] = useState(false);
  useEffect(() => setDraft(settings), [settings]);
  useEffect(() => {
    if (!settings) void useStatusStore.getState().loadSettings();
  }, [settings]);

  const changes = useMemo(() => {
    if (!draft || !settings) return {};
    const out: Partial<SettingsT> = {};
    (Object.keys(draft) as (keyof SettingsT)[]).forEach((k) => {
      if (JSON.stringify(draft[k]) !== JSON.stringify(settings[k])) (out as Record<string, unknown>)[k] = draft[k];
    });
    return out;
  }, [draft, settings]);
  const n = Object.keys(changes).length;

  if (!draft) return err ? <ErrorBox error={err} onRetry={() => void useStatusStore.getState().loadSettings()} /> : <Loading />;

  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Market data &amp; symbols</span>
        <span className="spacer" />
        {n > 0 && <span className="small muted">{n} unsaved</span>}
        <button className="btn btn-sm btn-ghost" disabled={!n || saving} onClick={() => setDraft(settings)}>
          Revert
        </button>
        <button
          className="btn btn-sm btn-primary"
          disabled={!n || saving}
          onClick={async () => {
            setSaving(true);
            try {
              const s = await save(changes);
              useUiStore.getState().setWatchlist(s.watchlist);
              toast.success("Settings saved");
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
      <div className="card-body stack-lg">
        <div className="grid-2">
          <div className="field">
            <span className="label">
              Stock feed <Tip text="IEX is free but covers only one exchange (~2–3% of volume) so quotes are a subset of the NBBO. SIP is the consolidated tape (Algo Trader Plus subscription)." />
            </span>
            <div className="seg" role="radiogroup" aria-label="Stock feed">
              {(["iex", "sip"] as const).map((f) => (
                <button key={f} role="radio" aria-checked={draft.stock_feed === f} className={draft.stock_feed === f ? "active" : ""} onClick={() => setDraft({ ...draft, stock_feed: f })}>
                  {f.toUpperCase()}
                </button>
              ))}
            </div>
          </div>
          <div className="field">
            <span className="label">
              Options feed <Tip text="'indicative' is free: delayed trades and modified quotes, so flow uses snapshot-based unusual activity. 'opra' is the real-time consolidated options feed (subscription) enabling sweep detection." />
            </span>
            <div className="seg" role="radiogroup" aria-label="Options feed">
              {(["indicative", "opra"] as const).map((f) => (
                <button key={f} role="radio" aria-checked={draft.options_feed === f} className={draft.options_feed === f ? "active" : ""} onClick={() => setDraft({ ...draft, options_feed: f })}>
                  {f === "opra" ? "OPRA" : "Indicative"}
                </button>
              ))}
            </div>
          </div>
        </div>
        <SymbolListEditor label="Universe" value={draft.universe} onChange={(v) => setDraft({ ...draft, universe: v })} help="Symbols the scanner and strategies evaluate every cycle. Larger universes mean slower scans and more API calls." />
        <SymbolListEditor label="Watchlist" value={draft.watchlist} onChange={(v) => setDraft({ ...draft, watchlist: v })} help="Shown with live prices on the dashboard." />
        <div className="field" style={{ maxWidth: 260 }}>
          <span className="label">
            WebSocket flush interval (ms) <Tip text="How often the backend batches updates to the browser. Lower = snappier but more CPU; 50 ms (20 updates/s per channel) is the default." />
          </span>
          <NumberInput value={draft.ws_flush_ms} onChange={(v) => setDraft({ ...draft, ws_flush_ms: v })} min={10} max={2000} integer className="input input-num" ariaLabel="Flush interval" />
        </div>
      </div>
    </div>
  );
}

function LlmPanel() {
  const llm = useAsync((signal) => api.llm({ signal }), []);
  const [draft, setDraft] = useState<{ enabled: boolean; model: string; base_url: string; temperature: number | null } | null>(null);
  const [saving, setSaving] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<LlmTestResult | null>(null);
  const [testErr, setTestErr] = useState<string | null>(null);

  useEffect(() => {
    if (llm.data) setDraft({ enabled: llm.data.enabled, model: llm.data.model, base_url: llm.data.base_url, temperature: null });
  }, [llm.data]);

  if (llm.error) return <ErrorBox error={`LLM settings unavailable: ${llm.error}`} onRetry={llm.reload} />;
  if (!llm.data || !draft) return <Loading />;
  const info: LlmInfo = llm.data;
  const dirty = draft.enabled !== info.enabled || draft.model !== info.model || draft.base_url !== info.base_url || draft.temperature !== null;
  const models = info.models.includes(draft.model) || !draft.model ? info.models : [draft.model, ...info.models];

  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Local LLM gate</span>
        <span className={`badge ${info.available ? "up" : "danger"}`}>{info.available ? "reachable" : "unreachable"}</span>
        <span className="spacer" />
        <button
          className="btn btn-sm btn-primary"
          disabled={!dirty || saving}
          onClick={async () => {
            setSaving(true);
            try {
              const r = await api.updateLlm({
                enabled: draft.enabled,
                model: draft.model,
                base_url: draft.base_url,
                ...(draft.temperature !== null ? { temperature: draft.temperature } : {}),
              });
              llm.setData(r);
              void useStatusStore.getState().loadStatus();
              toast.success("LLM settings saved");
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
      <div className="card-body stack">
        <p className="small muted" style={{ margin: 0 }}>
          An OpenAI/Ollama-compatible local model reviews trade ideas and news. It can only <strong>approve, reject or shrink</strong> a trade (size multiplier clamped to
          [0, 1]); it never adds trades and never overrides a risk rejection.
        </p>
        <Switch checked={draft.enabled} onChange={(v) => setDraft({ ...draft, enabled: v })} label="Enabled" />
        <div className="grid-2">
          <label className="field">
            <span className="label">Model</span>
            <select className="select" value={draft.model} onChange={(e) => setDraft({ ...draft, model: e.target.value })}>
              {models.length === 0 && <option value="">No models found</option>}
              {models.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="label">Base URL</span>
            <input className="input" value={draft.base_url} onChange={(e) => setDraft({ ...draft, base_url: e.target.value })} placeholder="http://127.0.0.1:11434" spellCheck={false} />
          </label>
          <label className="field">
            <span className="label">
              Temperature (optional) <Tip text="Sampling temperature sent to the model. Leave blank to keep the backend's current value. Low values (0–0.3) give more consistent gate decisions." />
            </span>
            <NumberInput value={draft.temperature} onChange={(v) => setDraft({ ...draft, temperature: v })} min={0} max={2} className="input input-num" ariaLabel="Temperature" placeholder="unchanged" />
          </label>
        </div>
        {info.stats && Object.keys(info.stats).length > 0 && (
          <dl className="kv" style={{ maxWidth: 420 }}>
            {Object.entries(info.stats).map(([k, v]) => (
              <FragmentKV key={k} k={k} v={v} />
            ))}
          </dl>
        )}
        <div className="plain-section stack" style={{ gap: 8 }}>
          <div className="row">
            <input className="input" placeholder="Test prompt (optional)" value={prompt} onChange={(e) => setPrompt(e.target.value)} aria-label="Test prompt" />
            <button
              className="btn"
              disabled={testing}
              onClick={async () => {
                setTesting(true);
                setTestErr(null);
                setTest(null);
                try {
                  setTest(await api.testLlm(prompt.trim() || undefined));
                } catch (e) {
                  setTestErr(errorMessage(e));
                } finally {
                  setTesting(false);
                }
              }}
            >
              {testing && <span className="spinner" />}
              Test
            </button>
          </div>
          {testErr && <div className="error-box">{testErr}</div>}
          {test && (
            <div className={`notice ${test.ok ? "" : "danger"}`}>
              <div className="small" style={{ marginBottom: 4 }}>
                <span className={test.ok ? "up" : "danger-text"}>{test.ok ? "OK" : "Failed"}</span> · <span className="num">{test.latency_ms} ms</span>
              </div>
              <div style={{ whiteSpace: "pre-wrap", color: "var(--text)" }}>{test.text || <span className="faint">(empty response)</span>}</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function FragmentKV({ k, v }: { k: string; v: unknown }) {
  const text = typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(3)) : typeof v === "string" || typeof v === "boolean" ? String(v) : JSON.stringify(v);
  return (
    <>
      <dt>{k.replace(/_/g, " ")}</dt>
      <dd>{text}</dd>
    </>
  );
}

function SystemInfo() {
  const status = useStatusStore((s) => s.status);
  const conn = useStatusStore((s) => s.conn);
  if (!status) return <Empty>Backend offline.</Empty>;
  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">System</span>
      </div>
      <div className="card-body">
        <dl className="kv" style={{ maxWidth: 520 }}>
          <dt>Backend version</dt>
          <dd>{status.version}</dd>
          <dt>Mode</dt>
          <dd>
            <ModeBadge mode={status.mode} />
          </dd>
          <dt>Paper keys / live keys</dt>
          <dd>
            {status.paper_keys ? "yes" : "no"} / {status.live_keys ? "yes" : "no"}
          </dd>
          <dt>Live trading allowed</dt>
          <dd className={status.live_allowed ? "warn-text" : ""}>{status.live_allowed ? "yes" : "no"}</dd>
          <dt>Stock / options feed</dt>
          <dd>
            {status.feeds.stocks} / {status.feeds.options}
          </dd>
          <dt>News stream / realtime flow</dt>
          <dd>
            {status.feeds.news ? "on" : "off"} / {status.feeds.flow_realtime ? "realtime" : "snapshot"}
          </dd>
          {Object.entries(status.feeds.connected).map(([k, v]) => (
            <FragmentKV key={k} k={`stream: ${k}`} v={v ? "connected" : `down${status.feeds.errors?.[k] ? ` — ${status.feeds.errors[k]}` : ""}`} />
          ))}
          {status.error && <FragmentKV k="last account error" v={status.error} />}
          <dt>UI socket</dt>
          <dd>
            {conn.state}
            {conn.rttMs !== null ? ` · ${conn.rttMs} ms RTT` : ""}
          </dd>
        </dl>
      </div>
    </div>
  );
}

export default function Settings() {
  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Settings</h1>
          <div className="sub">API keys and the live-trading lock live only in the backend .env — they are never sent to the browser.</div>
        </div>
      </div>
      <div className="stack-lg">
        <section>
          <div className="section-title">Trading mode</div>
          <ModeSwitcher />
        </section>
        <SimReset />
        <DataSettings />
        <LlmPanel />
        <SystemInfo />
      </div>
    </div>
  );
}

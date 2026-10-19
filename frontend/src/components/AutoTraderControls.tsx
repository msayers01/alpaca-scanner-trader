// Arm / disarm / kill / reset / run-cycle controls with the live-mode interlocks.
import { useState } from "react";
import { api, errorMessage } from "../lib/api";
import { fmtUsd } from "../lib/format";
import { useAutoTraderStore } from "../store/autotrader";
import { useStatusStore } from "../store/status";
import { ConfirmDialog } from "./ui/Modal";
import { toast } from "./ui/Toast";
import { ModeBadge } from "./StatusBits";
import { Icon } from "./ui/Icon";

export function ArmButton({ size = "" }: { size?: "" | "btn-sm" | "btn-lg" }) {
  const at = useAutoTraderStore((s) => s.state);
  const setAt = useAutoTraderStore((s) => s.set);
  const mode = useStatusStore((s) => s.status?.mode);
  const statusArmed = useStatusStore((s) => s.status?.autotrader.armed);
  const killed = at?.killed ?? useStatusStore.getState().status?.autotrader.killed ?? false;
  const armed = at?.armed ?? statusArmed ?? false;
  const [dialog, setDialog] = useState<"arm" | "disarm" | null>(null);
  const [busy, setBusy] = useState(false);

  const disarm = async () => {
    setBusy(true);
    try {
      setAt(await api.arm(false));
      void useStatusStore.getState().loadStatus();
      toast.info("Auto-trader disarmed");
    } catch (e) {
      toast.error(`Disarm failed: ${errorMessage(e)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {armed ? (
        <button className={`btn ${size}`} onClick={() => void disarm()} disabled={busy || !mode}>
          {busy && <span className="spinner" />}
          <Icon name="power" size={14} /> Disarm
        </button>
      ) : (
        <button className={`btn btn-up ${size}`} onClick={() => setDialog("arm")} disabled={killed || !mode} title={killed ? "Reset the kill switch first" : undefined}>
          <Icon name="play" size={12} /> Arm
        </button>
      )}
      {dialog === "arm" && mode && (
        <ConfirmDialog
          title={
            <span className="row">
              Arm auto-trader <ModeBadge mode={mode} />
            </span>
          }
          confirmLabel={mode === "live" ? "Arm with real money" : "Arm"}
          tone={mode === "live" ? "danger" : "up"}
          typeToConfirm={mode === "live" ? "LIVE" : undefined}
          onClose={() => setDialog(null)}
          onConfirm={async () => {
            try {
              const s = await api.arm(true, mode === "live" ? "LIVE" : undefined);
              setAt(s);
              void useStatusStore.getState().loadStatus();
              toast.success("Auto-trader armed");
            } catch (e) {
              throw new Error(errorMessage(e));
            }
          }}
        >
          <p className="muted">
            While armed, enabled strategies may place orders automatically every cycle. Every order still passes the risk manager; the kill switch stops trading
            instantly.
          </p>
          {mode === "live" && (
            <div className="notice danger">
              You are in <strong>LIVE</strong> mode. Orders will use <strong>real money</strong> in your Alpaca account.
            </div>
          )}
        </ConfirmDialog>
      )}
    </>
  );
}

export function KillButton({ size = "" }: { size?: "" | "btn-sm" | "btn-lg" }) {
  const setAt = useAutoTraderStore((s) => s.set);
  const killed = useAutoTraderStore((s) => s.state?.killed) ?? useStatusStore.getState().status?.autotrader.killed ?? false;
  const mode = useStatusStore((s) => s.status?.mode);
  const [open, setOpen] = useState(false);
  const [flatten, setFlatten] = useState(false);
  if (killed) return <ResetKillButton size={size} />;
  return (
    <>
      <button className={`btn btn-danger ${size}`} onClick={() => setOpen(true)} disabled={!mode}>
        <Icon name="alert" size={14} /> Kill switch
      </button>
      {open && (
        <ConfirmDialog
          title="Engage kill switch"
          confirmLabel={flatten ? "Kill and flatten" : "Kill"}
          tone="danger"
          onClose={() => setOpen(false)}
          onConfirm={async () => {
            try {
              setAt(await api.kill(flatten));
              void useStatusStore.getState().loadStatus();
              toast.info(flatten ? "Kill switch engaged — flattening positions" : "Kill switch engaged");
            } catch (e) {
              throw new Error(errorMessage(e));
            }
          }}
        >
          <p className="muted">Stops the auto-trader immediately and disarms it. No new entries are placed until you reset the kill switch.</p>
          <label className="checkbox">
            <input type="checkbox" checked={flatten} onChange={(e) => setFlatten(e.target.checked)} />
            Also flatten: close all positions and cancel open orders
          </label>
          {flatten && mode === "live" && <div className="notice danger">This sends real market orders to close every position.</div>}
        </ConfirmDialog>
      )}
    </>
  );
}

export function ResetKillButton({ size = "" }: { size?: "" | "btn-sm" | "btn-lg" }) {
  const setAt = useAutoTraderStore((s) => s.set);
  const [busy, setBusy] = useState(false);
  return (
    <button
      className={`btn ${size}`}
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          setAt(await api.resetKill());
          void useStatusStore.getState().loadStatus();
          toast.info("Kill switch reset — auto-trader remains disarmed");
        } catch (e) {
          toast.error(`Reset failed: ${errorMessage(e)}`);
        } finally {
          setBusy(false);
        }
      }}
    >
      {busy && <span className="spinner" />}
      Reset kill switch
    </button>
  );
}

export function RunCycleButton({ size = "" }: { size?: "" | "btn-sm" | "btn-lg" }) {
  const setAt = useAutoTraderStore((s) => s.set);
  const [busy, setBusy] = useState(false);
  return (
    <button
      className={`btn ${size}`}
      disabled={busy}
      title="Run one auto-trader cycle now (respects arming: unarmed cycles only log decisions)"
      onClick={async () => {
        setBusy(true);
        try {
          const s = await api.runCycle();
          setAt(s);
          toast.success(`Cycle complete${s.cycle_ms !== null ? ` in ${s.cycle_ms} ms` : ""}`);
        } catch (e) {
          toast.error(`Cycle failed: ${errorMessage(e)}`);
        } finally {
          setBusy(false);
        }
      }}
    >
      {busy ? <span className="spinner" /> : <Icon name="refresh" size={14} />}
      Run one cycle
    </button>
  );
}

export function AutoTraderStatusLine() {
  const at = useAutoTraderStore((s) => s.state);
  const st = useStatusStore((s) => s.status?.autotrader);
  const armed = at?.armed ?? st?.armed ?? false;
  const killed = at?.killed ?? st?.killed ?? false;
  const reason = at?.kill_reason ?? st?.kill_reason;
  return (
    <div className="status-hero">
      <span className={`big ${killed ? "danger-text" : armed ? "up" : "muted"}`}>{killed ? "KILLED" : armed ? "ARMED" : "DISARMED"}</span>
      {killed && reason && <span className="small danger-text">{reason}</span>}
      {at && (
        <span className="small muted num">
          Today: {at.today.trades} trades · <span className={at.today.pnl >= 0 ? "up" : "down"}>{fmtUsd(at.today.pnl, { signed: true })}</span>
        </span>
      )}
    </div>
  );
}

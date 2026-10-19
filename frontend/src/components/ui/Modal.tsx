import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./Icon";

interface ModalProps {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  danger?: boolean;
}

export function Modal({ title, onClose, children, footer, wide, danger }: ModalProps) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const node = ref.current;
    // focus first focusable (prefer inputs)
    // Focus the first form control; otherwise the first footer button (Cancel) — never auto-focus a destructive confirm.
    const first =
      node?.querySelector<HTMLElement>(".modal-body input:not([disabled]):not([type=hidden]), .modal-body select, .modal-body textarea") ??
      node?.querySelector<HTMLElement>(".modal-footer button:not([disabled])") ??
      node?.querySelector<HTMLElement>("button");
    first?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        closeRef.current();
      } else if (e.key === "Tab" && node) {
        const f = node.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        );
        if (!f.length) return;
        const a = f[0];
        const z = f[f.length - 1];
        if (e.shiftKey && document.activeElement === a) {
          e.preventDefault();
          z.focus();
        } else if (!e.shiftKey && document.activeElement === z) {
          e.preventDefault();
          a.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      prev?.focus?.();
    };
  }, []);

  return createPortal(
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div ref={ref} className={`modal${wide ? " wide" : ""}${danger ? " danger" : ""}`} role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <div className="modal-header">
          <h3 id={titleId} style={{ flex: 1 }}>
            {title}
          </h3>
          <button className="icon-btn" onClick={onClose} aria-label="Close dialog">
            <Icon name="close" />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-footer">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}

interface ConfirmProps {
  title: ReactNode;
  children?: ReactNode;
  confirmLabel?: string;
  /** "up" (buy), "down" (sell), "danger", or default primary. */
  tone?: "primary" | "up" | "down" | "danger";
  /** If set, the user must type this word exactly to enable the confirm button. */
  typeToConfirm?: string;
  onConfirm: () => Promise<unknown> | unknown;
  onClose: () => void;
}

/** Confirmation dialog. Shows errors from onConfirm inline and keeps the dialog open. */
export function ConfirmDialog({ title, children, confirmLabel = "Confirm", tone = "primary", typeToConfirm, onConfirm, onClose }: ConfirmProps) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const ok = !typeToConfirm || typed === typeToConfirm;
  const run = async () => {
    if (!ok || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await onConfirm();
      onClose();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };
  const btnClass = tone === "up" ? "btn btn-up" : tone === "down" ? "btn btn-down" : tone === "danger" ? "btn btn-danger" : "btn btn-primary";
  return (
    <Modal
      title={title}
      onClose={onClose}
      danger={tone === "danger" || !!typeToConfirm}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className={btnClass} onClick={run} disabled={!ok || busy}>
            {busy && <span className="spinner" />}
            {confirmLabel}
          </button>
        </>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run();
        }}
        className="col"
        style={{ gap: 12 }}
      >
        {children}
        {typeToConfirm && (
          <label className="field">
            <span className="label">
              Type <strong className="danger-text">{typeToConfirm}</strong> to confirm
            </span>
            <input className="input" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" spellCheck={false} aria-label={`Type ${typeToConfirm} to confirm`} />
          </label>
        )}
        {err && <div className="error-box">{err}</div>}
        <button type="submit" hidden />
      </form>
    </Modal>
  );
}

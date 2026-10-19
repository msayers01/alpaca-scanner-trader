// Friendly error screen for route-level render errors (replaces React Router's developer default).
import { isRouteErrorResponse, useNavigate, useRouteError } from "react-router-dom";
import { Icon } from "./ui/Icon";

function describe(err: unknown): { title: string; detail: string; stack?: string } {
  if (isRouteErrorResponse(err)) return { title: `${err.status} ${err.statusText}`, detail: String(err.data ?? "") };
  if (err instanceof Error) return { title: "Something went wrong on this page", detail: err.message, stack: err.stack };
  return { title: "Something went wrong on this page", detail: String(err) };
}

/**
 * `inShell`: rendered inside the app layout (sidebar stays usable). Otherwise it is the last-resort screen
 * for errors in the layout itself.
 */
export function RouteError({ inShell = true }: { inShell?: boolean }) {
  const err = useRouteError();
  const navigate = useNavigate();
  const { title, detail, stack } = describe(err);
  console.error("[route error]", err);
  return (
    <div className={inShell ? "page" : "page route-error-full"} role="alert">
      <div className="card" style={{ maxWidth: 760 }}>
        <div className="card-body">
          <div className="row" style={{ gap: 10, marginBottom: 8 }}>
            <Icon name="alert" size={18} />
            <h2 style={{ margin: 0 }}>{title}</h2>
          </div>
          <p className="muted" style={{ marginTop: 0 }}>
            The rest of Vantage is still running — the backend, the auto-trader and your positions are unaffected. This only stopped this view from
            rendering.
          </p>
          {detail && <pre className="llm-pre" style={{ maxHeight: 160 }}>{detail}</pre>}
          <div className="row" style={{ gap: 8, marginTop: 12 }}>
            <button className="btn btn-primary" onClick={() => window.location.reload()}>
              <Icon name="refresh" size={12} /> Reload page
            </button>
            <button className="btn" onClick={() => (inShell ? navigate("/") : (window.location.href = "/"))}>
              Go to dashboard
            </button>
          </div>
          {stack && (
            <details style={{ marginTop: 12 }}>
              <summary className="small muted" style={{ cursor: "pointer" }}>
                Technical details
              </summary>
              <pre className="llm-pre" style={{ maxHeight: 240 }}>{stack}</pre>
            </details>
          )}
        </div>
      </div>
    </div>
  );
}

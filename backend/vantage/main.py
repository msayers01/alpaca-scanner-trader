"""FastAPI application factory and entry point (`uv run vantage`)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from vantage import __version__
from vantage.api.routes import J, router
from vantage.api.ws import Hub
from vantage.app_state import AppState
from vantage.broker.base import BrokerError
from vantage.config import REPO_ROOT, Settings, get_settings

log = logging.getLogger("vantage")

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def allowed_origins(s: Settings) -> set[str]:
    out = set()
    for host in ("localhost", "127.0.0.1"):
        for port in (5173, 4173, s.port):
            out.add(f"http://{host}:{port}")
    return out


class OriginGuard(BaseHTTPMiddleware):
    """Reject state-changing requests from foreign origins (drive-by CSRF from other websites)."""

    def __init__(self, app: FastAPI, origins: set[str]):
        super().__init__(app)
        self.origins = origins

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        origin = request.headers.get("origin")
        if request.method in MUTATING and origin and origin not in self.origins:
            return Response('{"error":"forbidden_origin","detail":"cross-origin request blocked"}', 403,
                            media_type="application/json")
        return await call_next(request)


def create_app(settings: Settings | None = None, state: AppState | None = None, start_mode: str | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _quiet_windows_resets()
        vs = state or AppState(settings)
        app.state.vs = vs
        hub = Hub(vs.bus, lambda: vs.container)
        vs.hub = hub
        app.state.hub = hub
        hub.start()
        await vs.switch_mode(start_mode or vs.initial_mode())
        log.info("Vantage %s ready in %s mode on http://%s:%s", __version__, vs.container.mode if vs.container else "?",
                 settings.host, settings.port)
        try:
            yield
        finally:
            await hub.stop()
            await vs.shutdown()

    app = FastAPI(title="Vantage", version=__version__, lifespan=lifespan)
    origins = allowed_origins(settings)
    app.add_middleware(OriginGuard, origins=origins)
    app.add_middleware(CORSMiddleware, allow_origins=sorted(origins), allow_methods=["*"], allow_headers=["*"])
    app.include_router(router)

    @app.exception_handler(BrokerError)
    async def _broker_err(request: Request, exc: BrokerError):  # type: ignore[no-untyped-def]
        status = exc.status if exc.status and 400 <= exc.status < 600 else 502
        return J({"error": "broker_error", "detail": str(exc)}, status)

    @app.exception_handler(PermissionError)
    async def _perm_err(request: Request, exc: PermissionError):  # type: ignore[no-untyped-def]
        return J({"error": "forbidden", "detail": str(exc)}, 403)

    @app.exception_handler(ValueError)
    async def _val_err(request: Request, exc: ValueError):  # type: ignore[no-untyped-def]
        return J({"error": "bad_request", "detail": str(exc)}, 400)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        origin = ws.headers.get("origin")
        if origin and origin not in origins:
            await ws.close(code=1008)
            return
        await app.state.hub.handle(ws)

    if FRONTEND_DIST.exists():
        index = FRONTEND_DIST / "index.html"

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):  # type: ignore[no-untyped-def]
            f = (FRONTEND_DIST / path).resolve()
            if path and f.is_file() and FRONTEND_DIST.resolve() in f.parents:
                return FileResponse(f)
            return FileResponse(index)

    return app


def _quiet_windows_resets() -> None:
    """Browsers closing sockets make the Windows proactor log ConnectionResetError tracebacks; ignore those."""
    import asyncio

    loop = asyncio.get_running_loop()
    default = loop.get_exception_handler()

    def handler(lp: asyncio.AbstractEventLoop, ctx: dict) -> None:  # type: ignore[type-arg]
        if isinstance(ctx.get("exception"), ConnectionResetError):
            return
        if default:
            default(lp, ctx)
        else:
            lp.default_exception_handler(ctx)

    loop.set_exception_handler(handler)


def run() -> None:
    import uvicorn

    s = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if s.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("HOST=%s exposes the trading API beyond this machine — not recommended", s.host)
    uvicorn.run(create_app(s), host=s.host, port=s.port, log_level="info", ws_ping_interval=20, access_log=False)


if __name__ == "__main__":  # pragma: no cover
    run()


_ = Path

"""ASGI application assembly and request boundaries."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import auth, routes
from .config import Settings
from .hermes import APIError, Hermes
from .relay import Relay

STATIC = Path(__file__).parent / "static"


class BrowserBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        path = scope["path"]

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; "
                        b"style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
                        b"font-src 'self'; object-src 'none'; base-uri 'none'; "
                        b"frame-ancestors 'none'; "
                        b"form-action 'self'",
                    ),
                ]
                if not path.startswith("/static/"):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        error = None
        if path.startswith("/api/"):
            public = path in {"/api/bootstrap", "/api/login"}
            if not public and not auth.authenticated(request):
                error = (401, "Please sign in to continue.", "unauthenticated")
            elif request.method not in {"GET", "HEAD", "OPTIONS"}:
                if not auth.browser_request_valid(request, login=path == "/api/login"):
                    error = (
                        403,
                        "Your session needs refreshing. Reload the page and try again.",
                        "csrf",
                    )
        if error:
            status, message, code = error
            return await JSONResponse({"error": message, "code": code}, status_code=status)(
                scope, receive, secure_send
            )
        return await self.app(scope, receive, secure_send)


async def index(request):
    return FileResponse(STATIC / "index.html")


async def health(request):
    return JSONResponse({"status": "ok"})


async def api_error(request, exc: APIError):
    return JSONResponse({"error": exc.message, "code": exc.code}, status_code=exc.status)


def create_app(settings: Settings, config_path: Path, *, transport=None) -> Starlette:
    @asynccontextmanager
    async def lifespan(app):
        yield
        await app.state.relay.close()
        await app.state.hermes.close()

    app = Starlette(
        lifespan=lifespan,
        exception_handlers={APIError: api_error},
        routes=[
            Route("/", index),
            Route("/health", health),
            Route("/api/bootstrap", routes.bootstrap),
            Route("/api/login", routes.login, methods=["POST"]),
            Route("/api/logout", routes.logout, methods=["POST"]),
            Route("/api/connection", routes.connection, methods=["GET", "PUT"]),
            Route("/api/connection/test", routes.connection, methods=["POST"]),
            Route("/api/capabilities", routes.capabilities),
            Route("/api/models", routes.model_options),
            Route("/api/sessions", routes.sessions, methods=["GET", "POST"]),
            Route("/api/sessions/{session_id}/messages", routes.messages),
            Route("/api/sessions/{session_id}/fork", routes.fork, methods=["POST"]),
            Route("/api/sessions/{session_id}", routes.session, methods=["GET", "PATCH", "DELETE"]),
            Route("/api/runs", routes.start_run, methods=["POST"]),
            Route("/api/runs/{run_id}/events", routes.events),
            Route("/api/runs/{run_id}", routes.run),
            Route("/api/runs/{run_id}/{action}", routes.control, methods=["POST"]),
            Mount("/static", StaticFiles(directory=STATIC)),
        ],
    )
    app.state.settings, app.state.config_path = settings, config_path
    app.state.hermes = Hermes(settings.hermes_url, settings.api_key, transport=transport)
    app.state.relay = Relay(app.state.hermes)
    app.state.limiter = auth.LoginLimiter()
    app.state.connection_lock = asyncio.Lock()
    app.state.capabilities = {}
    app.add_middleware(BrowserBoundary)
    return app

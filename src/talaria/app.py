"""ASGI application assembly and request boundaries."""

import asyncio
import hashlib
import html
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from starlette._utils import get_route_path
from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import (
    auth,
    extensions,
    installation,
    metadata,
    routes,
    session_commands,
    session_preferences,
    session_views,
    transcripts,
    updates,
)
from .auth_sessions import Revocations
from .config import Settings
from .hermes import APIError, Hermes, valid_api_key
from .profiles import ProfileRouter, Profiles
from .profiles import listing as profile_listing
from .profiles import remove as profile_remove
from .proxy import PublicPath
from .relay import Relay
from .session_commands import CommandFollowers
from .session_state import SessionState, StateUnavailable

STATIC = Path(__file__).parent / "static"
STATE_UNAVAILABLE = "Talaria could not use its saved session data. Its server log has the reason."
IMMUTABLE = "public, max-age=31536000, immutable"
log = logging.getLogger(__name__)


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = get_route_path(scope)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-robots-tag", b"noindex, nofollow"),
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

        return await self.app(scope, receive, secure_send)


class BrowserBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        path = get_route_path(scope)
        error = None
        if path.startswith("/api/"):
            public = path in {"/api/bootstrap", "/api/login"}
            if not public and not auth.authenticated(request):
                error = (401, "Please sign in to continue.", "unauthenticated")
            elif request.method not in {"GET", "HEAD", "OPTIONS"} and not (
                auth.browser_request_valid(request, login=path == "/api/login")
            ):
                error = (
                    403,
                    "Your session needs refreshing. Reload the page and try again.",
                    "csrf",
                )
        if error:
            status, message, code = error
            return await JSONResponse({"error": message, "code": code}, status_code=status)(
                scope, receive, send
            )
        return await self.app(scope, receive, send)


class Assets(StaticFiles):
    def __init__(self, *, cache_control="no-cache", **kwargs):
        super().__init__(**kwargs)
        self.cache_control = cache_control

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        # Stable filenames revalidate with ETag instead of risking a mixture of
        # cached modules from different app versions. A build's own directory never
        # changes, so browsers keep its files without asking.
        response.headers["Cache-Control"] = self.cache_control
        return response


def build_path(directory, seed=b""):
    """A static path naming this exact build, so its relative imports stay within it."""
    digest = hashlib.sha256(seed)
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
            digest.update(path.read_bytes())
    return f"static/build-{digest.hexdigest()[:16]}/"


def in_build(document, build):
    return (
        document.replace("__TALARIA_BASE__static/", "__TALARIA_BASE__" + build)
        # The manifest keeps its stable installation identity and launch scope.
        .replace(build + "app.webmanifest", "static/app.webmanifest")
    )


def asset_routes(immutable):
    """The static mounts, and the document that loads its assets from them."""
    document = (STATIC / "index.html").read_text()
    stable = Mount("/static", Assets(directory=STATIC))
    if not immutable:
        return [stable], document
    build = build_path(STATIC)
    named = Mount("/" + build.rstrip("/"), Assets(directory=STATIC, cache_control=IMMUTABLE))
    return [named, stable], in_build(document, build)


async def index(request):
    # The document is tiny; cache the template per application, not per request.
    base = html.escape(request.app.state.settings.base_path + "/", quote=True)
    return HTMLResponse(request.app.state.index_html.replace("__TALARIA_BASE__", base))


async def robots(request):
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


async def health(request):
    return JSONResponse({"status": "ok", **installation.build_info()})


async def unexpected_error(request, exc):
    # Starlette re-raises for Uvicorn's traceback logging after sending this.
    return JSONResponse(
        {
            "error": "Talaria could not complete this request. Please try again.",
            "code": "internal_error",
        },
        status_code=500,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


async def api_error(request, exc: APIError):
    return JSONResponse({"error": exc.message, "code": exc.code}, status_code=exc.status)


async def state_unavailable(request, exc: StateUnavailable):
    log.warning("Talaria session state is unavailable: %s", exc)
    return await api_error(request, APIError(STATE_UNAVAILABLE, 503, "state_unavailable"))


def create_app(
    settings: Settings,
    config_path: Path,
    *,
    transport=None,
    development=False,
    immutable_assets=False,
    profiles=None,
    profile_id="default",
) -> Starlette:
    @asynccontextmanager
    async def lifespan(app):
        await app.state.command_followers.resume()
        try:
            yield {"talaria_stop_streams": app.state.profiles.stop_streams}
        finally:
            await app.state.command_followers.close()
            await app.state.profiles.close()

    assets, index_html = asset_routes(immutable_assets)
    app = Starlette(
        lifespan=lifespan,
        exception_handlers={
            APIError: api_error,
            StateUnavailable: state_unavailable,
            Exception: unexpected_error,
        },
        routes=[
            Route("/", index),
            Route("/robots.txt", robots),
            Route("/health", health),
            Route("/api/bootstrap", routes.bootstrap),
            Route("/api/installation", installation.details),
            Route("/api/installation/{action}", updates.submit, methods=["POST"]),
            Route("/api/profiles", profile_listing, methods=["GET", "POST"]),
            Route("/api/profiles/test", routes.connection, methods=["POST"]),
            Route("/api/profiles/{profile_id}", profile_remove, methods=["DELETE"]),
            Route("/api/login", routes.login, methods=["POST"]),
            Route("/api/logout", routes.logout, methods=["POST"]),
            Route("/api/connection", routes.connection, methods=["GET", "PUT"]),
            Route("/api/connection/test", routes.connection, methods=["POST"]),
            Route("/api/capabilities", routes.capabilities),
            Route("/api/models", routes.model_options),
            Route("/api/commands", session_commands.commands, methods=["GET", "POST"]),
            Route(
                "/api/commands/{command_id}", session_commands.command, methods=["GET", "DELETE"]
            ),
            Route("/api/preferences/import", session_preferences.import_choices, methods=["POST"]),
            Route("/api/agent", metadata.details),
            Route("/api/readiness", metadata.readiness),
            Route("/api/search", session_views.view),
            Route("/api/activity", session_views.view),
            Route("/api/sessions/{session_id}/around", session_views.view),
            Route("/api/sessions", routes.sessions, methods=["GET", "POST"]),
            Route("/api/sessions/{session_id}/messages", routes.messages),
            Route("/api/sessions/{session_id}/context", extensions.session_extension),
            Route("/api/sessions/{session_id}/response", extensions.session_extension),
            Route(
                "/api/sessions/{session_id}/rewind", extensions.session_extension, methods=["POST"]
            ),
            Route("/api/sessions/{session_id}/export", transcripts.download),
            Route("/api/sessions/{session_id}/commands", session_commands.session_commands),
            Route(
                "/api/sessions/{session_id}/preferences",
                session_preferences.update,
                methods=["PUT"],
            ),
            Route("/api/sessions/{session_id}/fork", routes.fork, methods=["POST"]),
            Route("/api/sessions/{session_id}", routes.session, methods=["GET", "PATCH", "DELETE"]),
            Route("/api/runs", routes.start_run, methods=["POST"]),
            Route("/api/runs/{run_id}/events", routes.events),
            Route("/api/runs/{run_id}", routes.run),
            Route("/api/runs/{run_id}/discard", routes.discard, methods=["POST"]),
            Route("/api/runs/{run_id}/{action}", routes.control, methods=["POST"]),
            *assets,
        ],
    )
    app.state.settings, app.state.config_path = settings, config_path
    app.state.revocations = profiles.app.state.revocations if profiles else Revocations(config_path)
    app.state.development = development
    app.state.cookie_name = "talaria_dev_session" if development else auth.COOKIE
    if settings.base_path:
        app.state.cookie_name += "_" + hashlib.sha256(settings.base_path.encode()).hexdigest()[:10]
    app.state.index_html = index_html
    # Resolve runtime secrets separately from settings so saves never persist them.
    key = os.environ.get("TALARIA_HERMES_API_KEY") if profile_id == "default" else None
    if key is not None and (not valid_api_key(key) or len(key) > 4096):
        raise ValueError(
            "TALARIA_HERMES_API_KEY must contain 1-4096 printable ASCII characters without spaces."
        )
    app.state.key_from_env = key is not None
    app.state.hermes_key = settings.api_key if key is None else key
    app.state.hermes = Hermes(settings.hermes_url, app.state.hermes_key, transport=transport)
    app.state.relay = Relay(app.state.hermes)
    app.state.limiter = auth.LoginLimiter(settings.trusted_proxies)
    app.state.connection_lock = asyncio.Lock()
    app.state.capabilities = {}
    app.state.extensions = extensions.Extensions()
    app.state.profile_id = profile_id
    app.state.profiles = profiles or Profiles(app, transport)
    # One record of session activity serves every profile; rows carry the profile ID.
    app.state.session_state = (
        profiles.app.state.session_state if profiles else SessionState(config_path)
    )
    app.state.command_followers = (
        profiles.app.state.command_followers
        if profiles
        else CommandFollowers(app.state.session_state, app.state.profiles)
    )
    if profiles is None:
        app.add_middleware(ProfileRouter, profiles=app.state.profiles)
        # Outside every profile's app. Starlette leaves event streams uncompressed.
        app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
        app.add_middleware(BrowserBoundary)
        app.add_middleware(SecurityHeaders)
        app.add_middleware(PublicPath, prefix=settings.base_path)
    return app

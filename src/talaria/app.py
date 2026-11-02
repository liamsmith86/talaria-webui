"""ASGI application assembly and request boundaries."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from starlette._utils import get_route_path
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

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
from .web import STATIC, Compression, SecurityHeaders, asset_routes, health, index, robots

STATE_UNAVAILABLE = "Talaria could not use its saved session data. Its server log has the reason."

log = logging.getLogger(__name__)


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

    assets, index_html = asset_routes(immutable_assets, STATIC)
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
    app.state.cookie_name = auth.cookie_name(settings.base_path, development=development)
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
        app.add_middleware(Compression, minimum_size=1024, compresslevel=6)
        app.add_middleware(BrowserBoundary)
        app.add_middleware(SecurityHeaders)
        app.add_middleware(PublicPath, prefix=settings.base_path)
    return app

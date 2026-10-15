"""Native plugin rewiring on a live aiohttp server, with isolated profile storage."""

import asyncio
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.secret_scope import set_multiplex_active
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import PluginManifest
from hermes_constants import get_hermes_home
from hermes_state import SessionDB

from talaria.hermes_plugin.bridge import register

home = Path(sys.argv[1])
assert home.is_dir() and str(home).startswith("/tmp/")
PROFILES = {
    "default": (home, "default-lifecycle-key-0123456789"),
    "other": (home / "profiles/other", "other-lifecycle-key-0123456789"),
    "disabled": (home / "profiles/disabled", "disabled-lifecycle-key-0123456789"),
}
for name, (directory, key) in PROFILES.items():
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.yaml").write_text(
        json.dumps({"plugins": {"enabled": [] if name == "disabled" else ["talaria-webui"]}})
    )
    (directory / ".env").write_text(f"API_SERVER_KEY={key}\n")
    (directory / "SOUL.md").write_text(f"Name: {name} agent\n")


def headers(profile):
    return {"Authorization": f"Bearer {PROFILES[profile][1]}"}


async def verify_routes(client):
    # Every route must reach authentication, including methods and match-info captures.
    paths = [
        ("GET", "capabilities"),
        ("HEAD", "capabilities"),
        ("GET", "commands"),
        ("POST", "commands"),
        ("GET", "commands/fixture-command-id"),
        *(("GET", action) for action in ("models", "search", "activity")),
        ("POST", "runs"),
        ("POST", "runs/fixture/clarification"),
        *(("GET", f"sessions/fixture/{action}") for action in ("context", "response", "around")),
        *(("POST", f"sessions/fixture/{action}") for action in ("rewind", "fork")),
    ]
    for prefix, profile in (("", "default"), ("/p/default", "default"), ("/p/other", "other")):
        for method, path in paths:
            response = await client.request(method, f"{prefix}/talaria/v1/{path}")
            assert response.status == 401, (method, prefix, path, await response.text())
        response = await client.get(f"{prefix}/talaria/v1/capabilities", headers=headers(profile))
        assert response.status == 200, await response.text()
        assert (await response.json())["agent"]["name"] == f"{profile} agent"
        assert response.headers["X-Lifecycle-Middleware"] == "present"

    for profile, credentials, status in (
        ("other", "default", 401),
        ("default", "other", 401),
        ("disabled", "disabled", 404),
        ("unknown", "default", 404),
    ):
        response = await client.get(
            f"/p/{profile}/talaria/v1/capabilities", headers=headers(credentials)
        )
        assert response.status == status, (profile, credentials, await response.text())

    # Existing core routes, named resources and the ingress fallback still work.
    assert (await client.get("/health")).status == 200
    assert (await client.get("/p/other/health")).status == 200
    assert (await client.get("/api/sessions")).status == 401
    assert (await client.get("/p/other/unclaimed")).status == 404
    response = await client.get("/native/unchanged")
    assert (await response.json()) == {"item": "unchanged"}


async def verify_commands_and_cleanup(client, adapter, cleanup_started):
    release = threading.Event()
    both_started = threading.Event()
    executions = []

    def execute(*args):
        executions.append(str(get_hermes_home()))
        if len(executions) == 2:
            both_started.set()
        assert release.wait(10), "Command cleanup did not drain"
        return {"text": "Finished", "session_id": ""}

    closing = None
    with patch("talaria.hermes_plugin.commands.execute", execute):
        try:
            payload = {"command": "version", "request_id": "shared-lifecycle-command"}
            for prefix, profile in (("", "default"), ("/p/other", "other")):
                response = await client.post(
                    f"{prefix}/talaria/v1/commands", headers=headers(profile), json=payload
                )
                assert response.status == 202, await response.text()
            assert await asyncio.to_thread(both_started.wait, 5)
            assert adapter.active_agent_work_count() == 2
            adapter.rewire_plugin_handlers()
            response = await client.post(
                "/talaria/v1/commands", headers=headers("default"), json=payload
            )
            assert response.status == 202
            assert adapter.active_agent_work_count() == 2, "Rewire lost command replay state"
            response = await client.get(
                "/p/other/talaria/v1/commands/shared-lifecycle-command", headers=headers("other")
            )
            assert (await response.json())["status"] == "running"
            closing = asyncio.create_task(client.close())
            await asyncio.wait_for(cleanup_started.wait(), 5)
            await asyncio.sleep(0)
            assert not closing.done(), "App cleanup abandoned active commands"
        finally:
            release.set()
            if closing is not None:
                await asyncio.wait_for(closing, 5)
    assert adapter.active_agent_work_count() == 0
    assert sorted(executions) == sorted(str(PROFILES[name][0]) for name in ("default", "other"))


async def verify_app(adapter, manager, *, late):
    cleanup_started = asyncio.Event()
    request_started = asyncio.Event()
    finish_request = asyncio.Event()
    cleaned = []
    context = []

    async def native_context(app):
        context.append("started")
        yield
        context.append("closed")

    async def cleanup(app):
        cleaned.append(app)
        cleanup_started.set()

    @web.middleware
    async def middleware(request, handler):
        response = await handler(request)
        response.headers["X-Lifecycle-Middleware"] = "present"
        return response

    async def native(request):
        if request.match_info["item"] == "inflight":
            request_started.set()
            await finish_request.wait()
        return web.json_response({"item": request.match_info["item"]})

    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware(), middleware])
    app.cleanup_ctx.append(native_context)
    app.on_cleanup.append(cleanup)
    app.router.add_get("/native/{item}", native, name="native")
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
        app.router.add_route(method, f"/p/{{profile}}{path}", handler)
    # This is registered before plugin factories in APIServerAdapter.connect().
    app.router.add_route("*", "/p/{profile}/{tail:.*}", adapter._handle_profile_ingress)
    adapter._wire_plugin_handlers(app)
    async with TestClient(TestServer(app)) as client:
        if late:
            assert (await client.get("/talaria/v1/capabilities")).status == 404
            pending = asyncio.create_task(client.get("/native/inflight"))
            try:
                await asyncio.wait_for(request_started.wait(), 5)
                register(PluginContext(PluginManifest(name="talaria-webui"), manager))
                adapter.rewire_plugin_handlers()
            finally:
                finish_request.set()
                response = await asyncio.wait_for(pending, 5)
            assert (await response.json()) == {"item": "inflight"}
        assert app.frozen and app.router.frozen and app.on_cleanup.frozen
        assert str(app.router["native"].url_for(item="kept")) == "/native/kept"
        await verify_routes(client)
        await verify_commands_and_cleanup(client, adapter, cleanup_started)
    assert cleaned == [app], "Existing cleanup callbacks must run exactly once"
    assert context == ["started", "closed"], "Native cleanup contexts must remain attached"


async def main():
    manager = PluginManager()
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": PROFILES["default"][1]}))
    # Newer Hermes borrows profile storage from the gateway's authority registry.
    authorities = {
        directory: SimpleNamespace(db=SessionDB(directory / "state.db"))
        for directory, _ in PROFILES.values()
    }
    adapter.gateway_runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True),
        session_authorities=SimpleNamespace(for_home=lambda path: authorities.get(Path(path))),
    )
    set_multiplex_active(True)
    try:
        with (
            patch("hermes_cli.plugins.get_plugin_manager", return_value=manager),
            patch(
                "hermes_cli.profiles.profiles_to_serve",
                return_value=[(name, directory) for name, (directory, _) in PROFILES.items()],
            ),
        ):
            await verify_app(adapter, manager, late=True)
            # Rebuilding the native app must wire again, with a fresh command store.
            await verify_app(adapter, manager, late=False)
    finally:
        await adapter.disconnect()
        for authority in authorities.values():
            authority.db.close()
        manager.unload()
        set_multiplex_active(False)


asyncio.run(main())

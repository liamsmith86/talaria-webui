"""Real plugin discovery/startup, repeated callbacks and safe late-load deferral."""

import asyncio
import importlib
import io
import json
import logging
import os
import shutil
import sys
import threading
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, web
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_cli.plugins import discover_plugins, get_plugin_manager

from talaria.hermes_plugin import bridge

home = Path(sys.argv[1])
assert home.is_dir() and str(home).startswith("/tmp/")
os.environ["HERMES_ENABLE_PROJECT_PLUGINS"] = "false"
key = "startup-contract-key-0123456789"
plugin = home / "plugins/talaria-webui"
shutil.copytree(Path(bridge.__file__).parent, plugin)
(home / ".env").write_text(f"API_SERVER_KEY={key}\n")
(home / "SOUL.md").write_text("Name: Startup agent\n")
messages = io.StringIO()
handler = logging.StreamHandler(messages)
logging.getLogger().addHandler(handler)


def discover(enabled):
    (home / "config.yaml").write_text(
        json.dumps({"plugins": {"enabled": ["talaria-webui"] if enabled else []}})
    )
    discover_plugins(force=True)
    return get_plugin_manager()


def native_adapter():
    return APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": key, "host": "127.0.0.1", "port": 0})
    )


async def verify_routes(client, base):
    for method, path in (
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
    ):
        async with client.request(method, f"{base}/talaria/v1/{path}") as response:
            assert response.status == 401, (method, path, await response.text())
    async with client.get(
        base + "/talaria/v1/capabilities", headers={"Authorization": f"Bearer {key}"}
    ) as response:
        assert response.status == 200, await response.text()
        assert (await response.json())["agent"]["name"] == "Startup agent"
    async with client.get(base + "/health") as response:
        assert response.status == 200
    async with client.get(base + "/api/sessions") as response:
        assert response.status == 401


async def verify_command_cleanup(adapter, client, base, factory, commands, cleanup_started):
    release = threading.Event()
    started = threading.Event()
    closing = None

    def execute(*args):
        started.set()
        assert release.wait(10), "Command cleanup did not drain"
        return {"text": "Finished", "session_id": ""}

    # The host's cleanup signal remains the public, awaited owner of pending jobs.
    with patch.object(commands, "execute", execute):
        try:
            async with client.post(
                base + "/talaria/v1/commands",
                headers={"Authorization": f"Bearer {key}"},
                json={"command": "version", "request_id": "startup-contract-command"},
            ) as response:
                assert response.status == 202, await response.text()
            assert await asyncio.to_thread(started.wait, 5)
            assert adapter.active_agent_work_count() == 1
            factory(adapter._app, adapter)
            async with client.get(
                base + "/talaria/v1/commands/startup-contract-command",
                headers={"Authorization": f"Bearer {key}"},
            ) as response:
                assert (await response.json())["status"] == "running"
            closing = asyncio.create_task(adapter.disconnect())
            await asyncio.wait_for(cleanup_started.wait(), 5)
            assert not closing.done(), "Cleanup abandoned an active command"
        finally:
            release.set()
            if closing is not None:
                await asyncio.wait_for(closing, 5)
    assert adapter.active_agent_work_count() == 0


async def startup():
    manager = discover(True)
    factories = manager.get_platform_handler_factories("api_server")
    factory = next(factory for factory, name in factories if name == "talaria-webui")
    commands = importlib.import_module(factory.__module__.rsplit(".", 1)[0] + ".commands")
    cleanup_started = asyncio.Event()
    original_close = commands.CommandJobs.close

    async def close(jobs, app):
        cleanup_started.set()
        await original_close(jobs, app)

    adapter = native_adapter()
    try:
        with patch.object(commands.CommandJobs, "close", close):
            assert await adapter.connect()
        app = adapter._app
        router, cleanup = app.router, app.on_cleanup
        callbacks = tuple(cleanup)
        base = f"http://127.0.0.1:{adapter._runner.addresses[0][1]}"
        async with ClientSession() as client:
            await verify_routes(client, base)
            # A repeated native callback must preserve routes and the command store.
            factory(app, adapter)
            discover(True)
            adapter.rewire_plugin_handlers()
            assert app.router is router and app.on_cleanup is cleanup
            assert tuple(cleanup) == callbacks
            await verify_routes(client, base)
            await verify_command_cleanup(adapter, client, base, factory, commands, cleanup_started)
    finally:
        await adapter.disconnect()


async def late_load():
    manager = discover(False)
    assert not manager.get_platform_handler_factories("api_server")
    adapter = native_adapter()
    try:
        assert await adapter.connect()
        app = adapter._app
        router, cleanup = app.router, app.on_cleanup
        resources, callbacks = tuple(router.resources()), tuple(cleanup)
        discover(True)
        adapter.rewire_plugin_handlers()
        adapter.rewire_plugin_handlers()
        assert app.router is router and app.on_cleanup is cleanup
        assert tuple(router.resources()) == resources and tuple(cleanup) == callbacks
        assert "Talaria API registration requires a gateway restart" in messages.getvalue()
        assert "handler factory raised" not in messages.getvalue()
        base = f"http://127.0.0.1:{adapter._runner.addresses[0][1]}"
        async with ClientSession() as client:
            async with client.get(base + "/health") as response:
                assert response.status == 200
            # A first-time late load is deferred, never advertised as working HTTP access.
            async with client.get(base + "/talaria/v1/capabilities") as response:
                assert response.status == 404
        factory = next(
            f
            for f, name in manager.get_platform_handler_factories("api_server")
            if name == "talaria-webui"
        )
        for part in ("router", "on_cleanup"):
            pending = web.Application()
            original_callbacks = tuple(pending.on_cleanup)
            getattr(pending, part).freeze()
            factory(pending, adapter)
            assert not list(pending.router.resources())
            assert tuple(pending.on_cleanup) == original_callbacks
    finally:
        await adapter.disconnect()


async def main():
    try:
        await late_load()
        # Restart recovery uses a fresh native application and ordinary startup discovery.
        await startup()
        await startup()
    finally:
        get_plugin_manager().unload()
        logging.getLogger().removeHandler(handler)


asyncio.run(main())

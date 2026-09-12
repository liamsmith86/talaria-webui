"""CLI signals must drain live responses before ASGI lifespan cleanup."""

import asyncio
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from talaria import auth
from talaria.app import create_app
from talaria.config import Settings, save, save_data
from talaria.server import Server

from .conftest import serve


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for the isolated CLI fixture")


@pytest.fixture
def peer():
    accepted, release = threading.Event(), threading.Event()
    opened, closed = [], []

    async def handle(request):
        if request.url.path.endswith("/events"):

            async def frames():
                opened.append(request.url.path)
                try:
                    yield 'data: {"event":"message.delta","delta":"fixture"}\n\n'
                    await asyncio.Event().wait()
                finally:
                    closed.append(request.url.path)

            return StreamingResponse(frames(), media_type="text/event-stream")
        accepted.set()
        await asyncio.to_thread(release.wait, 5)
        return JSONResponse({"run_id": "accepted-during-shutdown"})

    app = Starlette(routes=[Route("/{path:path}", handle, methods=["GET", "POST"])])
    server, thread, url = serve(app)
    yield SimpleNamespace(url=url, accepted=accepted, release=release, opened=opened, closed=closed)
    release.set()
    server.should_exit = True
    thread.join(3)
    assert not thread.is_alive()


@pytest.fixture
def cli_process(tmp_path, peer):
    @contextmanager
    def launch(development=False, port=0):
        settings = Settings(
            hermes_url=peer.url,
            port=port,
            password_hash=auth.hash_password("fixture-password"),
            signing_key="fixture-signing-key",
        )
        path = tmp_path / "config.json"
        save(path, settings)
        save_data(
            path.with_name("profiles.json"),
            {
                "version": 1,
                "profiles": [
                    {
                        "id": "a" * 32,
                        "label": "Research",
                        "url": peer.url + "/p/research",
                        "api_key": "fixture-key",
                    }
                ],
            },
        )
        log_path = tmp_path / "cli.log"
        env = {key: value for key, value in os.environ.items() if not key.startswith("TALARIA_")}
        env["WEB_CONCURRENCY"] = "1"
        args = [
            sys.executable,
            "-c",
            "import fcntl, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
            "from talaria.cli import main; main()",
            "--config",
            str(path),
        ]
        if development:
            args.append("--dev")
        terminal, slave = os.openpty()
        try:
            with log_path.open("w") as log:
                process = subprocess.Popen(
                    args,
                    stdin=slave,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=True,
                )
        except BaseException:
            os.close(terminal)
            raise
        finally:
            os.close(slave)
        try:
            yield SimpleNamespace(
                process=process, log=log_path, settings=settings, terminal=terminal
            )
        finally:
            try:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            finally:
                os.close(terminal)

    return launch


@contextmanager
def connect(cli, development=False):
    match = wait_until(lambda: re.search(r"running on (http://[^ ]+)", cli.log.read_text()))
    cookie = "talaria_dev_session" if development else auth.COOKIE
    client = httpx.Client(
        base_url=match[1],
        cookies={cookie: auth.issue_cookie(cli.settings)},
        timeout=3,
        trust_env=False,
    )

    def ready():
        try:
            return client.get("/health").status_code == 200
        except httpx.TransportError:
            return False

    try:
        wait_until(ready)
        yield client
    finally:
        client.close()


@pytest.mark.parametrize("development", [False, True])
@pytest.mark.parametrize("live", [False, True])
def test_ctrl_c_exits_promptly_with_idle_connections_or_live_profiles(
    cli_process, peer, development, live
):
    with cli_process(development) as cli, ExitStack() as stack:
        client = stack.enter_context(connect(cli, development))
        streams = []
        if live:
            for profile in ("default", "a" * 32):
                response = stack.enter_context(
                    client.stream(
                        "GET", "/api/runs/fixture/events", params={"talaria_profile": profile}
                    )
                )
                assert response.status_code == 200
                lines = response.iter_lines()
                assert any('"delta":"fixture"' in line for line in lines)
                streams.append(lines)
            assert len(peer.opened) == 2
        started = time.monotonic()
        # Send actual Ctrl+C through the controlling terminal, including to
        # Uvicorn's reload child when development mode is active.
        os.write(cli.terminal, b"\x03")
        cli.process.wait(timeout=3)
        assert time.monotonic() - started < 3
        output = cli.log.read_text()
        assert cli.process.returncode == 0, output
        assert "Application shutdown complete." in output
        assert not any(text in output for text in ("ERROR", "Traceback", "CancelledError")), output
        for lines in streams:
            list(lines)  # A complete HTTP response, with no truncated chunked body.
        wait_until(lambda: sorted(peer.closed) == sorted(peer.opened))


def test_startup_failure_is_still_an_error(cli_process):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        with cli_process(port=occupied.getsockname()[1]) as cli:
            cli.process.wait(timeout=3)
            assert cli.process.returncode == 3
            output = cli.log.read_text()
            assert "ERROR" in output and "address already in use" in output.lower()
            assert "Application shutdown complete." in output


def test_cli_keeps_its_single_worker_contract(monkeypatch, caplog):
    from talaria.server import run

    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(SystemExit) as error:
        run(object(), log_config=None)
    assert error.value.code == 3
    assert "Set WEB_CONCURRENCY=1" in caplog.text


def test_shutdown_preserves_an_inflight_run_acceptance(cli_process, peer):
    with cli_process() as cli, connect(cli) as client, ThreadPoolExecutor(1) as pool:
        cookie = client.cookies[auth.COOKIE]
        future = pool.submit(
            client.post,
            "/api/runs",
            json={"session_id": "fixture", "input": "A delayed admission"},
            headers={
                "X-Talaria-Request": "1",
                "X-CSRF-Token": auth.signature(cli.settings, "csrf:" + cookie),
            },
        )
        assert peer.accepted.wait(3)
        os.write(cli.terminal, b"\x03")
        wait_until(lambda: "Shutting down" in cli.log.read_text())
        peer.release.set()
        response = future.result(timeout=3)
        assert response.status_code == 202
        assert response.json() == {"run_id": "accepted-during-shutdown"}
        cli.process.wait(timeout=3)
        assert cli.process.returncode == 0
        assert not peer.opened
        assert "ERROR" not in cli.log.read_text()


async def test_early_stream_drain_keeps_clients_and_pending_writes_until_lifespan_exit(tmp_path):
    app = create_app(
        Settings(), tmp_path / "config.json", transport=httpx.MockTransport(lambda r: None)
    )
    profiles = app.state.profiles
    release = asyncio.Event()
    saved = tmp_path / "saved.txt"

    async def write():
        await release.wait()
        assert not app.state.hermes.client.is_closed
        saved.write_text("committed")

    async with app.router.lifespan_context(app) as state:
        pending = asyncio.create_task(profiles.finish_mutation(write()))
        await asyncio.sleep(0)
        await state["talaria_stop_streams"]()
        assert not pending.done()
        assert not app.state.hermes.client.is_closed
        profiles.records["lazy"] = {"url": "http://fixture.invalid", "api_key": "fixture-key"}
        lazy = profiles.resolve("lazy")
        channel = profiles.attach(lazy.state, "late-admission")
        assert channel.finished and channel.task is None
        assert [frame async for frame in app.state.relay.stream(channel, 0)] == [": connected\n\n"]
        asyncio.get_running_loop().call_soon(release.set)
    await pending
    assert saved.read_text() == "committed"
    assert all(profile.state.hermes.client.is_closed for profile in profiles.apps.values())


async def test_early_drain_failure_still_runs_cleanup_and_remains_visible(monkeypatch):
    cleaned = []

    async def fail():
        raise RuntimeError("Fixture cleanup failure")

    async def shutdown(server, sockets=None):
        cleaned.append(True)

    server = Server(uvicorn.Config(None))
    server.lifespan = SimpleNamespace(state={"talaria_stop_streams": fail})
    monkeypatch.setattr(uvicorn.Server, "shutdown", shutdown)
    with pytest.raises(RuntimeError, match="Fixture cleanup failure"):
        await server.shutdown()
    assert cleaned == [True]

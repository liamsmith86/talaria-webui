import socket
import threading
import time

import pytest
import uvicorn

from talaria.app import create_app
from talaria.auth import hash_password
from talaria.config import Settings

from .fake_hermes import KEY, FakeHermes


def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.02)
    else:
        raise RuntimeError("Test server did not start")
    return server, thread, f"http://127.0.0.1:{port}"


@pytest.fixture
def live_app(tmp_path):
    peer = FakeHermes()
    upstream, ut, url = serve(peer.app)
    settings = Settings(
        hermes_url=url,
        api_key=KEY,
        password_hash=hash_password("test-password"),
        signing_key="test-signing-key",
    )
    app = create_app(settings, tmp_path / "config.json")
    server, thread, address = serve(app)
    yield address, peer, app
    server.should_exit = True
    thread.join(6)
    upstream.should_exit = True
    ut.join(6)
    assert not thread.is_alive(), "Talaria did not shut down cleanly"


@pytest.fixture
def playwright_runtime():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as runtime:
        yield runtime


@pytest.fixture
def page(playwright_runtime, live_app):
    import os

    browser = getattr(playwright_runtime, os.getenv("TALARIA_TEST_BROWSER", "chromium")).launch()
    context = browser.new_context(viewport={"width": 1440, "height": 960})
    page = context.new_page()
    page.goto(live_app[0])
    page.get_by_label("Password", exact=True).fill("test-password")
    page.get_by_role("button", name="Step inside").click()
    page.locator(".topbar-title").wait_for()
    # Start scenarios after the simulator's initial discovery. Reloading while those
    # reads are pending produces WebKit navigation-cancellation diagnostics.
    state = page.evaluate_handle("async () => (await import('/static/store.js')).state")
    page.wait_for_function(
        "state => !!state.defaultModel && state.readiness.status === 'ok'", arg=state
    )
    state.dispose()
    yield page
    context.close()
    browser.close()

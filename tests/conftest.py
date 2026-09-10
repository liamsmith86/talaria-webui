import os
import socket
import threading
import time

import pytest
import uvicorn

from talaria.app import create_app
from talaria.auth import hash_password
from talaria.config import Settings

from .fake_hermes import KEY, FakeHermes


def pytest_addoption(parser):
    parser.addoption("--fail-on-skip", action="store_true", help="Fail CI on missing test setup")


def pytest_collection_modifyitems(items):
    for item in items:
        if "playwright_runtime" in item.fixturenames:
            item.add_marker(pytest.mark.browser)


def pytest_sessionfinish(session, exitstatus):
    if session.config.getoption("--fail-on-skip"):
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter and reporter.stats.get("skipped"):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def serve(app):
    # Some hosts allocate low ephemeral ports, including browser-blocked ports.
    # Keep the socket bound while selecting a port outside that range.
    while True:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        if port >= 16384:
            break
        sock.close()
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


@pytest.fixture(scope="session")
def playwright_runtime():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as runtime:
        yield runtime


@pytest.fixture(scope="session")
def browser(playwright_runtime):
    browser = getattr(playwright_runtime, os.getenv("TALARIA_TEST_BROWSER", "chromium")).launch()
    yield browser
    browser.close()


@pytest.fixture
def page(browser, live_app):
    # Reuse the process, but isolate cookies, storage, permissions, and routes per test.
    context = browser.new_context(viewport={"width": 1440, "height": 960})
    try:
        page = context.new_page()
        page.goto(live_app[0])
        page.get_by_label("Password", exact=True).fill("test-password")
        page.get_by_role("button", name="Step inside").click()
        page.locator(".topbar-title").wait_for()
        # Let the simulator's initial discovery finish before starting a scenario.
        state = page.evaluate_handle("async () => (await import('/static/store.js')).state")
        page.wait_for_function(
            "state => !!state.defaultModel && state.readiness.status === 'ok'", arg=state
        )
        state.dispose()
        yield page
    finally:
        # Let intercepted requests finish before closing their response context.
        for tab in context.pages:
            tab.unroute_all(behavior="wait")
        context.close()

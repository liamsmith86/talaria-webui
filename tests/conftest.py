import os
import re
import socket
import threading
import time
from contextlib import ExitStack, contextmanager
from functools import wraps
from pathlib import Path
from unittest.mock import patch

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from talaria.app import create_app
from talaria.auth import hash_password
from talaria.config import Settings

from .fake_hermes import KEY, FakeHermes


def pytest_addoption(parser):
    parser.addoption("--fail-on-skip", action="store_true", help="Fail CI on missing test setup")
    parser.addoption("--browser-shard", help="Run browser partition INDEX/TOTAL (one-based)")


def browser_shard(items, shard):
    if not re.fullmatch(r"[1-9][0-9]*/[1-9][0-9]*", shard):
        raise pytest.UsageError("--browser-shard must be INDEX/TOTAL, with 1 <= INDEX <= TOTAL")
    index, total = map(int, shard.split("/"))
    if not 1 <= index <= total <= 16:
        raise pytest.UsageError("--browser-shard requires 1 <= INDEX <= TOTAL <= 16")
    # Round-robin cases, including parameters, spreads expensive files across jobs.
    return sorted(items, key=lambda item: item.nodeid)[index - 1 :: total]


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        if "playwright_runtime" in item.fixturenames:
            item.add_marker(pytest.mark.browser)
    if shard := config.getoption("--browser-shard"):
        if config.option.markexpr != "browser":
            raise pytest.UsageError("--browser-shard requires '-m browser'")
        selected = browser_shard([item for item in items if "browser" in item.keywords], shard)
        selected_set = set(selected)
        config.hook.pytest_deselected(items=[item for item in items if item not in selected_set])
        items[:] = selected


def pytest_sessionfinish(session, exitstatus):
    if session.config.getoption("--fail-on-skip"):
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter and reporter.stats.get("skipped"):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def serve(app, port=0):
    # Some hosts allocate low ephemeral ports, including browser-blocked ports.
    # Keep the socket bound while selecting a port outside that range.
    while True:
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        bound_port = sock.getsockname()[1]
        if bound_port >= 16384:
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
    return server, thread, f"http://127.0.0.1:{bound_port}"


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
    from playwright.sync_api import Frame, Page, sync_playwright

    def checked_wait(original):
        @wraps(original)
        def wait(self, expression, **kwargs):
            if re.match(r"\s*\(*\s*async\b", expression):
                raise ValueError("Use a synchronous polling predicate or wait_for_store().")
            return original(self, expression, **kwargs)

        return wait

    with ExitStack() as stack, sync_playwright() as runtime:
        for cls in (Page, Frame):
            stack.enter_context(
                patch.object(cls, "wait_for_function", checked_wait(cls.wait_for_function))
            )
        yield runtime


@pytest.fixture(scope="session")
def browser(playwright_runtime):
    browser = getattr(playwright_runtime, os.getenv("TALARIA_TEST_BROWSER", "chromium")).launch()
    yield browser
    browser.close()


@contextmanager
def browser_context(browser):
    # Reuse the process, but isolate cookies, storage, permissions, and routes per test.
    context = browser.new_context(viewport={"width": 1440, "height": 960})
    errors = capture_browser_errors(context)
    try:
        yield context
        assert not errors, f"Unhandled browser errors: {errors}"
    finally:
        # Let intercepted requests finish before closing their response context.
        for tab in context.pages:
            tab.unroute_all(behavior="wait")
        context.close()


@pytest.fixture
def page(browser, live_app, request):
    with browser_context(browser) as context:
        page = context.new_page()
        if request.node.get_closest_marker("clock"):
            page.clock.install()
        page.goto(live_app[0])
        page.get_by_label("Password", exact=True).fill("test-password")
        page.get_by_role("button", name="Sign in").click()
        page.locator(".topbar-title").wait_for()
        # Discovery and listing finish before tests install artificial session state.
        wait_for_store(page, "state => !!state.defaultModel && state.readiness.status === 'ok'")
        page.evaluate("async () => (await import('/static/store.js')).refreshSessions()")
        yield page


@pytest.fixture(scope="session")
def module_server():
    """Real styles/vendor scripts, without application startup or an API server."""
    static = Path(__file__).resolve().parents[1] / "src/talaria/static"
    document = (static / "index.html").read_text().split("<body>", 1)[0] + "<body></body></html>"
    document = re.sub(
        r'<script type="module"[^>]*></script>|<link rel="modulepreload"[^>]*>', "", document
    )
    document = document.replace("__TALARIA_BASE__", "/")

    async def index(request):
        return HTMLResponse(document)

    app = Starlette(routes=[Route("/", index), Mount("/static", StaticFiles(directory=static))])
    server, thread, address = serve(app)
    yield address
    server.should_exit = True
    thread.join(6)
    assert not thread.is_alive(), "Module test server did not shut down cleanly"


@pytest.fixture
def module_page(browser, module_server):
    with browser_context(browser) as context:
        page = context.new_page()
        page.goto(module_server)
        yield page


def capture_browser_errors(context):
    # WebKit's inspector promotes some caught unload-time fetch diagnostics to
    # Playwright pageerror. DOM events identify actual uncaught exceptions and
    # rejected promises, without allowing particular messages or browser types.
    errors = []
    context.expose_binding("__talariaReportError", lambda source, error: errors.append(error))
    context.add_init_script("""(() => {
      const report = error => window.__talariaReportError(error).catch(() => {});
      addEventListener('error', event => {
        if (event instanceof ErrorEvent) report({
          kind: 'error', message: event.message,
          stack: event.error?.stack || '', url: event.filename || location.href,
        });
      });
      addEventListener('unhandledrejection', event => report({
        kind: 'rejection', message: String(event.reason),
        stack: event.reason?.stack || '', url: location.href,
      }));
    })()""")
    return errors


def wait_for_store(page, predicate):
    # Playwright polls synchronously: an async predicate is a truthy Promise,
    # so it can resolve false once instead of waiting for the desired state.
    state = page.evaluate_handle("async () => (await import('/static/store.js')).state")
    try:
        page.wait_for_function(predicate, arg=state)
    finally:
        state.dispose()

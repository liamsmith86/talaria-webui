import json
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

# Behavior that can differ between browser engines: streaming and reconnecting,
# input and gestures, scrolling and layout, images and storage, and locale formats.
# Releases run these on Firefox and WebKit too; the rest run there nightly.
CROSS_BROWSER = {
    "test_browser.py",
    "test_conversation_features.py",
    "test_frontend_recovery.py",
    "test_frontend_ui_qa.py",
    "test_i18n.py",
    "test_image_draft_migration.py",
    "test_keyboard_continuity.py",
    "test_markdown_streaming.py",
    "test_message_submission.py",
    "test_native_interactions.py",
    "test_reply_navigation.py",
    "test_reply_settlement.py",
    "test_resilience.py",
    "test_response_turns.py",
    "test_sidebar_collapse.py",
    "test_stream_reveal.py",
}


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
            if item.path.name in CROSS_BROWSER:
                item.add_marker(pytest.mark.cross_browser)
    if shard := config.getoption("--browser-shard"):
        mark = config.option.markexpr
        if mark not in {"browser", "cross_browser"}:
            raise pytest.UsageError("--browser-shard requires '-m browser' or '-m cross_browser'")
        selected = browser_shard([item for item in items if mark in item.keywords], shard)
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
        # Naming TCP matters: asyncio sets TCP_NODELAY only then, as for a production bind.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        bound_port = sock.getsockname()[1]
        if bound_port >= 16384:
            break
        sock.close()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    # A ceiling only: a healthy start returns at once, and a CI runner can stall for seconds.
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("Test server did not start")
        time.sleep(0.02)
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
    from playwright.sync_api import Frame, Page, expect, sync_playwright

    # A ceiling only: a met condition returns at once, and a CI runner can stall.
    expect.set_options(timeout=15_000)

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
        else:
            hold_timers(page, *BACKGROUND_TIMERS)
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


# The periodic check, and what a toast, scroll controls and copy feedback wait
# for before leaving. Run on real time, a slow test step loses a race with them.
BACKGROUND_TIMERS = (15000, 3500, 2000, 1800)

HOLD_TIMERS = """delays => {
  if (window.heldTimers) return delays.forEach((delay) => heldDelays.add(delay));
  window.heldTimers = new Map();
  window.heldDelays = new Set(delays);
  const {setTimeout: start, clearTimeout: cancel} = window;
  let next = -1;
  window.setTimeout = (run, delay, ...args) => {
    if (!heldDelays.has(delay)) return start(run, delay, ...args);
    heldTimers.set(next, {delay, run: () => run(...args)});
    return next--;
  };
  window.clearTimeout = (id) => { heldTimers.delete(id); cancel(id); };
  window.fireHeldTimers = (delay) => {
    for (const [id, timer] of heldTimers) {
      if (delay != null && timer.delay !== delay) continue;
      heldTimers.delete(id);
      timer.run();
    }
  };
}"""


def hold_timers(page, *delays):
    """Keep timers of these lengths from firing until fire_timers, across reloads too."""
    page.add_init_script(f"({HOLD_TIMERS})({json.dumps(delays)})")
    page.evaluate(HOLD_TIMERS, list(delays))


def fire_timers(page, delay=None):
    page.evaluate("delay => fireHeldTimers(delay)", delay)


def wait_for_timer(page, delay):
    page.wait_for_function("d => [...heldTimers.values()].some((t) => t.delay === d)", arg=delay)


# The store instance the page runs, wherever its document loaded the modules from.
STORE_URL = (
    "new URL('store.js', document.querySelector('script[type=module]')?.src"
    " ?? new URL('/static/', location))"
)


def wait_for_store(page, predicate):
    # Playwright polls synchronously: an async predicate is a truthy Promise,
    # so it can resolve false once instead of waiting for the desired state.
    state = page.evaluate_handle(f"async () => (await import({STORE_URL})).state")
    try:
        page.wait_for_function(predicate, arg=state)
    finally:
        state.dispose()


def wait_in_browser(page, read, timeout=10):
    """Poll a value that Playwright's route and request events fill in, and return it."""
    deadline = time.monotonic() + timeout
    while not (value := read()):
        assert time.monotonic() < deadline, "Timed out waiting for the browser"
        page.wait_for_timeout(50)
    return value


def wait_for(read, predicate, timeout=8):
    """Poll a server-side value until it satisfies predicate."""
    deadline = time.monotonic() + timeout
    while True:
        value = read()
        if predicate(value):
            return value
        assert time.monotonic() < deadline, f"Timed out; last value: {value!r}"
        time.sleep(0.1)

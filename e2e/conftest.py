"""End-to-end checks: the real WebUI and a real Hermes gateway.

Each run starts a throwaway gateway with this checkout's plugin, serves Talaria
against it and drives Chromium the way people use Talaria. Every test ends by
comparing the settled conversation with a fresh reload of the same session.

Tests in scripted/ use a loopback model that follows a plan, so they can only fail
on Talaria and Hermes. test_real_model.py checks that a real provider works too.

    TALARIA_E2E_HERMES=/opt/hermes-agent/current OPENROUTER_API_KEY=... \\
        uv run --locked pytest e2e

The tools run as the current user in a temporary workspace, so run the suite as an
unprivileged user without credentials (see CONTRIBUTING.md).
"""

import difflib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright
from scripted_model import scripted_model

from talaria.app import create_app
from talaria.auth import hash_password
from talaria.config import Settings

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "src/talaria/hermes_plugin"
HERMES = os.getenv("TALARIA_E2E_HERMES")
PROVIDER_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = os.getenv("TALARIA_E2E_MODEL", "z-ai/glm-5.3-flash")
PASSWORD = "e2e-password"
REPLY_TIMEOUT = 240_000
SCRIPTED_TESTS = Path(__file__).parent / "scripted"
usage = {}


def pytest_configure(config):
    config.addinivalue_line("markers", "no_reload_check: the test checks its own reloads")
    config.addinivalue_line("markers", "viewport(width, height): browser viewport size")


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="Set TALARIA_E2E_HERMES and OPENROUTER_API_KEY")
    for item in items:
        scripted = item.path.is_relative_to(SCRIPTED_TESTS)
        if not HERMES or not (PROVIDER_KEY or scripted):
            item.add_marker(skip)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    setattr(item, f"report_{report.when}", report)
    return report


def pytest_sessionstart(session):
    # Measured once by the controlling process, also with parallel workers.
    if HERMES and PROVIDER_KEY and not hasattr(session.config, "workerinput"):
        usage["before"] = provider_usage()


def pytest_terminal_summary(terminalreporter):
    if usage.get("before") is not None and (after := provider_usage()) is not None:
        # The key's account-wide usage, which updates within a minute or so.
        spent = after - usage["before"]
        terminalreporter.write_line(f"OpenRouter spend for {MODEL} (approximate): ${spent:.4f}")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fetch(url, key=None, timeout=5):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def provider_usage():
    try:
        return float(fetch("https://openrouter.ai/api/v1/key", PROVIDER_KEY)["data"]["usage"])
    except Exception:
        return None


@pytest.fixture(scope="session")
def artifacts(tmp_path_factory):
    path = Path(os.getenv("TALARIA_E2E_ARTIFACTS") or tmp_path_factory.mktemp("e2e-artifacts"))
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Gateway:
    url: str
    key: str
    workspace: Path
    command: list
    env: dict
    log: Path
    version: str
    process: subprocess.Popen | None = None

    def start(self):
        with self.log.open("ab") as log:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.workspace,
                env=self.env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        plugin = None
        for _ in range(180):
            if self.process.poll() is not None:
                break
            try:
                plugin = fetch(f"{self.url}/talaria/v1/capabilities", self.key)
                break
            except Exception:
                time.sleep(1)
        if self.process.poll() is not None or not plugin:
            tail = self.log.read_text(errors="replace")[-4000:]
            pytest.fail(f"Hermes gateway did not start:\n{tail}")
        assert plugin.get("plugin_version") == self.version, plugin

    def stop(self):
        if self.process is None or self.process.poll() is not None:
            return
        os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(30)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(10)

    def restart(self):
        self.stop()
        self.start()


@contextmanager
def gateway(tmp_path_factory, artifacts, **settings):
    """A throwaway gateway with this checkout's plugin. Settings override its config."""
    home = tmp_path_factory.mktemp("hermes-home")
    workspace = tmp_path_factory.mktemp("workspace")
    port = free_port()
    key = secrets.token_urlsafe(24)
    # JSON is YAML, and avoids a YAML dependency here.
    config = {
        "model": {"default": MODEL, "provider": "openrouter"},
        "platforms": {"api_server": {"enabled": True, "host": "127.0.0.1", "port": port}},
        "gateway": {"multiplex_profiles": False},
        "plugins": {"enabled": ["talaria-webui"]},
        "terminal": {"cwd": str(workspace)},
        # Tests share one gateway; memories would carry between them.
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        # API sessions omit clarify by default; Talaria answers it inline.
        "platform_toolsets": {"api_server": ["hermes-api-server", "clarify"]},
        # Ask in Talaria for every flagged command, instead of an auxiliary model.
        "approvals": {"mode": "manual"},
    } | settings
    (home / "config.yaml").write_text(json.dumps(config, indent=2))
    env_file = home / ".env"
    # Only a real model's gateway may spend: Hermes also uses this key for auxiliary tasks.
    provider = config["model"]["provider"]
    provider_key = f"OPENROUTER_API_KEY={PROVIDER_KEY}\n" if provider == "openrouter" else ""
    env_file.write_text(f"{provider_key}API_SERVER_ENABLED=true\nAPI_SERVER_KEY={key}\n")
    env_file.chmod(0o600)
    target = home / "plugins/talaria-webui"
    target.mkdir(parents=True)
    for path in PLUGIN.iterdir():
        if path.is_file() and (path.suffix in {".py", ".yaml"} or path.name == "README.md"):
            shutil.copyfile(path, target / path.name)
    shutil.copyfile(ROOT / "LICENSE", target / "LICENSE")
    version = next(
        line.split('"')[1]
        for line in (PLUGIN / "plugin.yaml").read_text().splitlines()
        if line.startswith("version:")
    )
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("TALARIA_", "OPENROUTER_", "HERMES_"))
    }
    env |= {
        "HERMES_HOME": str(home),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    running = Gateway(
        url=f"http://127.0.0.1:{port}",
        key=key,
        workspace=workspace,
        command=[
            str(Path(HERMES) / "venv/bin/hermes"),
            "gateway",
            "run",
            "--replace",
            "--external-supervisor",
        ],
        env=env,
        log=artifacts / f"gateway-{provider}-{os.getenv('PYTEST_XDIST_WORKER', 'main')}.log",
        version=version,
    )
    try:
        running.start()
        yield running
    finally:
        running.stop()


@pytest.fixture(scope="session")
def hermes(tmp_path_factory, artifacts):
    with gateway(tmp_path_factory, artifacts) as running:
        yield running


@pytest.fixture
def token(hermes):
    """Write a random value to a workspace file, which a model can only read with a tool."""

    def write(name):
        value = f"tok-{secrets.token_hex(4)}"
        (hermes.workspace / name).write_text(value + "\n")
        return value

    return write


@pytest.fixture(scope="session")
def scripted_hermes(tmp_path_factory, artifacts):
    with (
        scripted_model() as url,
        gateway(
            tmp_path_factory,
            artifacts,
            model={
                "default": "scripted",
                "provider": "custom",
                "base_url": url,
                "api_key": "none",
                "context_length": 128_000,
            },
            auxiliary={"title_generation": {"enabled": False}},
        ) as running,
    ):
        yield running


@contextmanager
def serve_talaria(hermes, tmp_path_factory):
    settings = Settings(
        hermes_url=hermes.url,
        api_key=hermes.key,
        password_hash=hash_password(PASSWORD),
        signing_key=secrets.token_hex(32),
    )
    app = create_app(settings, tmp_path_factory.mktemp("talaria") / "config.json")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(250):
        if server.started:
            break
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(10)


@pytest.fixture(scope="session")
def talaria(hermes, tmp_path_factory):
    with serve_talaria(hermes, tmp_path_factory) as url:
        yield url


@pytest.fixture(scope="session")
def scripted_talaria(scripted_hermes, tmp_path_factory):
    with serve_talaria(scripted_hermes, tmp_path_factory) as url:
        yield url


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        yield browser
        browser.close()


# The settled view of each message: what people read, without timestamps,
# durations or controls. A reload of the same session must show the same.
SNAPSHOT = """() => {
  // Text content, so folded and expanded steps compare alike. Streamed tables
  // and Markdown blocks may have no whitespace between cells or paragraphs,
  // which is invisible but not to textContent.
  const text = (node) => {
    const copy = node.cloneNode(true);
    copy.querySelectorAll("th, td, .markdown-block, p, li, h1, h2, h3, h4, h5, h6, pre, blockquote")
      .forEach((block) => block.append(" "));
    return copy.textContent.replace(/\\s+/g, " ").trim();
  };
  const parts = ".user-content, .message-text, .activity > summary .tool-label, "
    + ".tool-card > summary .tool-label, .tool-status.failed, .reasoning > summary .tool-label, "
    + ".guidance, .agent-card > summary, .run-notice, .run-error, .notice summary";
  return [...document.querySelectorAll(".conversation-content > .message")].map((message) => {
    const role = ["user", "assistant", "notice"].find((r) => message.classList.contains(r));
    const shown = [...message.querySelectorAll(parts)]
      .map((node) => (node.matches(".tool-status.failed") ? "(failed)" : text(node)))
      .filter(Boolean);
    return `${role}: ${shown.join(" | ")}`;
  });
}"""

SETTLED = """(state) => {
  const live = state.active && state.lives[state.active];
  const done = ["completed", "failed", "cancelled", "interrupted", "unknown"];
  // A completed reply settles once its saved rows replace the live copy.
  return !state.loading &&
    (!live || (done.includes(live.status) && (live.persisted || live.status !== "completed")));
}"""


class Chat:
    def __init__(self, page, talaria):
        self.page = page
        self.url = talaria
        self.box = page.get_by_label("Message Hermes")
        self.stop = page.get_by_role("button", name="Stop response", exact=True)
        self.messages = page.locator(".conversation-content > .message")

    def store(self, predicate, timeout=REPLY_TIMEOUT):
        state = self.page.evaluate_handle("async () => (await import('/static/store.js')).state")
        try:
            self.page.wait_for_function(predicate, arg=state, timeout=timeout)
        finally:
            state.dispose()

    def value(self, expression):
        load = "const { state } = await import('/static/store.js');"
        return self.page.evaluate(f"async () => {{ {load} return {expression}; }}")

    def new_session(self):
        sidebar = self.page.get_by_role("button", name="Open sidebar")
        if sidebar.is_visible():
            sidebar.click()
        self.page.get_by_role("button", name="New session").first.click()
        expect(self.box).to_be_editable()

    def submit(self, text):
        users = self.page.locator(".conversation-content > .message.user").count()
        self.box.fill(text)
        if text.startswith("/"):
            # A slash command's first Enter completes the picker's selection.
            self.box.press("Enter")
            if self.box.input_value().strip():
                self.box.press("Enter")
            return
        # Enter adds a line on touch screens, so send the way people do there too.
        self.page.get_by_role("button", name="Send message", exact=True).click()
        expect(self.page.locator(".conversation-content > .message.user")).to_have_count(
            users + 1, timeout=30_000
        )

    def settle(self, timeout=REPLY_TIMEOUT):
        expect(self.stop).to_be_hidden(timeout=timeout)
        self.store(SETTLED, timeout)
        expect(self.page.locator(".conversation-content .working")).to_have_count(0)

    def confirm(self, name):
        """Accept the open dialog, which closes once its action has been admitted."""
        dialog = self.page.get_by_role("dialog")
        dialog.get_by_role("button", name=name, exact=True).click()
        expect(dialog).to_have_count(0)

    def send(self, text, timeout=REPLY_TIMEOUT):
        self.submit(text)
        self.settle(timeout)
        return self.reply()

    def reply(self):
        last = self.page.locator(".conversation-content > .message.assistant").last
        return last.locator(".message-text").last.inner_text()

    def snapshot(self):
        return self.page.evaluate(SNAPSHOT)

    def reload(self, history=True):
        self.page.reload()
        expect(self.box).to_be_visible(timeout=30_000)
        self.store(SETTLED, 60_000)
        if history:
            self.store("s => !!s.active && s.history.length > 0", 60_000)
        self.page.wait_for_timeout(500)

    def assert_reload_matches(self):
        settled = self.snapshot()
        self.reload(history=bool(settled))
        reloaded = self.snapshot()
        if settled != reloaded:
            diff = "\n".join(
                difflib.unified_diff(settled, reloaded, "settled", "reloaded", lineterm="")
            )
            raise AssertionError(f"The reloaded session differs from the settled view:\n{diff}")


def sign_in(browser, talaria, problems, width=1280, height=900):
    context = browser.new_context(
        viewport={"width": width, "height": height},
        accept_downloads=True,
        has_touch=width < 600,
        is_mobile=width < 600,
    )
    page = context.new_page()
    page.set_default_timeout(30_000)
    page.on("pageerror", lambda error: problems.append(f"page error: {error}"))
    page.on("console", lambda m: m.type == "error" and problems.append(f"console: {m.text}"))
    page.on(
        "response",
        lambda r: (
            r.status >= 500 and problems.append(f"HTTP {r.status} {r.request.method} {r.url}")
        ),
    )
    page.goto(talaria)
    page.get_by_label("Password", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    session = Chat(page, talaria)
    session.store("s => !!s.defaultModel && s.readiness.status === 'ok'", 60_000)
    return session


@pytest.fixture
def chat(browser, talaria, request, artifacts):
    size = request.node.get_closest_marker("viewport")
    problems = []
    session = sign_in(browser, talaria, problems, *(size.args if size else ()))
    page, context = session.page, session.page.context
    session.new_session()
    yield session
    name = request.node.name.replace("/", "_")
    try:
        call = getattr(request.node, "report_call", None)
        if not call or not call.passed:
            page.screenshot(path=artifacts / f"{name}.png", full_page=True)
        elif not request.node.get_closest_marker("no_reload_check"):
            try:
                session.assert_reload_matches()
            except Exception:
                page.screenshot(path=artifacts / f"{name}-reloaded.png", full_page=True)
                raise
        assert not problems, "\n".join(problems)
    finally:
        context.close()


@pytest.fixture
def device(browser, talaria, request, artifacts):
    """Open a page in another browser with its own storage, like a phone next to a laptop."""
    devices, problems = [], []

    def open_device(url):
        other = sign_in(browser, talaria, problems)
        devices.append(other)
        other.page.goto(url)
        other.store("s => !!s.active && !s.loading", 60_000)
        return other

    yield open_device
    call = getattr(request.node, "report_call", None)
    for index, other in enumerate(devices):
        if not call or not call.passed:
            other.page.screenshot(path=artifacts / f"{request.node.name}-device{index}.png")
        other.page.context.close()
    assert not problems, "\n".join(problems)

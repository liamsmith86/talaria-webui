"""A real gateway restarted while a turn runs, the way Talaria's setup and plugin updates do it.

From 0.22.3, with no terminal and no service manager, plain `hermes gateway restart` refuses while
work runs, and with `--interrupt` alone it becomes the replacement gateway, so setup would wait
until it timed out and killed it. `setup.restart_gateway` passes `--interrupt --detach`, as Hermes's
own updater does: the command must return and leave a gateway that answers.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from talaria.hermes_plugin import bridge
from talaria.setup_hermes import unattended_restart

if not unattended_restart():
    sys.exit(0)  # Older Hermes restarts without these flags and never refuses.

temporary = Path(sys.argv[1])
source = Path(os.environ["HERMES_SOURCE"])
home = temporary / "hermes"
home.mkdir()
held, release = threading.Event(), threading.Event()


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        held.set()
        release.wait(60)
        self.send_response(500)
        self.end_headers()


def free_port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


provider = ThreadingHTTPServer(("127.0.0.1", free_port()), Provider)
threading.Thread(target=provider.serve_forever, daemon=True).start()
port, key = free_port(), "restart-contract-key-0123456789"
(home / "plugins").mkdir()
(home / "plugins/talaria-webui").symlink_to(Path(bridge.__file__).parent)
(home / ".env").write_text(f"API_SERVER_KEY={key}\n")
config = {
    "_config_version": 12,
    "model": {
        "provider": "custom",
        "base_url": f"http://127.0.0.1:{provider.server_port}/v1",
        "default": "restart-contract",
        "api_key": "local-only",
        "context_length": 131072,
    },
    "auxiliary": {"session_title": {"enabled": False}},
    "platforms": {"api_server": {"enabled": True, "host": "127.0.0.1", "port": port, "key": key}},
    "plugins": {"enabled": ["talaria-webui"]},
}
(home / "config.yaml").write_text(json.dumps(config))
user = temporary / "user"
user.mkdir()
environment = {key: os.environ[key] for key in ("PATH", "LANG", "TZ") if key in os.environ}
environment.update(
    HOME=str(user),
    HERMES_HOME=str(home),
    HERMES_GATEWAY_LOCK_DIR=str(temporary / "locks"),
    HERMES_ENABLE_PROJECT_PLUGINS="false",
    PYTHONPATH=os.environ["PYTHONPATH"],
    PYTHONUNBUFFERED="1",
)
client = httpx.Client(
    base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {key}"}, timeout=5
)


def answering(within):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            if client.get("/talaria/v1/capabilities").status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    return False


def hermes(*arguments):
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "gateway", *arguments],
        cwd=home,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


with (temporary / "gateway.log").open("w") as log:
    first = subprocess.Popen(
        [sys.executable, "-m", "gateway.run"],
        cwd=source,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
try:
    assert answering(30), (temporary / "gateway.log").read_text()
    assert client.post("/v1/runs", json={"input": "Wait"}).status_code in (200, 202)
    assert held.wait(30), "The turn never reached the provider"
    restart = hermes("restart", "--interrupt", "--detach")
    assert restart.returncode == 0, restart.stdout + restart.stderr
    first.wait(30)
    assert answering(30), "The replacement gateway does not answer"
    stop = hermes("stop", "--interrupt")
    assert stop.returncode == 0, stop.stdout + stop.stderr
finally:
    release.set()
    if first.poll() is None:
        os.killpg(first.pid, signal.SIGKILL)
    provider.shutdown()

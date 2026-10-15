"""Real multiplexed gateway processes: startup, control-socket activation and restart."""

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from talaria.hermes import APIError, Hermes
from talaria.hermes_plugin import bridge

temporary = Path(sys.argv[1])
assert temporary.is_dir() and str(temporary).startswith("/tmp/")
source = Path(os.environ["HERMES_SOURCE"])
user = temporary / "user"
user.mkdir()
home = temporary / "hermes"
secondary = home / "profiles/research"
keys = {
    "default": "default-startup-contract-key-0123456789",
    "research": "research-startup-contract-key-9876543210",
}
with socket.socket() as reservation:
    reservation.bind(("127.0.0.1", 0))
    port = reservation.getsockname()[1]
base = f"http://127.0.0.1:{port}"
configs = {}
for name, directory in (("default", home), ("research", secondary)):
    directory.mkdir(parents=True)
    shutil.copytree(Path(bridge.__file__).parent, directory / "plugins/talaria-webui")
    (directory / "SOUL.md").write_text(f"Name: Contract {name}\n")
    (directory / ".env").write_text(f"API_SERVER_KEY={keys[name]}\n")
    configs[name] = {
        "_config_version": 12,
        "gateway": {"multiplex_profiles": True},
        "model": {
            "provider": "custom",
            "base_url": "http://127.0.0.1:1/v1",
            "default": "startup-contract",
            "api_key": "local-only",
            "context_length": 8192,
        },
        "auxiliary": {"session_title": {"enabled": False}},
        "platforms": {
            "api_server": {"enabled": True, "host": "127.0.0.1", "port": port, "key": keys[name]}
        },
        "plugins": {"enabled": ["talaria-webui"]},
    }
    (directory / "config.yaml").write_text(json.dumps(configs[name]))

environment = {
    key: os.environ[key] for key in ("PATH", "LANG", "TZ", "PYTHONPATH") if key in os.environ
}
environment.update(
    HOME=str(user),
    USERPROFILE=str(user),
    HERMES_HOME=str(home),
    HERMES_GATEWAY_LOCK_DIR=str(temporary / "locks"),
    HERMES_ENABLE_PROJECT_PLUGINS="false",
    PYTHONUNBUFFERED="1",
)


@contextmanager
def gateway(label):
    gateway_log = home / "logs/gateway.log"
    offset = len(gateway_log.read_text()) if gateway_log.exists() else 0
    output = temporary / f"{label}.log"
    with output.open("w") as stream:
        child = subprocess.Popen(
            [sys.executable, "-m", "gateway.run"],
            cwd=source,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and child.poll() is None:
                if (
                    gateway_log.exists()
                    and "Press Ctrl+C to stop" in gateway_log.read_text()[offset:]
                ):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("Gateway did not start: " + output.read_text())
            yield gateway_log
            assert "handler factory raised" not in gateway_log.read_text()[offset:]
        finally:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            assert child.returncode == 0, output.read_text()


def reload_plugins():
    # Same public control-socket request sent by Hermes's plugin-enable command.
    code = (
        "import os; from pathlib import Path; "
        "from gateway.control_socket import reload_gateway_plugins; "
        "answer = reload_gateway_plugins(Path(os.environ['HERMES_HOME'])); "
        "assert answer and answer.get('reloaded'), answer"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=source,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def capabilities(profile, key, expected=200):
    client = Hermes(base + (f"/p/{profile}" if profile else ""), key)
    try:
        try:
            result = await client.request("GET", "/talaria/v1/capabilities")
        except APIError as error:
            assert error.status == expected, (profile, error)
            return
        assert expected == 200, (profile, result)
        assert result["agent"]["name"] == "Contract " + (profile or "default"), result
    finally:
        await client.close()


async def profiles_work():
    await asyncio.gather(
        capabilities("", keys["default"]),
        *(capabilities(name, keys[name]) for _ in range(3) for name in keys),
        capabilities("research", keys["default"], 401),
        capabilities("default", keys["research"], 401),
        capabilities("missing", keys["default"], 404),
    )


with gateway("cold-start"):
    asyncio.run(profiles_work())
    reload_plugins()
    asyncio.run(profiles_work())

configs["default"]["plugins"]["enabled"] = []
(home / "config.yaml").write_text(json.dumps(configs["default"]))
with gateway("late-enable") as log:
    asyncio.run(capabilities("", keys["default"], 404))
    configs["default"]["plugins"]["enabled"] = ["talaria-webui"]
    (home / "config.yaml").write_text(json.dumps(configs["default"]))
    reload_plugins()
    assert "Talaria API registration requires a gateway restart" in log.read_text()
    asyncio.run(capabilities("", keys["default"], 404))

with gateway("restart-after-enable"):
    asyncio.run(profiles_work())

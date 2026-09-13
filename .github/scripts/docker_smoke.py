"""Exercise the shipped image, including private persistent config and a restart."""

import json
import subprocess
import sys
import time
import tomllib
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


name = "talaria-ci-" + uuid.uuid4().hex[:12]
volume = name + "-data"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
public_url = sys.argv[2] if len(sys.argv) > 2 else ""
port = int(sys.argv[3]) if len(sys.argv) > 3 else 8766
prefix = urlsplit(public_url).path.rstrip("/")
start = (
    "run",
    "--detach",
    "--name",
    name,
    "--health-interval",
    "1s",
    "--publish",
    f"127.0.0.1::{port}",
    "--mount",
    f"type=volume,source={volume},target=/data",
    sys.argv[1],
    "--host",
    "0.0.0.0",
    "--port",
    str(port),
    *(["--public-url", public_url] if public_url else []),
)
try:
    docker(*start)

    def ready():
        # Docker may assign a different ephemeral host port after a restart.
        ports = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]
        base = f"http://127.0.0.1:{ports[f'{port}/tcp'][0]['HostPort']}"
        for _ in range(60):
            try:
                with opener.open(base + "/health", timeout=2) as response:
                    assert json.load(response)["status"] == "ok"
                health = json.loads(docker("inspect", name))[0]["State"]["Health"]["Status"]
                if health == "healthy":
                    return base
            except OSError:
                pass
            time.sleep(0.5)
        raise AssertionError("Container did not become healthy")

    base = ready()
    for asset in (
        "/",
        "/static/boot.js",
        "/static/app.js",
        "/static/styles/chat.css",
        "/static/vendor/manifest.json",
    ):
        with opener.open(base + prefix + asset, timeout=3) as response:
            assert response.status == 200 and response.read(), asset
    installed = json.loads(
        docker(
            "exec",
            name,
            "python",
            "-c",
            """
import importlib.metadata as m, json, os
assert os.getuid() != 0
versions = {d.metadata['Name'].lower().replace('_', '-'): d.version for d in m.distributions()}
print(json.dumps(versions))
""",
        )
    )
    locked = {
        p["name"]: p["version"] for p in tomllib.loads(Path("uv.lock").read_text())["package"]
    }
    assert all(locked.get(package) == version for package, version in installed.items()), installed
    assert not {"pytest", "playwright", "ruff", "uv"} & installed.keys()
    fingerprint = """
import hashlib
from pathlib import Path
p = Path('/data/talaria/config.json')
assert p.stat().st_mode & 0o077 == 0
print(hashlib.sha256(p.read_bytes()).hexdigest())
"""
    before = docker("exec", name, "python", "-c", fingerprint)
    docker("exec", name, "talaria", "hermes-plugin", "--home", "/tmp/hermes-export")
    docker("exec", name, "test", "-s", "/tmp/hermes-export/plugins/talaria/plugin.yaml")
    docker("restart", name)
    ready()
    assert before == docker("exec", name, "python", "-c", fingerprint)
    docker("rm", "--force", name)
    docker(*start)
    ready()
    assert before == docker("exec", name, "python", "-c", fingerprint)
    print("Image passed: startup, assets, locked packages, non-root, plugin export, replacement.")
finally:
    subprocess.run(["docker", "rm", "--force", name], check=False, capture_output=True)
    subprocess.run(["docker", "volume", "rm", volume], check=False, capture_output=True)

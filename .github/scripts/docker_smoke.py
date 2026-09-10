"""Exercise the shipped image, including private persistent config and a restart."""

import json
import subprocess
import sys
import time
import tomllib
import urllib.request
import uuid
from pathlib import Path


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


name = "talaria-ci-" + uuid.uuid4().hex[:12]
volume = name + "-data"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    docker(
        "run",
        "--detach",
        "--name",
        name,
        "--publish",
        "127.0.0.1::8766",
        "--mount",
        f"type=volume,source={volume},target=/data",
        sys.argv[1],
    )
    port = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]["8766/tcp"][0][
        "HostPort"
    ]
    base = f"http://127.0.0.1:{port}"

    def ready():
        for _ in range(60):
            try:
                with opener.open(base + "/health", timeout=2) as response:
                    assert json.load(response)["status"] == "ok"
                return
            except OSError:
                time.sleep(0.5)
        raise AssertionError("Container did not become healthy")

    ready()
    for asset in ("/", "/static/app.js", "/static/styles/chat.css", "/static/vendor/manifest.json"):
        with opener.open(base + asset, timeout=3) as response:
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
print(json.dumps({d.metadata['Name'].lower().replace('_', '-'): d.version for d in m.distributions()}))
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
    print("Image passed: startup, assets, locked packages, non-root, plugin export, persistence.")
finally:
    subprocess.run(["docker", "rm", "--force", name], check=False, capture_output=True)
    subprocess.run(["docker", "volume", "rm", volume], check=False, capture_output=True)

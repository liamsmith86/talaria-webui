"""Probe the container's effective listener, including CLI port overrides."""

import json
from urllib.request import ProxyHandler, build_opener

from .config import default_path, save_data


def listener_path():
    return default_path().parent / "container-listener.json"


def record(host, port):
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    save_data(listener_path(), {"host": host, "port": port})


def main():
    try:
        with listener_path().open() as file:
            data = json.loads(file.read(4096))
        host, port = data["host"], data["port"]
        if not isinstance(host, str) or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid listener")
        authority = f"[{host}]" if ":" in host else host
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://{authority}:{port}/health", timeout=2) as response:
            if json.loads(response.read(16384)).get("status") != "ok":
                raise ValueError("Not ready")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise SystemExit("Talaria health check failed.") from exc

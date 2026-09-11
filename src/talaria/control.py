"""Small, bounded local protocol shared by the web UI, CLI, and launcher."""

import json
import re
import socket
from pathlib import Path

SOCKET = "control.sock"
JOB = "maintenance.json"
TOKEN = re.compile(r"[0-9a-f]{32}\Z")
COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")


class ControlError(Exception):
    pass


def receive(stream):
    raw = stream.readline(4097)
    if len(raw) > 4096 or not raw.endswith(b"\n"):
        raise ControlError("Invalid launcher request.")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ControlError("Invalid launcher request.") from exc
    if not isinstance(data, dict):
        raise ControlError("Invalid launcher request.")
    return data


def send(stream, data):
    stream.write(json.dumps(data).encode() + b"\n")
    stream.flush()


def request(root: Path, data):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        sock.connect(str(root / SOCKET))
        with sock.makefile("rwb") as stream:
            send(stream, data)
            result = receive(stream)
    if result.get("error"):
        raise ControlError(result["error"])
    return result


def validate(data):
    if (
        not isinstance(data.get("action"), str)
        or set(data) - {"action", "id", "expect"}
        or data.get("action")
        not in {
            "check",
            "update",
            "rollback",
        }
    ):
        raise ControlError("Choose a supported update operation.")
    if not isinstance(data.get("id"), str) or not TOKEN.fullmatch(data["id"]):
        raise ControlError("Invalid update request identifier.")
    expect = data.get("expect")
    if expect is not None and (not isinstance(expect, str) or not COMMIT.fullmatch(expect)):
        raise ControlError("Check for updates again before installing.")
    if data["action"] == "update" and expect is None:
        raise ControlError("Check for updates before installing.")
    return data

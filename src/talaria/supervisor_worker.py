"""Short-lived deployment worker; its private pipe can request a web-process restart."""

import json
import signal
import socket
import sys
from pathlib import Path

from .control import JOB, receive, send, validate
from .deployment import Deployment, now, write_json


def execute(root, operation, restart):
    operation = validate(operation)
    state = {**operation, "status": "running", "phase": "checking", "error": None}

    def report(text):
        # Only bounded progress labels cross into the browser; Git/build errors
        # can include private repository addresses and stay out of public state.
        for prefix, phase in (
            ("Building", "building"),
            ("Checking startup", "verifying"),
            ("Activating", "restarting"),
            ("Restoring", "recovering"),
        ):
            if text.startswith(prefix):
                state["phase"] = phase
        write_json(root / JOB, state)

    write_json(root / JOB, state)
    try:
        deployment = Deployment(root, report=report, restart=restart)
        if operation["action"] == "rollback":
            deployment.rollback()
        else:
            deployment.update(check=operation["action"] == "check", expect=operation.get("expect"))
        state.update(status="finishing", phase="completed", finished_at=now())
    except BaseException:
        state.update(
            status="finishing",
            phase="failed",
            finished_at=now(),
            error="Update operation failed. The previous release is retained; check server logs.",
        )
        raise
    finally:
        write_json(root / JOB, state)


def main():
    root, fd, operation = Path(sys.argv[1]), int(sys.argv[2]), json.loads(sys.argv[3])

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    with socket.socket(fileno=fd) as sock, sock.makefile("rwb") as stream:

        def restart():
            send(stream, {"restart": True})
            if receive(stream).get("ok") is not True:
                raise RuntimeError("Launcher could not restart the web process.")

        execute(root, operation, restart)


if __name__ == "__main__":
    main()

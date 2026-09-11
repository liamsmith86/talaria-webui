"""Send managed CLI updates through the same launcher used by the browser."""

import secrets
import time

from .control import JOB, SOCKET, ControlError, request
from .deployment import DeploymentError
from .installation import read_json


def wait(root, operation):
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        state = read_json(root / JOB)
        if state.get("id") != operation["id"]:
            raise DeploymentError("The update result changed; check talaria status.")
        if state.get("status") == "failed":
            raise DeploymentError(state.get("error") or "Update failed.")
        if state.get("status") == "completed":
            return
        time.sleep(0.25)
    raise DeploymentError("The launcher is still working; check talaria status.")


def dispatch(root, action, expect=None):
    operation = {"action": action, "id": secrets.token_hex(16)}
    if expect:
        operation["expect"] = expect
    try:
        accepted = request(root, operation)
    except (OSError, ValueError, ControlError) as exc:
        raise DeploymentError(
            "Launcher unavailable; check talaria status before retrying."
        ) from exc
    wait(root, accepted)


def manage(args, root):
    if args.command not in {"update", "rollback"} or not (root / SOCKET).exists():
        return False
    if args.command == "rollback":
        dispatch(root, "rollback")
    else:
        dispatch(root, "check", args.expect)
        state = read_json(root / "update.json")
        if not args.check and state.get("available"):
            dispatch(root, "update", args.expect or state["latest_commit"])
        elif state.get("available"):
            print("Update available.")
    print("Launcher operation completed.")
    return True

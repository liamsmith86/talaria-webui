"""Standard-library primitives shared by deployment and isolated plugin maintenance."""

import fcntl
from contextlib import contextmanager


class DeploymentError(Exception):
    pass


@contextmanager
def locked(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".update.lock").open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentError("Another update is running. Try again when it finishes.") from exc
        yield

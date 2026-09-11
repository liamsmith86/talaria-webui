"""First-login credentials, shared by setup and direct server startup."""

import os
import secrets
from pathlib import Path

from .auth import hash_password
from .config import load, save


def initialize_password(path: Path, settings, *, save_settings=save) -> Path | None:
    if settings.password_hash:
        save_settings(path, settings)
        return None
    password = secrets.token_urlsafe(18)
    settings.password_hash = hash_password(password)
    private_path = path.parent / "initial-password.txt"
    private_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError(
            f"{private_path} already exists. Use --set-password with this config "
            "to choose a sign-in password."
        ) from exc
    try:
        with os.fdopen(fd, "w") as file:
            file.write(password + "\n")
            file.flush()
            os.fsync(file.fileno())
        save_settings(path, settings)
    except BaseException:
        # The atomic config replacement can finish just before interruption.
        # Keep its password unless we can confirm the hash was not committed.
        try:
            committed = load(path).password_hash == settings.password_hash
        except Exception:
            committed = True
        if not committed:
            private_path.unlink(missing_ok=True)
        raise
    return private_path

"""Private, portable application settings."""

import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class Settings:
    hermes_url: str = "http://127.0.0.1:8642"
    api_key: str = ""
    password_hash: str = ""
    signing_key: str = ""
    public_url: str = ""
    host: str = "127.0.0.1"
    port: int = 8766

    @property
    def secure(self) -> bool:
        return self.public_url.startswith("https://")


def validate_url(value: str, *, origin: bool = False) -> str:
    url = urlsplit(value.strip())
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or any(ord(c) < 33 for c in value.strip())
        or (origin and url.path not in {"", "/"})
    ):
        raise ValueError("Enter an HTTP or HTTPS address without credentials or a query.")
    _ = url.port
    path = url.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return f"{url.scheme}://{url.netloc}{path}"


def default_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "talaria" / "config.json"


def save(path: Path, settings: Settings) -> None:
    save_data(path, asdict(settings))


def save_data(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".talaria-")
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(data, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load(path: Path) -> Settings:
    data = json.loads(path.read_text()) if path.exists() else {}
    data.pop("hermes_home", None)  # Retired in favor of the authenticated Hermes plugin.
    settings = Settings(**data)
    if not settings.signing_key:
        settings.signing_key = secrets.token_hex(32)
    settings.hermes_url = validate_url(settings.hermes_url)
    if settings.public_url:
        settings.public_url = validate_url(settings.public_url, origin=True)
    return settings

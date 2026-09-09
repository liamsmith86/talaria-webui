"""Application entry point."""

import argparse
import getpass
import secrets
from pathlib import Path

import uvicorn

from . import __version__
from .app import create_app
from .auth import hash_password
from .config import default_path, load, save


def main():
    parser = argparse.ArgumentParser(description="Talaria — a web client for Hermes Agent")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--config", type=Path, default=default_path(), help="Private JSON config path"
    )
    parser.add_argument("--host", help="Bind address; default: 127.0.0.1")
    parser.add_argument("--port", type=int, help="Listen port; default: 8766")
    parser.add_argument(
        "--set-password", action="store_true", help="Set the sign-in password and exit"
    )
    args = parser.parse_args()
    settings = load(args.config)
    if args.set_password:
        password = getpass.getpass("New password (at least 12 characters): ")
        if len(password) < 12 or password != getpass.getpass("Confirm password: "):
            parser.error("Passwords must match and contain at least 12 characters.")
        settings.password_hash = hash_password(password)
        settings.signing_key = secrets.token_hex(32)
        save(args.config, settings)
        print("Password saved. Restart Talaria to use it.")
        return
    if not settings.password_hash:
        password = secrets.token_urlsafe(18)
        settings.password_hash = hash_password(password)
        save(args.config, settings)
        private_path = args.config.parent / "initial-password.txt"
        private_path.touch(mode=0o600, exist_ok=False)
        private_path.write_text(password + "\n")
        print(f"Initial sign-in password saved to {private_path}", flush=True)
    uvicorn.run(
        create_app(settings, args.config),
        host=args.host or settings.host,
        port=args.port or settings.port,
        proxy_headers=False,
        access_log=False,
    )

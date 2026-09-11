"""Application entry point."""

import argparse
import getpass
import os
import secrets
import sys
from pathlib import Path

from . import __version__
from .auth import hash_password
from .config import default_path, load, save, validate_public_url


def development_app():
    """Importable factory for Uvicorn's built-in Python file reloader."""
    from .app import create_app

    path = Path(os.environ["TALARIA_DEV_CONFIG"])
    return create_app(load(path), path, development=True)


def main():
    if (
        os.name == "nt"
        and len(sys.argv) > 1
        and sys.argv[1] in {"setup", "install", "update", "rollback", "status", "uninstall"}
    ):
        raise SystemExit("Managed installation requires Linux, macOS, or Ubuntu inside WSL2.")
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        from .setup import main as setup

        return setup(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "uninstall":
        from .uninstall import main as uninstall

        return uninstall(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "hermes-plugin":
        from .plugin_install import main as install_plugin

        return install_plugin(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in {"install", "update", "rollback", "status"}:
        from .deployment import main as manage

        return manage(sys.argv[1:])
    serve()


def serve():
    parser = argparse.ArgumentParser(description="Talaria — a web client for Hermes Agent")
    parser.epilog = (
        "Managed installations: talaria install, talaria update [--check], "
        "talaria rollback, talaria status, talaria uninstall. Use COMMAND --help for details."
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--config", type=Path, help="Private JSON config path; --dev uses a separate config"
    )
    parser.add_argument("--host", help="Bind address; default: 127.0.0.1")
    parser.add_argument("--port", type=int, help="Listen port; default: 8766")
    parser.add_argument(
        "--public-url",
        type=validate_public_url,
        help="External browser URL, including any reverse-proxy subpath",
    )
    parser.add_argument(
        "--dev", action="store_true", help="Development badge, separate login, and Python reload"
    )
    parser.add_argument(
        "--set-password", action="store_true", help="Set the sign-in password and exit"
    )
    args = parser.parse_args()
    if args.config is None:
        path = default_path()
        args.config = path.parent.with_name("talaria-dev") / path.name if args.dev else path
    import uvicorn

    from .app import create_app

    settings = load(args.config)
    if args.public_url is not None:
        settings.public_url = args.public_url
    if args.dev and not args.config.exists():
        settings.port = 8767
    if args.set_password:
        password = getpass.getpass("New password (at least 4 characters): ")
        if not 4 <= len(password) <= 1024 or password != getpass.getpass("Confirm password: "):
            parser.error("Passwords must match and contain 4 to 1024 characters.")
        settings.password_hash = hash_password(password)
        settings.signing_key = secrets.token_hex(32)
        save(args.config, settings)
        print("Password saved. Restart Talaria to use it.")
        return
    if not settings.password_hash:
        from .credentials import initialize_password

        try:
            private_path = initialize_password(args.config, settings, save_settings=save)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Initial sign-in password saved to {private_path}", flush=True)
    if args.dev:
        os.environ["TALARIA_DEV_CONFIG"] = str(args.config.resolve())
    uvicorn.run(
        "talaria.cli:development_app" if args.dev else create_app(settings, args.config),
        factory=args.dev,
        reload=args.dev,
        reload_dirs=[str(Path(__file__).parent)] if args.dev else None,
        host=args.host or settings.host,
        port=args.port or settings.port,
        proxy_headers=False,
        access_log=False,
    )

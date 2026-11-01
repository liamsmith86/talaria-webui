"""Executed by Hermes's own Python; never imported by the Talaria server."""

import json
import os
import secrets
import sys
from pathlib import Path


def backup_hermes(home):
    # Run only inside the owner helper. Never replace the first backup on retry; the
    # path is not safe for a privileged parent to open. .env stays out: setup only adds
    # the API settings to it, which doesn't justify a second copy of every secret.
    source = home / "config.yaml"
    target = home / "config.yaml.before-talaria"
    if source.is_file() and not target.exists():
        with source.open("rb") as original:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(original.read())
                file.flush()
                os.fsync(file.fileno())


def add_hermes_root():
    # Started as Hermes's launcher starts it, which selects the dependencies too.
    if root := os.environ.get("TALARIA_HERMES_ROOT"):
        for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
            os.environ.pop(name, None)
        sys.path.insert(0, root)
        import hermes_bootstrap  # noqa: F401

        return
    # An editable install lists root modules from when it was installed, so an updated
    # Hermes can have newer ones. Its own CLI puts its root on the path the same way.
    import hermes_cli

    root = os.path.realpath(os.path.join(os.path.dirname(hermes_cli.__file__), os.pardir))
    if root not in sys.path:
        sys.path.insert(0, root)


def configure(home, request):
    add_hermes_root()
    from hermes_cli import __version__
    from hermes_cli.config import load_env, read_raw_config, save_env_value
    from hermes_cli.env_loader import load_hermes_dotenv

    raw = read_raw_config()  # Fail closed before the gateway's fallback loader.
    plugins = raw.get("plugins") if isinstance(raw, dict) else None
    plugins = plugins if isinstance(plugins, dict) else {}
    load_hermes_dotenv(hermes_home=home)
    from gateway.config import Platform, load_gateway_config

    config = load_gateway_config()
    api = config.platforms.get(Platform.API_SERVER)
    extra = api.extra if api else {}
    key = extra.get("key", "") or ""
    changed = False
    if request.get("enable") or request.get("plugin"):
        pending = home / "plugins/.talaria-maintenance/config-restart-required"
        pending.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        pending.touch(mode=0o600)
    if request.get("enable"):
        env_path = home / ".env"
        if env_path.exists():
            env_path.chmod(0o600)
        if not key:
            key = secrets.token_urlsafe(32)
            save_env_value("API_SERVER_KEY", key)
        save_env_value("API_SERVER_ENABLED", "true")
        env = load_env()
        if env.get("API_SERVER_ENABLED") != "true" or not key:
            raise ValueError("Hermes configuration is managed or not writable")
        changed = True
    if request.get("plugin"):
        from hermes_cli.plugins_cmd import cmd_enable

        cmd_enable("talaria-webui", allow_tool_override=False)
        changed = True
    return {
        "enabled": bool(request.get("enable") or (api and api.enabled)),
        "host": extra.get("host", "127.0.0.1"),
        "port": extra.get("port", 8642),
        "key": key,
        "multiplex": config.multiplex_profiles,
        "changed": changed,
        "version": __version__,
        "plugin_enabled": bool(
            request.get("plugin")
            or (
                isinstance(plugins.get("enabled"), list)
                and isinstance(plugins.get("disabled", []), list)
                and "talaria-webui" in (plugins.get("enabled") or [])
                and "talaria-webui" not in (plugins.get("disabled") or [])
            )
        ),
    }


def main():
    home = Path(os.environ["HERMES_HOME"])
    request = json.load(sys.stdin)
    action = request.get("action")
    if action == "backup":
        backup_hermes(home)
        result = {}
    elif action == "clear_restart":
        for name in ("restart-required", "config-restart-required"):
            (home / "plugins/.talaria-maintenance" / name).unlink(missing_ok=True)
        result = {}
    else:
        try:
            result = configure(home, request)
        except Exception as exc:
            # A type and module name say what broke; Hermes's own text can contain credentials.
            cause = ": ".join(filter(None, (type(exc).__name__, getattr(exc, "name", None))))
            print("TALARIA_SETUP_ERROR=" + json.dumps(cause))
            raise SystemExit(1) from None
    # Delimit native CLI chatter; the caller captures this pipe and never logs secrets.
    print("TALARIA_SETUP_RESULT=" + json.dumps(result))


if __name__ == "__main__":
    main()

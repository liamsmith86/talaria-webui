"""Executed by Hermes's own Python; never imported by the Talaria server."""

import json
import os
import secrets
import sys
from pathlib import Path


def main():
    from hermes_cli import __version__
    from hermes_cli.config import load_env, read_raw_config, save_env_value
    from hermes_cli.env_loader import load_hermes_dotenv

    home = Path(os.environ["HERMES_HOME"])
    raw = read_raw_config()  # Fail closed before the gateway's fallback loader.
    plugins = raw.get("plugins") if isinstance(raw, dict) else None
    plugins = plugins if isinstance(plugins, dict) else {}
    load_hermes_dotenv(hermes_home=home)
    from gateway.config import Platform, load_gateway_config

    request = json.load(sys.stdin)
    config = load_gateway_config()
    api = config.platforms.get(Platform.API_SERVER)
    extra = api.extra if api else {}
    key = extra.get("key", "") or ""
    changed = False
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

        cmd_enable("talaria", allow_tool_override=False)
        changed = True
    # Delimit native CLI chatter; the caller captures this pipe and never logs secrets.
    print(
        "TALARIA_SETUP_RESULT="
        + json.dumps(
            {
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
                        and "talaria" in (plugins.get("enabled") or [])
                        and "talaria" not in (plugins.get("disabled") or [])
                    )
                ),
            }
        )
    )


if __name__ == "__main__":
    main()

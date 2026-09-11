"""Opt-in local plugin maintenance, outside the unprivileged HTTP process."""

import os
import pwd
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .deployment import Deployment, DeploymentError, locked, write_json
from .hermes_plugin.release import fingerprint


def run_as_owner(home, source, restart=True, command=None):
    owner = pwd.getpwuid(home.stat().st_uid)
    identity = {}
    if os.geteuid() == 0:
        identity = {"user": owner.pw_uid, "group": owner.pw_gid, "extra_groups": []}
    elif os.geteuid() != owner.pw_uid:
        raise DeploymentError("Run plugin maintenance as the Hermes owner or root.")
    env = {key: os.environ[key] for key in ("PATH", "LANG", "SSL_CERT_FILE") if key in os.environ}
    env.update(
        HOME=owner.pw_dir,
        HERMES_HOME=str(home),
        PYTHONDONTWRITEBYTECODE="1",
        HERMES_ENABLE_PROJECT_PLUGINS="false",
    )
    runtime = Path(f"/run/user/{owner.pw_uid}")
    if runtime.is_dir():
        env.update(
            XDG_RUNTIME_DIR=str(runtime), DBUS_SESSION_BUS_ADDRESS=f"unix:path={runtime}/bus"
        )
    args = [
        sys.executable,
        "-m",
        "talaria.plugin_install",
        "--home",
        str(home),
        "--source",
        str(source),
    ]
    if restart:
        args.append("--restart")
    if command:
        args.extend(["--hermes-command", str(command)])
    result = subprocess.run(
        args, env=env, cwd="/", capture_output=True, text=True, timeout=1320, **identity
    )
    if result.returncode:
        # Native diagnostics may contain credentials; never put them in browser job state.
        raise DeploymentError(
            "Local plugin maintenance failed. Previous plugin files are retained for recovery; "
            "check the Hermes gateway status."
        )


def restart_and_verify(home, target, command):
    from .setup import find_hermes_python, hermes_request, local_url, restart_gateway

    python = find_hermes_python(home, command=command)
    if not python:
        raise DeploymentError("Cannot locate Hermes's Python to verify the local plugin.")
    info = hermes_request(python, home)
    if not info.get("enabled") or not info.get("key"):
        raise DeploymentError("Enable this local Hermes API before managing its plugin.")
    restart_gateway(home, command)
    expected = fingerprint(target) if (target / "release.py").is_file() else None
    url = local_url(str(info["host"]), int(info["port"])) + "/talaria/v1/capabilities"
    deadline = time.monotonic() + 30
    with httpx.Client(
        trust_env=False, timeout=2, headers={"Authorization": "Bearer " + info["key"]}
    ) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(url)
                data = response.json()
                if (
                    response.status_code == 200
                    and isinstance(data, dict)
                    and data.get("version") == 1
                    and (expected is None or data.get("revision") == expected)
                ):
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.25)
    raise DeploymentError("Hermes did not report the installed plugin after restarting.")


def sync(deployment, release):
    config = deployment.config.get("hermes_plugin")
    if not config:
        return
    sources = list(
        (deployment.root / release).glob("venv/lib/python*/site-packages/talaria/hermes_plugin")
    )
    if len(sources) != 1:
        raise DeploymentError("The selected release has no unambiguous plugin bundle.")
    deployment.report("Updating local Hermes plugin…")
    run_as_owner(Path(config["home"]), sources[0], command=config["command"])


def register(root, home, command):
    from .plugin_install import require_bundled_target
    from .setup_services import migrate_service

    require_bundled_target(home)
    executable = str(command.expanduser().resolve()) if command else shutil.which("hermes")
    if not executable or not Path(executable).is_file():
        raise DeploymentError("Pass --hermes-command with the local Hermes executable.")
    if not (home / "plugins/talaria/plugin.yaml").is_file():
        raise DeploymentError("Install and enable the local Talaria plugin before linking updates.")
    deployment = Deployment(root)
    with locked(deployment.root):
        previous = dict(deployment.config)
        if previous.get("service") and not previous.get("setup"):
            raise DeploymentError(
                "This service is externally managed; use the standalone plugin command."
            )
        deployment.config["hermes_plugin"] = {"home": str(home), "command": executable}
        write_json(deployment.root / "deployment.json", deployment.config)
        try:
            migrate_service(deployment, refresh=True)
        except BaseException:
            write_json(deployment.root / "deployment.json", previous)
            raise
    print("Local plugin linked. Talaria updates and rollbacks will restart Hermes when needed.")

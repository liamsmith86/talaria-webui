"""Optional app services. Updates are always invoked manually."""

import os
import plistlib
import pwd
import shlex
import shutil
import sys
from pathlib import Path

from .deployment import DeploymentError, run, write_file

MARKER = "# Managed by talaria setup\n"
LABEL = "app.talaria.webui"


def passwordless_sudo():
    if os.geteuid() == 0 or not shutil.which("sudo"):
        return False
    try:
        run(["sudo", "-n", "true"], timeout=5)
        return True
    except DeploymentError:
        return False


def supported_service():
    if sys.platform == "darwin" and os.geteuid() != 0 and shutil.which("launchctl"):
        try:
            run(["launchctl", "print", f"gui/{os.getuid()}"], timeout=10)
            return "launchd"
        except DeploymentError:
            return "none"
    if sys.platform.startswith("linux") and Path("/run/systemd/system").exists():
        if os.geteuid() == 0 or passwordless_sudo():
            return "systemd"
        try:
            run(["systemctl", "--user", "show-environment"], timeout=10)
            return "systemd"
        except DeploymentError:
            pass
    return "none"


def unit_quote(value):
    # systemd has its own quoting, specifier, and environment expansion rules.
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise ValueError("Service paths cannot contain control characters.")
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
    )


def service_plan(kind, root, config):
    system = os.geteuid() == 0
    if kind == "systemd":
        folder = Path("/etc/systemd/system") if system else Path.home() / ".config/systemd/user"
        unit = folder / "talaria.service"
        command = ["systemctl"] + ([] if system else ["--user"])
        account = "talaria-webui" if system else None
        content = MARKER + "[Unit]\nDescription=Talaria WebUI\nAfter=network.target\n\n[Service]\n"
        content += (
            f"ExecStart={unit_quote(root / 'manager/venv/bin/python')} "
            f"-m talaria.supervisor --directory {unit_quote(root)}\n"
        )
        content += "Restart=on-failure\nRestartSec=3\nTimeoutStopSec=20\nUMask=0077\n"
        content += "NoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\n"
        content += f"ReadWritePaths={unit_quote(root)} {unit_quote(config.parent)}\n"
        content += "Environment=PYTHONUNBUFFERED=1\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n"
        content += f"Environment={unit_quote('HOME=' + str(Path.home()))}\n"
        # The launcher retains install permissions and drops HTTP to account.
        content += "\n[Install]\nWantedBy=" + (
            "multi-user.target\n" if system else "default.target\n"
        )
        return {
            "kind": kind,
            "path": unit,
            "text": content,
            "account": account,
            "service": "talaria.service",
            "scope": "system" if system else "user",
            "start": [*command, "enable", "--now", "talaria.service"],
            "stop": [*command, "stop", "talaria.service"],
            "restart": [*command, "restart", "talaria.service"],
        }
    path = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    content = plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [
                str(root / "manager/venv/bin/python"),
                "-m",
                "talaria.supervisor",
                "--directory",
                str(root),
            ],
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 3,
            "Umask": 0o077,
            "StandardOutPath": str(config.parent / "service.log"),
            "StandardErrorPath": str(config.parent / "service.log"),
            "EnvironmentVariables": {
                "HOME": str(Path.home()),
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        }
    ).decode()
    domain = f"gui/{os.getuid()}"
    return {
        "kind": kind,
        "path": path,
        "text": content,
        "account": None,
        "service": LABEL,
        "scope": "user",
        "start": ["launchctl", "bootstrap", domain, str(path)],
        "stop": ["launchctl", "bootout", f"{domain}/{LABEL}"],
        "restart": ["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"],
    }


def preflight(plan, root, config, *, resuming=False):
    if plan["path"].exists() and plan["path"].read_text() != plan["text"]:
        raise DeploymentError(
            f"{plan['path']} already exists with different settings; preserved. "
            "Use --service none or manage this service yourself."
        )
    if plan["kind"] == "systemd" and any(
        path.is_relative_to(temporary.resolve())
        for path in (root, config, Path(sys._base_executable).resolve())
        for temporary in (Path("/tmp"), Path("/var/tmp"))
    ):
        raise DeploymentError(
            "Use persistent installation/config paths outside /tmp and /var/tmp "
            "for a systemd service (its temporary directory is private)."
        )
    if not plan["account"]:
        return
    check_service_account(plan)
    check_system_directory(root)
    # A user service must be able to traverse its interpreter and release parents.
    for target in (Path(sys._base_executable).resolve(), root):
        for parent in [target, *target.parents]:
            if parent.exists() and not parent.stat().st_mode & 0o001:
                raise DeploymentError(
                    f"The system service cannot traverse {parent}. "
                    "Use install.sh with an accessible Python, or --service none."
                )
    if not resuming and config.parent.exists() and any(config.parent.iterdir()):
        raise DeploymentError(
            "System service setup requires a new dedicated config directory. "
            "Use --service none for an existing installation."
        )


def check_system_directory(root):
    for path in (root, *root.parents):
        if path.exists() and (path.stat().st_uid != 0 or path.stat().st_mode & 0o022):
            raise DeploymentError("System installations must have root-owned, unwritable parents.")


def prepare_account(plan, config):
    if not plan["account"]:
        return False
    created = False
    try:
        account = pwd.getpwnam(plan["account"])
    except KeyError:
        shell = shutil.which("nologin") or "/bin/false"
        run(
            [
                "useradd",
                "--system",
                "--user-group",
                "--no-create-home",
                "--home-dir",
                config.parent,
                "--shell",
                shell,
                plan["account"],
            ]
        )
        account = pwd.getpwnam(plan["account"])
        created = True
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(config.parent, account.pw_uid, account.pw_gid)
    for file in config.parent.iterdir():
        if file.is_file() and not file.is_symlink():
            os.chown(file, account.pw_uid, account.pw_gid)
    return created


def install_service(plan):
    path = plan["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    write_file(path, plan["text"], 0o644)
    if plan["kind"] == "systemd":
        command = ["systemctl"] + (["--user"] if plan["scope"] == "user" else [])
        run([*command, "daemon-reload"])
        run(plan["start"], timeout=45)
    else:
        # Existing matching agents are already loaded; restart instead of bootstrapping twice.
        try:
            run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], timeout=10)
        except DeploymentError:
            run(plan["start"])
        else:
            run(plan["restart"])


def describe(plan):
    prefix = (
        ["sudo", "-n"]
        if plan["scope"] == "system" and os.environ.get("SUDO_UID", "0") != "0"
        else []
    )
    return {action: shlex.join([*prefix, *plan[action]]) for action in ("start", "stop", "restart")}


def check_service_account(plan):
    # Do not repurpose an existing interactive or privileged account.
    try:
        account = pwd.getpwnam(plan["account"])
    except KeyError:
        if not shutil.which("useradd"):
            raise DeploymentError(
                "System service setup needs useradd; use --service none."
            ) from None
    else:
        if account.pw_uid == 0 or account.pw_shell.rsplit("/", 1)[-1] not in {"nologin", "false"}:
            raise DeploymentError("The talaria-webui account is unsuitable for a service.")


def migrate_service(deployment):
    """Upgrade only our unmodified app unit; restore it if the new launcher fails."""
    import hashlib

    from .deployment import check_health, write_json

    metadata = deployment.config
    owned = metadata.get("setup")
    if not owned or metadata.get("supervised"):
        return
    if (owned["scope"] == "system") != (os.geteuid() == 0):
        raise DeploymentError(
            "Run setup as the service installation owner to upgrade its launcher."
        )
    plan = service_plan(owned["kind"], deployment.root, Path(metadata["config"]))
    path = plan["path"]
    if str(path) != owned["service_file"] or path.is_symlink():
        raise DeploymentError("Service ownership changed; its configuration was preserved.")
    previous = path.read_text()
    if hashlib.sha256(previous.encode()).hexdigest() != owned["service_sha256"]:
        raise DeploymentError("Service configuration changed; it was preserved.")
    upgraded = {
        **metadata,
        "supervised": True,
        "setup": {
            **owned,
            "service_sha256": hashlib.sha256(plan["text"].encode()).hexdigest(),
        },
    }
    write_json(deployment.root / "deployment.json", upgraded)
    try:
        if plan["kind"] == "launchd":
            run(plan["stop"])
        install_service(plan)
        if plan["kind"] == "systemd":
            run(plan["restart"], timeout=45)
        check_health(metadata["health_url"], deployment.status()["current"]["commit"])
    except BaseException:
        write_file(path, previous, 0o644)
        write_json(deployment.root / "deployment.json", metadata)
        if plan["kind"] == "systemd":
            run([*plan["restart"][:-2], "daemon-reload"])
        else:
            run(plan["stop"])
            run(plan["start"])
        run(plan["restart"], timeout=45)
        raise
    deployment.config = upgraded

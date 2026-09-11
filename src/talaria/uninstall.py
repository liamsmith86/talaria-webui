"""Remove a managed installation; Hermes and shared prerequisites are never removed."""

import argparse
import fcntl
import grp
import hashlib
import json
import os
import pwd
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from .deployment import Deployment, DeploymentError, default_directory, locked, run
from .setup import terminal
from .setup_services import service_plan


def service_to_remove(root, metadata, config):
    owned = metadata.get("setup", {})
    kind = owned.get("kind") or metadata.get("manager")
    if not kind:
        if metadata.get("service"):
            raise DeploymentError("Remove the externally managed service before uninstalling.")
        return None
    if kind not in {"systemd", "launchd"}:
        raise DeploymentError("Unknown service manager; installation preserved.")
    plan = service_plan(kind, root, config)
    if owned and (owned.get("service_file") != str(plan["path"])
                  or owned.get("scope") != plan["scope"]):
        raise DeploymentError("Service ownership does not match this account; preserved.")
    if metadata.get("service") not in {None, plan["service"]}:
        raise DeploymentError("Service name does not match setup; preserved.")
    path = plan["path"]
    expected = owned.get("service_sha256") or hashlib.sha256(plan["text"].encode()).hexdigest()
    if path.is_symlink() or (
        path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != expected
    ):
        raise DeploymentError(f"Service file changed; restore or remove {path} before retrying.")
    return plan


def stop_service(plan):
    if plan["kind"] == "systemd":
        command = ["systemctl"] + (["--user"] if plan["scope"] == "user" else [])
        fragment = run([*command, "show", plan["service"], "--property=FragmentPath", "--value"])
        if fragment and Path(fragment) != plan["path"]:
            raise DeploymentError("Another service uses this name; preserved.")
        if fragment:
            run([*command, "disable", "--now", plan["service"]], timeout=45)
        plan["path"].unlink(missing_ok=True)
        run([*command, "daemon-reload"])
    else:
        domain = f"gui/{os.getuid()}/{plan['service']}"
        loaded = subprocess.run(["launchctl", "print", domain], capture_output=True)
        if loaded.returncode == 0:
            run(plan["stop"], timeout=45)
        plan["path"].unlink(missing_ok=True)


def uninstall(root, *, yes=False, prompts=None):
    root = root.expanduser().resolve()
    if not root.exists():
        print(f"Already removed: {root}")
        return
    metadata = Deployment(root).config
    system = metadata.get("scope") == "system" or metadata.get("setup", {}).get("scope") == "system"
    if system and os.geteuid() != 0:
        raise DeploymentError("Use sudo to uninstall this system installation.")
    if not system and root.stat().st_uid != os.geteuid():
        raise DeploymentError("Run uninstall as the installation owner.")
    if (root / ".git").exists() or root in {Path("/"), Path.home()}:
        raise DeploymentError("This is not an isolated installation directory; preserved.")
    if not (root / "repository.git/HEAD").is_file() or not (root / "releases").is_dir():
        raise DeploymentError("Managed runtime files are missing; installation preserved.")
    if any((root / name).is_symlink() for name in ("releases", "repository.git", "bin")):
        raise DeploymentError("Managed runtime directories cannot be symbolic links.")
    config = Path(metadata["config"])
    if not config.is_absolute():
        raise DeploymentError("Invalid configuration path; installation preserved.")
    files = [config, *(config.parent / name for name in (
        "initial-password.txt", "profiles.json", "service.log",
    ))]
    plan = service_to_remove(root, metadata, config)
    account = None
    if metadata.get("setup", {}).get("account_created"):
        with suppress(KeyError):
            account = pwd.getpwnam("talaria-webui")
        if account and (account.pw_uid == 0 or Path(account.pw_dir) != config.parent or Path(
            account.pw_shell
        ).name not in {"nologin", "false"}):
            raise DeploymentError("Service account has changed; installation preserved.")
    if not plan:
        running = False
        with suppress(OSError, ValueError):
            opener = build_opener(ProxyHandler({}))
            with opener.open(metadata["health_url"].rstrip("/") + "/health", timeout=1) as response:
                running = json.loads(response.read(4096)).get("status") == "ok"
        if running:
            raise DeploymentError("Stop the manually started Talaria process before uninstalling.")
    print(f"Remove installation: {root}")
    if plan:
        print(f"Remove service: {plan['path']}")
    for path in files:
        if path.exists() or path.is_symlink():
            if path.is_dir() and not path.is_symlink():
                raise DeploymentError(f"Expected a configuration file at {path}; preserved.")
            print(f"Remove configuration file: {path}")
    print("Hermes, its sessions/plugin, and shared Python/Git/uv installations are kept.")
    if not yes and (prompts is None or prompts.terminal is None):
        raise DeploymentError("Use --yes for non-interactive removal.")
    if not yes and not prompts.yes("Uninstall Talaria?", False):
        print("Cancelled; nothing removed.")
        return
    # Serialize with both setup and update before stopping the service or deleting files.
    with locked(root), (root / ".setup.lock").open("a") as setup_lock:
        try:
            fcntl.flock(setup_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentError("Setup is running; retry when it has finished.") from exc
        if Deployment(root).config != metadata:
            raise DeploymentError("Installation changed during confirmation; retry uninstall.")
        service_to_remove(root, metadata, config)
        if plan:
            stop_service(plan)
        for path in files:
            path.unlink(missing_ok=True)
        with suppress(OSError):
            config.parent.rmdir()
        # Only remove an account we created, once its private directory is gone.
        if account and not config.parent.exists():
            run(["userdel", "talaria-webui"])
            with suppress(KeyError):
                grp.getgrnam("talaria-webui")
                run(["groupdel", "talaria-webui"])
        for name in ("releases", "repository.git"):
            shutil.rmtree(root / name)
        for name in ("current", "previous", "manager", "deployment.json", "update.json",
                     "activation.json", ".setup-pending.json", ".setup.lock", ".update.lock"):
            (root / name).unlink(missing_ok=True)
        (root / "bin/talaria").unlink(missing_ok=True)
        with suppress(OSError):
            (root / "bin").rmdir()
        with suppress(OSError):
            root.rmdir()
    print("Talaria uninstalled.")
    if root.exists():
        print(f"Unrelated files preserved in {root}")


def main(argv):
    parser = argparse.ArgumentParser(description="Remove a managed Talaria installation.")
    parser.add_argument("--directory", type=Path, default=default_directory())
    parser.add_argument("--yes", action="store_true", help="Confirm removal without prompting")
    args = parser.parse_args(argv)
    try:
        with terminal(not args.yes, "--yes") as prompts:
            uninstall(args.directory, yes=args.yes, prompts=prompts)
    except (DeploymentError, OSError, KeyError, ValueError) as exc:
        parser.exit(1, f"Talaria: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Talaria: uninstall interrupted; rerun to finish.\n")

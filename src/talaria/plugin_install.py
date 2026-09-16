"""Install a bundled plugin on this host, with restart and recoverable replacement."""

import argparse
import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

from .hermes_plugin.release import fingerprint
from .maintenance import DeploymentError, locked


def hermes_managed(home):
    metadata = home / "plugins/.install-metadata.json"
    try:
        with metadata.open() as file:
            data = json.loads(file.read(1024 * 1024))
        if not isinstance(data, dict):
            raise ValueError("Invalid plugin provenance")
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        raise DeploymentError(
            "Cannot verify Hermes plugin ownership; nothing was changed."
        ) from exc
    target = home / "plugins/talaria"
    return (
        "talaria" in data
        or (target / ".git").exists()
        or (target / ".hermes-catalog.json").exists()
    )


def require_bundled_target(home):
    if hermes_managed(home):
        raise DeploymentError(
            "Hermes manages this plugin's source or pin. Update it through Hermes Desktop "
            "or its native plugin installer; Talaria will not overwrite it."
        )


def restore(target, backup):
    if not backup.is_dir():
        return
    if target.exists():
        shutil.rmtree(target)
    backup.replace(target)


def replace_plugin(source, target, backup, *, preserve_backup=False):
    # The backup survives interruption between the two directory renames.
    with tempfile.TemporaryDirectory(prefix=".talaria-stage-", dir=backup.parent) as temporary:
        staged = Path(temporary) / "talaria"
        shutil.copytree(target, staged, symlinks=True) if target.exists() else staged.mkdir()
        staged.chmod(0o700)
        for path in staged.iterdir():
            if path.suffix == ".py" or path.name in {"plugin.yaml", "__pycache__"}:
                shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
        for path in source.iterdir():
            if path.is_file() and path.suffix in {".py", ".yaml"}:
                (staged / path.name).unlink(missing_ok=True)
                shutil.copyfile(path, staged / path.name)
                (staged / path.name).chmod(0o600)
        if fingerprint(staged) != fingerprint(source):
            raise DeploymentError("Plugin staging verification failed.")
        previous = (
            Path(temporary) / "previous-export" if preserve_backup and backup.exists() else backup
        )
        if previous.exists():
            shutil.rmtree(previous)
        if target.exists():
            target.replace(previous)
        staged.replace(target)


def install_files(source, target, backup, pending):
    was_pending = pending.exists()
    pending.touch(mode=0o600)
    try:
        # Several exports can precede a restart. Keep the last verified version,
        # rather than replacing its backup with another unverified export.
        replace_plugin(source, target, backup, preserve_backup=was_pending)
    except BaseException:
        if not target.exists():
            restore(target, backup)
        if not was_pending:
            pending.unlink(missing_ok=True)
        raise


def install(home, source, restart=None):
    if not all((source / name).is_file() for name in ("plugin.yaml", "__init__.py")):
        raise DeploymentError("The selected plugin bundle is incomplete; nothing was changed.")
    plugins = home / "plugins"
    plugins.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Hermes scans two plugin-directory levels, including hidden directories.
    # Keep backup/staging manifests below that discovery depth.
    maintenance = plugins / ".talaria-maintenance"
    backup = maintenance / "backups" / "talaria"
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = plugins / "talaria"
    pending = maintenance / "restart-required"
    if target.is_symlink() or backup.is_symlink():
        raise DeploymentError("The Talaria plugin is symlinked; update it through its owner.")
    with locked(maintenance):
        # Ownership can change after an export, even when the code is identical.
        # Check before restoring backups or acting on an old pending restart.
        require_bundled_target(home)
        if not target.exists() and backup.exists():
            restore(target, backup)
        changed = not target.exists() or fingerprint(target) != fingerprint(source)
        if changed:
            install_files(source, target, backup, pending)
        if restart and pending.exists():
            try:
                restart(target)
            except BaseException:
                if backup.exists():
                    restore(target, backup)
                    restart(target)
                    pending.unlink(missing_ok=True)
                raise
            pending.unlink(missing_ok=True)
        return changed


def main(argv, *, owner=None):
    from .deployment import default_directory
    from .hermes_owner import select_owner

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument(
        "--restart", action="store_true", help="Restart and verify the local gateway if needed"
    )
    parser.add_argument("--hermes-command", type=Path, help="Local Hermes CLI executable")
    parser.add_argument(
        "--manage-updates",
        action="store_true",
        help="Link this local plugin to Talaria updates (does not restart Hermes)",
    )
    parser.add_argument("--directory", type=Path, default=default_directory())
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(__file__).parent / "hermes_plugin",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    from .plugin_updates import register, restart_and_verify, run_as_owner

    home = args.home.expanduser().absolute()
    owner = owner or select_owner(home)
    if args.manage_updates:
        if args.restart:
            parser.error("Link updates first; --manage-updates does not restart Hermes.")
        register(args.directory, home, args.hermes_command)
        return
    if os.geteuid() == 0 and owner.pw_uid != 0:
        run_as_owner(home, args.source, args.restart, args.hermes_command, owner=owner)
        return
    restart = (
        (lambda target: restart_and_verify(home, target, args.hermes_command, owner=owner))
        if args.restart
        else None
    )
    changed = install(home, args.source, restart)
    print(f"Plugin {'installed' if changed else 'unchanged'}: {home / 'plugins/talaria'}")
    if not args.restart:
        print("Enable with `hermes plugins enable talaria`, then restart the gateway.")
        print("For multiplexed gateways, also install and enable it in the primary profile.")


def owned_worker():
    """Use the same replacement transaction while the parent verifies restarts."""
    home, source, fd, restart = sys.argv[1:]
    with socket.socket(fileno=int(fd)) as sock, sock.makefile("rwb") as stream:
        sock.settimeout(1350)

        def verify(_target):
            stream.write(b"restart\n")
            stream.flush()
            if stream.readline(8) != b"ok\n":
                raise DeploymentError("The parent could not verify the Hermes restart.")

        install(Path(home), Path(source), verify if restart == "1" else None)


if __name__ == "__main__":
    main(sys.argv[1:])

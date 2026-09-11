"""Install a bundled plugin on this host, with restart and recoverable replacement."""

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from .deployment import DeploymentError, default_directory, locked
from .hermes_plugin.release import fingerprint


def require_bundled_target(home):
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
    if "talaria" in data or (home / "plugins/talaria/.git").exists():
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


def replace_plugin(source, target, backup):
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
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            target.replace(backup)
        staged.replace(target)


def install_files(source, target, backup, pending):
    was_pending = pending.exists()
    pending.touch(mode=0o600)
    try:
        replace_plugin(source, target, backup)
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
        if not target.exists() and backup.exists():
            restore(target, backup)
        changed = not target.exists() or fingerprint(target) != fingerprint(source)
        if changed:
            require_bundled_target(home)
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


def main(argv):
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

    home = args.home.expanduser().resolve()
    if args.manage_updates:
        if args.restart:
            parser.error("Link updates first; --manage-updates does not restart Hermes.")
        register(args.directory, home, args.hermes_command)
        return
    if home.exists() and os.geteuid() == 0 and home.stat().st_uid != 0:
        run_as_owner(home, args.source, args.restart, args.hermes_command)
        return
    restart = (
        (lambda target: restart_and_verify(home, target, args.hermes_command))
        if args.restart
        else None
    )
    changed = install(home, args.source, restart)
    print(f"Plugin {'installed' if changed else 'unchanged'}: {home / 'plugins/talaria'}")
    if not args.restart:
        print("Enable with `hermes plugins enable talaria`, then restart the gateway.")
        print("For multiplexed gateways, also install and enable it in the primary profile.")


if __name__ == "__main__":
    main(sys.argv[1:])

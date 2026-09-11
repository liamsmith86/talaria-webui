"""Real isolated owner workers; no live Hermes homes or gateways are used."""

import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from talaria import plugin_updates, plugin_worker
from talaria.deployment import DeploymentError, locked
from talaria.hermes_plugin.release import fingerprint

from .test_plugin_updates import bundle


def owner_cases():
    cases = [pytest.param(pwd.getpwuid(os.geteuid()), id="same-owner")]
    if os.geteuid() == 0:
        try:
            account = pwd.getpwnam("nobody")
        except KeyError:
            pass
        else:
            cases.append(pytest.param(account, id="different-owner"))
    return cases


def accessible_python(owner):
    candidates = [Path(sys.executable).resolve(), shutil.which("python3", path=os.defpath)]
    identity = (
        {"user": owner.pw_uid, "group": owner.pw_gid, "extra_groups": []}
        if os.geteuid() == 0
        else {}
    )
    for candidate in candidates:
        if not candidate:
            continue
        try:
            result = subprocess.run(
                [str(candidate), "-I", "-S", "-c", "import pathlib, socket, fcntl"],
                cwd="/",
                capture_output=True,
                timeout=5,
                **identity,
            )
        except OSError:
            continue
        if result.returncode == 0:
            return candidate
    pytest.fail("No standard Python accessible to the isolated test account")


@pytest.fixture(params=owner_cases())
def isolated_owner(request, monkeypatch):
    owner = request.param
    interpreter = accessible_python(owner)
    with tempfile.TemporaryDirectory(prefix="talaria-owner-test-") as temporary:
        directory = Path(temporary)
        directory.chmod(0o755)
        home = directory / "hermes"
        home.mkdir(mode=0o700)
        python = home / "hermes-agent/venv/bin/python"
        python.parent.mkdir(parents=True)
        # -I -S requires only a standard Python, not a Hermes/Talaria environment.
        python.symlink_to(interpreter)
        for path in [home, *home.rglob("*")]:
            if not path.is_symlink() and os.geteuid() == 0:
                os.chown(path, owner.pw_uid, owner.pw_gid)
        bootstrap = directory / "private-bootstrap"
        bootstrap.mkdir(mode=0o700)
        executable = bootstrap / "python"
        executable.symlink_to(sys.executable)
        source = bundle(bootstrap / "source", "old")
        monkeypatch.setattr(sys, "executable", str(executable))
        yield home, source, owner, bootstrap


def test_private_bootstrap_installs_and_verifies_as_owner(isolated_owner, monkeypatch):
    home, source, owner, bootstrap = isolated_owner
    verified = []

    def verify(candidate, target, command):
        assert candidate == home and target == home / "plugins/talaria"
        with (
            pytest.raises(DeploymentError, match="Another update"),
            locked(home / "plugins/.talaria-maintenance"),
        ):
            pass
        verified.append(fingerprint(target))

    monkeypatch.setattr(plugin_updates, "restart_and_verify", verify)
    plugin_updates.run_as_owner(home, source)
    assert verified == [fingerprint(source)]
    assert bootstrap.stat().st_mode & 0o777 == 0o700
    for path in [home / "plugins", *(home / "plugins").rglob("*")]:
        assert path.stat().st_uid == owner.pw_uid
        assert path.stat().st_mode & 0o077 == 0


def test_owner_worker_keeps_lock_through_failed_restart_and_recovery(isolated_owner, monkeypatch):
    home, source, _, _ = isolated_owner
    monkeypatch.setattr(plugin_updates, "restart_and_verify", lambda *args: None)
    plugin_updates.run_as_owner(home, source)
    old = fingerprint(source)
    bundle(source, "new")
    calls = []

    def verify(_home, target, _command):
        with (
            pytest.raises(DeploymentError, match="Another update"),
            locked(home / "plugins/.talaria-maintenance"),
        ):
            pass
        calls.append(fingerprint(target))
        if calls[-1] != old:
            raise ValueError("PRIVATE native diagnostic")

    monkeypatch.setattr(plugin_updates, "restart_and_verify", verify)
    with pytest.raises(DeploymentError, match="Local plugin maintenance failed") as error:
        plugin_updates.run_as_owner(home, source)
    assert "PRIVATE" not in str(error.value)
    assert calls == [fingerprint(source), old]
    assert fingerprint(home / "plugins/talaria") == old
    assert not (home / "plugins/.talaria-maintenance/restart-required").exists()


def test_early_worker_exit_removes_staging(isolated_owner, monkeypatch):
    home, source, _, _ = isolated_owner
    staged = []
    stage = plugin_worker.stage

    def record(directory, *args):
        staged.append(directory)
        return stage(directory, *args)

    monkeypatch.setattr(plugin_worker, "stage", record)
    monkeypatch.setattr(plugin_worker, "BOOT", "raise SystemExit(3)")
    with pytest.raises(DeploymentError, match="Local plugin maintenance failed"):
        plugin_updates.run_as_owner(home, source, restart=False)
    assert staged and not any(path.exists() for path in staged)
    assert not (home / "plugins").exists()


def test_parent_interruption_releases_lock_and_keeps_backup(isolated_owner, monkeypatch):
    home, source, _, _ = isolated_owner
    monkeypatch.setattr(plugin_updates, "restart_and_verify", lambda *args: None)
    plugin_updates.run_as_owner(home, source)
    old = fingerprint(source)
    bundle(source, "new")

    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(plugin_updates, "restart_and_verify", interrupted)
    with pytest.raises(KeyboardInterrupt):
        plugin_updates.run_as_owner(home, source)
    maintenance = home / "plugins/.talaria-maintenance"
    with locked(maintenance):
        backup = maintenance / "backups/talaria"
        # EOF can let the worker restore before SIGTERM arrives; either outcome
        # retains the old version and leaves the restart marker for recovery.
        assert fingerprint(backup if backup.exists() else home / "plugins/talaria") == old
        assert (maintenance / "restart-required").exists()


def test_worker_timeout_removes_staging_and_stops_child(isolated_owner, monkeypatch):
    home, source, _, _ = isolated_owner
    processes = []
    supervise = plugin_worker.supervise

    def briefly(process, channel, verify):
        processes.append(process)
        supervise(process, channel, verify, timeout=0.05)

    monkeypatch.setattr(plugin_worker, "BOOT", "import time; time.sleep(60)")
    monkeypatch.setattr(plugin_worker, "supervise", briefly)
    with pytest.raises(DeploymentError, match="timed out"):
        plugin_updates.run_as_owner(home, source, restart=False)
    assert processes and all(process.poll() is not None for process in processes)
    assert not any(Path(process.args[5]).exists() for process in processes)
    assert not (home / "plugins").exists()


def test_worker_cannot_request_an_unapproved_restart(isolated_owner, monkeypatch):
    home, source, _, _ = isolated_owner
    monkeypatch.setattr(
        plugin_worker,
        "BOOT",
        "import socket, sys, time; "
        "socket.socket(fileno=int(sys.argv[4])).sendall(b'restart\\n'); time.sleep(60)",
    )
    monkeypatch.setattr(
        plugin_updates,
        "restart_and_verify",
        lambda *args: pytest.fail("Unapproved restart"),
    )
    with pytest.raises(DeploymentError, match="Invalid plugin maintenance request"):
        plugin_updates.run_as_owner(home, source, restart=False)

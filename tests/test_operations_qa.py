"""Real failure boundaries for release staging and optional plugin installation."""

import fcntl
import importlib.util
import json
import os
import py_compile
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from talaria import cli, plugin_install
from talaria import deployment as operations
from talaria.auth import verify_password
from talaria.config import load
from talaria.deployment import DeploymentError, stop

from .test_deployment import A
from .test_deployment import deployment as deployment


def test_build_children_are_stopped_after_their_parent_exits(tmp_path):
    marker = tmp_path / "child.lock"
    child = (
        "import fcntl, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "file = open(sys.argv[1], 'w'); fcntl.flock(file, fcntl.LOCK_EX); "
        "print('ready', flush=True); time.sleep(60)"
    )
    parent = (
        "import subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], "
        "stdout=subprocess.PIPE); assert child.stdout.readline() == b'ready\\n'"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", parent, child, str(marker)], start_new_session=True
    )
    try:
        assert process.wait(timeout=5) == 0
        with marker.open() as file:
            with pytest.raises(BlockingIOError):
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stop(process)
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        pytest.fail("An orphaned build child survived cleanup")
                    time.sleep(0.01)
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def test_staged_startup_deadline_covers_partial_stdout(deployment, monkeypatch):
    release = deployment.root / "releases" / A
    binary = release / "venv/bin/python"
    binary.parent.mkdir(parents=True)
    binary.symlink_to(sys.executable)
    monkeypatch.setattr(
        operations, "PROBE", "import time; print('8', end='', flush=True); time.sleep(60)"
    )
    started = time.monotonic()
    with pytest.raises(DeploymentError, match="did not start"):
        deployment.probe(release, startup_timeout=0.2)
    assert time.monotonic() - started < 3


def test_corrupt_selected_release_is_never_removed_for_rebuilding(deployment, monkeypatch):
    release = deployment.root / "releases" / A
    (release / "release.json").write_text("broken metadata")
    executable = release / "still-running.py"
    executable.write_text("This active installation must be preserved")
    monkeypatch.setattr(deployment, "fetch", lambda: A)
    with pytest.raises(DeploymentError, match="selected release has invalid metadata"):
        deployment.update()
    assert executable.is_file()
    assert os.readlink(deployment.root / "current") == f"releases/{A}"


def test_non_object_health_response_fails_as_a_controlled_deployment_error(monkeypatch):
    from io import BytesIO

    class Opener:
        def open(self, *args, **kwargs):
            return BytesIO(json.dumps(["unexpected health"]).encode())

    monkeypatch.setattr(operations, "build_opener", lambda *args: Opener())
    with pytest.raises(DeploymentError, match="health check failed"):
        operations.check_health("http://fixture.test", None, timeout=0)


def test_plugin_export_replaces_stale_same_second_bytecode(tmp_path, monkeypatch):
    package = tmp_path / "package"
    source = package / "hermes_plugin"
    source.mkdir(parents=True)
    (source / "identity.py").write_text('VALUE = "new"\n')
    monkeypatch.setattr(plugin_install, "__file__", str(package / "plugin_install.py"))
    target = tmp_path / "hermes/plugins/talaria"
    target.mkdir(parents=True)
    module = target / "identity.py"
    module.write_text('VALUE = "old"\n')
    timestamp = int(time.time())
    os.utime(module, (timestamp, timestamp))
    py_compile.compile(str(module), doraise=True)
    plugin_install.main(["--home", str(tmp_path / "hermes")])
    # Reproduce coarse timestamp filesystems / rapid consecutive exports.
    os.utime(module, (timestamp, timestamp))
    spec = importlib.util.spec_from_file_location("fixture_identity", module)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    assert loaded.VALUE == "new"
    assert module.stat().st_mode & 0o077 == 0


def test_interrupted_plugin_copy_keeps_the_previous_module(tmp_path, monkeypatch):
    package = tmp_path / "package"
    source = package / "hermes_plugin"
    source.mkdir(parents=True)
    (source / "identity.py").write_text('VALUE = "new"\n')
    monkeypatch.setattr(plugin_install, "__file__", str(package / "plugin_install.py"))
    target = tmp_path / "hermes/plugins/talaria"
    target.mkdir(parents=True)
    module = target / "identity.py"
    module.write_text('VALUE = "old"\n')

    def failed_copy(source, destination):
        destination.write(b"incomplete")
        raise OSError("Interrupted copy")

    monkeypatch.setattr(plugin_install.shutil, "copyfileobj", failed_copy)
    with pytest.raises(OSError, match="Interrupted"):
        plugin_install.main(["--home", str(tmp_path / "hermes")])
    assert module.read_text() == 'VALUE = "old"\n'
    assert list(target.iterdir()) == [module]


def test_initial_password_is_readable_before_its_hash_is_saved(tmp_path, monkeypatch):
    config_path = tmp_path / "private/config.json"
    monkeypatch.setattr(sys, "argv", ["talaria", "--config", str(config_path)])
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    monkeypatch.setattr("talaria.app.create_app", lambda *args, **kwargs: None)
    cli.main()
    private_path = config_path.parent / "initial-password.txt"
    assert verify_password(private_path.read_text().strip(), load(config_path).password_hash)
    assert private_path.stat().st_mode & 0o077 == 0
    assert config_path.stat().st_mode & 0o077 == 0


def test_stale_initial_password_does_not_commit_an_unrecoverable_hash(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"port": 9876}')
    original = config_path.read_bytes()
    private_path = tmp_path / "initial-password.txt"
    private_path.write_text("previous password\n")
    monkeypatch.setattr(sys, "argv", ["talaria", "--config", str(config_path)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert config_path.read_bytes() == original
    assert private_path.read_text() == "previous password\n"


def test_failed_first_config_save_allows_a_clean_startup_retry(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(sys, "argv", ["talaria", "--config", str(config_path)])

    def failed_save(*args):
        raise OSError("Fixture disk failure")

    monkeypatch.setattr(cli, "save", failed_save)
    with pytest.raises(OSError, match="Fixture disk failure"):
        cli.main()
    assert not config_path.exists()
    assert not (tmp_path / "initial-password.txt").exists()


def test_interruption_after_config_commit_keeps_the_initial_password(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(sys, "argv", ["talaria", "--config", str(config_path)])
    original_save = cli.save

    def interrupted_save(*args):
        original_save(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "save", interrupted_save)
    with pytest.raises(KeyboardInterrupt):
        cli.main()
    password = (tmp_path / "initial-password.txt").read_text().strip()
    assert verify_password(password, load(config_path).password_hash)


def test_managed_launcher_exports_the_plugin_without_server_arguments(deployment):
    binary = deployment.root / f"releases/{A}/venv/bin/talaria"
    binary.parent.mkdir(parents=True)
    binary.write_text(f"#!{sys.executable}\nfrom talaria.cli import main\nmain()\n")
    binary.chmod(0o700)
    config_path = Path(deployment.config["config"])
    original_config = config_path.read_bytes()
    home = deployment.root.parent / "hermes profile"
    deployment.launcher()
    operations.run([deployment.root / "bin/talaria", "hermes-plugin", "--home", home])
    assert (home / "plugins/talaria/bridge.py").is_file()
    assert (home / "plugins/talaria/bridge.py").stat().st_mode & 0o077 == 0
    assert config_path.read_bytes() == original_config


def test_noop_update_refreshes_an_older_launcher_without_restarting(deployment, monkeypatch):
    binary = deployment.root / "bin/talaria"
    binary.parent.mkdir()
    binary.write_text("previous release launcher")
    monkeypatch.setattr(deployment, "fetch", lambda: A)
    monkeypatch.setattr(deployment, "restart", lambda: pytest.fail("Unnecessary restart"))
    deployment.update(check=True)
    assert binary.read_text() == "previous release launcher"
    deployment.update(expect=A)
    assert binary.read_text().startswith("#!/bin/sh\n")

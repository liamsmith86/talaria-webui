"""Deployment failures must leave a usable release and private config intact."""

import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from talaria.config import Settings, save
from talaria.deployment import (
    Deployment,
    DeploymentError,
    check_health,
    locked,
    readable_runtime,
    run,
    select,
    selected,
    write_json,
)
from talaria.installation import read_json

A, B, C = "a" * 40, "b" * 40, "c" * 40


def test_runtime_permissions_preserve_external_interpreter(tmp_path):
    runtime = tmp_path / "venv"
    runtime.mkdir(mode=0o700)
    module = runtime / "module.py"
    module.write_text("pass")
    module.chmod(0o600)
    outside = tmp_path / "python"
    outside.write_text("interpreter")
    outside.chmod(0o700)
    (runtime / "python").symlink_to(outside)
    readable_runtime(runtime)
    assert runtime.stat().st_mode & 0o777 == 0o755
    assert module.stat().st_mode & 0o777 == 0o644
    assert outside.stat().st_mode & 0o777 == 0o700


def release(root, commit):
    path = root / "releases" / commit
    path.mkdir(parents=True)
    write_json(path / "release.json", {"commit": commit, "version": "0.2.0", "health_commit": True})
    return f"releases/{commit}"


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / "installation"
    root.mkdir()
    config = tmp_path / "private/config.json"
    save(config, Settings(password_hash="kept-verbatim", signing_key="kept-signing-key"))
    write_json(
        root / "deployment.json",
        {
            "schema": 1,
            "repository": str(tmp_path / "remote"),
            "branch": "main",
            "config": str(config),
            "service": None,
            "scope": "user",
            "health_url": "http://127.0.0.1:1",
        },
    )
    select(root, "current", release(root, A))
    return Deployment(root, report=lambda text: None)


@pytest.fixture
def private_umask():
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def test_only_one_updater_can_change_an_installation(deployment):
    with locked(deployment.root), pytest.raises(DeploymentError, match="Another update"):
        deployment.update()
    with locked(deployment.root):
        pass


def test_failed_build_and_network_leave_current_untouched(deployment, monkeypatch):
    config = Path(deployment.config["config"])
    original = config.read_bytes()
    monkeypatch.setattr(deployment, "fetch", lambda: B)

    def fail(*args):
        raise DeploymentError("Simulated build failure")

    monkeypatch.setattr(deployment, "stage", fail)
    with pytest.raises(DeploymentError, match="build failure"):
        deployment.update()
    assert selected(deployment.root, "current") == f"releases/{A}"
    assert config.read_bytes() == original
    assert config.stat().st_mode & 0o077 == 0
    assert deployment.state["error"] and deployment.state["available"]
    monkeypatch.setattr(deployment, "fetch", fail)
    with pytest.raises(DeploymentError):
        deployment.update(check=True)
    assert selected(deployment.root, "current") == f"releases/{A}"


def test_check_and_expected_commit_never_activate(deployment, monkeypatch):
    monkeypatch.setattr(deployment, "fetch", lambda: B)
    monkeypatch.setattr(
        deployment, "stage", lambda _: pytest.fail("Check must not stage a release")
    )
    deployment.update(check=True)
    assert deployment.state["available"] and deployment.state["latest_commit"] == B
    with pytest.raises(DeploymentError, match="branch has moved"):
        deployment.update(expect=C)
    assert selected(deployment.root, "current") == f"releases/{A}"


@pytest.mark.parametrize("failure", ["restart", "health"])
def test_failed_activation_restores_and_checks_previous(deployment, monkeypatch, failure):
    root = deployment.root
    select(root, "previous", release(root, C))
    target = release(root, B)
    restarts, verified = [], []

    def restart():
        current = selected(root, "current")
        restarts.append(current)
        if failure == "restart" and current == target:
            raise DeploymentError("Simulated service restart failure")

    def verify(value):
        verified.append(value)
        if value == target:
            raise DeploymentError("Simulated failed health check")

    monkeypatch.setattr(deployment, "restart", restart)
    monkeypatch.setattr(deployment, "verify_running", verify)
    with pytest.raises(DeploymentError, match="previous release was restored"):
        deployment.activate(target)
    assert restarts == [target, f"releases/{A}"]
    assert verified[-1] == f"releases/{A}"
    assert selected(root, "current") == f"releases/{A}"
    assert selected(root, "previous") == f"releases/{C}"
    assert not (root / "activation.json").exists()


def test_failed_recovery_retains_journal_for_retry(deployment, monkeypatch):
    target = release(deployment.root, B)

    def fail():
        raise DeploymentError("Service manager unavailable")

    monkeypatch.setattr(deployment, "restart", fail)
    with pytest.raises(DeploymentError, match="recovery needs attention"):
        deployment.activate(target)
    assert deployment.status()["recovery_pending"]
    assert selected(deployment.root, "current") == f"releases/{A}"
    monkeypatch.setattr(deployment, "restart", lambda: None)
    deployment.recover()
    assert not deployment.status()["recovery_pending"]


def test_interrupted_activation_and_rollback_preserve_both_releases(deployment):
    root = deployment.root
    target = release(root, B)
    write_json(root / "activation.json", {"current": f"releases/{A}", "previous": None})
    select(root, "current", target)
    with pytest.raises(DeploymentError, match="activation is incomplete"):
        deployment.update(check=True)
    assert selected(root, "current") == target
    deployment.recover()
    assert selected(root, "current") == f"releases/{A}"
    deployment.activate(target)
    deployment.rollback()
    assert selected(root, "current") == f"releases/{A}"
    assert selected(root, "previous") == target
    deployment.rollback()
    assert selected(root, "current") == target


def test_release_retention_is_bounded_without_deleting_unknown_files(deployment):
    root = deployment.root
    target = release(root, B)
    release(root, C)
    unknown = root / "releases/operator-note"
    unknown.mkdir()
    deployment.activate(target)
    deployment.cleanup()
    assert (root / f"releases/{A}").is_dir()
    assert (root / f"releases/{B}").is_dir()
    assert not (root / f"releases/{C}").exists()
    assert unknown.is_dir()


def test_external_release_and_corrupt_journal_fail_closed(deployment):
    with pytest.raises(DeploymentError, match="Invalid release path"):
        select(deployment.root, "current", "../../other")
    (deployment.root / "activation.json").write_text('{"bad":')
    with pytest.raises(DeploymentError, match="journal is unreadable"):
        deployment.recover()
    assert selected(deployment.root, "current") == f"releases/{A}"


def test_health_requires_the_selected_build(live_app):
    check_health(live_app[0], None, timeout=0.1, assets=True)
    with pytest.raises(DeploymentError, match="health check failed"):
        check_health(live_app[0], B, timeout=0.1)


def test_command_timeout_is_bounded():
    with pytest.raises(DeploymentError, match="timed out"):
        run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)


def test_managed_launcher_never_injects_production_config_into_dev(deployment):
    executable = deployment.root / f"releases/{A}/venv/bin/talaria"
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    executable.chmod(0o755)
    deployment.launcher()
    launcher = deployment.root / "bin/talaria"
    assert run([launcher, "--port", "9876", "--dev"]).splitlines() == ["--port", "9876", "--dev"]
    assert run([launcher, "--port", "9876"]).splitlines() == [
        "--config",
        deployment.config["config"],
        "--port",
        "9876",
    ]


@pytest.mark.skipif(not shutil.which("uv") or not shutil.which("git"), reason="Needs Git and uv")
@pytest.mark.parametrize("sudo", [False, True])
def test_real_git_wheel_install_update_failure_and_rollback(
    deployment, tmp_path, monkeypatch, sudo, private_umask,
):
    """Exercise the actual build/install/probe pipeline without touching a host service."""
    # A fresh install puts uv here but does not edit the user's shell startup files.
    home = tmp_path / "home"
    user_bin = home / ".local/bin"
    user_bin.mkdir(parents=True)
    (user_bin / "uv").symlink_to(shutil.which("uv"))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("SUDO_UID", raising=False)
    if sudo:
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "root")
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setenv("SUDO_UID", "12345")
        monkeypatch.setattr(
            "talaria.deployment.pwd.getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home))
        )
    which = shutil.which
    monkeypatch.setattr(
        shutil, "which",
        lambda command, **kwargs: None if command == "uv" and not kwargs
        else which(command, **kwargs),
    )
    project = Path(__file__).resolve().parents[1]
    remote = Path(deployment.config["repository"])
    remote.mkdir()
    for name in ("pyproject.toml", "uv.lock", ".gitignore"):
        shutil.copyfile(project / name, remote / name)
    shutil.copytree(project / "src", remote / "src", ignore=shutil.ignore_patterns("__pycache__"))
    run(["git", "init", "-b", "main", remote])
    run(["git", "-C", remote, "config", "user.email", "test@example.invalid"])
    run(["git", "-C", remote, "config", "user.name", "Deployment test"])

    def commit():
        run(["git", "-C", remote, "add", "."])
        run(["git", "-C", remote, "commit", "-m", "Test release"])
        return run(["git", "-C", remote, "rev-parse", "HEAD"])

    first = commit()
    config = Path(deployment.config["config"])
    original = config.read_bytes()
    deployment.update(expect=first)
    root = deployment.root
    runtime = root / "current"
    for path in [runtime, runtime / "venv", runtime / "venv/bin/talaria"]:
        assert path.stat().st_mode & 0o005 == 0o005
    marker = tmp_path / "private-file"
    marker.write_text("private")
    assert marker.stat().st_mode & 0o077 == 0
    python = root / "current/venv/bin/python"
    info = json.loads(
        run(
            [
                python,
                "-c",
                "import json; from talaria.installation import public_info; "
                "print(json.dumps(public_info(False)))",
            ]
        )
    )
    assert info["commit"] == first and info["managed"]
    assert run([python, "-c", "import talaria; print(talaria.__file__)"]).startswith(str(root))
    packages = run(
        [
            python,
            "-c",
            "import importlib.metadata as m; "
            "print([d.metadata['Name'] for d in m.distributions()])",
        ]
    )
    assert "pytest" not in packages and "playwright" not in packages
    plugin_home = tmp_path / "exported-hermes"
    run([root / "current/venv/bin/talaria", "hermes-plugin", "--home", plugin_home])
    exported = plugin_home / "plugins/talaria"
    bundled = project / "src/talaria/hermes_plugin"
    for file in bundled.iterdir():
        if file.suffix in {".py", ".yaml"}:
            assert (exported / file.name).read_bytes() == file.read_bytes()
    module = remote / "src/talaria/app.py"
    good = module.read_text()
    module.write_text("raise RuntimeError('Broken staged release')\n")
    broken = commit()
    with pytest.raises(DeploymentError, match="staged application"):
        deployment.update()
    assert selected(root, "current") == f"releases/{first}"
    assert not (root / "releases" / broken).exists()
    assert config.read_bytes() == original
    module.write_text(good + "\n# Another tested build.\n")
    second = commit()
    deployment.update(expect=second)
    assert selected(root, "previous") == f"releases/{first}"
    assert len(list((root / "releases").iterdir())) == 2
    deployment.rollback()
    assert read_json(root / "current/release.json")["commit"] == first
    assert config.read_bytes() == original
    assert "recovery_pending" in run([root / "current/venv/bin/talaria", "status", "--json"])
    # Management survives a rollback to an application without management commands.
    legacy = root / f"releases/{first}/venv/bin/talaria"
    legacy.write_text("#!/bin/sh\nexit 99\n")
    assert "recovery_pending" in run([root / "bin/talaria", "status", "--json"])
    assert selected(root, "manager") == f"releases/{second}"

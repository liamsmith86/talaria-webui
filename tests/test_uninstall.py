"""Uninstall must remove owned state without touching Hermes or shared tools."""

import hashlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from talaria import uninstall as remove
from talaria.config import Settings, save
from talaria.deployment import DeploymentError, locked, write_json
from talaria.setup_services import prepare_account, service_plan


@pytest.fixture
def installed(tmp_path):
    root = tmp_path / "installation"
    (root / "repository.git").mkdir(parents=True)
    (root / "repository.git/HEAD").write_text("ref: refs/heads/main\n")
    (root / "releases/build").mkdir(parents=True)
    (root / "releases/build/app").write_text("packaged runtime")
    (root / "bin").mkdir()
    (root / "bin/talaria").write_text("launcher")
    (root / "current").symlink_to("releases/build")
    config = tmp_path / "private/config.json"
    save(config, Settings(password_hash="private", api_key="test-key"))
    for name in ("initial-password.txt", "profiles.json", "service.log"):
        (config.parent / name).write_text("private")
    metadata = {
        "schema": 1, "config": str(config), "scope": "user", "service": None,
        "health_url": "http://127.0.0.1:1",
    }
    write_json(root / "deployment.json", metadata)
    return root, config, metadata


def test_uninstall_preserves_unrelated_files_and_shared_tools(installed, tmp_path):
    root, config, _ = installed
    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    (hermes / "sessions").write_text("keep")
    uv = tmp_path / "uv"
    uv.write_text("keep")
    (root / "my-notes").write_text("keep")
    (config.parent / "unrelated").write_text("keep")
    remove.uninstall(root, yes=True)
    assert not (root / "releases").exists()
    assert not (root / "deployment.json").exists()
    assert (root / "my-notes").read_text() == "keep"
    assert (config.parent / "unrelated").read_text() == "keep"
    assert not config.exists()
    assert not (config.parent / "profiles.json").exists()
    assert (hermes / "sessions").read_text() == uv.read_text() == "keep"


def test_purge_removes_empty_directories_and_is_repeatable(installed):
    root, config, _ = installed
    remove.uninstall(root, yes=True)
    assert not root.exists() and not config.parent.exists()
    remove.uninstall(root, yes=True)


def test_confirmation_and_update_lock_prevent_removal(installed):
    root, config, _ = installed
    with pytest.raises(DeploymentError, match="--yes"):
        remove.uninstall(root)
    remove.uninstall(root, prompts=SimpleNamespace(terminal=True, yes=lambda *a: False))
    assert config.exists() and (root / "releases/build/app").exists()
    with locked(root), pytest.raises(DeploymentError, match="Another update"):
        remove.uninstall(root, yes=True)
    assert config.exists()


def test_refuses_development_checkout_and_symlinked_runtime(installed, tmp_path):
    root, config, _ = installed
    (root / ".git").mkdir()
    with pytest.raises(DeploymentError, match="isolated"):
        remove.uninstall(root, yes=True)
    (root / ".git").rmdir()
    (root / "bin").rename(tmp_path / "shared-bin")
    (root / "bin").symlink_to(tmp_path / "shared-bin", target_is_directory=True)
    with pytest.raises(DeploymentError, match="symbolic"):
        remove.uninstall(root, yes=True)
    assert (tmp_path / "shared-bin/talaria").exists() and config.exists()


def test_changed_launch_agent_is_preserved_then_owned_agent_is_removed(installed, monkeypatch):
    root, config, metadata = installed
    monkeypatch.setattr(Path, "home", lambda: root.parent / "home")
    plan = service_plan("launchd", root, config)
    plan["path"].parent.mkdir(parents=True)
    metadata["setup"] = {
        "kind": "launchd", "scope": "user", "service_file": str(plan["path"]),
        "service_sha256": hashlib.sha256(plan["text"].encode()).hexdigest(),
    }
    write_json(root / "deployment.json", metadata)
    plan["path"].write_text("another service")
    calls = []
    monkeypatch.setattr(remove, "run", lambda command, **kw: calls.append(command) or "")
    monkeypatch.setattr(
        remove.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0)
    )
    with pytest.raises(DeploymentError, match="Service file changed"):
        remove.uninstall(root, yes=True)
    assert not calls and config.exists()
    plan["path"].write_text(plan["text"])
    remove.uninstall(root, yes=True)
    assert calls == [plan["stop"]]
    assert not plan["path"].exists() and not root.exists()


@pytest.mark.parametrize("existed", [False, True])
def test_service_account_ownership_is_recorded_only_when_created(tmp_path, monkeypatch, existed):
    import talaria.setup_services as services

    config = tmp_path / "config.json"
    account = SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid())
    created = []

    def lookup(name):
        if not existed and not created:
            raise KeyError(name)
        return account

    monkeypatch.setattr(services.pwd, "getpwnam", lookup)
    monkeypatch.setattr(services, "run", lambda command: created.append(command))
    assert prepare_account({"account": "talaria-webui"}, config) is not existed
    assert bool(created) is not existed

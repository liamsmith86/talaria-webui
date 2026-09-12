"""Maintenance boundaries use synthetic paths and intercepted child processes."""

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from talaria import plugin_install, plugin_worker
from talaria import setup as wizard
from talaria.config import Settings
from talaria.deployment import DeploymentError


def account(uid=1234):
    return SimpleNamespace(pw_uid=uid, pw_gid=uid, pw_name="fixture", pw_dir="/fixture-home")


def test_stage_finishes_privileged_access_before_owner_handoff(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "plugin.yaml").write_text("name: fixture\n")
    directory = tmp_path / "stage"
    directory.mkdir(mode=0o700)
    handed_off = False
    original = Path.chmod

    def chmod(path, mode, **kwargs):
        assert not handed_off, "privileged traversal after exposing the staging directory"
        return original(path, mode, **kwargs)

    def chown(path, uid, gid):
        nonlocal handed_off
        assert not handed_off, "privileged ownership change after exposing staging"
        assert uid == gid == 1234
        handed_off = path == directory

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", chown)
    monkeypatch.setattr(Path, "chmod", chmod)
    bundle = plugin_worker.stage(directory, source, account())
    assert handed_off and (bundle / "plugin.yaml").read_text() == "name: fixture\n"


def test_setup_does_not_open_owner_backup_files_in_parent(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("fixture: unchanged\n")
    original = Path.open

    def open_file(path, *args, **kwargs):
        assert path != config, "the privileged setup parent opened an owner-controlled file"
        return original(path, *args, **kwargs)

    calls = []
    info = {"enabled": True, "key": "", "plugin_enabled": True}
    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(wizard, "hermes_request", lambda *a, **kw: calls.append(kw) or info)
    args = wizard.parser().parse_args([])
    wizard.apply_local_hermes(args, Settings(), home, None, info, True, False)
    assert [call.get("action") for call in calls] == ["backup", None]
    assert calls[0]["owner"] is calls[1]["owner"]


def test_export_does_not_revisit_owner_files_as_root(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    target = home / "plugins/talaria"
    target.mkdir(parents=True)
    (target / "plugin.yaml").write_text("name: fixture\n")
    calls = []
    monkeypatch.setattr(plugin_install, "main", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda *a: pytest.fail("Privileged post-export traversal"))
    wizard.export_owned_plugin(home)
    assert len(calls) == 1


@pytest.fixture
def root_paths(monkeypatch):
    from talaria import hermes_owner

    paths = {
        "/": (0, stat.S_IFDIR | 0o755),
        "/fixture": (0, stat.S_IFDIR | 0o755),
        "/fixture/home": (0, stat.S_IFDIR | 0o700),
        "/fixture/python": (0, stat.S_IFREG | 0o755),
    }
    links = {}
    real_stat, real_lstat, real_readlink = Path.stat, Path.lstat, Path.readlink

    def info(path, *, follow_symlinks=True):
        key = str(path)
        if key != "/" and not key.startswith("/fixture"):
            return real_stat(path, follow_symlinks=follow_symlinks)
        if key not in paths:
            raise FileNotFoundError(key)
        if follow_symlinks and path in links:
            target = links[path]
            return info(target if target.is_absolute() else path.parent / target)
        uid, mode = paths[key]
        return SimpleNamespace(st_uid=uid, st_mode=mode)

    def lstat(path):
        if str(path) == "/" or str(path).startswith("/fixture"):
            return info(path, follow_symlinks=False)
        return real_lstat(path)

    monkeypatch.setattr(Path, "stat", info)
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(Path, "readlink", lambda p: links[p] if p in links else real_readlink(p))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(hermes_owner.pwd, "getpwuid", lambda uid: account(uid))
    return paths, links


@pytest.mark.parametrize("uid,mode", [(1234, 0o755), (0, 0o775), (0, 0o777)])
def test_root_execution_rejects_mutable_parent(root_paths, uid, mode):
    from talaria.hermes_owner import validate_execution

    paths, _ = root_paths
    paths["/fixture"] = uid, stat.S_IFDIR | mode
    with pytest.raises(DeploymentError, match=r"root|writable"):
        validate_execution(Path("/fixture/home"), Path("/fixture/python"), account(0))


def test_root_paths_allow_protected_symlinks_and_existing_sticky_children(root_paths):
    from talaria.hermes_owner import require_owner_path, validate_execution

    paths, links = root_paths
    paths["/fixture"] = 0, stat.S_IFDIR | 0o1777
    paths["/fixture/link"] = 0, stat.S_IFLNK | 0o777
    links[Path("/fixture/link")] = Path("python")
    assert validate_execution(Path("/fixture/home"), "/fixture/link", account(0)) == Path(
        "/fixture/link"
    )
    with pytest.raises(DeploymentError, match="parents"):
        require_owner_path("/fixture/missing", 0, missing=True)
    require_owner_path("/fixture/home/new/profile", 0, missing=True)


def test_root_paths_recheck_symlink_targets_and_bound_loops(root_paths):
    from talaria.hermes_owner import require_owner_path

    paths, links = root_paths
    paths["/fixture/link"] = 0, stat.S_IFLNK | 0o777
    links[Path("/fixture/link")] = Path("python")
    paths["/fixture/python"] = 1234, stat.S_IFREG | 0o755
    with pytest.raises(DeploymentError, match="root"):
        require_owner_path("/fixture/link", 0)
    links[Path("/fixture/link")] = Path("link")
    with pytest.raises(DeploymentError, match="Too many symlinks"):
        require_owner_path("/fixture/link", 0)


def test_root_maintenance_rejects_another_accounts_home_alias(root_paths):
    from talaria.hermes_owner import select_owner

    paths, links = root_paths
    paths["/fixture/alice"] = 1001, stat.S_IFDIR | 0o755
    paths["/fixture/alice/hermes"] = 1001, stat.S_IFLNK | 0o777
    paths["/fixture/bob"] = 1002, stat.S_IFDIR | 0o755
    paths["/fixture/bob/hermes"] = 1002, stat.S_IFDIR | 0o755
    links[Path("/fixture/alice/hermes")] = Path("/fixture/bob/hermes")
    with pytest.raises(DeploymentError, match="owner or root"):
        select_owner(Path("/fixture/alice/hermes"))
    assert select_owner(Path("/fixture/bob/hermes")).pw_uid == 1002


@pytest.mark.parametrize("operation", ["inspect", "plugin"])
def test_root_maintenance_rejects_another_accounts_interpreter(root_paths, monkeypatch, operation):
    from talaria import plugin_updates

    paths, _ = root_paths
    paths["/fixture/home"] = 1002, stat.S_IFDIR | 0o700
    paths["/fixture/python"] = 1001, stat.S_IFREG | 0o755
    monkeypatch.setattr(os, "getgrouplist", lambda *a: [1002])
    calls = []
    monkeypatch.setattr(
        wizard.subprocess,
        "run",
        lambda *a, **kw: (
            calls.append(kw) or SimpleNamespace(returncode=0, stdout="TALARIA_SETUP_RESULT={}")
        ),
    )
    monkeypatch.setattr(wizard, "find_hermes_python", lambda *a, **kw: Path("/fixture/python"))
    monkeypatch.setattr(plugin_worker, "run", lambda *args: calls.append(args[4]))

    def invoke():
        if operation == "inspect":
            wizard.hermes_request(
                Path("/fixture/python"), Path("/fixture/home"), owner=account(1002)
            )
        else:
            plugin_updates.run_as_owner(
                Path("/fixture/home"), None, restart=False, owner=account(1002)
            )

    with pytest.raises(DeploymentError, match="owner or root"):
        invoke()
    assert not calls
    paths["/fixture/python"] = 1002, stat.S_IFREG | 0o755
    invoke()
    assert calls[0]["user"] == 1002


@pytest.mark.parametrize("operation", ["inspect", "restart"])
def test_helper_uses_pinned_owner_and_exact_absolute_executable(root_paths, monkeypatch, operation):
    paths, _ = root_paths
    paths["/fixture/hermes-cli"] = 0, stat.S_IFREG | 0o755
    home = Path("/fixture/home")
    owner = account()
    monkeypatch.chdir("/")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "getgrouplist", lambda *a: [1234])
    monkeypatch.setattr(wizard, "select_owner", lambda *a: pytest.fail("Reselected owner"))
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="TALARIA_SETUP_RESULT={}")

    monkeypatch.setattr(wizard.subprocess, "run", run)
    if operation == "inspect":
        wizard.hermes_request(Path("fixture/python"), home, owner=owner, action="backup")
        assert json.loads(calls[0][1]["input"]) == {"action": "backup"}
    else:
        wizard.restart_gateway(home, Path("fixture/hermes-cli"), owner=owner)
        assert "--system" not in calls[0][0]
    command, options = calls[0]
    assert Path(command[0]).parent == Path("/fixture")
    assert options["user"] == options["group"] == 1234
    assert options["env"]["HOME"] == "/fixture-home"


@pytest.mark.parametrize("skip", [False, True])
def test_remote_setup_never_inspects_local_runtime(tmp_path, monkeypatch, skip):
    home = tmp_path / "hermes"
    home.mkdir()
    args = wizard.parser().parse_args(["--hermes-url", "http://192.0.2.10:8642"])
    args.skip_hermes = skip
    monkeypatch.setattr(wizard, "discover_hermes_home", lambda *a: home)
    for name in ("select_owner", "find_hermes_python", "hermes_request"):
        monkeypatch.setattr(
            wizard, name, lambda *a: pytest.fail("Inspected an unused local runtime")
        )
    assert wizard.inspect_hermes(args, wizard.Prompts(None)) == (home, None, None, None)

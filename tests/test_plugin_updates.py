"""Local plugin replacement and update recovery; never restart a real gateway."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from talaria import plugin_install, plugin_updates, setup, setup_services
from talaria.deployment import DeploymentError, selected, write_json
from talaria.hermes_plugin.release import fingerprint
from talaria.installation import read_json

from .test_deployment import A, B, release
from .test_deployment import deployment as deployment_fixture

deployment = deployment_fixture


def bundle(path, value):
    path.mkdir(parents=True, exist_ok=True)
    (path / "plugin.yaml").write_text('name: talaria\nversion: "1"\n')
    (path / "__init__.py").write_text(f"VALUE = {value!r}\n")
    return path


@pytest.mark.parametrize("provenance", ["pin", "git", "invalid"])
def test_bundled_updates_preserve_native_ownership(tmp_path, provenance):
    home = tmp_path / "hermes"
    target = bundle(home / "plugins/talaria", "native")
    source = bundle(tmp_path / "source", "bundled")
    before = fingerprint(target)
    if provenance == "git":
        (target / ".git").mkdir()
    else:
        (home / "plugins/.install-metadata.json").write_text(
            '{"talaria":{"pinned":true,"revision":"reviewed"}}' if provenance == "pin" else "["
        )
    with pytest.raises(DeploymentError):
        plugin_install.install(home, source, lambda _: pytest.fail("Restarted native plugin"))
    assert fingerprint(target) == before
    assert not (home / "plugins/.talaria-maintenance/restart-required").exists()


def test_managed_cli_syncs_plugin_even_when_app_is_current(tmp_path, monkeypatch):
    from talaria import control, supervisor_cli

    (tmp_path / control.SOCKET).touch()
    write_json(tmp_path / "update.json", {"available": False, "latest_commit": A})
    calls = []
    monkeypatch.setattr(supervisor_cli, "dispatch", lambda *args: calls.append(args))
    args = SimpleNamespace(command="update", check=False, expect=None)
    assert supervisor_cli.manage(args, tmp_path)
    assert calls == [(tmp_path, "check", None), (tmp_path, "update", A)]
    calls.clear()
    args.check = True
    assert supervisor_cli.manage(args, tmp_path)
    assert calls == [(tmp_path, "check", None)]


def test_replacement_removes_retired_code_and_restarts_once(tmp_path):
    home = tmp_path / "hermes"
    source = bundle(tmp_path / "source", "old")
    (source / "retired.py").write_text("old = True\n")
    calls = []

    def restart(target):
        calls.append(fingerprint(target))

    assert plugin_install.install(home, source, restart)
    target = home / "plugins/talaria"
    (target / "config.yaml").write_text("user: retained\n")
    assert not plugin_install.install(home, source, restart)
    assert len(calls) == 1
    (source / "retired.py").unlink()
    bundle(source, "new")
    assert plugin_install.install(home, source, restart)
    assert not (target / "retired.py").exists()
    assert (target / "config.yaml").read_text() == "user: retained\n"
    assert len(calls) == 2
    assert (home / "plugins/.talaria-maintenance/backups/talaria/retired.py").exists()


def test_restart_failure_restores_and_verifies_previous_plugin(tmp_path):
    home = tmp_path / "hermes"
    source = bundle(tmp_path / "source", "old")
    plugin_install.install(home, source, lambda _: None)
    old = fingerprint(source)
    bundle(source, "new")
    calls = []

    def restart(target):
        calls.append(fingerprint(target))
        if calls[-1] != old:
            raise DeploymentError("New gateway failed its health check")

    with pytest.raises(DeploymentError, match="health check"):
        plugin_install.install(home, source, restart)
    assert calls == [fingerprint(source), old]
    assert fingerprint(home / "plugins/talaria") == old
    assert not (home / "plugins/.talaria-maintenance/restart-required").exists()


def test_export_without_restart_remembers_pending_restart(tmp_path):
    source = bundle(tmp_path / "source", "new")
    home = tmp_path / "hermes"
    plugin_install.install(home, source)
    calls = []
    assert not plugin_install.install(home, source, lambda _: calls.append(True))
    assert calls == [True]


def test_interrupted_directory_swap_recovers_original(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    source = bundle(tmp_path / "source", "old")
    plugin_install.install(home, source, lambda _: None)
    target = home / "plugins/talaria"
    backup = home / "plugins/.talaria-maintenance/backups/talaria"
    (home / "plugins/.talaria-maintenance/restart-required").touch()
    target.rename(backup)
    calls = []
    plugin_install.install(home, source, lambda _: calls.append(True))
    assert fingerprint(target) == fingerprint(source)
    assert not backup.exists()
    assert calls == [True]


def test_staging_failure_leaves_original_untouched(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    source = bundle(tmp_path / "source", "old")
    plugin_install.install(home, source, lambda _: None)
    target = home / "plugins/talaria"
    before = fingerprint(target)
    bundle(source, "new")
    monkeypatch.setattr(
        plugin_install.shutil,
        "copyfile",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError, match="disk full"):
        plugin_install.install(home, source)
    assert fingerprint(target) == before


def test_linked_plugin_follows_activation_recovery_and_rollback(deployment, tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    old = bundle(
        deployment.root / f"releases/{A}/venv/lib/python3.12/site-packages/talaria/hermes_plugin",
        "old",
    )
    target = release(deployment.root, B)
    new = bundle(
        deployment.root / target / "venv/lib/python3.14/site-packages/talaria/hermes_plugin", "new"
    )
    deployment.config["hermes_plugin"] = {"home": str(home), "command": "/fake/hermes"}
    calls = []

    def worker(home, source, **kwargs):
        plugin_install.install(home, source, lambda path: calls.append(fingerprint(path)))

    monkeypatch.setattr(plugin_updates, "run_as_owner", worker)
    deployment.sync_plugin(f"releases/{A}")
    deployment.activate(target)
    assert calls == [fingerprint(old), fingerprint(new)]
    deployment.rollback()
    assert calls[-1] == fingerprint(old)
    monkeypatch.setattr(
        deployment,
        "verify_running",
        lambda value: (
            (_ for _ in ()).throw(DeploymentError("web failed")) if value == target else None
        ),
    )
    with pytest.raises(DeploymentError, match="previous release was restored"):
        deployment.activate(target)
    assert selected(deployment.root, "current") == f"releases/{A}"
    assert fingerprint(home / "plugins/talaria") == fingerprint(old)
    assert calls[-2:] == [fingerprint(new), fingerprint(old)]


def test_linking_updates_never_restarts_hermes(deployment, tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    bundle(home / "plugins/talaria", "old")
    command = tmp_path / "hermes-cli"
    command.touch()
    refreshed = []
    monkeypatch.setattr(setup_services, "migrate_service", lambda obj, **kw: refreshed.append(kw))
    monkeypatch.setattr(setup, "restart_gateway", lambda *a: pytest.fail("Must not restart Hermes"))
    plugin_updates.register(deployment.root, home, command)
    assert read_json(deployment.root / "deployment.json")["hermes_plugin"]["home"] == str(home)
    assert refreshed == [{"refresh": True}]


def test_linking_failure_restores_deployment_config(deployment, tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    bundle(home / "plugins/talaria", "old")
    command = tmp_path / "hermes-cli"
    command.touch()
    previous = read_json(deployment.root / "deployment.json")
    monkeypatch.setattr(
        setup_services,
        "migrate_service",
        lambda *a, **k: (_ for _ in ()).throw(DeploymentError("custom unit preserved")),
    )
    with pytest.raises(DeploymentError, match="custom unit"):
        plugin_updates.register(deployment.root, home, command)
    assert read_json(deployment.root / "deployment.json") == previous


def test_systemd_only_adds_explicit_local_home(deployment, tmp_path, monkeypatch):
    monkeypatch.setattr(setup_services.os, "geteuid", lambda: 0)
    home = tmp_path / "hermes profile"
    config = Path(deployment.config["config"])
    assert str(home) not in setup_services.service_plan("systemd", deployment.root, config)["text"]
    deployment.config["hermes_plugin"] = {"home": str(home)}
    write_json(deployment.root / "deployment.json", deployment.config)
    text = setup_services.service_plan("systemd", deployment.root, config)["text"]
    assert f'ReadWritePaths="{home}"' in text
    assert "ProtectSystem=strict" in text


def test_worker_uses_owner_environment_without_inheriting_secrets(tmp_path, monkeypatch):
    from talaria import hermes_owner, plugin_worker

    home = tmp_path / "hermes"
    home.mkdir()
    interpreter = home / "python"
    interpreter.touch()
    monkeypatch.setenv("SECRET_FOR_ROOT", "private")
    monkeypatch.setattr(plugin_updates.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        hermes_owner.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_uid=uid, pw_gid=uid, pw_dir=str(home)),
    )
    calls = []
    monkeypatch.setattr(setup, "find_hermes_python", lambda *a, **kw: interpreter)
    monkeypatch.setattr(plugin_worker, "run", lambda *args: calls.append(args))
    plugin_updates.run_as_owner(home, tmp_path / "source", command="/local/hermes")
    python, target, _, _, identity, env, _ = calls[0]
    assert python == interpreter and target == home
    assert identity["user"] == home.stat().st_uid and identity["extra_groups"] == []
    assert env["HOME"] == str(home)
    assert "SECRET_FOR_ROOT" not in env


def test_restart_waits_for_the_new_loaded_revision(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    source = bundle(tmp_path / "source", "new")
    (source / "release.py").write_text("# revision support\n")
    monkeypatch.setattr(setup, "find_hermes_python", lambda _, command=None: Path("/fake/python"))
    monkeypatch.setattr(
        setup,
        "hermes_request",
        lambda *a, **kw: {"enabled": True, "key": "test-key", "host": "127.0.0.1", "port": 8642},
    )
    restarts, reads = [], []
    monkeypatch.setattr(setup, "restart_gateway", lambda *a, **kw: restarts.append(a))
    monkeypatch.setattr(plugin_updates.time, "sleep", lambda _: None)

    def response(request):
        assert request.headers["authorization"] == "Bearer test-key"
        reads.append(request)
        return httpx.Response(
            200,
            json={"version": 1, "revision": "stale" if len(reads) == 1 else fingerprint(source)},
        )

    original = httpx.Client
    monkeypatch.setattr(
        plugin_updates.httpx,
        "Client",
        lambda **kw: original(transport=httpx.MockTransport(response), **kw),
    )
    plugin_updates.restart_and_verify(home, source, "/fake/hermes")
    assert len(restarts) == 1 and len(reads) == 2


@pytest.mark.parametrize("exports", [1, 3])
def test_failed_deferred_restart_restores_previous_export(tmp_path, exports):
    home = tmp_path / "hermes"
    source = bundle(tmp_path / "source", "old")
    plugin_install.install(home, source, lambda _: None)
    old = fingerprint(source)
    for number in range(exports):
        bundle(source, f"new-{number}")
        plugin_install.install(home, source)  # Export now; restart later.
    calls = []

    def restart(target):
        calls.append(fingerprint(target))
        if calls[-1] != old:
            raise DeploymentError("New plugin did not load")

    with pytest.raises(DeploymentError, match="did not load"):
        plugin_install.install(home, source, restart)
    assert calls == [fingerprint(source), old]
    assert fingerprint(home / "plugins/talaria") == old


def test_interrupted_repeated_export_recovers_verified_plugin(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    source = bundle(tmp_path / "source", "verified")
    plugin_install.install(home, source, lambda _: None)
    verified = fingerprint(source)
    bundle(source, "unverified")
    plugin_install.install(home, source)
    bundle(source, "next")
    target = home / "plugins/talaria"
    replace = Path.replace

    def interrupted(path, destination):
        if path.parent.name.startswith(".talaria-stage-") and destination == target:
            raise OSError("Interrupted staged rename")
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", interrupted)
    with pytest.raises(OSError, match="Interrupted staged rename"):
        plugin_install.install(home, source)
    assert fingerprint(target) == verified
    assert (home / "plugins/.talaria-maintenance/restart-required").exists()


def test_update_checks_do_not_touch_linked_plugin(deployment, monkeypatch):
    deployment.config["hermes_plugin"] = {"home": "/unused", "command": "/unused"}
    monkeypatch.setattr(deployment, "fetch", lambda: A)
    monkeypatch.setattr(
        plugin_updates, "run_as_owner", lambda *a, **k: pytest.fail("Check must not change Hermes")
    )
    deployment.update(check=True)


def test_custom_hermes_command_identifies_its_own_python(tmp_path):
    runtime = tmp_path / "custom/venv"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "pyvenv.cfg").touch()
    python = runtime / "bin/python"
    python.touch()
    command = runtime / "bin/hermes"
    command.write_text("#!/custom/python\n")
    # Existing standard Hermes installations must not override an explicit CLI.
    assert setup.find_hermes_python(tmp_path / "profile", command=command) == python

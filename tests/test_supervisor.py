"""Exercise the installed launcher, real wheels, HTTP privileges, and CLI rollback."""

import os
import pwd
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from talaria import control
from talaria.auth import hash_password
from talaria.config import Settings, save
from talaria.deployment import Deployment, DeploymentError, check_health, run, selected, write_json
from talaria.installation import read_json
from talaria.uninstall import stopped_launcher

from .test_app import signed_in


def eventually(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.1)
    raise AssertionError("Launcher did not reach the expected state")


@pytest.fixture
def installed_launcher():
    # Root-only tests use an isolated traversable path, never a host installation/account.
    with tempfile.TemporaryDirectory(
        prefix="talaria-test-", dir="/opt" if os.geteuid() == 0 else None
    ) as temporary:
        base = Path(temporary)
        base.chmod(0o755)
        remote, root, config = base / "remote", base / "app", base / "private/config.json"
        remote.mkdir()
        root.mkdir()
        project = Path(__file__).resolve().parents[1]
        for name in ("pyproject.toml", "uv.lock", ".gitignore"):
            shutil.copyfile(project / name, remote / name)
        shutil.copytree(
            project / "src", remote / "src", ignore=shutil.ignore_patterns("__pycache__")
        )
        run(["git", "init", "-b", "main", remote])
        run(["git", "-C", remote, "config", "user.email", "test@example.invalid"])
        run(["git", "-C", remote, "config", "user.name", "Launcher test"])
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        address = f"http://127.0.0.1:{port}"
        save(
            config,
            Settings(
                host="127.0.0.1",
                port=port,
                password_hash=hash_password("test-password"),
                signing_key="launcher-test-only",
            ),
        )
        if os.geteuid() == 0:
            account = pwd.getpwnam("nobody")
            os.chown(config.parent, account.pw_uid, account.pw_gid)
            os.chown(config, account.pw_uid, account.pw_gid)
        write_json(
            root / "deployment.json",
            {
                "schema": 1,
                "repository": str(remote),
                "branch": "main",
                "config": str(config),
                "service": None,
                "scope": "user",
                "health_url": address,
            },
        )

        def commit():
            run(["git", "-C", remote, "add", "."])
            run(["git", "-C", remote, "commit", "-m", "Test release"])
            return run(["git", "-C", remote, "rev-parse", "HEAD"])

        first = commit()
        Deployment(root).update(expect=first)
        with (base / "launcher.log").open("w+") as log:
            process = subprocess.Popen(
                [
                    str(root / "manager/venv/bin/python"),
                    "-m",
                    "talaria.supervisor",
                    "--directory",
                    str(root),
                ],
                stdout=log,
                stderr=log,
            )
            try:
                check_health(address, first)
                yield root, remote, config, address, first, commit, process
            except BaseException:
                log.seek(0)
                print(log.read())
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    pytest.fail("Launcher failed to shut down")
            assert not (root / control.SOCKET).exists()


def test_real_launcher_update_failure_and_rollback(installed_launcher):
    root, remote, config, address, first, commit, process = installed_launcher
    original = config.read_bytes()
    with (
        pytest.raises(DeploymentError, match="Stop the Talaria launcher"),
        stopped_launcher(root),
    ):
        pytest.fail("Uninstall must not remove a running launcher")
    module = remote / "src/talaria/app.py"
    module.write_text(module.read_text() + "\n# A new release.\n")
    second = commit()

    def operation(client, action, expect=None):
        body = {"id": secrets.token_hex(16)}
        if expect:
            body["expect"] = expect
        response = client.post(f"/api/installation/{action}", json=body)
        assert response.status_code == 202, response.text
        return eventually(
            lambda: (
                state
                if (state := read_json(root / control.JOB)).get("id") == body["id"]
                and state.get("status") in {"completed", "failed"}
                else None
            ),
            timeout=90,
        )

    with signed_in(address) as client:
        assert client.get("/api/installation").json()["can_update"]
        assert operation(client, "check")["status"] == "completed"
        assert client.get("/api/installation").json()["update"]["latest_commit"] == second
        assert operation(client, "update", second)["status"] == "completed"
        eventually(lambda: selected(root, "supervisor") == f"releases/{second}")
        check_health(address, second)
        assert process.poll() is None  # Launcher upgraded through exec, preserving its PID.
        if os.geteuid() == 0:
            children = run(["ps", "-eo", "ppid=,uid="]).splitlines()
            assert [
                int(line.split()[1]) for line in children if int(line.split()[0]) == process.pid
            ] == [pwd.getpwnam("nobody").pw_uid]
        good = module.read_text()
        module.write_text("raise RuntimeError('Synthetic broken release')\n")
        broken = commit()
        assert operation(client, "check")["status"] == "completed"
        assert operation(client, "update", broken)["status"] == "failed"
        check_health(address, second)
        assert selected(root, "current") == f"releases/{second}"
        module.write_text(good)
        (remote / "src/talaria/supervisor.py").unlink()
        incompatible = commit()
        assert operation(client, "check")["status"] == "completed"
        assert operation(client, "update", incompatible)["status"] == "failed"
        check_health(address, second)
        run([root / "bin/talaria", "rollback", "--directory", root])
        check_health(address, first)
        assert config.read_bytes() == original


def test_launcher_supplies_home_for_service_git_credentials(monkeypatch):
    from talaria.supervisor import clean_environment

    monkeypatch.delenv("HOME", raising=False)
    assert clean_environment()["HOME"] == pwd.getpwuid(os.geteuid()).pw_dir
    monkeypatch.setenv("HOME", "/explicit/service/home")
    assert clean_environment()["HOME"] == "/explicit/service/home"

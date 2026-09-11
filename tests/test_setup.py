"""Installer failure paths use only temporary homes and fake service managers."""

import io
import json
import os
import plistlib
import pty
import re
import select
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from talaria import setup as wizard
from talaria.auth import verify_password
from talaria.config import load
from talaria.deployment import DeploymentError, run
from talaria.setup_services import preflight, service_plan, unit_quote


@pytest.mark.parametrize("piped_stdin", [False, True])
@pytest.mark.parametrize("color", [False, True])
def test_interactive_setup_uses_controlling_terminal(piped_stdin, color):
    """Exercise real prompts and password echo with shell and curl-style stdin."""
    script = """
import fcntl
import sys
import termios
fcntl.ioctl(1, termios.TIOCSCTTY, 0)
sys.path.insert(0, sys.argv[1])
from talaria import setup as wizard
def check(args, prompts):
    assert prompts.ask('Service', 'auto', ('auto', 'none')) == 'none'
    assert wizard.getpass.getpass('Password: ', stream=prompts.terminal) == 'test-private-password'
    assert prompts.yes('Continue?', True)
    print('Interactive setup passed', flush=True)
wizard.setup = check
wizard.main([])
"""
    terminal, slave = pty.openpty()
    env = {**os.environ, "TERM": "xterm"}
    if color:
        env.pop("NO_COLOR", None)
    else:
        env["NO_COLOR"] = "1"
    try:
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(Path(wizard.__file__).resolve().parents[1])],
            stdin=subprocess.PIPE if piped_stdin else slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            env=env,
        )
    finally:
        os.close(slave)
    if child.stdin is not None:
        child.stdin.close()
    transcript = b""
    try:
        for prompt, answer in [
            (b'Service [auto]: ', b'none\n'),
            (b'Password: ', b'test-private-password\n'),
            (b'Continue? (yes/no) [yes]: ', b'\n'),
            (b'Interactive setup passed', None),
        ]:
            while prompt not in re.sub(rb"\x1b\[[0-9;]*m", b"", transcript):
                assert select.select([terminal], [], [], 10)[0], transcript.decode()
                try:
                    chunk = os.read(terminal, 4096)
                except OSError:
                    pytest.fail(f'Terminal closed before prompt: {transcript.decode()}')
                assert chunk, transcript.decode()
                transcript += chunk
            if answer is not None:
                os.write(terminal, answer)
        assert child.wait(timeout=10) == 0
        assert b'test-private-password' not in transcript
        assert (b'\x1b[' in transcript) == color
    finally:
        os.close(terminal)
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


@pytest.fixture
def options(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(wizard, "supported_service", lambda: "none")
    monkeypatch.setattr(wizard, "check_port", lambda host, port: None)
    args = wizard.parser().parse_args(
        [
            "--non-interactive",
            "--skip-hermes",
            "--service",
            "none",
            "--directory",
            str(tmp_path / "install"),
            "--config",
            str(tmp_path / "private/config.json"),
        ]
    )
    return args


def test_password_retries_and_accepts_four_characters(monkeypatch):
    replies = iter(["abc", "abcd", "different", "1234", "1234"])
    monkeypatch.setattr(wizard.getpass, "getpass", lambda *a, **kw: next(replies))
    terminal = io.StringIO()
    assert wizard.Prompts(terminal).password() == "1234"
    assert "4 to 1024" in terminal.getvalue() and "do not match" in terminal.getvalue()
    assert "\033[" not in terminal.getvalue()


def test_discovery_limits_directory_checks_to_32_homes(options, monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace

    options.skip_hermes = False
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    accounts = [
        SimpleNamespace(pw_dir=str(tmp_path / f"user{i}"), pw_name=f"user{i:04}",
                        pw_shell="/bin/bash")
        for i in range(1000)
    ]
    monkeypatch.setattr(wizard.pwd, "getpwall", lambda: accounts)
    checked = []
    monkeypatch.setattr(Path, "is_dir", lambda path: checked.append(path) or False)
    current = wizard.default_hermes_home(options)
    assert wizard.discover_hermes_home(options, wizard.Prompts(None)) == current
    assert checked[0] == current
    assert len(checked[1:]) == wizard.MAX_DISCOVERY_HOMES == 32
    assert "--hermes-home" in capsys.readouterr().out


def test_discovery_selects_without_reading_other_users_configuration(
    options, monkeypatch, tmp_path
):
    from types import SimpleNamespace

    options.skip_hermes = False
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    accounts = []
    for name in ("alice", "bob"):
        directory = tmp_path / name
        (directory / ".hermes").mkdir(parents=True)
        accounts.append(SimpleNamespace(pw_dir=str(directory), pw_name=name, pw_shell="/bin/sh"))
    monkeypatch.setattr(wizard.pwd, "getpwall", lambda: accounts)
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: pytest.fail("Read during discovery"))
    with pytest.raises(ValueError, match="select one with --hermes-home"):
        wizard.discover_hermes_home(options, wizard.Prompts(None))
    prompts = wizard.Prompts(io.StringIO())
    monkeypatch.setattr(prompts, "ask", lambda *a: "2")
    assert wizard.discover_hermes_home(options, prompts) == tmp_path / "bob/.hermes"
    assert options.hermes_home == tmp_path / "bob/.hermes"
    monkeypatch.setattr(wizard.pwd, "getpwall", lambda: pytest.fail("Ignored explicit selection"))
    assert wizard.discover_hermes_home(options, prompts) == options.hermes_home
    options.hermes_home = None
    current = wizard.default_hermes_home(options)
    current.mkdir(parents=True)
    assert wizard.discover_hermes_home(options, prompts) == current


@pytest.mark.parametrize("passwordless", [False, True])
def test_sudo_discovery_elevates_before_reading_other_homes(options, monkeypatch, passwordless):
    options.skip_hermes = False
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(wizard.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(wizard, "passwordless_sudo", lambda: passwordless)
    calls = []
    monkeypatch.setattr(wizard.subprocess, "call", lambda command: calls.append(command) or 0)
    monkeypatch.setattr(wizard, "elevate_setup", lambda args, **kw: calls.append(kw) or 0)
    prompts = wizard.Prompts(io.StringIO())
    monkeypatch.setattr(prompts, "yes", lambda *a: True)
    with pytest.raises(SystemExit) as stopped:
        wizard.setup(options, prompts)
    assert stopped.value.code == 0
    assert calls == ([] if passwordless else [["sudo", "-v"]]) + [{"service": "none"}]
    assert not options.directory.exists()


def test_discovered_home_uses_that_users_hermes_python(tmp_path):
    python = tmp_path / "alice/hermes-agent/venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    assert wizard.find_hermes_python(tmp_path / "alice/.hermes") == python


def test_invalid_setup_does_not_write_configuration(options):
    options.public_url = "https://example.com/path?oops"
    with pytest.raises(ValueError):
        wizard.setup(options, wizard.Prompts(None))
    assert not options.config.exists()


def test_interrupted_install_reuses_saved_password_on_retry(options, monkeypatch, capsys):
    def fail(command):
        raise DeploymentError("Simulated interrupted build")

    monkeypatch.setattr(wizard, "manage", fail)
    for _ in range(2):
        with pytest.raises(DeploymentError, match="interrupted build"):
            wizard.setup(options, wizard.Prompts(None))
        settings = load(options.config)
        password_path = options.config.parent / "initial-password.txt"
        password = password_path.read_text().strip()
        assert verify_password(password, settings.password_hash)
        assert password_path.stat().st_mode & 0o077 == 0
        assert options.config.stat().st_mode & 0o077 == 0
        if _ == 0:
            original = options.config.read_bytes(), password_path.read_bytes()
        else:
            assert original == (options.config.read_bytes(), password_path.read_bytes())
        assert password not in capsys.readouterr().out


def test_chosen_password_file_can_be_reused_but_cannot_reset_login(options, monkeypatch):
    password = options.directory.parent / "password"
    password.write_text("1234\n")
    password.chmod(0o600)
    options.password_file = password
    monkeypatch.setattr(wizard, "manage", lambda _: (_ for _ in ()).throw(DeploymentError("build")))
    for _ in range(2):
        with pytest.raises(DeploymentError, match="build"):
            wizard.setup(options, wizard.Prompts(None))
    original = options.config.read_bytes()
    password.write_text("A different private password\n")
    with pytest.raises(ValueError, match="different password"):
        wizard.setup(options, wizard.Prompts(None))
    assert options.config.read_bytes() == original
    assert not (options.config.parent / "initial-password.txt").exists()


def test_occupied_port_is_rejected():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        with pytest.raises(ValueError, match="Cannot bind"):
            wizard.check_port("127.0.0.1", sock.getsockname()[1])


def test_remote_url_does_not_reuse_unrelated_local_key(options):
    settings = load(options.config)
    settings.api_key = "previous-profile-secret"
    options.hermes_url = "https://other.example/hermes"
    wizard.configure_hermes(options, wizard.Prompts(None), settings, None, None, None)
    assert settings.hermes_url == options.hermes_url and not settings.api_key


def test_private_files_and_service_quoting(tmp_path, monkeypatch):
    private = tmp_path / "password"
    private.write_text("password")
    private.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        wizard.secret_file(private, "password")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / 'a space "$% directory'
    config = tmp_path / "private/config.json"
    plan = service_plan("systemd", root, config)
    assert '\\"$$%%' in plan["text"]
    assert "update" not in plan["text"] and "timer" not in plan["text"]
    plan["path"].parent.mkdir(parents=True)
    plan["path"].write_text("An unrelated existing unit")
    with pytest.raises(DeploymentError, match="preserved"):
        preflight(plan, root, config)
    assert plan["path"].read_text() == "An unrelated existing unit"
    launchd = service_plan("launchd", root, config)
    data = plistlib.loads(launchd["text"].encode())
    assert data["ProgramArguments"] == [
        str(root / "current/venv/bin/talaria"),
        "--config",
        str(config),
    ]
    assert "StartInterval" not in data
    with pytest.raises(ValueError):
        unit_quote("/a\nmalicious.service")


def test_bootstrap_help_and_unknown_branch_fail_before_execution(tmp_path):
    script = Path(__file__).resolve().parents[1] / "install.sh"
    subprocess.run(["bash", "-n", script], check=True)
    result = subprocess.run(["bash", script, "--help"], capture_output=True, text=True, check=True)
    assert "--non-interactive" in result.stdout and "No automatic updates" in result.stdout
    result = subprocess.run(["bash", script, "--repository"], capture_output=True, text=True)
    assert result.returncode and "needs a value" in result.stderr


def test_real_bootstrap_fresh_wheel_and_repeat_install(tmp_path):
    project = Path(__file__).resolve().parents[1]
    remote = tmp_path / "remote"
    remote.mkdir()
    for name in ("pyproject.toml", "uv.lock", ".gitignore", "install.sh"):
        shutil.copyfile(project / name, remote / name)
    shutil.copytree(project / "src", remote / "src", ignore=shutil.ignore_patterns("__pycache__"))
    run(["git", "init", "-b", "main", remote])
    run(
        [
            "git",
            "-C",
            remote,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "add",
            ".",
        ]
    )
    run(
        [
            "git",
            "-C",
            remote,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "Fixture",
        ]
    )
    root = tmp_path / "a space/install"
    config = tmp_path / "private/config.json"
    # Use the existing interpreter/uv without downloading or altering the host.
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "HERMES_HOME": str(tmp_path / "absent"),
        "UV_PYTHON": sys._base_executable,
        "UV_CACHE_DIR": str(tmp_path / "cold-cache"),
    }
    for name in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONPATH"):
        env.pop(name, None)
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    command = [
        "bash",
        remote / "install.sh",
        "--non-interactive",
        "--repository",
        str(remote),
        "--directory",
        str(root),
        "--config",
        str(config),
        "--skip-hermes",
        "--service",
        "none",
        "--port",
        str(port),
        "--public-url",
        "https://example.com/telaria",
    ]
    for attempt in range(3):
        with socket.socket() as occupied:
            if attempt:
                # An already-running manual instance must not block installing an update.
                occupied.bind(("127.0.0.1", port))
                occupied.listen()
            result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr
        runtime = root / "current/venv"
        packages = list(runtime.glob("lib/python*/site-packages/starlette/__init__.py"))
        assert packages and all(package.stat().st_mode & 0o004 for package in packages)
        settings = load(config)
        assert settings.public_url == "https://example.com/telaria"
        assert "Update manually:" in result.stdout
        assert not (root / ".setup-pending.json").exists()
        if attempt == 0:
            assert "Hermes: not configured" in result.stdout
        else:
            assert "saved setup is retained" in result.stdout
        if attempt == 0:
            saved = config.read_bytes()
            module = remote / "src/talaria/__init__.py"
            module.write_text(module.read_text() + "\n# Next fixture release.\n")
            run(["git", "-C", remote, "add", "."])
            run(
                [
                    "git",
                    "-C",
                    remote,
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-m",
                    "Next release",
                ]
            )
        else:
            assert config.read_bytes() == saved
            assert json.loads((root / "current/release.json").read_text())["commit"] == run(
                ["git", "-C", remote, "rev-parse", "HEAD"]
            )
    installed = run(
        [root / "current/venv/bin/python", "-c", "import talaria; print(talaria.__file__)"]
    )
    assert installed.startswith(str(root))
    assert json.loads((root / "deployment.json").read_text())["service"] is None
    removed = run([root / "bin/talaria", "uninstall", "--yes"])
    assert "Talaria uninstalled" in removed
    assert not root.exists() and not config.parent.exists()


@pytest.mark.hermes
@pytest.mark.skipif(
    not os.getenv("HERMES_SOURCE"), reason="Set HERMES_SOURCE for native setup checks"
)
@pytest.mark.parametrize("setup_request", [{}, {"enable": True}])
def test_native_hermes_external_api_key_is_detected_and_reused(tmp_path, setup_request):
    source = Path(os.environ["HERMES_SOURCE"])
    (tmp_path / ".env").write_text("BWS_ACCESS_TOKEN=test-bootstrap\nAPI_SERVER_ENABLED=true\n")
    (tmp_path / "config.yaml").write_text(
        "secrets:\n  bitwarden:\n    enabled: true\n    project_id: test-project\n"
        "    access_token_env: BWS_ACCESS_TOKEN\n"
    )
    original_config = (tmp_path / "config.yaml").read_bytes()
    script = """
import runpy, sys
from pathlib import Path
import agent.secret_sources.bitwarden as bitwarden
bitwarden.find_bws = lambda **kwargs: Path('/fake/bws')
bitwarden.fetch_bitwarden_secrets = lambda **kwargs: (
    {'API_SERVER_KEY': 'external-test-api-key'}, []
)
runpy.run_path(sys.argv[1], run_name='__main__')
"""
    result = subprocess.run(
        [str(source / "venv/bin/python"), "-c", script,
         str(Path(wizard.__file__).with_name("setup_hermes.py"))],
        input=json.dumps(setup_request), text=True, capture_output=True, check=True,
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path)},
        timeout=45,
    )
    data = json.loads(result.stdout.rsplit("TALARIA_SETUP_RESULT=", 1)[1])
    assert data["enabled"] and data["key"] == "external-test-api-key"
    assert "API_SERVER_KEY=" not in (tmp_path / ".env").read_text()
    assert (tmp_path / "config.yaml").read_bytes() == original_config


@pytest.mark.hermes
@pytest.mark.skipif(
    not os.getenv("HERMES_SOURCE"), reason="Set HERMES_SOURCE for native setup checks"
)
def test_native_hermes_key_reuse_plugin_and_config_preservation(tmp_path):
    source = Path(os.environ["HERMES_SOURCE"])
    python = source / "venv/bin/python"
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model:\n  default: preserve-this-model\n")
    original = (home / "config.yaml").read_bytes()
    info = wizard.hermes_request(python, home)
    assert not info["enabled"] and not info["key"]
    wizard.backup_hermes(home)
    from talaria.plugin_install import main as export

    export(["--home", str(home)])
    info = wizard.hermes_request(python, home, enable=True, plugin=True)
    assert info["enabled"] and len(info["key"]) >= 32
    again = wizard.hermes_request(python, home, enable=True, plugin=True)
    assert again["key"] == info["key"]
    assert (home / "config.yaml.before-talaria").read_bytes() == original
    assert "preserve-this-model" in (home / "config.yaml").read_text()
    assert "talaria" in (home / "config.yaml").read_text()
    assert (home / ".env").stat().st_mode & 0o077 == 0


def test_passwordless_sudo_selects_system_service_without_user_bus(monkeypatch):
    from talaria import setup_services

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "exists", lambda _: True)
    monkeypatch.setattr(shutil, "which", lambda command: "/usr/bin/" + command)
    calls = []
    monkeypatch.setattr(setup_services, "run", lambda command, **_: calls.append(command))
    assert setup_services.supported_service() == "systemd"
    assert calls == [["sudo", "-n", "true"]]


def test_sudo_elevation_preserves_explicit_paths_and_hermes_identity(options, monkeypatch):
    options.skip_hermes = False
    options.hermes_home = options.directory.parent / "users-profile"
    options.hermes_home.mkdir()
    options.hermes_python = options.directory.parent / "native-python"
    options.service = "auto"
    calls = []
    monkeypatch.setattr(subprocess, "call", lambda command: calls.append(command) or 0)
    assert wizard.elevate_setup(options) == 0
    command = calls[0]
    assert command[:5] == ["sudo", "-n", "-H", "--", "env"]
    assert "PYTHONDONTWRITEBYTECODE=1" in command[5:command.index(sys.executable)]
    assert command[command.index("--service") + 1] == "systemd"
    assert command[command.index("--hermes-home") + 1] == str(options.hermes_home)
    assert command[command.index("--hermes-python") + 1] == str(options.hermes_python)
    assert command[command.index("--config") + 1] == str(options.config)
    assert "--non-interactive" in command


def test_sudo_git_credentials_run_as_the_caller(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from talaria import deployment

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", "12345")
    monkeypatch.setattr(
        deployment.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path))
    )
    captured = []
    original = subprocess.Popen

    def launch(command, **kwargs):
        captured.append((command, kwargs["env"]))
        return original([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    deployment.run(["git", "fetch", "remote"])
    command, env = captured[0]
    assert "credential.helper=" not in command
    helper = next(arg for arg in command if arg.startswith("credential.helper=!"))
    assert "'#12345'" in helper and "credential" in helper
    assert "sudo -n -H -u '#12345'" in env["GIT_SSH_COMMAND"]
    assert command[-2:] == ["fetch", "remote"]


def test_root_shell_keeps_its_existing_git_credentials(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from talaria import deployment

    config = tmp_path / "gitconfig"
    config.write_text(
        '[credential]\n helper = "!f() { echo username=test-owner; '
        'echo password=test-token; }; f"\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("SUDO_UID", "12345")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        deployment.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path))
    )
    request = tmp_path / "request"
    request.write_text("protocol=https\nhost=example.invalid\n\n")
    original = subprocess.Popen
    with request.open() as input_file:
        monkeypatch.setattr(
            subprocess, "Popen", lambda *args, **kwargs: original(
                *args, stdin=input_file, **kwargs
            )
        )
        result = deployment.run(["git", "credential", "fill"], cwd=tmp_path)
    assert "username=test-owner" in result and "password=test-token" in result


def test_completed_installer_rerun_only_updates(options, monkeypatch, tmp_path):
    from talaria.config import Settings, save
    from talaria.deployment import Deployment, select, write_json

    from .test_deployment import A, release

    root = options.directory
    root.mkdir()
    save(options.config, Settings(password_hash="saved", api_key="keep-this-key"))
    select(root, "current", release(root, A))
    write_json(
        root / "deployment.json",
        {
            "schema": 1,
            "repository": options.repository,
            "branch": options.branch,
            "config": str(options.config),
            "service": "existing.service",
            "scope": "user",
        },
    )
    before = options.config.read_bytes()
    # Original setup flags must not trigger reconfiguration during an update.
    options.skip_hermes = False
    options.plugin = options.enable_hermes_api = options.restart_hermes = True
    options.password_file = tmp_path / "no-longer-needed-password-file"
    calls = []
    monkeypatch.setattr(Deployment, "update", lambda self, **kw: calls.append(kw))
    monkeypatch.setattr(Deployment, "restart", lambda _: pytest.fail("Extra restart"))
    monkeypatch.setattr(wizard, "configure_hermes", lambda *a: pytest.fail("Repeated Hermes setup"))
    monkeypatch.setattr(wizard, "supported_service", lambda: pytest.fail("Repeated service setup"))
    monkeypatch.setattr(wizard, "check_port", lambda *a: pytest.fail("Running port checked"))
    wizard.setup(options, wizard.Prompts(None))
    assert calls == [{"expect": None}]
    assert options.config.read_bytes() == before


def test_installer_refuses_ambiguous_installations(tmp_path, monkeypatch):
    first, second = tmp_path / "one", tmp_path / "two"
    for path in (first, second):
        path.mkdir()
        (path / "deployment.json").write_text("{}")
    monkeypatch.setattr(wizard, "default_directory", lambda: first)
    original = Path.resolve
    monkeypatch.setattr(
        Path, "resolve", lambda p: second if str(p) == "/opt/talaria" else original(p)
    )
    original_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda p: True if str(p) == "/opt/talaria/deployment.json" else original_file(p),
    )
    with pytest.raises(ValueError, match="Multiple installations"):
        wizard.existing_installation(wizard.parser().parse_args([]))


def test_failed_service_setup_resumes_after_release_was_installed(options, monkeypatch):
    from talaria.deployment import Deployment, select, write_json

    from .test_deployment import A, release

    options.service = "systemd"
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(wizard, "supported_service", lambda: "systemd")
    plan = {
        "kind": "systemd",
        "service": "test.service",
        "scope": "user",
        "path": options.directory.parent / "test.service",
        "text": "test service",
        "start": ["systemctl", "--user", "start", "test.service"],
        "stop": ["systemctl", "--user", "stop", "test.service"],
        "restart": ["systemctl", "--user", "restart", "test.service"],
    }
    monkeypatch.setattr(wizard, "service_plan", lambda *a: plan)
    monkeypatch.setattr(wizard, "preflight", lambda *a, **kw: None)
    monkeypatch.setattr(wizard, "prepare_account", lambda *a: None)
    monkeypatch.setattr(wizard, "run", lambda *a, **kw: None)
    monkeypatch.setattr(wizard, "check_health", lambda *a, **kw: None)
    monkeypatch.setattr(Deployment, "update", lambda *a, **kw: None)

    def stage(command):
        root = options.directory
        write_json(
            root / "deployment.json",
            {
                "schema": 1,
                "repository": options.repository,
                "branch": options.branch,
                "config": str(options.config),
                "scope": "user",
                "service": None,
            },
        )
        select(root, "current", release(root, A))

    monkeypatch.setattr(wizard, "manage", stage)
    attempts = []

    def start_service(plan):
        attempts.append(plan)
        if len(attempts) == 1:
            raise DeploymentError("Service unavailable")

    monkeypatch.setattr(wizard, "install_service", start_service)
    with pytest.raises(DeploymentError, match="Service unavailable"):
        wizard.setup(options, wizard.Prompts(None))
    saved = options.config.read_bytes()
    assert (options.directory / ".setup-pending.json").exists()
    wizard.setup(options, wizard.Prompts(None))
    assert len(attempts) == 2
    assert options.config.read_bytes() == saved
    assert not (options.directory / ".setup-pending.json").exists()
    assert (
        json.loads((options.directory / "deployment.json").read_text())["service"] == "test.service"
    )


def test_resumed_setup_restarts_only_when_saved_credentials_changed(options, monkeypatch):
    from talaria.config import Settings, save
    from talaria.deployment import Deployment, select, write_json

    from .test_deployment import A, release

    root = options.directory
    root.mkdir()
    save(options.config, Settings(password_hash="saved", api_key="previous-key"))
    select(root, "current", release(root, A))
    write_json(
        root / "deployment.json",
        {
            "schema": 1,
            "repository": options.repository,
            "branch": options.branch,
            "config": str(options.config),
            "service": "test.service",
            "scope": "user",
        },
    )
    write_json(root / ".setup-pending.json", {"service": "none", "config": str(options.config)})
    calls = []
    monkeypatch.setattr(Deployment, "update", lambda self, **kw: None)
    monkeypatch.setattr(Deployment, "restart", lambda self: calls.append("restart"))
    monkeypatch.setattr(wizard, "check_health", lambda *a: None)
    monkeypatch.setattr(wizard, "verify_hermes", lambda *a: "connected")

    def credentials(args, prompts, settings, *rest):
        settings.api_key = "new-key"
        return False

    monkeypatch.setattr(wizard, "configure_hermes", credentials)
    wizard.setup(options, wizard.Prompts(None))
    assert calls == ["restart"]
    assert load(options.config).api_key == "new-key"
    wizard.setup(options, wizard.Prompts(None))
    assert calls == ["restart"]

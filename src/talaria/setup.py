"""Guided, repeatable setup; no setup logic runs in the web application."""

import argparse
import fcntl
import getpass
import hashlib
import io
import ipaddress
import json
import os
import pwd
import shlex
import shutil
import socket
import subprocess
import sys
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path

from .auth import hash_password, verify_password
from .config import default_path, load, validate_public_url, validate_url
from .credentials import initialize_password
from .deployment import (
    COMMIT,
    REPOSITORY,
    Deployment,
    DeploymentError,
    check_health,
    default_directory,
    run,
    write_json,
)
from .deployment import (
    main as manage,
)
from .installation import read_json
from .setup_services import (
    describe,
    install_service,
    passwordless_sudo,
    preflight,
    prepare_account,
    service_plan,
    supported_service,
)

MAX_DISCOVERY_HOMES = 32


class Prompts:
    def __init__(self, terminal):
        self.terminal = terminal

    def highlight(self, text, code="1;36"):
        if (
            self.terminal is not None
            and self.terminal.isatty()
            and os.environ.get("TERM") != "dumb"
            and "NO_COLOR" not in os.environ
        ):
            return f"\033[{code}m{text}\033[0m"
        return text

    def ask(self, label, default="", choices=()):
        if self.terminal is None:
            return default
        while True:
            print(
                f"{self.highlight(label)} {self.highlight(f'[{default}]', '1;33')}: ",
                end="", file=self.terminal, flush=True,
            )
            answer = self.terminal.readline()
            if not answer:
                raise ValueError("Input closed; no further changes made.")
            answer = answer.strip() or default
            if not choices or answer in choices:
                return answer
            print(self.highlight("Choose " + ", ".join(choices), "1;31"), file=self.terminal)

    def yes(self, label, default=False):
        return self.ask(label + " (yes/no)", "yes" if default else "no", ("yes", "no")) == "yes"

    def password(self):
        while True:
            password = getpass.getpass(
                self.highlight("WebUI password (4+ characters): "), stream=self.terminal
            )
            if not 4 <= len(password) <= 1024:
                print(self.highlight("Use 4 to 1024 characters.", "1;31"), file=self.terminal)
                continue
            if password == getpass.getpass(
                self.highlight("Confirm password: "), stream=self.terminal
            ):
                return password
            print(self.highlight("Passwords do not match. Try again.", "1;31"), file=self.terminal)


def secret_file(path, label):
    with path.expanduser().open() as file:
        if os.fstat(file.fileno()).st_mode & 0o077:
            raise ValueError(f"{label} file must have owner-only permissions (chmod 600).")
        value = file.read(4097).rstrip("\r\n")
    if not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError(f"Invalid {label} file.")
    return value


def lan_address():
    candidates = set()
    try:
        for family, _, _, _, address in socket.getaddrinfo(socket.gethostname(), None):
            if family == socket.AF_INET:
                candidates.add(address[0])
        # Route lookup only; UDP connect sends no packet.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            candidates.add(sock.getsockname()[0])
    except OSError:
        pass
    private = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
    candidates = sorted(
        ip
        for ip in candidates
        if any(ipaddress.ip_address(ip) in ipaddress.ip_network(net) for net in private)
    )
    if len(candidates) != 1:
        raise ValueError("Could not choose one LAN address; pass --host with the desired local IP.")
    return candidates[0]


def local_url(host, port):
    if host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    return f"http://{'[' + host + ']' if ':' in host else host}:{port}"


def check_port(host, port):
    address = ipaddress.ip_address(host)
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    try:
        with socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET) as sock:
            sock.bind((host, port))
    except OSError as exc:
        raise ValueError(f"Cannot bind {host}:{port}; choose another address or --port.") from exc


def find_hermes_python(home):
    candidates = [
        home / "hermes-agent/venv/bin/python",
        home / "venv/bin/python",
        home.parent / "hermes-agent/venv/bin/python",
        Path.home() / "hermes-agent/venv/bin/python",
        Path("/usr/local/lib/hermes-agent/venv/bin/python"),
    ]
    executable = shutil.which("hermes")
    if executable:
        command = Path(executable).resolve()
        if (command.parent.parent / "pyvenv.cfg").is_file():
            candidates.append(command.parent / "python")
        try:
            first = command.open().readline().strip()
            if first.startswith("#!/") and "python" in first and " " not in first:
                candidates.append(Path(first[2:]))
        except (OSError, UnicodeError):
            pass
    return next((path for path in candidates if path.is_file()), None)


def hermes_request(python, home, **request):
    # Keep the native runtime isolated from Talaria's environment and never expose
    # native stdout/stderr: Hermes can include credentials in diagnostics.
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "SSL_CERT_FILE")
        if key in os.environ
    }
    env.update(HERMES_HOME=str(home), HERMES_ENABLE_PROJECT_PLUGINS="false")
    owner = home.stat()
    identity = {}
    if os.geteuid() == 0 and owner.st_uid != 0:
        account = pwd.getpwuid(owner.st_uid)
        identity = {
            "user": owner.st_uid,
            "group": owner.st_gid,
            "extra_groups": os.getgrouplist(account.pw_name, account.pw_gid),
        }
        env["HOME"] = account.pw_dir
    try:
        result = subprocess.run(
            [str(python), "-c", Path(__file__).with_name("setup_hermes.py").read_text()],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            cwd=home,
            env=env,
            timeout=45,
            **identity,
        )
        if result.returncode:
            raise ValueError(
                "Native Hermes configuration helper failed; configuration may be "
                "managed or this Hermes version may be incompatible. "
                "Use --skip-hermes and configure connectivity in the WebUI."
            )
        line = next(
            line
            for line in reversed(result.stdout.splitlines())
            if line.startswith("TALARIA_SETUP_RESULT=")
        )
        return json.loads(line.split("=", 1)[1])
    except (OSError, subprocess.TimeoutExpired, StopIteration, json.JSONDecodeError) as exc:
        raise ValueError(
            "Cannot inspect Hermes. Supply --hermes-python or use --skip-hermes."
        ) from exc


def backup_hermes(home):
    # Never replace the first backup on a retry; both files may contain secrets.
    for name in ("config.yaml", ".env"):
        source = home / name
        target = home / (name + ".before-talaria")
        if source.is_file() and not target.exists():
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as file:
                if os.geteuid() == 0:
                    owner = source.stat()
                    os.fchown(file.fileno(), owner.st_uid, owner.st_gid)
                file.write(source.read_bytes())
                file.flush()
                os.fsync(file.fileno())


def configure_hermes(args, prompts, settings, home, python, info):
    if args.hermes_url:
        url = validate_url(args.hermes_url)
        if url != settings.hermes_url:
            settings.api_key = ""
        settings.hermes_url = url
    if args.hermes_key_file:
        settings.api_key = secret_file(args.hermes_key_file, "Hermes API key")
    if args.skip_hermes:
        return False
    if info is None:
        if args.enable_hermes_api or args.plugin or args.restart_hermes or args.hermes_home:
            raise ValueError(
                "Local Hermes was not found; supply --hermes-home and --hermes-python."
            )
        if not settings.api_key and prompts.terminal:
            url = prompts.ask("Hermes API URL (blank to connect later)", args.hermes_url or "")
            if url:
                settings.hermes_url = validate_url(url)
                settings.api_key = getpass.getpass(
                    prompts.highlight("Hermes API key: "), stream=prompts.terminal
                )
        if not settings.api_key:
            print("Connect to Hermes in the WebUI after signing in.")
        return False
    print(f"Found Hermes at {home} (API {'enabled' if info['enabled'] else 'disabled'}).")
    enable = args.enable_hermes_api or (
        not info["enabled"] and prompts.yes("Enable this Hermes profile's API server?")
    )
    plugin = args.plugin or prompts.yes(
        "Install and enable Talaria's extended-access plugin?", True
    )
    # A headless run never implicitly changes Hermes, even for recommended defaults.
    if prompts.terminal is None:
        plugin = args.plugin
    if enable or plugin:
        backup_hermes(home)
        if plugin:
            from .plugin_install import main as export_plugin

            plugins = home / "plugins"
            parent_exists = plugins.exists()
            export_plugin(["--home", str(home)])
            if os.geteuid() == 0:
                owner = home.stat()
                target = plugins / "talaria"
                owned = [target, *target.iterdir()] + ([] if parent_exists else [plugins])
                for path in owned:
                    if not path.is_symlink():
                        os.chown(path, owner.st_uid, owner.st_gid)
        info = hermes_request(python, home, enable=enable, plugin=plugin)
    if info["enabled"] and info["key"]:
        if not args.hermes_url and not settings.api_key:
            settings.hermes_url = local_url(str(info["host"]), int(info["port"]))
        if not args.hermes_key_file and not settings.api_key and not args.hermes_url:
            settings.api_key = info["key"]
    changed = bool(enable or plugin)
    if changed:
        print(f"Hermes configuration backup: {home}/{{config.yaml,.env}}.before-talaria")
        if info.get("multiplex"):
            print(
                "Multiplexed gateway: also enable the plugin in its primary profile. "
                "Use --hermes-url for a routed /p/PROFILE endpoint."
            )
    restart = args.restart_hermes or (
        changed and prompts.yes("Restart Hermes now? Active work may be interrupted.")
    )
    if restart:
        command = shutil.which("hermes")
        if not command:
            raise ValueError("Hermes CLI is not on PATH; restart your gateway manually.")
        env = {**os.environ, "HERMES_HOME": str(home)}
        owner = home.stat()
        identity = {}
        if os.geteuid() == 0 and owner.st_uid != 0:
            account = pwd.getpwuid(owner.st_uid)
            identity = {
                "user": owner.st_uid,
                "group": owner.st_gid,
                "extra_groups": os.getgrouplist(account.pw_name, account.pw_gid),
            }
            env["HOME"] = account.pw_dir
        command = [command, "gateway", "restart"] + (["--system"] if owner.st_uid == 0 else [])
        # Restart diagnostics can contain private Hermes configuration; keep them private.
        try:
            result = subprocess.run(
                command, env=env, cwd=home, capture_output=True, timeout=60, **identity
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                "Hermes restart is still pending; check `hermes gateway status`."
            ) from exc
        if result.returncode:
            raise ValueError("Hermes could not restart; check `hermes gateway status`.")
    elif changed:
        print("Restart the Hermes gateway to apply its configuration changes.")
    return changed and not restart


def verify_hermes(settings):
    import httpx

    if not settings.api_key:
        return "not configured"
    try:
        with httpx.Client(timeout=8, trust_env=False) as client:
            response = client.get(
                settings.hermes_url + "/v1/capabilities",
                headers={"Authorization": "Bearer " + settings.api_key},
            )
            response.raise_for_status()
            data = response.json()
            if data.get("platform") != "hermes-agent" and not data.get("features"):
                return "unexpected API response; check the Hermes URL"
            plugin = client.get(
                settings.hermes_url + "/talaria/v1/capabilities",
                headers={"Authorization": "Bearer " + settings.api_key},
            )
            return (
                "connected; extended access available" if plugin.status_code == 200 else "connected"
            )
    except (httpx.HTTPError, ValueError, AttributeError):
        return "not reachable or authenticated yet; check the API address/key and gateway status"


def parser():
    result = argparse.ArgumentParser(
        description="Set up Talaria; root is optional. No automatic updates."
    )
    result.add_argument("--non-interactive", action="store_true")
    result.add_argument("--directory", type=Path)
    result.add_argument("--config", type=Path)
    result.add_argument("--repository", default=REPOSITORY)
    result.add_argument("--branch", default="main")
    result.add_argument("--expect", help="Require this full Git commit")
    binding = result.add_mutually_exclusive_group()
    binding.add_argument("--bind", choices=("local", "lan", "all"))
    binding.add_argument("--host", help="Explicit local IPv4 or IPv6 bind address")
    result.add_argument("--port", type=int)
    result.add_argument("--public-url", help="External URL, including any proxy subpath")
    result.add_argument("--password-file", type=Path)
    result.add_argument("--hermes-home", type=Path)
    result.add_argument("--hermes-python", type=Path)
    result.add_argument("--hermes-url")
    result.add_argument("--hermes-key-file", type=Path)
    result.add_argument("--enable-hermes-api", action="store_true")
    result.add_argument("--plugin", action="store_true")
    result.add_argument("--restart-hermes", action="store_true")
    result.add_argument("--skip-hermes", action="store_true")
    result.add_argument("--service", choices=("auto", "none", "systemd", "launchd"))
    return result


def default_hermes_home(args):
    return (
        (args.hermes_home or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")))
        .expanduser()
        .resolve()
    )


def discover_hermes_home(args, prompts):
    home = default_hermes_home(args)
    if (
        args.skip_hermes or args.hermes_url or args.hermes_home
        or "HERMES_HOME" in os.environ or home.is_dir() or os.geteuid() != 0
    ):
        return home
    # One directory lookup per distinct account home; never traverse its contents.
    candidates = {}
    seen = set()
    accounts = sorted(
        pwd.getpwall(),
        key=lambda account: (
            Path(account.pw_shell).name in {"nologin", "false", "sync", "halt", "shutdown"},
            account.pw_name,
        ),
    )
    for account in accounts:
        directory = Path(account.pw_dir)
        if not directory.is_absolute() or directory == Path("/"):
            continue
        candidate = directory / ".hermes"
        if candidate in seen:
            continue
        if len(seen) == MAX_DISCOVERY_HOMES:
            print(
                f"Discovery stopped after {MAX_DISCOVERY_HOMES} account homes. "
                "Use --hermes-home PATH for another location."
            )
            break
        seen.add(candidate)
        try:
            if candidate.is_dir():
                candidates[candidate] = account.pw_name
        except OSError:
            continue
    if not candidates:
        return home
    paths = sorted(candidates, key=str)
    if prompts.terminal is None:
        raise ValueError(
            "Hermes found under another user; select one with --hermes-home: "
            + ", ".join(map(str, paths))
        )
    print(Prompts(sys.stdout).highlight("Hermes installations:"))
    for number, path in enumerate(paths, 1):
        print(f"  {number}. {candidates[path]} — {path}")
    print("  0. Connect manually later")
    choice = prompts.ask(
        "Which Hermes installation should Talaria use?",
        "1" if len(paths) == 1 else "0",
        tuple(str(index) for index in range(len(paths) + 1)),
    )
    if choice == "0":
        args.skip_hermes = True
        return home
    args.hermes_home = paths[int(choice) - 1]
    return args.hermes_home


def elevate_setup(args, *, service="systemd"):
    """Elevate setup while retaining explicit paths and the caller's existing Hermes."""
    values = vars(args).copy()
    values["service"] = service
    home = default_hermes_home(args)
    if not args.skip_hermes and home.is_dir() and (not args.hermes_url or args.hermes_home):
        values["hermes_home"] = home
        values["hermes_python"] = args.hermes_python or find_hermes_python(home)
    argv = []
    for name, value in values.items():
        if value is None or value is False:
            continue
        argv.append("--" + name.replace("_", "-"))
        if value is not True:
            argv.append(str(value.expanduser().resolve() if isinstance(value, Path) else value))
    command = [
        "sudo",
        "-n",
        "-H",
        "--",
        "env",
        "PATH=" + os.environ["PATH"],
        # The caller must still be able to remove the temporary bootstrap venv.
        "PYTHONDONTWRITEBYTECODE=1",
        sys.executable,
        "-c",
        "from talaria.cli import main; main()",
        "setup",
        *argv,
    ]
    if os.environ.get("SSH_AUTH_SOCK"):
        command.insert(
            command.index(sys.executable), "SSH_AUTH_SOCK=" + os.environ["SSH_AUTH_SOCK"]
        )
    for name in ("TERM", "NO_COLOR"):
        if name in os.environ:
            command.insert(command.index(sys.executable), name + "=" + os.environ[name])
    # sudo sets SUDO_UID; Git can fall back to that account's credential helpers.
    return subprocess.call(command)


def setup(args, prompts):
    with ExitStack() as stack:
        return _setup(args, prompts, stack)


def existing_installation(args):
    if args.directory:
        candidates = [args.directory.expanduser().resolve()]
    else:
        candidates = [default_directory(), Path("/opt/talaria")]
    found = {
        path.resolve()
        for path in candidates
        if (path / "deployment.json").is_file() or (path / ".setup-pending.json").is_file()
    }
    if len(found) > 1:
        raise ValueError("Multiple installations found; select one with --directory.")
    return next(iter(found), None)


def setup_lock(root, stack):
    lock = stack.enter_context((root / ".setup.lock").open("a"))
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise ValueError("Another setup is running for this installation.") from exc


def print_management_commands(root):
    prefix = "sudo -n " if os.geteuid() == 0 and os.environ.get("SUDO_UID", "0") != "0" else ""
    print(f"Update manually: {prefix}{shlex.quote(str(root / 'bin/talaria'))} update")
    print(f"Uninstall: {prefix}{shlex.quote(str(root / 'bin/talaria'))} uninstall")


def update_existing(root, args, stack):
    deployment = Deployment(root)
    config = deployment.config
    if args.config and str(args.config.expanduser().resolve()) != config["config"]:
        raise ValueError("This installation already uses another config; use its existing path.")
    if config["repository"] != args.repository or config["branch"] != args.branch:
        raise ValueError(
            "This installation has another source; pass its existing repository/branch."
        )
    if os.geteuid() != 0 and (config.get("scope") == "system" or not os.access(root, os.W_OK)):
        if not passwordless_sudo():
            raise ValueError("This installation requires sudo to update.")
        args.directory = root
        raise SystemExit(elevate_setup(args))
    setup_lock(root, stack)
    print(f"Existing installation: {root}. Updating Talaria; saved setup is retained.")
    deployment.update(expect=args.expect)
    print(f"Config: {config['config']}")
    print_management_commands(root)
    if args.plugin:
        print(
            "The Hermes plugin is separate: refresh it with talaria hermes-plugin "
            "on the Hermes host, then restart the gateway."
        )


def _setup(args, prompts, stack):
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        raise ValueError("Use Linux, macOS, or WSL2; native Windows is not supported.")
    if args.expect and not COMMIT.fullmatch(args.expect):
        raise ValueError("--expect needs a full commit SHA.")
    if args.skip_hermes and (args.enable_hermes_api or args.plugin or args.restart_hermes):
        raise ValueError("--skip-hermes conflicts with Hermes modification flags.")
    for command in ("git", "uv"):
        if not shutil.which(command):
            raise ValueError(f"{command} is missing. Use install.sh to install prerequisites.")
    existing = existing_installation(args)
    if existing:
        pending = existing / ".setup-pending.json"
        if (existing / "current").is_symlink() and not pending.exists():
            return update_existing(existing, args, stack)
        args.directory = existing
        resume = read_json(pending)
        if args.config is None and resume.get("config"):
            args.config = Path(resume["config"])
        if args.service is None:
            args.service = resume.get("service")
    if (
        os.geteuid() != 0 and not args.skip_hermes and not args.hermes_url
        and not args.hermes_home and "HERMES_HOME" not in os.environ
        and not default_hermes_home(args).is_dir() and shutil.which("sudo")
    ):
        admin = passwordless_sudo()
        if not admin and prompts.terminal is not None and prompts.yes(
            "Check other users for Hermes using sudo?", True
        ):
            admin = subprocess.call(["sudo", "-v"]) == 0
        if admin:
            print("Using sudo to check other users and complete installation.")
            raise SystemExit(elevate_setup(args, service=args.service))
    available = supported_service()
    kind = args.service or "none"
    if args.service is None and prompts.terminal is not None and available != "none":
        if prompts.yes("Install as persistent background service?", True):
            kind = available
    if kind == "auto":
        kind = available
    if kind not in {"none", available}:
        raise ValueError(f"{kind} is not available in this login; use --service none.")
    if kind == "systemd" and os.geteuid() != 0 and passwordless_sudo():
        print("Using passwordless sudo for the system installation and app service.")
        raise SystemExit(elevate_setup(args))
    system = kind == "systemd" and os.geteuid() == 0
    root = (
        (args.directory or (Path("/opt/talaria") if system else default_directory()))
        .expanduser()
        .resolve()
    )
    old_mask = os.umask(0o022)
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o755)
    finally:
        os.umask(old_mask)
    setup_lock(root, stack)
    pending_setup = root / ".setup-pending.json"
    metadata = read_json(root / "deployment.json")
    config = (
        (
            args.config
            or (
                Path(metadata["config"])
                if metadata
                else Path("/var/lib/talaria/config.json")
                if system
                else default_path()
            )
        )
        .expanduser()
        .resolve()
    )
    if metadata and str(config) != metadata["config"]:
        raise ValueError("This installation already uses another config; use its existing path.")
    if metadata and (
        metadata["repository"] != args.repository or metadata["branch"] != args.branch
    ):
        raise ValueError(
            "This installation has another source; pass its existing repository/branch."
        )
    settings = load(config)
    original = (settings.host, settings.port, settings.public_url)
    home = discover_hermes_home(args, prompts)
    python = args.hermes_python or find_hermes_python(home)
    info = None
    if (
        not args.skip_hermes
        and home.is_dir()
        and python
        and (not args.hermes_url or args.hermes_home)
    ):
        if args.hermes_home or prompts.yes(f"Import the Hermes connection from {home}?", True):
            info = hermes_request(python, home)
    if args.host:
        settings.host = args.host
    elif args.bind:
        settings.host = {"local": "127.0.0.1", "all": "0.0.0.0"}.get(args.bind) or lan_address()
    elif not config.exists():
        suggested = str(info["host"]) if info else "127.0.0.1"
        default = "all" if suggested in {"0.0.0.0", "::"} and prompts.terminal else "local"
        if prompts.terminal is not None:
            print("local = this machine; lan = local network; all = all network interfaces.")
        binding = prompts.ask(
            "Allow connections from (local/lan/all)", default, ("local", "lan", "all")
        )
        settings.host = {"local": "127.0.0.1", "all": "0.0.0.0"}.get(binding) or lan_address()
    settings.port = args.port if args.port is not None else settings.port
    public = (
        args.public_url
        if args.public_url is not None
        else prompts.ask(
            "Public URL (optional, e.g. https://example.com/talaria)", settings.public_url
        )
    )
    settings.public_url = validate_public_url(public) if public else ""
    existing_service = metadata.get("service")
    if existing_service and original != (settings.host, settings.port, settings.public_url):
        raise ValueError(
            "For an existing service, edit its config and restart it to change its URL or binding."
        )
    if not existing_service:
        check_port(settings.host, settings.port)
    plan = service_plan(kind, root, config) if kind != "none" and not existing_service else None
    if plan:
        preflight(plan, root, config, resuming=bool(metadata) or pending_setup.exists())
    if args.password_file:
        password = secret_file(args.password_file, "sign-in password")
        if settings.password_hash and not verify_password(password, settings.password_hash):
            raise ValueError(
                "A different password exists; use talaria --set-password to change it."
            )
        if not 4 <= len(password) <= 1024:
            raise ValueError("Sign-in password must contain 4 to 1024 characters.")
        if not settings.password_hash:
            settings.password_hash = hash_password(password)
    elif not settings.password_hash and prompts.yes(
        "Choose your own WebUI password instead of generating one?", False
    ):
        settings.password_hash = hash_password(prompts.password())
    write_json(pending_setup, {"service": kind, "config": str(config)})
    pending = configure_hermes(args, prompts, settings, home, python, info)
    settings_changed = settings != load(config)
    password_path = initialize_password(config, settings)
    # Shared runtime files must be readable by the optional dedicated service user.
    old_mask = os.umask(0o022)
    try:
        if metadata:
            deployment = Deployment(root)
            before = deployment.status()["current"]["commit"]
            deployment.config["health_url"] = local_url(settings.host, settings.port)
            write_json(root / "deployment.json", deployment.config)
            deployment.update(expect=args.expect)
            if (
                existing_service
                and settings_changed
                and deployment.status()["current"]["commit"] == before
            ):
                # Resuming a failed setup can change credentials without changing releases.
                deployment.restart()
                check_health(deployment.config["health_url"], before)
        else:
            command = [
                "install",
                "--directory",
                str(root),
                "--config",
                str(config),
                "--repository",
                args.repository,
                "--branch",
                args.branch,
            ]
            if args.expect:
                command += ["--expect", args.expect]
            manage(command)
            deployment = Deployment(root)
        if plan:
            created = prepare_account(plan, config)
            deployment.config["setup"] = {
                "kind": plan["kind"],
                "scope": plan["scope"],
                "service_file": str(plan["path"]),
                "service_sha256": hashlib.sha256(plan["text"].encode()).hexdigest(),
                "account_created": bool(created or deployment.config.get("setup", {}).get(
                    "account_created"
                )),
            }
            write_json(root / "deployment.json", deployment.config)
            try:
                install_service(plan)
                check_health(
                    local_url(settings.host, settings.port),
                    deployment.status()["current"]["commit"],
                )
            except BaseException:
                # Do not leave a newly configured, failing app restarting forever.
                with suppress(DeploymentError):
                    run(plan["stop"], timeout=45)
                if plan["kind"] == "systemd":
                    with suppress(DeploymentError):
                        run([*plan["stop"][:-2], "disable", plan["service"]], timeout=10)
                raise
            deployment.config.update(
                service=plan["service"], scope=plan["scope"], manager=plan["kind"]
            )
            write_json(root / "deployment.json", deployment.config)
    finally:
        os.umask(old_mask)
    url = settings.public_url or local_url(settings.host, settings.port)
    if settings.host in {"0.0.0.0", "::"} and not settings.public_url:
        print(
            "Listening on all interfaces; replace the loopback address below "
            "with this server's IP for remote access."
        )
    print(f"\nURL: {url + '/'}\nConfig: {config}")
    if password_path or (config.parent / "initial-password.txt").is_file():
        print(f"Sign-in password file: {config.parent / 'initial-password.txt'}")
    connection = "restart required" if pending else verify_hermes(settings)
    print("Hermes: " + connection)
    if plan:
        for action, command in describe(plan).items():
            print(f"{action.capitalize()}: {command}")
    elif existing_service:
        print(f"Service: {existing_service} ({metadata.get('scope', 'user')})")
    else:
        print(f"Start: {shlex.quote(str(root / 'bin/talaria'))}")
    print_management_commands(root)
    if args.restart_hermes and (
        not connection.startswith("connected")
        or (args.plugin and "extended access available" not in connection)
    ):
        raise ValueError(
            "Talaria is installed, but Hermes connectivity was not verified. "
            "Check the gateway status and configured endpoint."
        )

    pending_setup.unlink(missing_ok=True)


@contextmanager
def terminal(interactive, flag="--non-interactive"):
    with ExitStack() as stack:
        stream = None
        if interactive:
            try:
                # Buffered read/write files require seeking; terminals cannot seek.
                raw = stack.enter_context(open("/dev/tty", "r+b", buffering=0))
                stream = stack.enter_context(io.TextIOWrapper(raw, write_through=True))
            except OSError as exc:
                raise ValueError(f"No terminal; use {flag} and explicit flags.") from exc
        yield Prompts(stream)


def main(argv):
    command = parser()
    args = command.parse_args(argv)
    try:
        with terminal(not args.non_interactive) as prompts:
            setup(args, prompts)
    except (ValueError, OSError, DeploymentError) as exc:
        error = Prompts(sys.stderr).highlight(f"Talaria setup: {exc}", "1;31")
        command.exit(
            1,
            error + "\n"
            "Re-run with the same options to retry; saved credentials are preserved.\n",
        )
    except (KeyboardInterrupt, EOFError):
        command.exit(130, "Talaria setup interrupted. Re-run with the same options to resume.\n")

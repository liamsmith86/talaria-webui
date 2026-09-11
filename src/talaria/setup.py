"""Guided, repeatable setup; no setup logic runs in the web application."""

import argparse
import fcntl
import getpass
import ipaddress
import json
import os
import pwd
import shlex
import shutil
import socket
import subprocess
import sys
from contextlib import ExitStack, suppress
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


class Prompts:
    def __init__(self, terminal):
        self.terminal = terminal

    def ask(self, label, default="", choices=()):
        if self.terminal is None:
            return default
        while True:
            print(f"{label} [{default}]: ", end="", file=self.terminal, flush=True)
            answer = self.terminal.readline()
            if not answer:
                raise ValueError("Input closed; no further changes made.")
            answer = answer.strip() or default
            if not choices or answer in choices:
                return answer
            print("Choose " + ", ".join(choices), file=self.terminal)

    def yes(self, label, default=False):
        return self.ask(label + " (yes/no)", "yes" if default else "no", ("yes", "no")) == "yes"


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
        Path.home() / "hermes-agent/venv/bin/python",
        Path("/usr/local/lib/hermes-agent/venv/bin/python"),
    ]
    executable = shutil.which("hermes")
    if executable:
        command = Path(executable).resolve()
        if (command.parent.parent / "pyvenv.cfg").is_file():
            candidates.insert(0, command.parent / "python")
        try:
            first = command.open().readline().strip()
            if first.startswith("#!/") and "python" in first and " " not in first:
                candidates.insert(0, Path(first[2:]))
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
                settings.api_key = getpass.getpass("Hermes API key: ", stream=prompts.terminal)
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


def elevate_setup(args):
    """Only service installation elevates; retain explicit paths and the caller's Hermes."""
    values = vars(args).copy()
    values["service"] = "systemd"
    home = (
        (args.hermes_home or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")))
        .expanduser()
        .resolve()
    )
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
    # sudo sets SUDO_UID; Git uses that account's credential helpers during deployment.
    return subprocess.call(command)


def setup(args, prompts):
    with ExitStack() as stack:
        return _setup(args, prompts, stack)


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
    available = supported_service()
    kind = args.service or prompts.ask(
        "App service (auto/none)", "auto" if available != "none" else "none", ("auto", "none")
    )
    if prompts.terminal is None and not args.service:
        kind = "none"
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
    setup_lock = stack.enter_context((root / ".setup.lock").open("a"))
    try:
        fcntl.flock(setup_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise ValueError("Another setup is running for this installation.") from exc
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
    home = (
        (args.hermes_home or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")))
        .expanduser()
        .resolve()
    )
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
        binding = prompts.ask("Bind (local/lan/all)", default, ("local", "lan", "all"))
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
        preflight(plan, root, config, resuming=bool(metadata))
    if args.password_file:
        password = secret_file(args.password_file, "sign-in password")
        if settings.password_hash and not verify_password(password, settings.password_hash):
            raise ValueError(
                "A different password exists; use talaria --set-password to change it."
            )
        if not 12 <= len(password) <= 1024:
            raise ValueError("Sign-in password must contain 12 to 1024 characters.")
        if not settings.password_hash:
            settings.password_hash = hash_password(password)
    elif not settings.password_hash and prompts.yes("Choose your own WebUI password?", False):
        password = getpass.getpass("WebUI password (12+ characters): ", stream=prompts.terminal)
        if not 12 <= len(password) <= 1024 or password != getpass.getpass(
            "Confirm: ", stream=prompts.terminal
        ):
            raise ValueError("Passwords must match and contain 12 to 1024 characters.")
        settings.password_hash = hash_password(password)
    pending = configure_hermes(args, prompts, settings, home, python, info)
    password_path = initialize_password(config, settings)
    # Shared runtime files must be readable by the optional dedicated service user.
    old_mask = os.umask(0o022)
    try:
        if metadata:
            deployment = Deployment(root)
            deployment.config["health_url"] = local_url(settings.host, settings.port)
            write_json(root / "deployment.json", deployment.config)
            deployment.update(expect=args.expect)
            if existing_service:
                # A no-op release update still needs to pick up configuration changes.
                deployment.restart()
                check_health(
                    deployment.config["health_url"], deployment.status()["current"]["commit"]
                )
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
            prepare_account(plan, config)
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
    prefix = "sudo -n " if os.geteuid() == 0 and os.environ.get("SUDO_UID", "0") != "0" else ""
    print(f"Update manually: {prefix}{shlex.quote(str(root / 'bin/talaria'))} update")
    if args.restart_hermes and (
        not connection.startswith("connected")
        or (args.plugin and "extended access available" not in connection)
    ):
        raise ValueError(
            "Talaria is installed, but Hermes connectivity was not verified. "
            "Check the gateway status and configured endpoint."
        )


def main(argv):
    command = parser()
    args = command.parse_args(argv)
    try:
        with ExitStack() as stack:
            terminal = None
            if not args.non_interactive:
                try:
                    terminal = stack.enter_context(open("/dev/tty", "r+"))
                except OSError as exc:
                    raise ValueError(
                        "No terminal; use --non-interactive and explicit flags."
                    ) from exc
            setup(args, Prompts(terminal))
    except (ValueError, OSError, DeploymentError) as exc:
        command.exit(
            1,
            f"Talaria setup: {exc}\n"
            "Re-run with the same options to retry; saved credentials are preserved.\n",
        )
    except KeyboardInterrupt:
        command.exit(130, "Talaria setup interrupted. Re-run with the same options to resume.\n")

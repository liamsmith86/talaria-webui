"""The Hermes home on this machine whose gateway serves a connection, and the Talaria plugin in it.

Only a few small files are read; Hermes is never run or imported. A service install's launcher
answers for its web process, which can't read another account's home.
"""

import ipaddress
import json
import os
import pwd
import shlex
import socket
from pathlib import Path
from urllib.parse import urlsplit

from .hermes_plugin.release import PLUGIN_VERSION
from .maintenance import DeploymentError
from .plugin_install import hermes_install
from .plugin_status import installed_status
from .setup_command import short_command

MAX_DISCOVERY_HOMES = 32
MAX_PROFILES = 32
STATUSES = {"missing", "current", "outdated", "newer", "different", "unknown"}


def account_homes(report=print):
    """Each account's existing ~/.hermes, with one directory lookup per distinct account home."""
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
            report(
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
    return candidates


def profiles(home):
    try:
        return sorted(path for path in (home / "profiles").iterdir() if path.is_dir())[
            :MAX_PROFILES
        ]
    except OSError:
        return []


def candidate_homes(recorded=()):
    """Homes setup recorded, then this account's, then every account's when running as root.
    A profile that runs its own gateway records it in its own home."""
    homes = [*map(Path, recorded)]
    if os.environ.get("HERMES_HOME"):
        homes.append(Path(os.environ["HERMES_HOME"]))
    homes.append(Path.home() / ".hermes")
    if os.geteuid() == 0:
        homes += account_homes(report=lambda _: None)
    found = []
    for home in homes:
        for path in (home, *profiles(home)):
            if path.is_absolute() and path not in found:
                found.append(path)
    return found


def process_start(pid):
    """Field 22 of /proc/<pid>/stat, the start time Hermes records with its PID on Linux."""
    try:
        return int(Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()[19])
    except (IndexError, ValueError, OSError):
        return None


def gateway_pid(home):
    """The running gateway that recorded itself in this home, or None."""
    try:
        with (home / "gateway.pid").open() as file:
            record = json.loads(file.read(4096))
    except (OSError, ValueError):
        return None
    pid, start = (record, None) if type(record) is int else (None, None)
    if isinstance(record, dict):
        pid, start = record.get("pid"), record.get("start_time")
    if type(pid) is not int or pid <= 0:
        return None
    # Without /proc (macOS), Hermes records a psutil time, which can't be compared here.
    if type(start) is int and Path("/proc/self/stat").exists() and process_start(pid) != start:
        return None
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass
    except OSError:
        return None
    return pid


def owner_name(home):
    try:
        return pwd.getpwuid(home.stat().st_uid).pw_name
    except (KeyError, OSError):
        return ""


def describe(pid, recorded=(), linked=None):
    """The plugin in the home whose gateway is this PID; None if no readable home is."""
    try:
        home = next((home for home in candidate_homes(recorded) if gateway_pid(home) == pid), None)
        if home is None or len(str(home)) > 1024:
            return None
        release = installed_status(home)
        source = hermes_install(home)
    except (OSError, DeploymentError):
        # Without Hermes's install record, who updates the plugin is unknown, so say nothing.
        return None
    managed = None
    if release["status"] != "missing":
        managed = "hermes" if source else "talaria" if linked and Path(linked) == home else "manual"
    return {
        "home": str(home),
        "owner": owner_name(home),
        **release,
        "managed": managed,
        "source": source if managed == "hermes" else None,
    }


def local_address(url):
    """Whether a connection's host is this machine. A PID alone could match an unrelated local
    gateway when Talaria connects to another machine."""
    host = urlsplit(url).hostname
    if not host:
        return False
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False
    for family, _, _, _, address in addresses:
        try:
            if ipaddress.ip_address(address[0].split("%")[0]).is_loopback:
                return True
            # Only an address of this machine can be bound.
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.bind((address[0], 0, *address[2:]))
            return True
        except (OSError, ValueError):
            continue
    return False


def default_home(owner):
    """The ~/.hermes that both Hermes and Talaria use when the owner names no other."""
    try:
        return Path(pwd.getpwnam(owner).pw_dir) / ".hermes"
    except KeyError:
        return None


def hermes_cli(home, owner):
    """The Hermes command that selects this home when its owner runs it, as Hermes's own
    guidance spells it."""
    default = default_home(owner)
    if home == default:
        return "hermes"
    if default and home.parent == default / "profiles":
        return f"hermes -p {shlex.quote(home.name)}"
    return f"HERMES_HOME={shlex.quote(str(home))} hermes"


def commands(facts, talaria, *, linkable, elevate):
    """The commands Settings offers for this plugin, run on this machine as the home's owner."""
    home = Path(facts["home"])
    hermes = hermes_cli(home, facts["owner"])
    talaria = f"{'sudo ' if elevate else ''}{talaria} hermes-plugin"
    # Under sudo, Talaria's default would be root's home, and sudo's PATH may not reach Hermes.
    if elevate or home != default_home(facts["owner"]):
        talaria += f" --home {shlex.quote(str(home))}"
    command = ' --hermes-command "$(command -v hermes)"' if elevate else ""
    enable = f"{hermes} plugins enable talaria-webui"
    # The restart verifies that the gateway serves the new files, and restores the old ones if not.
    link = [f"{talaria} --manage-updates{command}"] * linkable
    install = [talaria, enable, f"{talaria} --restart", *link]
    return {
        "hermes_update": [f"{hermes} plugins update talaria-webui", f"{hermes} gateway restart"],
        "refresh": [f"{talaria} --restart", *link],
        "install": install,
        "switch": [f"{hermes} plugins remove talaria-webui", *install],
        "enable": [enable, f"{hermes} gateway restart"],
    }


def valid(facts):
    return (
        isinstance(facts, dict)
        and isinstance(facts.get("home"), str)
        and Path(facts["home"]).is_absolute()
        and isinstance(facts.get("owner"), str)
        and facts.get("status") in STATUSES
        and facts.get("managed") in {None, "hermes", "talaria", "manual"}
        and facts.get("source") in {None, "catalog", "git", "other"}
    )


def local_plugin(url, pid, development=False):
    """What Settings shows about the plugin in the Hermes on this machine serving a connection;
    None when that Hermes is elsewhere or its home can't be read."""
    from . import control
    from .installation import managed_root, read_json

    if type(pid) is not int or not 0 < pid < 2**31 or not local_address(url):
        return None
    root = None if development else managed_root()
    config = read_json(root / "deployment.json") if root else {}
    if root and (root / control.SOCKET).exists():
        try:
            facts = control.request(root, {"action": "plugin", "pid": pid}).get("plugin")
        except (control.ControlError, OSError, ValueError):
            return None
    else:
        facts = describe(pid)
    if not valid(facts):
        return None
    # Only a launcher-managed install can link the plugin to its updates, unless another tool
    # runs its service.
    linkable = bool(root) and not (config.get("service") and not config.get("setup"))
    elevate = config.get("scope") == "system" and facts["owner"] != "root"
    talaria = "talaria"
    if root:
        talaria = shlex.quote(str(root / "bin/talaria")) if elevate else short_command(root)
    return {
        **{key: facts.get(key) for key in ("home", "owner", "status", "version", "managed")},
        "source": facts.get("source"),
        "bundled_version": PLUGIN_VERSION,
        "commands": commands(facts, talaria, linkable=linkable, elevate=elevate),
    }

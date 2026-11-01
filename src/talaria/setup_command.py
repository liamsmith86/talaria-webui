"""Make the installed launcher a command the user can run, and say what still needs them."""

import os
import pwd
import shlex
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path

LINK_NAME = "talaria"
LINGER_DIRECTORY = Path("/var/lib/systemd/linger")


def link_directories():
    return Path("/usr/local/bin"), Path.home() / ".local/bin"


def is_link_to(link, launcher):
    return link.is_symlink() and Path(os.readlink(link)) == launcher


def links_to(root):
    launcher = root / "bin/talaria"
    return [
        directory / LINK_NAME
        for directory in link_directories()
        if is_link_to(directory / LINK_NAME, launcher)
    ]


def shell_path():
    # install.sh and uv add their own directories, so PATH alone can't say what the user finds.
    return os.environ.get("TALARIA_SHELL_PATH", os.environ.get("PATH", ""))


def sudo_prefix():
    return "sudo -n " if os.geteuid() == 0 and os.environ.get("SUDO_UID", "0") != "0" else ""


def link_launcher(root):
    """Link the launcher among the user's commands. Whatever is already there stays."""
    system, user = link_directories()
    link = (system if os.geteuid() == 0 else user) / LINK_NAME
    launcher = root / "bin/talaria"
    with suppress(OSError):
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(launcher)
    return link if is_link_to(link, launcher) else None


def resolved_command():
    """What `talaria` means where the printed commands will run."""
    if sudo_prefix():
        # Sudo can search a narrower PATH than the caller's.
        with suppress(OSError, subprocess.SubprocessError):
            found = subprocess.run(
                ["sudo", "-n", "sh", "-c", f"command -v {LINK_NAME}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return found.stdout.strip()
        return None
    return shutil.which(LINK_NAME, path=shell_path())


def install_command(root):
    """Return how to run this installation: its short name only where that really reaches it."""
    launcher = root / "bin/talaria"
    link = link_launcher(root)
    if not link:
        return shlex.quote(str(launcher))
    found = resolved_command()
    if found and Path(found).resolve() == launcher.resolve():
        return LINK_NAME
    if not sudo_prefix() and str(link.parent) not in shell_path().split(os.pathsep):
        print(f"Add {link.parent} to your PATH to run `{LINK_NAME}` from anywhere.")
    return shlex.quote(str(launcher))


def print_linger_hint(setup):
    """A user's systemd service stops at logout and waits for login after a reboot."""
    if setup.get("kind") != "systemd" or setup.get("scope") != "user":
        return
    if not (LINGER_DIRECTORY / pwd.getpwuid(os.geteuid()).pw_name).exists():
        print("To keep Talaria running while you're logged out: loginctl enable-linger")

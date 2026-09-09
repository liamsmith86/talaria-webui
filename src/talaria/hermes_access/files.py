"""Optional, bounded reads of explicitly supported Hermes identity files."""

import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_BYTES = 64 * 1024
IDENTITY_FILES = ("IDENTITY.md", "SOUL.md")


@dataclass(frozen=True)
class AccessDetails:
    status: str = "disabled"
    name: str = ""
    source: str = ""

    def public(self):
        return asdict(self)


def validate_home(value: str) -> str:
    if not value.strip():
        return ""
    if any(ord(c) < 32 for c in value):
        raise ValueError("Enter a directory on the Talaria server.")
    try:
        path = Path(value.strip()).expanduser()
    except RuntimeError as exc:
        raise ValueError("Enter an absolute directory on the Talaria server.") from exc
    if not path.is_absolute():
        raise ValueError("Enter an absolute directory, such as /home/you/.hermes.")
    if path.name == "config.yaml":
        path = path.parent
    return str(path)


def _name(value: str) -> str:
    value = value.strip().strip("*\"'").strip()
    if not 1 <= len(value) <= 80 or value.lower().startswith(("a ", "an ", "the ")):
        return ""
    return value if all(c.isalnum() or c in " -_’'" for c in value) else ""


def identity_name(content: str) -> str:
    field = re.search(
        r"(?im)^[ \t]*(?:[-*][ \t]+)?(?:\*\*)?name(?:\*\*)?[ \t]*:(?:\*\*)?[ \t]*([^\n]+)$", content
    )
    if field and (name := _name(field[1])):
        return name
    introduction = re.search(r"(?im)^you are\s+([^,.;!\n]{1,84})[,.;!\n]", content + "\n")
    return _name(introduction[1]) if introduction else ""


def _read(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("Unsupported identity file")
        data = file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Oversized identity file")
        return data.decode("utf-8-sig")


def inspect_home(directory: str) -> AccessDetails:
    if not directory:
        return AccessDetails()
    if not isinstance(directory, str):
        return AccessDetails("unreadable")
    try:
        home = Path(validate_home(directory)).resolve()
        if not home.is_dir():
            return AccessDetails("not_found")
    except (OSError, ValueError, RuntimeError):
        return AccessDetails("unreadable")
    status = "no_name"
    for filename in IDENTITY_FILES:
        try:
            path = home / filename
            if path.is_symlink():
                status = "unsupported"
                continue
            content = _read(path)
            if name := identity_name(content):
                return AccessDetails("ready", name, filename)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, UnicodeError):
            status = "unreadable"
    return AccessDetails(status)

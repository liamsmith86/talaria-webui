"""Identity projection inside Hermes. No paths or file contents cross the API."""

import re
from pathlib import Path

from .files import read_text

MAX_BYTES = 64 * 1024
IDENTITY_FILES = ("IDENTITY.md", "SOUL.md")


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


def read_name(home: Path) -> str:
    for filename in IDENTITY_FILES:
        try:
            path = home / filename
            if path.is_symlink():
                continue
            if name := identity_name(read_text(path, MAX_BYTES)):
                return name
        except (OSError, ValueError, UnicodeError):
            continue
    return ""

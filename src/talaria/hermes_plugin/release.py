"""Identify the plugin code loaded by this gateway process."""

import hashlib
import re
from pathlib import Path


def fingerprint(directory):
    digest = hashlib.sha256()
    for path in sorted(Path(directory).iterdir()):
        if path.is_file() and (path.suffix == ".py" or path.name == "plugin.yaml"):
            digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def read_field(directory, field, prefix=""):
    """A version-shaped field of the manifest, such as ``version`` or ``requires_hermes``."""
    try:
        with (Path(directory) / "plugin.yaml").open() as manifest:
            match = re.search(
                rf"^{field}: *[\"\']?{prefix}(\d{{1,4}}\.\d{{1,4}}\.\d{{1,4}})[\"\']? *$",
                manifest.read(4096),
                re.MULTILINE,
            )
        return match[1] if match else ""
    except (OSError, UnicodeError):
        return ""


def read_version(directory):
    return read_field(directory, "version")


PLUGIN_VERSION = read_version(Path(__file__).parent)
# Talaria's own minimum too: setup, plugin updates and Settings check Hermes against it.
HERMES_MINIMUM = read_field(Path(__file__).parent, "requires_hermes", ">=")
LOADED_REVISION = fingerprint(Path(__file__).parent)

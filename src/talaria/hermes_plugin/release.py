"""Identify the plugin code loaded by this gateway process."""

import hashlib
from pathlib import Path


def fingerprint(directory):
    digest = hashlib.sha256()
    for path in sorted(Path(directory).iterdir()):
        if path.is_file() and (path.suffix == ".py" or path.name == "plugin.yaml"):
            digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


LOADED_REVISION = fingerprint(Path(__file__).parent)

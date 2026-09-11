"""Compare optional plugin release metadata with this Talaria bundle, without network checks."""

import re
from pathlib import Path

from .hermes_plugin.release import LOADED_REVISION, PLUGIN_VERSION, fingerprint, read_version


def release_status(data):
    data = data if isinstance(data, dict) else {}
    version = data.get("plugin_version")
    version = (
        version
        if isinstance(version, str) and re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", version)
        else ""
    )
    revision = data.get("revision")
    revision = (
        revision if isinstance(revision, str) and re.fullmatch("[0-9a-f]{64}", revision) else ""
    )
    status = "unknown"
    if revision == LOADED_REVISION:
        status = "current"
    elif version:
        installed = tuple(map(int, version.split(".")))
        bundled = tuple(map(int, PLUGIN_VERSION.split(".")))
        status = (
            "outdated" if installed < bundled else "newer" if installed > bundled else "different"
        )
    elif revision:
        status = "different"
    return {"status": status, "version": version, "bundled_version": PLUGIN_VERSION}


def installed_status(home):
    directory = Path(home) / "plugins/talaria"
    if not (directory / "plugin.yaml").is_file():
        return {"status": "missing", "version": "", "bundled_version": PLUGIN_VERSION}
    return release_status(
        {"plugin_version": read_version(directory), "revision": fingerprint(directory)}
    )

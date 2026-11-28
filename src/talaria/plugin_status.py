"""Compare optional plugin release metadata with this Talaria bundle, without network checks."""

import re
from pathlib import Path

from .hermes_plugin.release import (
    HERMES_MINIMUM,
    LOADED_REVISION,
    PLUGIN_VERSION,
    fingerprint,
    read_version,
)


def hermes_supported(version):
    """Whether a Hermes version meets Talaria's minimum, or None when it can't be read."""
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version) if isinstance(version, str) else None
    if not match:
        return None
    return tuple(map(int, match.groups())) >= tuple(map(int, HERMES_MINIMUM.split(".")))


def outdated_hermes(version):
    """Why this Hermes can't take Talaria's plugin; None if it can or its version is unknown."""
    if hermes_supported(version) is not False:
        return None
    return (
        f"Talaria needs Hermes {HERMES_MINIMUM} or later, and this is Hermes {version}. "
        "Update Hermes, then try again."
    )


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


def reported_status(data):
    """The release a plugin's answer describes: once a restart is due, the files it will load."""
    data = data if isinstance(data, dict) else {}
    installed = data.get("installed")
    if data.get("state") == "restart_required" and isinstance(installed, dict):
        return release_status(installed)
    return release_status(data)


def installed_status(home):
    directory = Path(home) / "plugins/talaria-webui"
    if not (directory / "plugin.yaml").is_file():
        return {"status": "missing", "version": "", "bundled_version": PLUGIN_VERSION}
    return release_status(
        {"plugin_version": read_version(directory), "revision": fingerprint(directory)}
    )

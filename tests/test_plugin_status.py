"""Release drift must not disguise newer/custom plugins or break ordinary chat."""

import getpass
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from playwright.sync_api import expect

from talaria import local_hermes, plugin_status
from talaria.hermes_plugin.release import HERMES_MINIMUM, LOADED_REVISION, PLUGIN_VERSION

from .conftest import wait_for_store
from .test_app import signed_in


@pytest.mark.parametrize(
    ("data", "status"),
    [
        ({"revision": LOADED_REVISION}, "current"),
        ({"plugin_version": "1.0.0"}, "outdated"),
        ({"plugin_version": "999.0.0"}, "newer"),
        ({"plugin_version": PLUGIN_VERSION, "revision": "a" * 64}, "different"),
        ({"revision": "a" * 64}, "different"),
        ({"plugin_version": [1, 0, 0], "revision": {}}, "unknown"),
        ({"plugin_version": "1.0.0<script>"}, "unknown"),
        (None, "unknown"),
    ],
)
def test_release_comparison_is_bounded_and_does_not_infer_downgrades(data, status):
    result = plugin_status.release_status(data)
    assert result["status"] == status
    assert result["bundled_version"] == PLUGIN_VERSION


def test_local_detection_compares_files_not_just_the_version(tmp_path):
    assert plugin_status.installed_status(tmp_path)["status"] == "missing"
    target = tmp_path / "plugins/talaria-webui"
    shutil.copytree(Path(plugin_status.__file__).with_name("hermes_plugin"), target)
    assert plugin_status.installed_status(tmp_path)["status"] == "current"
    with (target / "bridge.py").open("a") as file:
        file.write("\n# Local modification\n")
    assert plugin_status.installed_status(tmp_path)["status"] == "different"


def test_local_commands_leave_out_what_the_owner_already_has():
    owner = getpass.getuser()
    home = local_hermes.default_home(owner)
    facts = {"home": str(home), "owner": owner}
    plain = local_hermes.commands(facts, "talaria", linkable=True, elevate=False)
    assert plain["hermes_update"][0] == "hermes plugins update talaria-webui"
    assert plain["refresh"] == [
        "talaria hermes-plugin --restart",
        "talaria hermes-plugin --manage-updates",
    ]
    # sudo changes the home Talaria defaults to and the PATH that finds Hermes.
    elevated = local_hermes.commands(facts, "talaria", linkable=True, elevate=True)
    assert elevated["refresh"] == [
        f"sudo talaria hermes-plugin --home {home} --restart",
        f"sudo talaria hermes-plugin --home {home} --manage-updates"
        ' --hermes-command "$(command -v hermes)"',
    ]


@pytest.mark.parametrize("version", [True, 2, "1", None])
def test_unknown_plugin_protocol_disables_extensions_and_preserves_sessions(live_app, version):
    url, peer, _ = live_app
    peer.extension = {"version": version, "context_runs": True, "rewind": True}
    with signed_in(url) as client:
        assert client.get("/api/capabilities").json()["talaria_extensions"] == {}
        assert client.get("/api/sessions").status_code == 200


def test_connection_detects_releases_and_offers_native_installation(page, live_app):
    peer = live_app[1]
    page.get_by_role("button", name="Settings").click()
    page.get_by_role("tab", name="Connection", exact=True).click()
    link = page.get_by_role("link", name="Server installation instructions")
    install_url = "https://github.com/liamsmith86/talaria-webui#hermes-plugin"
    expect(link).to_have_attribute("href", install_url)
    # Opening settings also reads the plugin, and would overwrite a newer check.
    wait_for_store(page, "s => !!s.agentInfo")
    peer.extension = {"plugin_version": "1.0.0", "revision": "b" * 64}
    page.get_by_role("button", name="Check again", exact=True).click()
    expect(page.get_by_role("tabpanel")).to_contain_text("Plugin update available")
    expect(link).to_have_attribute("href", install_url)
    peer.extension = {"plugin_version": "999.0.0"}
    page.get_by_role("button", name="Check again", exact=True).click()
    expect(page.get_by_role("tabpanel")).to_contain_text("Plugin is newer")
    expect(link).to_have_count(0)
    # An update waiting for a restart is judged by the files the restart loads.
    current = {"plugin_version": PLUGIN_VERSION, "revision": LOADED_REVISION}
    peer.extension = {"plugin_version": "1.0.0", "state": "restart_required", "installed": current}
    page.get_by_role("button", name="Check again", exact=True).click()
    expect(page.get_by_role("tabpanel")).to_contain_text("Restart the Hermes gateway")
    expect(page.get_by_role("tabpanel")).to_contain_text("Up to date")
    expect(link).to_have_count(0)
    # A plugin that matches this release but not its Hermes needs a newer Talaria.
    peer.extension = {**current, "state": "unsupported_hermes"}
    page.get_by_role("button", name="Check again", exact=True).click()
    expect(page.get_by_role("tabpanel")).to_contain_text("can't run on this Hermes version")
    expect(page.get_by_role("tabpanel")).to_contain_text("Check for a Talaria update")
    peer.extension = current
    page.get_by_role("button", name="Check again", exact=True).click()
    expect(page.get_by_role("tabpanel")).to_contain_text("Up to date")
    expect(page.get_by_role("tabpanel")).not_to_contain_text("Hermes version")
    # Hermes refuses a plugin that requires a newer Hermes, so installing it would not help.
    peer.extension, peer.version = None, "0.1.0"
    page.get_by_role("button", name="Check again", exact=True).click()
    expect(page.get_by_role("tabpanel")).to_contain_text(
        f"This Hermes is 0.1.0. The Talaria plugin needs Hermes {HERMES_MINIMUM} or later"
    )
    expect(link).to_have_count(0)


@pytest.mark.hermes
@pytest.mark.skipif(
    not os.getenv("HERMES_SOURCE"), reason="Set HERMES_SOURCE for native validation"
)
def test_native_plugin_validation(tmp_path):
    source = Path(os.environ["HERMES_SOURCE"])
    plugin = Path(plugin_status.__file__).with_name("hermes_plugin")
    result = subprocess.run(
        [
            str(source / "venv/bin/python3"),
            "-m",
            "hermes_cli.main",
            "plugins",
            "doctor",
            str(plugin),
            "--ci",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "HERMES_HOME": str(tmp_path),
            "PYTHONPATH": str(source),
            "HERMES_ENABLE_PROJECT_PLUGINS": "false",
        },
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ERROR:" not in result.stdout and "WARN:" not in result.stdout, result.stdout

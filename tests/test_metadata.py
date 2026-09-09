import json
import os

import httpx
import pytest

from talaria.hermes_plugin import identity as files

from .fake_hermes import KEY
from .test_app import signed_in


@pytest.mark.parametrize(
    ("content", "name"),
    [
        ("You are Juniper, a personal agent.\nPrivate instructions follow.", "Juniper"),
        ("# Identity\n- **Name:** Juniper\n- Description: private", "Juniper"),
        ("Name: Renée O’Neil", "Renée O’Neil"),
        ("You are an AI assistant.", ""),
        ("# New identity format\nagent_name = 'Juniper'", ""),
        ("Name: <script>alert(1)</script>", ""),
    ],
)
def test_supported_identity_formats(tmp_path, content, name):
    (tmp_path / "SOUL.md").write_text(content)
    details = files.inspect_home(str(tmp_path))
    assert details.name == name
    assert details.status == ("ready" if name else "no_name")
    assert "Private instructions" not in json.dumps(details.public())


@pytest.mark.parametrize("kind", ["oversized", "binary", "directory", "symlink", "fifo"])
def test_unsupported_identity_files_fall_back(tmp_path, kind):
    path = tmp_path / "IDENTITY.md"
    if kind == "oversized":
        path.write_text("Name: Hidden\n" + "x" * files.MAX_BYTES)
    elif kind == "binary":
        path.write_bytes(b"Name: Hidden\xff")
    elif kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "private.md"
        target.write_text("Name: Hidden")
        path.symlink_to(target)
    else:
        os.mkfifo(path)
    assert files.inspect_home(str(tmp_path)).name == ""
    (tmp_path / "SOUL.md").write_text("You are Juniper, an agent.")
    assert files.inspect_home(str(tmp_path)).name == "Juniper"


def test_unreadable_directory_is_optional(tmp_path, monkeypatch):
    assert files.inspect_home("").status == "disabled"
    assert files.inspect_home(str(tmp_path / "missing")).status == "not_found"

    def denied(path):
        raise PermissionError("Private server details")

    monkeypatch.setattr(files, "_read", denied)
    details = files.inspect_home(str(tmp_path))
    assert details.status == "unreadable" and not details.name
    assert "Private server details" not in json.dumps(details.public())


def test_extended_access_is_private_and_optional(live_app):
    url, peer, app = live_app
    assert httpx.get(url + "/api/agent").status_code == 401
    assert httpx.get(url + "/api/bootstrap").json()["agent"] is None
    with signed_in(url) as client:
        assert client.get("/api/capabilities").json()["talaria_extensions"] == {}
        assert client.put("/api/hermes-access", json={"path": "/root/.hermes"}).status_code == 404
        peer.extension = {"agent": {"name": "Juniper", "secret": KEY}, "private": KEY}
        caps = client.get("/api/capabilities")
        assert caps.json()["talaria_agent"] == {"name": "Juniper", "name_source": "extension"}
        assert KEY not in caps.text
        assert client.get("/api/bootstrap").json()["agent"]["name"] == "Juniper"
        app.state.capabilities["agent"] = {"name": "API identity"}
        assert client.get("/api/agent").json()["name"] == "API identity"
        del app.state.capabilities["agent"]
        peer.extension = None
        assert client.get("/api/agent").json()["name"] == "Hermes"
        assert client.get("/api/sessions").status_code == 200


def test_agent_information_filters_private_fields_and_tolerates_drift(live_app):
    url, peer, _ = live_app
    with signed_in(url) as client:
        client.get("/api/capabilities")
        response = client.get("/api/agent")
        assert response.status_code == 200
        info = response.json()
        assert info["version"] == "1.2.3" and "active_agents" not in info
        assert info["platforms"] == [{"name": "api_server", "state": "connected"}]
        assert info["skills"][0]["name"] == "project-notes"
        assert info["toolsets"][0]["tools"] == ["read_file", "write_file"]
        assert KEY not in response.text and "pid" not in response.text
        peer.discovery_overrides.update(
            {
                "/health/detailed": (
                    {"status": "ok", "version": ["changed"], "platforms": []},
                    200,
                ),
                "/v1/skills": ({"new_envelope": []}, 200),
                "/v1/toolsets": ({"error": "Private failure"}, 503),
            }
        )
        info = client.get("/api/agent").json()
        assert info["version"] == "" and info["platforms"] == []
        assert info["skills"] == [] and info["toolsets"] == []
        assert info["available"] == {"health": True, "skills": False, "toolsets": False}
        peer.discovery_overrides["/health/detailed"] = (None, 200)
        assert client.get("/api/agent").json()["available"]["health"] is False
        assert client.get("/api/sessions").status_code == 200

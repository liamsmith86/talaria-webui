import json
import os

import httpx
import pytest

from talaria.hermes_access import files

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


def test_extended_access_is_private_testable_and_optional(live_app, tmp_path):
    url, _, app = live_app
    home = tmp_path / "hermes"
    home.mkdir()
    soul = home / "SOUL.md"
    soul.write_text("You are Juniper, an agent.\nPrivate identity instructions.")
    (home / "config.yaml").write_text("secret: never-expose-this")
    (home / ".env").write_text("API_KEY=never-expose-this")
    assert httpx.get(url + "/api/hermes-access").status_code == 401
    assert httpx.get(url + "/api/agent").status_code == 401
    assert httpx.get(url + "/api/bootstrap").json()["agent"] is None
    with signed_in(url) as client:
        assert client.get("/api/hermes-access").json()["status"] == "disabled"
        tested = client.post("/api/hermes-access/test", json={"path": str(home / "config.yaml")})
        assert tested.json()["path"] == str(home)
        assert tested.json()["name"] == "Juniper"
        assert not app.state.settings.hermes_home
        assert not app.state.config_path.exists()
        assert (
            client.put(
                "/api/hermes-access", json={"path": str(home)}, headers={"X-CSRF-Token": "bad"}
            ).status_code
            == 403
        )
        for path in ("relative/path", ["invalid"], "path\nwith-control"):
            assert client.put("/api/hermes-access", json={"path": path}).status_code == 400
        response = client.put("/api/hermes-access", json={"path": str(home)})
        assert response.json()["agent"] == {"name": "Juniper", "name_source": "files"}
        assert app.state.config_path.stat().st_mode & 0o077 == 0
        assert json.loads(app.state.config_path.read_text())["hermes_home"] == str(home)
        assert client.get("/api/bootstrap").json()["agent"]["name"] == "Juniper"
        assert client.get("/api/capabilities").json()["talaria_agent"]["name"] == "Juniper"
        for path in ("/api/hermes-access", "/api/agent", "/api/bootstrap"):
            body = client.get(path).text
            assert "Private identity instructions" not in body
            assert "never-expose-this" not in body
            assert KEY not in body
        app.state.capabilities["agent"] = {"name": "API identity"}
        assert client.get("/api/agent").json()["name"] == "API identity"
        del app.state.capabilities["agent"]
        soul.write_text("New upstream format without a supported name")
        info = client.get("/api/agent").json()
        assert info["name"] == "Hermes" and info["extended_access"]["status"] == "no_name"
        soul.unlink()
        assert client.get("/api/bootstrap").json()["agent"]["name"] == "Hermes"
        assert client.get("/api/sessions").status_code == 200
        assert client.put("/api/hermes-access", json={"path": ""}).json()["status"] == "disabled"
        assert not app.state.settings.hermes_home


def test_agent_information_filters_private_fields_and_tolerates_drift(live_app):
    url, peer, _ = live_app
    with signed_in(url) as client:
        client.get("/api/capabilities")
        response = client.get("/api/agent")
        assert response.status_code == 200
        info = response.json()
        assert info["version"] == "1.2.3" and info["active_agents"] == 0
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

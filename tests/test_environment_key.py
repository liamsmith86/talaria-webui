"""Runtime credentials must not become persisted or cross profile boundaries."""

import json
import os
import pwd
from types import SimpleNamespace

import httpx
import pytest

from talaria import auth, config, supervisor
from talaria.app import create_app


@pytest.mark.parametrize("saved_key", ["", "saved-fallback"])
async def test_environment_key_survives_connection_saves_without_persisting(
    tmp_path, monkeypatch, saved_key
):
    secret = "environment-only-test-key"
    monkeypatch.setenv("TALARIA_HERMES_API_KEY", secret)
    headers = []

    def upstream(request):
        headers.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"platform": "hermes-agent"})

    path = tmp_path / "config.json"
    settings = config.Settings(api_key=saved_key, signing_key="test-only")
    config.save(path, settings)
    app = create_app(settings, path, transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(
        base_url="http://talaria.test", transport=httpx.ASGITransport(app=app)
    ) as client:
        client.cookies.set(auth.COOKIE, auth.issue_cookie(settings))
        client.headers["X-Talaria-Request"] = "1"
        bootstrap = (await client.get("/api/bootstrap")).json()
        assert bootstrap["connected"]
        client.headers["X-CSRF-Token"] = bootstrap["csrf"]
        connection = await client.get("/api/connection")
        assert connection.json()["key_from_env"] and secret not in connection.text
        for method, endpoint in (("POST", "test"), ("PUT", "")):
            response = await client.request(
                method,
                "/api/connection" + ("/test" if endpoint else ""),
                json={"url": settings.hermes_url},
            )
            assert response.status_code == 200, response.text
            assert secret not in response.text
        assert headers == ["Bearer " + secret] * 2
        assert json.loads(path.read_text())["api_key"] == saved_key
        assert secret not in path.read_text()
        for body in (
            {"url": "http://different.test"},
            {"url": settings.hermes_url, "api_key": "replacement"},
        ):
            response = await client.put("/api/connection", json=body)
            assert response.status_code == 409
        assert len(headers) == 2  # Never forward the injected key to another address.
        profile_id = "a" * 32
        app.state.profiles.records[profile_id] = {
            "id": profile_id,
            "label": "Other",
            "url": "http://other.test",
            "api_key": "separate-profile-key",
        }
        other = app.state.profiles.resolve(profile_id)
        assert not other.state.key_from_env
        assert other.state.hermes.client.headers["authorization"] == "Bearer separate-profile-key"
    await app.state.profiles.close()
    monkeypatch.delenv("TALARIA_HERMES_API_KEY")
    fallback = create_app(config.load(path), path)
    assert fallback.state.hermes_key == saved_key
    await fallback.state.profiles.close()


@pytest.mark.parametrize("value", ["", "with space", "bad\nheader", "clé", "x" * 4097])
def test_invalid_environment_keys_fail_without_disclosing_the_value(tmp_path, monkeypatch, value):
    monkeypatch.setenv("TALARIA_HERMES_API_KEY", value)
    with pytest.raises(ValueError, match="TALARIA_HERMES_API_KEY must contain"):
        create_app(config.Settings(), tmp_path / "config.json")


def test_root_launcher_forwards_only_the_intended_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        pwd,
        "getpwuid",
        lambda _: SimpleNamespace(
            pw_uid=1234,
            pw_gid=1234,
            pw_name="talaria-test",
            pw_shell="/usr/sbin/nologin",
            pw_dir=str(tmp_path),
        ),
    )
    monkeypatch.setattr(supervisor, "check_system_directory", lambda _: None)
    monkeypatch.setenv("TALARIA_HERMES_API_KEY", "injected-test-key")
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "unrelated-manager-token")
    path = tmp_path / "config.json"
    path.write_text("{}")
    identity, env = supervisor.web_identity(tmp_path, path)
    assert identity["user"] == 1234
    assert env["TALARIA_HERMES_API_KEY"] == "injected-test-key"
    assert "BWS_ACCESS_TOKEN" not in env


def test_connection_dialog_explains_environment_management(page, live_app):
    from playwright.sync_api import expect

    app = live_app[2]
    # The fixture's existing synthetic credential is now managed by the environment.
    app.state.key_from_env = True
    page.get_by_role("button", name="Settings").click()
    page.get_by_role("tab", name="Connection", exact=True).click()
    expect(page.get_by_label("API key", exact=True)).to_be_disabled()
    expect(page.get_by_label("API key", exact=True)).to_have_value("")
    expect(
        page.get_by_text("Using TALARIA_HERMES_API_KEY from the server environment.")
    ).to_be_visible()
    page.get_by_role("button", name="Test connection", exact=True).click()
    expect(page.get_by_text("Hermes connection verified.", exact=True)).to_be_visible()

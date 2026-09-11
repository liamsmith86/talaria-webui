import json
from dataclasses import replace

import httpx
from playwright.sync_api import expect

from talaria import installation
from talaria.app import create_app

from .conftest import serve
from .test_app import signed_in


def test_dev_and_production_cookies_coexist_on_the_same_hostname(live_app, tmp_path):
    production, _, app = live_app
    settings = replace(app.state.settings, signing_key="separate-development-key")
    dev = create_app(settings, tmp_path / "dev.json", development=True)
    server, thread, development = serve(dev)
    try:
        with httpx.Client(headers={"X-Talaria-Request": "1"}) as browser:
            for url in (production, development):
                assert (
                    browser.post(url + "/api/login", json={"password": "test-password"}).status_code
                    == 200
                )
            assert set(browser.cookies.keys()) == {"talaria_session", "talaria_dev_session"}
            prod = browser.get(production + "/api/bootstrap").json()
            test = browser.get(development + "/api/bootstrap").json()
            assert prod["authenticated"] and test["authenticated"]
            assert prod["environment"] == "production" and test["environment"] == "development"
            assert prod["csrf"] != test["csrf"]
            assert (
                browser.post(
                    development + "/api/logout", headers={"X-CSRF-Token": prod["csrf"]}
                ).status_code
                == 403
            )
            assert (
                browser.post(
                    development + "/api/logout", headers={"X-CSRF-Token": test["csrf"]}
                ).status_code
                == 200
            )
            assert browser.get(production + "/api/bootstrap").json()["authenticated"]
            assert not browser.get(development + "/api/bootstrap").json()["authenticated"]
    finally:
        server.should_exit = True
        thread.join(6)
        assert not thread.is_alive()


def test_installation_info_is_authenticated_and_contains_no_private_paths(
    live_app, tmp_path, monkeypatch
):
    root = tmp_path / "deployment"
    root.mkdir()
    (root / "deployment.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "branch": "main",
                "scope": "system",
                "config": "/private/config.json",
                "repository": "private-remote",
                "api_key": "never-emit",
            }
        )
    )
    (root / "update.json").write_text(json.dumps({"available": True, "latest_commit": "b" * 40}))
    monkeypatch.setattr(installation, "managed_root", lambda: root)
    assert httpx.get(live_app[0] + "/api/installation").status_code == 401
    with signed_in(live_app[0]) as client:
        response = client.get("/api/installation")
        assert response.status_code == 200
        data = response.json()
        assert data["managed"] and data["update"]["available"]
        assert "update_command" not in data
        assert data["can_update"] is False
        for private in (str(root), "/private", "private-remote", "never-emit"):
            assert private not in response.text
        (root / "deployment.json").write_text("invalid json")
        assert client.get("/api/installation").status_code == 200


def test_oversized_or_unrecognized_build_metadata_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "installation.py"
    monkeypatch.setattr(installation, "__file__", str(path))
    build = tmp_path / "_build.json"
    try:
        for value in (
            '{"commit": []}',
            '{"commit": "invalid"}',
            '{"commit": "' + "a" * 20000 + '"}',
        ):
            build.write_text(value)
            installation.build_info.cache_clear()
            assert installation.build_info()["commit"] is None
    finally:
        installation.build_info.cache_clear()


def test_development_is_identifiable_on_desktop_mobile_and_login(page, live_app):
    live_app[2].state.development = True
    page.reload()
    expect(page).to_have_title("Talaria · Dev")
    expect(page.locator(".sidebar-brand .environment-badge")).to_have_text("Dev")
    page.get_by_role("button", name="Settings").click()
    page.get_by_role("tab", name="Talaria", exact=True).click()
    expect(page.get_by_role("tabpanel")).to_contain_text("Development")
    expect(page.get_by_role("tabpanel")).to_contain_text("Working checkout")
    page.get_by_role("button", name="Close dialog").click()
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator(".topbar .environment-badge")).to_have_text("Dev")
    page.context.clear_cookies()
    page.reload()
    expect(page.locator(".login-brand .environment-badge")).to_have_text("Dev")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

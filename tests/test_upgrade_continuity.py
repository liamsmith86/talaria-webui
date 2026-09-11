"""An existing browser document survives an actual service restart on the same port."""

import shutil
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import expect

from talaria import app as application
from talaria.auth import hash_password
from talaria.config import Settings

from .conftest import capture_browser_errors, serve, wait_for_store
from .fake_hermes import KEY, FakeHermes
from .test_conversation_features import seed
from .test_session_continuity import open_session


def test_old_tab_keeps_draft_and_auth_across_service_cutover(browser, tmp_path, monkeypatch):
    peer = FakeHermes()
    upstream, upstream_thread, native_url = serve(peer.app)
    settings = Settings(
        hermes_url=native_url,
        api_key=KEY,
        password_hash=hash_password("test-password"),
        signing_key="fixture-key",
    )
    old = application.create_app(settings, tmp_path / "config.json")
    server, thread, address = serve(old)
    context = browser.new_context()
    errors = capture_browser_errors(context)
    try:
        page = context.new_page()
        page.goto(address)
        page.get_by_label("Password", exact=True).fill("test-password")
        page.get_by_role("button", name="Sign in").click()
        wait_for_store(page, "s => s.connected && s.defaultModel")
        sid = seed(peer)
        open_session(page, sid)
        composer = page.get_by_label("Message Hermes")
        composer.fill("Draft survives the update")
        page.evaluate("window.originalDocument = true")
        # Different immutable asset bytes, same compatible public interfaces.
        assets = tmp_path / "release/static"
        shutil.copytree(application.STATIC, assets)
        with (assets / "lib.js").open("a") as file:
            file.write('\nwindow.releaseProbe = "replacement";\n')
        monkeypatch.setattr(application, "STATIC", assets)
        replacement = application.create_app(settings, tmp_path / "config.json")
        server.should_exit = True
        thread.join(6)
        assert not thread.is_alive()
        server, thread, new_address = serve(replacement, port=urlsplit(address).port)
        assert new_address == address
        probe = httpx.get(address + "/static/lib.js")
        assert "window.releaseProbe" in probe.text
        peer.messages[sid].append({"id": 2, "role": "assistant", "content": "External update"})
        page.evaluate("dispatchEvent(new Event('focus'))")
        expect(page.locator(".message.assistant")).to_contain_text("External update")
        expect(composer).to_have_value("Draft survives the update")
        assert page.evaluate("window.originalDocument && !window.releaseProbe")
        composer.press("Enter")
        wait_for_store(page, "s => s.lives[s.active]?.persisted === true")
        assert len(peer.runs) == 1
        assert next(iter(peer.runs.values()))["input"] == "Draft survives the update"
        composer.fill("Next unsent draft")
        page.reload()
        expect(composer).to_have_value("Next unsent draft")
        assert page.evaluate("window.releaseProbe") == "replacement"
        assert page.evaluate("window.originalDocument === undefined")
        wait_for_store(page, 's => s.connected && s.active === "notes"')
        assert not errors, errors
    finally:
        context.close()
        server.should_exit = True
        thread.join(6)
        upstream.should_exit = True
        upstream_thread.join(6)
        assert not thread.is_alive() and not upstream_thread.is_alive()

from dataclasses import replace
from pathlib import Path

import pytest
from playwright.sync_api import expect

from talaria import app as application
from talaria.auth import issue_cookie

from . import test_browser as browser_cases
from . import test_demo_browser as demo_cases
from .conftest import browser_context, serve, wait_for_store

demo_page = demo_cases.demo_page
test_chat_and_settings = browser_cases.test_chat_and_settings
test_approval_and_reconnect = browser_cases.test_approval_and_reconnect
test_mobile = browser_cases.test_mobile
test_demo_streaming = (
    demo_cases.test_demo_native_chat_streams_settles_and_stops_without_duplicate_turns
)
test_demo_tabs = demo_cases.test_demo_new_session_is_local_and_other_tabs_keep_their_samples
test_demo_settings = demo_cases.test_demo_uses_real_readonly_settings_and_search

pytestmark = pytest.mark.production


def test_startup_delivery(page):
    resources = page.evaluate("""() => performance.getEntriesByType('resource').map((r) => ({
      path: new URL(r.name).pathname, bytes: r.encodedBodySize,
    }))""")
    scripts = [entry for entry in resources if entry["path"].endswith(".js")]
    assert len(scripts) == 3
    assert sum(entry["bytes"] for entry in scripts) < 106 * 1024
    assert len([entry for entry in resources if entry["path"].endswith(".css")]) == 1
    assert page.locator('script[type="module"]').get_attribute("data-store-entry") == "app.js"


def test_rendering_matches_source(browser, live_app, tmp_path, monkeypatch):
    production, peer, app = live_app
    peer.sessions["visual"] = {"id": "visual", "title": "Visual parity", "source": "api_server"}
    peer.messages["visual"] = [
        {"id": 1, "role": "user", "content": "A table and some code"},
        {
            "id": 2,
            "role": "assistant",
            "content": "## Example\n\nA **bold** and *italic* reply.\n\n"
            "| Item | Value |\n| --- | --- |\n| One | Two |\n\n"
            "```python\nanswer = 42\n```\n\n<script>window.injected = true</script>",
        },
    ]
    with monkeypatch.context() as patch:
        patch.setattr(
            application, "STATIC", Path(__file__).resolve().parents[1] / "src/talaria/static"
        )
        source_app = application.create_app(app.state.settings, tmp_path / "source.json")
    server, thread, source = serve(source_app)
    images = []
    try:
        for url in (source, production):
            with browser_context(browser) as context:
                context.add_cookies(
                    [
                        {
                            "name": app.state.cookie_name,
                            "value": issue_cookie(app.state.settings),
                            "url": url,
                        }
                    ]
                )
                tab = context.new_page()
                tab.set_viewport_size({"width": 1440, "height": 960})
                tab.goto(url)
                wait_for_store(tab, "s => !!s.defaultModel && s.readiness.status === 'ok'")
                tab.get_by_role("link", name="Visual parity", exact=True).click()
                expect(tab.locator(".markdown .token").first).to_be_attached()
                assert tab.evaluate("window.injected") is None
                tab.evaluate("() => document.fonts.ready")
                tab.evaluate(
                    "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
                )
                images.append(tab.screenshot(animations="disabled", caret="hide"))
        if images[0] != images[1]:
            for name, data in zip(("source", "production"), images, strict=True):
                (tmp_path / f"{name}.png").write_bytes(data)
        assert images[0] == images[1], f"Rendering differs; captures are in {tmp_path}"
    finally:
        server.should_exit = True
        thread.join(6)


def test_language_catalog_in_production(page):
    page.get_by_role("button", name="Settings").click()
    page.get_by_role("tab", name="Appearance", exact=True).click()
    page.get_by_role("combobox", name="Language", exact=True).select_option("de")
    expect(page.locator("html")).to_have_attribute("lang", "de")
    expect(page.get_by_role("dialog")).to_contain_text("Darstellung")
    page.reload()
    expect(page.locator("html")).to_have_attribute("lang", "de")


def test_bootstrap_recovers_from_a_missing_app(browser, live_app):
    url, _, app = live_app
    context = browser.new_context()
    context.add_cookies(
        [{"name": app.state.cookie_name, "value": issue_cookie(app.state.settings), "url": url}]
    )
    tab = context.new_page()
    tab.route("**/app.js", lambda route: route.abort())
    try:
        tab.goto(url)
        expect(tab.get_by_role("status")).to_have_text("Could not load Talaria.")
        expect(tab.get_by_role("link", name="Reload Talaria")).to_be_visible()
    finally:
        context.close()


def test_subpath_navigation_and_submission(browser, live_app, tmp_path):
    settings = replace(live_app[2].state.settings, public_url="http://127.0.0.1/apps/talaria")
    app = application.create_app(settings, tmp_path / "subpath.json", immutable_assets=True)
    server, thread, url = serve(app)
    settings.public_url = url + "/apps/talaria"
    try:
        with browser_context(browser) as context:
            context.add_cookies(
                [{"name": app.state.cookie_name, "value": issue_cookie(settings), "url": url}]
            )
            page = context.new_page()
            page.goto(settings.public_url + "/")
            wait_for_store(page, "s => !!s.defaultModel && s.readiness.status === 'ok'")
            page.get_by_label("Message Hermes").fill("Check the production subpath")
            page.get_by_role("button", name="Send message", exact=True).click()
            expect(page.get_by_role("heading", name="A thoughtful place to start")).to_be_visible()
            expect(page.get_by_role("button", name="Stop response")).to_have_count(0)
            page.reload()
            expect(page.get_by_role("heading", name="A thoughtful place to start")).to_be_visible()
    finally:
        server.should_exit = True
        thread.join(6)

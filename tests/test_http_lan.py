"""Core browser actions must also work on ordinary HTTP LAN origins."""

import re
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from .conftest import capture_browser_errors, wait_for_store
from .test_commands import CATALOG
from .test_conversation_features import png


@pytest.mark.parametrize("hostname", ["127.0.0.1", "talaria.test"])
def test_http_origin_supports_dialogs_images_messages_and_commands(browser, live_app, hostname):
    address, peer, _ = live_app
    origin = address.replace("127.0.0.1", hostname)
    catalog = [*CATALOG, {"name": "help", "aliases": [], "mode": "web", "description": "Commands"}]
    peer.extension = {"commands": True}
    peer.discovery_overrides["/talaria/v1/commands"] = ({"commands": catalog}, 200)
    context = browser.new_context(viewport={"width": 1440, "height": 960})
    errors = capture_browser_errors(context)

    def local_network(route):
        # Remap transport only. The browser retains its real origin/security
        # context, and Talaria receives that origin's Host and CSRF headers.
        response = route.fetch(
            url=route.request.url.replace(origin, address, 1),
            headers={**route.request.headers, "host": urlsplit(origin).netloc},
        )
        route.fulfill(response=response)

    context.route(origin + "/**", local_network)
    try:
        page = context.new_page()
        page.goto(origin)
        assert page.evaluate("isSecureContext") is (hostname == "127.0.0.1")
        if hostname != "127.0.0.1":
            assert page.evaluate("typeof crypto.randomUUID") == "undefined"
        page.get_by_label("Password", exact=True).fill("test-password")
        page.get_by_role("button", name="Sign in", exact=True).click()
        page.locator(".topbar-title").wait_for()
        wait_for_store(page, "state => !!state.defaultModel && state.readiness.status === 'ok'")
        page.get_by_role("button", name=re.compile(r"^Settings\b")).click()
        expect(page.get_by_role("dialog", name="Settings", exact=True)).to_be_visible()
        page.get_by_role("button", name="Close dialog", exact=True).click()

        composer = page.get_by_label("Message Hermes")
        page.get_by_label("File attachment").set_input_files(
            {"name": "lan.png", "mimeType": "image/png", "buffer": png()}
        )
        expect(page.get_by_role("button", name="Remove lan.png", exact=True)).to_be_visible()
        composer.fill("A message from a LAN browser")
        page.get_by_role("button", name="Send message", exact=True).click()
        expect(page.locator(".message.assistant")).to_contain_text(
            "What would you like to explore next?"
        )
        expect(page.get_by_role("button", name="Stop response", exact=True)).to_have_count(0)
        assert len(peer.runs) == 1
        page.reload()
        expect(page.locator(".message.assistant")).to_contain_text(
            "What would you like to explore next?"
        )
        assert len(peer.runs) == 1

        posted = []

        def command(route):
            if route.request.method == "GET":
                route.fulfill(json={"commands": catalog})
            else:
                posted.append(route.request.post_data_json)
                route.fulfill(json={"status": "running"})

        page.route("**/api/commands", command)
        page.route(
            "**/api/commands/*",
            lambda route: route.fulfill(
                json={
                    "status": "completed",
                    "session_id": next(iter(peer.sessions)),
                    "text": "Compressed.",
                }
            ),
        )
        composer.fill("/help")
        expect(page.get_by_role("option", name="/help Commands")).to_be_visible()
        page.get_by_role("button", name="Send message", exact=True).click()
        expect(page.locator(".command-card")).to_be_visible()
        expect(composer).to_have_value("")
        composer.fill("/compress")
        expect(page.get_by_role("option").first).to_contain_text("/compress")
        page.get_by_role("button", name="Send message", exact=True).click()
        expect(page.locator(".command-card")).to_contain_text("Compressed.")
        assert len(posted) == 1
        assert len(peer.runs) == 1
        assert not errors
    finally:
        context.unroute_all(behavior="wait")
        context.close()

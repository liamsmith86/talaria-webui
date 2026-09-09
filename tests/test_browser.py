from pathlib import Path

from playwright.sync_api import expect


def screenshot(page, name):
    Path("test-results").mkdir(exist_ok=True)
    page.evaluate("document.fonts.ready")
    page.screenshot(path=f"test-results/{name}.png", full_page=True, animations="disabled")


def test_chat_and_settings(page, live_app):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    screenshot(page, "desktop-welcome")
    page.get_by_label("Message Hermes").fill("Help me plan a thoughtful interface")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("heading", name="A thoughtful place to start")).to_be_visible()
    expect(page.get_by_role("button", name="Stop response")).to_have_count(0)
    screenshot(page, "desktop-conversation")
    assert len(live_app[1].runs) == 1
    page.reload()
    expect(page.get_by_role("heading", name="A thoughtful place to start")).to_be_visible()
    page.get_by_role("button", name="Your space").click()
    page.get_by_role("button", name="Dark", exact=True).click()
    page.get_by_role("button", name="Sage", exact=True).click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    screenshot(page, "dark-settings")
    page.get_by_role("button", name="Close dialog").click()
    screenshot(page, "dark-conversation")
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-palette", "sage")
    assert not errors


def test_approval_and_reconnect(page, live_app):
    page.get_by_label("Message Hermes").fill("Please test approval")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("button", name="Allow once")).to_be_visible()
    screenshot(page, "approval")
    page.reload()
    expect(page.get_by_role("button", name="Allow once")).to_be_visible()
    page.get_by_role("button", name="Allow once").click()
    expect(page.get_by_role("heading", name="A thoughtful place to start")).to_be_visible()
    assert live_app[1].approvals == 1
    assert len(live_app[1].runs) == 1


def test_mobile(page):
    page.set_viewport_size({"width": 390, "height": 844})
    screenshot(page, "mobile-welcome")
    page.get_by_role("button", name="Open sidebar").click()
    page.get_by_role("button", name="Your space").click()
    screenshot(page, "mobile-settings")
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_role("button", name="Close sidebar", exact=True).first.click()
    expect(page.get_by_label("Message Hermes")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

"""The demo uses normal chat controls; visitor text stays in its browser tab."""

import pytest
from playwright.sync_api import expect

from talaria.demo import create_demo

from .conftest import capture_browser_errors, serve


@pytest.fixture
def demo_page(browser):
    server, thread, url = serve(create_demo())
    context = browser.new_context(viewport={"width": 1440, "height": 960})
    errors, writes = capture_browser_errors(context), []
    context.on("request", lambda req: writes.append(req.url) if req.method != "GET" else None)
    page = context.new_page()
    try:
        page.goto(url)
        expect(page.get_by_role("heading", name="New session")).to_be_visible()
        yield page
        assert not errors
        assert not writes, f"Demo sent mutation requests: {writes}"
    finally:
        context.close()
        server.should_exit = True
        thread.join(6)
        assert not thread.is_alive()


def send(page, text):
    page.get_by_role("textbox", name="Message Hermes").fill(text)
    page.get_by_role("button", name="Send message", exact=True).click()


def test_demo_native_chat_streams_settles_and_stops_without_duplicate_turns(demo_page):
    page = demo_page
    page.get_by_role("link", name="Build a small reading list").click()
    content = page.locator(".conversation-content")
    expect(content).to_contain_text("Both checks passed")
    send(page, "Can you show me another example?")
    expect(page.get_by_role("button", name="Stop response", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Send message", exact=True)).to_be_visible(timeout=20000)
    expect(page.locator(".message.assistant")).to_have_count(2)
    expect(page.locator(".message.user")).to_have_count(2)
    expect(content).to_contain_text("Can you show me another example?")
    page.get_by_role("button", name="Response details", exact=True).last.click()
    expect(page.get_by_role("dialog")).to_contain_text("claude-sonnet-4-6")
    expect(page.get_by_role("dialog")).not_to_contain_text("Scripted")
    page.get_by_role("button", name="Close dialog", exact=True).click()
    send(page, "One more please")
    page.get_by_role("button", name="Stop response", exact=True).click()
    expect(page.get_by_role("button", name="Send message", exact=True)).to_be_visible()
    expect(page.locator(".message.user")).to_have_count(3)
    page.get_by_role("link", name="A weekend outside").click()
    page.get_by_role("link", name="Build a small reading list").click()
    expect(page.locator(".message.user")).to_have_count(3)
    expect(page.get_by_role("button", name="Regenerate response", exact=True).first).to_be_visible()
    page.reload()
    expect(page.locator(".message.user")).to_have_count(1)
    expect(content).not_to_contain_text("Can you show me another example?")


def test_demo_new_session_is_local_and_other_tabs_keep_their_samples(demo_page):
    page = demo_page
    other = page.context.new_page()
    other.goto(page.url)
    send(page, "A private throwaway demo prompt")
    expect(page.get_by_role("button", name="Stop response", exact=True)).to_be_visible()
    expect(other.get_by_role("heading", name="New session")).to_be_visible()
    expect(other.get_by_role("navigation", name="Session history")).not_to_contain_text(
        "A private throwaway"
    )
    expect(page.get_by_role("button", name="Send message", exact=True)).to_be_visible(timeout=20000)
    expect(page.locator(".message.assistant")).to_have_count(1)
    page.reload()
    expect(page.get_by_role("heading", name="New session")).to_be_visible()
    expect(page.locator(".message")).to_have_count(0)


@pytest.mark.parametrize("width", [390, 1440])
def test_demo_uses_real_readonly_settings_and_search(demo_page, width):
    page = demo_page
    page.set_viewport_size({"width": width, "height": 844})
    if width == 390:
        page.get_by_role("button", name="Open sidebar", exact=True).click()
    page.get_by_role("button", name="Settings Connected to Hermes").click()
    dialog = page.get_by_role("dialog", name="Settings", exact=True)
    expect(dialog).not_to_contain_text("scripted demo", ignore_case=True)
    page.get_by_role("tab", name="Connection", exact=True).click()
    address = dialog.get_by_role("textbox", name="Hermes address")
    expect(address).to_have_value("http://127.0.0.1:8642")
    expect(address).not_to_be_editable()
    expect(dialog.get_by_label("API key", exact=True)).not_to_be_editable()
    expect(dialog.get_by_role("button", name="Add profile", exact=True)).to_be_disabled()
    expect(dialog.get_by_role("button", name="Save connection", exact=True)).to_be_disabled()
    expect(dialog.get_by_role("button", name="Test connection", exact=True)).to_be_disabled()
    expect(dialog).to_contain_text("Connected · Talaria plugin")
    page.get_by_role("tab", name="Talaria", exact=True).click()
    expect(dialog).to_contain_text("Up to date")
    expect(dialog.get_by_role("button", name="Check for updates", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Sign out", exact=True)).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("button", name="Close dialog", exact=True).click()
    page.get_by_role("textbox", name="Search sessions").fill("standard library")
    hit = page.locator(".history-search-hit").first
    expect(hit.locator("mark")).to_contain_text("standard library")
    hit.click()
    expect(page.locator(".message.search-match")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_demo_attachments_and_downloads_stay_local(demo_page):
    import json
    from pathlib import Path

    from .test_conversation_features import png

    page = demo_page
    page.get_by_label("File attachment").set_input_files(
        {"name": "sample.png", "mimeType": "image/png", "buffer": png()}
    )
    expect(page.locator(".attachment-chip")).to_have_count(1)
    send(page, "Describe this sample")
    expect(page.get_by_role("button", name="Stop response", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Send message", exact=True)).to_be_visible(timeout=20000)
    expect(page.locator(".message.user img")).to_have_count(1)
    page.get_by_role("button", name="Session options", exact=True).click()
    page.get_by_role("button", name="Download transcript", exact=True).click()
    with page.expect_download() as pending:
        page.get_by_role("button", name="JSON Messages, tools, and image data.").click()
    data = json.loads(Path(pending.value.path()).read_text())
    user = next(message for message in data["messages"] if message["role"] == "user")
    assert user["content"][0]["text"] == "Describe this sample"
    assert user["content"][1]["image_url"]["url"].startswith("data:image/")
    assert any(message["role"] == "tool" for message in data["messages"])


def session_action(page, name):
    page.get_by_role("button", name="Session options", exact=True).click()
    page.get_by_role("button", name=name, exact=True).click()


def test_demo_session_actions_edit_regenerate_and_delete_are_native_and_isolated(demo_page):
    page = demo_page
    page.get_by_role("link", name="Build a small reading list").click()
    session_action(page, "Pin session")
    expect(page.get_by_text("Pinned", exact=True)).to_be_visible()
    session_action(page, "Rename")
    page.get_by_label("Session name", exact=True).fill("Reading ideas")
    page.get_by_role("button", name="Save name", exact=True).click()
    expect(page.locator(".topbar-title")).to_have_text("Reading ideas")
    session_action(page, "Branch session")
    page.get_by_label("Session name", exact=True).fill("Working copy")
    page.get_by_role("button", name="Create branch", exact=True).click()
    expect(page.locator(".topbar-title")).to_have_text("Working copy")
    page.get_by_role("button", name="Edit and resend", exact=True).click()
    dialog = page.get_by_role("dialog", name="Edit and resend", exact=True)
    dialog.get_by_role("textbox", name="Edit message", exact=True).fill("An edited request")
    dialog.get_by_role("button", name="Edit and resend", exact=True).click()
    expect(page.get_by_role("button", name="Stop response", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Send message", exact=True)).to_be_visible(timeout=20000)
    expect(page.locator(".message.user")).to_have_count(1)
    expect(page.locator(".message.user")).to_contain_text("An edited request")
    page.get_by_role("button", name="Regenerate response", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Regenerate response", exact=True).click()
    expect(page.get_by_role("button", name="Stop response", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Send message", exact=True)).to_be_visible(timeout=20000)
    expect(page.locator(".message.assistant")).to_have_count(1)
    page.get_by_role("button", name="Delete turn", exact=True).last.click()
    page.get_by_role("dialog").get_by_role("button", name="Delete turn", exact=True).click()
    expect(page.locator(".message")).to_have_count(0)
    session_action(page, "Delete session")
    page.get_by_role("dialog").get_by_role("button", name="Delete session", exact=True).click()
    expect(page.get_by_role("heading", name="New session")).to_be_visible()
    page.get_by_role("link", name="Reading ideas").click()
    expect(page.locator(".message.user")).to_contain_text("Write a small Python function")
    expect(page.get_by_role("link", name="Working copy")).to_have_count(0)
    page.reload()
    expect(page.locator(".topbar-title")).to_have_text("Build a small reading list")


def test_demo_native_command_menu_and_results(demo_page):
    page = demo_page
    page.get_by_role("link", name="A weekend outside").click()
    send(page, "/help")
    expect(page.get_by_role("region", name="/help command")).to_contain_text("/compress")
    send(page, "/compress")
    expect(page.get_by_role("region", name="/compress command")).to_contain_text(
        "Context is already compact."
    )
    expect(page.locator(".message.user")).to_have_count(2)
    send(page, "/branch")
    expect(page.get_by_role("dialog", name="Branch session", exact=True)).to_be_visible()

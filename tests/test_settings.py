import re

from playwright.sync_api import expect

from .test_browser import screenshot


def test_agent_settings_and_extended_access(page, live_app, tmp_path):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.get_by_role("button", name="Your space").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    expect(page.get_by_text("1.2.3", exact=True)).to_be_visible()
    expect(page.locator(".default-model-card")).to_contain_text("hermes-test")
    page.get_by_role("tab", name="Your agent", exact=True).focus()
    page.keyboard.press("End")
    expect(page.get_by_role("tab", name="Talaria", exact=True)).to_be_focused()
    page.keyboard.press("ArrowLeft")
    expect(page.get_by_role("tab", name="Connection", exact=True)).to_be_focused()
    expect(page.get_by_label("Hermes directory", exact=True)).to_have_count(0)
    expect(page.get_by_text("Talaria plugin not detected", exact=True)).to_be_visible()
    live_app[1].extension = {"agent": {"name": "Juniper"}}
    page.get_by_role("button", name="Check again", exact=True).click()
    expect(page.get_by_text("Connected · Talaria plugin", exact=True)).to_be_visible()
    page.get_by_role("tab", name="Your agent", exact=True).click()
    expect(page.get_by_role("heading", name="Juniper", exact=True)).to_be_visible()
    assert page.locator(".settings-panel").evaluate("el => el.scrollTop") == 0
    screenshot(page, "agent-settings")
    page.get_by_role("tab", name="Tools & skills", exact=True).click()
    page.get_by_label("Search tools and skills").fill("project")
    expect(page.get_by_text("project-notes", exact=True)).to_be_visible()
    expect(page.get_by_text("File tools", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Close dialog").click()
    expect(page.get_by_text("Connected to Juniper", exact=True)).to_be_visible()
    page.reload()
    expect(page.get_by_text("Connected to Juniper", exact=True)).to_be_visible()
    live_app[1].extension = None
    page.get_by_role("button", name="Your space").click()
    expect(page.get_by_role("heading", name="Hermes", exact=True)).to_be_visible()
    page.get_by_role("tab", name="Connection", exact=True).click()
    expect(page.get_by_text("Talaria plugin not detected", exact=True)).to_be_visible()
    page.get_by_role("button", name="Close dialog").click()
    expect(page.get_by_text("Connected to Hermes", exact=True)).to_be_visible()
    assert not errors


def test_models_are_scoped_to_each_session(page, live_app):
    chooser = page.get_by_role("button", name="Choose model", exact=True)
    expect(chooser).to_contain_text("Test provider")
    expect(chooser).to_contain_text("hermes-test")
    expect(chooser).not_to_contain_text("Default")
    chooser.click()
    expect(
        page.get_by_text("Your model selection will only apply to this session.")
    ).to_be_visible()
    page.get_by_role("button", name="Hermes Fast").click()
    expect(chooser).not_to_contain_text("Default")

    def send(message):
        page.get_by_label("Message Hermes").fill(message)
        page.get_by_role("button", name="Send message", exact=True).click()
        expect(page.get_by_role("button", name="Stop response")).to_have_count(0, timeout=10000)
        expect(page.get_by_label("Message Hermes")).to_have_value("")

    send("A chosen model")
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    first = next(iter(live_app[1].runs.values()))
    assert (first["model"], first["provider"]) == ("hermes-fast", "test")
    page.get_by_role("button", name=re.compile("^New conversation")).click()
    expect(chooser).not_to_contain_text("Default")
    send("A default model")
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    second = list(live_app[1].runs.values())[1]
    assert second["model"] is None and second["provider"] is None
    page.get_by_role("button", name="A chosen model", exact=True).click()
    expect(chooser).to_contain_text("hermes-fast")
    page.reload()
    expect(chooser).to_contain_text("hermes-fast")
    chooser.click()
    page.get_by_role("button", name=re.compile("hermes-test.*Default")).click()
    expect(chooser).not_to_contain_text("Default")
    send("Follow the configured default")
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_have_count(2)
    third = list(live_app[1].runs.values())[2]
    assert third["model"] is None and third["provider"] is None
    assert live_app[1].default_model == "hermes-test"
    live_app[1].default_model = "provider/new-default"
    page.get_by_role("button", name="Your space").click()
    expect(page.locator(".default-model-card")).to_contain_text("provider/new-default")
    page.get_by_role("button", name="Close dialog").click()
    expect(chooser).to_contain_text("provider/new-default")


def test_incomplete_discovery_keeps_settings_and_chat_usable(page, live_app):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    live_app[1].discovery_overrides.update(
        {
            "/health/detailed": ({}, 503),
            "/v1/toolsets": ({"changed": []}, 200),
            "/v1/skills": (None, 200),
            "/api/model/options": (
                {
                    "model": {},
                    "providers": [
                        None,
                        {"models": {}},
                        {
                            "slug": "partial",
                            "label": "Partial provider",
                            "authenticated": True,
                            "models": [None, {}, {"id": []}, "usable-model"],
                        },
                    ],
                },
                200,
            ),
        }
    )
    page.reload()
    page.get_by_role("button", name="Your space").click()
    expect(page.get_by_role("button", name="Refresh information")).to_be_enabled()
    expect(page.locator(".default-model-card")).to_contain_text("Not shared by Hermes")
    page.get_by_role("tab", name="Tools & skills", exact=True).click()
    expect(page.get_by_text("Toolset information is not available", exact=False)).to_be_visible()
    page.get_by_role("tab", name="Appearance", exact=True).click()
    page.get_by_role("button", name="Light", exact=True).click()
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_role("button", name="Choose model", exact=True).click()
    expect(page.get_by_role("button", name="usable-model Partial provider")).to_be_visible()
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_label("Message Hermes").fill("Chat still works")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    assert not errors

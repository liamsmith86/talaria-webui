from playwright.sync_api import expect


def test_model_selection_and_session_actions(page, live_app):
    page.get_by_role("button", name="Hermes default", exact=True).click()
    page.get_by_role("button", name="Hermes Fast").click()
    page.get_by_label("Message Hermes").fill("A model selection check")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    assert next(iter(live_app[1].runs.values()))["model"] == "hermes-fast"
    page.get_by_role("button", name="Conversation options", exact=True).click()
    page.get_by_role("button", name="Rename", exact=True).click()
    page.get_by_label("Conversation name").fill("A better name")
    page.get_by_role("button", name="Save name").click()
    expect(page.locator(".topbar-title")).to_have_text("A better name")
    page.get_by_role("button", name="Conversation options", exact=True).click()
    page.get_by_role("button", name="Branch conversation", exact=True).click()
    page.get_by_role("button", name="Create branch").click()
    expect(page.locator(".topbar-title")).to_have_text("A better name · branch")
    assert len(live_app[1].sessions) == 2
    page.get_by_role("button", name="Conversation options", exact=True).click()
    page.get_by_role("button", name="Delete conversation", exact=True).click()
    page.get_by_role("button", name="Delete conversation", exact=True).click()
    expect(page.locator(".topbar-title")).to_have_text("Your next beginning")
    assert len(live_app[1].sessions) == 1


def test_lost_submission_acknowledgment_does_not_duplicate_run(page, live_app):
    def lose_ack(route):
        route.fetch()
        route.abort("connectionreset")

    page.route("**/api/runs", lose_ack, times=1)
    page.get_by_label("Message Hermes").fill("A submission receipt check")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("button", name="Retry submission")).to_be_visible()
    page.get_by_role("button", name="Retry submission").click()
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    assert len(live_app[1].runs) == 1


def test_text_attachment_and_draft_restore(page, live_app):
    page.get_by_label("Message Hermes").fill("Keep this thought")
    page.reload()
    expect(page.get_by_label("Message Hermes")).to_have_value("Keep this thought")
    page.get_by_label("Text attachment").set_input_files(
        {"name": "notes.md", "mimeType": "text/markdown", "buffer": b"These are the project notes."}
    )
    expect(page.get_by_label("Message Hermes")).to_have_value(
        "Keep this thought\n\nFile: notes.md\n\n```\nThese are the project notes.\n```"
    )
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    assert "These are the project notes." in next(iter(live_app[1].runs.values()))["input"]

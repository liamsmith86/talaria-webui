"""Optional WCAG checks using a caller-supplied, locally pinned axe-core script."""

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

from talaria import installation

from .test_conversation_features import png

AXE = os.environ.get("TALARIA_AXE_PATH")
pytestmark = pytest.mark.skipif(not AXE, reason="Set TALARIA_AXE_PATH to a local axe-core script")


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("palette", ["blue", "sage", "violet", "rose"])
def test_theme_accessibility(page, live_app, theme, palette, monkeypatch):
    live_app[1].extension = {
        "context_runs": True,
        "profile_context": {"instructions": "ready", "prefill": "unavailable"},
    }
    monkeypatch.setattr(
        installation,
        "public_info",
        lambda _: {
            "environment": "production",
            "version": "0.2.0",
            "commit": "a" * 40,
            "managed": True,
            "branch": "main",
            "installed_at": "2026-09-08T20:00:00Z",
            "previous": {"version": "0.1.0", "commit": "b" * 40},
            "update_command": "sudo talaria update",
            "update": {"error": None, "available": True, "checked_at": "2026-09-08T21:00:00Z"},
        },
    )
    page.context.add_init_script(path=AXE)
    page.emulate_media(reduced_motion="reduce")
    page.reload()
    page.evaluate(
        "([t,p]) => Object.assign(document.documentElement.dataset, {theme:t, palette:p})",
        [theme, palette],
    )
    page.evaluate("document.fonts.ready")
    reports = {}

    def audit(name):
        result = page.evaluate("""async () => (await axe.run(document, {
          runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa']}
        })).violations.map(v => ({id:v.id, impact:v.impact,
          nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))}))""")
        reports[name] = result
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), name

    audit("welcome")
    page.get_by_role("button", name="Switch profile").click()
    expect(page.get_by_role("dialog", name="Choose a profile")).to_be_visible()
    audit("profile-picker")
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_role("button", name="Your space").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    audit("settings")
    for section in ("Appearance", "Providers", "Tools & skills", "Connection", "Talaria"):
        page.get_by_role("tab", name=section, exact=True).click()
        audit("settings-" + section)
        if section == "Connection":
            page.get_by_role("button", name="Add profile", exact=True).click()
            expect(page.get_by_label("Display name")).to_be_visible()
            audit("add-profile")
            page.get_by_role("button", name="Back", exact=True).click()
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_label("Message Hermes").fill("An approval check")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("button", name="Allow once")).to_be_visible()
    audit("approval")
    page.get_by_role("button", name="Allow once").click()
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    audit("conversation")
    page.get_by_role("button", name="Response details", exact=True).last.click()
    expect(page.get_by_role("dialog")).to_contain_text("actual-response-model")
    audit("response-details")
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_role("button", name="Context usage", exact=True).click()
    expect(page.get_by_role("progressbar")).to_be_visible()
    audit("context-usage")
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_role("button", name="Edit and resend", exact=True).first.click()
    expect(page.get_by_label("Edit message", exact=True)).to_be_visible()
    audit("edit-message")
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_role("button", name="Choose model", exact=True).click()
    audit("model-picker")
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_role("button", name="Choose reasoning", exact=True).click()
    audit("reasoning-picker")
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_role("button", name="Conversation options", exact=True).click()
    audit("conversation-menu")
    page.get_by_role("dialog").get_by_role(
        "button", name="Conversation details", exact=True
    ).click()
    expect(page.locator(".details-refresh")).to_have_count(0)
    audit("conversation-usage")
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_role("button", name="Conversation options", exact=True).click()
    page.get_by_role("button", name="Download transcript", exact=True).click()
    audit("transcript-download")
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_role("button", name="Find in conversation", exact=True).click()
    page.get_by_label("Find text in conversation").fill("thoughtful")
    expect(page.locator(".find-count")).to_have_text("1 of 1")
    audit("conversation-find")
    page.get_by_role("button", name="Close find").click()
    live_app[1].persist_image_originals = False
    page.get_by_label("File attachment").set_input_files(
        {"name": "sample.png", "mimeType": "image/png", "buffer": png()}
    )
    expect(page.locator(".attachment-chip")).to_have_count(1)
    audit("image-draft")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.locator(".message-image")).to_have_count(1)
    expect(page.get_by_role("button", name="Stop response")).to_have_count(0)
    page.get_by_role("button", name="Open sample.png", exact=True).click()
    audit("image-preview")
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_label("Message Hermes").fill("Delegate this check")
    page.get_by_role("button", name="Send message", exact=True).click()
    card = page.locator(".tool-card").filter(has_text="Subagent")
    expect(card).to_contain_text("12.5s")
    expect(page.get_by_role("button", name="Stop response")).to_have_count(0)
    card.locator("summary").click()
    audit("child-agent")
    card.get_by_role("button", name="Open child conversation").click()
    expect(page.get_by_text("The child transcript is ready.", exact=True)).to_be_visible()
    audit("child-transcript")
    page.get_by_role("button", name="Back to parent conversation").click()
    live_app[1].discovery_overrides["/health/detailed"] = (
        {
            "status": "degraded",
            "readiness": {"status": "degraded", "checks": {"model": {"status": "degraded"}}},
        },
        200,
    )
    page.evaluate("async () => (await import('/static/store.js')).refreshReadiness()")
    expect(page.get_by_label("Hermes readiness")).to_be_visible()
    audit("connection-readiness")
    page.set_viewport_size({"width": 390, "height": 844})
    audit("mobile")
    page.get_by_role("button", name="Conversation details", exact=True).click()
    audit("mobile-usage")
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_role("button", name="Choose model", exact=True).click()
    audit("mobile-models")
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_role("button", name="Open sidebar").click()
    audit("mobile-sidebar")
    page.get_by_role("button", name="Switch profile").click()
    audit("mobile-profile-picker")
    Path("test-results").mkdir(exist_ok=True)
    Path(f"test-results/accessibility-{theme}-{palette}.json").write_text(
        json.dumps(reports, indent=2)
    )
    assert not {state: issues for state, issues in reports.items() if issues}

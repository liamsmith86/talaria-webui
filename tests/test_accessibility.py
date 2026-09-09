"""Optional WCAG checks using a caller-supplied, locally pinned axe-core script."""

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

AXE = os.environ.get("TALARIA_AXE_PATH")
pytestmark = pytest.mark.skipif(not AXE, reason="Set TALARIA_AXE_PATH to a local axe-core script")


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("palette", ["blue", "sage", "violet", "rose"])
def test_theme_accessibility(page, theme, palette):
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

    audit("welcome")
    page.get_by_role("button", name="Your space").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    audit("settings")
    for section in ("Appearance", "Providers", "Tools & skills", "Connection"):
        page.get_by_role("tab", name=section, exact=True).click()
        audit("settings-" + section)
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_label("Message Hermes").fill("An approval check")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("button", name="Allow once")).to_be_visible()
    audit("approval")
    page.get_by_role("button", name="Allow once").click()
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    audit("conversation")
    page.set_viewport_size({"width": 390, "height": 844})
    audit("mobile")
    page.get_by_role("button", name="Open sidebar").click()
    audit("mobile-sidebar")
    Path("test-results").mkdir(exist_ok=True)
    Path(f"test-results/accessibility-{theme}-{palette}.json").write_text(
        json.dumps(reports, indent=2)
    )
    assert not {state: issues for state, issues in reports.items() if issues}

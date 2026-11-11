"""Keyboard-sized viewport changes preserve input."""

import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize("width", [641, 804, 1024])
def test_model_dialog_retains_focus_query_and_draft_across_keyboard_resize(page, width):
    page.set_viewport_size({"width": width, "height": 1000})
    composer = page.get_by_label("Message Hermes")
    composer.fill("Do not erase my draft")
    trigger = page.get_by_role("button", name="Choose model", exact=True)
    trigger.click()
    dialog = page.get_by_role("dialog", name="Choose a model")
    search = dialog.get_by_role("textbox", name="Search models")
    search.fill("synthetic query")
    search.evaluate("el => el.setSelectionRange(2, 7)")
    # This is a browser geometry regression, not a physical keyboard emulator.
    for size in [(width, 400), (1000, 500), (width, 1000)]:
        page.set_viewport_size({"width": size[0], "height": size[1]})
        expect(dialog).to_be_visible()
        expect(search).to_be_focused()
        expect(search).to_have_value("synthetic query")
        assert search.evaluate("el => [el.selectionStart, el.selectionEnd]") == [2, 7]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        box = dialog.bounding_box()
        assert box and box["x"] >= -1 and box["y"] >= -1
        assert box["y"] + box["height"] <= size[1] + 1
    dialog.get_by_role("button", name="Close dialog", exact=True).click()
    expect(trigger).to_be_focused()
    expect(composer).to_have_value("Do not erase my draft")
    composer.fill("Still editable")
    expect(composer).to_have_value("Still editable")

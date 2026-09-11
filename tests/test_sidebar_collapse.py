"""Desktop collapsing is persistent and independent from the mobile drawer."""

from playwright.sync_api import expect


def test_sidebar_collapse_persists_and_search_expands_it(page):
    sidebar = page.get_by_role("complementary", name="Sessions")
    expect(sidebar).to_be_visible()
    sidebar.get_by_role("button", name="Close sidebar", exact=True).click()
    expect(sidebar).to_be_hidden()
    opener = page.get_by_role("button", name="Open sidebar", exact=True)
    expect(opener).to_be_focused()
    page.reload()
    expect(sidebar).to_be_hidden()
    opener.click()
    expect(sidebar).to_be_visible()
    sidebar.get_by_role("button", name="Close sidebar", exact=True).click()
    page.keyboard.press("Control+k")
    expect(page.get_by_label("Search sessions")).to_be_focused()
    expect(sidebar).to_be_visible()


def test_mobile_drawer_does_not_change_desktop_preference(page):
    page.get_by_role("button", name="Close sidebar", exact=True).click()
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="Open sidebar", exact=True).click()
    drawer = page.get_by_role("dialog", name="Sessions", exact=True)
    expect(drawer).to_be_visible()
    drawer.get_by_role("button", name="Close sidebar", exact=True).click()
    expect(page.get_by_role("button", name="Open sidebar", exact=True)).to_be_focused()
    page.set_viewport_size({"width": 1280, "height": 800})
    expect(page.get_by_role("complementary", name="Sessions")).to_be_hidden()
    page.get_by_role("button", name="Open sidebar", exact=True).click()
    expect(page.get_by_role("complementary", name="Sessions")).to_be_visible()

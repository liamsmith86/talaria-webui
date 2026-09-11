"""Search unloaded history through the public API without losing live navigation or drafts."""

from playwright.sync_api import expect

from .conftest import wait_for_store
from .test_app import signed_in
from .test_conversation_features import seed


def enable(page, peer):
    peer.extension = {"history_search": True, "session_activity": True}
    page.reload()
    wait_for_store(page, "s => s.caps.talaria_extensions?.history_search === true")


def test_search_opens_exact_unloaded_message_and_preserves_draft(page, live_app):
    peer = live_app[1]
    seed(peer, count=700)
    enable(page, peer)
    page.get_by_role("link", name="Design notes", exact=True).click()
    page.get_by_label("Message Hermes").fill("Keep my unsent draft")
    page.get_by_label("Search sessions").fill("Message 020")
    hit = page.locator(".history-search-hit")
    expect(hit).to_have_count(1)
    assert "message=21" in hit.get_attribute("href")
    hit.click()
    expect(page.locator(".search-match")).to_contain_text("Message 020")
    expect(page.locator(".search-match")).to_be_in_viewport()
    assert page.locator(".message").count() <= 11
    expect(page.get_by_label("Message Hermes")).to_have_count(0)
    # Focus catch-up must not turn the archived window back into the current tail.
    page.evaluate("async () => (await import('/static/store.js')).refreshHistory('notes')")
    expect(page.locator(".search-match")).to_contain_text("Message 020")
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    page.reload()
    expect(page.locator(".search-match")).to_contain_text("Message 020")
    page.get_by_role("button", name="View latest messages").click()
    expect(page.get_by_label("Message Hermes")).to_have_value("Keep my unsent draft")
    expect(page.get_by_text("Message 699", exact=True)).to_be_visible()
    page.go_back()
    expect(page.locator(".search-match")).to_contain_text("Message 020")
    assert "message=21" in page.url


def test_search_debounces_recovers_and_does_not_render_markup(page, live_app):
    peer = live_app[1]
    seed(peer, count=40)
    peer.messages["notes"][0]["content"] = '<img src=x onerror="window.bad=true">'
    enable(page, peer)
    search = page.get_by_label("Search sessions")
    search.fill("Message 01")
    search.fill("Message 02")
    search.fill("onerror")
    hit = page.locator(".history-search-hit")
    expect(hit).to_have_count(1)
    expect(hit).to_contain_text("<img")
    assert hit.locator("img").count() == 0
    queries = [query["q"] for _, path, query in peer.calls if path == "/talaria/v1/search"]
    assert queries == ["onerror"]
    peer.discovery_overrides["/talaria/v1/search"] = ({"error": "Fixture unavailable"}, 503)
    search.fill("Failure")
    expect(page.get_by_role("button", name="Retry search")).to_be_visible()
    del peer.discovery_overrides["/talaria/v1/search"]
    page.get_by_role("button", name="Retry search").click()
    expect(page.get_by_text("No matching messages.", exact=True)).to_be_visible()


def test_orphan_tool_result_search_and_missing_message(page, live_app):
    peer = live_app[1]
    seed(peer)
    peer.messages["notes"] = [
        {
            "id": i + 1,
            "role": "tool",
            "content": f"Result {i}",
            "tool_call_id": f"tool-{i}",
            "tool_name": "terminal",
        }
        for i in range(20)
    ]
    enable(page, peer)
    page.get_by_label("Search sessions").fill("Result 18")
    page.locator(".history-search-hit").click()
    expect(page.locator(".search-match")).to_be_visible()
    matched = page.locator("[data-search-target]")
    expect(matched).to_have_count(1)
    expect(matched.locator(".tool-content")).to_be_visible()
    expect(matched).to_contain_text("Result 18")
    expect(matched).to_be_in_viewport()
    peer.messages["notes"] = []
    page.reload()
    expect(page.get_by_role("alert")).to_contain_text("no longer available")
    expect(page.get_by_role("button", name="View latest messages")).to_be_visible()


def test_search_validation_and_plugin_failure_are_explicit(live_app):
    with signed_in(live_app[0]) as client:
        for path in (
            "/api/search?q=",
            "/api/search?q=x&offset=-1",
            "/api/search?q=" + "x" * 201,
            "/api/sessions/notes/around?message_id=0",
        ):
            assert client.get(path).status_code == 400
        assert client.get("/api/search?q=hello").status_code == 404


def test_incompatible_search_response_is_a_readable_error(live_app):
    peer = live_app[1]
    peer.discovery_overrides["/talaria/v1/search"] = ({"data": [None]}, 200)
    with signed_in(live_app[0]) as client:
        result = client.get("/api/search?q=hello")
        assert result.status_code == 502
        assert "unreadable session view" in result.json()["error"]

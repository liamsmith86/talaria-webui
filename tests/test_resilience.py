import asyncio
from pathlib import Path

import httpx
from playwright.sync_api import expect

from talaria.relay import Relay

from .test_app import signed_in


def test_duplicate_conversation_names(live_app):
    with signed_in(live_app[0]) as client:
        a = client.post("/api/sessions", json={"title": "Hello"}).json()
        b = client.post("/api/sessions", json={"title": "Hello"}).json()
        assert a["id"] != b["id"]
        assert b["title"] == "Hello (2)"


def test_request_limits_and_login_throttling(live_app):
    url = live_app[0]
    with signed_in(url) as client:
        oversized = client.post(
            "/api/runs", json={"session_id": "limit-check", "input": "x" * 1_100_000}
        )
        assert oversized.status_code == 400
        assert oversized.json()["error"] == "Invalid input."
        assert client.post("/api/runs", json={"input": "x" * 9_600_000}).status_code == 413
        assert client.get("/api/sessions/invalid%20identifier/messages").status_code == 400
    with httpx.Client(base_url=url, headers={"X-Talaria-Request": "1"}) as client:
        codes = [client.post("/api/login", json={"password": "bad"}).status_code for _ in range(9)]
        assert codes[:8] == [401] * 8
        assert codes[-1] == 429


def test_relay_restart_does_not_restart_agent(page, live_app):
    page.get_by_label("Message Hermes").fill("A slow response for reconnect testing")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("heading", name="A thoughtful place to start")).to_be_visible()
    app = live_app[2]
    channel = next(iter(app.state.relay.channels.values()))

    async def replace_relay():
        await app.state.relay.close()
        app.state.relay = Relay(app.state.hermes)

    asyncio.run_coroutine_threadsafe(replace_relay(), channel.task.get_loop()).result(timeout=5)
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible(
        timeout=20000
    )
    expect(page.get_by_role("button", name="Stop response")).to_have_count(0)
    expect(page.get_by_role("heading", name="A thoughtful place to start")).to_have_count(1)
    assert len(live_app[1].runs) == 1


def test_stop_and_steer(page, live_app):
    page.get_by_label("Message Hermes").fill("A slow response")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("button", name="Stop response")).to_be_visible()
    page.get_by_label("Message Hermes").fill("Focus on readability")
    page.get_by_role("button", name="Send guidance").click()
    expect(page.get_by_label("Message Hermes")).to_have_value("")
    page.get_by_role("button", name="Stop response").click()
    expect(page.get_by_text("You stopped this response.")).to_be_visible()
    assert live_app[1].stops == 1
    assert next(iter(live_app[1].runs.values()))["steer"] == "Focus on readability"


def test_long_history_and_malicious_markdown(page, live_app):
    peer = live_app[1]
    sid = "long-history"
    peer.sessions[sid] = {"id": sid, "title": "A long session"}
    peer.messages[sid] = [
        {"id": i + 1, "role": "user" if i % 2 == 0 else "assistant", "content": f"History item {i}"}
        for i in range(130)
    ]
    peer.messages[sid].append(
        {
            "id": 131,
            "role": "assistant",
            "content": (
                "<script>window.compromised=1</script>\n\n"
                '<img src=x onerror="window.compromised=2">\n\n'
                "[Bad link](javascript:alert(1))\n\n![Remote](https://untrusted.example/tracker.png)\n\n"
                '```python\nprint("safe")\n```'
            ),
        }
    )
    requested = []
    page.on("request", lambda r: requested.append(r.url))
    page.reload()
    page.get_by_role("button", name="A long session", exact=True).click()
    expect(page.get_by_role("button", name="Load earlier messages")).to_be_visible()
    page.get_by_role("button", name="Load earlier messages").click()
    expect(page.get_by_text("History item 0", exact=True)).to_be_visible()
    assert not page.evaluate("window.compromised")
    assert not any("untrusted.example" in url for url in requested)
    assert page.locator('.markdown a[href^="javascript:"]').count() == 0
    assert page.locator(".token.string").count() > 0


def test_connection_setup_without_exposing_key(page, live_app):
    page.get_by_role("button", name="Settings").click()
    page.get_by_role("tab", name="Connection", exact=True).click()
    expect(page.get_by_label("API key", exact=True)).to_have_value("")
    page.get_by_role("button", name="Test connection", exact=True).click()
    expect(page.get_by_text("Hermes connection verified.", exact=True)).to_be_visible()
    page.get_by_role("button", name="Save connection").click()
    expect(page.locator(".toast")).to_have_text("Connected to Hermes")
    path = live_app[2].state.config_path
    assert path.exists() and path.stat().st_mode & 0o077 == 0
    assert "test-hermes-key" not in page.content()


def test_small_screen_keeps_composer_visible(page):
    page.set_viewport_size({"width": 390, "height": 430})
    box = page.get_by_label("Message Hermes").bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] < 430
    assert page.locator(".send-button").bounding_box()["y"] < 400
    Path("test-results").mkdir(exist_ok=True)
    page.screenshot(path="test-results/compact-mobile.png", animations="disabled")

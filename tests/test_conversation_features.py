"""Session workflows across the browser, Talaria, and the Hermes protocol."""

import base64
import json
import re
import struct
import zlib

import httpx
import pytest
from playwright.sync_api import expect

from talaria.hermes import APIError

from .conftest import fire_timers, wait_for_store, wait_for_timer
from .test_app import signed_in
from .test_browser import screenshot


def png(width=48, height=32):
    def chunk(name, data):
        return (
            struct.pack("!I", len(data)) + name + data + struct.pack("!I", zlib.crc32(name + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x41\x68\xcc" * width) * height))
        + chunk(b"IEND", b"")
    )


IMAGE = "data:image/png;base64," + base64.b64encode(png()).decode()


def seed(peer, sid="notes", *, count=1):
    peer.sessions[sid] = {"id": sid, "title": "Design notes", "source": "cli", "pinned": False}
    peer.messages[sid] = [
        {"id": i + 1, "role": "user" if i % 2 == 0 else "assistant", "content": f"Message {i:03}"}
        for i in range(count)
    ]
    return sid


def send(page, text):
    page.get_by_label("Message Hermes").fill(text)
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()


def test_pin_validation_and_native_persistence(live_app):
    url, peer, _ = live_app
    sid = seed(peer)
    assert httpx.patch(url + f"/api/sessions/{sid}", json={"pinned": True}).status_code == 401
    with signed_in(url) as client:
        for body in ({"pinned": "true"}, {"archived": True}, {}):
            assert client.patch(f"/api/sessions/{sid}", json=body).status_code == 400
        assert (
            client.patch(
                f"/api/sessions/{sid}", json={"pinned": True}, headers={"X-CSRF-Token": "bad"}
            ).status_code
            == 403
        )
        response = client.patch(f"/api/sessions/{sid}", json={"pinned": True})
        assert response.status_code == 200
        assert peer.sessions[sid]["pinned"] is True
        assert peer.sessions[sid]["title"] == "Design notes"
    with signed_in(url) as other_client:
        assert other_client.get(f"/api/sessions/{sid}").json()["pinned"] is True


def test_complete_exports_and_image_structure(live_app):
    url, peer, _ = live_app
    sid = seed(peer, count=237)
    peer.messages[sid][-1]["content"] = [
        {"type": "text", "text": "Final image"},
        {"type": "image_url", "image_url": {"url": IMAGE}},
    ]
    peer.messages[sid][2]["tool_calls"] = [
        {"id": "tool-1", "function": {"name": "read_file", "arguments": '{"path":"notes.md"}'}}
    ]
    assert httpx.get(url + f"/api/sessions/{sid}/export").status_code == 401
    with signed_in(url) as client:
        response = client.get(
            f"/api/sessions/{sid}/export?format=json", headers={"Accept-Encoding": "identity"}
        )
        assert response.status_code == 200
        exported = response.json()
        assert exported["messages"] == peer.messages[sid]
        assert int(response.headers["content-length"]) == len(response.content)
        assert "attachment;" in response.headers["content-disposition"]
        markdown = client.get(f"/api/sessions/{sid}/export").text
        assert "Message 000" in markdown and "Message 235" in markdown and "Final image" in markdown
        assert "read_file" in markdown and "included in the JSON transcript" in markdown
        assert "data:image" not in markdown
        assert client.get(f"/api/sessions/{sid}/export?format=html").status_code == 400


def test_history_adapts_large_pages_and_failed_export_is_not_partial(live_app, monkeypatch):
    url, peer, app = live_app
    sid = seed(peer, count=61)
    original = app.state.hermes.request

    async def limited(method, path, **kwargs):
        if path.endswith("/messages") and kwargs["params"]["limit"] > 25:
            raise APIError("Large image history", code="response_too_large")
        return await original(method, path, **kwargs)

    monkeypatch.setattr(app.state.hermes, "request", limited)
    with signed_in(url) as client:
        first = client.get(f"/api/sessions/{sid}/messages").json()
        assert first["data"] == peer.messages[sid][-25:]
        assert first["next_offset"] == 25 and first["has_more"]
        second = client.get(f"/api/sessions/{sid}/messages?offset=25").json()
        assert second["data"] == peer.messages[sid][-50:-25]
        assert (
            client.get(f"/api/sessions/{sid}/export?format=json").json()["messages"]
            == peer.messages[sid]
        )

        async def failed_page(method, path, **kwargs):
            if path.endswith("/messages") and kwargs["params"]["offset"] > 0:
                raise APIError("A history page is unavailable.")
            return await limited(method, path, **kwargs)

        monkeypatch.setattr(app.state.hermes, "request", failed_page)
        response = client.get(f"/api/sessions/{sid}/export?format=json")
        assert response.status_code == 502
        assert "content-disposition" not in response.headers
        assert "history page is unavailable" in response.json()["error"]


def test_images_and_reasoning_are_forwarded_without_rebuilding_history(live_app):
    url, peer, _ = live_app
    sid = seed(peer)
    with signed_in(url) as client:
        payload = {
            "session_id": sid,
            "input": "Inspect the image",
            "images": [{"url": IMAGE}],
            "reasoning": "high",
            "request_id": "image-once",
        }
        response = client.post("/api/runs", json=payload)
        assert response.status_code == 202
        assert client.post("/api/runs", json=payload).json()["run_id"] == response.json()["run_id"]
        run = next(iter(peer.runs.values()))
        assert run["raw_input"] == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect the image"},
                    {"type": "image_url", "image_url": {"url": IMAGE}},
                ],
            }
        ]
        assert run["model_options"] == {"reasoning": {"enabled": True, "effort": "high"}}
        assert peer.messages[sid][0]["content"] == "Message 000"
        assert len(peer.runs) == 1
        for bad in (
            None,
            "https://example.com/image.png",
            [{"url": "file:///secret"}],
            [{"url": "data:image/svg+xml;base64,PHN2Zz4="}],
            [{"url": "data:image/png;base64,bm90LWFuLWltYWdl"}],
            [{"url": IMAGE}] * 5,
        ):
            assert client.post("/api/runs", json={**payload, "images": bad}).status_code == 400
        for bad in (None, [], "invented", "constructor"):
            assert client.post("/api/runs", json={**payload, "reasoning": bad}).status_code == 400
        assert len(peer.runs) == 1


def test_readiness_projects_known_checks_and_survives_drift(live_app):
    url, peer, _ = live_app
    health = {
        "status": "degraded",
        "readiness": {
            "status": "degraded",
            "checks": {
                "model": {"status": "degraded", "private": "DO-NOT-EXPOSE"},
                "disk": {"status": "degraded", "used_percent": 94.5},
                "future_check": {"status": "degraded", "detail": "DO-NOT-EXPOSE"},
            },
        },
    }
    peer.discovery_overrides["/health/detailed"] = (health, 200)
    with signed_in(url) as client:
        response = client.get("/api/readiness")
        assert response.status_code == 200 and "DO-NOT-EXPOSE" not in response.text
        assert "No default model is configured" in response.text and "94.5%" in response.text
        health["readiness"]["checks"]["model"]["status"] = {}
        assert client.get("/api/readiness").status_code == 200
        peer.discovery_overrides["/health/detailed"] = ({"error": "DO-NOT-EXPOSE"}, 503)
        response = client.get("/api/readiness")
        assert response.json()["status"] == "unavailable" and "DO-NOT-EXPOSE" not in response.text


def test_pins_and_source_labels_across_pagination(page, live_app):
    peer = live_app[1]
    for i in range(115):
        sid = seed(peer, f"saved-{i}")
        peer.sessions[sid]["title"] = f"Saved note {i}"
    peer.sessions["saved-0"]["pinned"] = True
    page.reload()
    expect(page.locator(".session-group").first).to_have_text("Pinned")
    expect(page.locator(".session-select").first).to_have_accessible_name("Saved note 0")
    expect(page.locator(".source-badge").first).to_have_text("CLI")
    page.get_by_role("button", name="Load more sessions").click()
    expect(page.locator(".session-select")).to_have_count(115)
    page.get_by_role("button", name="Options for Saved note 0", exact=True).click()
    page.get_by_role("button", name="Unpin session", exact=True).click()
    expect(page.get_by_text("Pinned", exact=True)).to_have_count(0)
    assert peer.sessions["saved-0"]["pinned"] is False
    page.get_by_role("button", name="Options for Saved note 114", exact=True).click()
    # The row moves at once; a reload could abort or outrun the save.
    with page.expect_response(lambda r: r.request.method == "PATCH"):
        page.get_by_role("button", name="Pin session", exact=True).click()
    expect(page.locator(".session-select").first).to_have_accessible_name("Saved note 114")
    page.reload()
    expect(page.locator(".session-group").first).to_have_text("Pinned")


def test_usage_distinguishes_zero_unknown_and_estimate(page, live_app):
    peer = live_app[1]
    sid = seed(peer)
    peer.sessions[sid].update(
        actual_cost_usd=0,
        estimated_cost_usd=0.24,
        input_tokens=9876,
        output_tokens=0,
        cache_read_tokens=None,
        reasoning_tokens=123,
    )
    page.reload()
    page.get_by_role("link", name="Design notes", exact=True).click()
    page.get_by_role("button", name="Session details", exact=True).click()
    expect(page.locator(".usage-total")).to_contain_text("Reported cost")
    expect(page.locator(".usage-total strong")).to_have_text("$0.00")
    expect(page.locator(".usage-grid > div").filter(has_text="Input tokens")).to_contain_text(
        "9,876"
    )
    expect(page.locator(".usage-grid > div").filter(has_text="Output tokens")).to_contain_text("0")
    expect(page.locator(".usage-grid > div").filter(has_text="Cache read tokens")).to_contain_text(
        "—"
    )
    screenshot(page, "conversation-usage")


def test_reasoning_is_session_scoped_and_catalog_is_inherited(page, live_app):
    chooser = page.get_by_role("button", name="Choose reasoning", exact=True)
    chooser.click()
    page.get_by_role("button", name="High", exact=True).click()
    send(page, "Reason carefully")
    assert next(iter(live_app[1].runs.values()))["model_options"]["reasoning"]["effort"] == "high"
    page.reload()
    chooser.click()
    expect(page.get_by_role("button", name="High", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_role("button", name=re.compile("^New session")).click()
    expect(chooser).to_contain_text("Auto")
    send(page, "Use the default reasoning")
    assert list(live_app[1].runs.values())[-1]["model_options"] is None


def test_model_without_published_levels_offers_the_full_reasoning_range(page, live_app):
    page.get_by_role("button", name="Choose reasoning", exact=True).click()
    for level in ("Low", "Medium", "High", "Extra high", "Maximum"):
        expect(page.get_by_role("button", name=level, exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Ultra", exact=True)).to_have_count(0)
    expect(
        page.get_by_text("Hermes may adjust the level to match the model’s supported settings.")
    ).to_be_visible()
    page.get_by_role("button", name="Maximum", exact=True).click()
    send(page, "Think as hard as you can")
    assert next(iter(live_app[1].runs.values()))["model_options"]["reasoning"]["effort"] == "max"


def test_catalog_warnings_prices_and_unavailable_models(page, live_app):
    peer = live_app[1]
    peer.discovery_overrides["/api/model/options"] = (
        {
            "model": "available",
            "provider": "test",
            "providers": [
                {
                    "slug": "test",
                    "name": "Test provider",
                    "authenticated": True,
                    "is_current": True,
                    "models": ["available", "locked", "plain"],
                    "unavailable_models": ["locked"],
                    "featured_models": ["available"],
                    "pricing": {"available": {"input": "$0.25", "output": "$1.00"}},
                    "warning": "Account information needs refreshing.",
                    "capabilities": {
                        "available": {"reasoning": True, "can_disable_reasoning": False},
                        "plain": {"reasoning": False},
                    },
                }
            ],
        },
        200,
    )
    page.reload()
    page.get_by_role("button", name="Choose model", exact=True).click()
    expect(page.get_by_role("button", name=re.compile("locked.*Unavailable"))).to_be_disabled()
    expect(page.get_by_text("Per 1M tokens", exact=False)).to_contain_text("$0.25")
    page.locator(".catalog-warnings summary").click()
    expect(page.get_by_text("Account information needs refreshing.", exact=False)).to_be_visible()
    page.get_by_role("button", name="Refresh model catalog").click()
    expect(page.get_by_role("button", name="Refresh model catalog")).to_be_enabled()
    assert ("GET", "/api/model/options", {"refresh": "1"}) in peer.calls
    screenshot(page, "model-information")
    page.get_by_role("button", name="plain Test provider", exact=True).click()
    page.get_by_role("button", name="Choose reasoning", exact=True).click()
    expect(page.get_by_text("This model does not support adjustable reasoning.")).to_be_visible()
    expect(page.get_by_role("button", name="High", exact=True)).to_have_count(0)


def test_image_draft_history_display_and_download(page, live_app):
    page.get_by_label("File attachment").set_input_files(
        {"name": "diagram.png", "mimeType": "image/png", "buffer": png()}
    )
    expect(page.locator(".attachment-chip img")).to_have_count(1)
    page.reload()
    expect(page.locator(".attachment-chip img")).to_have_count(1)
    send(page, "Explain this diagram")
    expect(page.locator(".message-image img")).to_have_count(1)
    page.wait_for_function(
        "document.querySelector('.message-image img').complete"
        " && document.querySelector('.message-image img').naturalWidth === 48"
    )
    assert not page.evaluate("JSON.stringify(localStorage).includes('data:image')")
    page.reload()
    expect(page.locator(".message-image img")).to_have_count(1)
    page.get_by_role("button", name="Open Image 1", exact=True).click()
    expect(page.locator(".image-dialog img")).to_be_visible()
    screenshot(page, "image-preview")
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_role("button", name="Session options", exact=True).click()
    page.get_by_role("button", name="Download transcript", exact=True).click()
    with page.expect_download() as downloaded:
        page.get_by_role("button", name=re.compile("^JSON")).click()
    content = json.loads(downloaded.value.path().read_text())
    assert len(content["messages"]) == 2
    assert content["messages"][0]["content"][1]["image_url"]["url"] == IMAGE
    expect(page.locator(".attachment-chip")).to_have_count(0)


@pytest.mark.parametrize(
    ("text", "attached"),
    [
        ("Quarterly results\n" + "Revenue grew in every region. " * 170, False),
        ("image.png", True),
        ("https://example.test/chart.png", True),
        ("", True),
    ],
)
def test_pasting_text_with_a_picture_of_it_keeps_the_text(page, text, attached):
    prevented = page.get_by_label("Message Hermes").evaluate(
        """(el, [data, text]) => {
      const transfer = new DataTransfer();
      const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0));
      transfer.items.add(new File([bytes], 'image.png', {type: 'image/png'}));
      if (text) transfer.setData('text/plain', text);
      const event = new ClipboardEvent('paste', {
        clipboardData: transfer, bubbles: true, cancelable: true});
      Object.defineProperty(event, 'clipboardData', {value: transfer});
      el.dispatchEvent(event);
      return event.defaultPrevented;
    }""",
        [base64.b64encode(png()).decode(), text],
    )
    assert prevented is attached
    expect(page.locator(".attachment-chip")).to_have_count(int(attached))


@pytest.mark.parametrize("retained_by_hermes", [True, False])
def test_pasted_images_recover_an_ambiguous_submission_without_duplication(
    page, live_app, retained_by_hermes
):
    live_app[1].persist_image_originals = retained_by_hermes
    page.get_by_label("Message Hermes").evaluate(
        """(el, data) => {
      const transfer = new DataTransfer();
      const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0));
      transfer.items.add(new File([bytes], 'pasted.png', {type:'image/png'}));
      const event = new ClipboardEvent('paste', {
        clipboardData: transfer, bubbles: true, cancelable: true});
      // Firefox strips constructor-supplied files from synthetic clipboard
      // events. Supply the fixture explicitly; this tests app recovery, not OS paste.
      Object.defineProperty(event, 'clipboardData', {value: transfer});
      el.dispatchEvent(event);
    }""",
        base64.b64encode(png()).decode(),
    )
    expect(page.locator(".attachment-chip")).to_have_count(1)

    def lose_ack(route):
        route.fetch()
        route.abort("connectionreset")

    page.route("**/api/runs", lose_ack, times=1)
    page.get_by_label("Message Hermes").fill("A pasted screenshot")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("button", name="Retry submission")).to_be_visible()
    assert not page.evaluate("JSON.stringify(localStorage).includes('data:image')")
    page.reload()
    expect(page.get_by_role("button", name="Retry submission")).to_be_visible()
    page.get_by_role("button", name="Retry submission").click()
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    assert len(live_app[1].runs) == 1
    expect(page.locator(".message-image img")).to_have_count(1)
    page.get_by_role("button", name=re.compile("^New session")).click()
    expect(page.locator(".attachment-chip")).to_have_count(0)
    expect(page.get_by_label("Message Hermes")).to_have_value("")


def test_browser_images_survive_native_placeholders_and_export_without_changing_history(
    page, live_app
):
    peer = live_app[1]
    peer.persist_image_originals = False
    sid = seed(peer, count=2)
    peer.messages[sid][0]["content"] = "Inspect this image\n[screenshot]"
    page.reload()
    page.get_by_role("link", name="Design notes", exact=True).click()
    expect(page.locator(".image-unavailable")).to_have_count(1)
    page.get_by_label("File attachment").set_input_files(
        {"name": "saved.png", "mimeType": "image/png", "buffer": png()}
    )
    expect(page.locator(".attachment-chip")).to_have_count(1)
    send(page, "Inspect this image")
    expect(page.locator(".message-image img")).to_have_count(1)
    page.reload()
    expect(page.locator(".message-image img")).to_have_count(1)
    expect(page.locator(".image-retention-note")).to_have_text("Original kept in this browser")
    expect(page.locator(".image-unavailable")).to_have_count(1)
    assert peer.messages[sid][-2]["content"] == "Inspect this image\n[screenshot]"
    page.get_by_role("button", name="Session options", exact=True).click()
    page.get_by_role("button", name="Download transcript", exact=True).click()
    with page.expect_download() as downloaded:
        page.get_by_role("button", name=re.compile("^JSON")).click()
    content = json.loads(downloaded.value.path().read_text())
    assert content["messages"] == peer.messages[sid]
    assert len(content["browser_attachments"]) == 1
    assert content["browser_attachments"][0]["message_id"] == peer.messages[sid][-2]["id"]
    assert content["browser_attachments"][0]["images"][0]["url"] == IMAGE
    other_context = page.context.browser.new_context()
    other = other_context.new_page()
    other.goto(live_app[0])
    other.get_by_label("Password", exact=True).fill("test-password")
    other.get_by_role("button", name="Sign in").click()
    other.get_by_role("link", name="Design notes", exact=True).click()
    expect(other.locator(".message-image")).to_have_count(0)
    expect(other.locator(".image-unavailable")).to_have_count(2)
    other_context.close()
    page.get_by_role("button", name="Session options", exact=True).click()
    page.get_by_role("button", name="Delete session", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Delete session", exact=True).click()
    expect(page.locator(".topbar-title")).to_have_text("New session")
    assert (
        page.evaluate(
            "async sid => (await import('/static/attachments.js')).browserAttachments(sid)", sid
        )
        == []
    )


def test_native_image_preview_survives_reload_during_a_run_and_stop(page, live_app):
    live_app[1].persist_image_originals = False
    page.get_by_label("File attachment").set_input_files(
        {"name": "ongoing.png", "mimeType": "image/png", "buffer": png()}
    )
    expect(page.locator(".attachment-chip")).to_have_count(1)
    page.get_by_label("Message Hermes").fill("Hold this image review")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("button", name="Stop response")).to_be_visible()
    page.reload()
    expect(page.locator(".message-image img")).to_have_count(1)
    expect(page.locator(".image-unavailable")).to_have_count(0)
    page.get_by_role("button", name="Stop response").click()
    expect(page.get_by_role("button", name="Stop response")).to_have_count(0)
    page.reload()
    try:
        expect(page.locator(".message-image img")).to_have_count(1)
    except AssertionError as error:
        diagnostic = page.evaluate("""async () => {
            const {state} = await import('/static/store.js');
            const {pendingStorage} = await import('/static/attachments.js');
            const live = state.lives[state.active];
            return {active:state.active, cached:await pendingStorage('image-index'),
              live:live && {id:live.id, status:live.status, receipt:live.imageReceipt,
                images:live.userImages?.length, boundary:live.imageBoundary},
              history:state.history.map(m => ({id:m.id, content:m.content,
                images:m.browserImages?.length}))};
        }""")
        raise AssertionError(diagnostic) from error
    assert len(live_app[1].runs) == 1


def test_interrupted_cache_write_keeps_the_original_until_recovery(page, live_app):
    live_app[1].persist_image_originals = False
    page.evaluate("""() => {
        const original = IDBObjectStore.prototype.put;
        IDBObjectStore.prototype.put = function (value, key) {
            if (key.startsWith('image.')) {
                IDBObjectStore.prototype.put = original;
                throw new DOMException('Interrupted cache write', 'AbortError');
            }
            return original.call(this, value, key);
        };
    }""")
    page.get_by_label("File attachment").set_input_files(
        {"name": "interrupted.png", "mimeType": "image/png", "buffer": png()}
    )
    expect(page.locator(".attachment-chip")).to_have_count(1)
    send(page, "Keep this original")
    expect(page.locator(".toast")).to_contain_text("could not retain the image")
    page.reload()
    expect(page.locator(".message-image img")).to_have_count(1)
    page.reload()
    expect(page.locator(".message-image img")).to_have_count(1)
    assert len(live_app[1].runs) == 1


def test_browser_image_cache_evicts_older_originals_within_its_budget(module_page):
    # PNG padding represents accepted, large originals without a decoder dependency.
    original = "data:image/png;base64," + base64.b64encode(png() + b"\0" * 1024 * 1024).decode()
    result = module_page.evaluate(
        """async url => {
        const {cacheMessageImages, pendingStorage} = await import('/static/attachments.js');
        const images = Array.from({length:4}, (_, i) => ({url, name:`Image ${i}`, size:1048576}));
        for (let i=0; i<8; i++) await cacheMessageImages('cache-budget', i, images);
        const index = await pendingStorage('image-index');
        return {bytes:index.reduce((n, item) => n + item.bytes, 0),
          oldest:await pendingStorage('image.cache-budget.0'),
          newest:(await pendingStorage('image.cache-budget.7')).length};
    }""",
        original,
    )
    assert result["bytes"] <= 32 * 1024 * 1024
    assert result.get("oldest") is None
    assert result["newest"] == 4


def test_bad_image_is_explained_and_text_remains_usable(page):
    page.get_by_label("File attachment").set_input_files(
        {"name": "unsupported.svg", "mimeType": "image/svg+xml", "buffer": b"<svg/>"}
    )
    expect(page.locator(".attachment-error")).to_contain_text("PNG, JPEG, WebP, or GIF")
    expect(page.locator(".attachment-chip")).to_have_count(0)
    send(page, "Text still works")


def test_image_resize_removal_and_image_only_message(page, live_app):
    page.get_by_label("File attachment").set_input_files(
        [
            {"name": "wide.png", "mimeType": "image/png", "buffer": png(3000, 20)},
            {"name": "small.png", "mimeType": "image/png", "buffer": png()},
        ]
    )
    expect(page.locator(".attachment-chip")).to_have_count(2)
    expect(page.locator(".attachment-chip").first).to_contain_text("Resized")
    page.wait_for_function("document.querySelector('.attachment-chip img').naturalWidth === 2560")
    page.get_by_role("button", name="Remove small.png", exact=True).click()
    expect(page.locator(".attachment-chip")).to_have_count(1)
    page.reload()
    expect(page.locator(".attachment-chip")).to_have_count(1)
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    expect(page.locator(".message-image")).to_have_count(1)
    expect(page.locator(".topbar-title")).to_have_text("Untitled")
    assert next(iter(live_app[1].sessions.values()))["title"] == "Untitled"
    run = next(iter(live_app[1].runs.values()))
    assert len(run["raw_input"][0]["content"]) == 1
    assert run["raw_input"][0]["content"][0]["image_url"]["url"].startswith("data:image/jpeg")


def test_image_storage_failure_preserves_text_and_allows_retry(page):
    page.evaluate("""() => {
        const original = IDBObjectStore.prototype.put;
        IDBObjectStore.prototype.put = function (...args) {
            IDBObjectStore.prototype.put = original;
            throw new DOMException('Browser storage is full.', 'QuotaExceededError');
        };
    }""")
    page.get_by_label("Message Hermes").fill("Keep this text")
    attachment = {"name": "sample.png", "mimeType": "image/png", "buffer": png()}
    page.get_by_label("File attachment").set_input_files(attachment)
    expect(page.locator(".attachment-error")).to_contain_text("storage is full")
    expect(page.get_by_label("Message Hermes")).to_have_value("Keep this text")
    expect(page.locator(".attachment-chip")).to_have_count(0)
    page.get_by_label("File attachment").set_input_files(attachment)
    expect(page.locator(".attachment-chip")).to_have_count(1)
    expect(page.get_by_label("Message Hermes")).to_have_value("Keep this text")


def test_find_includes_earlier_messages_and_literal_formatted_text(page, live_app):
    peer = live_app[1]
    sid = seed(peer, count=122)
    peer.messages[sid][0]["content"] = "Earlier needle (literal)"
    peer.messages[sid][-1]["content"] = "Latest **needle** (literal) and emoji 🌿"
    page.reload()
    page.get_by_role("link", name="Design notes", exact=True).click()
    page.get_by_role("button", name="Find in session", exact=True).click()
    field = page.get_by_label("Find text in session")
    field.fill("needle (literal)")
    expect(page.locator(".find-count")).to_have_text("1 of 1")
    expect(page.locator(".find-scope")).to_contain_text("Earlier messages are not included yet")
    page.get_by_role("button", name="Include earlier messages").click()
    expect(page.locator(".find-count")).to_have_text("1 of 2")
    page.get_by_role("button", name="Next match").click()
    expect(page.locator(".find-count")).to_have_text("2 of 2")
    field.fill("🌿")
    expect(page.locator(".find-count")).to_have_text("1 of 1")
    field.fill("Earlier needle")
    expect(page.locator(".find-count")).to_have_text("1 of 1")
    expect(page.locator(".find-focus")).to_contain_text("Earlier needle")
    expect(page.locator(".find-focus")).to_be_in_viewport()
    screenshot(page, "conversation-find")
    field.press("Escape")
    expect(page.get_by_role("button", name="Find in session", exact=True)).to_be_focused()


def test_child_details_and_read_only_transcript(page, live_app):
    seed(live_app[1], count=0)
    page.reload()
    page.get_by_role("link", name="Design notes", exact=True).click()
    send(page, "Delegate this review")
    # Saving replaces the card, which would close it after this opens it.
    wait_for_store(page, "s => s.lives[s.active]?.persisted === true")
    card = page.locator(".tool-card").filter(has_text="Subagent")
    expect(card).to_have_count(1)
    card.locator("summary").click()
    expect(card).to_contain_text("hermes-fast")
    expect(card).to_contain_text("12.5s")
    expect(card).to_contain_text("$0.0024")
    card.locator(".child-facts").scroll_into_view_if_needed()
    screenshot(page, "child-agent-details")
    card.get_by_role("button", name="Open child session").click()
    expect(page.get_by_text("The child transcript is ready.", exact=True)).to_be_visible()
    expect(page.get_by_label("Message Hermes")).to_have_count(0)
    page.get_by_role("button", name="Session options", exact=True).click()
    expect(page.get_by_role("button", name="Pin session", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Delete session", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Close dialog").click()
    page.reload()
    expect(page.get_by_label("Message Hermes")).to_have_count(0)
    page.get_by_role("button", name="Back to parent session").click()
    expect(page.get_by_label("Message Hermes")).to_be_visible()
    expect(page.locator(".topbar-title")).to_have_text("Design notes")


@pytest.mark.parametrize(
    "authority,plugin",
    [(False, False), (True, False), (True, True)],
    ids=["agent", "bare", "plugin"],
)
def test_sessions_owned_by_other_apps_continue_in_a_branch(page, live_app, authority, plugin):
    peer = live_app[1]
    sid = seed(peer, count=2)
    peer.session_authority, peer.extension = authority, {} if plugin else None
    page.reload()
    page.get_by_role("link", name="Design notes", exact=True).click()
    expect(page.get_by_text("Message 001", exact=True)).to_be_visible()
    if not authority:
        # Before Hermes 0.22.1, API turns may continue any session.
        expect(page.get_by_label("Message Hermes")).to_be_visible()
        return
    expect(page.get_by_label("Message Hermes")).to_have_count(0)
    notice = "This conversation belongs to CLI. Hermes accepts new messages for it only there."
    expect(page.get_by_text(notice, exact=True)).to_be_visible()
    branch = page.get_by_role("button", name="Continue in a branch", exact=True)
    if not plugin:
        # A native fork would end the CLI's session, so it is not offered.
        expect(branch).to_have_count(0)
        page.get_by_role("button", name="Session options", exact=True).click()
        expect(page.get_by_role("button", name="Branch session", exact=True)).to_be_disabled()
        return
    screenshot(page, "external-session")
    branch.click()
    intro = "The branch is a copy you can continue here. The original stays in CLI."
    expect(page.get_by_text(intro, exact=True)).to_be_visible()
    page.get_by_role("button", name="Create branch", exact=True).click()
    expect(page.get_by_text("Session branched", exact=True)).to_be_visible()
    copy = next(s for s in peer.sessions.values() if s.get("parent_session_id") == sid)
    assert copy["source"] == "api_server" and "end_reason" not in peer.sessions[sid]
    send(page, "Continue here")
    assert [run["session_id"] for run in peer.runs.values()] == [copy["id"]]


NO_CHAT = "This session has no chat history."


def open_empty_session(page, peer, *, source="tui", rows=(), **fields):
    peer.session_authority, peer.extension = True, {}
    peer.sessions["empty"] = {"id": "empty", "title": None, "source": source, "pinned": False}
    peer.sessions["empty"].update(fields)
    peer.messages["empty"] = list(rows)
    page.reload()
    page.get_by_role("link", name="Untitled session", exact=True).click()


def menu_action(page, name):
    page.get_by_role("button", name="Session options", exact=True).click()
    return page.get_by_role("dialog").get_by_role("button", name=name, exact=True)


def test_session_with_nothing_saved_says_so_and_offers_a_new_session(page, live_app):
    peer = live_app[1]
    open_empty_session(page, peer)
    expect(page.get_by_text(NO_CHAT, exact=True)).to_be_visible()
    expect(
        page.get_by_text("Hermes has no saved messages from Terminal.", exact=True)
    ).to_be_visible()
    notice = "This conversation belongs to Terminal. Hermes accepts new messages for it only there."
    expect(page.get_by_text(notice, exact=True)).to_be_visible()
    expect(page.get_by_label("Message Hermes")).to_have_count(0)
    # There is nothing to continue, so a branch would only make an empty session.
    expect(page.get_by_role("button", name="Continue in a branch", exact=True)).to_have_count(0)
    screenshot(page, "no-chat-history")
    for width, name in ((390, "mobile"), (320, "narrow")):
        page.set_viewport_size({"width": width, "height": 700})
        expect(page.get_by_text(NO_CHAT, exact=True)).to_be_in_viewport()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        screenshot(page, f"no-chat-history-{name}")
    page.set_viewport_size({"width": 1280, "height": 800})
    page.emulate_media(color_scheme="dark")
    screenshot(page, "no-chat-history-dark")
    page.emulate_media(color_scheme="light")

    for name in ("Branch session", "Download transcript"):
        expect(menu_action(page, name)).to_be_disabled()
        page.keyboard.press("Escape")
    for name in ("Rename", "Delete session", "Session details"):
        expect(menu_action(page, name)).to_be_enabled()
        page.keyboard.press("Escape")

    page.get_by_role("button", name="Find in session", exact=True).click()
    page.get_by_label("Find text in session", exact=True).fill("anything")
    expect(page.get_by_text(NO_CHAT, exact=True)).to_be_visible()

    page.locator(".child-return").get_by_role("button", name="New session", exact=True).click()
    expect(page.get_by_label("Message Hermes")).to_be_visible()
    expect(page.get_by_text(NO_CHAT, exact=True)).to_have_count(0)
    assert list(peer.sessions) == ["empty"]


@pytest.mark.parametrize(
    "rows",
    [
        [{"id": 1, "role": "user", "content": "Hidden", "display_kind": "hidden"}],
        [{"id": 1, "role": "system", "content": "System prompt"}],
        [{"id": 1, "role": "session_meta", "content": "Metadata"}],
        [{"id": 1, "role": "tool", "tool_call_id": "gone", "content": "Orphaned result"}],
        [{"id": 1, "role": "assistant", "content": ""}],
    ],
    ids=["hidden", "system", "metadata", "orphan-tool", "blank-reply"],
)
def test_saved_rows_that_show_nothing_are_no_chat_history(page, live_app, rows):
    open_empty_session(page, live_app[1], rows=rows)
    expect(page.get_by_text(NO_CHAT, exact=True)).to_be_visible()
    expect(page.locator(".child-return").get_by_role("button", name="New session")).to_be_visible()


def test_a_failed_read_never_claims_a_session_is_empty(page, live_app):
    peer = live_app[1]
    peer.discovery_overrides["/api/sessions/empty/messages"] = ({"error": "Unavailable"}, 500)
    open_empty_session(page, peer)
    expect(page.locator(".error-banner")).to_be_visible()
    wait_for_store(page, "state => !state.loading")
    expect(page.get_by_text(NO_CHAT, exact=True)).to_have_count(0)
    expect(menu_action(page, "Download transcript")).to_be_enabled()
    page.keyboard.press("Escape")
    # Dismissing the error does not turn "unknown" into "empty" either.
    page.get_by_role("button", name="Dismiss error", exact=True).click()
    expect(page.get_by_text(NO_CHAT, exact=True)).to_have_count(0)

    del peer.discovery_overrides["/api/sessions/empty/messages"]
    page.evaluate("async () => (await import('/static/store.js')).refreshHistory('empty')")
    expect(page.get_by_text(NO_CHAT, exact=True)).to_be_visible()


@pytest.mark.parametrize("older_chat", [True, False], ids=["chat-behind", "hidden-all-the-way"])
def test_a_page_of_hidden_rows_does_not_hide_older_chat(page, live_app, older_chat):
    older = {"id": 1, "role": "user", "content": "Older question"}
    if not older_chat:
        older["display_kind"] = "hidden"
    hidden = [
        {"id": i + 2, "role": "user", "content": "Hidden", "display_kind": "hidden"}
        for i in range(100)
    ]
    open_empty_session(page, live_app[1], rows=[older, *hidden])
    load = page.get_by_role("button", name="Load earlier messages", exact=True)
    expect(load).to_be_visible()
    expect(page.get_by_text(NO_CHAT, exact=True)).to_have_count(0)
    load.click()
    expect(load).to_have_count(0)
    if older_chat:
        expect(page.get_by_text("Older question", exact=True)).to_be_visible()
        expect(page.get_by_text(NO_CHAT, exact=True)).to_have_count(0)
    else:
        expect(page.get_by_text(NO_CHAT, exact=True)).to_be_visible()


def test_an_empty_session_fills_in_when_its_app_saves_messages(page, live_app):
    peer = live_app[1]
    open_empty_session(page, peer)
    expect(page.get_by_text(NO_CHAT, exact=True)).to_be_visible()
    peer.messages["empty"] = [
        {"id": 1, "role": "user", "content": "Typed in the terminal"},
        {"id": 2, "role": "assistant", "content": "Answered there"},
    ]
    wait_for_timer(page, 15000)
    fire_timers(page, 15000)
    expect(page.get_by_text("Answered there", exact=True)).to_be_visible()
    expect(page.get_by_text(NO_CHAT, exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Continue in a branch", exact=True)).to_be_visible()


def test_an_empty_api_session_takes_its_first_message(page, live_app):
    peer = live_app[1]
    open_empty_session(page, peer, source="api_server")
    expect(page.get_by_text(NO_CHAT, exact=True)).to_be_visible()
    expect(page.get_by_text("Send a message to begin.", exact=True)).to_be_visible()
    screenshot(page, "no-chat-history-composer")
    expect(menu_action(page, "Branch session")).to_be_disabled()
    page.keyboard.press("Escape")
    page.get_by_label("Message Hermes").fill("hold this first message")
    page.get_by_role("button", name="Send message", exact=True).click()
    # Hermes has saved nothing yet, and the turn on screen is what the reader is following.
    expect(page.get_by_role("button", name="Stop response", exact=True)).to_be_visible()
    expect(page.get_by_text("hold this first message", exact=True)).to_be_visible()
    expect(page.get_by_text(NO_CHAT, exact=True)).to_have_count(0)
    peer.release()
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    expect(page.get_by_text(NO_CHAT, exact=True)).to_have_count(0)
    expect(menu_action(page, "Branch session")).to_be_enabled()


def test_a_new_session_never_shows_the_empty_notice_while_starting(page, live_app):
    peer = live_app[1]
    page.evaluate(
        """() => { window.sawNotice = false;
        new MutationObserver(() => { window.sawNotice ||= !!document.querySelector('.no-chat'); })
          .observe(document.body, { childList: true, subtree: true }); }"""
    )
    send(page, "A brand new session")
    assert len(peer.sessions) == 1
    assert page.evaluate("window.sawNotice") is False


def test_an_unopened_empty_session_explains_why_it_cannot_branch(page, live_app):
    peer = live_app[1]
    seed(peer, count=2)
    open_empty_session(page, peer)
    page.get_by_role("link", name="Design notes", exact=True).click()
    expect(page.get_by_text("Message 001", exact=True)).to_be_visible()
    # Nothing is known about a session until it opens, so its menu still offers this.
    page.get_by_role("button", name="Options for session", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Branch session", exact=True).click()
    page.get_by_role("button", name="Create branch", exact=True).click()
    message = "This session has no chat history to branch."
    expect(page.get_by_role("alert").get_by_text(message, exact=True)).to_be_visible()
    assert set(peer.sessions) == {"notes", "empty"}


def test_an_empty_child_session_still_leads_back_to_its_parent(page, live_app):
    peer = live_app[1]
    seed(peer)
    peer.sessions["empty"] = {"id": "empty", "source": "subagent", "parent_session_id": "notes"}
    peer.messages["empty"] = []
    # Hermes leaves subagents out of its session list; a parent's card opens them.
    page.evaluate(
        "async () => (await import('/static/store.js'))"
        ".openSession('empty', { id: 'notes', title: 'Design notes' })"
    )
    expect(page.get_by_text(NO_CHAT, exact=True)).to_be_visible()
    expect(page.locator(".no-chat p")).to_have_count(0)
    expect(page.get_by_role("button", name="Back to parent session", exact=True)).to_be_visible()


def test_saved_child_cards_use_native_tool_results_and_explain_missing_links(page, live_app):
    peer = live_app[1]
    sid = seed(peer, count=0)
    child = seed(peer, "saved-child", count=1)
    peer.sessions[child].update(
        source="subagent", parent_session_id=sid, model_config={"_delegate_from": sid}
    )
    result = {
        "task_index": 0,
        "status": "completed",
        "summary": "A saved child summary",
        "model": "child-model",
        "duration_seconds": 65.2,
        "cost_usd": 0.004,
        "cost_status": "estimated",
        "child_session_id": child,
    }
    peer.messages[sid] = [
        {"role": "user", "content": "Review the notes"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "delegate-1",
                    "function": {
                        "name": "delegate_task",
                        "arguments": json.dumps({"goal": "Review notes"}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "delegate-1",
            "content": json.dumps({"results": [result]}),
        },
        {"role": "assistant", "content": "The review is ready."},
    ]
    page.reload()
    page.get_by_role("link", name="Design notes", exact=True).click()
    card = page.locator(".tool-card").filter(has_text="Subagent")
    card.locator("summary").click()
    expect(card).to_contain_text("child-model")
    expect(card).to_contain_text("1m 5s")
    expect(card).to_contain_text("$0.004")
    card.get_by_role("button", name="Open child session").click()
    expect(page.get_by_text("Message 000", exact=True)).to_be_visible()
    expect(page.get_by_label("Message Hermes")).to_have_count(0)
    page.get_by_role("button", name="Back to parent session").click()
    result.pop("child_session_id")
    result.update(cost_usd=0, cost_status="unknown")
    peer.messages[sid][2]["content"] = json.dumps({"results": [result]})
    page.reload()
    card.locator("summary").click()
    expect(card).to_contain_text("Hermes did not include a child transcript link")
    expect(card.locator(".child-facts")).not_to_contain_text("$")
    expect(card.get_by_role("button", name="Open child session")).to_have_count(0)


def test_readiness_explains_issues_and_recovers(page, live_app):
    peer = live_app[1]
    peer.discovery_overrides["/health/detailed"] = (
        {
            "status": "degraded",
            "readiness": {
                "status": "degraded",
                "checks": {
                    "model": {"status": "degraded"},
                    "disk": {"status": "degraded", "used_percent": 94},
                },
            },
        },
        200,
    )
    page.reload()
    expect(page.get_by_label("Hermes readiness")).to_contain_text("No default model is configured")
    expect(page.get_by_label("Hermes readiness")).to_contain_text("94%")
    expect(page.get_by_text("Hermes needs attention", exact=True).first).to_be_visible()
    screenshot(page, "connection-readiness")
    del peer.discovery_overrides["/health/detailed"]
    page.get_by_role("button", name="Check again", exact=True).click()
    expect(page.get_by_label("Hermes readiness")).to_have_count(0)
    expect(page.get_by_text("Connected to Hermes", exact=True)).to_be_visible()

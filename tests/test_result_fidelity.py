"""Native tool content and outcomes remain faithful across settlement and reload."""

import asyncio
import json
import threading
from pathlib import Path

from playwright.sync_api import expect

from .conftest import wait_for_store
from .test_conversation_features import seed
from .test_session_continuity import open_session, refresh


def rows(output, offset=0):
    return [
        {"id": offset + 1, "role": "user", "content": "Same prompt"},
        {
            "id": offset + 2,
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call", "function": {"name": "terminal", "arguments": '{"command":"false"}'}}
            ],
        },
        {"id": offset + 3, "role": "tool", "tool_call_id": "call", "content": output},
        {"id": offset + 4, "role": "assistant", "content": "Finished checking"},
    ]


def test_result_keeps_error_and_exit_metadata_and_neutral_saved_status(page, live_app):
    peer = live_app[1]
    sid = seed(peer)
    values = [
        {"output": "", "error": "Synthetic failure", "exit_code": 1},
        {"output": "Partial output", "error": "Stopped early", "exit_code": 2},
        {"output": "The word error does not mean failure", "exit_code": 0},
        {"error": {"code": "unavailable"}, "output": "", "retryable": True},
    ]
    peer.messages[sid] = [
        row for i, value in enumerate(values) for row in rows(json.dumps(value), i * 4)
    ]
    open_session(page, sid)
    for card, value in zip(page.locator(".tool-card").all(), values, strict=True):
        expect(card.locator(".tool-status")).to_have_text("finished")
        expect(card.locator(".tool-status")).to_have_attribute(
            "title", "Outcome not reported by Hermes"
        )
        card.locator("summary").click()
        result = card.get_by_role("region", name="Tool result", exact=True)
        assert json.loads(result.text_content()) == value


def test_failure_outcome_belongs_only_to_its_settled_native_turn(page, live_app):
    peer = live_app[1]
    sid = seed(peer)
    peer.messages[sid] = rows("First result") + rows("Second result", 4)
    open_session(page, sid)
    page.evaluate(
        """async sid => {
      const s = await import('/static/store.js');
      const {applyEvent} = await import('/static/runs.js');
      const live = applyEvent({id:'run', status:'completed', text:'Finished checking',
        userText:'Same prompt', tools:[{id:'call', name:'terminal', status:'running'}]},
        {type:'tool.completed', tool_call_id:'call', error:true, duration:2});
      s.update({lives:{[sid]: {...live, persisted:true, savedMessageId:8}},
        history:[...s.state.history]});
    }""",
        sid,
    )
    statuses = page.locator(".tool-status")
    expect(statuses).to_have_text(["finished", "failed"])
    refresh(page, sid)
    expect(statuses).to_have_text(["finished", "failed"])
    # A different client's identical prompt and reused call ID are not this run.
    peer.messages[sid] += rows("Third result", 8)
    refresh(page, sid)
    expect(statuses).to_have_text(["finished", "failed", "finished"])
    page.reload()
    expect(statuses).to_have_text(["finished", "finished", "finished"])


def test_large_result_download_is_complete_literal_and_rendering_stays_bounded(page, live_app):
    peer = live_app[1]
    sid = seed(peer)
    raw = "<script>literal only</script>\r\n" + "🦋" * 40000 + "\nEND OF RESULT"
    peer.messages[sid] = rows(raw)
    open_session(page, sid)
    card = page.locator(".tool-card")
    card.locator("summary").click()
    result = card.get_by_role("region", name="Tool result", exact=True)
    assert result.evaluate("el => el.textContent.length") <= 20000
    expect(result.locator("script, .token")).to_have_count(0)
    expect(card.get_by_text("Preview shortened", exact=True)).to_be_visible()
    with page.expect_download() as downloaded:
        card.get_by_role("button", name="Download full result", exact=True).click()
    assert downloaded.value.suggested_filename == "tool-result.txt"
    assert Path(downloaded.value.path()).read_bytes().decode("utf-8") == raw
    # Refresh/reopen never substitutes the preview for the native result.
    refresh(page, sid)
    with page.expect_download() as downloaded:
        card.get_by_role("button", name="Download full result", exact=True).click()
    assert Path(downloaded.value.path()).read_bytes().decode("utf-8") == raw


def test_small_results_preserve_plain_malformed_and_single_payload_forms(page, live_app):
    peer = live_app[1]
    sid = seed(peer)
    values = [
        ("plain text", "plain text"),
        ('{"partial":', '{"partial":'),
        ('{"output":"hello"}', "hello"),
        ('{"output":""}', "No output"),
        ("[1,2]", "[\n  1,\n  2\n]"),
        ("0", "0"),
    ]
    peer.messages[sid] = [row for i, (raw, _) in enumerate(values) for row in rows(raw, i * 4)]
    open_session(page, sid)
    for card, (_, wanted) in zip(page.locator(".tool-card").all(), values, strict=True):
        card.locator("summary").click()
        assert card.get_by_role("region", name="Tool result", exact=True).text_content() == wanted
    expect(page.get_by_role("button", name="Download full result")).to_have_count(0)


def test_failed_tool_stream_settles_without_becoming_successful(page, live_app, monkeypatch):
    peer = live_app[1]
    sid = seed(peer)
    peer.messages[sid] = []
    release = threading.Event()

    async def events(run):
        def frame(event, **fields):
            return "data: " + json.dumps({"event": event, **fields}) + "\n\n"

        yield frame("tool.started", tool="terminal", tool_call_id="call")
        yield frame("tool.completed", tool="terminal", tool_call_id="call", error=True)
        assert await asyncio.to_thread(release.wait, 15)
        peer.messages[sid] = rows('{"output":"", "error":"Synthetic failure", "exit_code":1}')
        run.update(status="completed", output="Finished checking")
        yield frame("run.completed", output="Finished checking")

    monkeypatch.setattr(peer, "events", events)
    open_session(page, sid)
    page.get_by_label("Message Hermes").fill("Same prompt")
    page.get_by_label("Message Hermes").press("Enter")
    try:
        expect(page.locator(".tool-status")).to_have_text("failed")
    finally:
        release.set()
    wait_for_store(page, "s => s.lives[s.active]?.persisted === true")
    expect(page.locator(".tool-status")).to_have_text("failed")
    page.locator(".tool-card summary").click()
    expect(page.get_by_role("region", name="Tool result")).to_contain_text("Synthetic failure")
    page.reload()
    # The persisted native contract has no outcome field: do not invent success.
    expect(page.locator(".tool-status")).to_have_text("finished")
    page.locator(".tool-card summary").click()
    expect(page.get_by_role("region", name="Tool result")).to_contain_text("Synthetic failure")

"""Long-reply navigation and stable tool headings, using synthetic sessions only."""

import asyncio
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .conftest import wait_for_store
from .test_conversation_features import seed


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("motion", ["reduce", "no-preference"])
def test_scroll_shortcuts_stay_clear_of_text_and_fade_while_reading(page, width, motion):
    page.set_viewport_size({"width": width, "height": 844})
    page.emulate_media(reduced_motion=motion)
    page.evaluate("""async () => (await import('/static/store.js')).update({
      active:'reading',history:[],loading:false,lives:{reading:{id:'reading-run',status:'running',
        text:'## A long reply\\n\\n'+('A paragraph to read. '.repeat(20)+'\\n\\n').repeat(12),
        tools:[],userText:'Tell me more',baseHistoryLength:0}}})""")
    controls = page.locator(".conversation-jumps")
    start = page.get_by_role("button", name="Jump to start of reply", exact=True)
    expect(start).to_be_visible()
    expect(controls).to_have_css("opacity", "1")
    button_box = start.bounding_box()
    content_box = page.locator(".conversation-content").bounding_box()
    assert button_box["x"] >= content_box["x"] + content_box["width"]
    assert button_box["width"] == button_box["height"] == 36
    expect(controls).to_have_css("opacity", "0", timeout=4000)
    expect(controls).to_have_css("pointer-events", "none")
    # Automatic follow must not reveal the controls on every arriving token.
    page.evaluate("""async () => {
      const {state,update}=await import('/static/store.js');
      update({lives:{reading:{...state.lives.reading,
        text:state.lives.reading.text+'\\n\\nThe next part arrives.'}}});
    }""")
    expect(page.get_by_text("The next part arrives.", exact=True)).to_be_visible()
    expect(controls).to_have_css("opacity", "0")
    viewport = page.locator(".conversation-viewport")
    viewport.hover()
    page.mouse.wheel(0, -160)
    expect(controls).to_have_css("opacity", "1")
    expect(page.get_by_role("button", name="Jump to latest message")).to_be_visible()
    expect(controls).to_have_css("opacity", "0", timeout=4000)
    viewport.dispatch_event("pointerdown", {"pointerType": "touch"})
    expect(controls).to_have_css("opacity", "1")
    start.focus()
    expect(controls).to_have_class("conversation-jumps is-idle", timeout=4000)
    expect(controls).to_have_css("opacity", "1")
    page.keyboard.press("Enter")
    wait_at_reply_start(page, "A long reply")


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("motion", ["reduce", "no-preference"])
def test_reply_start_tracks_the_visible_reply_and_releases_stream_follow(page, width, motion):
    page.set_viewport_size({"width": width, "height": 844})
    page.emulate_media(reduced_motion=motion)
    page.evaluate("""async () => {
      const {update}=await import('/static/store.js');
      const long=heading=>'## '+heading+'\\n\\n'+
        ('A paragraph worth reading. '.repeat(12)+'\\n\\n').repeat(16);
      update({active:'jump-test',loading:false,history:[
        {id:1,role:'user',content:'Earlier question'},
        {id:2,role:'assistant',content:long('Earlier reply')},
        {id:3,role:'user',content:'Current question'},
      ],lives:{'jump-test':{id:'local-run',status:'running',text:long('Current reply'),
        tools:[],userText:'Current question',baseHistoryLength:3,baseUserId:3}}});
    }""")
    jump = page.get_by_role("button", name="Jump to start of reply", exact=True)
    expect(jump).to_be_visible()
    if motion == "reduce":
        Path("test-results").mkdir(exist_ok=True)
        page.screenshot(path=f"test-results/reply-start-{width}.png")
    jump.click()
    wait_at_reply_start(page, "Current reply")
    expect(jump).to_have_count(0)
    expect(page.get_by_role("button", name="Jump to latest message")).to_be_visible()
    top = page.locator(".conversation-viewport").evaluate("el=>el.scrollTop")
    page.evaluate("""async () => {
      const {state,update}=await import('/static/store.js');
      const live=state.lives['jump-test'];
      update({lives:{'jump-test':{...live,
        text:live.text+'\\n\\nMore streamed text. '.repeat(100)}}});
    }""")
    wait_for_store(
        page,
        """state => document.querySelector('.message.assistant:last-of-type')
      .textContent.includes('More streamed text.')""",
    )
    assert abs(page.locator(".conversation-viewport").evaluate("el=>el.scrollTop") - top) < 3
    # Navigate into the older reply: the shortcut must not target the active run.
    page.evaluate("""() => {
      const viewport=document.querySelector('.conversation-viewport');
      const reply=document.querySelector('.message.assistant');
      viewport.scrollTop+=reply.getBoundingClientRect().top-viewport.getBoundingClientRect().top
        +viewport.clientHeight;
    }""")
    expect(jump).to_be_visible()
    jump.focus()
    page.keyboard.press("Enter")
    wait_at_reply_start(page, "Earlier reply")
    expect(jump).to_have_count(0)
    page.get_by_role("button", name="Jump to latest message").click()
    page.wait_for_function("""() => {
      const el=document.querySelector('.conversation-viewport');
      return el.scrollHeight-el.scrollTop-el.clientHeight<3;
    }""")
    expect(jump).to_be_visible()
    # Reconciliation replaces the live article with native saved history.
    page.evaluate("""async () => {
      const {state,update}=await import('/static/store.js');
      const live=state.lives['jump-test'];
      update({history:[...state.history,{id:4,role:'assistant',content:live.text}],
        lives:{'jump-test':{...live,status:'completed',persisted:true,savedMessageId:4}}});
    }""")
    jump.click()
    wait_at_reply_start(page, "Current reply")
    # Navigating away disconnects the target and must not leave a stale shortcut.
    page.evaluate("""async () => (await import('/static/store.js')).update({
      active:'short',history:[{id:1,role:'assistant',content:'Short reply'}],lives:{}})""")
    expect(page.get_by_text("Short reply", exact=True)).to_be_visible()
    expect(jump).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")


def wait_at_reply_start(page, heading):
    page.wait_for_function(
        """heading => {
      const reply=[...document.querySelectorAll('.message.assistant')]
        .find(el=>el.querySelector('h2')?.textContent===heading);
      const viewport=document.querySelector('.conversation-viewport').getBoundingClientRect();
      const gap=reply.getBoundingClientRect().top-viewport.top;
      return Math.abs(gap-16)<1 && document.activeElement===reply;
    }""",
        arg=heading,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"file_path": "references/example.md", "name": "category/example"},
        {"command": "printf hello\nprintf world"},
    ],
)
def test_tool_name_survives_completion_and_reload(page, live_app, monkeypatch, arguments):
    peer = live_app[1]
    sid = seed(peer, "tool-heading")
    peer.messages[sid] = []
    finished = threading.Event()

    async def events(run):
        def frame(name, **fields):
            return "data: " + json.dumps({"event": name, **fields}) + "\n\n"

        yield frame("tool.started", tool="skill_manage", preview="Inspecting a skill")
        assert await asyncio.to_thread(finished.wait, 15)
        peer.messages[sid].extend(
            [
                {
                    "id": 2,
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {
                                "name": "skill_manage",
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
                {"id": 3, "role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'},
                {"id": 4, "role": "assistant", "content": "Finished inspecting."},
            ]
        )
        run.update(status="completed", output="Finished inspecting.")
        yield frame("tool.completed", tool="skill_manage")
        yield frame("run.completed", output="Finished inspecting.")

    monkeypatch.setattr(peer, "events", events)
    page.evaluate("async sid => (await import('/static/store.js')).openSession(sid)", sid)
    page.get_by_label("Message Hermes").fill("Inspect this skill")
    page.get_by_role("button", name="Send message", exact=True).click()
    label = page.locator(".tool-card .tool-label")
    try:
        expect(label).to_have_text("skill manage")
        page.locator(".tool-card summary").click()
        expect(page.get_by_role("region", name="Tool details")).to_have_text("Inspecting a skill")
        finished.set()
        wait_for_store(page, "state=>state.lives[state.active]?.persisted")
        expect(label).to_have_text("skill manage")
        page.reload()
        expect(label).to_have_text("skill manage")
        page.locator(".tool-card summary").click()
        details = page.get_by_role("region", name="Tool details")
        expect(details).to_be_visible()
        for value in arguments.values():
            expect(details).to_contain_text(value)
        expect(page.get_by_role("region", name="Tool result")).to_contain_text('"ok": true')
    finally:
        finished.set()


def test_reply_start_appears_as_stream_grows_and_updates_on_resize(page):
    page.emulate_media(reduced_motion="reduce", color_scheme="dark")
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""async () => (await import('/static/store.js')).update({
      active:'growing',history:[],loading:false,lives:{growing:{id:'grow',status:'running',
        text:'A short start.',tools:[],userText:'Tell me more',baseHistoryLength:0}}})""")
    jump = page.get_by_role("button", name="Jump to start of reply", exact=True)
    expect(page.get_by_text("A short start.", exact=True)).to_be_visible()
    expect(jump).to_have_count(0)
    page.evaluate("""async () => {
      const {state,update}=await import('/static/store.js');
      update({lives:{growing:{...state.lives.growing,
        text:'## Growing reply\\n\\n'+('More to read. '.repeat(15)+'\\n\\n').repeat(12)}}});
    }""")
    expect(jump).to_be_visible()
    # A tall viewport can fit the reply: no unnecessary navigation control.
    page.set_viewport_size({"width": 1440, "height": 4000})
    expect(jump).to_have_count(0)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(jump).to_be_visible()
    jump.click()
    wait_at_reply_start(page, "Growing reply")
    expect(jump).to_have_count(0)

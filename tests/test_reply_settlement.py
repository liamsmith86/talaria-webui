"""A terminal event and native-history reconciliation settle the existing reply in place."""

import json

import pytest
from playwright.sync_api import expect

from .conftest import wait_for_store


def start_reply(page, live_app, text, *, prefix="", reasoning=""):
    peer = live_app[1]
    peer.sessions["settling"] = {
        "id": "settling",
        "title": "Settling reply",
        "source": "api_server",
    }
    rows = [{"id": 1, "role": "user", "content": "Read this reply"}]
    events = []
    if prefix:
        rows.extend(
            [
                {
                    "id": 2,
                    "role": "assistant",
                    "content": prefix,
                    "tool_calls": [
                        {
                            "id": "a",
                            "function": {
                                "name": "terminal",
                                "arguments": json.dumps({"command": "date"}),
                            },
                        }
                    ],
                },
                {"id": 3, "role": "tool", "tool_call_id": "a", "content": "Saved tool result"},
            ]
        )
        events.extend(
            [
                {"event": "message.delta", "delta": prefix},
                {
                    "event": "tool.started",
                    "tool": "terminal",
                    "tool_call_id": "a",
                    "preview": "date",
                },
                {"event": "tool.completed", "tool_call_id": "a"},
            ]
        )
    if reasoning:
        events.append({"event": "reasoning.available", "text": reasoning})
    rows.append(
        {"id": len(rows) + 1, "role": "assistant", "content": text, "reasoning_content": reasoning}
    )
    peer.messages["settling"] = rows
    events.append({"event": "message.delta", "delta": text})
    page.evaluate(
        """async events => {
      const {update}=await import('/static/store.js');
      const {applyEvent,subscribe}=await import('/static/runs.js');
      let live={id:'settling-run',requestId:'settling-request',status:'running',
        text:'',tools:[],userText:'Read this reply',baseHistoryLength:0,baseMessageId:0};
      for(const event of events) live=applyEvent(live,event);
      update({active:'settling',history:[],loading:false,historyHasMore:false,
        sessions:[{id:'settling',title:'Settling reply',source:'api_server'}],
        lives:{settling:live}});
      const original=window.EventSource;
      window.EventSource=class {
        constructor() {window.settlingSource=this;}
        close() {}
      };
      try {subscribe('settling');} finally {window.EventSource=original;}
    }""",
        events,
    )
    expect(page.locator(".message.assistant")).to_be_visible()
    if text:
        expect(page.locator(".message.assistant .message-text").last).to_contain_text(text[:20])
    # Only the initial entrance animation may run. Sample completion with normal motion enabled.
    page.wait_for_function("""() => [...document.querySelectorAll('.message')]
      .every(node=>node.getAnimations({subtree:true})
        .every(animation=>['breathe','rotate'].includes(animation.animationName)))""")


def finish_reply(page, text):
    page.evaluate(
        """output => settlingSource.onmessage({data:JSON.stringify({
      event:'run.completed',output
    })})""",
        text,
    )


@pytest.mark.parametrize("mobile", [False, True])
@pytest.mark.parametrize("following", [False, True])
def test_finishing_reply_keeps_each_frame_at_the_reading_position(
    page, live_app, mobile, following
):
    if mobile:
        page.set_viewport_size({"width": 390, "height": 844})
    text = "\n\n".join(f"Paragraph {i}. " + "Readable detail. " * 8 for i in range(30))
    start_reply(page, live_app, text)
    page.wait_for_function("""() => {
      const el=document.querySelector('.conversation-viewport');
      return el.scrollHeight-el.scrollTop-el.clientHeight<3;
    }""")
    page.evaluate(
        """following => {
      const viewport=document.querySelector('.conversation-viewport');
      const paragraphs=document.querySelectorAll('.message.assistant p');
      window.settlingAnchor=following ? paragraphs[paragraphs.length-1] : paragraphs[14];
      if(!following) {
        viewport.scrollTop+=settlingAnchor.getBoundingClientRect().top-
          viewport.getBoundingClientRect().top-40;
        viewport.dispatchEvent(new Event('scroll'));
      }
    }""",
        following,
    )
    if not following:
        expect(page.get_by_role("button", name="Jump to latest message")).to_be_visible()
    routes = []
    page.route("**/api/sessions/settling/messages?*", lambda route: routes.append(route))
    page.evaluate("""() => {
      const viewport=document.querySelector('.conversation-viewport');
      window.settlingFrames=[];
      window.settlingBefore={top:viewport.scrollTop,y:settlingAnchor.getBoundingClientRect().top};
      window.recordSettling=true;
      function sample() {
        settlingFrames.push({top:viewport.scrollTop,y:settlingAnchor.getBoundingClientRect().top,
          connected:settlingAnchor.isConnected});
        if(recordSettling) requestAnimationFrame(sample);
      }
      requestAnimationFrame(sample);
    }""")
    finish_reply(page, text)
    wait_for_store(page, "s => s.lives.settling.status === 'completed'")
    # Hold the history response to distinguish terminal layout shifts from replacement flashes.
    page.wait_for_timeout(100)
    assert routes
    for route in routes:
        route.continue_()
    wait_for_store(page, "s => s.lives.settling.persisted")
    page.wait_for_timeout(300)
    result = page.evaluate("""() => {
      recordSettling=false;
      return {disconnected:settlingFrames.some(frame=>!frame.connected),
        scrollShift:Math.max(...settlingFrames.map(frame=>Math.abs(frame.top-settlingBefore.top))),
        anchorShift:Math.max(...settlingFrames.map(frame=>Math.abs(frame.y-settlingBefore.y)))};
    }""")
    assert result == {"disconnected": False, "scrollShift": 0, "anchorShift": 0}


def test_saved_turn_keeps_tools_code_selection_and_identity_across_the_next_run(page, live_app):
    prefix = (
        'A selectable introduction.\n\n```javascript\nconst wide = "' + "word " * 150 + '";\n```'
    )
    text = "The final answer."
    start_reply(page, live_app, text, prefix=prefix, reasoning="A considered answer.")
    page.locator(".tool-card summary").click()
    page.locator(".reasoning summary").click()
    page.evaluate("""() => {
      const reply=document.querySelector('.message.assistant');
      const pre=reply.querySelector('pre');
      pre.focus({preventScroll:true});pre.scrollLeft=100;
      const paragraph=reply.querySelector('p');
      const range=document.createRange();range.selectNodeContents(paragraph);
      getSelection().removeAllRanges();getSelection().addRange(range);
      window.settlingNodes={reply,user:document.querySelector('.message.user'),pre,paragraph,
        tool:reply.querySelector('.tool-card'),reasoning:reply.querySelector('.reasoning')};
      window.settlingSelection=getSelection().toString();
    }""")
    finish_reply(page, text)
    wait_for_store(page, "s => s.lives.settling.persisted")
    result = page.evaluate("""() => ({
      retained:Object.values(settlingNodes).every(node=>node.isConnected),
      expanded:settlingNodes.tool.open && settlingNodes.reasoning.open,
      focus:document.activeElement===settlingNodes.pre,
      selection:getSelection().toString()===settlingSelection,
      scroll:settlingNodes.pre.scrollLeft,
      animations:settlingNodes.reply.getAnimations().length,
      result:settlingNodes.tool.textContent.includes('Saved tool result')
    })""")
    assert result == {
        "retained": True,
        "expanded": True,
        "focus": True,
        "selection": True,
        "scroll": 100,
        "animations": 0,
        "result": True,
    }
    # Replacing the cached run must not change the keys assigned to the earlier native turn.
    page.evaluate("""async () => {
      const {update}=await import('/static/store.js');
      update({lives:{settling:{id:'next-run',status:'running',tools:[],text:'Next reply',
        userText:'Next question',baseUserId:1,baseMessageId:4,baseHistoryLength:4}}});
    }""")
    expect(page.locator(".message.assistant")).to_have_count(2)
    assert page.evaluate("Object.values(settlingNodes).every(node=>node.isConnected)")


def test_native_reconciliation_preserves_visible_prose_when_unsaved_reasoning_disappears(
    page, live_app
):
    text = "\n\n".join(f"Paragraph {i}. " + "A useful explanation. " * 5 for i in range(30))
    text += "\n\n[The saved reference][source]"
    start_reply(page, live_app, text, reasoning="Thinking through the answer.\n\n" * 12)
    page.locator(".reasoning summary").click()
    page.evaluate("""() => {
      const viewport=document.querySelector('.conversation-viewport');
      window.settlingAnchor=document.querySelectorAll('.message-text p')[14];
      viewport.scrollTop+=settlingAnchor.getBoundingClientRect().top-
        viewport.getBoundingClientRect().top-40;
      viewport.dispatchEvent(new Event('scroll'));
    }""")
    expect(page.get_by_role("button", name="Jump to latest message")).to_be_visible()
    page.evaluate("""() => {
      window.settlingBefore=settlingAnchor.getBoundingClientRect().top;
      window.settlingPositions=[];
      window.recordSettling=true;
      function sample() {
        settlingPositions.push(settlingAnchor.getBoundingClientRect().top);
        if(recordSettling) requestAnimationFrame(sample);
      }
      requestAnimationFrame(sample);
    }""")
    # Hermes may omit transient reasoning and provide Markdown definitions only in the saved row.
    final = live_app[1].messages["settling"][-1]
    del final["reasoning_content"]
    final["content"] += "\n\n[source]: https://example.com/saved"
    finish_reply(page, text)
    wait_for_store(page, "s => s.lives.settling.persisted")
    expect(page.locator(".reasoning")).to_have_count(0)
    expect(page.get_by_role("link", name="The saved reference")).to_have_attribute(
        "href", "https://example.com/saved"
    )
    page.wait_for_timeout(250)
    result = page.evaluate("""() => {
      recordSettling=false;
      return {retained:settlingAnchor.isConnected,
        shift:Math.max(...settlingPositions.map(top=>Math.abs(top-settlingBefore)))};
    }""")
    assert result["retained"]
    # Browser scroll offsets can round the fractional height of removed content.
    assert result["shift"] <= 0.5


def test_tool_only_completion_uses_the_same_reserved_footer(page, live_app):
    start_reply(page, live_app, "")
    calls = [
        {"id": f"tool-{i}", "function": {"name": "terminal", "arguments": "{}"}} for i in range(20)
    ]
    live_app[1].messages["settling"][-1]["tool_calls"] = calls
    page.evaluate(
        """async calls => {
      const {state,update}=await import('/static/store.js');
      const {applyEvent}=await import('/static/runs.js');
      let live=state.lives.settling;
      for(const call of calls) {
        live=applyEvent(live,{event:'tool.started',tool:'terminal',tool_call_id:call.id});
        live=applyEvent(live,{event:'tool.completed',tool_call_id:call.id});
      }
      update({lives:{settling:live}});
    }""",
        calls,
    )
    page.wait_for_function("""() => {
      const el=document.querySelector('.conversation-viewport');
      return el.scrollHeight>el.clientHeight && el.scrollHeight-el.scrollTop-el.clientHeight<3;
    }""")
    before = page.locator(".conversation-viewport").evaluate("el=>el.scrollTop")
    finish_reply(page, "")
    wait_for_store(page, "s=>s.lives.settling.persisted")
    page.wait_for_timeout(150)
    expect(page.locator(".tool-card")).to_have_count(20)
    assert page.locator(".conversation-viewport").evaluate("el=>el.scrollTop") == before

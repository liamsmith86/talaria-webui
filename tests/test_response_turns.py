"""Turn presentation follows Hermes ordering across tool rounds and cancellation."""

import asyncio
import json

import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize("cancelled", [False, True])
def test_tool_rounds_settle_to_one_ordered_response(page, live_app, monkeypatch, cancelled):
    peer = live_app[1]
    peer.extension = {}

    async def events(run):
        def frame(name, **fields):
            return "data: " + json.dumps({"event": name, **fields}) + "\n\n"

        yield frame("message.delta", delta="Before the first tool.")
        yield frame("tool.started", tool="terminal", preview="date", tool_call_id="a")
        yield frame("tool.completed", tool="terminal", tool_call_id="a")
        yield frame("message.delta", delta="Between the tools.")
        yield frame("tool.started", tool="terminal", preview="uname", tool_call_id="b")
        yield frame("tool.completed", tool="terminal", tool_call_id="b")
        yield frame("reasoning.available", text="Considering the final answer.")
        # Let the test inspect live order before completing or stopping the run.
        ready = asyncio.Event()
        loop = asyncio.get_running_loop()
        peer.finish_rounds = lambda: loop.call_soon_threadsafe(ready.set)
        await asyncio.wait_for(ready.wait(), 15)
        final = (
            "Operation interrupted: waiting for model response (3.5s elapsed)."
            if run["status"] == "cancelled" else "The final answer."
        )
        messages = peer.messages[run["session_id"]]
        for text, call, command in [
            ("Before the first tool.", "a", "date"),
            ("Between the tools.", "b", "uname"),
        ]:
            messages.append({
                "id": len(messages) + 1, "role": "assistant", "content": text,
                "tool_calls": [{"id": call, "function": {
                    "name": "terminal", "arguments": json.dumps({"command": command}),
                }}],
            })
            messages.append({
                "id": len(messages) + 1, "role": "tool", "tool_call_id": call, "content": "done",
            })
        messages.append({"id": len(messages) + 1, "role": "assistant", "content": final})
        if run["status"] == "cancelled":
            yield frame("run.cancelled")  # Native Hermes omits output and usage here.
        else:
            yield frame("message.delta", delta=final)
            run.update(status="completed", output=final)
            yield frame("run.completed", output=final)

    monkeypatch.setattr(peer, "events", events)
    page.reload()
    page.get_by_label("Message Hermes").fill("Show ordered tool rounds")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.locator(".reasoning")).to_be_visible()
    order = page.locator(".message.assistant").evaluate("""el => [...el.children]
      .filter(node => node.matches('.message-text,.tool-stack'))
      .map(node => node.className)""")
    assert order == ["message-text", "tool-stack", "message-text", "tool-stack"]
    if cancelled:
        page.get_by_role("button", name="Stop response", exact=True).click()
        page.wait_for_function("""async () => {
          const {state}=await import('/static/store.js');
          return state.lives[state.active]?.status === 'stopping';
        }""")
    peer.finish_rounds()
    page.wait_for_function("""async () => {
      const {state}=await import('/static/store.js');return state.lives[state.active]?.persisted;
    }""")
    response = page.locator(".message.assistant")
    expect(response).to_have_count(1)
    expect(response.locator(".tool-card")).to_have_count(2)
    expect(response.get_by_role("button", name="Response details", exact=True)).to_have_count(1)
    expect(response.get_by_role("button", name="Regenerate response", exact=True)).to_have_count(1)
    expect(response.get_by_role("button", name="Delete turn", exact=True)).to_have_count(1)
    text = response.inner_text()
    assert text.index("Before the first") < text.index("date") < text.index("Between the tools")
    assert text.index("Between the tools") < text.index("uname")
    ending = "Operation interrupted" if cancelled else "The final answer"
    assert text.index("uname") < text.index(ending)
    if cancelled:
        page.route("**/response?message_id=6", lambda route: route.fulfill(
            json={"message_id": 6},
        ))
    response.get_by_role("button", name="Response details", exact=True).click()
    assert page.evaluate("""async () => {
      const {state}=await import('/static/store.js');return state.modal.message.id;
    }""") == 6
    if cancelled:
        expect(page.get_by_role("dialog")).to_contain_text("This response was interrupted")
        expect(page.get_by_role("dialog")).to_contain_text("Not reported")
    page.get_by_role("button", name="Close dialog").click()
    page.reload()
    expect(page.locator(".message.assistant")).to_have_count(1)
    expect(page.locator(".message.assistant .tool-card")).to_have_count(2)
    page.locator(".message.assistant").get_by_role("button", name="Delete turn", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Delete turn", exact=True).click()
    expect(page.locator(".message")).to_have_count(0)
    assert peer.rewinds == 1


def test_settlement_does_not_adopt_an_older_identical_prompt(page):
    assert page.evaluate("""async () => {
      const {responseSaved}=await import('/static/runs.js');
      const history=[{id:1,role:'user',content:'Again'},
        {id:2,role:'assistant',content:'Earlier answer'}];
      const live={baseUserId:1,userText:'Again',status:'cancelled',text:'Partial'};
      return !responseSaved(history,live) && !responseSaved([...history,
        {id:3,role:'user',content:'Again'}],live) && responseSaved([...history,
        {id:3,role:'user',content:'Again'},
        {id:4,role:'assistant',content:'Operation interrupted.'}],live);
    }""")


def test_terminal_text_preserves_earlier_rounds_and_closes_working_tools(page):
    result = page.evaluate("""async () => {
      const {applyEvent,responseSaved}=await import('/static/runs.js');
      let live={text:'',tools:[],status:'running'};
      for(const event of [
        {event:'message.delta',delta:'Earlier prose'},
        {event:'tool.started',tool:'terminal',tool_call_id:'a'},
        {event:'tool.completed',tool_call_id:'a'},
        {event:'message.delta',delta:'Final ans'},
        {event:'run.completed',output:'Final answer'}]) live=applyEvent(live,event);
      const completed=live.parts.map(part=>part.text||part.id);
      const recovered=responseSaved([{id:1,role:'user',content:'Ask'},
        {id:2,role:'assistant',content:'Final answer'}],{...live,userText:'Ask'});
      live=applyEvent({...live,status:'running'},
        {event:'tool.started',tool:'terminal',tool_call_id:'b'});
      live=applyEvent(live,{event:'run.cancelled'});
      return {completed,recovered,statuses:live.tools.map(tool=>tool.status)};
    }""")
    assert result == {"completed": ["Earlier prose", "a", "Final answer"],
                      "recovered": True, "statuses": ["completed", "cancelled"]}


def test_late_reasoning_does_not_split_or_duplicate_the_current_markdown(page):
    result = page.evaluate("""async () => {
      const {applyEvent}=await import('/static/runs.js');
      let live={text:'',tools:[],status:'running'};
      for(const event of [
        {event:'message.delta',delta:'**One '},
        {event:'reasoning.available',text:'Thinking'},
        {event:'message.delta',delta:'paragraph**'},
        {event:'reasoning.available',text:'Final thinking'},
        {event:'run.completed',output:'**One paragraph**'}]) live=applyEvent(live,event);
      return live.parts;
    }""")
    assert result == [{"kind": "reasoning", "text": "Final thinking"},
                      {"kind": "text", "text": "**One paragraph**"}]

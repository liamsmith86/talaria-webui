"""Turn presentation follows Hermes ordering across tool rounds and cancellation."""

import asyncio
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .conftest import wait_for_store


@pytest.mark.parametrize("chunk_size", [31, 4096])
def test_native_completed_trace_reconciles_with_fragmented_or_batched_delivery(
    page,
    live_app,
    monkeypatch,
    chunk_size,
):
    trace = json.loads((Path(__file__).with_name("fixtures") / "hermes-completed.json").read_text())
    peer = live_app[1]

    async def events(run):
        peer.messages[run["session_id"]] = [
            {"id": index + 1, **row} for index, row in enumerate(trace["messages"])
        ]
        run.update(status="completed", output=trace["messages"][-1]["content"])
        wire = "".join("data: " + json.dumps(event) + "\n\n" for event in trace["events"])
        for offset in range(0, len(wire), chunk_size):
            yield wire[offset : offset + chunk_size]
            await asyncio.sleep(0.005)

    monkeypatch.setattr(peer, "events", events)
    page.get_by_label("Message Hermes").fill(trace["messages"][0]["content"])
    page.get_by_role("button", name="Send message", exact=True).click()
    wait_for_store(page, "state => state.lives[state.active]?.persisted")
    expect(page.locator(".message.assistant")).to_have_count(1)
    expect(page.locator(".message.assistant .message-text")).to_have_text("Fixture reply.")
    page.reload()
    expect(page.locator(".message.assistant")).to_have_count(1)
    expect(page.locator(".message.assistant .message-text")).to_have_text("Fixture reply.")


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
            if run["status"] == "cancelled"
            else "The final answer."
        )
        messages = peer.messages[run["session_id"]]
        for text, call, command in [
            ("Before the first tool.", "a", "date"),
            ("Between the tools.", "b", "uname"),
        ]:
            messages.append(
                {
                    "id": len(messages) + 1,
                    "role": "assistant",
                    "content": text,
                    "tool_calls": [
                        {
                            "id": call,
                            "function": {
                                "name": "terminal",
                                "arguments": json.dumps({"command": command}),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "id": len(messages) + 1,
                    "role": "tool",
                    "tool_call_id": call,
                    "content": "done",
                }
            )
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
        wait_for_store(page, "state => state.lives[state.active]?.status === 'stopping'")
    peer.finish_rounds()
    wait_for_store(page, "state => state.lives[state.active]?.persisted")
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
        page.route(
            "**/response?message_id=6",
            lambda route: route.fulfill(
                json={"message_id": 6},
            ),
        )
    response.get_by_role("button", name="Response details", exact=True).click()
    assert (
        page.evaluate("""async () => {
      const {state}=await import('/static/store.js');return state.modal.message.id;
    }""")
        == 6
    )
    if cancelled:
        expect(page.locator(".response-facts > div").filter(has_text="Status")).to_contain_text(
            "Interrupted"
        )
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
    assert result == {
        "completed": ["Earlier prose", "a", "Final answer"],
        "recovered": True,
        "statuses": ["completed", "cancelled"],
    }


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
    assert result == [
        {"kind": "reasoning", "text": "Final thinking"},
        {"kind": "text", "text": "**One paragraph**"},
    ]


def test_expired_live_updates_use_the_same_saved_turn_reconciliation(page, live_app):
    peer = live_app[1]
    peer.sessions["expired"] = {"id": "expired", "title": "Expired updates", "source": "api_server"}
    peer.messages["expired"] = [
        {"id": 3, "role": "user", "content": "Question"},
        {"id": 4, "role": "assistant", "content": "The saved answer"},
    ]
    page.evaluate("""async () => {
      const {update}=await import('/static/store.js');
      const {subscribe}=await import('/static/runs.js');
      update({active:'expired',loading:false,history:[],lives:{expired:{id:'expired-run',
        status:'running',text:'The saved',userText:'Question',baseUserId:1,baseMessageId:2,
        tools:[{id:'unfinished',name:'terminal',status:'running'}]}}});
      const original=window.EventSource;
      window.EventSource=class {
        constructor(){window.expiredSource=this}
        close(){this.closed=true}
      };
      try {subscribe('expired')} finally {window.EventSource=original}
      expiredSource.onmessage({data:JSON.stringify({event:'talaria.unavailable',
        message:'Live updates expired.'})});
    }""")
    wait_for_store(page, "state => state.lives.expired.persisted")
    expect(page.locator(".message.assistant")).to_have_count(1)
    expect(page.locator(".message.assistant")).to_contain_text("The saved answer")
    assert page.evaluate("expiredSource.closed")
    assert (
        page.evaluate("""async () => (await import('/static/store.js'))
      .state.lives.expired.tools[0].status""")
        == "not_reported"
    )
    page.get_by_role("button", name="Response details", exact=True).click()
    assert (
        page.evaluate("""async () => (await import('/static/store.js'))
      .state.modal.message.responseStatus""")
        is None
    )


def test_saved_turn_can_be_recognized_when_its_user_row_is_on_an_older_page(page):
    assert page.evaluate("""async () => {
      const {responseSaved}=await import('/static/runs.js');
      const history=[{id:150,role:'assistant',tool_calls:[{id:'tool'}]},
        {id:151,role:'tool',content:'result'},
        {id:152,role:'assistant',content:'Finished a long turn'}];
      const live={status:'completed',baseMessageId:10,baseUserId:9,userText:'Long task',
        text:'Partial preview'};
      return responseSaved(history,live) &&
        !responseSaved(history,{...live,baseMessageId:152}) &&
        !responseSaved(history,{...live,baseMessageId:undefined}) &&
        !responseSaved(history,{...live,status:'running'});
    }""")


def test_cached_run_status_stays_with_its_saved_message_after_history_grows(page):
    page.evaluate("""async () => {
      const {update}=await import('/static/store.js');
      update({active:'extended',loading:false,history:[
        {id:1,role:'user',content:'First'}, {id:2,role:'assistant',content:'Stopped first reply'},
        {id:3,role:'user',content:'Second'},
        {id:4,role:'assistant',content:'New reply from another tab'}],
        lives:{extended:{id:'old-run',persisted:true,savedMessageId:2,status:'cancelled',
          userText:'First',text:'Stopped first reply',tools:[{id:'old-child',kind:'agent',
          name:'Earlier child',status:'completed'}]}}});
    }""")
    expect(page.locator(".completed-children")).to_have_count(0)
    details = page.get_by_role("button", name="Response details", exact=True)
    details.first.click()
    assert (
        page.evaluate("""async () => (await import('/static/store.js'))
      .state.modal.message.responseStatus""")
        == "cancelled"
    )
    page.get_by_role("button", name="Close dialog").click()
    details.last.click()
    assert (
        page.evaluate("""async () => (await import('/static/store.js'))
      .state.modal.message.responseStatus""")
        is None
    )


def test_prior_delegation_does_not_hide_current_unsaved_child_details(page):
    page.evaluate("""async () => {
      const {update}=await import('/static/store.js');
      update({active:'delegations',loading:false,history:[
        {id:1,role:'user',content:'Old task'},
        {id:2,role:'assistant',content:'',tool_calls:[{id:'old-tool',function:{
          name:'delegate_task',arguments:'{"goal":"Old task"}'}}]},
        {id:3,role:'tool',tool_call_id:'old-tool',
          content:'{"task_index":0,"summary":"Old result"}'},
        {id:4,role:'assistant',content:'Old reply'},
        {id:5,role:'user',content:'New task'}, {id:6,role:'assistant',content:'New reply'}],
        lives:{delegations:{id:'new-run',persisted:true,savedMessageId:6,status:'completed',
          userText:'New task',text:'New reply',tools:[{id:'new-child',kind:'agent',
          name:'New task',preview:'Current child result',status:'completed'}]}}});
    }""")
    expect(page.locator(".tool-card")).to_have_count(2)
    expect(page.locator(".completed-children")).to_contain_text("New task")

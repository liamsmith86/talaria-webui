import asyncio
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .conftest import wait_for_store
from .test_app import signed_in


def question_stream(peer, *, multi=False):
    async def events(run):
        def frame(event, **fields):
            return "data: " + json.dumps({"event": event, **fields}) + "\n\n"

        yield frame("talaria.status", kind="lifecycle", text="Preparing the request")
        yield frame("talaria.reasoning.delta", block_id="one", delta="First reasoning ")
        yield frame("talaria.reasoning.delta", block_id="one", delta="continues.")
        yield frame("message.delta", delta="Let me check which environment you want.")
        yield frame("tool.started", tool="clarify", tool_call_id="question-tool")
        prompt = {
            "request_id": "question-one",
            "question": "Which environment should I use?",
            "choices": ["Development (Recommended)", "Production"],
            "multi_select": multi,
        }
        run.update(status="waiting_for_input", clarification=prompt)
        yield frame("talaria.clarification.request", **prompt)
        for _ in range(3000):
            if run.get("answers") or run["status"] == "cancelled":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Question did not receive an answer")
        if run["status"] == "cancelled":
            yield frame("run.cancelled")
            return
        yield frame("talaria.clarification.resolved", request_id=prompt["request_id"])
        yield frame("tool.completed", tool="clarify", tool_call_id="question-tool")
        yield frame("talaria.reasoning.delta", block_id="two", delta="Second reasoning block.")
        yield frame("message.delta", delta="Your answer was received.")
        run.update(status="completed", output="Your answer was received.")
        rows = peer.messages[run["session_id"]]
        rows.append({"id": len(rows) + 1, "role": "assistant", "content": run["output"]})
        yield frame("run.completed", output=run["output"])

    return events


@pytest.mark.parametrize("width", [390, 1440])
def test_question_choices_survive_reload_and_submit_once(page, live_app, monkeypatch, width):
    peer = live_app[1]
    peer.extension = {"live_interactions": True}
    monkeypatch.setattr(peer, "events", question_stream(peer))
    page.set_viewport_size({"width": width, "height": 850})
    page.get_by_label("Message Hermes").fill("Ask me which environment")
    page.get_by_role("button", name="Send message", exact=True).click()
    card = page.get_by_role("region", name="Question from your agent")
    expect(card).to_be_visible()
    expect(page.locator(".reasoning")).to_have_count(1)
    page.locator(".reasoning summary").click()
    expect(page.locator(".reasoning")).to_contain_text("First reasoning continues.")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    directory = Path("test-results")
    directory.mkdir(exist_ok=True)
    page.screenshot(path=str(directory / f"clarification-{width}.png"))
    page.reload()
    expect(card).to_be_visible()
    button = card.get_by_role("button", name="Development (Recommended)", exact=True)
    button.evaluate("button => { button.click(); button.click(); }")
    expect(card).to_have_count(0)
    wait_for_store(page, "state => state.lives[state.active]?.status === 'completed'")
    assert next(iter(peer.runs.values()))["answers"] == ["Development (Recommended)"]


@pytest.mark.parametrize("mode", ["text", "multi", "composer", "skip", "stop"])
def test_question_answer_modes(page, live_app, monkeypatch, mode):
    peer = live_app[1]
    peer.extension = {"live_interactions": True}
    monkeypatch.setattr(peer, "events", question_stream(peer, multi=mode == "multi"))
    page.get_by_label("Message Hermes").fill("Ask a question")
    page.get_by_role("button", name="Send message", exact=True).click()
    card = page.get_by_role("region", name="Question from your agent")
    expect(card).to_be_visible()
    if mode == "multi":
        card.get_by_role("checkbox", name="Development (Recommended)").check()
        card.get_by_role("checkbox", name="Production", exact=True).check()
        card.get_by_role("button", name="Send answer").click()
    elif mode == "text":
        card.get_by_label("Or write an answer").fill("A separate staging environment")
        card.get_by_role("button", name="Send answer").click()
    elif mode == "composer":
        page.get_by_label("Message Hermes").fill("Use staging instead")
        page.get_by_label("Message Hermes").press("Enter")
    elif mode == "skip":
        card.get_by_role("button", name="Skip", exact=True).click()
    else:
        page.get_by_role("button", name="Stop response", exact=True).click()
    expect(card).to_have_count(0)
    wait_for_store(
        page, "state => ['completed', 'cancelled'].includes(state.lives[state.active]?.status)"
    )
    run = next(iter(peer.runs.values()))
    if mode == "multi":
        assert json.loads(run["answers"][0]) == ["Development (Recommended)", "Production"]
    elif mode == "text":
        assert run["answers"] == ["A separate staging environment"]
    elif mode == "composer":
        assert run["answers"] == ["Use staging instead"]
    elif mode == "skip":
        assert run["answers"] == [""]
    else:
        assert not run.get("answers") and peer.stops == 1


def test_saved_model_label_and_explicit_default(page, live_app):
    peer = live_app[1]
    peer.sessions["notes"] = {
        "id": "notes",
        "title": "Remembered session",
        "source": "api_server",
        "model": "remembered-model",
    }
    peer.messages["notes"] = []
    page.reload()
    page.get_by_role("link", name="Remembered session", exact=True).click()
    wait_for_store(
        page,
        "state => state.active === 'notes' && state.sessionDetails?.model === 'remembered-model'",
    )
    result = page.evaluate("""async () => {
      const {state} = await import('/static/store.js');
      const {sessionModel} = await import('/static/models.js');
      const inherited = sessionModel(state);
      const modelChoices = {...state.modelChoices, notes: null};
      const overridden = sessionModel({...state, modelChoices});
      return {inherited, overridden};
    }""")
    assert result["inherited"]["id"] == "remembered-model"
    assert result["inherited"]["inherited"] is True
    assert result["overridden"] is None


def test_invalid_answers_never_resolve_a_question(live_app):
    url, peer, _ = live_app
    peer.runs["question-run"] = {"clarification": {"request_id": "question-id"}}
    with signed_in(url) as client:
        for payload in (
            {"request_id": "question-id"},
            {"request_id": "question-id", "answer": ["Development"]},
            {"request_id": "question-id", "answer": "a" * 16001},
            {"request_id": "", "answer": ""},
        ):
            response = client.post("/api/runs/question-run/clarification", json=payload)
            assert response.status_code == 400
            assert peer.runs["question-run"]["clarification"] is not None
        response = client.post(
            "/api/runs/question-run/clarification",
            json={"request_id": "question-id", "answer": ""},
        )
        assert response.status_code == 200
        assert peer.runs["question-run"]["answers"] == [""]

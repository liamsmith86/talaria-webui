"""Branch real transcript boundaries without changing the installed plugin protocol."""

import copy

import pytest
from playwright.sync_api import expect

from talaria.hermes import APIError

from .conftest import wait_for_store
from .test_backend_qa import backend as backend


def seed_branch(peer):
    peer.extension = {}
    peer.sessions["original"] = {"id": "original", "title": "Original", "source": "api_server"}
    peer.messages["original"] = [
        {"id": 10, "role": "user", "content": "First question"},
        {
            "id": 11,
            "role": "assistant",
            "content": "Checking",
            "tool_calls": [
                {"id": "call", "function": {"name": "read_file", "arguments": "{}"}},
            ],
        },
        {"id": 12, "role": "tool", "content": "File contents", "tool_call_id": "call"},
        {"id": 13, "role": "assistant", "content": "First answer"},
        {"id": 14, "role": "user", "content": "Later question"},
        {"id": 15, "role": "assistant", "content": "Later answer"},
    ]


async def test_branch_retains_the_complete_tool_turn_and_maps_copied_ids(backend):
    client, _, peer = backend
    seed_branch(peer)
    original = copy.deepcopy(peer.messages["original"])
    response = await client.post("/api/sessions/original/fork", json={"message_id": 13})
    assert response.status_code == 201, response.text
    child = response.json()["id"]
    assert [row["content"] for row in peer.messages[child]] == [
        row["content"] for row in original[:4]
    ]
    assert peer.messages[child][0]["id"] != original[0]["id"]
    assert peer.messages[child][1]["tool_calls"] == original[1]["tool_calls"]
    assert peer.messages[child][2]["tool_call_id"] == "call"
    assert peer.messages["original"] == original
    assert peer.rewinds == 1


async def test_branch_at_latest_response_needs_no_rewind(backend):
    client, _, peer = backend
    seed_branch(peer)
    response = await client.post("/api/sessions/original/fork", json={"message_id": 15})
    assert response.status_code == 201, response.text
    assert len(peer.messages[response.json()["id"]]) == 6
    assert peer.rewinds == 0


@pytest.mark.parametrize("mid", [True, 0, -1, "13", 2**63])
async def test_invalid_branch_message_never_creates_a_copy(backend, mid):
    client, _, peer = backend
    seed_branch(peer)
    response = await client.post("/api/sessions/original/fork", json={"message_id": mid})
    assert response.status_code == 400
    assert list(peer.sessions) == ["original"]


@pytest.mark.parametrize("mid", [1, 10, 11, 99])
async def test_unavailable_or_incomplete_turn_never_creates_a_copy(backend, mid):
    client, _, peer = backend
    seed_branch(peer)
    peer.messages["original"].insert(
        0, {"id": 1, "role": "assistant", "content": "Archived", "active": False}
    )
    response = await client.post("/api/sessions/original/fork", json={"message_id": mid})
    assert response.status_code == 409
    assert list(peer.sessions) == ["original"]


async def test_branch_buttons_exclude_archived_history_and_tool_calls(backend):
    client, _, peer = backend
    seed_branch(peer)
    peer.messages["original"].insert(
        0, {"id": 1, "role": "assistant", "content": "Archived", "active": False}
    )
    await client.get("/api/capabilities")
    response = await client.get("/api/sessions/original/messages")
    assert {row["id"] for row in response.json()["data"] if row.get("branchable")} == {13, 15}


async def test_unavailable_active_history_keeps_chat_readable(backend, monkeypatch):
    client, app, peer = backend
    seed_branch(peer)
    await client.get("/api/capabilities")
    real = app.state.hermes.request

    async def request(method, path, **kwargs):
        if kwargs.get("params", {}).get("include_compacted") == "false":
            raise APIError("Synthetic unavailable active history", 503)
        return await real(method, path, **kwargs)

    monkeypatch.setattr(app.state.hermes, "request", request)
    response = await client.get("/api/sessions/original/messages")
    assert response.status_code == 200
    assert len(response.json()["data"]) == 6
    assert not any(row["branchable"] for row in response.json()["data"])


@pytest.mark.parametrize(
    "failure", ["rewind", "copy_changed", "original_changed", "rewind_changed"]
)
async def test_failed_branch_preserves_original_and_concurrent_copy_changes(
    backend, monkeypatch, failure
):
    client, app, peer = backend
    seed_branch(peer)
    original = copy.deepcopy(peer.messages["original"])
    real = app.state.hermes.request

    async def request(method, path, **kwargs):
        if path.endswith("/rewind") and not kwargs.get("json", {}).get("preview"):
            if failure == "rewind":
                raise APIError("Synthetic rewind failure", 503)
            if failure == "rewind_changed":
                sid = path.split("/")[-2]
                peer.messages[sid].append({"id": 100, "role": "user", "content": "Concurrent"})
        result = await real(method, path, **kwargs)
        if path.endswith("/fork"):
            if failure == "copy_changed":
                peer.messages[result["session"]["id"]][0]["content"] = "Changed copy"
            if failure == "original_changed":
                peer.messages["original"].append(
                    {"id": 100, "role": "user", "content": "Concurrent"}
                )
        return result

    monkeypatch.setattr(app.state.hermes, "request", request)
    response = await client.post("/api/sessions/original/fork", json={"message_id": 13})
    assert response.status_code >= 400
    if failure in {"copy_changed", "rewind_changed"}:
        assert len(peer.sessions) == 2
        assert "unfinished copy" in response.json()["error"]
    else:
        assert list(peer.sessions) == ["original"]
    assert peer.messages["original"][:6] == original
    assert len(peer.messages["original"]) == (7 if failure == "original_changed" else 6)


async def test_cleanup_failure_reports_the_retained_copy(backend, monkeypatch):
    client, app, peer = backend
    seed_branch(peer)
    real = app.state.hermes.request

    async def request(method, path, **kwargs):
        if method == "DELETE" or (
            path.endswith("/rewind") and not kwargs.get("json", {}).get("preview")
        ):
            raise APIError("Synthetic failure", 503)
        return await real(method, path, **kwargs)

    monkeypatch.setattr(app.state.hermes, "request", request)
    response = await client.post("/api/sessions/original/fork", json={"message_id": 13})
    assert response.status_code == 502
    assert "unfinished copy" in response.json()["error"]
    assert len(peer.sessions) == 2 and len(peer.messages["original"]) == 6


@pytest.mark.parametrize("width", [1280, 390])
def test_message_branch_button_retains_the_selected_response(page, live_app, width):
    page.set_viewport_size({"width": width, "height": 844})
    peer = live_app[1]
    seed_branch(peer)
    page.reload()
    if width < 768:
        page.get_by_role("button", name="Open sidebar", exact=True).click()
    page.get_by_role("link", name="Original", exact=True).click()
    buttons = page.get_by_role("button", name="Branch from here", exact=True)
    expect(buttons).to_have_count(2)
    buttons.first.click()
    dialog = page.get_by_role("dialog", name="Branch from here", exact=True)
    expect(dialog).to_contain_text("through this response")
    dialog.get_by_role("button", name="Create branch", exact=True).click()
    wait_for_store(page, "s => s.active !== 'original' && !s.loading")
    expect(page.locator(".message.user")).to_have_count(1)
    expect(page.locator(".message.assistant")).to_have_count(1)
    expect(page.locator(".conversation-content")).not_to_contain_text("Later answer")
    expect(page.locator(".tool-card")).to_have_count(1)
    if width < 768:
        page.get_by_role("button", name="Open sidebar", exact=True).click()
    page.get_by_role("link", name="Original", exact=True).click()
    expect(page.locator(".message.user")).to_have_count(2)
    expect(page.locator(".conversation-content")).to_contain_text("Later answer")


def test_branching_requires_the_installed_plugin(page, live_app):
    peer = live_app[1]
    seed_branch(peer)
    peer.extension = None
    page.reload()
    page.get_by_role("link", name="Original", exact=True).click()
    expect(page.locator(".message.assistant")).to_have_count(2)
    expect(page.get_by_role("button", name="Branch from here")).to_have_count(0)


def test_archived_responses_do_not_offer_branching(page, live_app):
    peer = live_app[1]
    seed_branch(peer)
    peer.messages["original"][:0] = [
        {"id": 1, "role": "user", "content": "Old question", "active": False},
        {"id": 2, "role": "assistant", "content": "Old answer", "active": False},
    ]
    page.reload()
    page.get_by_role("link", name="Original", exact=True).click()
    old = page.locator(".message.assistant").filter(has_text="Old answer")
    expect(old).to_be_visible()
    expect(old.get_by_role("button", name="Branch from here")).to_have_count(0)
    expect(page.get_by_role("button", name="Branch from here")).to_have_count(2)

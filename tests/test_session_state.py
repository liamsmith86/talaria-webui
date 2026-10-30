"""Talaria's own record of session activity is the same everywhere and outlives restarts."""

import stat
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from talaria import session_commands
from talaria.app import STATE_UNAVAILABLE, create_app
from talaria.session_state import StateUnavailable, state_path

from .conftest import serve, wait_for
from .test_app import signed_in


@pytest.fixture(autouse=True)
def prompt_followers(monkeypatch):
    """Followers ask Hermes often, so these tests wait on outcomes, not intervals."""
    monkeypatch.setattr(session_commands, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(session_commands, "RETRY_SECONDS", 0.1)


def session_with_history(client, peer, title="Notes"):
    sid = client.post("/api/sessions", json={"title": title}).json()["id"]
    peer.messages[sid] = [
        {"id": 1, "role": "user", "content": "One", "timestamp": 100.0},
        {"id": 2, "role": "assistant", "content": "Two", "timestamp": 101.0},
        {"id": 3, "role": "user", "content": "Three", "timestamp": 102.0},
        {"id": 4, "role": "assistant", "content": "Four", "timestamp": 103.0},
    ]
    return sid


def commands(client, sid, **params):
    response = client.get(f"/api/sessions/{sid}/commands", params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def start(client, sid, command="compress", args="", request_id="command-request-0001"):
    return client.post(
        "/api/commands",
        json={"request_id": request_id, "session_id": sid, "command": command, "args": args},
    )


def test_compaction_result_is_kept_without_any_browser_and_shared_by_every_client(live_app):
    url, peer, _ = live_app
    peer.extension = {"commands": True}
    with signed_in(url) as first, signed_in(url) as second:
        sid = session_with_history(first, peer)
        response = start(first, sid)
        assert response.status_code == 202
        record = response.json()
        assert (record["status"], record["anchor"]) == ("running", 103.0)
        # Another device sees the compaction running before it ends.
        assert [r["status"] for r in commands(second, sid)] == ["running"]
        # No browser asks again: Talaria's server follows the job by itself.
        payload = peer.finish_command(
            session_id=sid, changed=True, text="Compressed 4 messages to 2."
        )
        assert payload == {
            "request_id": "command-request-0001",
            "session_id": sid,
            "command": "compress",
            "args": "",
        }
        done = wait_for(lambda: commands(second, sid), lambda r: r[0]["status"] != "running")
        assert done[0] | {"started_at": 0, "finished_at": 0} == {
            "id": "command-request-0001",
            "session_id": sid,
            "result_session_id": sid,
            "command": "compress",
            "args": "",
            "status": "completed",
            "changed": True,
            "text": "Compressed 4 messages to 2.",
            "anchor": 103.0,
            "started_at": 0,
            "finished_at": 0,
        }
        assert first.get("/api/commands/command-request-0001").json() == done[0]
        # Repeating a request ID never runs the command twice.
        assert start(first, sid).status_code == 202
        assert len(peer.commands) == 1
        assert start(first, sid, args="--focus other").status_code == 409


def test_running_command_resumes_after_talaria_restarts(live_app, tmp_path):
    url, peer, app = live_app
    peer.extension = {"commands": True}
    with signed_in(url) as client:
        sid = session_with_history(client, peer)
        assert start(client, sid).status_code == 202
    # A second Talaria process over the same data, as after a restart.
    restarted = create_app(app.state.settings, tmp_path / "config.json")
    server, thread, address = serve(restarted)
    try:
        with signed_in(address) as client:
            peer.finish_command(session_id=sid, changed=False, text="Nothing to compress yet.")
            done = wait_for(lambda: commands(client, sid), lambda r: r[0]["status"] != "running")
            assert (done[0]["status"], done[0]["text"]) == ("completed", "Nothing to compress yet.")
    finally:
        server.should_exit = True
        thread.join(6)


def test_command_is_never_reported_lost_while_hermes_is_still_accepting_it(live_app):
    url, peer, _ = live_app
    peer.extension = {"commands": True}
    peer.command_delay = 2.5
    with signed_in(url) as first, signed_in(url) as second:
        sid = session_with_history(first, peer)
        with ThreadPoolExecutor(1) as pool:
            started = pool.submit(start, first, sid)
            # Another device lists the command before Hermes has accepted it.
            wait_for(lambda: commands(second, sid), lambda r: r and r[0]["status"] == "running")
            time.sleep(1)  # Long enough for a follower to ask Hermes many times.
            assert commands(second, sid)[0]["status"] == "running"
            assert started.result().status_code == 202
        peer.finish_command(session_id=sid, changed=True, text="Compressed.")
        done = wait_for(lambda: commands(second, sid), lambda r: r[0]["status"] != "running")
        assert (done[0]["status"], done[0]["text"]) == ("completed", "Compressed.")


def test_command_whose_start_answer_was_lost_is_still_followed(live_app):
    url, peer, _ = live_app
    peer.extension = {"commands": True}
    peer.command_answer_lost = True
    with signed_in(url) as client:
        sid = session_with_history(client, peer)
        assert start(client, sid).status_code == 202
        time.sleep(1)  # Hermes is slow to accept it; asking now finds nothing.
        peer.commands["command-request-0001"] = {
            "payload": {},
            "result": {"status": "completed", "changed": False, "text": "Nothing to compress."},
        }
        done = wait_for(lambda: commands(client, sid), lambda r: r[0]["status"] != "running")
        assert (done[0]["status"], done[0]["text"]) == ("completed", "Nothing to compress.")


def test_refused_command_records_nothing_and_explains_why(live_app):
    url, peer, _ = live_app
    peer.extension = {"commands": True}
    peer.command_refusal = "This session is busy. Wait for the response to finish."
    with signed_in(url) as client:
        sid = session_with_history(client, peer)
        response = start(client, sid)
        assert response.status_code == 400
        assert response.json()["error"] == "This session is busy. Wait for the response to finish."
        assert commands(client, sid) == []
        assert client.get("/api/commands/command-request-0001").status_code == 404


@pytest.mark.parametrize("cause", ["hermes restarted", "hermes never finished"])
def test_unreported_result_is_unknown_not_guessed(live_app, monkeypatch, cause):
    url, peer, _ = live_app
    peer.extension = {"commands": True}
    if cause == "hermes never finished":
        monkeypatch.setattr(session_commands, "COMMAND_SECONDS", 1.0)
    with signed_in(url) as client:
        sid = session_with_history(client, peer)
        assert start(client, sid).status_code == 202
        if cause == "hermes restarted":
            peer.commands.clear()  # The plugin keeps its jobs only in memory.
        done = wait_for(lambda: commands(client, sid), lambda r: r[0]["status"] != "running")
        assert (done[0]["status"], done[0]["changed"], done[0]["text"]) == ("unknown", False, "")
        # Hermes reporting it afterwards changes nothing.
        peer.commands["command-request-0001"] = {
            "payload": {},
            "result": {"status": "completed", "changed": True, "text": "Compressed."},
        }
        time.sleep(0.3)
        assert commands(client, sid)[0]["status"] == "unknown"


def test_rotated_continuation_lists_the_compaction_and_keeps_choices(live_app):
    url, peer, _ = live_app
    peer.extension = {"commands": True}
    with signed_in(url) as client:
        sid = session_with_history(client, peer)
        choice = {"model": {"id": "model-b", "provider": "test"}, "reasoning": "high"}
        assert client.put(f"/api/sessions/{sid}/preferences", json=choice).json() == {"ok": True}
        assert start(client, sid).status_code == 202
        peer.finish_command(session_id="continuation", changed=True, text="Compressed.")
        done = wait_for(
            lambda: commands(client, "continuation", lineage=sid),
            lambda r: r and r[0]["status"] != "running",
        )
        assert (done[0]["session_id"], done[0]["result_session_id"]) == (sid, "continuation")
        # Listing the continuation alone still finds the compaction that made it.
        assert [r["id"] for r in commands(client, "continuation")] == ["command-request-0001"]
        peer.sessions["continuation"] = {
            **peer.sessions[sid],
            "id": "continuation",
            "parent_session_id": sid,
        }
        peer.messages["continuation"] = []
        details = client.get("/api/sessions/continuation").json()
        assert details["talaria_preferences"] == choice
        # The continuation keeps what it inherited, so its own continuation does too.
        peer.sessions["later"] = {
            **peer.sessions[sid],
            "id": "later",
            "parent_session_id": "continuation",
        }
        peer.messages["later"] = []
        assert client.get("/api/sessions/later").json()["talaria_preferences"] == choice


def test_choices_are_shared_inherited_validated_and_imported_without_overwriting(live_app):
    url, peer, _ = live_app

    def choice(client, sid):
        return client.get(f"/api/sessions/{sid}").json()["talaria_preferences"]

    def choose(client, sid, **changes):
        response = client.put(f"/api/sessions/{sid}/preferences", json=changes)
        assert response.json() == {"ok": True}

    def session(sid, parent=None):
        peer.sessions[sid] = {"id": sid, "title": sid, "parent_session_id": parent}
        peer.messages[sid] = []

    with signed_in(url) as first, signed_in(url) as second:
        sid = session_with_history(first, peer)
        assert choice(first, sid) == {}
        choose(first, sid, model=None)
        choose(first, sid, reasoning="low")
        assert choice(second, sid) == {"model": None, "reasoning": "low"}
        for invalid in (
            {},
            {"model": "model-b"},
            {"model": {"id": "", "provider": "test"}},
            {"reasoning": "extreme"},
            {"other": True},
        ):
            response = first.put(f"/api/sessions/{sid}/preferences", json=invalid)
            assert response.status_code == 400, invalid
        # A branch starts with its parent's choice, and choosing automatic
        # reasoning there is a choice of its own.
        session("branch", sid)
        assert choice(second, "branch") == {"model": None, "reasoning": "low"}
        choose(second, "branch", reasoning="auto")
        assert choice(first, "branch") == {"model": None, "reasoning": "auto"}
        assert choice(first, sid) == {"model": None, "reasoning": "low"}
        # Choosing one part before anything was inherited keeps the other part.
        session("other-branch", sid)
        choose(first, "other-branch", reasoning="high")
        assert choice(second, "other-branch") == {"model": None, "reasoning": "high"}
        imported = first.post(
            "/api/preferences/import",
            json={
                "sessions": {
                    sid: {"model": {"id": "stale", "provider": "test"}},
                    "older": {"model": {"id": "model-c", "provider": "test"}, "reasoning": "high"},
                    "bad id/": {"reasoning": "high"},
                    "invalid": {"reasoning": "extreme"},
                }
            },
        )
        assert imported.json() == {"imported": 2}
        # The import never replaces a choice another device already shared.
        assert choice(first, sid) == {"model": None, "reasoning": "low"}
        session("older")
        assert choice(first, "older") == {
            "model": {"id": "model-c", "provider": "test"},
            "reasoning": "high",
        }


def test_dismissal_applies_everywhere_and_deleting_a_session_forgets_it(live_app):
    url, peer, _ = live_app
    peer.extension = {"commands": True}
    with signed_in(url) as client:
        sid = session_with_history(client, peer)
        start(client, sid, command="version", request_id="version-request-0001")
        assert client.delete("/api/commands/version-request-0001").status_code == 404
        peer.finish_command(session_id=sid, text="Hermes Agent 0.22.5")
        wait_for(lambda: commands(client, sid), lambda r: r[0]["status"] == "completed")
        assert client.delete("/api/commands/version-request-0001").status_code == 200
        assert commands(client, sid) == []
        client.put(f"/api/sessions/{sid}/preferences", json={"reasoning": "high"})
        start(client, sid, request_id="compress-request-0001")
        peer.finish_command(session_id=sid, changed=True, text="Compressed.")
        wait_for(lambda: commands(client, sid), lambda r: r and r[0]["status"] == "completed")
        assert client.delete(f"/api/sessions/{sid}").status_code == 200
        assert commands(client, sid) == []
        peer.sessions[sid] = {"id": sid, "title": "Recreated"}
        peer.messages[sid] = []
        assert client.get(f"/api/sessions/{sid}").json()["talaria_preferences"] == {}


def test_state_file_is_private(live_app, tmp_path):
    url, peer, _ = live_app
    with signed_in(url) as client:
        sid = session_with_history(client, peer)
        client.put(f"/api/sessions/{sid}/preferences", json={"reasoning": "high"})
    mode = stat.S_IMODE(state_path(tmp_path / "config.json").stat().st_mode)
    assert mode == 0o600


def test_unavailable_state_refuses_changes_and_never_hides_a_session(live_app, monkeypatch):
    url, peer, app = live_app
    peer.extension = {"commands": True}

    def unavailable():
        raise StateUnavailable("disk I/O error")

    with signed_in(url) as client:
        sid = session_with_history(client, peer)
        monkeypatch.setattr(app.state.session_state, "connect", unavailable)
        details = client.get(f"/api/sessions/{sid}")
        assert details.status_code == 200 and "talaria_preferences" not in details.json()
        unsaved = {"error": STATE_UNAVAILABLE, "code": "state_unavailable"}
        response = client.put(f"/api/sessions/{sid}/preferences", json={"reasoning": "high"})
        assert (response.status_code, response.json()) == (503, unsaved)
        response = start(client, sid)
        assert (response.status_code, response.json()) == (503, unsaved)
        # Nothing Talaria could not record ever reaches Hermes.
        assert peer.commands == {}

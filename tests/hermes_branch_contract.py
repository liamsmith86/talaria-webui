"""Real native fork/resume behavior, temporary storage and synthetic messages only."""

import asyncio
import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB

from talaria.hermes_plugin.bridge import wire

home = Path(sys.argv[1])
assert home.is_dir() and str(home).startswith("/tmp/")
(home / "config.yaml").write_text(json.dumps({"plugins": {"enabled": ["talaria"]}}))


async def main():
    db = SessionDB(home / "state.db")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-key"}))
    adapter._session_db = db
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/fork", adapter._handle_fork_session)
    app.router.add_get("/api/sessions/{session_id}/messages", adapter._handle_session_messages)
    wire(app, adapter)
    headers = {"Authorization": "Bearer fixture-key"}
    async with TestClient(TestServer(app)) as client:
        for prefix, sid in (("/api", "native-original"), ("/talaria/v1", "plugin-original")):
            db.create_session(sid, "discord")
            db.append_message(sid, "user", "Original question")
            db.append_message(sid, "assistant", "Original answer")
            response = await client.post(
                f"{prefix}/sessions/{sid}/fork", headers=headers, json={"title": sid + " branch"}
            )
            assert response.status == 201, await response.text()
            fork = (await response.json())["session"]
            history = await (
                await client.get(
                    f"/api/sessions/{sid}/messages",
                    headers=headers,
                )
            ).json()
            if prefix == "/api":
                # Capture the upstream defect without inventing a browser scenario.
                redirected = history["session_id"] == fork["id"]
                print("Native API redirects original to clone:", redirected)
            else:
                assert fork["source"] == "api_server"
                assert fork["parent_session_id"] == sid
                assert history["session_id"] == sid, history["session_id"]
                assert db.resolve_resume_session_id(fork["id"]) == fork["id"]
                assert db.get_session_model_config_value(fork["id"], "_branched_from") == sid
                db.end_session(sid, "gateway_shutdown")
                assert db.resolve_resume_session_id(sid) == sid
                assert [m["content"] for m in db.get_messages(sid)] == [
                    m["content"] for m in db.get_messages(fork["id"])
                ]
        # Genuine compression continuations must keep resolving to their tip.
        db.create_session("compressed-original", "discord")
        db.end_session("compressed-original", "compression")
        db.create_session("compressed-tip", "discord", parent_session_id="compressed-original")
        db.append_message("compressed-tip", "assistant", "New context")
        assert db.resolve_resume_session_id("compressed-original") == "compressed-tip"
        # Default branch names belong to Hermes; no client-generated fixed suffix.
        db.create_session("named-original", "api_server")
        db.set_session_title("named-original", "Named original")
        for number in (2, 3):
            response = await client.post(
                "/talaria/v1/sessions/named-original/fork", headers=headers, json={}
            )
            assert response.status == 201, await response.text()
            fork = (await response.json())["session"]
            assert fork["title"] == f"Named original #{number}"
            assert db.resolve_resume_session_id("named-original") == "named-original"
        # A rejected custom title must not create an unnamed, unmarked child.
        before = db._read_one("SELECT count(*) AS n FROM sessions")["n"]
        for title in ("Named original #2", "x" * 1000):
            response = await client.post(
                "/talaria/v1/sessions/named-original/fork", headers=headers, json={"title": title}
            )
            assert response.status == 400, await response.text()
            assert db._read_one("SELECT count(*) AS n FROM sessions")["n"] == before
            assert db.resolve_resume_session_id("named-original") == "named-original"
        await verify_failed_branches(client, adapter, db, headers)
        await verify_failed_copy(client, db, headers)
        await verify_command_heads(client, headers)
    await adapter.disconnect()
    db.close()


async def verify_failed_branches(client, adapter, db, headers):
    # Default naming must be validated before closing or copying the source.
    db.create_session("long-title", "discord")
    db.set_session_title("long-title", "X" * db.MAX_TITLE_LENGTH)
    before = db.get_session("long-title")
    count = db._read_one("SELECT count(*) AS n FROM sessions")["n"]
    response = await client.post("/talaria/v1/sessions/long-title/fork", headers=headers, json={})
    assert response.status == 400, await response.text()
    assert db.get_session("long-title") == before
    assert db._read_one("SELECT count(*) AS n FROM sessions")["n"] == count

    # Synchronize only title preflight reads, reproducing two real clients that
    # both saw the same available title before either submitted the final write.
    db.create_session("racing-parent", "discord")
    db.append_message("racing-parent", "user", "Synthetic original")
    db.append_message("racing-parent", "assistant", "Synthetic reply")
    barrier = threading.Barrier(2)
    lookup, create = db.get_session_by_title, db.create_session
    created = []

    def simultaneous_lookup(title):
        result = lookup(title)
        if title == "Contended branch title":
            barrier.wait(timeout=5)
        return result

    def observed_create(session_id, source, **kwargs):
        result = create(session_id, source, **kwargs)
        if kwargs.get("parent_session_id") == "racing-parent":
            # The native INSERT itself must mark the child, not a later patch.
            assert (
                db.get_session_model_config_value(session_id, "_branched_from") == "racing-parent"
            )
            assert db.resolve_resume_session_id("racing-parent") == "racing-parent"
            created.append(session_id)
        return result

    async def branch():
        response = await client.post(
            "/talaria/v1/sessions/racing-parent/fork",
            headers=headers,
            json={"title": "Contended branch title"},
        )
        return response.status, await response.json()

    with (
        patch.object(db, "get_session_by_title", simultaneous_lookup),
        patch.object(db, "create_session", observed_create),
    ):
        results = await asyncio.gather(branch(), branch())
    assert sorted(status for status, _ in results) == [201, 400], results
    assert len(created) == 2
    surviving = [sid for sid in created if db.get_session(sid)]
    assert len(surviving) == 1  # The rejected operation's child was rolled back.
    assert db.resolve_resume_session_id("racing-parent") == "racing-parent"
    assert adapter._session_db is db
    assert db.get_session_by_title == lookup and db.create_session == create
    assert db.get_session("racing-parent")["end_reason"] == "branched"


async def verify_failed_copy(client, db, headers):
    db.create_session("copy-failure-parent", "discord")
    db.append_message("copy-failure-parent", "user", "Keep this source untouched")
    source = db.get_session("copy-failure-parent")
    count = db._read_one("SELECT count(*) AS n FROM sessions")["n"]
    with patch.object(db, "replace_messages", side_effect=ValueError("Synthetic copy failure")):
        response = await client.post(
            "/talaria/v1/sessions/copy-failure-parent/fork",
            headers=headers,
            json={"title": "Copy failure fixture"},
        )
    assert response.status == 400
    assert db.get_session("copy-failure-parent") == source
    assert db._read_one("SELECT count(*) AS n FROM sessions")["n"] == count
    assert db.resolve_resume_session_id("copy-failure-parent") == "copy-failure-parent"

    # A caller-supplied existing ID must never be treated as our failed child.
    db.create_session("existing-session", "api_server")
    db.append_message("existing-session", "user", "Keep this unrelated session")
    existing = db.get_session("existing-session")
    response = await client.post(
        "/talaria/v1/sessions/copy-failure-parent/fork",
        headers=headers,
        json={"id": "existing-session", "title": "Collision fixture"},
    )
    assert response.status == 409
    assert db.get_session("existing-session") == existing
    assert db.get_session("copy-failure-parent") == source


async def verify_command_heads(client, headers):
    from talaria.hermes_plugin import commands

    called = []

    def execute(*args):
        called.append(args)
        return {"text": "Unexpected HEAD execution"}

    with patch.object(commands, "execute", execute):
        for path, status in (
            ("/talaria/v1/commands", 200),
            ("/talaria/v1/commands/missing-command", 404),
        ):
            response = await client.head(
                path,
                headers=headers,
                json={
                    "request_id": "head-must-not-run-1234",
                    "command": "version",
                },
            )
            assert response.status == status
        await asyncio.sleep(0)
        assert not called


asyncio.run(main())

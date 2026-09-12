"""Exercise real Hermes compression, leases and SQLite; substitute only the summary LLM."""

import asyncio
import json
import threading
import uuid
from unittest.mock import patch

from agent.context_compressor import ContextCompressor


async def verify_commands(client, db, model):
    headers = {"Authorization": "Bearer fixture-key"}
    assert (await client.get("/talaria/v1/commands")).status == 401
    response = await client.get("/talaria/v1/commands", headers=headers)
    catalog = (await response.json())["commands"]
    assert next(c for c in catalog if c["name"] == "compress")["mode"] == "native"
    assert next(c for c in catalog if c["name"] == "clear")["mode"] == "unavailable"

    async def command(sid, args, *, name="compress", request_id=None):
        key = request_id or uuid.uuid4().hex
        payload = {"command": name, "args": args, "session_id": sid, "request_id": key}
        response = await client.post("/talaria/v1/commands", headers=headers, json=payload)
        assert response.status == 202, await response.text()
        for _ in range(1000):
            result = await (await client.get(f"/talaria/v1/commands/{key}", headers=headers)).json()
            if result["status"] != "running":
                return result
            await asyncio.sleep(0.01)
        raise AssertionError("Command did not finish")

    assert (await command("", "", name="version"))["status"] == "completed"
    db.create_session("discord-command", "discord")
    assert "Branch" in (await command("discord-command", ""))["error"]
    for partial, rotate in ((False, False), (True, False), (True, True)):
        sid = f"compression-{partial}-{rotate}"
        db.create_session(sid, "api_server", model=model)
        for i in range(40):
            db.append_message(
                sid, "user" if i % 2 == 0 else "assistant", f"Message {i}: " + "detail " * 2000
            )
        before = db.get_messages(sid)
        before_conversation = db.get_messages_as_conversation(sid)
        preview = await command(sid, "here 2 --preview")
        assert preview["status"] == "completed" and preview["changed"] is False, preview
        assert db.get_messages(sid) == before
        assert db.get_session(sid)["ended_at"] is None
        assert db.try_acquire_session_turn_lease(sid, "test-busy")
        try:
            busy = await command(sid, "")
            assert busy["status"] == "failed" and "busy" in busy["error"], busy
            assert db.get_messages(sid) == before
        finally:
            db.release_session_turn_lease(sid, "test-busy")
        key = uuid.uuid4().hex
        from talaria.hermes_plugin.compression import create_agent

        def configured_agent(adapter, session, rotate=rotate):
            agent = create_agent(adapter, session)
            agent.compression_in_place = not rotate
            return agent

        with (
            patch("talaria.hermes_plugin.compression.create_agent", configured_agent),
            patch.object(
                ContextCompressor,
                "_call_summary_llm",
                return_value=(
                    "The user discussed detailed fixture messages. "
                    "Preserve the numbered conversation and continue the task."
                ),
            ) as summary,
        ):
            result = await command(sid, "here 2" if partial else "", request_id=key)
            assert result["status"] == "completed" and result["changed"] is True, result
            assert summary.call_count == 1, result
            again = await command(sid, "here 2" if partial else "", request_id=key)
            assert again == result and summary.call_count == 1
        canonical = result["session_id"]
        assert (canonical != sid) is rotate
        assert db.resolve_resume_session_id(sid) == canonical
        after = db.get_messages_as_conversation(canonical)
        assert len(json.dumps(after)) < len(json.dumps(before)) / 2, result
        assert db.get_session(canonical)["ended_at"] is None
        if partial:
            assert [m["content"] for m in after[-4:]] == [
                m["content"] for m in before_conversation[-4:]
            ]
        assert not any(m["content"].startswith("/compress") for m in after)
        assert db.try_acquire_session_turn_lease(canonical, "test-after")
        db.release_session_turn_lease(canonical, "test-after")
    await verify_admission(client, db)
    print("Native slash commands: preview, compression, partial tail, leases and replay passed")


async def verify_admission(client, db):
    headers = {"Authorization": "Bearer fixture-key"}
    release = threading.Event()

    def slow_command(adapter, database, sid, name, args):
        assert release.wait(10)
        return {"session_id": sid, "text": "Done"}

    from talaria.hermes_plugin.commands import CommandJobs

    # Exercise the real HTTP route/reservations while holding only the execution boundary.
    with patch("talaria.hermes_plugin.commands.execute", slow_command):
        try:
            for i in range(4):
                payload = {"command": "version", "request_id": f"concurrent-command-{i}"}
                response = await client.post("/talaria/v1/commands", json=payload, headers=headers)
                assert response.status == 202, await response.text()
            # Replay still succeeds when all slots are occupied; a changed payload does not.
            assert (
                await client.post("/talaria/v1/commands", json=payload, headers=headers)
            ).status == 202
            assert (
                await client.post(
                    "/talaria/v1/commands", json={**payload, "args": "changed"}, headers=headers
                )
            ).status == 409
            assert (
                await client.post(
                    "/talaria/v1/commands",
                    json={**payload, "request_id": "concurrent-command-5"},
                    headers=headers,
                )
            ).status == 429
        finally:
            release.set()
    # Finished records expire, but an active job must never be evicted.
    jobs = CommandJobs()
    jobs.jobs["finished"] = {"created": 0, "result": {"status": "completed"}}
    jobs.jobs["active"] = {"created": 0, "result": {"status": "running"}}
    # A fresh CI runner may have less than an hour of monotonic uptime.
    with patch("talaria.hermes_plugin.commands.time.monotonic", return_value=3601):
        jobs.prune()
    assert list(jobs.jobs) == ["active"]
    assert db.get_session("discord-command")["ended_at"] is None

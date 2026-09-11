"""Real native fork/resume behavior, temporary storage and synthetic messages only."""

import asyncio
import json
import sys
from pathlib import Path

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
            response = await client.post(f"{prefix}/sessions/{sid}/fork", headers=headers,
                                         json={"title": sid + " branch"})
            assert response.status == 201, await response.text()
            fork = (await response.json())["session"]
            history = await (await client.get(
                f"/api/sessions/{sid}/messages", headers=headers,
            )).json()
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
            response = await client.post("/talaria/v1/sessions/named-original/fork",
                                         headers=headers, json={})
            assert response.status == 201, await response.text()
            fork = (await response.json())["session"]
            assert fork["title"] == f"Named original #{number}"
            assert db.resolve_resume_session_id("named-original") == "named-original"
        # A rejected custom title must not create an unnamed, unmarked child.
        before = db._read_one("SELECT count(*) AS n FROM sessions")["n"]
        for title in ("Named original #2", "x" * 1000):
            response = await client.post("/talaria/v1/sessions/named-original/fork",
                                         headers=headers, json={"title": title})
            assert response.status == 400, await response.text()
            assert db._read_one("SELECT count(*) AS n FROM sessions")["n"] == before
            assert db.resolve_resume_session_id("named-original") == "named-original"
    await adapter.disconnect()
    db.close()


asyncio.run(main())

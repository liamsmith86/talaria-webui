"""Native Hermes commands, recorded by Talaria so every device sees the same activity.

The plugin runs a command as a job that lives in Hermes's memory and expires.
Talaria records the command before Hermes sees it, follows the job to its end
on the server, and keeps the outcome. A browser only ever reads that record,
so closing a tab, switching laptops, or restarting Talaria loses nothing.
"""

import asyncio
import contextlib
import logging
import re
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from .extensions import PREFIX, numeric, plugin_refusal
from .hermes import IDENTIFIER, APIError, identifier
from .session_state import StateUnavailable
from .transcripts import message_page

log = logging.getLogger(__name__)
COMMAND_ID = re.compile(r"[a-zA-Z0-9_-]{16,128}\Z")
POLL_SECONDS = 1.0
RETRY_SECONDS = 5.0
# How long a start Hermes may not have received yet can go unseen there,
# longer than a Hermes request may take.
ACCEPT_SECONDS = 60.0
# Hermes gives commands no time limit. Talaria stops waiting for one still
# unfinished after this long, so the session can run commands again.
COMMAND_SECONDS = 30 * 60.0
FINISHED = {"completed", "failed", "unknown"}
# Never guess whether a command Hermes no longer reports ran: a compaction may
# have committed. The browser explains, in the reader's language.
UNKNOWN = {"status": "unknown"}
GONE = object()
MISSING = object()


class CommandFollowers:
    """Follow each running command to its end, independent of browser connections."""

    def __init__(self, state, profiles):
        self.state, self.profiles = state, profiles
        self.tasks = {}
        self.posting = {}
        self.stopping = False

    @contextlib.contextmanager
    def starting(self, profile_id, command_id):
        """While Talaria posts a command, Hermes may not know it yet. Asking
        then would find nothing, so its start hands it to a follower after."""
        key = (profile_id, command_id)
        self.posting[key] = self.posting.get(key, 0) + 1
        try:
            yield
        finally:
            self.posting[key] -= 1
            if not self.posting[key]:
                del self.posting[key]

    def follow(self, profile_id, record, patience=0.0):
        key = (profile_id, record["id"])
        if self.stopping or key in self.tasks or key in self.posting:
            return
        deadline = record["started_at"] + COMMAND_SECONDS
        task = asyncio.get_running_loop().create_task(self.run(*key, deadline, patience))
        self.tasks[key] = task
        task.add_done_callback(lambda _: self.tasks.pop(key, None))

    async def resume(self):
        try:
            running = await asyncio.to_thread(self.state.running_commands)
        except StateUnavailable as exc:
            log.warning("Cannot resume Hermes commands: %s", exc)
            return
        for profile_id, record in running:
            if profile_id == "default" or profile_id in self.profiles.records:
                self.follow(profile_id, record)

    async def close(self):
        self.stopping = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self, profile_id, command_id, deadline, patience):
        delay, accepting = POLL_SECONDS, time.monotonic() + patience
        while True:
            await asyncio.sleep(delay)
            result = await self.poll(profile_id, command_id)
            if result is GONE:
                return
            if result is MISSING:
                # The plugin keeps results only in memory, which a Hermes restart
                # loses. A start still being accepted is not there yet either.
                result = UNKNOWN if time.monotonic() >= accepting else None
            elif time.time() >= deadline and not finished(result):
                result = UNKNOWN
            if finished(result):
                if await self.finish(profile_id, command_id, result):
                    return
                delay = RETRY_SECONDS
            elif isinstance(result, dict) and result.get("status") == "running":
                delay = POLL_SECONDS
            else:
                delay = RETRY_SECONDS

    async def poll(self, profile_id, command_id):
        try:
            hermes = self.profiles.resolve(profile_id).state.hermes
        except APIError as exc:
            return GONE if exc.code == "profile_missing" else None
        try:
            return await hermes.request("GET", f"{PREFIX}/commands/{command_id}")
        except APIError as exc:
            return MISSING if exc.status == 404 else None

    async def finish(self, profile_id, command_id, result):
        """Save how a command ended. False if Talaria cannot save it yet."""
        status = result["status"]
        text = result.get("text" if status == "completed" else "error")
        target = result.get("session_id")
        try:
            await asyncio.to_thread(
                self.state.finish_command,
                profile_id,
                command_id,
                status,
                text if isinstance(text, str) else "",
                status == "completed" and result.get("changed") is True,
                target if isinstance(target, str) and IDENTIFIER.fullmatch(target) else None,
            )
        except StateUnavailable as exc:
            log.warning("Cannot save a Hermes command result yet: %s", exc)
            return False
        return True


def finished(result):
    return isinstance(result, dict) and result.get("status") in FINISHED


def context(request):
    state = request.app.state
    return state.session_state, state.command_followers, state.profile_id


async def latest_timestamp(client, sid):
    """Hermes's time for the newest saved row, so the result keeps its place in history.

    In-place compaction gives kept rows new IDs but keeps their timestamps, and
    using Hermes's clock avoids any skew between the Talaria and Hermes hosts.
    """
    try:
        page = await message_page(client, sid, 0, order="latest", limit=5)
    except APIError:
        return None
    stamps = [numeric(row.get("timestamp")) for row in page["data"]]
    return max((stamp for stamp in stamps if stamp is not None), default=None)


async def commands(request: Request):
    if request.method == "POST":
        return await start(request)
    return JSONResponse(await request.app.state.hermes.request("GET", f"{PREFIX}/commands"))


async def start(request: Request):
    from .routes import body, text_field

    data = await body(request, 12000)
    fields = {
        key: text_field(data, key, limit)
        for key, limit in (
            ("command", 80),
            ("args", 2000),
            ("session_id", 256),
            ("request_id", 128),
        )
    }
    command_id, sid = fields["request_id"], fields["session_id"]
    if not COMMAND_ID.fullmatch(command_id) or not fields["command"]:
        raise APIError("Invalid command.", 400)
    if sid:
        identifier(sid)
    state, followers, profile_id = context(request)
    hermes = request.app.state.hermes
    anchor = await latest_timestamp(hermes, sid) if sid else None
    patience = 0.0
    with followers.starting(profile_id, command_id):
        record, created = await asyncio.to_thread(
            state.start_command,
            profile_id,
            command_id,
            sid,
            fields["command"],
            fields["args"],
            anchor,
        )
        if not created and (record["session_id"], record["command"], record["args"]) != (
            sid,
            fields["command"],
            fields["args"],
        ):
            raise APIError("Command identifier already used.", 409)
        if created and not await post(hermes, state, profile_id, fields):
            # Hermes may still be accepting it. The follower asks by ID, never posting twice.
            patience = ACCEPT_SECONDS
    if record["status"] == "running":
        followers.follow(profile_id, record, patience)
    return JSONResponse(record, status_code=202)


async def post(hermes, state, profile_id, fields):
    """Send a recorded command to Hermes. False if Hermes's answer never arrived."""
    try:
        await hermes.request("POST", f"{PREFIX}/commands", json=fields)
    except APIError as exc:
        if 400 <= exc.status < 500:
            # Hermes refused before running anything. Nothing happened to record.
            with contextlib.suppress(StateUnavailable):
                await asyncio.to_thread(state.discard_command, profile_id, fields["request_id"])
            raise plugin_refusal(exc) from exc
        return False
    return True


async def command(request: Request):
    command_id = request.path_params["command_id"]
    if not COMMAND_ID.fullmatch(command_id):
        raise APIError("Invalid command.", 400)
    state, followers, profile_id = context(request)
    if request.method == "DELETE":
        if not await asyncio.to_thread(state.dismiss_command, profile_id, command_id):
            raise APIError("This command result is no longer shown.", 404)
        return JSONResponse({"ok": True})
    record = await asyncio.to_thread(state.command, profile_id, command_id)
    if not record:
        raise APIError("This command result is no longer available.", 404)
    if record["status"] == "running":
        followers.follow(profile_id, record)
    return JSONResponse(record)


async def session_commands(request: Request):
    sid = identifier(request.path_params["session_id"])
    # A compaction continuation's history includes rows from earlier sessions.
    lineage = [
        identifier(value) for value in request.query_params.get("lineage", "").split(",") if value
    ]
    state, followers, profile_id = context(request)
    records = await asyncio.to_thread(state.session_commands, profile_id, [sid, *lineage])
    for record in records:
        if record["status"] == "running":
            followers.follow(profile_id, record)
    return JSONResponse({"data": records, "server_time": time.time()})

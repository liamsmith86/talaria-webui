"""Discovered native commands, with bounded jobs independent of browser connections."""

import asyncio
import logging
import re
import time

from . import compression

log = logging.getLogger(__name__)
READ_ONLY = {"version", "profile", "egress", "bundles"}
WEB = {"help", "commands", "new", "model", "reasoning", "title", "branch", "status", "save"}


def available(adapter):
    try:
        from gateway.platforms.api_server import (
            _release_pending_api_work,
            _reserve_pending_api_work,
        )
        from hermes_cli.commands import COMMAND_REGISTRY
        from hermes_cli.slash_exec import resolve_executor

        return isinstance(COMMAND_REGISTRY, list) and all(
            callable(value)
            for value in (
                _release_pending_api_work,
                _reserve_pending_api_work,
                resolve_executor,
                getattr(adapter, "_bind_api_server_session", None),
                getattr(adapter, "_draining_response", None),
            )
        )
    except (ImportError, AttributeError):
        return False


def catalog(adapter, db):
    from hermes_cli.commands import COMMAND_REGISTRY
    from hermes_cli.slash_exec import resolve_executor

    if not available(adapter):
        return []
    can_compress = compression.supported(adapter, db)
    result = []
    for command in COMMAND_REGISTRY:
        name = command.name
        mode = "unavailable"
        if (name in READ_ONLY and resolve_executor(command)) or (
            name == "compress" and can_compress
        ):
            mode = "native"
        elif name in WEB:
            mode = "web"
        result.append(
            {
                "name": name,
                "aliases": list(command.aliases),
                "description": command.description,
                "args": command.args_hint if mode == "native" else "",
                "mode": mode,
                "reason": "Use this command in Hermes's terminal or messaging client."
                if mode == "unavailable"
                else "",
            }
        )
    return result


def execute(adapter, db, sid, name, args):
    from gateway.session_context import clear_session_vars
    from hermes_cli.commands import COMMAND_REGISTRY
    from hermes_cli.slash_exec import CommandContext, resolve_executor

    tokens = adapter._bind_api_server_session(session_id=sid, session_key=f"talaria:{sid}")
    try:
        if name == "compress":
            return compression.compress(adapter, db, sid, args)
        command = next(command for command in COMMAND_REGISTRY if command.name == name)
        if name not in READ_ONLY or args:
            raise compression.CommandError("This command accepts no arguments in Talaria.")
        reply = resolve_executor(command)(CommandContext(surface="gateway"))
        return {"session_id": sid, "text": reply.text[:64000]}
    finally:
        clear_session_vars(tokens)


class CommandJobs:
    def __init__(self):
        self.jobs = {}
        self.tasks = set()

    async def close(self, app):
        # Draining preserves a compressor already committing after its browser disconnected.
        if self.tasks:
            await asyncio.gather(*self.tasks)

    async def dispatch(self, request, adapter, db):
        from aiohttp import web
        from hermes_constants import get_hermes_home

        scope = str(get_hermes_home())
        if request.method == "GET":
            jid = request.match_info.get("command_id")
            if not jid:
                return web.json_response(
                    {"commands": await asyncio.to_thread(catalog, adapter, db)}
                )
            job = self.jobs.get((scope, jid))
            return web.json_response(
                job["result"] if job else {"error": "Command result expired or Hermes restarted."},
                status=200 if job else 404,
            )
        data = await request.json()
        try:
            return await self.submit(data, scope, adapter, db)
        except compression.CommandError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def submit(self, data, scope, adapter, db):
        from aiohttp import web

        if not isinstance(data, dict):
            raise compression.CommandError("Invalid command.")
        jid, sid, name, args = (
            data.get(k, "") for k in ("request_id", "session_id", "command", "args")
        )
        if not all(isinstance(value, str) for value in (jid, sid, name, args)):
            raise compression.CommandError("Invalid command.")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{16,128}", jid) or len(sid) > 256 or len(args) > 2000:
            raise compression.CommandError("Invalid command identifier or arguments.")
        commands = await asyncio.to_thread(catalog, adapter, db)
        command = next((c for c in commands if name in [c["name"], *c["aliases"]]), None)
        if not command or command["mode"] != "native":
            raise compression.CommandError("This command is unavailable through the Hermes API.")
        payload = (sid, command["name"], args)
        key = (scope, jid)
        if existing := self.jobs.get(key):
            if existing["payload"] != payload:
                return web.json_response({"error": "Command identifier already used."}, status=409)
            return web.json_response(existing["result"], status=202)
        self.prune()
        if len(self.tasks) >= 4 or len(self.jobs) >= 256:
            return web.json_response(
                {"error": "Command queue is full. Try again later."}, status=429
            )
        job = {"payload": payload, "created": time.monotonic(), "result": {"status": "running"}}
        from gateway.platforms.api_server import (
            _release_pending_api_work,
            _reserve_pending_api_work,
        )

        draining = adapter._draining_response()
        if draining is not None:
            return draining
        with _reserve_pending_api_work(adapter) as reservation:
            task = asyncio.create_task(self.run(job, adapter, db))
            task.add_done_callback(lambda _: _release_pending_api_work(adapter, reservation))
            reservation["detached"] = True
        self.jobs[key] = job
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return web.json_response(job["result"], status=202)

    def prune(self):
        for key, job in list(self.jobs.items()):
            if job["result"]["status"] != "running" and time.monotonic() - job["created"] > 3600:
                del self.jobs[key]

    async def run(self, job, adapter, db):
        try:
            result = await asyncio.to_thread(execute, adapter, db, *job["payload"])
            job["result"] = {"status": "completed", **result}
        except compression.CommandError as exc:
            job["result"] = {"status": "failed", "error": str(exc)}
        except Exception:
            log.exception("Hermes command failed")
            job["result"] = {
                "status": "failed",
                "error": (
                    "Hermes could not complete this command. Refresh the session before retrying."
                ),
            }

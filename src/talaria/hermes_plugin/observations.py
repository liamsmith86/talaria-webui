"""Small, profile-scoped observations; never stores prompts, replies, or credentials."""

import json
import math
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import closing, suppress


def number(value):
    try:
        return (
            value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
        )
    except OverflowError:
        return None


def label(value):
    return value[:256] if isinstance(value, str) else None


def has_uid(db):
    """Stores from before plugin 1.6 have no column for the durable message ID."""
    return any(column[1] == "uid" for column in db.execute("PRAGMA table_info(responses)"))


def reasoning(request):
    """Read only the reasoning fields actually sent to the provider."""
    request = request.get("body", request)
    if not isinstance(request, dict):
        return None
    extra = request.get("extra_body") or {}
    extra = extra if isinstance(extra, dict) else {}
    value = request.get("reasoning") or extra.get("reasoning") or {}
    thinking = request.get("thinking") or extra.get("thinking") or {}
    value = value if isinstance(value, dict) else {}
    thinking = thinking if isinstance(thinking, dict) else {}
    effort = request.get("reasoning_effort") or value.get("effort")
    if value.get("enabled") is False or thinking.get("type") == "disabled":
        return "none"
    if isinstance(effort, str) and effort in {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    }:
        return effort
    if thinking.get("type") in ("enabled", "adaptive") or value.get("enabled") is True:
        return "enabled"
    return None


class Observations:
    def __init__(self, home):
        self.home = home
        self.pending = OrderedDict()
        self.lock = threading.Lock()

    def connect(self):
        directory = self.home / "talaria"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / "observations.db"
        path.touch(mode=0o600, exist_ok=True)
        db = sqlite3.connect(path, timeout=2)
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS responses (session TEXT, message INTEGER, "
                "data TEXT NOT NULL, PRIMARY KEY(session, message))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS compactions (session TEXT PRIMARY KEY, "
                "data TEXT NOT NULL)"
            )
            if not has_uid(db):
                with suppress(sqlite3.OperationalError):  # Another process just added it.
                    db.execute("ALTER TABLE responses ADD COLUMN uid TEXT")
            db.execute(
                "CREATE INDEX IF NOT EXISTS responses_uid ON responses(uid) WHERE uid IS NOT NULL"
            )
        except sqlite3.Error:
            db.close()
            raise
        return db

    def save(self, session, message, data, uid=None):
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO responses (session, message, data, uid) "
                "VALUES (?, ?, ?, ?)",
                (session, message, json.dumps(data, allow_nan=False), uid),
            )
            db.execute(
                "DELETE FROM responses WHERE rowid IN (SELECT rowid FROM responses "
                "ORDER BY rowid DESC LIMIT -1 OFFSET 10000)"
            )

    def reader(self):
        # Viewing details never creates, touches, or migrates the optional store.
        path = self.home / "talaria" / "observations.db"
        return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2))

    def read(self, session, message=None, uid=None):
        """A reply's details, by the ID it has now or the durable ID that outlives compaction."""
        try:
            with self.reader() as db:
                uids = has_uid(db)
                column = "uid" if uids else "NULL"
                if message is None:
                    row = db.execute(
                        f"SELECT message, data, {column} FROM responses WHERE session = ? "
                        "ORDER BY message DESC LIMIT 1",
                        (session,),
                    ).fetchone()
                else:
                    row = db.execute(
                        f"SELECT message, data, {column} FROM responses "
                        "WHERE session = ? AND message = ?",
                        (session, message),
                    ).fetchone()
                    if row is None and uid and uids:
                        row = db.execute(
                            "SELECT message, data, uid FROM responses WHERE uid = ? "
                            "ORDER BY rowid DESC LIMIT 1",
                            (uid,),
                        ).fetchone()
            data = json.loads(row[1]) if row else None
            if not isinstance(data, dict):
                return None
            return {**data, "message_id": row[0], **({"uid": row[2]} if row[2] else {})}
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
            return None

    def read_compaction(self, session):
        try:
            with self.reader() as db:
                row = db.execute(
                    "SELECT data FROM compactions WHERE session = ?", (session,)
                ).fetchone()
            data = json.loads(row[0]) if row else None
            return data if isinstance(data, dict) else None
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
            return None

    def forget_compaction(self, session):
        self.remove("compactions", [session])

    def remove(self, table, sessions):
        if not (self.home / "talaria" / "observations.db").exists():
            return
        with closing(self.connect()) as db, db:
            for start in range(0, len(sessions), 500):
                chunk = sessions[start : start + 500]
                marks = ",".join("?" * len(chunk))
                db.execute(f"DELETE FROM {table} WHERE session IN ({marks})", chunk)

    def compacted(self, *, session_id=None, old_session_id=None, after_tokens=None, **kwargs):
        """Hermes's size of a compacted context, which no provider request has measured yet."""
        from hermes_constants import get_hermes_home

        tokens = number(after_tokens)
        if not session_id or tokens is None:
            return
        store = Observations(get_hermes_home())
        known = {}
        for session in (old_session_id, session_id):
            known = (session and store.read(session)) or known
        with closing(store.connect()) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO compactions (session, data) VALUES (?, ?)",
                (
                    session_id,
                    json.dumps(
                        {
                            "tokens": tokens,
                            "maximum": known.get("context_max"),
                            "model": known.get("model"),
                            "observed_at": time.time(),
                        },
                        allow_nan=False,
                    ),
                ),
            )
            db.execute(
                "DELETE FROM compactions WHERE rowid IN (SELECT rowid FROM compactions "
                "ORDER BY rowid DESC LIMIT -1 OFFSET 10000)"
            )

    def deleted(self, *, session_id=None, reason=None, deleted_ids=None, **kwargs):
        """Details leave with a deleted session; an archived one can come back."""
        from hermes_constants import get_hermes_home

        if reason == "archive":
            return
        sessions = sorted({session_id, *(deleted_ids or [])} - {None, ""})
        store = Observations(get_hermes_home())
        for table in ("responses", "compactions"):
            store.remove(table, sessions)

    def before(
        self,
        *,
        turn_id=None,
        task_id=None,
        request=None,
        started_at=None,
        model=None,
        provider=None,
        api_request_id=None,
        **kwargs,
    ):
        if not turn_id or not task_id or not isinstance(request, dict):
            return
        key = (task_id, turn_id)
        with self.lock:
            prior = self.pending.pop(key, {})
            self.pending[key] = {
                # A new model call invalidates the previous call's usage and
                # duration. An interrupted request may never emit an after hook.
                "reasoning": reasoning(request),
                "started_at": prior.get("started_at", number(started_at)),
                "model": label(model),
                "provider": label(provider),
                "api_request_id": api_request_id,
            }
            while len(self.pending) > 128:
                self.pending.popitem(last=False)

    def after(
        self,
        *,
        turn_id=None,
        task_id=None,
        usage=None,
        model=None,
        provider=None,
        response_model=None,
        base_url=None,
        api_duration=None,
        ended_at=None,
        finish_reason=None,
        api_request_id=None,
        **kwargs,
    ):
        try:
            from agent.model_metadata import get_cached_context_length

            context_max = number(get_cached_context_length(model, base_url or ""))
        except Exception:
            context_max = None

        with self.lock:
            data = self.pending.get((task_id, turn_id))
            if data is None:
                return
            if api_request_id is not None and data.get("api_request_id") != api_request_id:
                return
            usage = usage if isinstance(usage, dict) else {}
            data.update(
                model=label(response_model) or label(model) or data.get("model"),
                provider=label(provider) or data.get("provider"),
                duration_seconds=number(api_duration),
                observed_at=number(ended_at),
                finish_reason=label(finish_reason),
                usage={
                    key: number(usage.get(source))
                    for key, source in (
                        ("input_tokens", "prompt_tokens"),
                        ("output_tokens", "output_tokens"),
                        ("reasoning_tokens", "reasoning_tokens"),
                        ("cache_read_tokens", "cache_read_tokens"),
                    )
                },
                context_max=context_max,
            )

    def end(
        self,
        *,
        session_id=None,
        turn_id=None,
        task_id=None,
        completed=False,
        failed=False,
        interrupted=False,
        **kwargs,
    ):
        from hermes_state_registry import acquire, release_or_close

        with self.lock:
            data = self.pending.pop((task_id, turn_id), None)
        if (
            not data
            or not session_id
            or not (completed or interrupted)
            or failed
            or (not interrupted and not data.get("usage"))
        ):
            return
        db = acquire(self.home / "state.db")
        try:
            rows = db.get_messages(session_id, limit=1, latest=True)
        finally:
            release_or_close(db)
        # Never attribute an observation to an older reply after a failed persistence.
        if not rows or rows[0].get("role") != "assistant":
            return
        if (number(rows[0].get("timestamp")) or 0) < (data.get("started_at") or 0):
            return
        data.pop("started_at", None)
        data.pop("api_request_id", None)
        data["status"] = "interrupted" if interrupted else "completed"
        self.save(session_id, rows[0]["id"], data, uid=rows[0].get("message_uid"))

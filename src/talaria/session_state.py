"""Talaria's durable record of session activity that Hermes does not keep for clients.

Hermes owns transcripts. It does not record which commands a client ran in a
session, or the model and reasoning a person chose before sending. Keeping
these here makes them the same on every device, after reloads, and across
Talaria and Hermes restarts. Browser storage is never the record of either.
"""

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

SCHEMA_VERSION = 1
# Rows are small. Bound each profile so a long-lived installation stays light.
MAX_COMMANDS = 4000
MAX_PREFERENCES = 4000
MAX_TEXT = 64000
# A session and the sessions its compaction lineage continues.
MAX_LINEAGE = 32

SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    profile TEXT NOT NULL,
    id TEXT NOT NULL,
    session TEXT NOT NULL,
    command TEXT NOT NULL,
    args TEXT NOT NULL,
    -- Hermes's time for the session's newest row when the command began.
    anchor REAL,
    started REAL NOT NULL,
    -- running, completed, failed, or unknown: Hermes never reported a result.
    status TEXT NOT NULL,
    finished REAL,
    changed INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    result_session TEXT,
    dismissed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (profile, id)
);
CREATE INDEX IF NOT EXISTS commands_by_session ON commands (profile, session);
CREATE INDEX IF NOT EXISTS commands_by_result ON commands (profile, result_session);
CREATE INDEX IF NOT EXISTS commands_by_age ON commands (profile, started);
CREATE INDEX IF NOT EXISTS commands_running ON commands (status) WHERE status = 'running';
CREATE TABLE IF NOT EXISTS preferences (
    profile TEXT NOT NULL,
    session TEXT NOT NULL,
    -- SQL NULL in either column: no choice, so the parent session's applies,
    -- or else Hermes's session model and automatic reasoning.
    -- model: JSON, null for Hermes's default model or {"id", "provider"}.
    model TEXT,
    reasoning TEXT,
    updated REAL NOT NULL,
    PRIMARY KEY (profile, session)
);
CREATE INDEX IF NOT EXISTS preferences_by_age ON preferences (profile, updated);
"""

FIND_COMMAND = "SELECT * FROM commands WHERE profile = ? AND id = ?"
# A choice replaces what it names.
CHOOSE = (
    "INSERT INTO preferences (profile, session, model, reasoning, updated) "
    "VALUES (?, ?, ?, ?, ?) ON CONFLICT (profile, session) DO UPDATE SET "
    "model = COALESCE(excluded.model, model), "
    "reasoning = COALESCE(excluded.reasoning, reasoning), updated = excluded.updated"
)
# An inherited or imported choice only fills what the session has not chosen.
INHERIT = (
    "INSERT INTO preferences (profile, session, model, reasoning, updated) "
    "VALUES (?, ?, ?, ?, ?) ON CONFLICT (profile, session) DO UPDATE SET "
    "model = COALESCE(model, excluded.model), "
    "reasoning = COALESCE(reasoning, excluded.reasoning), updated = excluded.updated"
)


def state_path(config: Path) -> Path:
    return config.with_name(config.stem + ".state.db")


class StateUnavailable(Exception):
    """The state file cannot be read or written."""


def command_record(row):
    return {
        "id": row["id"],
        "session_id": row["session"],
        "result_session_id": row["result_session"],
        "command": row["command"],
        "args": row["args"],
        "status": row["status"],
        "changed": bool(row["changed"]),
        "text": row["text"],
        "anchor": row["anchor"],
        "started_at": row["started"],
        "finished_at": row["finished"],
    }


def preference_record(model, reasoning):
    record = {}
    if model is not None:
        record["model"] = json.loads(model)
    if reasoning is not None:
        record["reasoning"] = reasoning
    return record


def choice_columns(changes):
    """A validated choice as its columns. Whatever it does not name stays NULL."""
    model = json.dumps(changes["model"], sort_keys=True) if "model" in changes else None
    return model, changes.get("reasoning")


def prune(db, table, order, profile, limit):
    db.execute(
        f"DELETE FROM {table} WHERE profile = ? AND rowid IN (SELECT rowid FROM {table} "
        f"WHERE profile = ? ORDER BY {order} DESC LIMIT -1 OFFSET ?)",
        (profile, profile, limit),
    )


class SessionState:
    def __init__(self, config_path: Path):
        self.path = state_path(config_path)
        self.ready = False

    def connect(self):
        try:
            if not self.ready:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Command output and choices are private; never rely on the umask.
                os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
            db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            if not self.ready:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    db.close()
                    raise StateUnavailable(f"{self.path} is from a newer Talaria release.")
                db.executescript(SCHEMA)
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self.ready = True
            return db
        except (OSError, sqlite3.Error) as exc:
            raise StateUnavailable(str(exc)) from exc

    def read(self, query, parameters=()):
        """The rows of one query, which reads a consistent snapshot by itself."""
        try:
            with closing(self.connect()) as db:
                return db.execute(query, parameters).fetchall()
        except sqlite3.Error as exc:
            raise StateUnavailable(str(exc)) from exc

    def write(self, operation):
        """Run operation(db) as one transaction, holding the write lock from the start."""
        try:
            with closing(self.connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    result = operation(db)
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                db.execute("COMMIT")
                return result
        except sqlite3.Error as exc:
            raise StateUnavailable(str(exc)) from exc

    # Commands

    def start_command(self, profile, command_id, session, command, args, anchor):
        """Record a command before Hermes sees it. Returns (record, created)."""

        def operation(db):
            if row := db.execute(FIND_COMMAND, (profile, command_id)).fetchone():
                return command_record(row), False
            db.execute(
                "INSERT INTO commands (profile, id, session, command, args, anchor, started, "
                "status) VALUES (?, ?, ?, ?, ?, ?, ?, 'running')",
                (profile, command_id, session, command, args, anchor, time.time()),
            )
            prune(db, "commands", "started", profile, MAX_COMMANDS)
            return command_record(db.execute(FIND_COMMAND, (profile, command_id)).fetchone()), True

        return self.write(operation)

    def finish_command(self, profile, command_id, status, text="", changed=False, result=None):
        self.write(
            lambda db: db.execute(
                "UPDATE commands SET status = ?, text = ?, changed = ?, result_session = ?, "
                "finished = ? WHERE profile = ? AND id = ? AND status = 'running'",
                (status, text[:MAX_TEXT], int(changed), result, time.time(), profile, command_id),
            )
        )

    def discard_command(self, profile, command_id):
        """Forget a command Hermes refused before running it."""
        self.write(
            lambda db: db.execute(
                "DELETE FROM commands WHERE profile = ? AND id = ?", (profile, command_id)
            )
        )

    def command(self, profile, command_id):
        rows = self.read(FIND_COMMAND, (profile, command_id))
        return command_record(rows[0]) if rows else None

    def session_commands(self, profile, sessions):
        """Undismissed commands that ran in, or rotated into, any of these sessions."""
        sessions = list(dict.fromkeys(s for s in sessions if s))[:MAX_LINEAGE]
        if not sessions:
            return []
        marks = ", ".join("?" * len(sessions))
        rows = self.read(
            f"SELECT * FROM commands WHERE profile = ? AND dismissed = 0 AND "
            f"(session IN ({marks}) OR result_session IN ({marks})) "
            "ORDER BY started DESC LIMIT 200",
            (profile, *sessions, *sessions),
        )
        return [command_record(row) for row in reversed(rows)]

    def dismiss_command(self, profile, command_id):
        def operation(db):
            cursor = db.execute(
                "UPDATE commands SET dismissed = 1 WHERE profile = ? AND id = ? "
                "AND status != 'running'",
                (profile, command_id),
            )
            return cursor.rowcount > 0

        return self.write(operation)

    def running_commands(self):
        """(profile, record) for every command still running, oldest first."""
        rows = self.read("SELECT * FROM commands WHERE status = 'running' ORDER BY started")
        return [(row["profile"], command_record(row)) for row in rows]

    # Preferences

    def preferences(self, profile, session, parent=None):
        """A session's choice. Whatever it has not chosen comes from its parent.

        A compaction continuation or branch starts with its parent's choice and
        keeps it, so a later continuation inherits it in turn.
        """
        sessions = [s for s in dict.fromkeys((session, parent)) if s]
        marks = ", ".join("?" * len(sessions))
        rows = {
            row["session"]: row
            for row in self.read(
                f"SELECT session, model, reasoning FROM preferences "
                f"WHERE profile = ? AND session IN ({marks})",
                (profile, *sessions),
            )
        }
        chosen = [rows[s] for s in sessions if s in rows]
        model = next((row["model"] for row in chosen if row["model"] is not None), None)
        reasoning = next((row["reasoning"] for row in chosen if row["reasoning"] is not None), None)
        own = rows.get(session)
        if (model, reasoning) != ((own["model"], own["reasoning"]) if own else (None, None)):
            self.write(
                lambda db: db.execute(INHERIT, (profile, session, model, reasoning, time.time()))
            )
        return preference_record(model, reasoning)

    def set_preferences(self, profile, session, changes):
        """Save a choice: changes holds "model" (dict or None), "reasoning", or both."""

        def operation(db):
            db.execute(CHOOSE, (profile, session, *choice_columns(changes), time.time()))
            prune(db, "preferences", "updated", profile, MAX_PREFERENCES)

        self.write(operation)

    def import_preferences(self, profile, choices):
        """Adopt choices {session: changes}, never replacing one already saved."""

        def operation(db):
            now = time.time()
            for session, changes in choices.items():
                db.execute(INHERIT, (profile, session, *choice_columns(changes), now))
            prune(db, "preferences", "updated", profile, MAX_PREFERENCES)

        self.write(operation)

    # Cleanup

    def forget_session(self, profile, session):
        def operation(db):
            db.execute(
                "DELETE FROM commands WHERE profile = ? AND (session = ? OR result_session = ?)",
                (profile, session, session),
            )
            db.execute(
                "DELETE FROM preferences WHERE profile = ? AND session = ?", (profile, session)
            )

        self.write(operation)

    def forget_profile(self, profile):
        def operation(db):
            db.execute("DELETE FROM commands WHERE profile = ?", (profile,))
            db.execute("DELETE FROM preferences WHERE profile = ?", (profile,))

        self.write(operation)

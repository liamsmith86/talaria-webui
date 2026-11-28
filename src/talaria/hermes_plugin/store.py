"""A profile's session storage, as each supported Hermes hands it to the API adapter.

Hermes 0.23.3 moved storage behind a store handle (``persistence``); earlier releases hand out a
``SessionDB``. Both offer the plugin the same reads, as the row dictionaries Hermes's API serves,
and a rewind (see ``Store.rewind`` for how its guard differs). Drop ``Legacy`` once the plugin
requires Hermes 0.23.3.
"""

import inspect
import time


async def open_store(adapter):
    """The store of the request's profile, or None when Hermes has none for it."""
    if callable(getattr(adapter, "_ensure_store_async", None)):
        handle = await adapter._ensure_store_async()
        return None if handle is None else Store(handle)
    db = await adapter._ensure_session_db_async()
    return None if db is None else Legacy(db)


def open_store_sync(adapter):
    if callable(getattr(adapter, "_ensure_store", None)):
        handle = adapter._ensure_store()
        return None if handle is None else Store(handle)
    db = adapter._ensure_session_db()
    return None if db is None else Legacy(db)


def compatible(adapter):
    return any(
        callable(getattr(adapter, name, None))
        for name in ("_ensure_store_async", "_ensure_session_db_async")
    )


def newest_row(home, sid):
    """The newest active row of a session in the profile at ``home``, as a list of at most one."""
    try:
        from persistence_assembly import open_persistence, profile_scope
    except ImportError:  # Before 0.23.3
        from hermes_state_registry import acquire, release_or_close

        db = acquire(home / "state.db")
        try:
            return db.get_messages(sid, limit=1, latest=True)
        finally:
            release_or_close(db)
    with open_persistence(profile_scope(home=home)) as handle:
        return Store(handle).latest(sid, 1)


def user_turn_parts(row):
    """Split a user row into its compaction handoff and the person's own message."""
    try:
        from session_rules.turns import split_user_originated_turn
    except ImportError:  # Before 0.23.3
        from agent.context_compressor import split_user_originated_turn
    return split_user_originated_turn(row)


class Legacy:
    """Hermes before 0.23.3."""

    def __init__(self, db):
        self.db = db

    def session(self, sid):
        return self.db.get_session(sid)

    def active(self, sid, limit):
        return self.db.get_messages(sid, limit=limit)

    def active_ids(self, sid):
        return list(self.db.get_active_message_ids(sid))

    def message(self, sid, mid):
        """The active row with this ID, or None."""
        rows = self.db.get_messages(sid, after_id=mid - 1, limit=1)
        return rows[0] if rows and rows[0]["id"] == mid else None

    def latest(self, sid, limit):
        return self.db.get_messages(sid, limit=limit, latest=True)

    def displayed(self, sid):
        return self.db.get_messages(sid, include_compacted=True)

    def conversation(self, sid):
        """The display rows of this session's conversation and the key they're read under.

        A compaction copies kept turns into a newer row, and rotation into a new session. Hermes's
        display keeps one row for each; versions before 0.22 can only do so within one session.
        """
        if "include_ancestors" not in inspect.signature(self.db.get_messages).parameters:
            return sid, lambda: self.displayed(sid)
        tip = self.db.get_compression_tip(sid)
        return tip, lambda: self.db.get_messages(
            tip, include_compacted=True, include_ancestors=True
        )

    def stored(self, sid, mid):
        """The row with this ID whatever its visibility, or None."""
        window = self.db.get_messages_around(sid, mid, window=0)["window"]
        return window[0] if window else None

    def search(self, query, limit, offset, fields):
        return self.db.search_messages(query, limit=limit, offset=offset, fields=fields)

    def searchable(self, fields):
        try:
            inspect.signature(self.db.search_messages).bind(
                "query", limit=1, offset=0, fields=fields
            )
            inspect.signature(self.db.get_messages_around).bind("session", 1, window=5)
            return True
        except (AttributeError, TypeError, ValueError):
            return False

    def branch_title(self, source, title):
        """The title a branch of ``source`` takes; ``ValueError`` when it can't."""
        if title is None:
            title = self.db.get_next_title_in_lineage(source.get("title") or "fork")
        title = self.db.sanitize_title(str(title))
        if title and self.db.get_session_by_title(title):
            raise ValueError("That session name is already in use.")
        return title

    def pending_turn(self, sid):
        """Whether a turn another client sent is queued, running or of unknown outcome."""
        try:
            from hermes_state_runtime import list_session_admissions
        except ImportError:  # Before 0.22.1 turns are not admitted, and the turn lease suffices.
            return False
        return bool(list_session_admissions(self.db, session_id=sid))

    def compressible(self):
        return all(
            callable(getattr(self.db, name, None))
            for name in ("try_acquire_session_turn_lease", "resolve_resume_session_id")
        )

    def turn_lease(self, agent, sid, holder, ttl):
        """The session's turn lease, held for ``agent``; None while another turn holds it."""
        from agent.turn_facade_lease import DurableTurnLease

        if not self.db.try_acquire_session_turn_lease(sid, holder, ttl_seconds=ttl):
            return None
        return DurableTurnLease(agent, self.db, sid, holder)

    def resume_target(self, sid):
        return self.db.resolve_resume_session_id(sid)

    def replay(self, sid, max_messages):
        """The conversation a turn would load, with row IDs; ``ResumeTooLarge`` above the limit."""
        self.db.assert_resume_safe(sid, max_messages=max_messages, tip_only=True)
        return self.db.get_messages_as_conversation(sid, include_row_ids=True)

    def rewindable(self):
        try:
            if not callable(getattr(self.db, "get_active_message_ids", None)):
                return False
            inspect.signature(self.db.rewind_to_message).bind(
                "session",
                1,
                expected_active_ids=[],
                preserve_compaction_handoff=False,
                expected_target_content="",
            )
            return True
        except (AttributeError, TypeError, ValueError):
            return False

    def rewind(self, sid, active, target, handoff, content):
        """Rewind ``active[target]`` and every later row, unless the active rows changed."""
        result = self.db.rewind_to_message(
            sid,
            active[target]["id"],
            expected_active_ids=[row["id"] for row in active],
            preserve_compaction_handoff=handoff is not None,
            expected_target_content=content,
        )
        return result["rewound_count"]


class Store:
    """Hermes 0.23.3+."""

    def __init__(self, handle):
        self.handle = handle

    def read(self, sid, **request):
        from persistence.conversation.messages import TranscriptRead

        rows = self.handle.messages.read_transcript(sid, TranscriptRead(**request))
        return [row.as_wire_row() for row in rows]

    def session(self, sid):
        record = self.handle.sessions.get(sid)
        return None if record is None else record.as_session_row()

    def active(self, sid, limit):
        return self.read(sid, limit=limit)

    def active_ids(self, sid):
        return list(self.handle.messages.active_message_ids(sid))

    def message(self, sid, mid):
        rows = self.read(sid, after_id=mid - 1, limit=1)
        return rows[0] if rows and rows[0]["id"] == mid else None

    def latest(self, sid, limit):
        return self.read(sid, limit=limit, latest=True)

    def displayed(self, sid):
        from persistence.conversation.messages import TranscriptView

        return self.read(sid, view=TranscriptView.DISPLAY)

    def conversation(self, sid):
        from persistence.conversation.messages import TranscriptView

        tip = self.handle.lineage.tip(sid)
        return tip, lambda: self.read(tip, view=TranscriptView.DISPLAY, include_lineage=True)

    def stored(self, sid, mid):
        from persistence import RowVisibility

        window = self.handle.messages.read_around(sid, mid, window=0).window
        if not window:
            return None
        row = window[0]
        return {
            "id": row.id,
            "role": row.role,
            "display_kind": row.display_kind,
            "active": row.visibility is RowVisibility.ACTIVE,
            "compacted": row.visibility is RowVisibility.COMPACTION_ARCHIVED,
        }

    def search(self, query, limit, offset, fields):
        hits = self.handle.search.search_messages(query, limit=limit, offset=offset, fields=fields)
        return [
            {
                "id": hit.message_id,
                **{name: getattr(hit, name) for name in fields if name != "id"},
            }
            for hit in hits
        ]

    def searchable(self, fields):
        return True

    def branch_title(self, source, title):
        from persistence.conversation.sessions import sanitize_title

        if title is None:
            title = self.handle.sessions.next_title_in_lineage(source.get("title") or "fork")
        title = sanitize_title(str(title))
        if title and self.handle.sessions.get_by_title(title):
            raise ValueError("That session name is already in use.")
        return title

    def pending_turn(self, sid):
        return bool(self.handle.admissions.list(session_id=sid))

    def compressible(self):
        return True

    def turn_lease(self, agent, sid, holder, ttl):
        from agent.turn_facade_lease import DurableTurnLease

        # Taken before acquiring, so it is never later than the lease's own expiry.
        expires_at = time.time() + ttl
        coordination = self.handle.coordination
        if not coordination.try_acquire_turn_lease(session_id=sid, holder=holder, ttl_seconds=ttl):
            return None
        return DurableTurnLease(
            agent, self.handle, sid, holder, expires_at=expires_at, persistence=self.handle
        )

    def resume_target(self, sid):
        return self.handle.lineage.resolve_resume_target(sid)

    def replay(self, sid, max_messages):
        from persistence.conversation.messages import ConversationRequest

        messages = self.handle.messages
        messages.assert_resume_safe(sid, max_messages=max_messages, tip_only=True)
        request = ConversationRequest(include_row_ids=True)
        return [message.as_openai_message() for message in messages.read_conversation(sid, request)]

    def rewindable(self):
        return True

    def rewind(self, sid, active, target, handoff, content):
        """Hermes's store rewinds only by user-turn ordinal, which skips the rows API turns are
        saved as. Replace the active transcript with the rows before the target instead: the store
        keeps that prefix in place and marks the rest rewound, as its own rewind does.

        Unlike ``Legacy``, the write can't pin the active rows, so they're checked again just
        before it, and a compaction holding its lock refuses; the write itself refuses while a turn
        or compaction holds the session. Something written between that check and the write is
        rewound too: a Hermes limitation."""
        from persistence.conversation.messages import NewMessage, ReplaceMode

        kept = [NewMessage.from_live(row) for row in active[:target]]
        if handoff is not None:
            kept.append(NewMessage.from_live(handoff))
        if self.active_ids(sid) != [row["id"] for row in active]:
            raise RuntimeError("The session changed. Reopen the action and try again.")
        if self.handle.coordination.get_compression_lock_holder(session_id=sid) is not None:
            raise RuntimeError("The session is being compressed.")
        self.handle.messages.replace_transcript(
            sid, kept, ReplaceMode.ARCHIVE_DROPPED, reject_active_turn_lease=True
        )
        return len(active) - target

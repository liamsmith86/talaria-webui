"""A profile's session storage, through the store handle Hermes gives the API adapter.

The handle (``persistence``) offers the reads the plugin needs, as the row dictionaries Hermes's
API serves, and a transcript replace the rewind uses (see ``Store.rewind`` for its guard).
"""

import time


async def open_store(adapter):
    """The store of the request's profile, or None when Hermes has none for it."""
    handle = await adapter._ensure_store_async()
    return None if handle is None else Store(handle)


def open_store_sync(adapter):
    handle = adapter._ensure_store()
    return None if handle is None else Store(handle)


def compatible(adapter):
    return callable(getattr(adapter, "_ensure_store_async", None)) and callable(
        getattr(adapter, "_ensure_store", None)
    )


def newest_row(home, sid):
    """The newest active row of a session in the profile at ``home``, as a list of at most one."""
    from persistence_assembly import open_persistence, profile_scope

    with open_persistence(profile_scope(home=home)) as handle:
        return Store(handle).latest(sid, 1)


def user_turn_parts(row):
    """Split a user row into its compaction handoff and the person's own message."""
    from session_rules.turns import split_user_originated_turn

    return split_user_originated_turn(row)


class Store:
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

    def rewind(self, sid, active, target, handoff, content):
        """Hermes's store rewinds only by user-turn ordinal, which skips the rows API turns are
        saved as. Replace the active transcript with the rows before the target instead: the store
        keeps that prefix in place and marks the rest rewound, as its own rewind does.

        The write can't pin the active rows, so they're checked again just before it, and a
        compaction holding its lock refuses; the write itself refuses while a turn or compaction
        holds the session. Something written between that check and the write is rewound too: a
        Hermes limitation."""
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

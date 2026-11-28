"""Session fixtures for the native contracts, through the store handle Hermes gives its adapter.

``StoreFixtures`` offers the fixture calls the contracts make through the store's verbs, in the
row shapes Hermes's API serves.
"""

from pathlib import Path

from persistence_assembly import open_persistence, profile_scope

from talaria.hermes_plugin.store import Store


def open_db(home):
    """Fixtures for the profile at ``home``: one ``state.db`` per profile folder."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    return StoreFixtures(open_persistence(profile_scope(home=home)), home)


def native(db):
    """What this Hermes's API adapter holds as its store, for ``adapter._session_db``."""
    return db.handle


def plugin_store(db):
    """The plugin's view of the same store."""
    return Store(db.handle)


def serve(adapter_class, db):
    """Give a stand-in API adapter Hermes's store accessors, answering ``db``."""

    async def ensure_async(self):
        return db.handle

    adapter_class._ensure_store_async = ensure_async
    adapter_class._ensure_store = lambda self: db.handle
    return adapter_class


def adapter_db(adapter):
    """Fixtures over the store the API adapter answers for the active profile."""
    return StoreFixtures(adapter._ensure_store(), None)


class StoreFixtures:
    """The fixture calls the contracts make, named and shaped as Hermes's former ``SessionDB``."""

    def __init__(self, handle, home):
        from persistence.conversation.sessions import MAX_TITLE_LENGTH

        self.handle = handle
        self.db_path = None if home is None else home / "state.db"
        self.MAX_TITLE_LENGTH = MAX_TITLE_LENGTH

    def close(self):
        self.handle.close()

    def create_session(self, session_id, source, **kwargs):
        from persistence.conversation.sessions import NewSession

        return self.handle.sessions.create(NewSession(session_id, source, **kwargs))

    def end_session(self, session_id, reason):
        self.handle.sessions.end(session_id, reason)

    def get_session(self, session_id):
        record = self.handle.sessions.get(session_id)
        return None if record is None else record.as_session_row()

    def session_count(self):
        return self.handle.sessions.count_sessions()

    def set_session_title(self, session_id, title):
        return self.handle.sessions.set_title(session_id, title)

    def get_session_title(self, session_id):
        return self.handle.sessions.get_title(session_id)

    def get_session_title_source(self, session_id):
        return self.get_session(session_id)["title_source"]

    def get_session_by_title(self, title):
        record = self.handle.sessions.get_by_title(title)
        return None if record is None else record.as_session_row()

    def get_session_model_config_value(self, session_id, key):
        return self.handle.sessions.get_model_config_value(session_id, key)

    def set_session_hidden(self, session_id, hidden):
        return self.handle.sessions.set_hidden(session_id, hidden)

    def update_system_prompt(self, session_id, prompt):
        self.handle.sessions.update_system_prompt(session_id, prompt)

    def update_session_runtime_lock(self, session_id, **lock):
        from persistence.conversation.sessions import RuntimeLock

        self.handle.sessions.set_runtime_lock(session_id, RuntimeLock(**lock))

    def resolve_resume_session_id(self, session_id):
        return self.handle.lineage.resolve_resume_target(session_id)

    def append_message(self, session_id, role, content=None, *, tool_calls=None, **fields):
        from persistence.conversation.messages import NewMessage

        message = NewMessage(role, content, tool_calls=tuple(tool_calls or ()), **fields)
        return self.handle.messages.append(session_id, message)

    def get_messages(
        self, session_id, include_inactive=False, include_compacted=False, include_ancestors=False
    ):
        from persistence.conversation.messages import TranscriptRead, TranscriptView

        view = (
            TranscriptView.ALL
            if include_inactive
            else TranscriptView.DISPLAY
            if include_compacted
            else TranscriptView.ACTIVE
        )
        request = TranscriptRead(view=view, include_lineage=include_ancestors)
        rows = self.handle.messages.read_transcript(session_id, request)
        return [row.as_wire_row() for row in rows]

    def get_active_message_ids(self, session_id):
        return list(self.handle.messages.active_message_ids(session_id))

    def get_messages_as_conversation(self, session_id):
        rows = self.handle.messages.read_conversation(session_id)
        return [row.as_openai_message() for row in rows]

    def rewind_to_message(self, session_id, message_id):
        """Rewind the row with this ID and every later one, as ``SessionDB`` did."""
        from persistence.conversation.messages import NewMessage, ReplaceMode

        kept = [
            NewMessage.from_live(row)
            for row in self.get_messages(session_id)
            if row["id"] < message_id
        ]
        self.handle.messages.replace_transcript(session_id, kept, ReplaceMode.ARCHIVE_DROPPED)

    def archive_and_compact(self, session_id, messages):
        from persistence.conversation.messages import NewMessage

        replacement = [NewMessage.from_live(message) for message in messages]
        self.handle.messages.compact(session_id, replacement)

    def try_acquire_session_turn_lease(self, session_id, holder, ttl_seconds=300.0):
        return self.handle.coordination.try_acquire_turn_lease(
            session_id=session_id, holder=holder, ttl_seconds=ttl_seconds
        )

    def release_session_turn_lease(self, session_id, holder):
        self.handle.coordination.release_turn_lease(session_id=session_id, holder=holder)

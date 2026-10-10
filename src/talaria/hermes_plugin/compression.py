"""HTTP surface for Hermes's native manual compression primitives and durable lease."""

import inspect
import os
import uuid
from contextlib import contextmanager


class CommandError(ValueError):
    """Expected command feedback safe to return to the authenticated client."""


def supported(adapter, db):
    try:
        from agent.conversation_compression_manual import compress_now, parse_compress_args
        from agent.turn_facade_lease import DurableTurnLease
        from run_agent import AIAgent

        inspect.signature(AIAgent._compress_context).bind(
            None, [], None, force=True, defer_context_engine_notification=True, verbatim_tail=[]
        )
        return all(
            callable(value)
            for value in (
                DurableTurnLease,
                compress_now,
                parse_compress_args,
                AIAgent._compress_context,
                getattr(adapter, "_effective_session_runtime_request", None),
                getattr(db, "try_acquire_session_turn_lease", None),
                getattr(db, "resolve_resume_session_id", None),
            )
        )
    except (ImportError, AttributeError, TypeError):
        return False


def create_agent(adapter, session):
    from .context import ProfileRunAdapter, load_context

    runtime = adapter._effective_session_runtime_request(session=session, body={})
    locked = bool(runtime.get("require_model_lock"))
    model = adapter._stored_session_model(session)
    route = runtime.get("route") if locked else adapter._resolve_route(model)
    agent = ProfileRunAdapter(adapter, load_context())._create_agent(
        session_id=session["id"],
        gateway_session_key=f"talaria:{session['id']}",
        route=route,
        session_model=None if locked or route else model,
        model_options=runtime.get("model_options"),
        confirmed_runtime_lock=locked,
    )
    # The existing transcript owns its prompt; the temporary agent must not regenerate it.
    if session.get("system_prompt"):
        agent._cached_system_prompt = session["system_prompt"]
    agent._session_db_created = True
    agent._end_session_on_close = False
    return agent


@contextmanager
def session_lease(agent, db, sid):
    from agent.turn_facade_lease import LEASE_TTL_SECONDS, DurableTurnLease

    try:
        from hermes_state_pidns import holder_namespace_token
    except ImportError:
        namespace = ""
    else:
        namespace = holder_namespace_token()
    holder = f"pid={os.getpid()}{namespace}:talaria-command={uuid.uuid4().hex}"
    if not db.try_acquire_session_turn_lease(sid, holder, ttl_seconds=LEASE_TTL_SECONDS):
        raise CommandError("This session is busy. Wait for the response to finish.")
    lease = DurableTurnLease(agent, db, sid, holder)
    agent._active_session_turn_lease_holder = holder
    agent._active_session_turn_lease_ttl_seconds = LEASE_TTL_SECONDS
    try:
        lease.start()
        yield
    finally:
        lease.stop_refresher()
        lease.join_threads()
        lease.clear_interrupt()
        lease.release()


def compress(adapter, db, sid, args):
    from agent.conversation_compression import finalize_context_engine_compression_notification

    session = db.get_session(sid)
    if not session or session.get("source") != "api_server":
        raise CommandError("Branch this session into an API session before compressing it.")
    agent = create_agent(adapter, session)
    try:
        with session_lease(agent, db, sid):
            # Resolve only after admission: another client may have rotated this transcript.
            canonical = db.resolve_resume_session_id(sid)
            if canonical != sid:
                raise CommandError("This session has moved. Refresh it before compressing.")
            db.assert_resume_safe(sid, max_messages=20000, tip_only=True)
            history = db.get_messages_as_conversation(sid, include_row_ids=True)
            result = compress_history(agent, history, args)
            return {
                "session_id": agent.session_id,
                "text": result,
                "changed": agent.session_id != sid
                or getattr(agent, "_last_compaction_in_place", False) is True,
            }
    finally:
        finalize_context_engine_compression_notification(agent, committed=False)
        agent.close()


def compress_history(agent, history, args):
    from agent.conversation_compression import finalize_context_engine_compression_notification
    from agent.conversation_compression_manual import (
        AGGRESSIVE_UNSUPPORTED,
        MIN_MESSAGES,
        compress_now,
        parse_compress_args,
        render_compress_result,
    )

    if len(history) < MIN_MESSAGES:
        return "Not enough history to compress (need at least four messages)."
    request = parse_compress_args(args)
    if request.aggressive:
        raise CommandError(AGGRESSIVE_UNSUPPORTED)
    if not request.preview and getattr(agent, "api_mode", None) == "codex_app_server":
        raise CommandError("Compress this model's live thread from its Hermes client.")
    prior_session = agent.session_id
    result = compress_now(agent, history, request)
    lines = render_compress_result(result)
    if result.status == "lock_skipped":
        raise CommandError("\n".join(lines))
    if result.status == "compressed":
        # Match Hermes's CLI: in-place compaction commits its tail atomically;
        # a rotated continuation needs the returned transcript flushed as well.
        if agent.session_id != prior_session:
            agent._flush_messages_to_session_db(result.after_messages, None)
        finalize_context_engine_compression_notification(agent, committed=True)
    return "\n".join(lines)

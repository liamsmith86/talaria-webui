"""HTTP surface for Hermes's native manual compression primitives and durable lease."""

import inspect
import os
import uuid
from contextlib import contextmanager
from functools import wraps


class CommandError(ValueError):
    """Expected command feedback safe to return to the authenticated client."""


def supported(adapter, db):
    try:
        from agent.turn_facade_lease import DurableTurnLease
        from hermes_cli import partial_compress
        from run_agent import AIAgent

        inspect.signature(AIAgent._compress_context).bind(
            None, [], None, force=True, defer_context_engine_notification=True
        )
        return all(
            callable(value)
            for value in (
                DurableTurnLease,
                partial_compress.extract_compress_flags,
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

    holder = f"pid={os.getpid()}:talaria-command={uuid.uuid4().hex}"
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
    from agent.model_metadata import estimate_request_tokens_rough
    from hermes_cli.partial_compress import (
        extract_compress_flags,
        parse_partial_compress_args,
        summarize_compress_preview,
    )

    if len(history) < 4:
        return "Not enough history to compress (need at least four messages)."
    raw, preview, aggressive = extract_compress_flags(args)
    if aggressive:
        raise CommandError("Hermes does not support --aggressive. Use /compress here [N].")
    partial, keep, focus = parse_partial_compress_args(raw)
    estimate = {
        "system_prompt": getattr(agent, "_cached_system_prompt", "") or "",
        "tools": getattr(agent, "tools", None) or None,
    }
    before = estimate_request_tokens_rough(history, **estimate)
    if preview:
        return "\n".join(summarize_compress_preview(history, partial, keep, focus, before)["lines"])
    if getattr(agent, "api_mode", None) == "codex_app_server":
        raise CommandError("Compress this model's live thread from its Hermes client.")
    compressed = apply_compression(agent, history, partial, keep, focus, before)
    from agent.manual_compression_feedback import summarize_manual_compression

    report = summarize_manual_compression(
        history,
        compressed,
        before,
        estimate_request_tokens_rough(compressed, **estimate),
        compression_state=agent.context_compressor,
    )
    return "\n".join(filter(None, (report["headline"], report["token_line"], report["note"])))


def apply_compression(agent, history, partial, keep, focus, before):
    from agent.conversation_compression import finalize_context_engine_compression_notification
    from agent.manual_compression_feedback import describe_compression_lock_skip

    with partial_compressor(agent.context_compressor, partial, keep):
        compressed, _ = agent._compress_context(
            history,
            None,
            approx_tokens=before,
            focus_topic=focus,
            force=True,
            defer_context_engine_notification=True,
        )
    skipped = getattr(agent, "_compression_skipped_due_to_lock", None)
    if skipped is True or isinstance(skipped, str):
        raise CommandError(describe_compression_lock_skip(skipped))
    finalize_context_engine_compression_notification(agent, committed=True)
    return compressed


@contextmanager
def partial_compressor(compressor, partial, keep):
    """Rejoin BEFORE Hermes commits, so its native atomic write includes the tail.

    Only this command's temporary compressor gets an adapted callable. The full
    history still reaches Hermes's lease, watermark, anti-growth and commit logic.
    Rejoining after _compress_context (as older CLI handlers do) loses the tail
    when the core now commits in place rather than rotating the session.
    """
    from agent.context_compressor import _strip_persistence_markers
    from hermes_cli.partial_compress import (
        rejoin_compressed_head_and_tail,
        split_history_for_partial_compress,
    )

    if not partial:
        yield
        return
    original = compressor.compress
    previous = vars(compressor).get("compress")

    @wraps(original)
    def compress_with_tail(messages, **kwargs):
        head, tail = split_history_for_partial_compress(messages, keep)
        compressed = original(head, **kwargs)
        preserved = [{**message, "_compaction_tail": True} for message in tail]
        result = rejoin_compressed_head_and_tail(compressed, preserved)
        _strip_persistence_markers(result)
        return result

    compressor.compress = compress_with_tail
    try:
        yield
    finally:
        if previous is None:
            del compressor.compress
        else:
            compressor.compress = previous

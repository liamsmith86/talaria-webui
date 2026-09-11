"""Profile-owned instructions and examples. Only their status crosses the API."""

import asyncio
import inspect
import json
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from .files import read_text
from .request_log import scoped_run
from .runtime import inherit_defaults, inherit_options, session_runtime
from .surface import apply_surface

log = logging.getLogger(__name__)
MAX_INSTRUCTIONS = 64 * 1024
MAX_PREFILL = 256 * 1024
MAX_MESSAGES = 128


@dataclass
class ProfileContext:
    instructions: str = ""
    prefill: list = field(default_factory=list)
    instructions_status: str = "unavailable"
    prefill_status: str = "unavailable"

    def public(self):
        return {
            "instructions": self.instructions_status,
            "prefill": self.prefill_status,
            "prefill_messages": len(self.prefill),
        }


def load_context():
    result = ProfileContext()
    try:
        from agent.secret_scope import get_secret
        from gateway.run import _load_gateway_runtime_config
        from hermes_cli.config import resolve_ephemeral_system_prompt_from_config
        from hermes_constants import get_hermes_home

        cfg = _load_gateway_runtime_config()
    except Exception:
        log.warning("Talaria could not read the Hermes profile context")
        return result
    try:
        prompt = get_secret("HERMES_EPHEMERAL_SYSTEM_PROMPT", "") or (
            resolve_ephemeral_system_prompt_from_config(cfg)
        )
        if not isinstance(prompt, str) or len(prompt) > MAX_INSTRUCTIONS:
            raise ValueError("Unsupported instructions")
        result.instructions = prompt
        result.instructions_status = "ready" if prompt else "not_configured"
    except Exception:
        log.warning("Talaria could not inherit this profile's agent instructions")
    try:
        agent = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
        source = (
            get_secret("HERMES_PREFILL_MESSAGES_FILE", "")
            or cfg.get("prefill_messages_file")
            or agent.get("prefill_messages_file")
        )
        if not source:
            result.prefill_status = "not_configured"
            return result
        if not isinstance(source, str):
            raise ValueError("Unsupported prefill path")
        path = Path(source).expanduser()
        if not path.is_absolute():
            path = get_hermes_home() / path
        messages = json.loads(read_text(path, MAX_PREFILL))
        if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
            raise ValueError("Unsupported prefill")
        if any(
            not isinstance(m, dict)
            or m.get("role") not in {"system", "user", "assistant"}
            or not isinstance(m.get("content"), str)
            for m in messages
        ):
            raise ValueError("Unsupported prefill message")
        result.prefill = [{"role": m["role"], "content": m["content"]} for m in messages]
        result.prefill_status = "ready"
    except Exception:
        log.warning("Talaria could not inherit this profile's prefill messages")
    return result


class ProfileRunAdapter:
    """A per-admission view of the native adapter; its shared runtime stays untouched.

    Hermes owns admission, leases, history, events, cancellation and persistence.
    This run's agent inherits profile defaults and optional presentation callbacks.
    """

    def __init__(self, adapter, context, use_default_model=False, live_interactions=False):
        self._adapter, self._context = adapter, context
        self._use_default_model = use_default_model
        self._live_interactions = live_interactions

    def __getattr__(self, name):
        return getattr(self._adapter, name)

    def _make_run_event_callback(self, run_id, loop):
        from .live import LiveRun, supported

        original = self._adapter._make_run_event_callback(run_id, loop)
        if not self._live_interactions or not supported(self._adapter):
            return original
        self._live = LiveRun(self._adapter, run_id, loop)
        return self._live.progress(original)

    def _create_agent(self, **kwargs):
        if not self._use_default_model:
            session_runtime(self._adapter, kwargs)
        inherit_options(kwargs)
        if not kwargs.get("ephemeral_system_prompt"):
            kwargs["ephemeral_system_prompt"] = self._context.instructions or None
        agent = self._adapter._create_agent(**kwargs)
        inherit_defaults(agent)
        apply_surface(agent)
        if "_live" in self.__dict__:
            self._live.bind(agent)
        # Prefer native support if a later Hermes version starts loading these itself.
        if hasattr(agent, "prefill_messages") and not agent.prefill_messages:
            agent.prefill_messages = deepcopy(self._context.prefill)
        if callable(getattr(agent, "run_conversation", None)):
            agent.run_conversation = scoped_run(agent.run_conversation)
        return agent


def supports_context_runs(adapter):
    try:
        from gateway.platforms import api_server, api_server_runs

        if not all(
            callable(value)
            for value in (
                getattr(adapter, "_create_agent", None),
                getattr(api_server, "_admit_api_agent_request", None),
                getattr(api_server_runs, "_handle_runs", None),
            )
        ):
            return False
        inspect.signature(adapter._create_agent).bind_partial(ephemeral_system_prompt=None)
        inspect.signature(api_server._admit_api_agent_request).bind(None)
        inspect.signature(api_server_runs._handle_runs).bind(adapter, None, _api_server=api_server)
        return True
    except Exception:
        return False


async def start_run(adapter, request):
    from gateway.platforms import api_server, api_server_runs

    async def dispatch(native, request):
        context = await asyncio.to_thread(load_context)
        data = await request.json()
        use_default = isinstance(data, dict) and data.get("use_default_model") is True
        live = isinstance(data, dict) and data.get("live_interactions") is True
        return await api_server_runs._handle_runs(
            ProfileRunAdapter(native, context, use_default, live), request, _api_server=api_server
        )

    # Admission runs on the real adapter: its pending/drain counters remain shared
    # with standard API runs. The body and idempotency fingerprint are unchanged.
    return await api_server._admit_api_agent_request(dispatch)(adapter, request)

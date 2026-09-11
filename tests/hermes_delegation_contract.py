"""Native delegation workers must preserve task-local profile and approval authority."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from agent.secret_scope import set_multiplex_active
from gateway.run import _profile_runtime_scope
from hermes_constants import get_hermes_home
from tools.approval_context import (
    get_current_session_key,
    reset_current_session_key,
    set_current_session_key,
)
from tools.delegate_tool_child_run import _ChildRun
from tools.delegate_tool_dispatch import _resolve_async_session_key


async def verify_delegation(home):
    def child_turn(**kwargs):
        return {"profile": str(get_hermes_home()), "authority": get_current_session_key()}

    async def run_profile(name):
        directory = home / name
        directory.mkdir()
        authority = f"api-run:{name}"
        with _profile_runtime_scope(directory, prepared_secret_scope={}):
            token = set_current_session_key(authority)
            try:
                parent = SimpleNamespace(session_id=f"parent-{name}")
                assert _resolve_async_session_key(parent, "")[0] == authority
                child = SimpleNamespace(session_id=f"child-{name}", run_conversation=child_turn)
                fixture = SimpleNamespace(
                    child=child,
                    task_index=0,
                    goal="Fixture",
                    child_task_id="fixture",
                    relay_text=None,
                )
                result, error, deferred = await asyncio.to_thread(_ChildRun.await_child, fixture)
                assert result == {"profile": str(directory), "authority": authority}
                assert error is None and not deferred
            finally:
                reset_current_session_key(token)

    set_multiplex_active(True)
    try:
        with patch("tools.delegate_tool._get_child_timeout", return_value=5):
            await asyncio.gather(run_profile("delegate-a"), run_profile("delegate-b"))
    finally:
        set_multiplex_active(False)
    assert str(get_hermes_home()) == str(home)

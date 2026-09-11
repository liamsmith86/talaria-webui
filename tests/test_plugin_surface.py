import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

from talaria.hermes_plugin.surface import HINT, apply_surface


@pytest.mark.parametrize("shape", ["chat", "responses", "instructions", "anthropic", "blocks"])
def test_surface_hint_preserves_provider_payload_and_user_instructions(monkeypatch, shape):
    original = "Native API formatting hint"
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "agent.prompt_builder",
        SimpleNamespace(PLATFORM_HINTS={"api_server": original}),
    )
    system = "PROFILE_PERSONA\n" + original + "\nPROFILE_APPEND"
    blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    user = {"role": "user", "content": original}  # User text must not be rewritten.
    if shape in {"chat", "blocks", "responses"}:
        field = "input" if shape == "responses" else "messages"
        request = {
            field: [{"role": "developer", "content": blocks if shape == "blocks" else system}, user]
        }
    elif shape == "anthropic":
        request = {"system": blocks, "messages": [user]}
    else:
        request = {"instructions": system, "input": [user]}
    snapshot = deepcopy(request)
    overrides = {"api_server": {"append": "PROFILE_APPEND"}}
    agent = SimpleNamespace(_platform_hint_overrides=overrides, _build_api_kwargs=lambda: request)
    apply_surface(agent)
    wire = agent._build_api_kwargs()
    assert request == snapshot and overrides == {"api_server": {"append": "PROFILE_APPEND"}}
    assert user in (wire.get("messages") or wire.get("input"))
    content = wire.get("system") or wire.get("instructions")
    if content is None:
        content = (wire.get("messages") or wire["input"])[0]["content"]
    if isinstance(content, list):
        assert content[0]["cache_control"] == {"type": "ephemeral"}
        content = content[0]["text"]
    assert content == "PROFILE_PERSONA\n" + HINT + "\nPROFILE_APPEND"
    assert agent._platform_hint_overrides["api_server"]["append"] == "PROFILE_APPEND"


def test_explicit_platform_replacement_wins(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "agent.prompt_builder",
        SimpleNamespace(PLATFORM_HINTS={"api_server": "NATIVE"}),
    )
    request = {"messages": [{"role": "system", "content": "OWNER_INSTRUCTION"}]}
    agent = SimpleNamespace(
        _platform_hint_overrides={"api_server": {"replace": "OWNER_INSTRUCTION"}},
        _build_api_kwargs=lambda: request,
    )
    apply_surface(agent)
    assert agent._build_api_kwargs() == request

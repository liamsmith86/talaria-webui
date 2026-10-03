import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

from talaria.hermes_plugin.request_log import run_scope
from talaria.hermes_plugin.surface import HINT, shape_request


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
    assert shape_request(request=request, platform="api_server") is None
    with run_scope():
        assert shape_request(request=request, platform="discord") is None
        wire = shape_request(request=request, platform="api_server")["request"]
    assert request == snapshot
    assert user in (wire.get("messages") or wire.get("input"))
    content = wire.get("system") or wire.get("instructions")
    if content is None:
        content = (wire.get("messages") or wire["input"])[0]["content"]
    if isinstance(content, list):
        assert content[0]["cache_control"] == {"type": "ephemeral"}
        content = content[0]["text"]
    assert content == "PROFILE_PERSONA\n" + HINT + "\nPROFILE_APPEND"


def test_explicit_platform_replacement_wins(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "agent.prompt_builder",
        SimpleNamespace(PLATFORM_HINTS={"api_server": "NATIVE"}),
    )
    request = {"messages": [{"role": "system", "content": "OWNER_INSTRUCTION"}]}
    with run_scope():
        assert shape_request(request=request, platform="api_server") is None

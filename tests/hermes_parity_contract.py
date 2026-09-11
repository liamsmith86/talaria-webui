"""Native provider-wire checks; all state and credentials belong to the simulator."""

import json
from unittest.mock import patch

from agent.prompt_builder import PLATFORM_HINTS

from talaria.hermes_plugin.surface import HINT


async def verify_parity(run, db, seen, agents, configure, model_name, provider_name):
    original = db.get_session("context-test")["system_prompt"].replace(
        HINT, PLATFORM_HINTS["api_server"]
    )
    configure(
        agent={"system_prompt": "PROFILE_INSTRUCTION", "service_tier": "priority"},
        provider_routing={
            "only": ["FixtureProvider"],
            "sort": "latency",
            "data_collection": "deny",
        },
    )
    db.create_session("saved-model", "api_server", model="stored-fixture-model")
    await run("Resume the stored model", "saved-model", session_id="saved-model")
    assert seen[-1]["model"] == "stored-fixture-model", seen[-1]["model"]
    assert agents[-1].service_tier == "priority"
    assert agents[-1].providers_allowed == ["FixtureProvider"]
    if provider_name == "openrouter":
        assert seen[-1]["provider"]["only"] == ["FixtureProvider"]
        assert seen[-1]["provider"]["sort"] == "latency"
        assert seen[-1]["provider"]["data_collection"] == "deny"
    # The loopback endpoint is intentionally not a billable first-party fast route.
    assert "service_tier" not in seen[-1]
    if provider_name == "openai":
        with patch("hermes_cli.models._fast_mode_route_supported", return_value=True):
            await run("Use eligible priority processing", "priority", model="gpt-5.4")
        assert seen[-1]["service_tier"] == "priority"
    await run(
        "Explicit selections still win",
        "explicit-model",
        session_id="saved-model",
        model=model_name,
        model_options={"service_tier": None},
    )
    assert seen[-1]["model"] == model_name
    assert agents[-1].service_tier is None
    assert seen[-1].get("service_tier") != "priority"

    # Hermes owns its confirmed lock, including reasoning and provider resolution.
    db.create_session("locked-model", "api_server", model=model_name)
    db.update_session_runtime_lock(
        "locked-model",
        model="locked-fixture-model",
        provider=provider_name,
        model_options={"reasoning": {"enabled": True, "effort": "high"}},
        route_source="raw_request",
        confirmed=True,
    )
    await run("Resume the native lock", "locked-model", session_id="locked-model")
    assert seen[-1]["model"] == "locked-fixture-model"
    assert agents[-1].reasoning_config["effort"] == "high"
    await run(
        "Return to Hermes's configured default",
        "reset-default",
        session_id="locked-model",
        use_default_model=True,
    )
    assert seen[-1]["model"] == model_name

    # Existing cached API prompts get the same renderer correction as new sessions.
    db.create_session("legacy-hint", "api_server", model=model_name)
    db.update_system_prompt("legacy-hint", original)
    db.append_message("legacy-hint", "user", "Earlier turn")
    db.append_message("legacy-hint", "assistant", "Earlier answer")
    await run("Use this renderer", "legacy-hint", session_id="legacy-hint")
    wire = json.dumps(seen[-1]["messages"])
    assert HINT in wire and PLATFORM_HINTS["api_server"] not in wire
    assert db.get_session("legacy-hint")["system_prompt"] == original

    configure(
        platform_hints={
            "api_server": {"replace": "CUSTOM_PLATFORM_HINT", "append": "CUSTOM_APPEND"}
        }
    )
    db.create_session("custom-hint", "api_server")
    await run("Use the owner's hint", "custom-hint", session_id="custom-hint")
    wire = json.dumps(seen[-1]["messages"])
    assert "CUSTOM_PLATFORM_HINT" in wire and "CUSTOM_APPEND" in wire and HINT not in wire
    await run("An older client still works", "older-client", live_interactions=False)
    assert agents[-1].reasoning_callback is None and agents[-1].clarify_callback is None
    configure(debug_requests=False)

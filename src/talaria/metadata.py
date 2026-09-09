"""Small, read-only projections of Hermes's public discovery endpoints."""

import asyncio

from starlette.responses import JSONResponse

from .hermes import APIError


def text(value, limit=160):
    return value[:limit].strip() if isinstance(value, str) else ""


def rows(value):
    data = value.get("data") if isinstance(value, dict) else value
    if not isinstance(data, list):
        return None
    return [row for row in data[:2000] if isinstance(row, dict) and text(row.get("name"))]


def agent_identity(caps, extended):
    caps = caps if isinstance(caps, dict) else {}
    agent = caps.get("agent")
    name = text(agent.get("name"), 80) if isinstance(agent, dict) else ""
    name = name or text(caps.get("agent_name"), 80)
    extension_name = text((extended.get("agent") or {}).get("name"), 80)
    return {
        "name": name or extension_name or "Hermes",
        "name_source": "hermes" if name else "extension" if extension_name else "fallback",
    }


def readiness_info(health):
    health = health if isinstance(health, dict) else {}
    value = health.get("readiness")
    value = value if isinstance(value, dict) else {}
    status = text(value.get("status") or health.get("status"), 32)
    checks = value.get("checks")
    checks = checks if isinstance(checks, dict) else {}
    labels = {
        "state_db": "Conversation storage",
        "session_store": "Conversation access",
        "config": "Configuration",
        "model": "Default model",
        "disk": "Storage space",
        "gateway": "Gateway",
        "background_queues": "Background work",
    }
    issues = []
    for name, label in labels.items():
        check = checks.get(name)
        if not isinstance(check, dict) or text(check.get("status"), 32) in {"", "ok", "ready"}:
            continue
        detail = text(check.get("detail"), 240)
        if (
            name == "disk"
            and type(check.get("used_percent")) in {int, float}
            and 0 <= check["used_percent"] <= 100
        ):
            detail = f"{check['used_percent']:g}% of storage is used."
        elif name == "model" and not detail:
            detail = "No default model is configured in Hermes."
        elif name == "gateway" and not detail:
            detail = text(check.get("state"), 64).replace("_", " ")
        issues.append(
            {
                "name": name,
                "label": label,
                "status": text(check.get("status"), 32),
                "detail": detail,
            }
        )
    return {
        "status": status if status in {"ok", "ready", "degraded", "unavailable"} else "unknown",
        "issues": issues,
    }


async def readiness(request):
    try:
        health = await request.app.state.hermes.request("GET", "/health/detailed", timeout=6)
        value = readiness_info(health)
    except APIError as exc:
        value = {
            "status": "unknown" if exc.status == 404 else "unavailable",
            "issues": [],
            "message": exc.message,
        }
    return JSONResponse(value)


async def details(request):
    state = request.app.state
    caps = state.capabilities if isinstance(state.capabilities, dict) else {}
    advertised = caps.get("endpoints") or {}
    advertised = advertised if isinstance(advertised, (dict, list)) else {}
    features = caps.get("features")
    features = features if isinstance(features, dict) else {}

    async def read(path, enabled=True):
        if not enabled:
            return None
        try:
            return await state.hermes.request("GET", path, timeout=6)
        except APIError:
            return None

    from .extensions import discover

    health, skills, toolsets, extended = await asyncio.gather(
        read("/health/detailed"),
        read("/v1/skills", "skills" in advertised or features.get("skills_api") is True),
        read("/v1/toolsets", "toolsets" in advertised or features.get("skills_api") is True),
        discover(state.hermes),
    )
    state.extensions = extended
    health = health if isinstance(health, dict) and text(health.get("status")) else None
    skills, toolsets = rows(skills), rows(toolsets)
    platforms = (health or {}).get("platforms", {})
    return JSONResponse(
        {
            **agent_identity(caps, extended),
            "extended_access": extended,
            "version": text((health or {}).get("version"), 40),
            "status": text((health or {}).get("status"), 32),
            "readiness": readiness_info(health),
            "gateway_state": text((health or {}).get("gateway_state"), 32),
            "platforms": [
                {"name": text(name), "state": text(value.get("state"), 32)}
                for name, value in list(platforms.items())[:50]
                if isinstance(value, dict)
            ]
            if isinstance(platforms, dict)
            else [],
            "skills": [
                {
                    "name": text(row.get("name")),
                    "description": text(row.get("description"), 500),
                    "category": text(row.get("category")),
                }
                for row in skills or []
            ],
            "toolsets": [
                {
                    "name": text(row.get("name")),
                    "label": text(row.get("label")),
                    "description": text(row.get("description"), 500),
                    "enabled": row.get("enabled") is True,
                    "configured": row.get("configured") is True,
                    "tools": [
                        text(item) for item in row.get("tools", [])[:200] if isinstance(item, str)
                    ]
                    if isinstance(row.get("tools"), list)
                    else [],
                }
                for row in toolsets or []
            ],
            "available": {
                "health": health is not None,
                "skills": skills is not None,
                "toolsets": toolsets is not None,
            },
        }
    )

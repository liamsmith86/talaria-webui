"""Small, read-only projections of Hermes's public discovery endpoints."""

import asyncio

from starlette.responses import JSONResponse

from .hermes import APIError
from .hermes_access.files import inspect_home


def text(value, limit=160):
    return value[:limit].strip() if isinstance(value, str) else ""


def rows(value):
    data = value.get("data") if isinstance(value, dict) else value
    if not isinstance(data, list):
        return None
    return [row for row in data[:2000] if isinstance(row, dict) and text(row.get("name"))]


def agent_identity(caps, local):
    caps = caps if isinstance(caps, dict) else {}
    agent = caps.get("agent")
    name = text(agent.get("name"), 80) if isinstance(agent, dict) else ""
    name = name or text(caps.get("agent_name"), 80)
    return {
        "name": name or local.name or "Hermes",
        "name_source": "hermes" if name else "files" if local.name else "fallback",
    }


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

    health, skills, toolsets, local = await asyncio.gather(
        read("/health/detailed"),
        read("/v1/skills", "skills" in advertised or features.get("skills_api") is True),
        read("/v1/toolsets", "toolsets" in advertised or features.get("skills_api") is True),
        asyncio.to_thread(inspect_home, state.settings.hermes_home),
    )
    health = health if isinstance(health, dict) and text(health.get("status")) else None
    skills, toolsets = rows(skills), rows(toolsets)
    platforms = (health or {}).get("platforms", {})
    return JSONResponse(
        {
            **agent_identity(caps, local),
            "extended_access": local.public(),
            "version": text((health or {}).get("version"), 40),
            "status": text((health or {}).get("status"), 32),
            "gateway_state": text((health or {}).get("gateway_state"), 32),
            "active_agents": (health or {}).get("active_agents")
            if type((health or {}).get("active_agents")) is int
            else None,
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

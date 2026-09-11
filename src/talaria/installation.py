"""Public release and launcher progress, with private deployment settings omitted."""

import asyncio
import json
import re
from functools import lru_cache
from pathlib import Path

from starlette.responses import JSONResponse

from . import __version__
from .control import JOB, SOCKET


def read_json(path: Path) -> dict:
    try:
        with path.open() as file:
            data = json.loads(file.read(16_385))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=1)
def build_info() -> dict:
    build = read_json(Path(__file__).with_name("_build.json"))
    commit = build.get("commit", "")
    return {
        "version": __version__,
        "commit": commit
        if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40,64}", commit)
        else None,
    }


def managed_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if parent.name == "releases" and (parent.parent / "deployment.json").is_file():
            return parent.parent
    return None


def release_info(path: Path) -> dict:
    data = read_json(path / "release.json")
    return {key: str(data.get(key, ""))[:80] for key in ("commit", "version", "installed_at")}


def public_info(development: bool) -> dict:
    root = None if development else managed_root()
    data = {
        **build_info(),
        "environment": "development" if development else "production",
        "managed": bool(root),
    }
    if root:
        config = read_json(root / "deployment.json")
        state = read_json(root / "update.json")
        data.update(
            branch=str(config.get("branch", "main"))[:120],
            installed_at=release_info(root / "current")["installed_at"],
            can_update=(root / SOCKET).exists(),
            updates_local_plugin=isinstance(config.get("hermes_plugin"), dict),
            operation={
                key: value
                for key, value in read_json(root / JOB).items()
                if key in {"id", "action", "status", "phase", "error", "expect"}
                and isinstance(value, (str, type(None)))
            },
            update={
                **{
                    key: state[key][:300] if isinstance(state.get(key), str) else None
                    for key in ("checked_at", "latest_commit", "error")
                },
                "available": state.get("available")
                if isinstance(state.get("available"), bool)
                else None,
            },
        )
    return data


async def details(request):
    return JSONResponse(await asyncio.to_thread(public_info, request.app.state.development))

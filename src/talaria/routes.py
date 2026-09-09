"""Validated browser routes over the public Hermes API."""

import asyncio
import json
import secrets
from dataclasses import replace

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from . import __version__, auth
from .content import MAX_CHAT_BODY, REASONING, image_inputs
from .hermes import APIError, Hermes, identifier
from .hermes_access.files import inspect_home, validate_home
from .metadata import agent_identity
from .profiles import connection_url
from .relay import Relay
from .transcripts import message_page


async def body(request: Request, limit=1024 * 1024) -> dict:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise APIError(
                "This request is too large. Reduce the text or attachments.", 413, "too_large"
            )
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeDecodeError) as exc:
        raise APIError("Invalid request.", 400, "invalid_request") from exc


def text_field(data: dict, key: str, limit: int, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise APIError(f"Invalid {key.replace('_', ' ')}.", 400, "invalid_request")
    return value


async def bootstrap(request: Request):
    logged_in = auth.authenticated(request)
    settings = request.app.state.settings
    local = await asyncio.to_thread(inspect_home, settings.hermes_home) if logged_in else None
    return JSONResponse(
        {
            "version": __version__,
            "environment": "development" if request.app.state.development else "production",
            "authenticated": logged_in,
            "csrf": auth.csrf_token(request) if logged_in else None,
            "connected": bool(settings.api_key) if logged_in else False,
            "agent": agent_identity(request.app.state.capabilities, local) if logged_in else None,
            "profile": request.app.state.profiles.describe(request.app.state.profile_id)
            if logged_in
            else None,
            "profiles": request.app.state.profiles.public()["profiles"] if logged_in else [],
        }
    )


async def login(request: Request):
    data = await body(request)
    state = request.app.state
    address = request.client.host if request.client else "local"
    if not state.limiter.allow(address):
        raise APIError("Too many attempts. Please try again in five minutes.", 429)
    password = text_field(data, "password", 1024)
    if not await asyncio.to_thread(auth.verify_password, password, state.settings.password_hash):
        raise APIError("That password didn’t match. Please try again.", 401, "login_failed")
    state.limiter.success(address)
    cookie = auth.issue_cookie(state.settings)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        state.cookie_name,
        cookie,
        max_age=auth.TTL,
        httponly=True,
        secure=state.settings.secure,
        samesite="strict",
        path="/",
    )
    return response


async def logout(request: Request):
    response = JSONResponse({"ok": True})
    response.delete_cookie(request.app.state.cookie_name, path="/")
    return response


async def connection(request: Request):
    state = request.app.state
    if request.method == "GET":
        return JSONResponse(
            {
                "url": state.settings.hermes_url,
                "key_set": bool(state.settings.api_key),
                **state.profiles.describe(state.profile_id),
            }
        )
    data = await body(request)
    try:
        text_field(data, "url", 2048)
        url = connection_url(data)
    except (ValueError, TypeError, AttributeError) as exc:
        raise APIError(str(exc), 400, "invalid_url") from exc
    key = text_field(data, "api_key", 4096)
    if not key and url == state.settings.hermes_url and request.url.path != "/api/profiles/test":
        key = state.settings.api_key
    if not key or any(ord(c) < 32 for c in key):
        raise APIError("Enter the API key from your Hermes API server.", 400)
    if request.method == "PUT" and state.settings.api_key and url != state.settings.hermes_url:
        raise APIError("Add a profile to connect to another Hermes address or profile.", 409)
    client = Hermes(url, key, transport=state.profiles.transport)
    try:
        caps = await client.request("GET", "/v1/capabilities")
        if caps.get("platform") != "hermes-agent" and not caps.get("features"):
            raise APIError("This address did not return Hermes capabilities.", 400)
        if request.method == "PUT":
            async with state.connection_lock:
                if any(not c.finished for c in state.relay.channels.values()):
                    raise APIError(
                        "Wait for active conversations to finish before changing Hermes.", 409
                    )
                settings = replace(state.settings, hermes_url=url, api_key=key)
                await state.profiles.save_settings(state.profile_id, settings)
                await state.relay.close()
                await state.hermes.close()
                state.settings, state.hermes = settings, client
                state.relay = Relay(client)
                state.capabilities = caps
                client = None
        return JSONResponse({"ok": True, "capabilities": caps})
    finally:
        if client:
            await client.close()


async def capabilities(request: Request):
    state = request.app.state
    caps = await state.hermes.request("GET", "/v1/capabilities")
    state.capabilities = caps
    local = await asyncio.to_thread(inspect_home, state.settings.hermes_home)
    return JSONResponse({**caps, "talaria_agent": agent_identity(caps, local)})


async def hermes_access(request: Request):
    state = request.app.state
    directory = state.settings.hermes_home
    if request.method != "GET":
        data = await body(request)
        try:
            directory = validate_home(text_field(data, "path", 4096))
        except ValueError as exc:
            raise APIError(str(exc), 400, "invalid_path") from exc
    local = await asyncio.to_thread(inspect_home, directory)
    if request.method == "PUT":
        async with state.connection_lock:
            settings = replace(state.settings, hermes_home=directory)
            await state.profiles.save_settings(state.profile_id, settings)
            state.settings = settings
    return JSONResponse(
        {"path": directory, **local.public(), "agent": agent_identity(state.capabilities, local)}
    )


async def model_options(request: Request):
    params = {"refresh": "1"} if request.query_params.get("refresh") == "1" else {}
    return JSONResponse(
        await request.app.state.hermes.request("GET", "/api/model/options", params=params)
    )


async def sessions(request: Request):
    client = request.app.state.hermes
    if request.method == "GET":
        try:
            offset = min(max(int(request.query_params.get("offset", "0")), 0), 1_000_000)
        except ValueError as exc:
            raise APIError("Invalid page.", 400) from exc
        return JSONResponse(
            await client.request(
                "GET",
                "/api/sessions",
                params={"limit": 100, "offset": offset, "include_children": "false"},
            )
        )
    data = await body(request)
    payload = {"title": text_field(data, "title", 160, "New conversation"), "source": "api_server"}
    for attempt in range(6):
        try:
            result = await client.request("POST", "/api/sessions", json=payload)
            return JSONResponse(result.get("session", result), status_code=201)
        except APIError as exc:
            if exc.code != "invalid_title":
                raise
            base = text_field(data, "title", 160, "New conversation")[:140]
            payload["title"] = f"{base} ({attempt + 2})"
    payload.pop("title", None)
    result = await client.request("POST", "/api/sessions", json=payload)
    return JSONResponse(result.get("session", result), status_code=201)


async def session(request: Request):
    sid = identifier(request.path_params["session_id"])
    payload = {}
    if request.method == "PATCH":
        data = await body(request)
        if not data or set(data) - {"title", "pinned"}:
            raise APIError("Choose a name or pin this conversation.", 400)
        fields = {}
        if "title" in data:
            title = text_field(data, "title", 160).strip()
            if not title:
                raise APIError("Give this conversation a name.", 400)
            fields["title"] = title
        if "pinned" in data:
            if type(data["pinned"]) is not bool:
                raise APIError("Invalid pin state.", 400)
            fields["pinned"] = data["pinned"]
        payload["json"] = fields
    return JSONResponse(
        await request.app.state.hermes.request(request.method, f"/api/sessions/{sid}", **payload)
    )


async def messages(request: Request):
    sid = identifier(request.path_params["session_id"])
    try:
        offset = max(0, min(int(request.query_params.get("offset", "0")), 1_000_000))
    except ValueError as exc:
        raise APIError("Invalid message page.", 400) from exc
    return JSONResponse(await message_page(request.app.state.hermes, sid, offset))


async def fork(request: Request):
    sid = identifier(request.path_params["session_id"])
    data = await body(request)
    payload = {"title": text_field(data, "title", 160, "Branched conversation")}
    result = await request.app.state.hermes.request(
        "POST", f"/api/sessions/{sid}/fork", json=payload
    )
    return JSONResponse(result.get("session", result), status_code=201)


async def start_run(request: Request):
    data = await body(request, MAX_CHAT_BODY)
    sid = identifier(text_field(data, "session_id", 256))
    prompt = text_field(data, "input", 800_000).strip()
    images = image_inputs(data.get("images", []))
    if not prompt and not images:
        raise APIError("Write a message first.", 400)
    payload = {"session_id": sid, "input": prompt}
    if images:
        content = ([{"type": "text", "text": prompt}] if prompt else []) + images
        payload["input"] = [{"role": "user", "content": content}]
    reasoning = data.get("reasoning", "auto")
    if reasoning != "auto":
        if not isinstance(reasoning, str) or reasoning not in REASONING:
            raise APIError("Choose a supported reasoning level.", 400, "invalid_reasoning")
        payload["model_options"] = {"reasoning": {"enabled": reasoning != "none"}}
        if reasoning != "none":
            payload["model_options"]["reasoning"]["effort"] = reasoning
    for key in ("model", "provider"):
        value = text_field(data, key, 256)
        if value:
            payload[key] = value
    idem = text_field(data, "request_id", 128) or secrets.token_hex(16)
    identifier(idem)
    state = request.app.state
    async with state.connection_lock, state.profiles.run_lock:
        if (
            sum(
                not c.finished
                for app in state.profiles.apps.values()
                for c in app.state.relay.channels.values()
            )
            >= 16
        ):
            raise APIError("There are too many active conversations. Wait for one to finish.", 429)
        result = await state.hermes.request(
            "POST",
            "/v1/runs",
            json=payload,
            headers={"Idempotency-Key": idem, "X-Hermes-Session-Key": f"talaria:{sid}"},
        )
        run_id = identifier(result.get("run_id", ""))
        state.profiles.attach(state, run_id)
    return JSONResponse(result, status_code=202)


async def run(request: Request):
    rid = identifier(request.path_params["run_id"])
    return JSONResponse(await request.app.state.hermes.request("GET", f"/v1/runs/{rid}"))


async def control(request: Request):
    rid, action = identifier(request.path_params["run_id"]), request.path_params["action"]
    data = await body(request)
    if action == "stop":
        payload = {}
    elif action == "steer":
        payload = {"input": text_field(data, "input", 50_000).strip()}
        if not payload["input"]:
            raise APIError("Write your guidance first.", 400)
    elif action == "approval":
        choice = data.get("choice")
        if choice not in {"once", "session", "always", "deny"}:
            raise APIError("Choose one of the available approval options.", 400)
        payload = {"choice": choice}
        if data.get("request_id"):
            payload["request_id"] = text_field(data, "request_id", 256)
    else:
        raise APIError("Unknown action.", 404)
    return JSONResponse(
        await request.app.state.hermes.request("POST", f"/v1/runs/{rid}/{action}", json=payload)
    )


async def events(request: Request):
    rid = identifier(request.path_params["run_id"])
    try:
        cursor = max(0, int(request.headers.get("last-event-id", "0")))
    except ValueError as exc:
        raise APIError("Invalid event cursor.", 400) from exc
    state = request.app.state
    channel = state.profiles.attach(state, rid)
    return StreamingResponse(
        request.app.state.relay.stream(channel, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )

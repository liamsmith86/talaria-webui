"""Hermes public HTTP contract. No agent internals are imported."""

import json
import math
import re

import httpx

MAX_RESPONSE = 16 * 1024 * 1024
MAX_EVENT = 2 * 1024 * 1024
MAX_STREAMS = 32
REQUEST_CONNECTIONS = 8
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")


def decode_json(raw):
    def check(value, depth=0):
        if depth > 64:
            raise ValueError("JSON nesting limit exceeded")
        if isinstance(value, str):
            value.encode("utf-8")
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Non-finite JSON number")
        elif isinstance(value, dict):
            for key, item in value.items():
                key.encode("utf-8")
                check(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                check(item, depth + 1)

    value = json.loads(raw)
    check(value)
    return value


def object_result(value):
    if not isinstance(value, dict):
        raise APIError("Hermes did not return a readable response.")
    return value


def valid_api_key(value):
    return isinstance(value, str) and bool(value) and all(33 <= ord(c) <= 126 for c in value)


async def event_lines(response):
    pending = bytearray()
    size = 0
    searched = 0
    async for chunk in response.aiter_bytes():
        pending.extend(chunk)
        while (end := pending.find(b"\n", searched)) >= 0:
            raw = pending[:end].rstrip(b"\r")
            del pending[: end + 1]
            searched = 0
            size += len(raw)
            if size > MAX_EVENT:
                raise APIError("A Hermes event exceeded the display limit.")
            if not raw:
                size = 0
            yield raw.decode("utf-8")
        # A fragmented JSON line can be megabytes long. The bytes already
        # checked contain no newline; only scan the newly received suffix.
        searched = len(pending)
        if size + len(pending) > MAX_EVENT:
            raise APIError("A Hermes event exceeded the display limit.")


class APIError(Exception):
    def __init__(self, message: str, status: int = 502, code: str = "upstream_error"):
        self.message, self.status, self.code = message, status, code
        super().__init__(message)


def identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value) or value in {".", ".."}:
        raise APIError("Invalid session or run identifier.", 400, "invalid_id")
    return value


def response_error(status: int, body: dict | None = None) -> APIError:
    messages = {
        400: "Hermes could not accept that request. Check the selected model and try again.",
        401: "The Hermes API key was not accepted. Check Connection in Settings.",
        403: "Hermes did not allow this action.",
        404: "This item is no longer available in Hermes.",
        409: "This action conflicts with the current session state. Refresh and try again.",
        429: "Hermes is busy. Please wait a moment and try again.",
    }
    code = (body or {}).get("error", {})
    code = code.get("code") if isinstance(code, dict) else None
    if code == "invalid_title":
        return APIError("That session name is already in use or is not valid.", status, code)
    return APIError(
        messages.get(status, "Hermes is temporarily unavailable. Try again shortly."),
        status if status in messages else 502,
    )


class Hermes:
    def __init__(self, url: str, key: str, *, transport=None):
        self.client = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {key}"} if key else {},
            timeout=httpx.Timeout(30, connect=5),
            # A full stream budget must leave room for status, stop, and history.
            limits=httpx.Limits(
                max_connections=MAX_STREAMS + REQUEST_CONNECTIONS, max_keepalive_connections=12
            ),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def close(self):
        await self.client.aclose()

    async def request(self, method: str, path: str, **kwargs):
        try:
            async with self.client.stream(method, path.lstrip("/"), **kwargs) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    limit = MAX_RESPONSE if response.is_success else 65536
                    if len(data) > limit:
                        if not response.is_success:
                            raise response_error(response.status_code)
                        raise APIError(
                            "This Hermes response is too large to display.",
                            code="response_too_large",
                        )
                if not response.is_success:
                    try:
                        detail = decode_json(data)
                    except (ValueError, RecursionError):
                        detail = {}
                    raise response_error(
                        response.status_code, detail if isinstance(detail, dict) else {}
                    )
                if not data:
                    return {}
                try:
                    return decode_json(data)
                except (ValueError, RecursionError) as exc:
                    raise APIError(
                        "Hermes returned an unreadable response. Try again shortly.",
                        code="invalid_response",
                    ) from exc
        except (httpx.HTTPError, ValueError, RecursionError) as exc:
            raise APIError("Cannot reach Hermes. Check the connection and try again.") from exc

    async def events(self, run_id: str):
        try:
            async with self.client.stream(
                "GET",
                f"v1/runs/{identifier(run_id)}/events",
                timeout=httpx.Timeout(65, connect=5),
            ) as response:
                if response.status_code != 200:
                    raise response_error(response.status_code)
                lines = []
                async for line in event_lines(response):
                    if line.startswith("data:"):
                        lines.append(line[5:].lstrip())
                    elif not line and lines:
                        payload = "\n".join(lines)
                        lines = []
                        if payload == "[DONE]":
                            return
                        event = decode_json(payload)
                        if isinstance(event, dict):
                            yield event
        except (httpx.HTTPError, ValueError, RecursionError) as exc:
            raise APIError("Live updates paused. Checking the run in Hermes.") from exc

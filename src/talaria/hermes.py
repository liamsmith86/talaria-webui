"""Hermes public HTTP contract. No agent internals are imported."""

import json
import re

import httpx

MAX_RESPONSE = 16 * 1024 * 1024
MAX_EVENT = 2 * 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")


async def event_lines(response):
    pending = bytearray()
    size = 0
    async for chunk in response.aiter_bytes():
        pending.extend(chunk)
        while (end := pending.find(b"\n")) >= 0:
            raw = pending[:end].rstrip(b"\r")
            del pending[: end + 1]
            size += len(raw)
            if size > MAX_EVENT:
                raise APIError("A Hermes event exceeded the display limit.")
            if not raw:
                size = 0
            yield raw.decode("utf-8")
        if size + len(pending) > MAX_EVENT:
            raise APIError("A Hermes event exceeded the display limit.")


class APIError(Exception):
    def __init__(self, message: str, status: int = 502, code: str = "upstream_error"):
        self.message, self.status, self.code = message, status, code
        super().__init__(message)


def identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(value) or value in {".", ".."}:
        raise APIError("Invalid conversation or run identifier.", 400, "invalid_id")
    return value


def response_error(status: int, body: dict | None = None) -> APIError:
    messages = {
        400: "Hermes could not accept that request. Check the selected model and try again.",
        401: "The Hermes API key was not accepted. Check Connection in Settings.",
        403: "Hermes did not allow this action.",
        404: "This item is no longer available in Hermes.",
        409: "This action conflicts with the current conversation state. Refresh and try again.",
        429: "Hermes is busy. Please wait a moment and try again.",
    }
    code = (body or {}).get("error", {})
    code = code.get("code") if isinstance(code, dict) else None
    if code == "invalid_title":
        return APIError("That conversation name is already in use or is not valid.", status, code)
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
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=12),
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
                        detail = json.loads(data)
                    except ValueError:
                        detail = {}
                    raise response_error(
                        response.status_code, detail if isinstance(detail, dict) else {}
                    )
                if not data:
                    return {}
                return json.loads(data)
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
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
                        event = json.loads(payload)
                        if isinstance(event, dict):
                            yield event
        except (httpx.HTTPError, ValueError) as exc:
            raise APIError("Live updates paused. Checking the run in Hermes.") from exc

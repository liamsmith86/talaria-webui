"""Hermes public HTTP contract. No agent internals are imported."""

import json
import re

import httpx

MAX_RESPONSE = 16 * 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")


class APIError(Exception):
    def __init__(self, message: str, status: int = 502, code: str = "upstream_error"):
        self.message, self.status, self.code = message, status, code
        super().__init__(message)


def identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(value) or value in {".", ".."}:
        raise APIError("Invalid conversation or run identifier.", 400, "invalid_id")
    return value


def response_error(status: int) -> APIError:
    messages = {
        400: "Hermes could not accept that request. Check the selected model and try again.",
        401: "The Hermes API key was not accepted. Check Connection in Settings.",
        403: "Hermes did not allow this action.",
        404: "This item is no longer available in Hermes.",
        409: "This action conflicts with the current conversation state. Refresh and try again.",
        429: "Hermes is busy. Please wait a moment and try again.",
    }
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
                if not 200 <= response.status_code < 300:
                    raise response_error(response.status_code)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE:
                        raise APIError("This Hermes response is too large to display.")
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
                lines, size = [], 0
                async for line in response.aiter_lines():
                    size += len(line)
                    if size > 2 * 1024 * 1024:
                        raise APIError("A Hermes event exceeded the display limit.")
                    if line.startswith("data:"):
                        lines.append(line[5:].lstrip())
                    elif not line and lines:
                        payload = "\n".join(lines)
                        lines, size = [], 0
                        if payload == "[DONE]":
                            return
                        event = json.loads(payload)
                        if isinstance(event, dict):
                            yield event
                    elif not line:
                        size = 0
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise APIError("Live updates paused. Checking the run in Hermes.") from exc

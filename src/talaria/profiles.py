"""Saved connections to native Hermes profiles; each reuses the same Talaria app.

Hermes enforces session/run ownership. Talaria only selects a URL and credential.
Profile selection belongs to each browser tab, never to shared server state.
"""

import asyncio
import re
import secrets
from contextlib import contextmanager
from dataclasses import replace
from urllib.parse import urlsplit, urlunsplit

from starlette.requests import Request
from starlette.responses import JSONResponse

from . import auth, config
from .hermes import APIError, Hermes, decode_json, object_result, valid_api_key

PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
MAX_PROFILES = 8
MAX_ACTIVE_RUNS = 16


def split_url(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    base, separator, name = parsed.path.rstrip("/").rpartition("/p/")
    if separator and PROFILE.fullmatch(name):
        return urlunsplit((parsed.scheme, parsed.netloc, base, "", "")), name
    return url, "default"


def connection_url(data: dict) -> str:
    url = config.validate_url(data.get("url", ""))
    if "profile" not in data:
        return url
    profile = data["profile"]
    if not isinstance(profile, str) or not PROFILE.fullmatch(profile):
        raise ValueError("Use a Hermes profile name, such as default or research.")
    base, _ = split_url(url)
    return base if profile == "default" else f"{base}/p/{profile}"


class Profiles:
    def __init__(self, app, transport=None):
        self.app, self.transport = app, transport
        self.path = app.state.config_path.with_name("profiles.json")
        self.apps = {"default": app}
        self.records = {}
        self.inflight = {}
        self.removing = set()
        self.changing = set()
        self.writes = set()
        self.default_label = ""
        self.error = ""
        self.lock = asyncio.Lock()
        self.pending_runs = 0
        if self.path.exists():
            try:
                with self.path.open() as file:
                    data = decode_json(file.read(131_073))
                if data.get("version") != 1 or len(data.get("profiles", [])) >= MAX_PROFILES:
                    raise ValueError
                self.default_label = str(data.get("default_label", ""))[:80]
                for record in data.get("profiles", []):
                    if (
                        not re.fullmatch(r"[0-9a-f]{32}", record["id"])
                        or record["id"] in self.records
                    ):
                        raise ValueError
                    if not all(
                        isinstance(record[k], str) and len(record[k]) <= limit
                        for k, limit in (("label", 80), ("api_key", 4096))
                    ):
                        raise ValueError
                    if not valid_api_key(record["api_key"]):
                        raise ValueError
                    record["url"] = config.validate_url(record["url"])
                    record.pop("hermes_home", None)
                    self.records[record["id"]] = record
            except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
                self.records = {}
                self.error = (
                    "Saved profiles could not be loaded. The initial connection is still available."
                )

    def describe(self, profile_id):
        if profile_id == "default":
            url, label = self.app.state.settings.hermes_url, self.default_label
        else:
            record = self.records[profile_id]
            url, label = record["url"], record["label"]
        base, name = split_url(url)
        return {
            "id": profile_id,
            "label": label or ("Default profile" if name == "default" else name),
            "profile": name,
            "server_url": base,
        }

    def public(self):
        return {
            "profiles": [self.describe(key) for key in ("default", *self.records)],
            "error": self.error,
        }

    def resolve(self, profile_id):
        if profile_id in self.changing:
            raise APIError("This connection is being updated. Please try again shortly.", 409)
        if profile_id in self.removing:
            raise APIError("This profile is being removed. Choose another profile.", 409)
        if profile_id not in self.apps:
            record = self.records.get(profile_id)
            if record is None:
                raise APIError(
                    "This profile is no longer configured. Choose another profile.",
                    404,
                    "profile_missing",
                )
            from .app import create_app

            settings = replace(
                self.app.state.settings,
                hermes_url=record["url"],
                api_key=record["api_key"],
            )
            self.apps[profile_id] = create_app(
                settings,
                self.app.state.config_path,
                transport=self.transport,
                development=self.app.state.development,
                profiles=self,
                profile_id=profile_id,
            )
        return self.apps[profile_id]

    async def persist(self, records):
        if self.error:
            raise APIError("Repair profiles.json before changing saved profiles.", 409)
        await asyncio.to_thread(
            config.save_data,
            self.path,
            {
                "version": 1,
                "default_label": self.default_label,
                "profiles": list(records.values()),
            },
        )
        self.records = records

    async def save_settings(self, profile_id, settings):
        if profile_id == "default":
            await asyncio.to_thread(config.save, self.app.state.config_path, settings)
        else:
            async with self.lock:
                record = self.records[profile_id]
                await self.persist(
                    {
                        **self.records,
                        profile_id: {
                            **record,
                            "url": settings.hermes_url,
                            "api_key": settings.api_key,
                        },
                    }
                )

    async def finish_mutation(self, operation):
        # A disconnected request must not split an atomic file save from its
        # in-memory update. Keep the write tracked until shutdown can await it.
        task = asyncio.create_task(operation)
        self.writes.add(task)
        cancelled = False
        try:
            while True:
                try:
                    result = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    if task.cancelled():
                        raise
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
            return result
        finally:
            self.writes.discard(task)

    async def close(self):
        await asyncio.gather(*self.writes, return_exceptions=True)
        for app in self.apps.values():
            await app.state.relay.close()
            await app.state.hermes.close()

    @contextmanager
    def reserve_run(self):
        # These synchronous check/update operations are atomic on the server's
        # event loop. Pending upstream admissions consume capacity too, without
        # making unrelated requests wait for another profile's network I/O.
        active = sum(
            not channel.finished
            for app in self.apps.values()
            for channel in app.state.relay.channels.values()
        )
        if active + self.pending_runs >= MAX_ACTIVE_RUNS:
            raise APIError("There are too many active conversations. Wait for one to finish.", 429)
        self.pending_runs += 1
        try:
            yield
        finally:
            self.pending_runs -= 1

    def attach(self, state, run_id):
        # Switching profiles must not multiply the original replay memory budget.
        if run_id not in state.relay.channels:
            channels = [
                (app.state.relay, channel)
                for app in self.apps.values()
                for channel in app.state.relay.channels.values()
            ]
            for relay, channel in sorted(channels, key=lambda item: item[1].touched):
                if len(channels) < 32:
                    break
                if channel.finished:
                    relay.channels.pop(channel.run_id)
                    channels.remove((relay, channel))
            if len(channels) >= 32:
                raise APIError("Too many live conversations. Wait for one to finish.", 429)
        return state.relay.attach(run_id)


class ProfileRouter:
    def __init__(self, app, profiles):
        self.app, self.profiles = app, profiles

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/"):
            request = Request(scope)
            path = scope["path"]
            if (
                auth.authenticated(request)
                and path not in {"/api/login", "/api/logout", "/api/profiles"}
                and not path.startswith("/api/profiles/")
            ):
                profile_id = request.query_params.get("talaria_profile", "default")
                try:
                    app = self.profiles.resolve(profile_id)
                except APIError as exc:
                    return await JSONResponse(
                        {"error": exc.message, "code": exc.code}, status_code=exc.status
                    )(scope, receive, send)
                self.profiles.inflight[profile_id] = self.profiles.inflight.get(profile_id, 0) + 1
                try:
                    target = self.app if profile_id == "default" else app
                    return await target(scope, receive, send)
                finally:
                    self.profiles.inflight[profile_id] -= 1
        return await self.app(scope, receive, send)


async def listing(request):
    profiles = request.app.state.profiles
    if request.method == "GET":
        return JSONResponse({**profiles.public(), "csrf": auth.csrf_token(request)})
    from .routes import body, text_field

    data = await body(request)
    label = text_field(data, "label", 80).strip()
    if not label:
        raise APIError("Give this profile a display name.", 400)
    try:
        text_field(data, "url", 2048)
        url = connection_url(data)
    except (ValueError, TypeError, AttributeError) as exc:
        raise APIError("Enter a valid Hermes address and profile name.", 400) from exc
    key = text_field(data, "api_key", 4096)
    if not valid_api_key(key):
        raise APIError("Enter this profile’s Hermes API key.", 400)
    async with profiles.lock:
        if len(profiles.records) >= MAX_PROFILES - 1:
            raise APIError(
                "You can save up to eight profiles. Remove an unused profile first.", 400
            )
        existing = [
            profiles.app.state.settings.hermes_url,
            *(r["url"] for r in profiles.records.values()),
        ]
        if url in existing:
            raise APIError(
                "This Hermes profile is already saved. Select it from the profile menu.", 409
            )
        client = Hermes(url, key, transport=profiles.transport)
        try:
            caps = object_result(await client.request("GET", "/v1/capabilities"))
            if caps.get("platform") != "hermes-agent" and not caps.get("features"):
                raise APIError("This address did not return Hermes capabilities.", 400)
        finally:
            await client.close()
        profile_id = secrets.token_hex(16)
        await profiles.finish_mutation(
            profiles.persist(
                {
                    **profiles.records,
                    profile_id: {
                        "id": profile_id,
                        "label": label,
                        "url": url,
                        "api_key": key,
                    },
                }
            )
        )
    return JSONResponse(profiles.describe(profile_id), status_code=201)


async def remove(request):
    profiles = request.app.state.profiles
    profile_id = request.path_params["profile_id"]
    if profile_id == "default":
        raise APIError("The initial connection cannot be removed.", 400)
    if profile_id == request.query_params.get("talaria_profile", "default"):
        raise APIError("Switch to another profile before removing this one.", 409)
    async with profiles.lock:
        if profile_id not in profiles.records:
            raise APIError("This profile has already been removed.", 404)
        app = profiles.apps.get(profile_id)
        if profiles.inflight.get(profile_id) or (
            app and any(not c.finished for c in app.state.relay.channels.values())
        ):
            raise APIError(
                "Wait for this profile’s active responses to finish before removing it.", 409
            )
        profiles.removing.add(profile_id)
        try:

            async def forget():
                await profiles.persist(
                    {key: value for key, value in profiles.records.items() if key != profile_id}
                )
                profiles.apps.pop(profile_id, None)
                profiles.inflight.pop(profile_id, None)
                if app:
                    await app.state.relay.close()
                    await app.state.hermes.close()

            await profiles.finish_mutation(forget())
        finally:
            profiles.removing.discard(profile_id)
    return JSONResponse({"ok": True})

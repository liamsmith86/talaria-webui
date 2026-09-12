"""Persist logout revocations without storing session credentials."""

import asyncio
import hashlib
import json
import math
import re
import time

from .config import save_data
from .hermes import APIError

MAX_REVOCATIONS = 8192


def revocation_path(config):
    return config.with_name(config.stem + ".sessions.json")


class Revocations:
    def __init__(self, config):
        self.path = revocation_path(config)
        self.lock = asyncio.Lock()
        self.entries = self.load()

    def load(self):
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(2 * 1024 * 1024 + 1)
        except FileNotFoundError:
            return {}
        try:
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError
            data = json.loads(raw)
            entries = data["revoked"]
            if (
                type(data["version"]) is not int
                or data["version"] != 1
                or not isinstance(entries, dict)
                or len(entries) > MAX_REVOCATIONS
            ):
                raise ValueError
            for digest, expiry in entries.items():
                if (
                    not re.fullmatch(r"[a-f0-9]{64}", digest)
                    or type(expiry) not in {int, float}
                    or not math.isfinite(expiry)
                ):
                    raise ValueError
            return {digest: expiry for digest, expiry in entries.items() if expiry > time.time()}
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            # Never silently discard revocations after a damaged file or downgrade.
            raise ValueError(f"Cannot read sign-out history: {self.path}") from exc

    def contains(self, cookie):
        digest = hashlib.sha256(cookie.encode()).hexdigest()
        return self.entries.get(digest, 0) > time.time()

    async def revoke(self, cookie, lifetime):
        digest = hashlib.sha256(cookie.encode()).hexdigest()
        expiry = int(cookie.split(".", 1)[0]) + lifetime
        async with self.lock:
            entries = {key: end for key, end in self.entries.items() if end > time.time()}
            if digest not in entries and len(entries) >= MAX_REVOCATIONS:
                # An unexpired revocation must never be evicted to make room.
                raise APIError(
                    "Could not sign out. Change the WebUI password to end all sessions.", 503
                )
            entries[digest] = expiry
            try:
                await asyncio.to_thread(save_data, self.path, {"version": 1, "revoked": entries})
            except OSError as exc:
                raise APIError("Could not save your sign-out. Please try again.", 503) from exc
            self.entries = entries

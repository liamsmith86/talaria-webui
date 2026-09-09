"""Single-owner authentication and browser request protection."""

import hashlib
import hmac
import secrets
import time
from collections import OrderedDict

from starlette.requests import Request

COOKIE = "talaria_session"
TTL = 60 * 60 * 24 * 14


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1)
    return f"{salt}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored or len(password) > 1024:
        return False
    salt, expected = stored.split(":", 1)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1)
    return hmac.compare_digest(digest.hex(), expected)


def signature(settings, message: str) -> str:
    return hmac.new(settings.signing_key.encode(), message.encode(), "sha256").hexdigest()


def issue_cookie(settings) -> str:
    payload = f"{int(time.time())}.{secrets.token_hex(16)}"
    return f"{payload}.{signature(settings, payload)}"


def authenticated(request: Request) -> bool:
    value = request.cookies.get(request.app.state.cookie_name, "")
    try:
        timestamp, nonce, sig = value.split(".")
        age = time.time() - int(timestamp)
        return 0 <= age < TTL and hmac.compare_digest(
            signature(request.app.state.settings, f"{timestamp}.{nonce}"), sig
        )
    except (ValueError, TypeError):
        return False


def csrf_token(request: Request) -> str:
    return signature(
        request.app.state.settings,
        "csrf:" + request.cookies.get(request.app.state.cookie_name, ""),
    )


def browser_request_valid(request: Request, *, login: bool = False) -> bool:
    expected = request.app.state.settings.public_url or str(request.base_url).rstrip("/")
    origin = request.headers.get("origin")
    if origin and origin != expected:
        return False
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    if request.headers.get("x-talaria-request") != "1":
        return False
    return login or hmac.compare_digest(
        request.headers.get("x-csrf-token", ""), csrf_token(request)
    )


class LoginLimiter:
    def __init__(self):
        self.failures: OrderedDict[str, list[float]] = OrderedDict()

    def allow(self, address: str) -> bool:
        now = time.monotonic()
        times = [t for t in self.failures.pop(address, []) if now - t < 300]
        self.failures[address] = times
        while len(self.failures) > 1024:
            self.failures.popitem(last=False)
        if len(times) >= 8:
            return False
        times.append(now)
        return True

    def success(self, address: str) -> None:
        self.failures.pop(address, None)

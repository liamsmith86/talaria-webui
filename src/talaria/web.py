"""Shared HTTP shell and static assets for the application and public demo."""

import hashlib
import html
from pathlib import Path

from starlette._utils import get_route_path
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from . import installation

STATIC = Path(__file__).parent / "static"
IMMUTABLE = "public, max-age=31536000, immutable"
ENCODINGS = {"br": ".br", "gzip": ".gz"}
ROBOTS = "User-agent: *\nDisallow: /\n"
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-robots-tag": "noindex, nofollow",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
        "font-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    ),
}


def accepted_encodings(header):
    """Honor explicit exclusions and weights; prefer Brotli for equal weights."""
    weights = {}
    for item in header.lower().split(","):
        name, *parameters = item.strip().split(";")
        weight = 1.0
        for parameter in parameters:
            key, _, value = parameter.strip().partition("=")
            if key == "q":
                try:
                    weight = float(value)
                except ValueError:
                    weight = 0
        weights[name] = weight if 0 <= weight <= 1 else 0
    ranked = sorted(
        ENCODINGS, key=lambda name: weights.get(name, weights.get("*", 0)), reverse=True
    )
    return [
        name
        for name in ranked
        if weights.get(name, weights.get("*", 0)) > 0
        and weights.get(name, weights.get("*", 0)) >= weights.get("identity", 0)
    ]


class Compression(GZipMiddleware):
    async def __call__(self, scope, receive, send):
        # Starlette checks for the substring "gzip", including "gzip;q=0".
        # Negotiate dynamic responses and precompressed assets consistently.
        if scope["type"] == "http" and "gzip" not in accepted_encodings(
            Headers(scope=scope).get("accept-encoding", "")
        ):
            return await self.app(scope, receive, send)
        return await super().__call__(scope, receive, send)


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = get_route_path(scope)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [(k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
                if not path.startswith("/static/"):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        return await self.app(scope, receive, secure_send)


class Assets(StaticFiles):
    def __init__(self, *, cache_control="no-cache", production=False, **kwargs):
        super().__init__(**kwargs)
        self.cache_control = cache_control
        built = Path(self.directory) / "built"
        if production and (built / "index.html").is_file():
            self.all_directories.insert(0, str(built))
        # Wheel contents are immutable. Index names rather than caching payloads or
        # doing extra filesystem lookups on every request; editable installs have none.
        files = {}
        for directory in map(Path, self.all_directories):
            for path in directory.rglob("*"):
                if path.is_file():
                    files.setdefault(path.relative_to(directory).as_posix(), path)
        self.precompressed = {
            name
            for name, path in files.items()
            if path.suffix in ENCODINGS.values()
            and files.get(name[: -len(path.suffix)]) == path.with_suffix("")
        }

    async def get_response(self, path, scope):
        if Path(path).suffix in ENCODINGS.values():
            raise HTTPException(404)
        headers = Headers(scope=scope)
        encoding = next(
            (
                name
                for name in accepted_encodings(headers.get("accept-encoding", ""))
                if path + ENCODINGS[name] in self.precompressed
            ),
            None,
        )
        # Ranges refer to the original file's bytes.
        if "range" in headers:
            encoding = None
        target = path + ENCODINGS[encoding] if encoding else path
        response = await super().get_response(target, scope)
        if encoding:
            response.headers["Content-Encoding"] = encoding
        response.headers.add_vary_header("Accept-Encoding")
        # Stable filenames revalidate with ETag instead of risking a mixture of
        # cached modules from different app versions. A build's own directory never
        # changes, so browsers keep its files without asking.
        response.headers["Cache-Control"] = self.cache_control
        return response


def build_path(directory, seed=b""):
    """A static path naming this exact build, so its relative imports stay within it."""
    digest = hashlib.sha256(seed)
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
            digest.update(path.read_bytes())
    return f"static/build-{digest.hexdigest()[:16]}/"


def in_build(document, build):
    return (
        document.replace("__TALARIA_BASE__static/", "__TALARIA_BASE__" + build)
        # The manifest keeps its stable installation identity and launch scope.
        .replace(build + "app.webmanifest", "static/app.webmanifest")
    )


def index_document(directory, production):
    built = directory / "built/index.html"
    return (built if production and built.is_file() else directory / "index.html").read_text()


def asset_routes(immutable, directory):
    """The static mounts, and the document that loads its assets from them."""
    document = index_document(directory, immutable)
    stable = Mount("/static", Assets(directory=directory))
    if not immutable:
        return [stable], document
    build = build_path(directory)
    named = Mount(
        "/" + build.rstrip("/"),
        Assets(directory=directory, cache_control=IMMUTABLE, production=True),
    )
    return [named, stable], in_build(document, build)


async def index(request):
    # The document is tiny; cache the template per application, not per request.
    base = html.escape(request.app.state.settings.base_path + "/", quote=True)
    return HTMLResponse(request.app.state.index_html.replace("__TALARIA_BASE__", base))


async def robots(request):
    return PlainTextResponse(ROBOTS)


async def health(request):
    return JSONResponse({"status": "ok", **installation.build_info()})

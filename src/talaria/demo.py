"""Public demo shell: only synthetic fixtures, never private config or Hermes."""

import argparse
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from . import __version__
from .app import STATIC, Assets, SecurityHeaders, health, index, robots
from .config import Settings, validate_public_url
from .proxy import PublicPath


def create_demo(*, public_url=""):
    sample_data = Path(__file__).with_name("demo_data.json").read_bytes()

    async def bootstrap(request):
        return JSONResponse(
            {
                "version": __version__,
                "environment": "demo",
                "authenticated": True,
                "connected": True,
                "agent": {"name": "Hermes", "name_source": "configured"},
                "profile": {
                    "id": "default",
                    "label": "Hermes",
                    "profile": "default",
                    "configured": True,
                },
            }
        )

    async def samples(request):
        return Response(sample_data, media_type="application/json")

    async def missing(request):
        return JSONResponse({"error": "Not found."}, status_code=404)

    async def readonly(request, exc):
        return JSONResponse(
            {"error": "The demo is read-only.", "code": "demo_read_only"},
            status_code=405,
            headers={"Allow": "GET, HEAD"},
        )

    app = Starlette(
        exception_handlers={405: readonly},
        routes=[
            Route("/", index),
            Route("/robots.txt", robots),
            Route("/health", health),
            Route("/api/bootstrap", bootstrap),
            Route("/api/samples", samples),
            Route("/api/{path:path}", missing),
            Mount("/static", GZipMiddleware(Assets(directory=STATIC), minimum_size=1024)),
        ],
    )
    app.state.settings = Settings(public_url=validate_public_url(public_url) if public_url else "")
    app.state.index_html = (
        (STATIC / "index.html")
        .read_text()
        .replace(
            "</head>",
            '<link rel="stylesheet" href="__TALARIA_BASE__static/styles/demo.css" /></head>',
        )
    )
    app.add_middleware(SecurityHeaders)
    app.add_middleware(PublicPath, prefix=app.state.settings.base_path)
    return app


def main(argv):
    import uvicorn

    parser = argparse.ArgumentParser(description="Serve read-only Talaria sample sessions.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--public-url", type=validate_public_url)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535.")
    if os.environ.get("TALARIA_CONTAINER") == "1":
        from .container_health import record

        record(args.host, args.port)
    uvicorn.run(
        create_demo(public_url=args.public_url),
        host=args.host,
        port=args.port,
        proxy_headers=False,
        access_log=False,
        timeout_graceful_shutdown=8,
        workers=1,
    )

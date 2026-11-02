"""Exercise the actual wheel's encodings, caching and browser module loading."""

import gzip
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount

from talaria.web import IMMUTABLE, Assets, Compression

from .conftest import browser_context, copy_release_project, serve


@pytest.mark.parametrize(
    "file,message",
    [
        ("app.js", "Production assets are stale"),
        ("built/app.js", "Production asset is modified"),
        ("built/obsolete.js", "Production outputs changed"),
    ],
)
def test_packaging_rejects_stale_assets(tmp_path, file, message):
    copy_release_project(Path(__file__).resolve().parents[1], tmp_path)
    modified = tmp_path / "src/talaria/static" / file
    modified.write_bytes(modified.read_bytes() + b"\n" if modified.exists() else b"export {};")
    result = subprocess.run(
        [shutil.which("uv"), "build", "--wheel", "--out-dir", str(tmp_path / "dist")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert message in result.stderr


def application(directory):
    app = Starlette(
        routes=[
            Mount("/static", Assets(directory=directory, cache_control=IMMUTABLE, production=True))
        ]
    )
    return Compression(app, minimum_size=1024)


@pytest.mark.parametrize(
    "accept,encoding",
    [
        ("gzip, br", "br"),
        ("br;q=0.5, gzip;q=1", "gzip"),
        ("br;q=0, gzip;q=0", None),
        ("br;q=0, *;q=1", "gzip"),
        ("identity;q=1, br;q=0.5", None),
        ("identity", None),
    ],
)
async def test_packaged_assets_negotiate_and_revalidate(packaged_assets, accept, encoding):
    plain = (packaged_assets / "built/app.js").read_bytes()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application(packaged_assets)), base_url="http://assets.test"
    ) as client:
        headers = {"Accept-Encoding": accept}
        async with client.stream("GET", "/static/app.js", headers=headers) as response:
            wire = b"".join([chunk async for chunk in response.aiter_raw()])
            assert response.status_code == 200
            assert response.headers.get("content-encoding") == encoding
            assert response.headers["content-type"].startswith("text/javascript")
            assert response.headers["cache-control"] == IMMUTABLE
            assert "Accept-Encoding" in response.headers["vary"]
            assert int(response.headers["content-length"]) == len(wire)
            etag = response.headers["etag"]
        if encoding == "br":
            assert wire == (packaged_assets / "built/app.js.br").read_bytes()
            assert len(wire) < len(gzip.compress(plain))
        else:
            assert (gzip.decompress(wire) if encoding else wire) == plain
        cached = await client.get("/static/app.js", headers={**headers, "If-None-Match": etag})
        assert cached.status_code == 304
        head = await client.head("/static/app.js", headers=headers)
        assert not head.content and int(head.headers["content-length"]) == len(wire)
        identity = await client.get("/static/app.js", headers={"Accept-Encoding": "identity"})
        if encoding:
            assert identity.headers["etag"] != etag
            changed = await client.get(
                "/static/app.js", headers={**headers, "If-None-Match": identity.headers["etag"]}
            )
            assert changed.status_code == 200
        partial = await client.get(
            "/static/app.js", headers={"Accept-Encoding": "br", "Range": "bytes=0-49"}
        )
        assert partial.status_code == 206 and partial.content == plain[:50]
        assert (await client.get("/static/app.js.br")).status_code == 404
        boot = await client.get("/static/boot.js", headers={"Accept-Encoding": "gzip"})
        assert boot.content == (packaged_assets / "built/boot.js").read_bytes()


def test_browser_decodes_packaged_modules(browser, packaged_assets):
    server, thread, url = serve(application(packaged_assets))
    try:
        with browser_context(browser) as context:
            page = context.new_page()
            page.goto(url + "/static/mark.svg")
            result = page.evaluate("""async () => {
              const {randomId, html} = await import('/static/lib.js');
              return {id: randomId().length, tag: html`<span />`.type};
            }""")
            assert result == {"id": 32, "tag": "span"}
    finally:
        server.should_exit = True
        thread.join(6)

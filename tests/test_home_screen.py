"""Home-screen launch URLs, icons and browser chrome follow the installation."""

import re
import struct
from urllib.parse import urljoin

import httpx
import pytest
from playwright.sync_api import expect

from talaria.app import create_app
from talaria.config import Settings


@pytest.mark.parametrize("prefix", ["", "/apps/talaria"])
async def test_home_screen_assets_are_public_and_scoped(tmp_path, prefix):
    origin = "https://talaria.example"
    app = create_app(Settings(public_url=origin + prefix), tmp_path / "config.json")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=origin) as client:
            page = await client.get(prefix + "/")
            manifest_path = re.search(r'rel="manifest" href="([^"]+)"', page.text)[1]
            response = await client.get(manifest_path)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("application/manifest+json")
            manifest = response.json()
            assert manifest["display"] == "standalone"
            for key in ("id", "start_url", "scope"):
                assert urljoin(str(response.url), manifest[key]) == origin + prefix + "/"
            assert {i["sizes"] for i in manifest["icons"]} == {"192x192", "512x512"}
            for icon in manifest["icons"]:
                image = await client.get(urljoin(str(response.url), icon["src"]))
                assert image.headers["content-type"] == "image/png"
                assert image.content[:8] == b"\x89PNG\r\n\x1a\n"
                assert struct.unpack(">II", image.content[16:24]) == tuple(
                    map(int, icon["sizes"].split("x"))
                )
            apple = re.search(r'rel="apple-touch-icon"[^>]*href="([^"]+)"', page.text)[1]
            image = await client.get(apple)
            assert struct.unpack(">II", image.content[16:24]) == (180, 180)
            # Home-screen support must not make any private API public.
            assert (await client.get(prefix + "/api/sessions")).status_code == 401
    finally:
        await app.state.profiles.close()


def test_home_screen_chrome_tracks_saved_and_system_theme(page):
    color = page.locator('meta[name="theme-color"]')
    page.emulate_media(color_scheme="dark")
    page.evaluate("document.documentElement.dataset.theme = 'system'")
    expect(color).to_have_attribute("content", "#191c1b")
    page.emulate_media(color_scheme="light")
    expect(color).to_have_attribute("content", "#fbfbf9")
    page.evaluate("document.documentElement.dataset.theme = 'dark'")
    expect(color).to_have_attribute("content", "#191c1b")
    page.evaluate("localStorage.setItem('talaria.theme', 'light')")
    page.reload()
    expect(color).to_have_attribute("content", "#fbfbf9")


def test_maskable_icon_keeps_the_mark_inside_the_safe_circle(page):
    assert page.evaluate("""async () => {
      const image = new Image(); image.src = '/static/icons/maskable-512.png';
      await image.decode();
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = 512;
      const ctx = canvas.getContext('2d'); ctx.drawImage(image, 0, 0);
      const pixels = ctx.getImageData(0, 0, 512, 512).data;
      for (let y=0; y<512; y++) for (let x=0; x<512; x++) {
        const i = (y*512+x)*4;
        if (pixels[i+3] !== 255) return false;
        if (pixels[i] > 200 && pixels[i+1] > 200 &&
            Math.hypot(x-256,y-256) > 512*.4) return false;
      }
      return true;
    }""")

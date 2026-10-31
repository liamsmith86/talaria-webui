"""Public subpaths must preserve authentication, assets, profiles, and streaming."""

import re
import shutil

import httpx
import pytest
from playwright.sync_api import expect

from talaria import app as application
from talaria.app import create_app
from talaria.auth import hash_password
from talaria.config import Settings, validate_public_url

from .conftest import capture_browser_errors, serve
from .fake_hermes import KEY, FakeHermes


@pytest.mark.parametrize("path", ["", "/apps/talaria"])
async def test_a_build_directory_is_immutable_and_names_its_contents(tmp_path, monkeypatch, path):
    settings = Settings(
        public_url="https://example.com" + path,
        password_hash=hash_password("test-password"),
        signing_key="key",
        api_key=KEY,
    )

    async def entry(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://example.com"
        ) as client:
            page = await client.get(path + "/")
            assert page.headers["cache-control"] == "no-store"
            return page.text, re.search(r'src="([^"]+/)boot\.js"', page.text)[1]

    app = create_app(settings, tmp_path / "config.json", immutable_assets=True)
    document, build = await entry(app)
    assert re.fullmatch(rf"{path}/static/build-[a-f0-9]{{16}}/", build)
    # Only the manifest stays put: it holds the installed app's identity and scope.
    stable = set(re.findall(rf'"{path}/static/(?!build-)([^"]+)"', document))
    assert stable == {"app.webmanifest"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://example.com"
    ) as client:
        for asset in ["app.js", "paths.js", "styles/theme.css", "vendor/inter.woff2"]:
            response = await client.get(build + asset)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
        # Release checks and tabs from before this scheme use the stable names.
        assert (await client.get(path + "/static/app.js")).headers["cache-control"] == "no-cache"

    # Any changed file moves every file, so a browser can never mix two builds.
    assets = tmp_path / "release"
    shutil.copytree(application.STATIC, assets)
    with (assets / "lib.js").open("a") as file:
        file.write("\n// A new release.\n")
    monkeypatch.setattr(application, "STATIC", assets)
    replacement = create_app(settings, tmp_path / "config.json", immutable_assets=True)
    _, next_build = await entry(replacement)
    assert next_build != build
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(replacement), base_url="https://example.com"
    ) as client:
        assert (await client.get(build + "lib.js")).status_code == 404
        assert (await client.get(next_build + "lib.js")).text.endswith("// A new release.\n")


@pytest.mark.parametrize("path", ["/telaria", "/apps/talaria", "/v1"])
@pytest.mark.parametrize("strip", [False, True])
async def test_proxy_authentication_and_assets(tmp_path, path, strip):
    settings = Settings(
        public_url="https://example.com" + path,
        password_hash=hash_password("test-password"),
        signing_key="key",
        api_key=KEY,
    )
    app = create_app(settings, tmp_path / "config.json")

    async def proxy(scope, receive, send):
        if strip and scope["path"].startswith(path + "/"):
            scope = dict(scope, path=scope["path"][len(path) :])
        await app(scope, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(proxy), base_url="https://example.com"
    ) as client:
        page = await client.get(path + "/")
        assert f'src="{path}/static/boot.js"' in page.text
        assert "base-uri 'none'" in page.headers["content-security-policy"]
        assert page.headers["x-robots-tag"] == "noindex, nofollow"
        robots = await client.get(path + "/robots.txt")
        assert robots.status_code == 200
        assert robots.text == "User-agent: *\nDisallow: /\n"
        for asset in ["app.js", "paths.js", "styles/theme.css", "vendor/inter.woff2"]:
            response = await client.get(path + "/static/" + asset)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-cache"
        assert (await client.get(path + "/api/sessions")).status_code == 401
        headers = {"Origin": "https://evil.example", "X-Talaria-Request": "1"}
        assert (
            await client.post(
                path + "/api/login", json={"password": "test-password"}, headers=headers
            )
        ).status_code == 403
        headers["Origin"] = "https://example.com"
        login = await client.post(
            path + "/api/login", json={"password": "test-password"}, headers=headers
        )
        assert login.status_code == 200
        assert f"Path={path}/" in login.headers["set-cookie"]
        assert "HttpOnly" in login.headers["set-cookie"] and "Secure" in login.headers["set-cookie"]
        bootstrap = (await client.get(path + "/api/bootstrap")).json()
        assert bootstrap["authenticated"] is True
        headers["X-CSRF-Token"] = bootstrap["csrf"]
        # Testing a new profile must never silently reuse this profile's key.
        tested = await client.post(
            path + "/api/profiles/test",
            headers=headers,
            json={"url": settings.hermes_url, "api_key": ""},
        )
        assert tested.status_code == 400
        assert (await client.post(path + "/api/logout", headers=headers)).status_code == 200
        assert (await client.get(path + "/api/bootstrap")).json()["authenticated"] is False
        redirect = await client.get(path + "?profile=default")
        assert redirect.status_code == 308
        assert redirect.headers["location"] == path + "/?profile=default"
    await app.state.profiles.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://host/a/../b",
        "https://host/%2e%2e",
        "https://host/a//b",
        "https://host/a b",
        "https://host/a?b",
        "https://host/a#b",
    ],
)
def test_ambiguous_public_paths_are_rejected(url):
    with pytest.raises(ValueError):
        validate_public_url(url)


@pytest.mark.parametrize("strip", [False, True])
def test_browser_behind_subpath_proxy(browser, tmp_path, strip):
    peer = FakeHermes()
    upstream, ut, hermes_url = serve(peer.app)
    settings = Settings(
        hermes_url=hermes_url,
        api_key=KEY,
        password_hash=hash_password("test-password"),
        signing_key="key",
        public_url="http://127.0.0.1/apps/telaria",
    )
    app = create_app(settings, tmp_path / "config.json")
    prefix = "/apps/telaria"

    async def proxy(scope, receive, send):
        if scope["type"] == "http":
            if not scope["path"].startswith(prefix):
                from starlette.responses import Response

                return await Response(status_code=404)(scope, receive, send)
            if strip and scope["path"].startswith(prefix + "/"):
                scope = dict(scope, path=scope["path"][len(prefix) :])
        await app(scope, receive, send)

    server, thread, address = serve(proxy)
    settings.public_url = address + prefix
    context = browser.new_context(viewport={"width": 390, "height": 844})
    errors = capture_browser_errors(context)
    page = context.new_page()
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    try:
        page.goto(address + prefix)
        page.get_by_label("Password", exact=True).fill("test-password")
        page.get_by_role("button", name="Sign in").click()
        page.locator(".topbar-title").wait_for()
        page.get_by_label("Message Hermes").fill("A streamed response behind a proxy")
        page.get_by_role("button", name="Send message").click()
        expect(page.locator(".message.assistant")).to_contain_text(
            "What would you like to explore next?"
        )
        values = page.evaluate(
            """async prefix => {
          const paths = await import(prefix + '/static/profile-context.js');
          const navigation = await import(prefix + '/static/session-navigation.js');
          return {api: paths.apiURL('/sessions'),
                  storage: paths.storagePrefix, db: paths.databaseName,
                  session: navigation.sessionURL('linked')};
        }""",
            prefix,
        )
        assert values["api"] == prefix + "/api/sessions"
        assert values["session"] == prefix + "/?session=linked"
        page.evaluate(
            "async prefix => (await import(prefix + '/static/store.js'))"
            ".update({modal:'profiles'})",
            prefix,
        )
        expect(page.locator("a.profile-option").first).to_have_attribute(
            "href", prefix + "/?profile=default"
        )
        assert "%2Fapps%2Ftelaria%2F" in values["storage"]
        assert "%2Fapps%2Ftelaria%2F" in values["db"]
        assert all(url.startswith(address + prefix + "/") for url in requests[1:])
        assert not errors
        # Reload with the path-scoped cookie; the session remains authenticated.
        page.reload()
        page.locator(".topbar-title").wait_for()
        assert context.cookies()[0]["path"] == prefix + "/"
    finally:
        context.close()
        server.should_exit = upstream.should_exit = True
        thread.join(timeout=10)
        ut.join(timeout=10)


def test_public_origin_matches_browser_normalization():
    assert validate_public_url("https://EXAMPLE.com:443/telaria/") == "https://example.com/telaria"
    assert validate_public_url("http://[::1]:80/telaria") == "http://[::1]/telaria"

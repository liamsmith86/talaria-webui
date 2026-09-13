"""User-visible failures and recovery, using isolated transports and browser fixtures."""

import httpx
import pytest
from playwright.sync_api import expect

from talaria import auth, config
from talaria.app import create_app
from talaria.hermes import APIError, Hermes


@pytest.mark.parametrize("failure", ["offline", "timeout", "malformed", 401, 429, 503])
async def test_upstream_failures_do_not_prevent_the_next_request(failure):
    failed = True

    def upstream(request):
        if not failed:
            return httpx.Response(200, json={"ok": True})
        if failure == "offline":
            raise httpx.ConnectError("internal address", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("internal address", request=request)
        if failure == "malformed":
            return httpx.Response(200, text="not json")
        return httpx.Response(failure, json={"error": {"message": "private upstream details"}})

    peer = Hermes("http://isolated.test", "test-key", transport=httpx.MockTransport(upstream))
    try:
        with pytest.raises(APIError) as caught:
            await peer.request("GET", "/v1/capabilities")
        assert "private" not in str(caught.value) and "internal address" not in str(caught.value)
        if failure == "malformed":
            assert caught.value.code == "invalid_response"
        failed = False
        assert await peer.request("GET", "/v1/capabilities") == {"ok": True}
    finally:
        await peer.close()


async def test_unexpected_server_failure_is_readable_and_next_request_works(tmp_path):
    settings = config.Settings(signing_key="test-only")
    app = create_app(settings, tmp_path / "config.json")

    async def broken(*args, **kwargs):
        raise RuntimeError("private implementation detail")

    app.state.hermes.request = broken
    async with httpx.AsyncClient(
        base_url="http://talaria.test",
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
    ) as client:
        client.cookies.set(auth.COOKIE, auth.issue_cookie(settings))
        response = await client.get("/api/capabilities")
        assert response.status_code == 500
        assert response.json()["code"] == "internal_error"
        assert "private" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert (await client.get("/api/bootstrap")).status_code == 200
    await app.state.profiles.close()


@pytest.mark.parametrize("error", [{"message": "Provider quota exhausted"}, {}, None])
def test_failed_run_always_displays_a_useful_error(page, error):
    page.evaluate(
        """async error => {
      const {applyEvent} = await import('/static/runs.js');
      const {update} = await import('/static/store.js');
      const live = applyEvent({id:'failure-test', text:'Partial response', tools:[],
        status:'running', userText:'Hello'}, {event:'run.failed',error});
      update({active:'failure-test',history:[],lives:{'failure-test':live}});
    }""",
        error,
    )
    expected = (error or {}).get("message") or "Hermes could not finish this response."
    expect(page.locator(".run-error")).to_contain_text(expected)
    expect(page.get_by_label("Message Hermes")).to_be_enabled()
    expect(page.get_by_role("button", name="Stop response")).to_have_count(0)


def test_structured_api_errors_remain_readable(module_page):
    result = module_page.evaluate("""async () => {
      const {api} = await import('/static/api.js');
      const fetch = window.fetch;
      try {
        window.fetch = async () => Response.json(
          {error:{message:'Provider quota exhausted'}}, {status:429});
        return await api('/failure-test').catch(e => ({message:e.message,status:e.status}));
      } finally {window.fetch = fetch;}
    }""")
    assert result == {"message": "Provider quota exhausted", "status": 429}

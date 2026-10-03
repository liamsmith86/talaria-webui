"""Connection feedback must describe the inputs and discovery that are still current."""

import pytest
from playwright.sync_api import expect


def render_connection(page, *, deferred_load=False):
    page.evaluate(
        """async deferredLoad => {
        const {html, render} = await import('/static/lib.js');
        const {Connection} = await import('/static/dialogs.js');
        window.connectionRequests = [];
        window.fetch = (url, options) => {
            if (String(url).endsWith('/connection')) {
                if (deferredLoad) return new Promise(resolve =>
                    window.connectionLoadReply = resolve);
                return Promise.resolve(Response.json({
                    url:'http://saved.example:8642', profile:'default', key_set:false,
                }));
            }
            if (String(url).endsWith('/connection/test')) {
                window.connectionRequests.push(JSON.parse(options.body));
                return new Promise(resolve => window.connectionTestReply = resolve);
            }
            throw new Error(`Unexpected request: ${url}`);
        };
        render(html`<${Connection} embedded onClose=${() => {}} />`, document.body);
    }""",
        deferred_load,
    )


@pytest.mark.parametrize("failed", [False, True])
def test_connection_waits_for_saved_values_before_allowing_edits_or_submission(module_page, failed):
    page = module_page
    render_connection(page, deferred_load=True)
    page.wait_for_function("typeof window.connectionLoadReply === 'function'")
    test = page.get_by_role("button", name="Test connection", exact=True)
    save = page.get_by_role("button", name="Save connection", exact=True)
    expect(test).to_be_disabled()
    expect(save).to_be_disabled()
    for label in ("Hermes address", "Hermes profile", "API key"):
        expect(page.get_by_label(label, exact=True)).to_be_disabled()
    page.evaluate(
        """failed => window.connectionLoadReply(failed
            ? Response.json({error:'Saved connection could not be loaded.'}, {status:503})
            : Response.json({url:'http://saved.example:8642',
                profile:'research', key_set:true}))""",
        failed,
    )
    if failed:
        expect(page.get_by_role("alert")).to_have_text("Saved connection could not be loaded.")
        expect(test).to_be_disabled()
        expect(save).to_be_disabled()
    else:
        expect(page.get_by_label("Hermes address", exact=True)).to_have_value(
            "http://saved.example:8642"
        )
        expect(page.get_by_label("Hermes profile", exact=True)).to_have_value("research")
        expect(page.get_by_label("API key", exact=True)).to_have_attribute(
            "placeholder", "Leave blank to keep saved key"
        )
        expect(test).to_be_enabled()
        expect(save).to_be_enabled()


def test_late_connection_test_does_not_verify_edited_credentials(module_page):
    page = module_page
    render_connection(page)
    expect(page.get_by_label("Hermes address", exact=True)).to_have_value(
        "http://saved.example:8642"
    )
    key = page.get_by_label("API key", exact=True)
    key.fill("original fixture key")
    test = page.get_by_role("button", name="Test connection", exact=True)
    test.click()
    page.wait_for_function("typeof window.connectionTestReply === 'function'")
    key.fill("changed fixture key")
    page.evaluate("window.connectionTestReply(Response.json({ok:true}))")
    expect(page.get_by_role("button", name="Save connection", exact=True)).to_be_enabled()
    expect(page.get_by_text("Hermes connection verified.", exact=True)).to_have_count(0)
    assert page.evaluate("window.connectionRequests[0].api_key") == "original fixture key"

    test.click()
    page.wait_for_function("window.connectionRequests.length === 2")
    page.evaluate(
        "window.connectionTestReply(Response.json({error:'Invalid new key.'}, {status:401}))"
    )
    expect(page.get_by_role("alert")).to_have_text("Invalid new key.")
    assert page.evaluate("window.connectionRequests[1].api_key") == "changed fixture key"
    expect(page.get_by_text("Hermes connection verified.", exact=True)).to_have_count(0)
    key.fill("valid fixture key")
    test.click()
    page.wait_for_function("window.connectionRequests.length === 3")
    page.evaluate("window.connectionTestReply(Response.json({ok:true}))")
    expect(page.get_by_text("Hermes connection verified.", exact=True)).to_be_visible()


@pytest.mark.parametrize("old_failure", [False, True])
def test_reconnecting_refreshes_health_and_discards_old_connection_responses(
    module_page, old_failure
):
    result = module_page.evaluate(
        """async oldFailure => {
        const s = await import('/static/store.js');
        const health = [], agents = [];
        window.fetch = url => {
            if (String(url).endsWith('/readiness'))
                return new Promise(resolve => health.push(resolve));
            if (String(url).endsWith('/agent')) return new Promise(resolve => agents.push(resolve));
            if (String(url).endsWith('/capabilities')) return Promise.resolve(Response.json({
                features:{}, talaria_agent:{name:'New connection', name_source:'hermes'},
                talaria_extensions:{version:1},
            }));
            throw new Error(`Unexpected request: ${url}`);
        };
        const oldHealth = s.refreshReadiness();
        const errors = [];
        const oldAgent = s.refreshAgentInfo().catch(e => errors.push(e.message));
        await s.connect();
        const current = s.refreshReadiness();
        if (health.length !== 2) {
            health[0](Response.json({status:'degraded', issues:[]}));
            agents[0](Response.json({name:'Old connection', extended_access:{},
                readiness:{status:'degraded'}}));
            await Promise.all([oldHealth, oldAgent, current]);
            return {requests:health.length};
        }
        health[1](Response.json({status:'ok', issues:[]}));
        await current;
        const stale = data => oldFailure
            ? Response.json({error:'Old connection failed.'}, {status:503}) : Response.json(data);
        health[0](stale({status:'degraded', issues:[]}));
        agents[0](stale({name:'Old connection', extended_access:{},
            readiness:{status:'degraded'}}));
        await Promise.all([oldHealth, oldAgent]);
        return {requests:health.length, agent:s.state.agent.name,
            readiness:s.state.readiness.status,
            plugin:s.state.caps.talaria_extensions.version, errors};
    }""",
        old_failure,
    )
    assert result == {
        "requests": 2,
        "agent": "New connection",
        "readiness": "ok",
        "plugin": 1,
        "errors": [],
    }


@pytest.mark.parametrize("old_failure", [False, True])
def test_agent_refresh_keeps_newer_identity_plugin_and_health(module_page, old_failure):
    result = module_page.evaluate(
        """async oldFailure => {
        const s = await import('/static/store.js');
        const replies = [];
        window.fetch = () => new Promise(resolve => replies.push(resolve));
        const errors = [];
        const old = s.refreshAgentInfo().catch(e => errors.push(e.message));
        const current = s.refreshAgentInfo();
        replies[1](Response.json({name:'Current agent', extended_access:{version:1},
            readiness:{status:'ok', issues:[]}}));
        await current;
        replies[0](oldFailure ? Response.json({error:'Outdated failure.'}, {status:503})
            : Response.json({name:'Old agent', extended_access:{}, readiness:{status:'degraded'}}));
        await old;
        return {agent:s.state.agent.name, readiness:s.state.readiness.status,
            plugin:s.state.caps.talaria_extensions.version, errors};
    }""",
        old_failure,
    )
    assert result == {"agent": "Current agent", "readiness": "ok", "plugin": 1, "errors": []}


@pytest.mark.parametrize("old_endpoint", ["readiness", "agent"])
def test_health_feedback_keeps_the_latest_check_across_endpoints(module_page, old_endpoint):
    result = module_page.evaluate(
        """async oldEndpoint => {
        const s = await import('/static/store.js');
        const replies = {};
        window.fetch = url => new Promise(resolve =>
            replies[String(url).split('/').at(-1)] = resolve);
        const old = oldEndpoint === 'readiness' ? s.refreshReadiness() : s.refreshAgentInfo();
        const current = oldEndpoint === 'readiness' ? s.refreshAgentInfo() : s.refreshReadiness();
        const data = status => ({name:'Current agent', extended_access:{version:1},
            readiness:{status, issues:[]}, status, issues:[]});
        replies[oldEndpoint === 'readiness' ? 'agent' : 'readiness'](Response.json(data('ok')));
        await current;
        replies[oldEndpoint](Response.json(data('degraded')));
        await old;
        return s.state.readiness.status;
    }""",
        old_endpoint,
    )
    assert result == "ok"

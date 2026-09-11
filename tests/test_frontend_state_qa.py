"""Frontend regressions using isolated browsers and the simulated Hermes service."""

import pytest
from playwright.sync_api import expect

from .conftest import wait_for_store
from .test_conversation_features import IMAGE, seed


def test_send_acknowledgement_does_not_erase_new_draft_after_navigation(page, live_app):
    sid = seed(live_app[1], "pending-send", count=2)
    child = seed(live_app[1], "read-only-child", count=2)
    page.evaluate("async sid => (await import('/static/store.js')).openSession(sid)", sid)
    admissions = []
    page.route("**/api/runs", lambda route: admissions.append(route))
    composer = page.get_by_label("Message Hermes")
    composer.fill("Original submitted message")
    page.get_by_role("button", name="Send message", exact=True).click()
    try:
        wait_for_store(page, "s => s.lives['pending-send']?.status === 'starting'")
        page.evaluate(
            "async ids => (await import('/static/store.js')).openSession(ids.child, {id:ids.sid})",
            {"child": child, "sid": sid},
        )
        expect(page.locator(".composer")).to_have_count(0)
        page.get_by_role("button", name="Back to parent session").click()
        expect(composer).to_have_value("Original submitted message")
        composer.fill("New draft while the original message is being accepted")
    finally:
        for route in admissions:
            route.continue_()
        page.unroute("**/api/runs")
    wait_for_store(page, "s => s.lives['pending-send']?.status === 'completed'")
    expect(composer).to_have_value("New draft while the original message is being accepted")
    page.reload()
    expect(composer).to_have_value("New draft while the original message is being accepted")


def test_approval_answered_elsewhere_clears_only_matching_prompt(page, live_app):
    sid = seed(live_app[1], "approval-sync", count=2)
    page.evaluate("async sid => (await import('/static/store.js')).openSession(sid)", sid)
    page.evaluate(
        """async sid => {
      const {update} = await import('/static/store.js');
      const {applyEvent} = await import('/static/runs.js');
      const live = {id:'test-run',status:'running',tools:[],text:'',userText:'Request',parts:[]};
      update({lives:{[sid]:applyEvent(live, {event:'approval.request',
        request_id:'second',command:'echo harmless',choices:['once','deny']})}});
    }""",
        sid,
    )
    card = page.get_by_role("region", name="Approval required")
    expect(card).to_be_visible()
    for request_id, remaining in [("first", True), ("second", False)]:
        page.evaluate(
            """async ({sid,request_id}) => {
          const {state,update} = await import('/static/store.js');
          const {applyEvent} = await import('/static/runs.js');
          update({lives:{[sid]:applyEvent(state.lives[sid],
            {event:'approval.responded',request_id,resolved:1})}});
        }""",
            {"sid": sid, "request_id": request_id},
        )
        expect(card).to_have_count(1 if remaining else 0)
    wait_for_store(page, "s => s.lives['approval-sync'].status === 'running'")


def test_repeated_default_branches_use_distinct_hermes_names(page, live_app):
    peer = live_app[1]
    peer.extension = {"session_fork": True}
    sid = seed(peer, "repeat-original", count=2)
    peer.sessions[sid]["title"] = "Original"
    page.reload()
    for _ in range(3):
        page.get_by_role("link", name="Original", exact=True).click()
        wait_for_store(page, "s => s.active === 'repeat-original' && !s.loading")
        page.get_by_role("button", name="Options for Original", exact=True).click()
        page.get_by_role("button", name="Branch session", exact=True).click()
        page.get_by_role("button", name="Create branch", exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        wait_for_store(page, "s => s.active !== 'repeat-original' && !s.loading")
    children = [s for s in peer.sessions.values() if s.get("parent_session_id") == sid]
    assert len(children) == 3
    assert {s["title"] for s in children} == {"Original #2", "Original #3", "Original #4"}


def test_rejected_branch_can_close_and_open_context_usage(page, live_app):
    peer = live_app[1]
    peer.extension = {"session_fork": True}
    sid = seed(peer, "rejected-original", count=2)
    peer.sessions[sid]["title"] = "Original"
    page.reload()
    page.get_by_role("link", name="Original", exact=True).click()
    page.get_by_role("button", name="Options for Original", exact=True).click()
    page.get_by_role("button", name="Branch session", exact=True).click()
    page.get_by_label("Session name", exact=True).fill("Original")
    page.get_by_role("button", name="Create branch", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("already in use")
    expect(page.get_by_role("button", name="Create branch", exact=True)).to_be_enabled()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    page.get_by_role("button", name="Context usage", exact=True).click()
    expect(page.get_by_role("dialog", name="Context usage")).to_be_visible()
    page.get_by_role("button", name="Close dialog", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert len(peer.sessions) == 1


def test_return_to_discord_original_after_branching(page, live_app):
    peer = live_app[1]
    peer.extension = {"session_fork": True}
    sid = seed(peer, "discord-original", count=2)
    peer.sessions[sid].update(source="discord", title="Discord original")
    page.reload()
    original = page.get_by_role("link", name="Discord original", exact=True)
    original.click()
    wait_for_store(page, "state => state.active === 'discord-original' && !state.loading")
    page.get_by_role("button", name="Options for Discord original", exact=True).click()
    page.get_by_role("button", name="Branch session", exact=True).click()
    page.get_by_label("Session name", exact=True).fill("Independent copy")
    page.get_by_role("button", name="Create branch", exact=True).click()
    wait_for_store(page, "state => state.active !== 'discord-original' && !state.loading")
    branch = next(s for s in peer.sessions.values() if s.get("parent_session_id") == sid)
    assert branch["source"] == "api_server"
    peer.messages[branch["id"]] = [
        *peer.messages[branch["id"]],
        {"id": 90, "role": "assistant", "content": "Only in the copy"},
    ]
    original.click()
    wait_for_store(page, "state => state.active === 'discord-original' && !state.loading")
    expect(original).to_have_attribute("aria-current", "page")
    expect(page.locator(".message")).to_have_count(2)
    expect(page.locator(".conversation-content")).not_to_contain_text("Only in the copy")
    page.get_by_role("link", name="Independent copy", exact=True).click()
    expect(page.locator(".message.assistant").last).to_contain_text("Only in the copy")


def test_image_submission_keeps_its_original_boundary_during_navigation(page):
    result = page.evaluate(
        """async image => {
      const {state,update,newConversation}=await import('/static/store.js');
      const {sendMessage}=await import('/static/runs.js');
      update({active:'origin',loading:false,history:[
        {id:10,role:'user',content:'Earlier'}, {id:11,role:'assistant',content:'Earlier answer'}]});
      const original=window.fetch;
      let release;
      window.fetch=async (url,options)=>{
        if(String(url).includes('/origin/messages')) {
          await new Promise(resolve=>release=resolve);
          return Response.json({data:[{id:11}]});
        }
        if(String(url)==='/api/runs') return Response.json({error:'Rejected'}, {status:400});
        return original(url,options);
      };
      try {
        const sending=sendMessage('Question',null,{images:[{id:'image',url:image}]}).catch(()=>{});
        newConversation();
        update({active:'elsewhere',history:[{id:99,role:'user',content:'Unrelated'}]});
        release();await sending;
        const live=state.lives.origin;
        return {active:state.active,user:live.baseUserId,message:live.baseMessageId,
          length:live.baseHistoryLength};
      } finally {window.fetch=original}
    }""",
        IMAGE,
    )
    assert result == {"active": "elsewhere", "user": 10, "message": 11, "length": 2}


def test_send_waits_for_the_selected_conversation_history(page, live_app):
    page.evaluate("""async () => {
      const {update}=await import('/static/store.js');
      update({active:'loading-history',history:[],loading:true});
    }""")
    page.get_by_label("Message Hermes").fill("Prepare this while history loads")
    expect(page.get_by_role("button", name="Send message", exact=True)).to_be_disabled()
    page.get_by_label("Message Hermes").press("Enter")
    assert page.evaluate("""async () => {
      const {sendMessage}=await import('/static/runs.js');
      return sendMessage('No premature submission').then(()=>false,
        error=>error.message.includes('session to load'));
    }""")
    assert not live_app[1].runs
    page.evaluate("""async () => (await import('/static/store.js')).update({loading:false})""")
    expect(page.get_by_role("button", name="Send message", exact=True)).to_be_enabled()


@pytest.mark.parametrize("body", ['{"run_id":', "{}", "aborted body"])
def test_incomplete_run_acknowledgement_preserves_draft_and_safe_retry(page, live_app, body):
    def incomplete_acknowledgement(route):
        response = route.fetch()
        route.fulfill(response=response, body=body)

    if body == "aborted body":
        page.evaluate("""() => {
            const json = Response.prototype.json;
            Response.prototype.json = function (...args) {
                if (this.url.endsWith('/api/runs') && this.ok) {
                    Response.prototype.json = json;
                    return Promise.reject(new DOMException('Lost response body', 'AbortError'));
                }
                return json.apply(this, args);
            };
        }""")
    else:
        page.route("**/api/runs", incomplete_acknowledgement, times=1)
    page.get_by_label("Message Hermes").fill("Recover this incomplete acknowledgement")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("button", name="Retry submission")).to_be_visible()
    expect(page.get_by_label("Message Hermes")).to_have_value(
        "Recover this incomplete acknowledgement"
    )
    page.get_by_role("button", name="Retry submission").click()
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    expect(page.get_by_label("Message Hermes")).to_have_value("")
    assert len(live_app[1].runs) == 1


def test_api_preserves_body_abort_and_http_error_status(page):
    result = page.evaluate("""async () => {
        const {api} = await import('/static/api.js');
        const fetch = window.fetch;
        try {
            window.fetch = async () => ({
                ok: true, status: 200,
                json: async () => { throw new DOMException('Cancelled', 'AbortError'); },
            });
            const aborted = await api('/qa-abort').catch(e => e.name);
            window.fetch = async () => new Response('not JSON', {status: 503});
            const failed = await api('/qa-unavailable').catch(e => e.status);
            window.fetch = async () => new Response(null, {status: 204});
            const empty = await api('/qa-empty');
            return {aborted, failed, empty};
        } finally {
            window.fetch = fetch;
        }
    }""")
    assert result == {"aborted": "AbortError", "failed": 503, "empty": {}}


def test_history_refresh_cannot_overwrite_a_reopened_session(page):
    result = page.evaluate("""async () => {
        const store = await import('/static/store.js');
        const fetch = window.fetch;
        const requests = [];
        window.fetch = (url, options) => String(url).includes('/qa-history/messages')
            ? new Promise(resolve => requests.push(resolve))
            : fetch(url, options);
        try {
            store.update({active:'qa-history', history:[], loading:false});
            const stale = store.refreshHistory('qa-history');
            store.newConversation();
            const reopened = store.openSession('qa-history');
            requests[1](Response.json({
                data:[{id:20, role:'user', content:'Current history'}], next_offset:1,
            }));
            await reopened;
            requests[0](Response.json({
                data:[{id:10, role:'user', content:'Outdated history'}], next_offset:50,
            }));
            await stale;
            return {ids:store.state.history.map(m => m.id), offset:store.state.historyOffset};
        } finally {
            window.fetch = fetch;
        }
    }""")
    assert result == {"ids": [20], "offset": 1}


def test_history_refresh_keeps_the_latest_successful_snapshot(page):
    result = page.evaluate("""async () => {
        const store = await import('/static/store.js');
        const fetch = window.fetch;
        const requests = [];
        window.fetch = (url, options) => String(url).includes('/qa-order/messages')
            ? new Promise(resolve => requests.push(resolve))
            : fetch(url, options);
        try {
            store.update({active:'qa-order', history:[], loading:false});
            const earlier = store.refreshHistory('qa-order');
            const latest = store.refreshHistory('qa-order');
            requests[1](Response.json({data:[{id:2, role:'user', content:'Latest'}]}));
            await latest;
            requests[0](Response.json({data:[{id:1, role:'user', content:'Earlier'}]}));
            await earlier;
            return store.state.history.map(m => m.id);
        } finally {
            window.fetch = fetch;
        }
    }""")
    assert result == [2]


def test_concurrent_older_pages_share_one_request_and_deduplicate_overlap(page):
    result = page.evaluate("""async () => {
        const store = await import('/static/store.js');
        const fetch = window.fetch;
        const requests = [];
        window.fetch = (url, options) => String(url).includes('/qa-older/messages')
            ? new Promise(resolve => requests.push(resolve))
            : fetch(url, options);
        try {
            store.update({
                active:'qa-older', history:[{id:3, role:'user', content:'Newest'}],
                loading:false, historyHasMore:true, historyOffset:1,
            });
            const first = store.loadOlderMessages();
            const second = store.loadOlderMessages();
            const count = requests.length;
            for (const resolve of requests) resolve(Response.json({
                data:[{id:2, role:'user', content:'Older'}, {id:3, role:'user', content:'Newest'}],
                next_offset:3,
            }));
            await Promise.all([first, second]);
            return {
                count, ids:store.state.history.map(m => m.id), offset:store.state.historyOffset,
            };
        } finally {
            window.fetch = fetch;
        }
    }""")
    assert result == {"count": 1, "ids": [2, 3], "offset": 3}


def test_older_page_cannot_prepend_into_a_reopened_session(page):
    result = page.evaluate("""async () => {
        const store = await import('/static/store.js');
        const fetch = window.fetch;
        const requests = [];
        window.fetch = (url, options) => String(url).includes('/qa-older/messages')
            ? new Promise(resolve => requests.push(resolve))
            : fetch(url, options);
        try {
            store.update({
                active:'qa-older', history:[{id:2, role:'user', content:'Previous'}],
                loading:false, historyHasMore:true, historyOffset:1,
            });
            const older = store.loadOlderMessages();
            const reopened = store.openSession('qa-older');
            requests[1](Response.json({data:[{id:9, role:'user', content:'Reopened'}]}));
            await reopened;
            requests[0](Response.json({data:[{id:1, role:'user', content:'Stale'}]}));
            await older;
            return store.state.history.map(m => m.id);
        } finally {
            window.fetch = fetch;
        }
    }""")
    assert result == [9]


def test_session_refresh_supersedes_stale_pagination(page):
    result = page.evaluate("""async () => {
        const store = await import('/static/store.js');
        const fetch = window.fetch;
        const requests = [];
        window.fetch = (url, options) => String(url).startsWith('/api/sessions?offset=')
            ? new Promise(resolve => requests.push(resolve))
            : fetch(url, options);
        try {
            store.update({sessions:[{id:'before'}], sessionsOffset:100, hasMore:true});
            const more = store.refreshSessions(true);
            const alsoMore = store.refreshSessions(true);
            const refresh = store.refreshSessions();
            requests.at(-1)(Response.json({data:[{id:'current'}], limit:100, has_more:false}));
            await refresh;
            for (const resolve of requests.slice(0, -1))
                resolve(Response.json({data:[{id:'stale'}], limit:100, has_more:true}));
            await Promise.all([more, alsoMore]);
            return {
                requests:requests.length, ids:store.state.sessions.map(s => s.id),
                offset:store.state.sessionsOffset, more:store.state.hasMore,
            };
        } finally {
            window.fetch = fetch;
        }
    }""")
    assert result == {"requests": 2, "ids": ["current"], "offset": 100, "more": False}


def test_session_pagination_waits_for_an_inflight_refresh(page):
    result = page.evaluate("""async () => {
        const store = await import('/static/store.js');
        const fetch = window.fetch;
        const requests = [];
        window.fetch = (url, options) => String(url).startsWith('/api/sessions?offset=')
            ? new Promise(resolve => requests.push({url, resolve}))
            : fetch(url, options);
        try {
            const refresh = store.refreshSessions();
            const more = store.refreshSessions(true);
            requests[0].resolve(Response.json({data:[{id:'first'}], limit:100, has_more:true}));
            await refresh;
            if (requests.length > 1)
                requests[1].resolve(Response.json({data:[{id:'second'}], has_more:false}));
            await more;
            return {
                requests:requests.map(r => r.url), ids:store.state.sessions.map(s => s.id),
            };
        } finally {
            window.fetch = fetch;
        }
    }""")
    assert result == {
        "requests": ["/api/sessions?offset=0", "/api/sessions?offset=100"],
        "ids": ["first", "second"],
    }


def test_replaced_event_sources_cannot_mutate_the_current_run(page):
    result = page.evaluate("""async () => {
        const store = await import('/static/store.js');
        const runs = await import('/static/runs.js');
        const EventSource = window.EventSource;
        const sources = [];
        window.EventSource = class {
            constructor() { sources.push(this); }
            close() { this.closed = true; }
        };
        try {
            store.update({lives:{'qa-stream':{id:'run-1', status:'running', text:'', tools:[]}}});
            runs.subscribe('qa-stream');
            runs.subscribe('qa-stream');
            const [stale, current] = sources;
            current.onerror();
            stale.onopen();
            stale.onmessage({data:JSON.stringify({event:'message.delta', delta:'stale'})});
            current.onmessage({data:'null'});
            current.onmessage({data:JSON.stringify({event:'message.delta', delta:'current'})});
            const live = store.state.lives['qa-stream'];
            return {text:live.text, reconnecting:live.reconnecting, closed:stale.closed};
        } finally {
            store.update({lives:{'qa-stream':{status:'completed'}}});
            runs.clearCompletedRun('qa-stream');
            window.EventSource = EventSource;
        }
    }""")
    assert result == {"text": "current", "reconnecting": True, "closed": True}


def test_tool_completion_uses_call_id_and_deltas_do_not_copy_tool_history(page):
    result = page.evaluate("""async () => {
        const {applyEvent} = await import('/static/runs.js');
        const original = {
            text:'', tools:[
                {id:'first', name:'terminal', kind:'tool', status:'running'},
                {id:'second', name:'terminal', kind:'tool', status:'running'},
            ],
        };
        const completed = applyEvent(original, {
            event:'tool.completed', tool:'terminal', tool_call_id:'second', duration:1,
        });
        const delta = applyEvent(completed, {event:'message.delta', delta:'Hello'});
        const terminal = applyEvent({...delta, reconnecting:true}, {event:'run.completed'});
        return {
            original:original.tools.map(t => t.status),
            completed:completed.tools.map(t => t.status),
            sameTools:delta.tools === completed.tools,
            reconnecting:terminal.reconnecting,
        };
    }""")
    assert result == {
        "original": ["running", "running"],
        "completed": ["running", "completed"],
        "sameTools": True,
        "reconnecting": False,
    }


def test_malformed_saved_run_state_does_not_crash_or_request_undefined_runs(page):
    result = page.evaluate("""async () => {
        const {restoreRuns} = await import('/static/runs.js');
        const {writeStorage} = await import('/static/lib.js');
        const fetch = window.fetch;
        const requests = [];
        window.fetch = (url, options) => {
            if (String(url).includes('/api/runs/')) requests.push(String(url));
            return fetch(url, options);
        };
        try {
            for (const saved of ['null', '[]', '{"broken":null,"missing":{}}']) {
                writeStorage('runs', saved);
                await restoreRuns();
            }
            return requests;
        } finally {
            window.fetch = fetch;
        }
    }""")
    assert result == []


def test_run_restoration_preserves_receipts_when_offline(page):
    result = page.evaluate("""async () => {
        const {restoreRuns} = await import('/static/runs.js');
        const {state} = await import('/static/store.js');
        const {readStorage, writeStorage} = await import('/static/lib.js');
        const fetch = window.fetch;
        writeStorage('runs', JSON.stringify({
            'qa-offline':{id:'saved-run', requestId:'request-1', userText:'Original input'},
        }));
        window.fetch = (url, options) => String(url).endsWith('/api/runs/saved-run')
            ? Promise.reject(new TypeError('Offline'))
            : fetch(url, options);
        try {
            await restoreRuns();
            const live = state.lives['qa-offline'];
            return {
                id:live?.id, uncertain:live?.uncertain,
                stored:JSON.parse(readStorage('runs'))['qa-offline']?.id,
            };
        } finally {
            window.fetch = fetch;
        }
    }""")
    assert result == {"id": "saved-run", "uncertain": True, "stored": "saved-run"}


def test_run_restoration_cannot_overwrite_a_new_submission(page):
    result = page.evaluate("""async () => {
        const {restoreRuns} = await import('/static/runs.js');
        const {state, update} = await import('/static/store.js');
        const {writeStorage} = await import('/static/lib.js');
        const fetch = window.fetch;
        let resolve;
        writeStorage('runs', JSON.stringify({'qa-restore':{id:'old-run', userText:'Old'}}));
        window.fetch = (url, options) => String(url).endsWith('/api/runs/old-run')
            ? new Promise(done => { resolve = done; })
            : fetch(url, options);
        try {
            const restore = restoreRuns();
            update({lives:{'qa-restore':{id:'new-run', status:'running', text:'', tools:[]}}});
            resolve(Response.json({status:'completed', output:'Old result'}));
            await restore;
            return state.lives['qa-restore'].id;
        } finally {
            window.fetch = fetch;
        }
    }""")
    assert result == "new-run"


def test_pending_run_restoration_preserves_drafts_and_explains_failed_recovery(page, live_app):
    sid = seed(live_app[1], "qa-pending-recovery")
    page.evaluate(
        "async sid => (await import('/static/store.js')).openSession(sid)",
        sid,
    )
    draft = "Keep this draft until the previous response is checked"
    page.get_by_label("Message Hermes").fill(draft)
    page.evaluate(
        """async sid => {
            const {restoreRuns} = await import('/static/runs.js');
            const {writeStorage} = await import('/static/lib.js');
            const fetch = window.fetch;
            writeStorage('runs', JSON.stringify({
                [sid]:{id:'qa-pending-run', requestId:'qa-receipt', userText:'Message 000'},
            }));
            window.fetch = (url, options) => String(url).endsWith('/api/runs/qa-pending-run')
                ? new Promise(resolve => {
                    window.qaFailRecovery = () => resolve(Response.json({}, {status:503}));
                })
                : fetch(url, options);
            window.qaRecovery = restoreRuns().finally(() => { window.fetch = fetch; });
        }""",
        sid,
    )
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_text("This session is reconnecting.", exact=False)).to_be_visible()
    expect(page.get_by_label("Message Hermes")).to_have_value(draft)
    assert len(live_app[1].runs) == 0

    page.evaluate("async () => { window.qaFailRecovery(); await window.qaRecovery; }")
    expect(page.locator(".run-error")).to_contain_text("Reload to reconnect")
    expect(page.get_by_label("Message Hermes")).to_have_value(draft)
    expect(page.get_by_role("button", name="Send message", exact=True)).to_be_disabled()
    page.route(
        "**/api/runs/qa-pending-run",
        lambda route: route.fulfill(json={"status": "completed", "output": ""}),
    )
    page.reload()
    expect(page.get_by_label("Message Hermes")).to_have_value(draft)
    expect(page.get_by_role("button", name="Send message", exact=True)).to_be_enabled()
    assert len(live_app[1].runs) == 0


def test_restored_active_runs_do_not_duplicate_the_status_snapshot(page):
    result = page.evaluate("""async () => {
        const runs = await import('/static/runs.js');
        const {state, update} = await import('/static/store.js');
        const {writeStorage} = await import('/static/lib.js');
        const fetch = window.fetch;
        const EventSource = window.EventSource;
        let source;
        window.EventSource = class {
            constructor() { source = this; }
            close() {}
        };
        writeStorage('runs', JSON.stringify({'qa-replay':{id:'replay-run', userText:'Input'}}));
        window.fetch = (url, options) => String(url).endsWith('/api/runs/replay-run')
            ? Promise.resolve(Response.json({status:'running', output:'First'}))
            : fetch(url, options);
        try {
            await runs.restoreRuns();
            for (const delta of ['First', ' second'])
                source.onmessage({data:JSON.stringify({event:'message.delta', delta})});
            return state.lives['qa-replay'].text;
        } finally {
            update({lives:{'qa-replay':{status:'completed'}}});
            runs.clearCompletedRun('qa-replay');
            window.fetch = fetch;
            window.EventSource = EventSource;
        }
    }""")
    assert result == "First second"


def test_restored_failed_runs_retain_their_diagnostic_and_pending_guidance(page):
    result = page.evaluate("""async () => {
        const {restoreRuns} = await import('/static/runs.js');
        const {state} = await import('/static/store.js');
        const {writeStorage} = await import('/static/lib.js');
        const fetch = window.fetch;
        writeStorage('runs', JSON.stringify({'qa-failed':{id:'failed-run', userText:'Input'}}));
        window.fetch = (url, options) => String(url).endsWith('/api/runs/failed-run')
            ? Promise.resolve(Response.json({
                status:'failed', error:'The provider could not finish this response.',
                usage:{input_tokens:10}, pending_steer:'Preserved guidance',
                approval:{request_id:'stale-approval'},
            }))
            : fetch(url, options);
        try {
            await restoreRuns();
            const live = state.lives['qa-failed'];
            return {
                error:live.error, usage:live.usage, pending:live.pendingSteer,
                approval:live.approval,
            };
        } finally {
            window.fetch = fetch;
        }
    }""")
    assert result == {
        "error": "The provider could not finish this response.",
        "usage": {"input_tokens": 10},
        "pending": "Preserved guidance",
        "approval": None,
    }


@pytest.mark.parametrize(
    ("status", "saved_assistant"),
    [("failed", None), ("failed", "A saved planning step"), ("completed", "A saved planning step")],
)
def test_recovered_output_requires_the_current_turns_matching_assistant(
    page, live_app, status, saved_assistant
):
    sid = seed(live_app[1], "qa-current-turn")
    live_app[1].messages[sid] = [
        {"id": 1, "role": "user", "content": "First question"},
        {"id": 2, "role": "assistant", "content": "Old answer"},
        {"id": 3, "role": "user", "content": "Second question"},
    ]
    if saved_assistant:
        live_app[1].messages[sid].append({"id": 4, "role": "assistant", "content": saved_assistant})
    output = "Recovered response that must remain visible"
    page.route(
        "**/api/runs/qa-current-run",
        lambda route: route.fulfill(json={"status": status, "output": output}),
    )
    persisted = page.evaluate(
        """async sid => {
            const {restoreRuns} = await import('/static/runs.js');
            const {state, openSession} = await import('/static/store.js');
            const {writeStorage} = await import('/static/lib.js');
            await openSession(sid);
            writeStorage('runs', JSON.stringify({
                [sid]:{id:'qa-current-run', userText:'Second question'},
            }));
            await restoreRuns();
            return !!state.lives[sid].persisted;
        }""",
        sid,
    )
    assert persisted is False
    expect(page.get_by_text(output, exact=True)).to_be_visible()


def test_settled_run_keeps_history_pagination_consistent(page, live_app):
    sid = seed(live_app[1], "qa-pagination", count=130)
    page.evaluate(
        """async sid => {
            const store = await import('/static/store.js');
            await store.openSession(sid);
            await store.loadOlderMessages();
        }""",
        sid,
    )
    page.get_by_label("Message Hermes").fill("Continue the paginated history")
    page.get_by_role("button", name="Send message", exact=True).click()
    state = page.evaluate_handle("async () => (await import('/static/store.js')).state")
    page.wait_for_function("state => state.lives[state.active]?.persisted === true", arg=state)
    state.dispose()
    result = page.evaluate("""async () => {
        const store = await import('/static/store.js');
        const pageSize = store.state.history.length;
        const offset = store.state.historyOffset;
        await store.loadOlderMessages();
        return {pageSize, offset, total:store.state.history.length};
    }""")
    assert result == {"pageSize": 132, "offset": 132, "total": 132}


def test_cached_images_are_read_in_one_transaction(page):
    result = page.evaluate(
        """async url => {
            const cache = await import('/static/attachments.js');
            for (const id of [1, 2])
                await cache.cacheMessageImages('qa-cache', id, [{url, name:'Image'}]);
            const transaction = IDBDatabase.prototype.transaction;
            let transactions = 0;
            IDBDatabase.prototype.transaction = function (...args) {
                transactions++;
                return transaction.apply(this, args);
            };
            try {
                const images = await cache.browserAttachments('qa-cache');
                return {transactions, ids:images.map(record => record.message_id)};
            } finally {
                IDBDatabase.prototype.transaction = transaction;
            }
        }""",
        IMAGE,
    )
    assert result == {"transactions": 1, "ids": [1, 2]}


def test_attachment_database_can_be_deleted_and_reopened(page):
    result = page.evaluate("""async () => {
        const {pendingStorage} = await import('/static/attachments.js');
        const {databaseName} = await import('/static/profile-context.js');
        await pendingStorage('qa-old', ['old']);
        const deleted = await new Promise(resolve => {
            const request = indexedDB.deleteDatabase(databaseName);
            request.onsuccess = () => resolve(true);
            request.onblocked = request.onerror = () => resolve(false);
        });
        if (!deleted) return {deleted};
        const old = await pendingStorage('qa-old');
        await pendingStorage('qa-new', ['new']);
        return {deleted, old:old ?? null, current:await pendingStorage('qa-new')};
    }""")
    assert result == {"deleted": True, "old": None, "current": ["new"]}


def test_oversized_image_cache_entry_keeps_its_receipt_and_existing_cache(page):
    result = page.evaluate(
        """async url => {
            const cache = await import('/static/attachments.js');
            await cache.cacheMessageImages('qa-size', 1, [{url, name:'Existing'}]);
            const receipt = 'run.qa-size.request';
            await cache.pendingStorage(receipt, {payload:{session_id:'qa-size'}, images:[{url}]});
            let rejected = false;
            try {
                await cache.cacheMessageImages(
                    'qa-size', 2, [{url:'x'.repeat(33*1024*1024)}], receipt,
                );
            } catch {
                rejected = true;
            }
            return {
                rejected,
                receipt:!!await cache.pendingStorage(receipt),
                ids:(await cache.pendingStorage('image-index')).map(record => record.messageId),
                orphan:!!await cache.pendingStorage('image.qa-size.2'),
            };
        }""",
        IMAGE,
    )
    assert result == {"rejected": True, "receipt": True, "ids": [1], "orphan": False}


def test_forgetting_images_does_not_delete_another_sessions_receipts(page):
    result = page.evaluate(
        """async url => {
            const cache = await import('/static/attachments.js');
            for (const sid of ['qa', 'qa.child']) {
                await cache.cacheMessageImages(sid, 1, [{url}]);
                await cache.pendingStorage('run.' + sid + '.request', {payload:{session_id:sid}});
            }
            await cache.pendingStorage('run.qa', {payload:{session_id:'qa'}});
            await cache.forgetImages('qa');
            return {
                legacy:!!await cache.pendingStorage('run.qa'),
                own:!!await cache.pendingStorage('run.qa.request'),
                other:!!await cache.pendingStorage('run.qa.child.request'),
                ownImages:(await cache.browserAttachments('qa')).length,
                otherImages:(await cache.browserAttachments('qa.child')).length,
            };
        }""",
        IMAGE,
    )
    assert result == {
        "legacy": False,
        "own": False,
        "other": True,
        "ownImages": 0,
        "otherImages": 1,
    }


def test_image_placeholder_suffix_keeps_literal_markers_inside_text(page):
    result = page.evaluate("""async () => {
        const {placeholderCount, withoutImagePlaceholders} =
            await import('/static/attachments.js');
        return [
            'Question\\n[screenshot]\\n[screenshot]\\n',
            'A literal [screenshot] inside text\\n',
            '[screenshot][screenshot]',
            '[screenshot]\\n'.repeat(10000) + 'More text',
        ].map(text => ({count:placeholderCount(text), text:withoutImagePlaceholders(text)}));
    }""")
    assert result[0] == {"count": 2, "text": "Question"}
    assert result[1] == {"count": 0, "text": "A literal [screenshot] inside text"}
    assert result[2] == {"count": 2, "text": ""}
    assert result[3]["count"] == 0 and result[3]["text"].endswith("More text")


def test_failed_thumbnail_recovers_when_the_image_changes(page):
    result = page.evaluate(
        """async url => {
            const {render} = await import('/static/lib.js');
            const {Images} = await import('/static/images.js');
            const host = document.createElement('div');
            document.body.appendChild(host);
            const {h} = await import('/static/vendor/preact.js');
            try {
                render(h(Images, {images:[
                    {id:'same', name:'Preview', url:'data:image/png;base64,AAAA'},
                ]}), host);
                host.querySelector('img').dispatchEvent(new Event('error'));
                await new Promise(requestAnimationFrame);
                const failed = !!host.querySelector('.image-unavailable');
                render(h(Images, {images:[{id:'same', name:'Preview', url}]}), host);
                await new Promise(requestAnimationFrame);
                return {failed, restored:host.querySelector('img')?.getAttribute('src') === url};
            } finally {
                render(null, host);
                host.remove();
            }
        }""",
        IMAGE,
    )
    assert result == {"failed": True, "restored": True}

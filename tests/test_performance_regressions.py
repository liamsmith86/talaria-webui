import gzip
import socket

import httpx
import pytest
from playwright.sync_api import expect

from talaria import server as talaria_server
from talaria.app import create_app
from talaria.auth import hash_password
from talaria.config import Settings

from .conftest import browser_context, serve
from .fake_hermes import KEY
from .test_app import signed_in


def test_static_compression_revalidation_and_uncompressed_event_streams(live_app):
    url = live_app[0]
    with httpx.Client(base_url=url) as client:
        plain = client.get("/static/vendor/purify.js", headers={"Accept-Encoding": "identity"})
        with client.stream(
            "GET", "/static/vendor/purify.js", headers={"Accept-Encoding": "gzip"}
        ) as response:
            compressed = b"".join(response.iter_raw())
            assert response.headers["content-encoding"] == "gzip"
            assert response.headers["cache-control"] == "no-cache"
            assert "Accept-Encoding" in response.headers["vary"]
            etag = response.headers["etag"]
        assert gzip.decompress(compressed) == plain.content
        assert len(compressed) < len(plain.content) / 2
        assert (
            client.get("/static/vendor/purify.js", headers={"If-None-Match": etag}).status_code
            == 304
        )
    with signed_in(url) as client:
        sid = client.post("/api/sessions", json={"title": "Compression"}).json()["id"]
        rid = client.post("/api/runs", json={"session_id": sid, "input": "Hello"}).json()["run_id"]
        with client.stream(
            "GET", f"/api/runs/{rid}/events", headers={"Accept-Encoding": "gzip"}
        ) as response:
            assert "content-encoding" not in response.headers
            assert response.headers["content-type"].startswith("text/event-stream")
            first = next(line for line in response.iter_lines() if line.startswith("data:"))
            assert first


def test_a_memoized_view_skips_renders_but_keeps_its_own_state(module_page):
    # A view rendered inline would put its hooks on the memo, whose props check then
    # discards each state update: an edit box in a saved message would stop responding.
    result = module_page.evaluate("""async () => {
      const {html, memo, render, useState} = await import('/static/lib.js');
      const root = document.createElement('div');
      document.body.append(root);
      const settle = () => new Promise((done) => requestAnimationFrame(() => setTimeout(done)));
      let renders = 0, bump;
      const View = memo(({label}) => {
        renders++;
        const [count, setCount] = useState(0);
        bump = () => setCount((value) => value + 1);
        return html`<output>${label} ${count}</output>`;
      });
      try {
        render(html`<${View} label="first" />`, root);
        render(html`<${View} label="first" />`, root);
        const skipped = renders;
        bump();
        await settle();
        const updated = root.textContent;
        render(html`<${View} label="second" />`, root);
        return {skipped, updated, changed: root.textContent};
      } finally {
        render(null, root);
        root.remove();
      }
    }""")
    assert result == {"skipped": 1, "updated": "first 1", "changed": "second 1"}


def test_templates_do_not_keep_the_vnodes_of_static_elements(module_page):
    # A VNode kept for the life of the page points at the tree it first rendered in, so
    # one static element would hold a finished conversation and all of its DOM.
    assert module_page.evaluate("""async () => {
      const {html} = await import('/static/lib.js');
      const view = (title) => html`<div title=${title}><span /></div>`;
      return view('first').props.children !== view('second').props.children;
    }""")


def test_reading_a_transcript_compresses_it(live_app):
    url, peer = live_app[0], live_app[1]
    peer.sessions["long"] = {"id": "long", "title": "Long", "source": "api_server"}
    prose = " ".join(f"The {index} plan keeps the relay honest." for index in range(40))
    peer.messages["long"] = [
        {"id": index + 1, "role": "assistant", "content": prose} for index in range(60)
    ]
    with (
        signed_in(url) as client,
        client.stream(
            "GET", "/api/sessions/long/messages", headers={"Accept-Encoding": "gzip"}
        ) as response,
    ):
        sent = b"".join(response.iter_raw())
        assert response.headers["content-encoding"] == "gzip"
        assert "Accept-Encoding" in response.headers["vary"]
        assert response.headers["cache-control"] == "no-store"
    assert len(gzip.decompress(sent)) > 3 * len(sent)


def test_restoration_prioritizes_visible_run_with_bounded_concurrency(module_page):
    module_page.evaluate("""async () => {
      const {restoreRuns} = await import('/static/runs.js');
      const {update} = await import('/static/store.js');
      const {writeStorage} = await import('/static/lib.js');
      const originalFetch=window.fetch, originalSource=window.EventSource;
      const saved={};
      for(let i=0;i<8;i++) saved['restore-'+i]={id:'run-'+i, requestId:'request-'+i};
      writeStorage('runs',JSON.stringify(saved));
      update({active:'restore-7',lives:{}});
      const check=window.restoreCheck={started:[],release:{},automatic:false,pending:0,peak:0};
      window.EventSource=class {close(){}};
      window.fetch=async (url,options) => {
        if(!String(url).includes('/api/runs/')) return originalFetch(url,options);
        const id=String(url).split('/').at(-1);
        check.started.push(id); check.pending++; check.peak=Math.max(check.peak,check.pending);
        if(!check.automatic) await new Promise(resolve=>check.release[id]=resolve);
        check.pending--;
        return new Response(JSON.stringify({status:'running'}));
      };
      check.done=restoreRuns().finally(()=>{
        window.fetch=originalFetch; window.EventSource=originalSource;
      });
    }""")
    module_page.wait_for_function("() => restoreCheck.started.length === 4")
    assert module_page.evaluate("restoreCheck.started[0]") == "run-7"
    module_page.evaluate("restoreCheck.release['run-7']()")
    module_page.wait_for_function("() => restoreCheck.started.length === 5")
    module_page.evaluate("""async () => {
      restoreCheck.automatic=true;
      Object.values(restoreCheck.release).forEach(release=>release());
      await restoreCheck.done;
    }""")
    assert module_page.evaluate("restoreCheck.peak") == 4
    assert module_page.evaluate("restoreCheck.started.length") == 8


def test_mobile_network_reconnect_replays_without_duplicate_messages(page, live_app):
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_label("Message Hermes").fill("Hold a mobile reconnect")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("heading", name="A thoughtful place to start")).to_be_visible()
    page.context.set_offline(True)
    page.wait_for_timeout(250)  # Fixed wait: only the length of the outage.
    page.context.set_offline(False)
    live_app[1].release()
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible(
        timeout=20000
    )
    expect(page.get_by_role("button", name="Stop response")).to_have_count(0)
    expect(page.get_by_role("heading", name="A thoughtful place to start")).to_have_count(1)
    expect(page.locator(".message.user .message-text")).to_have_text(["Hold a mobile reconnect"])
    assert len(live_app[1].runs) == 1


def test_historical_code_highlights_on_approach_and_find_still_reaches_it(page):
    page.evaluate("""async () => {
      const {update}=await import('/static/store.js');
      update({active:'history',history:Array.from({length:100},(_,i)=>[
        {id:i*2+1,role:'user',content:'Example '+i},
        {id:i*2+2,role:'assistant',content:'```javascript\\nconst entry'+i+' = 42;\\n```'},
      ]).flat(),sessions:[{id:'history',title:'History'}],loading:false});
    }""")
    last = page.locator(".message").last
    expect(last.locator(".token").first).to_be_attached()
    first = page.locator(".message.assistant").first
    assert "entry0" in first.locator("code").text_content()
    page.get_by_role("button", name="Find in session", exact=True).click()
    page.get_by_label("Find text in session").fill("entry0")
    expect(first.locator(".token").first).to_be_attached()
    expect(first.locator("code")).to_be_in_viewport()


def test_saved_message_parses_once_and_only_code_waits_for_approach(page):
    result = page.evaluate("""async () => {
      const {update}=await import('/static/store.js');
      const {marked}=await import('/static/vendor/marked.js');
      const proto=IntersectionObserver.prototype,observe=proto.observe,parse=marked.parse;
      let watched=0,parses=0;
      proto.observe=function(target){
        if(target.matches('.markdown'))watched++;
        return observe.call(this,target);
      };
      marked.parse=(...args)=>{parses++;return parse(...args)};
      try {
        update({active:'parsed',sessions:[{id:'parsed',title:'Parsed'}],loading:false,history:[
          {id:1,role:'assistant',content:'```javascript\\nconst oldest = 42;\\n```'},
          ...Array.from({length:60},(_,i)=>({id:i+2,role:'assistant',content:'Plain reply '+i})),
        ]});
        await new Promise(resolve=>setTimeout(resolve,200));
        const counts={watched,parses};
        document.querySelector('.message').scrollIntoView();
        while(!document.querySelector('.message .token'))
          await new Promise(resolve=>setTimeout(resolve,20));
        return {...counts,reparsed:parses-counts.parses};
      } finally {
        proto.observe=observe;
        marked.parse=parse;
      }
    }""")
    assert result == {"watched": 1, "parses": 61, "reparsed": 0}


@pytest.mark.parametrize("payload_size", [50000, 1000000])
def test_completed_response_cache_is_bounded_without_losing_recovery_state(page, payload_size):
    page.evaluate(
        """async (payloadSize) => {
      const {update}=await import('/static/store.js');
      const lives={};
      for(let i=0;i<100;i++) lives['cached-'+i]={id:'old-'+i,status:'completed',
        persisted:true,text:'x'.repeat(payloadSize),tools:[]};
      lives.working={id:'working',status:'running',text:'',tools:[]};
      lives.uncertain={id:'uncertain',status:'interrupted',uncertain:true,text:'',tools:[]};
      lives.images={id:'images',status:'completed',persisted:true,imageReceipt:true,text:'',tools:[]};
      lives.unpersisted={id:'unpersisted',status:'completed',text:'Unsaved reply',tools:[]};
      update({lives});
    }""",
        payload_size,
    )
    page.get_by_label("Message Hermes").fill("Trim only disposable presentation state")
    page.get_by_role("button", name="Send message", exact=True).click()
    state = page.evaluate_handle("async () => (await import('/static/store.js')).state")
    page.wait_for_function("state => !!state.lives[state.active]?.persisted", arg=state)
    state.dispose()
    result = page.evaluate("""async () => {
      const {state}=await import('/static/store.js');
      const cached=Object.entries(state.lives).filter(([id])=>id.startsWith('cached-'));
      return {cached:cached.length, bytes:cached.reduce((n,[,v])=>n+v.text.length,0),
        protected:['working','uncertain','images','unpersisted',state.active]
          .every(id=>Object.hasOwn(state.lives,id))};
    }""")
    count = min(16, (8 * 1024 * 1024) // (payload_size * 2))
    assert result == {"cached": count, "bytes": count * payload_size, "protected": True}


def test_opening_a_session_runs_one_entrance_and_a_reply_runs_its_own(page):
    result = page.evaluate("""async () => {
      const {update}=await import('/static/store.js');
      const nextFrame=()=>new Promise(requestAnimationFrame);
      update({active:'opening',loading:false,caps:{features:{}},sessions:[],lives:{},
        history:Array.from({length:40},(_,i)=>({id:i+1,role:i%2?'assistant':'user',
          content:'Saved message '+i}))});
      await nextFrame();
      const opened=[...document.querySelectorAll('.message')]
        .filter(message=>message.getAnimations().length).length;
      update({lives:{opening:{id:'run',requestId:'request',status:'running',text:'Reply',tools:[],
        baseHistoryLength:40,baseMessageId:40,createdAt:Date.now()}}});
      await nextFrame();
      return {opened,
        reply:document.querySelector('.message.live')?.getAnimations()
          .map(animation=>animation.animationName)};
    }""")
    assert result == {"opened": 0, "reply": ["arrive"]}


def test_session_opens_at_its_latest_message_in_its_first_frame(page):
    distance = page.evaluate("""async () => {
      const {update}=await import('/static/store.js');
      update({active:'opening',loading:false,caps:{features:{}},sessions:[],lives:{},
        history:Array.from({length:40},(_,i)=>({id:i+1,role:i%2?'assistant':'user',
          content:('Saved message '+i+'. ').repeat(40)}))});
      await new Promise(requestAnimationFrame);
      const el=document.querySelector('.conversation-viewport');
      return el.scrollHeight-el.scrollTop-el.clientHeight;
    }""")
    assert distance < 3


def test_offscreen_messages_skip_rendering_and_keep_their_place(page):
    result = page.evaluate("""async () => {
      const {update}=await import('/static/store.js');
      const nextFrame=()=>new Promise(requestAnimationFrame);
      update({active:'long',loading:false,caps:{features:{}},sessions:[],lives:{},
        history:Array.from({length:60},(_,i)=>({id:i+1,role:i%2?'assistant':'user',
          content:('Paragraph '+i+'. ').repeat(i%2?150:1)}))});
      await nextFrame();
      await Promise.allSettled(document.querySelector('.conversation-content')
        .getAnimations({subtree:true}).map(animation=>animation.finished));
      await nextFrame();
      await nextFrame();
      const messages=[...document.querySelectorAll('.message')];
      const rendered=messages.filter(message=>message.firstElementChild
        .checkVisibility({contentVisibilityAuto:true})).length;
      // Scrolling up replaces estimates with real heights, which mustn't move what's on screen.
      const viewport=document.querySelector('.conversation-viewport');
      const box=viewport.getBoundingClientRect();
      const middle=(box.top+box.bottom)/2;
      let drift=0;
      while(viewport.scrollTop>400) {
        const anchor=messages.find(message=>message.getBoundingClientRect().bottom>=middle);
        const before=anchor.getBoundingClientRect().top;
        viewport.scrollTop-=400;
        await nextFrame();
        await nextFrame();
        drift=Math.max(drift,Math.abs(anchor.getBoundingClientRect().top-before-400));
      }
      return {rendered,drift,total:messages.length,
        contained:getComputedStyle(messages[0]).contentVisibility==='auto'};
    }""")
    assert result["total"] == 60
    # A browser that can't hold a reader's place through a height change renders everything.
    assert result["rendered"] < 20 if result["contained"] else result["rendered"] == 60
    assert result["drift"] < 2


def test_text_deltas_reuse_sidebar_rows_and_tool_cards(page):
    result = page.evaluate("""async () => {
      const {state,update}=await import('/static/store.js');
      const {applyEvent}=await import('/static/runs.js');
      let titles=0,tools=0;
      const sessions=Array.from({length:300},(_,i)=>({id:'stable-'+i,source:'api_server',
        get title(){titles++;return 'Stable '+i;}}));
      const cards=Array.from({length:30},(_,i)=>({id:'tool-'+i,status:'running',
        get name(){tools++;return 'terminal';},preview:'A command'}));
      update({active:'stable-0',history:[],loading:false,sessionDetails:null,caps:{features:{}},
        sessions,lives:{'stable-0':{id:'preview',status:'running',text:'Hello',tools:cards}}});
      await new Promise(resolve=>setTimeout(resolve,200));
      titles=tools=0;
      for(let i=0;i<20;i++) {
        const live=applyEvent(state.lives['stable-0'],{event:'message.delta',delta:' more'});
        update({lives:{'stable-0':live}});
        await new Promise(resolve=>setTimeout(resolve,0));
      }
      const reads={titles,tools};
      const live=applyEvent(state.lives['stable-0'],
        {event:'tool.completed',tool_call_id:'tool-0',duration:1});
      update({lives:{'stable-0':{...live,status:'completed'}}});
      return reads;
    }""")
    assert result["titles"] < 100
    assert result["tools"] == 0
    expect(page.locator(".tool-card").first).not_to_have_class("tool-card working")
    expect(page.locator(".session-dot.live")).to_have_count(0)
    page.get_by_label("Search sessions").fill("Stable 12")
    expect(page.locator(".session-row")).to_have_count(11)


def test_find_reindexes_only_changed_dom_and_keeps_highlights_attached(page):
    page.evaluate("""async () => {
      const {update}=await import('/static/store.js');
      update({active:'indexed',loading:false,caps:{features:{}},sessions:[],lives:{},
        history:Array.from({length:100},(_,i)=>({id:i+1,role:'assistant',
          content:'Saved **needle** in reply '+i}))});
    }""")
    page.get_by_role("button", name="Find in session", exact=True).click()
    page.get_by_label("Find text in session").fill("needle")
    expect(page.locator(".find-count")).to_have_text("1 of 100")
    page.evaluate("""() => {
      const original=document.createTreeWalker.bind(document);
      window.findScans=0;
      document.createTreeWalker=(...args)=>{window.findScans++;return original(...args)};
      // Mimic deferred highlighting or a reveal changing nodes without a store update.
      const first=document.querySelector('.message-text p');
      const code=document.createElement('code');code.textContent='another needle';
      first.replaceChildren(document.createTextNode('needle and '),code);
    }""")
    expect(page.locator(".find-count")).to_have_text("1 of 101")
    assert page.evaluate("findScans") == 1
    # Highlights are applied in an effect, after the count above has painted.
    page.wait_for_function("""() => [...CSS.highlights.get('talaria-find')].every(range=>
      range.startContainer.isConnected && range.toString()==='needle')""")
    page.get_by_label("Find text in session").fill("another")
    expect(page.locator(".find-count")).to_have_text("1 of 1")
    assert page.evaluate("findScans") == 1


def test_find_updates_before_a_continuous_stream_stops(page):
    page.evaluate("""async () => {
      const {update}=await import('/static/store.js');
      update({active:'continuous',loading:false,caps:{features:{}},sessions:[],history:[],
        lives:{continuous:{id:'preview',status:'running',text:'Beginning',tools:[]}}});
    }""")
    page.get_by_role("button", name="Find in session", exact=True).click()
    page.get_by_label("Find text in session").fill("needle")
    expect(page.locator(".find-count")).to_have_text("No matches")
    result = page.evaluate("""async () => {
      const {state,update}=await import('/static/store.js');
      let during=false;
      for(let i=0;i<20;i++) {
        const live=state.lives.continuous;
        update({lives:{continuous:{...live,text:live.text+' needle'}}});
        await new Promise(resolve=>setTimeout(resolve,35));
        if(i<15 && /of [1-9]/.test(document.querySelector('.find-count').textContent))
          during=true;
      }
      return during;
    }""")
    assert result
    expect(page.locator(".find-count")).to_have_text("1 of 20")


def test_the_home_screen_does_not_fade_in_for_longer_than_the_rest_of_the_app(page):
    """Chromium reports the largest paint when this fade ends, and a reader waits for it too."""
    assert (
        page.locator(".welcome").evaluate("element => getComputedStyle(element).animationDuration")
        == "0.2s"
    )


def test_typing_does_not_search_the_document_for_open_dialogs(page):
    page.evaluate("""() => {
      const original=document.querySelector.bind(document);
      window.dialogSearches=0;
      document.querySelector=selector=>{
        if(selector==='dialog[open]')window.dialogSearches++;
        return original(selector);
      };
    }""")
    page.get_by_label("Message Hermes").press_sequentially("typing")
    assert page.evaluate("dialogSearches") == 0
    page.keyboard.press("Control+k")
    expect(page.locator(".sidebar .search-field input")).to_be_focused()
    assert page.evaluate("dialogSearches") == 1


def test_startup_history_and_catalog_do_not_wait_for_sidebar_listing(page):
    page.evaluate("""async () => {
      const {connect,update}=await import('/static/store.js');
      const {writeStorage}=await import('/static/lib.js');
      const original=window.fetch;
      const check=window.startupCheck={calls:[],released:false};
      window.fetch=async (url,options)=>{
        const path=new URL(url,location.href).pathname;
        check.calls.push(path);
        const result=value=>Promise.resolve(new Response(JSON.stringify(value)));
        if(path==='/api/capabilities')return result({features:{
          session_resources:true,model_options:true}});
        if(path==='/api/sessions') {
          await new Promise(resolve=>check.release=resolve);
          check.released=true;return result({data:[]});
        }
        if(path==='/api/sessions/startup/messages')return result({data:[{
          id:1,role:'assistant',content:'History arrived before the sidebar.'}]});
        if(path==='/api/sessions/startup')return result({id:'startup',title:'Startup'});
        if(path==='/api/models')return result({providers:[]});
        if(path==='/api/readiness')return result({status:'ok',issues:[]});
        return original(url,options);
      };
      writeStorage('last-session','startup');
      history.replaceState(null,'','?session=startup');update({active:null});
      check.done=connect().finally(()=>{window.fetch=original});
    }""")
    try:
        expect(page.get_by_text("History arrived before the sidebar.", exact=True)).to_be_visible()
        assert page.evaluate("""!startupCheck.released &&
          ['/api/models','/api/readiness'].every(path=>startupCheck.calls.includes(path))""")
    finally:
        page.evaluate("async()=>{startupCheck.release?.();await startupCheck.done}")


def test_every_server_bind_lets_asyncio_disable_nagle(monkeypatch):
    """asyncio sets TCP_NODELAY only on a listener naming TCP. Without it each response stalls
    about 40 ms between headers and body, which production doesn't, so a measurement must not."""

    async def app(scope, receive, send):
        raise AssertionError("Nothing connects")

    server, thread, _ = serve(app)
    try:
        assert server.servers[0].sockets[0].proto == socket.IPPROTO_TCP
    finally:
        server.should_exit = True
        thread.join(6)

    reloading = []

    class Reloader:
        def __init__(self, config, target, sockets):
            reloading.extend(sockets)

        def run(self):
            pass

    monkeypatch.setattr(talaria_server, "ChangeReload", Reloader)
    talaria_server.run(
        "talaria.cli:development_app", factory=True, reload=True, host="127.0.0.1", port=0
    )
    assert reloading[0].proto == socket.IPPROTO_TCP


def test_a_return_visit_asks_the_server_for_no_assets(browser, tmp_path):
    """Each revalidation costs a round trip, and only six run at once on HTTP/1.1."""
    settings = Settings(
        api_key=KEY, password_hash=hash_password("test-password"), signing_key="fixture-key"
    )
    app = create_app(settings, tmp_path / "config.json", immutable_assets=True)
    assets = []

    async def counting(scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/static/"):
            assets.append(scope["path"])
        await app(scope, receive, send)

    server, thread, url = serve(counting)
    try:
        with browser_context(browser) as context:
            page = context.new_page()
            page.goto(url)
            page.get_by_label("Password", exact=True).wait_for()
            first = list(assets)
            assert len(first) > 40
            # A manifest's URL identifies the app, so it isn't versioned. WebKit reads it each load.
            assert all("/static/build-" in path for path in first if "manifest" not in path)
            assets.clear()
            page.goto("about:blank")
            page.goto(url)
            page.get_by_label("Password", exact=True).wait_for()
            assert [path for path in assets if "/static/build-" in path] == []
    finally:
        server.should_exit = True
        thread.join(6)

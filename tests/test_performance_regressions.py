import gzip

import httpx
from playwright.sync_api import expect

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


def test_restoration_prioritizes_visible_run_with_bounded_concurrency(page):
    page.evaluate("""async () => {
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
    page.wait_for_function("() => restoreCheck.started.length === 4")
    assert page.evaluate("restoreCheck.started[0]") == "run-7"
    page.evaluate("restoreCheck.release['run-7']()")
    page.wait_for_function("() => restoreCheck.started.length === 5")
    page.evaluate("""async () => {
      restoreCheck.automatic=true;
      Object.values(restoreCheck.release).forEach(release=>release());
      await restoreCheck.done;
    }""")
    assert page.evaluate("restoreCheck.peak") == 4
    assert page.evaluate("restoreCheck.started.length") == 8


def test_mobile_network_reconnect_replays_without_duplicate_messages(page, live_app):
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_label("Message Hermes").fill("A slow mobile reconnect")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("heading", name="A thoughtful place to start")).to_be_visible()
    page.context.set_offline(True)
    page.wait_for_timeout(250)
    page.context.set_offline(False)
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible(
        timeout=20000
    )
    expect(page.get_by_role("button", name="Stop response")).to_have_count(0)
    expect(page.get_by_role("heading", name="A thoughtful place to start")).to_have_count(1)
    expect(page.locator(".message.user .message-text")).to_have_text(["A slow mobile reconnect"])
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


def test_completed_response_cache_is_bounded_without_losing_recovery_state(page):
    page.evaluate("""async () => {
      const {update}=await import('/static/store.js');
      const lives={};
      for(let i=0;i<100;i++) lives['cached-'+i]={id:'old-'+i,status:'completed',
        persisted:true,text:'x'.repeat(50000),tools:[]};
      lives.working={id:'working',status:'running',text:'',tools:[]};
      lives.uncertain={id:'uncertain',status:'interrupted',uncertain:true,text:'',tools:[]};
      lives.images={id:'images',status:'completed',persisted:true,imageReceipt:true,text:'',tools:[]};
      lives.unpersisted={id:'unpersisted',status:'completed',text:'Unsaved reply',tools:[]};
      update({lives});
    }""")
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
    assert result == {"cached": 16, "bytes": 800_000, "protected": True}


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
    assert page.evaluate("""() => [...CSS.highlights.get('talaria-find')].every(range=>
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
      writeStorage('last-session','startup');update({active:null});
      check.done=connect().finally(()=>{window.fetch=original});
    }""")
    try:
        expect(page.get_by_text("History arrived before the sidebar.", exact=True)).to_be_visible()
        assert page.evaluate("""!startupCheck.released &&
          ['/api/models','/api/readiness'].every(path=>startupCheck.calls.includes(path))""")
    finally:
        page.evaluate("async()=>{startupCheck.release?.();await startupCheck.done}")

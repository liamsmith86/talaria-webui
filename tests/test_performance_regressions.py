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
      update({active:'history',history:Array.from({length:100},(_,i)=>({
        id:i+1,role:'assistant',content:'```javascript\\nconst entry'+i+' = 42;\\n```',
      })),sessions:[{id:'history',title:'History'}],loading:false});
    }""")
    last = page.locator(".message").last
    expect(last.locator(".token").first).to_be_attached()
    first = page.locator(".message").first
    assert "entry0" in first.locator("code").text_content()
    page.get_by_role("button", name="Find in conversation", exact=True).click()
    page.get_by_label("Find text in conversation").fill("entry0")
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

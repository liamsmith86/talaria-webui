"""Visual pacing must never change received text or delay a terminal result."""

import pytest


@pytest.fixture
def reveal(page):
    page.evaluate(r"""async () => {
      const {html,render}=await import('/static/lib.js');
      const {Markdown}=await import('/static/markdown.js');
      const root=document.createElement('div');document.body.append(root);
      root.id='reveal-test';
      window.revealTest={
        render(text,streaming=true) {
          render(html`<${Markdown} text=${text} streaming=${streaming} smooth=${true} />`,root);
        },
        text:()=>root.querySelector('p')?.textContent || '',
        cleanup() {render(null,root);root.remove();delete document.hidden;},
        hidden(value) {
          Object.defineProperty(document,'hidden',{value,configurable:true});
          document.dispatchEvent(new Event('visibilitychange'));
        },
      };
      revealTest.render('Hello');
    }""")
    yield page
    page.evaluate("revealTest.cleanup()")


def test_reveal_steps_preserve_graphemes_and_finish_immediately(reveal):
    result = reveal.evaluate(r"""async () => {
      const full='Hello '+ '👩🏽‍💻 café! '.repeat(80);
      const segments=new Intl.Segmenter(undefined,{granularity:'grapheme'}).segment(full);
      const boundaries=new Set([...segments]
        .map(part=>part.index));boundaries.add(full.length);
      revealTest.render(full);
      const queued=revealTest.text()!==full;
      const lengths=[];
      for(let i=0;i<4;i++) {
        await new Promise(resolve=>setTimeout(resolve,40));
        lengths.push(revealTest.text().length);
      }
      revealTest.render(full,false);
      return {queued,progress:lengths[0]>5 && lengths.at(-1)>lengths[0],
        graphemes:lengths.every(n=>boundaries.has(n)),finished:revealTest.text()===full};
    }""")
    assert result == {"queued": True, "progress": True, "graphemes": True, "finished": True}


def test_hidden_tabs_pause_and_catch_up_without_replaying_a_backlog(reveal):
    result = reveal.evaluate(r"""async () => {
      revealTest.hidden(true);
      const text='Hello '+ 'More text. '.repeat(500);
      revealTest.render(text);
      await new Promise(resolve=>setTimeout(resolve,100));
      const paused=revealTest.text()==='Hello';
      revealTest.hidden(false);
      await new Promise(requestAnimationFrame);
      return {paused,caughtUp:revealTest.text()===text};
    }""")
    assert result == {"paused": True, "caughtUp": True}


def test_reduced_motion_bypasses_pacing_and_cancels_fades(reveal):
    reveal.emulate_media(reduced_motion="reduce")
    reveal.wait_for_function("() => matchMedia('(prefers-reduced-motion: reduce)').matches")
    reveal.evaluate("revealTest.render('Hello '+ 'A calm reply. '.repeat(100))")
    reveal.wait_for_function("() => revealTest.text() === 'Hello '+ 'A calm reply. '.repeat(100)")
    assert reveal.evaluate(
        "document.getElementById('reveal-test').getAnimations({subtree:true}).length"
    ) == 0


def test_replacements_and_unmount_discard_pending_reveal_work(reveal):
    result = reveal.evaluate(r"""async () => {
      revealTest.render('Hello '+ 'A long queued reply. '.repeat(100));
      revealTest.render('Corrected output');
      const replaced=revealTest.text()==='Corrected output';
      revealTest.render('Corrected output with more queued text '.repeat(30));
      revealTest.cleanup();
      await new Promise(resolve=>setTimeout(resolve,120));
      return {replaced,detached:!document.getElementById('reveal-test'),
        animations:document.getAnimations().filter(a=>a.effect?.target?.closest('#reveal-test')).length};
    }""")
    assert result == {"replaced": True, "detached": True, "animations": 0}


def test_fades_do_not_replay_or_replace_settled_text(reveal):
    result = reveal.evaluate(r"""async () => {
      const original=Element.prototype.animate;
      const seen=new Set();let replay=false,calls=0;
      Element.prototype.animate=function(...args) {
        if(seen.has(this)) replay=true;
        seen.add(this);calls++;return original.apply(this,args);
      };
      try {
        revealTest.render('Hello '+ 'Readable words. '.repeat(12));
        await new Promise(resolve=>setTimeout(resolve,350));
        const first=document.querySelector('#reveal-test p span span');
        const range=document.createRange();range.selectNodeContents(first);
        getSelection().removeAllRanges();getSelection().addRange(range);
        const selected=getSelection().toString();
        const full='Hello '+ 'Readable words. '.repeat(30);
        revealTest.render(full);
        await new Promise(resolve=>setTimeout(resolve,350));
        revealTest.render(full,false);
        return {calls,replay,stable:first===document.querySelector('#reveal-test p span span'),
          selected:getSelection().toString()===selected,
          animations:document.getElementById('reveal-test').getAnimations({subtree:true}).length};
      } finally {Element.prototype.animate=original;getSelection().removeAllRanges();}
    }""")
    assert result["calls"] > 0
    assert not result["replay"]
    assert result["stable"] and result["selected"]
    assert result["animations"] == 0


def test_mobile_scroll_follows_revealed_text_until_reader_scrolls_up(page):
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""async () => {
      const {update}=await import('/static/store.js');
      update({active:'paced',history:[],sessions:[{id:'paced',title:'Paced'}],loading:false,
        lives:{paced:{id:'paced-run',status:'running',text:'A readable reply. '.repeat(100),
          tools:[],userText:'Test',baseHistoryLength:0}}});
    }""")
    page.wait_for_function("""() => {
      const el=document.querySelector('.conversation-viewport');
      return el.scrollHeight>el.clientHeight && el.scrollHeight-el.scrollTop-el.clientHeight<5;
    }""")
    page.evaluate("""async () => {
      const {state,update}=await import('/static/store.js');
      const text=state.lives.paced.text+'More detail. '.repeat(100);
      update({lives:{paced:{...state.lives.paced,text}}});
    }""")
    page.wait_for_function("""async () => {
      const {state}=await import('/static/store.js');
      return document.querySelector('.message.assistant p').textContent===state.lives.paced.text;
    }""")
    page.wait_for_function("""() => {
      const el=document.querySelector('.conversation-viewport');
      return el.scrollHeight-el.scrollTop-el.clientHeight<5;
    }""")
    page.evaluate("""() => {
      const el=document.querySelector('.conversation-viewport');
      el.scrollTop=0;el.dispatchEvent(new Event('scroll'));
    }""")
    page.evaluate("""async () => {
      const {state,update}=await import('/static/store.js');
      const text=state.lives.paced.text+'Still arriving. '.repeat(60);
      update({lives:{paced:{...state.lives.paced,text}}});
    }""")
    page.wait_for_function("""async () => {
      const {state}=await import('/static/store.js');
      return document.querySelector('.message.assistant p').textContent===state.lives.paced.text;
    }""")
    assert page.locator(".conversation-viewport").evaluate("el => el.scrollTop") < 5

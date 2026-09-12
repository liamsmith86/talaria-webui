"""Recover interrupted frontend work without consuming newer drafts or mixing histories."""

import pytest
from playwright.sync_api import expect

from .conftest import wait_for_store
from .test_conversation_features import IMAGE, png, seed
from .test_image_draft_migration import image
from .test_reply_settlement import finish_reply, start_reply
from .test_session_search import enable


def attach(page, name):
    page.get_by_label("File attachment").set_input_files(
        {"name": name, "mimeType": "image/png", "buffer": png()}
    )
    expect(page.get_by_role("button", name=f"Remove {name}", exact=True)).to_be_visible()


@pytest.mark.parametrize("banner", ["error", "connection"])
def test_banners_preserve_the_composer_and_attachment_state(page, banner):
    attach(page, "keep.png")
    page.get_by_label("File attachment").set_input_files(
        {"name": "unsupported.bin", "mimeType": "application/octet-stream", "buffer": b"fixture"}
    )
    attachment_error = "Attach an image, or a text/code file up to 200 KB."
    expect(page.locator(".attachment-error")).to_contain_text(attachment_error)
    composer = page.get_by_label("Message Hermes", exact=True)
    composer.fill("Keep the draft I am still writing")
    page.evaluate("""() => {
      window.bannerReader = {
        composer:document.querySelector('.composer-input textarea'),
        image:document.querySelector('.attachment-chip'),
        error:document.querySelector('.attachment-error'),
      };
    }""")

    def retained():
        assert page.evaluate("""() =>
          bannerReader.composer === document.querySelector('.composer-input textarea') &&
          bannerReader.image === document.querySelector('.attachment-chip') &&
          bannerReader.error === document.querySelector('.attachment-error')""")
        expect(composer).to_be_focused()
        expect(composer).to_have_value("Keep the draft I am still writing")
        expect(page.locator(".attachment-chip")).to_have_count(1)
        expect(page.get_by_role("button", name="Remove keep.png", exact=True)).to_be_visible()
        expect(page.locator(".attachment-error")).to_contain_text(attachment_error)

    if banner == "error":
        page.evaluate("""async () => {
          const {fail} = await import('/static/store.js');
          fail({message:'Synthetic connection error'});
        }""")
        notice = page.locator(".error-banner")
        expect(notice).to_contain_text("Synthetic connection error")
    else:
        page.evaluate("""async () => {
          const {state, update} = await import('/static/store.js');
          window.bannerCapabilities = state.caps;
          update({caps:{...state.caps, features:{...state.caps.features, run_submission:false}}});
        }""")
        notice = page.locator(".connection-banner")
        expect(notice).to_contain_text("Update Hermes")
    retained()
    if banner == "error":
        # Exercise the dismissal handler without deliberately moving pointer
        # focus from the textarea onto the button being removed.
        page.get_by_role("button", name="Dismiss error", exact=True).dispatch_event("click")
    else:
        # This connection banner leaves the textarea enabled. Disconnecting
        # outright disables it and would intentionally release browser focus.
        page.evaluate("""async () => {
          (await import('/static/store.js')).update({caps:window.bannerCapabilities});
        }""")
    expect(notice).to_have_count(0)
    retained()


@pytest.mark.parametrize("original_image", [False, True])
@pytest.mark.parametrize("navigate", [False, True])
def test_ambiguous_retry_consumes_only_its_original_attachments(
    page, live_app, original_image, navigate
):
    sid = seed(live_app[1], "retry-draft", count=0)
    other = seed(live_app[1], "other-draft", count=0)
    page.evaluate("async id => (await import('/static/store.js')).openSession(id)", sid)
    page.get_by_label("Message Hermes").fill("Original request")
    if original_image:
        attach(page, "original.png")
    submitted = []

    def ambiguous(route):
        submitted.append(route.request.post_data_json)
        route.fulfill(status=503, json={"error": "Submission confirmation lost"})

    page.route("**/api/runs", ambiguous, times=1)
    page.get_by_role("button", name="Send message", exact=True).click()
    wait_for_store(page, "s => s.lives['retry-draft']?.uncertain === true")
    attach(page, "newer.png")
    held = []
    page.route("**/api/runs", lambda route: held.append(route), times=1)
    page.evaluate("() => {window.EventSource = class {close() {}};}")
    page.get_by_role("button", name="Retry submission", exact=True).click()
    expect(page.get_by_role("button", name="Recovering…", exact=True)).to_be_disabled()
    assert len(held) == 1
    assert held[0].request.post_data_json == submitted[0]
    if navigate:
        page.evaluate("async id => (await import('/static/store.js')).openSession(id)", other)
        page.get_by_label("Message Hermes").fill("Other draft")
        attach(page, "other.png")
    held[0].fulfill(json={"run_id": "recovered-original"})
    wait_for_store(page, "s => s.lives['retry-draft']?.recovered === true")
    if navigate:
        expect(page.get_by_label("Message Hermes")).to_have_value("Other draft")
        expect(page.get_by_role("button", name="Remove other.png", exact=True)).to_be_visible()
        page.evaluate("async id => (await import('/static/store.js')).openSession(id)", sid)
    expect(page.get_by_label("Message Hermes")).to_have_value("")
    expect(page.locator(".attachment-chip")).to_have_count(1)
    expect(page.get_by_role("button", name="Remove newer.png", exact=True)).to_be_visible()
    stored = page.evaluate("""async () => {
      const {pendingStorage} = await import('/static/attachments.js');
      return (await pendingStorage('draft.retry-draft') || []).map(image => image.name);
    }""")
    assert stored == ["newer.png"]


def test_retry_finishing_during_attachment_preparation_keeps_the_new_draft(page, live_app):
    sid = seed(live_app[1], "retry-preparing", count=0)
    page.evaluate("async id => (await import('/static/store.js')).openSession(id)", sid)
    page.get_by_label("Message Hermes").fill("Original request")
    attach(page, "original.png")
    page.route(
        "**/api/runs",
        lambda route: route.fulfill(status=503, json={"error": "Submission confirmation lost"}),
        times=1,
    )
    page.get_by_role("button", name="Send message", exact=True).click()
    wait_for_store(page, "s => s.lives['retry-preparing']?.uncertain === true")
    page.evaluate("""() => {
      const decode = Image.prototype.decode;
      Image.prototype.decode = function() {
        const image = this;
        return new Promise(resolve => {window.finishImageDecode = resolve;})
          .then(() => decode.call(image));
      };
      window.EventSource = class {constructor() {window.retrySource = this;} close() {}};
    }""")
    page.get_by_label("File attachment").set_input_files(
        {"name": "preparing.png", "mimeType": "image/png", "buffer": png()}
    )
    expect(page.get_by_text("Preparing attachments…", exact=True)).to_be_visible()
    page.route("**/api/runs", lambda route: route.fulfill(json={"run_id": "recovered"}), times=1)
    page.get_by_role("button", name="Retry submission", exact=True).click()
    wait_for_store(page, "s => s.lives['retry-preparing']?.recovered === true")
    live_app[1].messages[sid] = [
        {"id": 1, "role": "user", "content": "Original request\n[screenshot]"},
        {"id": 2, "role": "assistant", "content": "Saved answer"},
    ]
    page.evaluate("""() => retrySource.onmessage({data:JSON.stringify({
      event:'run.completed', output:'Saved answer'
    })})""")
    wait_for_store(page, "s => s.lives['retry-preparing']?.persisted === true")
    page.get_by_label("Message Hermes").fill("New request")
    page.evaluate("finishImageDecode()")
    expect(page.get_by_role("button", name="Remove preparing.png", exact=True)).to_be_visible()
    expect(page.locator(".attachment-chip")).to_have_count(1)
    expect(page.get_by_label("Message Hermes")).to_have_value("New request")
    assert page.evaluate("""async () => {
      const {pendingStorage} = await import('/static/attachments.js');
      return (await pendingStorage('draft.retry-preparing')).map(image=>image.name);
    }""") == ["preparing.png"]


def test_reopening_a_recovered_run_keeps_a_new_identical_text_draft(page, live_app):
    sid = seed(live_app[1], "retry-once", count=0)
    page.evaluate("async id => (await import('/static/store.js')).openSession(id)", sid)
    page.get_by_label("Message Hermes").fill("Same request")
    page.route(
        "**/api/runs",
        lambda route: route.fulfill(status=503, json={"error": "Submission confirmation lost"}),
        times=1,
    )
    page.get_by_role("button", name="Send message", exact=True).click()
    wait_for_store(page, "s => s.lives['retry-once']?.uncertain === true")
    page.evaluate("() => {window.EventSource = class {close() {}};}")
    page.route("**/api/runs", lambda route: route.fulfill(json={"run_id": "recovered"}), times=1)
    page.get_by_role("button", name="Retry submission", exact=True).click()
    wait_for_store(page, "s => s.lives['retry-once']?.recovered === true")
    expect(page.get_by_label("Message Hermes")).to_have_value("")
    page.get_by_label("Message Hermes").fill("Same request")
    page.evaluate("async () => (await import('/static/store.js')).newConversation()")
    expect(page.get_by_label("Message Hermes")).to_have_value("")
    page.evaluate("async () => (await import('/static/store.js')).openSession('retry-once')")
    expect(page.get_by_label("Message Hermes")).to_have_value("Same request")
    assert (
        page.evaluate("""async () => (await import('/static/lib.js'))
      .readStorage('draft.retry-once')""")
        == "Same request"
    )


def test_incomplete_retry_acknowledgement_preserves_draft_and_receipt(page, live_app):
    sid = seed(live_app[1], "retry-incomplete", count=0)
    page.evaluate("async id => (await import('/static/store.js')).openSession(id)", sid)
    page.get_by_label("Message Hermes").fill("Keep this request")
    attach(page, "submitted.png")
    page.route(
        "**/api/runs",
        lambda route: route.fulfill(status=503, json={"error": "Submission confirmation lost"}),
        times=1,
    )
    page.get_by_role("button", name="Send message", exact=True).click()
    wait_for_store(page, "s => s.lives['retry-incomplete']?.uncertain === true")
    attach(page, "newer.png")
    snapshot = """async () => {
      const {state} = await import('/static/store.js');
      const {readStorage} = await import('/static/lib.js');
      const {pendingStorage} = await import('/static/attachments.js');
      const live = state.lives['retry-incomplete'];
      return {text:readStorage('draft.retry-incomplete'), id:live.id,
        requestId:live.requestId, uncertain:live.uncertain,
        images:(await pendingStorage('draft.retry-incomplete')).map(image=>image.id),
        receipt:await pendingStorage(`run.retry-incomplete.${live.requestId}`)};
    }"""
    before = page.evaluate(snapshot)
    page.route("**/api/runs", lambda route: route.fulfill(json={}), times=1)
    page.get_by_role("button", name="Retry submission", exact=True).click()
    wait_for_store(page, "s => s.error === 'The submission confirmation was incomplete.'")
    assert page.evaluate(snapshot) == before
    expect(page.get_by_label("Message Hermes")).to_have_value("Keep this request")
    expect(page.locator(".attachment-chip")).to_have_count(2)
    expect(page.get_by_role("button", name="Retry submission", exact=True)).to_be_enabled()


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("navigate", [False, True])
def test_older_page_respects_transcript_identity_and_navigation(page, canonical, navigate):
    result = page.evaluate(
        """async ({canonical, navigate, image}) => {
      const store = await import('/static/store.js');
      const {pendingStorage} = await import('/static/attachments.js');
      const {readStorage, writeStorage} = await import('/static/lib.js');
      const row = id => ({id, role:'user', content:`Message ${id}`});
      const fetch = window.fetch;
      let release, heads = 0;
      window.fetch = (url, options) => {
        const path = new URL(url, location.href);
        if (path.pathname === '/api/sessions/original/messages') {
          if (path.search) return new Promise(resolve => {release = resolve;});
          heads++;
          return Promise.resolve(Response.json({session_id:'continued',
            data:[row(301), row(302)], has_more:true, next_offset:2}));
        }
        return fetch(url, options);
      };
      try {
        writeStorage('draft.original', 'Unsent original draft');
        await pendingStorage('draft.original', [image]);
        store.update({active:'original', history:[row(101),row(102)],
          loading:false, historyHasMore:true, historyOffset:2});
        const older = store.loadOlderMessages();
        if (navigate) store.newConversation();
        release(Response.json({session_id:canonical ? 'continued' : 'original',
          data:canonical ? [row(201),row(202)] : [row(100),row(101)],
          has_more:false, next_offset:3}));
        await older;
        return {active:store.state.active, ids:store.state.history.map(row=>row.id),
          offset:store.state.historyOffset, more:store.state.historyHasMore, heads,
          source:await pendingStorage('draft.original') ?? null,
          target:await pendingStorage('draft.continued') ?? null,
          sourceText:readStorage('draft.original'), targetText:readStorage('draft.continued')};
      } finally {window.fetch = fetch;}
    }""",
        {"canonical": canonical, "navigate": navigate, "image": image("unsent")},
    )
    if navigate:
        assert result == {
            "active": None,
            "ids": [],
            "offset": 0,
            "more": False,
            "heads": 0,
            "source": [image("unsent")],
            "target": None,
            "sourceText": "Unsent original draft",
            "targetText": "",
        }
    elif canonical:
        assert result == {
            "active": "continued",
            "ids": [301, 302],
            "offset": 2,
            "more": True,
            "heads": 1,
            "source": None,
            "target": [image("unsent")],
            "sourceText": "",
            "targetText": "Unsent original draft",
        }
        assert "session=continued" in page.url
    else:
        assert result == {
            "active": "original",
            "ids": [100, 101, 102],
            "offset": 3,
            "more": False,
            "heads": 0,
            "source": [image("unsent")],
            "target": None,
            "sourceText": "Unsent original draft",
            "targetText": "",
        }


def failed_settlement(page, live_app, *, receipt=False, saved=True):
    text = "Identical answer"
    start_reply(page, live_app, text)
    user = "Read this reply" + ("\n[screenshot]" if receipt else "")
    previous = [
        {"id": 1, "role": "user", "content": user},
        {"id": 2, "role": "assistant", "content": text},
    ]
    live_app[1].messages["settling"] = previous + (
        [
            {"id": 3, "role": "user", "content": user},
            {"id": 4, "role": "assistant", "content": text},
        ]
        if saved
        else []
    )
    page.evaluate(
        """async ({previous, receipt, image}) => {
      const {state, update} = await import('/static/store.js');
      const {pendingStorage} = await import('/static/attachments.js');
      const live = {...state.lives.settling, baseMessageId:2, baseUserId:1};
      if (receipt) {
        Object.assign(live, {imageBoundary:2, imageReceipt:true, userImages:[image]});
        await pendingStorage('run.settling.settling-request', {images:[image], imageBoundary:2});
      }
      update({history:previous, lives:{settling:live}});
      (await import('/static/session-navigation.js')).rememberSession('settling', true);
      const {options} = await import('/static/vendor/preact.js');
      options.debounceRendering = callback => queueMicrotask(callback);
      window.maxRecoveryBubbles = 0;
      window.recoveryObserver = new MutationObserver(() => {
        if (window.minRecoveryBubbles !== undefined)
          minRecoveryBubbles = Math.min(minRecoveryBubbles,
            document.querySelectorAll('.message.assistant').length);
        maxRecoveryBubbles = Math.max(maxRecoveryBubbles,
          document.querySelectorAll('.message.assistant').length);
      });
      recoveryObserver.observe(document.body, {childList:true, subtree:true});
    }""",
        {"previous": previous, "receipt": receipt, "image": {**image("submitted"), "url": IMAGE}},
    )
    expect(page.locator(".message.assistant")).to_have_count(2)
    page.route(
        "**/api/sessions/settling/messages?*",
        lambda route: route.fulfill(status=503, json={"error": "History temporarily unavailable"}),
        times=1,
    )
    finish_reply(page, text)
    wait_for_store(page, "s => s.error === 'History temporarily unavailable'")
    wait_for_store(
        page, "s => !s.lives.settling.persisted && s.lives.settling.status === 'completed'"
    )


@pytest.mark.parametrize("receipt", [False, True])
@pytest.mark.parametrize("reopen", [False, True])
def test_terminal_history_failure_recovers_without_duplicate_turns(page, live_app, receipt, reopen):
    failed_settlement(page, live_app, receipt=receipt)
    if reopen:
        page.evaluate("""async () => {
          const {newConversation, openSession} = await import('/static/store.js');
          newConversation(); await openSession('settling');
        }""")
    else:
        page.evaluate("window.minRecoveryBubbles = 2; dispatchEvent(new Event('focus'))")
    wait_for_store(page, "s => s.history.length === 4")
    expect(page.locator(".message.assistant")).to_have_count(2)
    wait_for_store(page, "s => s.lives.settling.persisted === true")
    assert page.evaluate("maxRecoveryBubbles") == 2
    if not reopen:
        assert page.evaluate("minRecoveryBubbles") == 2
    recovered = page.evaluate("""async () => {
      const {state} = await import('/static/store.js');
      const {pendingStorage} = await import('/static/attachments.js');
      const {readStorage} = await import('/static/lib.js');
      return {saved:state.lives.settling.savedMessageId,
        receipt:await pendingStorage('run.settling.settling-request') ?? null,
        imageRows:state.history.filter(row=>row.browserImages?.length).map(row=>row.id),
        storedRuns:JSON.parse(readStorage('runs','{}'))};
    }""")
    assert recovered == {
        "saved": 4,
        "receipt": None,
        "imageRows": [3] if receipt else [],
        "storedRuns": {},
    }
    if receipt:
        page.reload()
        wait_for_store(page, "s => s.history.length === 4")
        cached = page.evaluate("""async () => (await import('/static/store.js')).state.history
          .filter(row=>row.browserImages?.length).map(row=>row.id)""")
        assert cached == [3]


def test_recovery_does_not_adopt_an_earlier_identical_turn(page, live_app):
    failed_settlement(page, live_app, receipt=True, saved=False)
    page.evaluate("""async () => {
      await (await import('/static/store.js')).refreshHistory('settling');
    }""")
    expect(page.locator(".message.assistant")).to_have_count(2)
    result = page.evaluate("""async () => {
      const {state} = await import('/static/store.js');
      const {pendingStorage} = await import('/static/attachments.js');
      return {persisted:!!state.lives.settling.persisted,
        receipt:!!await pendingStorage('run.settling.settling-request'),
        cached:state.history.some(row=>row.browserImages?.length)};
    }""")
    assert result == {"persisted": False, "receipt": True, "cached": False}


@pytest.mark.parametrize("archived_window", [False, True])
def test_saved_background_run_reconciles_without_replacing_the_view(
    page, live_app, archived_window
):
    failed_settlement(page, live_app, receipt=True)
    result = page.evaluate(
        """async archived => {
      const store = await import('/static/store.js');
      if (archived) store.update({searchWindow:'1', history:store.state.history.slice(0, 1)});
      else store.newConversation();
      await store.refreshHistory('settling');
      return {active:store.state.active, history:store.state.history.map(row=>row.id),
        persisted:store.state.lives.settling.persisted,
        receipt:store.state.lives.settling.imageReceipt};
    }""",
        archived_window,
    )
    assert result == {
        "active": "settling" if archived_window else None,
        "history": [1] if archived_window else [],
        "persisted": True,
        "receipt": False,
    }


@pytest.mark.parametrize("navigate", [False, True])
def test_recovery_image_commit_cannot_retire_a_new_run_or_restore_old_navigation(
    page, live_app, navigate
):
    failed_settlement(page, live_app, receipt=True)
    result = page.evaluate(
        """async navigate => {
      const store = await import('/static/store.js');
      const put = IDBObjectStore.prototype.put;
      let interrupted = false;
      IDBObjectStore.prototype.put = function(value, key) {
        const result = put.call(this, value, key);
        if (key === 'image.settling.3') queueMicrotask(() => {
          interrupted = true;
          if (navigate) store.newConversation();
          else store.update({lives:{settling:{id:'newer-run', requestId:'newer-request',
            status:'running', userText:'New question', text:'New response', tools:[]}}});
        });
        return result;
      };
      try {
        await store.refreshHistory('settling');
        return {interrupted, active:store.state.active,
          history:store.state.history.map(row=>row.id),
          id:store.state.lives.settling.id, persisted:!!store.state.lives.settling.persisted};
      } finally {IDBObjectStore.prototype.put = put;}
    }""",
        navigate,
    )
    assert result == {
        "interrupted": True,
        "active": None if navigate else "settling",
        "history": [] if navigate else [1, 2, 3, 4],
        "id": "settling-run" if navigate else "newer-run",
        "persisted": False,
    }


def test_later_search_page_retry_preserves_preceding_hits(page, live_app):
    seed(live_app[1], count=70)
    enable(page, live_app[1])
    page.get_by_label("Search sessions").fill("Message")
    hits = page.locator(".history-search-hit")
    expect(hits).to_have_count(20)
    page.get_by_role("button", name="More matches", exact=True).click()
    expect(hits).to_have_count(40)
    preceding = hits.evaluate_all("nodes => nodes.map(node=>node.getAttribute('href'))")
    page.route(
        "**/api/search?*offset=40",
        lambda route: route.fulfill(
            status=503, json={"error": "Search page temporarily unavailable"}
        ),
        times=1,
    )
    page.get_by_role("button", name="More matches", exact=True).click()
    expect(page.get_by_role("button", name="Retry search", exact=True)).to_be_visible()
    expect(hits).to_have_count(40)
    page.get_by_role("button", name="Retry search", exact=True).click()
    expect(hits).to_have_count(60)
    recovered = hits.evaluate_all("nodes => nodes.map(node=>node.getAttribute('href'))")
    assert recovered[:40] == preceding
    assert len(set(recovered)) == 60
    page.get_by_role("button", name="More matches", exact=True).click()
    expect(hits).to_have_count(70)

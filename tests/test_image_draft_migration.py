"""Canonical session transitions retain browser-owned image drafts."""

import pytest
from playwright.sync_api import expect

from .test_app import signed_in
from .test_commands import prepare
from .test_conversation_features import IMAGE, png


def image(identifier, size=100):
    return {
        "id": identifier,
        "name": identifier + ".png",
        "size": size,
        "url": "data:image/png;base64,aQ==",
    }


def test_image_draft_move_merges_and_consumption_only_removes_submitted_ids(module_page):
    result = module_page.evaluate(
        """async (images) => {
          const {pendingStorage, migratePendingImages, consumePendingImages} =
            await import('/static/attachments.js');
          await pendingStorage('draft.original', images.slice(0, 2));
          await pendingStorage('draft.continued', images.slice(1));
          await migratePendingImages('draft.original', 'draft.continued');
          const moved = await pendingStorage('draft.continued');
          await consumePendingImages('draft.continued', []);
          const untouched = await pendingStorage('draft.continued');
          await consumePendingImages('draft.continued', [images[0], images[1]]);
          return {source: await pendingStorage('draft.original') ?? null, moved, untouched,
                  remaining: await pendingStorage('draft.continued')};
        }""",
        [image("one"), image("two"), image("newer")],
    )
    assert result["source"] is None
    assert [item["id"] for item in result["moved"]] == ["two", "newer", "one"]
    assert result["untouched"] == result["moved"]
    assert result["remaining"] == [image("newer")]


@pytest.mark.parametrize("large", [False, True])
def test_conflicting_image_drafts_abort_without_losing_either_record(module_page, large):
    source = [image("one", 2_000_000), image("two", 2_000_000)]
    target = [image("three", 2_000_000), image("four", 2_000_000)]
    if not large:
        source = [image("one"), image("two"), image("five")]
        target = [image("three"), image("four")]
    result = module_page.evaluate(
        """async ([source, target]) => {
          const {pendingStorage, migratePendingImages} = await import('/static/attachments.js');
          await pendingStorage('draft.original', source);
          await pendingStorage('draft.continued', target);
          let error;
          try { await migratePendingImages('draft.original', 'draft.continued'); }
          catch (failure) { error = failure.code; }
          return {error, source: await pendingStorage('draft.original'),
                  target: await pendingStorage('draft.continued')};
        }""",
        [source, target],
    )
    assert result == {"error": "draft_conflict", "source": source, "target": target}


@pytest.mark.parametrize("preparing_destination", [False, True])
def test_compaction_waits_for_attachment_preparation_before_switching(
    page, live_app, preparing_destination
):
    sid = prepare(page, live_app)
    with signed_in(live_app[0]) as client:
        continued = client.post("/api/sessions", json={"title": "Continued draft"}).json()["id"]
    if preparing_destination:
        page.evaluate(
            """async ({sid, continued, image}) => {
              const {pendingStorage} = await import('/static/attachments.js');
              await pendingStorage(`draft.${sid}`, [image]);
              await (await import('/static/store.js')).openSession(continued);
            }""",
            {"sid": sid, "continued": continued, "image": {**image("source"), "url": IMAGE}},
        )
    page.evaluate("""() => {
      const decode = Image.prototype.decode;
      Image.prototype.decode = function() {
        const image = this;
        return new Promise(resolve => { window.finishImageDecode = resolve; })
          .then(() => decode.call(image));
      };
    }""")
    page.get_by_label("File attachment").set_input_files(
        {"name": "preparing.png", "mimeType": "image/png", "buffer": png()}
    )
    expect(page.get_by_text("Preparing attachments…", exact=True)).to_be_visible()
    page.route(
        f"**/api/sessions/{sid}/messages",
        lambda route: route.fulfill(json={"session_id": continued, "data": []}),
    )
    page.evaluate(
        """async (sid) => {
      const {openSession} = await import('/static/store.js');
      window.movingDraft = openSession(sid);
    }""",
        sid,
    )
    expect(page.get_by_role("button", name="Attach files", exact=True)).to_be_disabled()
    assert page.evaluate("async () => (await import('/static/store.js')).state.active") == sid
    page.evaluate("window.finishImageDecode()")
    page.evaluate("window.movingDraft")
    expect(page.get_by_role("button", name="Remove preparing.png", exact=True)).to_be_visible()
    if preparing_destination:
        expect(page.get_by_role("button", name="Remove source.png", exact=True)).to_be_visible()
    assert page.evaluate("async () => (await import('/static/store.js')).state.active") == continued
    page.reload()
    expect(page.get_by_role("button", name="Remove preparing.png", exact=True)).to_be_visible()


def test_navigation_away_during_draft_migration_does_not_switch_back(page, live_app):
    sid = prepare(page, live_app)
    with signed_in(live_app[0]) as client:
        continued = client.post("/api/sessions", json={"title": "Continued draft"}).json()["id"]
        other = client.post("/api/sessions", json={"title": "Another session"}).json()["id"]
    page.route(
        f"**/api/sessions/{sid}/messages",
        lambda route: route.fulfill(json={"session_id": continued, "data": []}),
    )
    page.evaluate(
        """async (sid) => {
      const {beginPendingImages} = await import('/static/attachments.js');
      const {openSession} = await import('/static/store.js');
      window.finishDraftWork = beginPendingImages(`draft.${sid}`);
      window.movingDraft = openSession(sid);
    }""",
        sid,
    )
    page.evaluate("async (sid) => (await import('/static/store.js')).openSession(sid)", other)
    page.get_by_role("textbox", name="Message Hermes").fill("Other session draft")
    page.evaluate("window.finishDraftWork()")
    page.evaluate("window.movingDraft")
    assert page.evaluate("async () => (await import('/static/store.js')).state.active") == other
    expect(page.get_by_role("textbox", name="Message Hermes")).to_have_value("Other session draft")


def test_text_draft_continues_when_indexeddb_is_unavailable(page, live_app):
    sid = prepare(page, live_app)
    with signed_in(live_app[0]) as client:
        continued = client.post("/api/sessions", json={"title": "Continued text"}).json()["id"]
    page.add_init_script(
        "Object.defineProperty(window, 'indexedDB', {get() {throw new Error('Storage disabled')}})"
    )
    page.reload()
    composer = page.get_by_role("textbox", name="Message Hermes")
    composer.fill("Keep this text")
    page.route(
        f"**/api/sessions/{sid}/messages",
        lambda route: route.fulfill(json={"session_id": continued, "data": []}),
    )
    page.evaluate("async (sid) => (await import('/static/store.js')).openSession(sid)", sid)
    expect(composer).to_have_value("Keep this text")
    assert page.evaluate("async () => (await import('/static/store.js')).state.active") == continued


def test_aborted_image_move_preserves_both_records(module_page):
    result = module_page.evaluate(
        """async ([source, target]) => {
          const {pendingStorage, migratePendingImages} = await import('/static/attachments.js');
          await pendingStorage('draft.original', source);
          await pendingStorage('draft.continued', target);
          const put = IDBObjectStore.prototype.put;
          let failed = false;
          IDBObjectStore.prototype.put = function(value, key) {
            if (key === 'draft.continued') this.transaction.abort();
            return put.call(this, value, key);
          };
          try { await migratePendingImages('draft.original', 'draft.continued'); }
          catch { failed = true; }
          finally { IDBObjectStore.prototype.put = put; }
          return {failed, source: await pendingStorage('draft.original'),
                  target: await pendingStorage('draft.continued')};
        }""",
        [[image("one")], [image("two")]],
    )
    assert result == {"failed": True, "source": [image("one")], "target": [image("two")]}

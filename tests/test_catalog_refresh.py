"""Overlapping model discovery must never replace newer inventory or clear usable data."""

import pytest


@pytest.mark.parametrize(
    "old_failure,new_failure,old_first",
    [
        (False, False, False),
        (False, False, True),
        (True, False, False),
        (True, False, True),
        (False, True, False),
    ],
)
def test_catalog_refresh_orders_responses_and_keeps_last_usable_data(
    page, old_failure, new_failure, old_first
):
    result = page.evaluate(
        """async ([oldFailure, newFailure, oldFirst]) => {
      const s = await import('/static/store.js');
      const fetch = window.fetch, replies = new Map();
      window.fetch = (url, opts) => String(url).includes('/models')
        ? new Promise(resolve => replies.set(String(url), resolve)) : fetch(url, opts);
      const data = (id, failed) => new Response(JSON.stringify(failed
        ? {error:'Synthetic discovery failure'} : {model:id, provider:'probe',
          providers:[{id:'probe', authenticated:true, models:[id]}]}),
        {status:failed ? 503 : 200, headers:{'Content-Type':'application/json'}});
      const before = JSON.stringify(s.state.models);
      try {
        const old = s.refreshModels();
        const ordinaryShared = s.refreshModels() === old;
        const fresh = s.refreshModels(true);
        const forceShared = s.refreshModels(true) === fresh && s.refreshModels() === fresh;
        const errors = [];
        const observed = [old, fresh].map(p => p.catch(e=>errors.push(e.message)));
        const finishOld = async () => {
          replies.get('/api/models')(data('stale',oldFailure)); await observed[0];
        };
        const finishNew = async () => {
          replies.get('/api/models?refresh=1')(data('fresh',newFailure)); await observed[1];
        };
        if(oldFirst) {await finishOld(); await finishNew();}
        else {await finishNew(); await finishOld();}
        return {ordinaryShared, forceShared, requests:replies.size, errors,
          preserved:before === JSON.stringify(s.state.models),
          models:s.state.models.map(m=>m.id), default:s.state.defaultModel?.id};
      } finally {window.fetch = fetch;}
    }""",
        [old_failure, new_failure, old_first],
    )
    assert result["ordinaryShared"] and result["forceShared"] and result["requests"] == 2
    assert result["errors"] == (["Synthetic discovery failure"] if new_failure else [])
    if new_failure:
        assert result["preserved"]
    else:
        assert result["models"] == ["fresh"] and result["default"] == "fresh"


def test_reconnecting_does_not_reuse_previous_connection_discovery(page):
    result = page.evaluate("""async () => {
      const s = await import('/static/store.js');
      const fetch = window.fetch, replies = [];
      window.fetch = (url, opts) => String(url).includes('/models')
        ? new Promise(resolve => replies.push(resolve)) : fetch(url, opts);
      const data = id => new Response(JSON.stringify({model:id, provider:'fixture',
        providers:[{id:'fixture', authenticated:true, models:[id]}]}),
        {headers:{'Content-Type':'application/json'}});
      try {
        const old = s.refreshModels();
        await s.connect();
        const current = s.refreshModels();
        if (replies.length !== 2) {
          replies[0](data('old')); await old;
          return {requests:replies.length};
        }
        replies[1](data('new')); await current;
        replies[0](data('old')); await old;
        return {requests:replies.length, model:s.state.defaultModel.id};
      } finally {window.fetch=fetch;}
    }""")
    assert result == {"requests": 2, "model": "new"}

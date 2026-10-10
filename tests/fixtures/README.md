# Hermes stream fixtures

These JSON files were recorded from Hermes using
[`hermes_context_contract.py`](../hermes_context_contract.py), temporary sessions
and a local model simulator. All messages are synthetic. The fixtures cover
completed replies, tool calls, interruption and provider errors. Variable IDs
and timings are removed or normalized.

| Fixtures | Hermes commit used for capture or review |
| --- | --- |
| `hermes-completed.json` | [`fe04d5b`](https://github.com/NousResearch/hermes-agent/commit/fe04d5b36de1bc2d69d91185f4955c37aaef8c39) |
| Original tools, interrupted and failure files | [`205645e`](https://github.com/NousResearch/hermes-agent/commit/205645ee424163c7b6cfc032c331c3557797497b) |
| `-interim` variants | [`f97608f`](https://github.com/NousResearch/hermes-agent/commit/f97608f178d1ffeca59860195ab7da295f7c8e5f) |
| `-current` variants | [`7239625`](https://github.com/NousResearch/hermes-agent/commit/7239625ae1b786827c952a8ace41fa6fd7f2eec5) |
| `hermes-interrupted-hidden.json` | [`818c13b`](https://github.com/NousResearch/hermes-agent/commit/818c13be1dc4fd28987e1e881a9408224afd4535) (v0.21.6), also verified on [`46d7718`](https://github.com/NousResearch/hermes-agent/commit/46d7718a52ff33accb15dc0501736fbdb6833cab) |

Native tests compare fresh captures with these files. Browser tests replay each
variant with fragmented and batched delivery, then reload and continue the
session. See [native checks](../test_extended_access.py) and
[browser replay tests](../test_response_turns.py).

The hidden interruption variant preserves `display_kind` for the empty assistant
row that closes an interrupted tool sequence. Hermes uses that row to keep model
history valid while omitting the synthetic interruption banner from the display.

When Hermes changes its event format, inspect the difference before updating a
fixture. Keep recordings synthetic and limit them to fields the tests need;
never copy a real conversation into this directory.

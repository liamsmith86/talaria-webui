# Local development checks

CI stays lightweight while the repository is private. Run checks locally with Python 3.12+, uv, Git, and Node.js; Linux (including WSL2) supports all three Playwright engines. WebKit on Linux does not replace occasional testing on a physical iPhone or Mac.

One-time setup:

```sh
uv sync --locked
uv run --locked playwright install --with-deps chromium firefox webkit
git config --local core.hooksPath .githooks
```

If you already use custom Git hooks, integrate these two hooks instead of replacing your configuration.

- **Commit:** lint staged Python, check staged JavaScript syntax, and reject whitespace errors.
- **Push:** test the committed revision in a temporary checkout with locked dependencies: backend tests, Chromium submission/navigation/recovery regressions, and WebKit response/scrolling regressions. Documentation-only pushes skip runtime tests. Missing prerequisites and skipped tests fail the check.
- **Before release:** run `uv run --locked python contrib/check.py full` for backend, all browser/accessibility tests, and native Hermes contracts.

Run the push checks yourself with `uv run --locked python contrib/check.py check`. Hooks are local guardrails, not a server-enforced security boundary; Git allows bypassing them. They install no services and create no GitHub jobs.

Native contracts use a separate Hermes checkout and its `venv/bin/python3`, temporary storage, and a loopback model simulator. Set `HERMES_SOURCE=/path/to/hermes-agent`; the push hook also accepts `git config --local talaria.hermesSource /path/to/hermes-agent`. Hermes, dependency, and shared fixture changes require these checks. Never point test fixtures at production sessions or credentials.

For full accessibility checks, set `TALARIA_AXE_PATH` to a local `axe-core@4.13.0` `axe.min.js` with SHA-256 `c24f097bd2f451d4f933e8bc7d8d539f8672a2ebcb5cc9f9f3eec8ca9470a0c1`. The existing CI workflow has the download command. Browser binaries follow the locked Playwright version; no new dependency manager is needed.

## Evidence for bug fixes

Write a regression around the visible failure, then verify both revisions:

```sh
uv run --locked python contrib/check.py prove --base BASE_COMMIT \
  tests/test_response_turns.py::test_saved_turn_can_be_recognized_when_its_user_row_is_on_an_older_page
```

This runs the selected tests against current code and a temporary copy of the baseline with the current tests. All selected cases must pass now and fail before; setup errors, skips, unchanged behavior, or different dependency lockfiles are rejected. It does not modify your checkout. Inspect the failure reason: a failing assertion alone does not establish that a scenario is realistic.

Use synchronous Playwright polling predicates; the browser fixtures reject async predicates because they can return before the condition becomes true. Exercise event sequences and user intent (navigate during a request, reconnect, interrupt, scroll upward), not only isolated state snapshots. Distinguish reproduced bugs, preventive changes, and cleanup in PR descriptions, including the evidence and its limits.

The small [native stream fixture](tests/fixtures/README.md) comes from the actual Hermes adapter with synthetic model output. Native tests check for contract drift; browser tests replay its event ordering with fragmented and batched delivery. It covers completed responses; interrupted and tool-heavy turns still rely on separate constructed scenarios.

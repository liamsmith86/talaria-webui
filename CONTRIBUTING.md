# Local development checks

Run checks locally with Python 3.12+, uv, Git, Node.js 22.13+ (24 LTS recommended), and npm 12+; Linux (including WSL2) supports all three Playwright engines. WebKit on Linux does not replace occasional testing on a physical iPhone or Mac.

One-time setup:

```sh
uv sync --locked
npm ci
uv run --locked playwright install --with-deps chromium firefox webkit
git config --local core.hooksPath .githooks
```

If you already use custom Git hooks, integrate these two hooks instead of replacing your configuration.

- **Commit:** check the staged snapshot with Ruff lint/format, Vulture (60%), ESLint and hook rules, ShellCheck, actionlint, and file hygiene. No runtime tests. Unstaged changes cannot hide staged failures.
- **Push:** test the committed revision in a temporary checkout with locked dependencies. Installer changes run installation/backend and relevant native tests; check-runner changes test the runner; frontend changes run browser regressions. Other runtime changes fall back to backend plus Chromium/WebKit checks. Documentation-only pushes skip runtime tests. Missing prerequisites and skipped tests fail fresh checks.
- **Before release:** run `uv run --locked python contrib/check.py full` for backend, all browser/accessibility tests, and native Hermes contracts.

For daily work, run `uv run --locked python contrib/check.py check --changed` (compares with `origin/main`, including uncommitted files). Use `--base REF` for a narrower comparison. Checks use up to four workers; `TALARIA_TEST_WORKERS=2` reduces load, and `=0` runs serially.

Before a planned push, `uv run --locked python contrib/check.py check --revision HEAD` checks the committed snapshot. The hook reuses matching successful results for one hour; `--fresh` forces a rerun. Cache entries are local to `.git`, include the source tree, selected tests, tools and test environment, and are never used for native Hermes checks. `check` without selection flags retains the broader suite. Hooks are local guardrails, not a server-enforced security boundary; Git allows bypassing them. They install no services and create no GitHub jobs.

Native contracts use a separate Hermes checkout and its `venv/bin/python3`, temporary storage, and a loopback model simulator. Set `HERMES_SOURCE=/path/to/hermes-agent` or `git config --local talaria.hermesSource /path/to/hermes-agent`. Hermes, dependency, and shared fixture changes require these checks. Never point test fixtures at production sessions or credentials.

For full accessibility checks, set `TALARIA_AXE_PATH` to a local `axe-core@4.13.0` `axe.min.js` with SHA-256 `c24f097bd2f451d4f933e8bc7d8d539f8672a2ebcb5cc9f9f3eec8ca9470a0c1`. The existing CI workflow has the download command. Browser binaries follow the locked Playwright version. npm is only for development linting; the app still uses native browser modules without a build step.

## Evidence for bug fixes

Write a regression around the visible failure, then verify both revisions:

```sh
uv run --locked python contrib/check.py prove --base BASE_COMMIT \
  tests/test_response_turns.py::test_saved_turn_can_be_recognized_when_its_user_row_is_on_an_older_page
```

This runs the selected tests against current code and a temporary copy of the baseline with the current tests. All selected cases must pass now and fail before; setup errors, skips, unchanged behavior, or different dependency lockfiles are rejected. It does not modify your checkout. Inspect the failure reason: a failing assertion alone does not establish that a scenario is realistic.

Use synchronous Playwright polling predicates; the browser fixtures reject async predicates because they can return before the condition becomes true. Exercise event sequences and user intent (navigate during a request, reconnect, interrupt, scroll upward), not only isolated state snapshots. Distinguish reproduced bugs, preventive changes, and cleanup in PR descriptions, including the evidence and its limits.

The small [native stream fixtures](tests/fixtures/README.md) come from the actual Hermes adapter with synthetic model output. Native tests check for contract drift; browser tests replay completed replies, tools, interruption, and provider failure with fragmented and batched delivery.

## Static quality gates

Run `uv run --locked python contrib/check.py lint`. The same gates run in the existing CI lint job. Missing tools fail the check; `uv sync --locked` installs the Python tools, ShellCheck, and actionlint. The runner installs locked ESLint dependencies when needed and reuses them while the manifest, lockfile, npm settings, and Node/npm versions match. Python and npm dependency resolution both retain the seven-day release cooldown; npm lifecycle scripts are disabled.

Production Python functions are limited to complexity 10, 12 branches, and 50 statements. Tests and development orchestration are exempt from those three size limits, not correctness checks. Prefer named operations over suppressions. Vulture scans all production/development Python; its one dynamic Uvicorn entry point is documented in `contrib/vulture_whitelist.py`. Tests are not used to make otherwise dead production code look used.

ESLint checks our JavaScript, excluding vendored libraries. Hook exceptions must explain lifecycle or memoization intent at the exact dependency array. Hygiene checks reject malformed YAML/TOML/JSON, conflict markers, private keys, and files over 1 MiB; debugger checks come from Ruff and ESLint. Static checks supplement behavioral tests, not replace them.

## Translations

English source strings are translation keys. Mark new UI text with `t`, `rich`, `msg`, or `n` from `static/i18n.js`, then regenerate the English catalog:

```sh
uv run --locked node contrib/i18n.mjs --extract
```

Translate values in `src/talaria/static/locales/LOCALE.json`; keep the English keys and named placeholders such as `{name}` unchanged. Plural entries use the locale's CLDR categories from native `Intl.PluralRules` (ICU), including `other`; use `n` for counts. Catalogs contain no HTML. Links and code belong in named `rich` placeholders. Do not translate Hermes messages, commands, model identifiers, or paths.

Register a new locale's code, native name, and direction in `i18n.js`. The language selector offers registered locales and **System language** (`auto`); dates and numbers use native `Intl` formatting. Validate with `uv run --locked node contrib/i18n.mjs` and the usual `check --changed`. Framework/catalog edits automatically select catalog tests and language regressions in Chromium and WebKit. Check mobile layouts with expanded text and use logical CSS properties for new directions. Installer and emergency module-loading messages remain English.

Locale choices follow [W3C internationalization guidance](https://www.w3.org/International/quicktips/index.en), [CLDR plural rules](https://cldr.unicode.org/index/cldr-spec/plural-rules), and the browser’s native `Intl` implementation.

## Releases

Private PRs run lint only. Public PRs also run affected backend tests and focused
Chromium regressions. Publishing a GitHub Release tests the quality gates, then
runs backend/installation tests on Linux,
Linux ARM64, macOS and WSL2, all three browser engines with accessibility checks,
and pinned native Hermes contracts. Browser tests have a two-minute per-test limit
with thread dumps. A separate process deadline leaves time to upload test logs
and resource samples before the job expires.
The pinned Hermes baseline is `205645ee424163c7b6cfc032c331c3557797497b`.
A weekly canary tests Hermes `main`; select **hermes_latest** to run it on demand.
Each Docker architecture is built and smoke-tested
before publication. Run **Actions → CI → Run workflow → full** for the non-container
suite on demand, or leave **full** off and select one **browser** for a focused run.
Local Docker checks use `.github/scripts/docker_smoke.py IMAGE`.

Update the version in `pyproject.toml` and `src/talaria/__init__.py`, run `uv lock`,
and merge to `main`. Publish a release with the matching tag, such as `v0.3.3`.
For prereleases, use `v0.4.0-rc.1` with Python version `0.4.0rc1` and mark the release
as a prerelease (alpha/beta also work). Drafts and ordinary tag pushes do not publish images.

Successful releases publish `ghcr.io/liamsmith86/talaria-webui:VERSION` for AMD64
and ARM64. The newest successful stable version also becomes `:latest`; prereleases
and older reruns cannot replace it. The `stable` source branch advances only after
the full suite and both Docker architectures pass. New installs and managed updates
follow that branch; `--branch main` explicitly opts into development builds.
Existing installations retain their configured repository and branch.
Protect `stable` with the **Release validation** status check and disable force pushes/deletion.
Existing version tags are never overwritten.
If publication fails, rerun the workflow. Package visibility is managed separately;
the workflow never changes it.

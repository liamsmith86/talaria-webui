# Contributing

Use Python 3.12+, uv, Git, Node.js 24 LTS and npm 12+. Linux and WSL2 support all three Playwright engines; occasional physical iPhone/Mac testing still matters.

```sh
uv sync --locked
npm ci
uv run --locked playwright install --with-deps chromium firefox webkit
git config --local core.hooksPath .githooks
```

If you use custom Git hooks, integrate ours instead of replacing them. Python and npm keep the seven-day dependency release cooldown; npm lifecycle scripts are disabled. Node/npm are development tools: the application uses native browser modules without a build step.

## Daily checks

```sh
uv run --locked python contrib/check.py check --changed
```

This selects checks from changes against `origin/main`, including uncommitted files. Use `--base REF` to narrow the comparison. Checks use up to four workers; set `TALARIA_TEST_WORKERS=2` to reduce load or `=0` to run serially.

- **Commit hook:** lint the staged snapshot. No runtime tests.
- **Push hook:** test the committed revision in a temporary checkout with locked dependencies. Tests follow the affected code; documentation-only changes skip runtime tests. Missing prerequisites or skipped tests fail.
- **Full suite:** `uv run --locked python contrib/check.py full` runs backend, browser, accessibility and native Hermes checks.

To prepare a push, run `check --revision HEAD`. The hook reuses matching successful results for one hour; `--fresh` forces a rerun. Cache keys include source, tests, tools and environment. Native Hermes checks always run fresh; successful local checks remain reusable even when a native check fails. Hooks are local guardrails; Git allows bypassing them.

Native contracts use a separate Hermes checkout, its `venv/bin/python3`, temporary storage and a loopback model simulator. Set `HERMES_SOURCE=/path/to/hermes-agent` or `git config --local talaria.hermesSource /path/to/hermes-agent`. Never use production sessions or credentials.

Accessibility checks need `TALARIA_AXE_PATH` pointing to `axe-core@4.13.0`'s `axe.min.js`, SHA-256 `c24f097bd2f451d4f933e8bc7d8d539f8672a2ebcb5cc9f9f3eec8ca9470a0c1`. The CI workflow contains the download command. Browser binaries follow the locked Playwright version.

## Reproducing bugs

Write a regression around the visible failure, then check both revisions:

```sh
uv run --locked python contrib/check.py prove --base BASE_COMMIT \
  tests/test_response_turns.py::test_saved_turn_can_be_recognized_when_its_user_row_is_on_an_older_page
```

Selected cases must pass now and fail before. Setup errors, skips, unchanged behavior and different lockfiles are rejected. Inspect the failure reason; a failing assertion alone does not prove a realistic bug.

Use `module_page` for isolated module/component checks that do not need app startup or Hermes; it retains the real styles, vendor scripts and browser storage APIs. Use `page` for full user journeys and login/startup coverage. Both provide a fresh browser context and report uncaught errors. Mark timer-driven `page` tests with `@pytest.mark.clock` to install clock control before startup; advance timers and wait for observable network/DOM completion. Keep actual rendering and native network-deadline checks real.

Exercise user actions and event sequences: navigate during a request, reconnect, interrupt or scroll upward. Use synchronous Playwright polling predicates; our fixtures reject async predicates that can finish before their condition becomes true. PR descriptions should distinguish reproduced fixes, preventive changes and cleanup, with validation and its limits.

The [synthetic native stream fixtures](tests/fixtures/README.md) check Hermes contract drift and replay completion, tools, interruption and provider failures through the browser.

## Quality gates

`uv run --locked python contrib/check.py lint` runs Ruff lint/format, Vulture (60%), ESLint and hook rules, ShellCheck, actionlint, catalog validation and file hygiene. Missing tools fail; `uv sync --locked` installs the Python lint tools. Locked npm tools are installed when needed and reused.

Production Python functions are limited to complexity 10, 12 branches and 50 statements. Tests and orchestration are exempt from these size limits. Prefer named operations over suppressions. Document dynamic Vulture entry points in `contrib/vulture_whitelist.py` and explain hook dependency exceptions beside the dependency array.

Hygiene checks reject malformed YAML/TOML/JSON/manifests, conflict markers, private keys and files over 1 MiB. Vendored JavaScript is excluded from ESLint. Static checks supplement behavioral tests.

## Translations

Mark English source messages with `t`, `rich`, `msg` or `n`, then extract:

```sh
uv run --locked node contrib/i18n.mjs --extract
```

Translate values in `src/talaria/static/locales/LOCALE.json`, preserving English keys and named placeholders such as `{name}`. Plurals use native `Intl.PluralRules` categories, including `other`. Catalogs contain no HTML; links and code use `rich` placeholders. Never translate Hermes messages, commands, model identifiers or paths.

Register new locales in `i18n.js`. Validate with `uv run --locked node contrib/i18n.mjs` and `check --changed`; check mobile layouts with expanded text and use logical CSS properties. Dates and numbers use native `Intl`. Installer and emergency loading messages remain English.

## Demo

`uv run --locked talaria demo` serves read-only synthetic samples at `http://127.0.0.1:8768`. It loads no private configuration and never calls Hermes. Optional `--host`, `--port` and `--public-url` configure hosting. The normal chat UI uses a tab-local transport for placeholder replies; visitor input never reaches the server. Refreshing resets the samples. Keep sample content fictional.

## Releases

Pull requests run lint, affected backend tests and focused Chromium checks. A published GitHub Release runs the full backend suite on Linux and portable installation/runtime checks on Linux ARM64, macOS and WSL2; all three browser engines with accessibility checks; native Hermes contracts; and Docker smoke tests for AMD64/ARM64. `contrib/check.py platform` selects the shared portability suite; `--full` includes general backend logic.

The native baseline is `205645ee424163c7b6cfc032c331c3557797497b`. A weekly canary tests Hermes `main`; **hermes_latest** runs it on demand. In **Actions → CI → Run workflow**, choose **full** for the complete non-container suite or a single **browser** for a focused run. Each browser is split into two balanced jobs, partitioned by individual test case. Reproduce one with `uv run --locked pytest -n 2 --dist load --maxschedchunk=1 -m browser --browser-shard 1/2` and `TALARIA_TEST_BROWSER` set to its engine. Structural accessibility runs in light and dark mode; focused contrast checks cover every palette on real controls and surfaces. Small worker queues let a failure stop the job promptly. Browser tests have per-test and process deadlines; artifacts retain complete logs when oversized console lines are shortened. Local Docker checks use `.github/scripts/docker_smoke.py IMAGE`.

Update `pyproject.toml` and `src/talaria/__init__.py`, run `uv lock`, merge to `main`, and publish the matching version tag. For example, `v0.4.0-rc.1` uses Python version `0.4.0rc1` and must be marked as a prerelease. Drafts and ordinary tag pushes do not publish images. Never overwrite version tags.

Successful releases publish `ghcr.io/liamsmith86/talaria-webui:VERSION`. Only the newest successful stable release advances `:latest` and the `stable` source branch. Installs and managed updates follow `stable`; `--branch main` opts into development builds. Existing installations retain their configured branch.

Protect `stable` with the **Talaria stable release** status from GitHub Actions and disable force pushes/deletion. Rerun failed publication workflows after resolving the cause. The container package must be public. Releases verify anonymous image access before promoting the stable source; package visibility is managed separately in GitHub.

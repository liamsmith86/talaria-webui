# Performance checks

These scripts use synthetic data and isolated Hermes simulators. They never read
your conversations or call a real model. Install the development dependencies
with `uv sync --locked`, then `uv run playwright install chromium`.

```sh
uv run python -m benchmarks.admission
uv run python -m benchmarks.browser --output test-results/browser.json --profile
uv run python -m benchmarks.browser --stress --output test-results/stress.json
uv run python -m benchmarks.network --output test-results/network.json
uv run python -m benchmarks.server
```

Run timing measurements without other tests/builds competing for CPU. Compare on
the same machine and browser. Open `.cpuprofile` files in Chrome DevTools' JavaScript
profiler. The browser script reports frame intervals, long tasks, DOM size, heap
size, and browser task/layout/style time. Its input-timer delay is a scheduling
probe, **not** an INP measurement. Synthetic input also exercises composer resizing.

## September 10, 2026 results

Representative before/after runs on Linux x86-64, Python 3.12.3, Chromium
151.0.7922.34. Desktop uses a 1440 px viewport; the slower simulation uses a 390 px
viewport and 4× CPU throttling. This is an emulation, not a physical phone benchmark.
The baseline is commit `809018b`; raw measurements are in [results.json](results.json).

| Scenario | Before | After |
| --- | ---: | ---: |
| Eight fast admissions behind a 400 ms request, same profile | 404 ms | 4.5 ms |
| Same, separate profiles | 403 ms | 4.4 ms |
| 80 KiB mixed reply, desktop p95 frame interval | 150 ms | 17 ms |
| Same reply, slower simulation p95 frame interval | 667 ms | 33 ms |
| Same reply, slower simulation browser task time | 31.9 s | 2.8 s |
| Mount 100 rich history messages, slower simulation | 710 ms | 212 ms |
| Mount 1,000 rich history messages, slower simulation | 5.9 s | 1.3 s |
| Stream with 1,000 history messages, slower simulation p95 frame | 517 ms | 50 ms |
| Single 80 KiB paragraph, slower simulation p95 frame | 167 ms | 33 ms |
| Single 40 KiB code block, slower simulation p95 frame | 467 ms | 17 ms |
| Restore eight runs, each status check taking 100 ms | 802 ms | 204 ms |
| Restore the visible run, originally last of eight | 802 ms | 101 ms |
| Cold login ready, simulated 100 ms latency / 6 Mbps / 4× CPU | 1.68 s | 1.20 s |
| Cold startup resource transfer | 515 KB | 200 KB |

Warm login on that network simulation changed from 0.37 s to 1.06 s. Assets now
revalidate using ETags, preventing stale modules after an update. Warm transfer is
about 12 KB. Fingerprinted asset URLs would be the next way to combine reliable
updates with fewer warm-start requests.

The streaming scripts append 1 KiB per update, waiting two animation frames between
updates. A fast run therefore has an intentional pacing floor. Browser task time
and frame intervals are more useful than total wall time. Histories alternate
short user messages with formatted answers containing code and tables; 1,000
messages is a stress case after loading many pages, not the normal initial page.

The replay soak publishes 96,000 events across 16 buffers. Traced Python allocations
stay at about 34 MiB across all three cycles and return to roughly 1 KiB after
cleanup. This measures buffer allocations, not whole-process RSS. The isolated
Docker server used about 37 MiB idle. Retention remains bounded at 32 channels,
each at most 1,500 events or 2 MiB of encoded payload.

## What changed

- Admission reserves capacity synchronously and releases it on success, failure,
  or cancellation. Pending requests count toward the existing global limit of 16.
  Network requests no longer hold the shared or per-connection admission lock.
- Streaming Markdown reuses completed blocks and groups of complete inline tokens
  within long paragraphs. Full-source lexing preserves references and syntax that
  changes as more text arrives. Every new HTML fragment still goes through DOMPurify.
- History VNodes and timestamp formatting are reused. Offscreen messages use CSS
  containment; their text remains in the DOM for search and accessibility. Code
  highlighting is deferred until approaching the viewport.
- Growing code blocks remain plain until complete. Code blocks of 10,000 characters
  or more retain their complete text and copy controls without syntax highlighting,
  avoiding thousands of extra spans and expensive final rendering on mobile.
- Completed responses already confirmed in Hermes have a 16-entry background
  presentation cache. Active runs, the visible response, uncertain submissions,
  unsaved replies, and image recovery state are protected from eviction. A churn
  regression reduces 100 cached 50 KB replies from 5 MB to 800 KB of retained text.
- Supporting browsers size the composer with CSS; older browsers retain the
  JavaScript fallback. Restoration checks at most four runs concurrently and puts
  the visible conversation first.
- Static assets use gzip, direct stylesheet links, and module preloads. API and
  event-stream responses are outside compression. Docker now installs the lockfile
  into a Python 3.14 environment and runs as an unprivileged user.

## Validation and limits

The full Python 3.12 suite ran with native Hermes contracts and axe accessibility
checks: 276 passed initially; the remaining installer test passed after disabling
this host's seven-day dependency-age filter, which excluded the newly updated
AnyIO release. Two additional large-block regressions were then added and passed.
Targeted WebKit and Firefox checks cover streaming, search, code highlighting,
resizing, sending, attachments, and network reconnects. Backend checks also ran
under Python 3.13.13 and the Docker image's Python 3.14.7. Wheel/sdist builds and the
Docker startup, restart, health probe, and non-root runtime were checked.

Runtime dependencies are Starlette 1.6.0, HTTPX 0.28.1, Uvicorn 0.52.4, and AnyIO
4.15.1. The lock also updates Ruff to 0.16.7. The existing Preact 10.29.8, HTM 3.1.1,
Marked 18.0.12, DOMPurify 3.4.15, Prism 1.30.0, and Inter 5.3.0 packages were already
the latest stable releases when checked; no replacement framework was needed.

Use one server process per installation. This work does not add shared coordination
for multiple workers or replicas. It preserves the eight-profile and sixteen-active-
conversation limits. Real Hermes/model latency, physical iOS/Android devices, native
macOS/Windows, ARM hardware, and megabyte-sized individual replies are outside these
measurements. Full-source Markdown lexing and DOM size still grow with the reply;
the renderer is not an unbounded-document editor.

For comparable hardware, investigate regressions above 33 ms p95 desktop frames,
50 ms p95 throttled frames, 300 ms to mount a normal 100-message page, or 2 s cold
startup on the specified simulated network. These are review budgets, not flaky
wall-clock assertions in the normal test suite.

References: [Chrome CPU throttling](https://developer.chrome.com/docs/devtools/performance/reference),
[Marked lexer/parser](https://marked.js.org/using_pro),
[CSS containment](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/content-visibility),
[field sizing](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/field-sizing),
[Starlette releases](https://starlette.dev/release-notes/),
[Python releases](https://www.python.org/downloads/).

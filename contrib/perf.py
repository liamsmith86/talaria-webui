#!/usr/bin/env python3
"""Measure how Talaria feels in Chromium, on a desktop and on an emulated phone.

    uv run --locked python contrib/perf.py                          # everything, both profiles
    uv run --locked python contrib/perf.py stream_long --profile mobile
    uv run --locked python contrib/perf.py --save base.json         # keep the numbers
    uv run --locked python contrib/perf.py --compare base.json      # show what changed

Each scenario runs the real server and UI in a fresh browser, against a deterministic
Hermes peer with synthetic content. Durations depend on the machine, so compare runs
from one machine, and gate CI on counts rather than on these times.
"""

import argparse
import asyncio
import json
import random
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import Error as BrowserError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from talaria.app import create_app  # noqa: E402
from talaria.auth import hash_password  # noqa: E402
from talaria.config import Settings  # noqa: E402
from tests.conftest import serve, wait_for_store  # noqa: E402
from tests.fake_hermes import KEY, FakeHermes  # noqa: E402

PASSWORD = "perf-password"
PROFILES = {
    "desktop": {"device": None, "cpu": 1, "network": None},
    # A mid-range phone: Chrome's "slow" CPU preset on a busy 4G connection.
    "mobile": {
        "device": "Pixel 7",
        "cpu": 4,
        "network": {"latency": 150, "down": 1.6e6 / 8, "up": 750e3 / 8},
    },
}

# Records long tasks, slow interactions and layout shifts from before the first
# paint. The frame loop only runs on request, as it would itself keep the page awake.
PROBE = """(() => {
  const probe = window.__perf = {
    long: [], events: [], shift: 0, lcp: 0, frames: null,
    startFrames() {
      probe.frames = [];
      const tick = (now) => {
        if (!probe.frames) return;
        probe.frames.push(now);
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    },
    stopFrames() {
      const frames = probe.frames || [];
      probe.frames = null;
      return frames;
    },
    drain() {
      const out = { long: probe.long, events: probe.events };
      probe.long = [];
      probe.events = [];
      return out;
    },
  };
  const watch = (options, read) => {
    try {
      new PerformanceObserver((list) => list.getEntries().forEach(read)).observe(options);
    } catch {}
  };
  watch({ type: "longtask", buffered: true }, (e) => probe.long.push(e.duration));
  watch({ type: "event", durationThreshold: 16, buffered: true }, (e) =>
    probe.events.push({ name: e.name, duration: e.duration }));
  watch({ type: "layout-shift", buffered: true }, (e) => {
    if (!e.hadRecentInput) probe.shift += e.value;
  });
  watch({ type: "largest-contentful-paint", buffered: true }, (e) => (probe.lcp = e.startTime));
})()"""

NOUNS = [
    "plan",
    "notes",
    "gateway",
    "session",
    "branch",
    "relay",
    "stream",
    "buffer",
    "client",
    "profile",
    "test",
]
VERBS = [
    "keeps",
    "checks",
    "renders",
    "replays",
    "saves",
    "folds",
    "opens",
    "merges",
    "reads",
    "writes",
]
CODE = """def summarize(rows, *, limit=20):
    seen = {}
    for row in rows:
        key = row.get("id") or row["name"]
        seen[key] = seen.get(key, 0) + row["count"]
    return sorted(seen.items(), key=lambda item: -item[1])[:limit]
"""


def sentence(rng):
    words = [rng.choice(NOUNS if i % 2 else VERBS) for i in range(rng.randint(8, 18))]
    if rng.random() < 0.3:
        words[2] = f"**{words[2]}**"
    if rng.random() < 0.3:
        words[4] = f"`{words[4]}()`"
    return " ".join(words).capitalize() + "."


def block(rng):
    kind = rng.random()
    if kind < 0.5:
        return " ".join(sentence(rng) for _ in range(rng.randint(2, 5)))
    if kind < 0.65:
        return "\n".join(f"- {sentence(rng)}" for _ in range(rng.randint(3, 6)))
    if kind < 0.8:
        return f"```python\n{CODE}```"
    if kind < 0.9:
        rows = [f"| {rng.choice(NOUNS)} | {rng.randint(1, 99)} | {sentence(rng)} |" for _ in "abcd"]
        return "\n".join(["| Name | Count | Note |", "| --- | --- | --- |", *rows])
    return f"## The {rng.choice(NOUNS)} {rng.choice(NOUNS)}"


def markdown(chars, seed=1):
    rng = random.Random(seed)
    parts, size = [], 0
    while size < chars:
        parts.append(block(rng))
        size += len(parts[-1]) + 2
    return "\n\n".join(parts)


def transcript(turns, seed=1, *, tools=1):
    """Rows as Hermes saves them: a question, its tool steps, then the answer."""
    rows = []

    def row(**fields):
        rows.append({"id": len(rows) + 1, **fields})

    for turn in range(turns):
        row(role="user", content=f"Question {turn}: what {NOUNS[turn % len(NOUNS)]} comes next?")
        for step in range(tools):
            call = f"call-{turn}-{step}"
            tool = {"id": call, "function": {"name": "terminal", "arguments": '{"command":"ls"}'}}
            row(role="assistant", content="", tool_calls=[tool])
            row(role="tool", tool_call_id=call, content=markdown(400, seed + turn))
        row(role="assistant", content=markdown(1800, seed + turn))
    return rows


class Peer(FakeHermes):
    """The fake Hermes, playing whatever reply a scenario has planned for an input."""

    def __init__(self):
        super().__init__()
        self.plans = {}

    def plan(self, name, text, *, chunk=15, delay=0.015, tools=0, tool_output=0):
        self.plans[name] = {
            "text": text,
            "chunk": chunk,
            "delay": delay,
            "tools": tools,
            "tool_output": tool_output,
        }

    def add_session(self, sid, title, rows=(), **fields):
        self.sessions[sid] = {
            "id": sid,
            "title": title,
            "source": "api_server",
            "started_at": time.time(),
            "pinned": False,
            "message_count": len(rows),
            **fields,
        }
        self.messages[sid] = list(rows)

    async def events(self, run):
        plan = self.plans.get(run["input"].strip())
        if not plan:
            async for item in super().events(run):
                yield item
            return

        def event(name, **fields):
            return (
                "data: " + json.dumps({"event": name, "run_id": run["run_id"], **fields}) + "\n\n"
            )

        for index in range(plan["tools"]):
            call = f"tool-{index}"
            yield event("tool.started", tool="terminal", tool_call_id=call, preview=f"Step {index}")
            await asyncio.sleep(plan["delay"])
            yield event(
                "tool.completed",
                tool="terminal",
                tool_call_id=call,
                duration=0.1,
                output="x" * plan["tool_output"],
            )
        text = plan["text"]
        for start in range(0, len(text), plan["chunk"]):
            if run["status"] == "cancelled" or (start and run["input"].startswith("park")):
                for _ in range(12_000):
                    if run["status"] == "cancelled" or run.get("released"):
                        break
                    await asyncio.sleep(0.05)
                if run["status"] == "cancelled":
                    yield event("run.cancelled")
                    return
            yield event("message.delta", delta=text[start : start + plan["chunk"]])
            await asyncio.sleep(plan["delay"])
        run["status"], run["output"] = "completed", text
        rows = self.messages[run["session_id"]]
        rows.append(
            {
                "id": max((row.get("id", 0) for row in rows), default=0) + 1,
                "role": "assistant",
                "content": text,
            }
        )
        yield event("run.completed", output=text, usage={"input_tokens": 9, "output_tokens": 9})


def percentile(values, share):
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * share))]


def rounded(value, places=1):
    return round(value, places)


class Bench:
    """One browser and one signed-in page, with the means to measure a stretch of work."""

    def __init__(self, runtime, profile, peer):
        self.profile = PROFILES[profile]
        self.runtime = runtime
        self.peer = peer
        self.browser = runtime.chromium.launch(channel="chromium")
        device = self.profile["device"]
        self.context = self.browser.new_context(
            **(runtime.devices[device] if device else {"viewport": {"width": 1440, "height": 960}})
        )
        self.context.add_init_script(PROBE)
        self.page = self.context.new_page()
        self.cdp = self.context.new_cdp_session(self.page)
        self.system = self.browser.new_browser_cdp_session()
        for domain in ("Performance", "Network", "HeapProfiler", "Runtime"):
            self.cdp.send(f"{domain}.enable")
        self.encoded = {}
        self.requests = []
        self.cdp.on("Network.requestWillBeSent", self._on_request)
        self.cdp.on("Network.loadingFinished", self._on_finished)

    def _on_request(self, event):
        self.requests.append(event["request"]["url"])

    def _on_finished(self, event):
        self.encoded[event["requestId"]] = event["encodedDataLength"]

    def close(self):
        self.browser.close()

    def throttle(self, *, network=True):
        self.cdp.send("Emulation.setCPUThrottlingRate", {"rate": self.profile["cpu"]})
        conditions = self.profile["network"]
        if network and conditions:
            self.cdp.send(
                "Network.emulateNetworkConditions",
                {
                    "offline": False,
                    "latency": conditions["latency"],
                    "downloadThroughput": conditions["down"],
                    "uploadThroughput": conditions["up"],
                },
            )

    def sign_in(self, url):
        self.url = url
        self.page.goto(url)
        self.page.get_by_label("Password", exact=True).fill(PASSWORD)
        self.page.get_by_role("button", name="Sign in").click()
        self.ready()

    def ready(self):
        self.page.locator(".topbar-title").or_(self.page.locator(".sidebar")).first.wait_for()
        wait_for_store(self.page, "state => !!state.defaultModel")

    def store(self, predicate):
        wait_for_store(self.page, predicate)

    def counters(self):
        return {m["name"]: m["value"] for m in self.cdp.send("Performance.getMetrics")["metrics"]}

    def cpu_seconds(self):
        totals = {}
        for item in self.system.send("SystemInfo.getProcessInfo")["processInfo"]:
            totals[item["type"]] = totals.get(item["type"], 0) + item["cpuTime"]
        return totals

    def renderer_mb(self):
        """Memory only the renderers hold, which is what the page costs the machine."""
        total = 0
        for item in self.system.send("SystemInfo.getProcessInfo")["processInfo"]:
            if item["type"] != "renderer":
                continue
            try:
                rollup = Path(f"/proc/{item['id']}/smaps_rollup").read_text()
            except OSError:
                continue
            for line in rollup.splitlines():
                if line.startswith("Pss:"):
                    total += int(line.split()[1])
        return total / 1024

    def memory(self):
        """What survives a full collection: anything here is really being kept."""
        for _ in range(2):
            self.cdp.send("HeapProfiler.collectGarbage")
        metrics = self.counters()
        return {
            "heap_mb": rounded(metrics["JSHeapUsedSize"] / 1048576, 2),
            "nodes": int(metrics["Nodes"]),
            "listeners": int(metrics["JSEventListeners"]),
            "renderer_mb": rounded(self.renderer_mb()),
        }

    @contextmanager
    def span(self, result, *, frames=True):
        """Fill result with what the page and the browser spent inside the block."""
        self.page.evaluate("() => __perf.drain()")
        if frames:
            self.page.evaluate("() => __perf.startFrames()")
        before, cpu_before, start = self.counters(), self.cpu_seconds(), time.perf_counter()
        requests, encoded = len(self.requests), sum(self.encoded.values())
        yield
        wall = time.perf_counter() - start
        after, cpu_after = self.counters(), self.cpu_seconds()
        drained = self.page.evaluate("() => __perf.drain()")

        def delta(name):
            return after[name] - before[name]

        result.update(
            wall_s=rounded(wall, 2),
            main_cpu_pct=rounded(100 * delta("TaskDuration") / wall),
            script_ms=round(delta("ScriptDuration") * 1000),
            layout_ms=round(delta("LayoutDuration") * 1000),
            style_ms=round(delta("RecalcStyleDuration") * 1000),
            layouts=int(delta("LayoutCount")),
            style_recalcs=int(delta("RecalcStyleCount")),
            long_tasks=len(drained["long"]),
            long_max_ms=round(max(drained["long"], default=0)),
            blocked_ms=round(sum(max(0, value - 50) for value in drained["long"])),
            slow_events=sum(e["duration"] > 100 for e in drained["events"]),
            inp_ms=round(max((e["duration"] for e in drained["events"]), default=0)),
            requests=len(self.requests) - requests,
            transfer_kb=round((sum(self.encoded.values()) - encoded) / 1024, 1),
        )
        if self.profile["cpu"] == 1:
            # The emulated phone's throttle burns the renderer's own CPU, so this means
            # nothing there. It's how compositor and GPU work shows up on a desktop.
            for kind in ("renderer", "GPU"):
                spent = cpu_after.get(kind, 0) - cpu_before.get(kind, 0)
                result[f"{kind.lower()}_cpu_pct"] = rounded(100 * spent / wall)
        if frames:
            stamps = self.page.evaluate("() => __perf.stopFrames()")
            gaps = [later - earlier for earlier, later in pairwise(stamps)]
            result.update(
                fps=rounded(len(gaps) / wall),
                frame_p95_ms=rounded(percentile(gaps, 0.95)),
                frame_p99_ms=rounded(percentile(gaps, 0.99)),
                frame_max_ms=round(max(gaps, default=0)),
                dropped_frames=sum(max(0, round(gap / (1000 / 60)) - 1) for gap in gaps),
            )

    def send(self, text):
        # Enter adds a line on a phone, so press Send as a person would on either device.
        self.page.get_by_label("Message Hermes").fill(text)
        self.page.get_by_role("button", name="Send message", exact=True).click()

    def wait_idle(self):
        self.store(
            "state => !Object.values(state.lives).some("
            "(live) => ['running', 'queued', 'starting'].includes(live.status))"
        )

    def open_session(self, sid):
        self.page.evaluate(
            "async (sid) => (await import('/static/store.js')).openSession(sid)", sid
        )
        self.store(f"state => state.active === {sid!r} && !state.loading && state.historyLoaded")
        self.settled()

    def swipe(self, distance, *, speed=2400):
        """A compositor-driven scroll up the transcript at a brisk speed, waiting until done.

        Chromium's synthetic touch source moves nothing under device emulation, though real
        touch events scroll fine, so both profiles use the default source.
        """
        viewport = self.page.viewport_size
        self.cdp.send(
            "Input.synthesizeScrollGesture",
            {
                "x": viewport["width"] * 0.6,
                "y": viewport["height"] * 0.5,
                "yDistance": distance,
                "speed": speed,
            },
        )

    def settled(self):
        """Wait for rendering to catch up: two frames, the second after any layout."""
        self.page.evaluate(
            "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))"
        )

    def time_to(self, action, predicate):
        """Milliseconds from an action to a state being on screen, on the page's own clock."""
        start = self.page.evaluate("() => performance.now()")
        action()
        self.store(predicate)
        self.settled()
        return self.page.evaluate("() => performance.now()") - start


def navigation(bench):
    return bench.page.evaluate("""() => {
      const nav = performance.getEntriesByType('navigation')[0];
      const paint = (name) => performance.getEntriesByName(name)[0]?.startTime || 0;
      const resources = performance.getEntriesByType('resource');
      return {
        ttfb_ms: Math.round(nav.responseStart),
        fcp_ms: Math.round(paint('first-contentful-paint')),
        lcp_ms: Math.round(__perf.lcp),
        load_ms: Math.round(nav.loadEventEnd),
        cls: Math.round(__perf.shift * 1000) / 1000,
        resources: resources.length,
        decoded_kb: Math.round(resources.reduce((a, r) => a + r.decodedBodySize, 0) / 1024),
        transfer_kb: Math.round(resources.reduce((a, r) => a + r.transferSize, 0) / 1024),
      };
    }""")


def cold_load(bench):
    """First visit with nothing cached, and a return visit, over the profile's network."""
    for index in range(40):
        bench.peer.add_session(f"s{index}", f"Session {index}")
    bench.sign_in(bench.url)
    bench.throttle()
    result = {}
    bench.cdp.send("Network.clearBrowserCache")
    start = time.perf_counter()
    bench.page.reload()
    bench.ready()
    bench.settled()
    result["cold_ready_ms"] = round((time.perf_counter() - start) * 1000)
    result.update({f"cold_{key}": value for key, value in navigation(bench).items()})
    result["cold_long_tasks"] = len(bench.page.evaluate("() => __perf.drain().long"))
    result.update(bench.memory())
    start = time.perf_counter()
    bench.page.reload()
    bench.ready()
    bench.settled()
    result["warm_ready_ms"] = round((time.perf_counter() - start) * 1000)
    result.update({f"warm_{key}": value for key, value in navigation(bench).items()})
    return result


def idle(bench):
    """A session on screen, nothing happening: this should cost close to nothing."""
    bench.peer.add_session("idle", "Idle", transcript(10))
    bench.sign_in(bench.url)
    bench.open_session("idle")
    bench.throttle(network=False)
    bench.page.wait_for_timeout(2000)  # Fixed wait: the page finishes its startup work.
    result = {}
    with bench.span(result, frames=False):
        bench.page.wait_for_timeout(30_000)  # Fixed wait: idle cost is measured over time.
    return result


def waiting(bench):
    """A reply parked mid-stream, as during a slow tool: an animation may run, nothing else."""
    bench.peer.plan("park here", "A reply that pauses here, while a tool runs for a long time.")
    bench.sign_in(bench.url)
    bench.throttle(network=False)
    bench.send("park here")
    bench.store("state => Object.values(state.lives).some((live) => live.text)")
    bench.page.wait_for_timeout(1000)  # Fixed wait: the first delta's reveal finishes.
    result = {}
    with bench.span(result, frames=False):
        bench.page.wait_for_timeout(20_000)  # Fixed wait: waiting cost is measured over time.
    for run in bench.peer.runs.values():
        run["released"] = True
    return result


def streaming(bench, name, *, rows=(), **plan):
    bench.peer.plan(name, plan.pop("text"), **plan)
    bench.peer.add_session("stream", "Stream", rows)
    bench.sign_in(bench.url)
    bench.open_session("stream")
    bench.throttle(network=False)
    start = bench.memory()
    result = {}
    with bench.span(result):
        bench.send(name)
        bench.store("state => Object.keys(state.lives).length > 0")
        bench.wait_idle()
        bench.settled()
    end = bench.memory()
    result.update({f"after_{key}": value for key, value in end.items()})
    result["heap_growth_mb"] = rounded(end["heap_mb"] - start["heap_mb"], 2)
    return result


def stream_typical(bench):
    """An ordinary answer at a typical model speed, about 80 tokens a second."""
    return streaming(bench, "typical", text=markdown(6000), chunk=12, delay=0.03)


def stream_long(bench):
    """The big file case: a 100 KB document arriving quickly."""
    return streaming(bench, "long", text=markdown(100_000), chunk=40, delay=0.01)


def stream_on_history(bench):
    """An ordinary answer below 200 saved turns, which must cost the same as without them."""
    return streaming(
        bench, "typical", rows=transcript(200), text=markdown(6000), chunk=12, delay=0.03
    )


def tool_storm(bench):
    """An agent working hard: 200 tool calls with sizeable output, then a short answer."""
    return streaming(
        bench, "storm", text=markdown(1500), chunk=30, delay=0.01, tools=200, tool_output=4000
    )


def long_session(bench):
    """Open a session with 500 turns, scroll to its start, and leave it."""
    bench.peer.add_session("long", "Long", transcript(500))
    bench.peer.add_session("short", "Short", transcript(2))
    bench.sign_in(bench.url)
    bench.open_session("short")
    bench.throttle(network=False)
    result = {}
    result["open_ms"] = round(
        bench.time_to(
            lambda: bench.page.evaluate(
                "async () => { (await import('/static/store.js')).openSession('long'); }"
            ),
            "state => state.active === 'long' && !state.loading && state.historyLoaded",
        )
    )
    result.update({f"open_{key}": value for key, value in bench.memory().items()})
    scroll = {}
    position = "() => document.querySelector('.conversation-viewport').scrollTop"
    top = bench.page.evaluate(position)
    with bench.span(scroll):
        bench.swipe(30_000)
    scroll["px"] = round(top - bench.page.evaluate(position))
    result.update({f"scroll_{key}": value for key, value in scroll.items()})
    result.update({f"scrolled_{key}": value for key, value in bench.memory().items()})
    return result


def switching(bench):
    """Move between two sessions repeatedly: quick each time, keeping nothing behind."""
    bench.peer.add_session("left", "Left", transcript(30, seed=1))
    bench.peer.add_session("right", "Right", transcript(30, seed=2))
    bench.sign_in(bench.url)
    bench.open_session("left")
    bench.throttle(network=False)
    first = bench.memory()
    times = []
    result = {}
    with bench.span(result):
        for index in range(30):
            sid = "right" if index % 2 == 0 else "left"
            times.append(
                bench.time_to(
                    lambda sid=sid: bench.page.evaluate(
                        "async (sid) => { (await import('/static/store.js')).openSession(sid); }",
                        sid,
                    ),
                    f"state => state.active === {sid!r} && !state.loading && state.historyLoaded",
                )
            )
    last = bench.memory()
    result.update(
        switch_p50_ms=round(statistics.median(times)),
        switch_p95_ms=round(percentile(times, 0.95)),
        heap_growth_mb=rounded(last["heap_mb"] - first["heap_mb"], 2),
        node_growth=last["nodes"] - first["nodes"],
        listener_growth=last["listeners"] - first["listeners"],
    )
    return result


def typing(bench):
    """Type a paragraph below a long transcript: every key has to feel instant."""
    bench.peer.add_session("typing", "Typing", transcript(100))
    bench.sign_in(bench.url)
    bench.open_session("typing")
    bench.throttle(network=False)
    box = bench.page.get_by_label("Message Hermes")
    box.focus()
    result = {}
    with bench.span(result):
        box.press_sequentially(sentence(random.Random(4)) * 4, delay=30)
    return result


def sidebar(bench):
    """A long-time user's list: 2000 sessions to load, scroll and filter."""
    for index in range(2000):
        bench.peer.add_session(f"s{index}", f"Session {index} about the {NOUNS[index % 11]}")
    bench.sign_in(bench.url)
    bench.throttle(network=False)
    result = dict(bench.memory())
    search = bench.page.get_by_label("Search sessions")
    if not search.is_visible():
        bench.page.evaluate(
            "async () => (await import('/static/store.js')).update({ sidebar: true })"
        )
    search.wait_for()
    typed = {}
    with bench.span(typed):
        search.press_sequentially("session 19", delay=60)
        search.fill("")
    result.update({f"filter_{key}": value for key, value in typed.items()})
    result.update({f"after_{key}": value for key, value in bench.memory().items()})
    return result


def repeat_replies(bench):
    """Twenty short replies in a row: a long chat must not get heavier per turn than it saves."""
    bench.peer.plan("again", markdown(1500), chunk=30, delay=0.002)
    bench.sign_in(bench.url)
    bench.send("again")
    bench.store("state => !!state.active")
    bench.wait_idle()
    bench.throttle(network=False)
    first = bench.memory()
    result = {}
    with bench.span(result):
        for index in range(1, 20):
            bench.send("again")
            bench.store(
                f"state => state.history.filter((row) => row.role === 'assistant').length > {index}"
            )
            bench.wait_idle()
    last = bench.memory()
    result.update(
        heap_growth_mb=rounded(last["heap_mb"] - first["heap_mb"], 2),
        node_growth=last["nodes"] - first["nodes"],
        listener_growth=last["listeners"] - first["listeners"],
    )
    return result


SCENARIOS = {
    scenario.__name__: scenario
    for scenario in (
        cold_load,
        idle,
        waiting,
        stream_typical,
        stream_long,
        stream_on_history,
        tool_storm,
        long_session,
        switching,
        typing,
        sidebar,
        repeat_replies,
    )
}


def run_scenario(runtime, name, profile):
    peer = Peer()
    upstream, upstream_thread, url = serve(peer.app)
    with tempfile.TemporaryDirectory() as home:
        settings = Settings(
            hermes_url=url,
            api_key=KEY,
            password_hash=hash_password(PASSWORD),
            signing_key="perf-signing-key",
        )
        server, thread, address = serve(create_app(settings, Path(home) / "config.json"))
        bench = Bench(runtime, profile, peer)
        bench.url = address
        try:
            return SCENARIOS[name](bench)
        finally:
            bench.close()
            server.should_exit = True
            thread.join(6)
            upstream.should_exit = True
            upstream_thread.join(6)


def compare_line(key, value, base):
    if base is None or base == value or not isinstance(value, int | float):
        return f"    {key:28} {value}"
    change = f" ({100 * (value - base) / base:+.0f}%)" if base else ""
    return f"    {key:28} {base} -> {value}{change}"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenarios", nargs="*", choices=[[], *SCENARIOS], default=[])
    parser.add_argument("--profile", choices=[*PROFILES, "all"], default="all")
    parser.add_argument("--repeat", type=int, default=1, help="keep each metric's median")
    parser.add_argument("--save", type=Path)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    baseline = json.loads(args.compare.read_text()) if args.compare else {}
    profiles = list(PROFILES) if args.profile == "all" else [args.profile]
    results = {}
    failed = False
    with sync_playwright() as runtime:
        for name in args.scenarios or SCENARIOS:
            for profile in profiles:
                print(f"\n{name} / {profile}", flush=True)
                try:
                    runs = [run_scenario(runtime, name, profile) for _ in range(args.repeat)]
                except BrowserError as error:
                    failed = True
                    print(f"    FAILED: {str(error).splitlines()[0]}", flush=True)
                    continue
                merged = {key: statistics.median(run[key] for run in runs) for key in runs[0]}
                results[f"{name}/{profile}"] = merged
                base = baseline.get(f"{name}/{profile}", {})
                for key, value in merged.items():
                    print(compare_line(key, value, base.get(key)), flush=True)
                if args.save:
                    args.save.write_text(json.dumps(results, indent=2) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

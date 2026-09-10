"""Repeatable synthetic client profiling; never connects to a real Hermes instance.

Run: uv run python -m benchmarks.browser --output test-results/before.json
"""

import argparse
import json
import platform
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from talaria.app import create_app
from talaria.auth import hash_password
from talaria.config import Settings
from tests.conftest import serve
from tests.fake_hermes import KEY, FakeHermes

WORKLOAD = r"""async ({historyCount, chunks, shape='mixed'}) => {
  const {state, update} = await import('/static/store.js');
  const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
  const paint = async () => { await frame(); await frame(); };
  const section = '## A useful answer\n\n' +
    'A paragraph with **emphasis**, [a link](https://example.com), and `inline code`. '.repeat(4) +
    '\n\n```javascript\n' + 'const greet = (name) => `Hello ${name}`;\n'.repeat(10) +
    '```\n\n| Column | Value |\n| --- | --- |\n| First | Second |\n\n';
  const source = shape === 'code'
    ? '```javascript\n' + 'const greeting = (name) => `Hello ${name}`;\n'.repeat(2000)
    : shape === 'paragraph' ? 'A long paragraph with **bold** and `code`. '.repeat(4000)
    : section.repeat(120);
  const answer = source.slice(0, chunks * 1024);
  const history = Array.from({length: historyCount}, (_, i) => ({
    id: i + 1, role: i % 2 ? 'assistant' : 'user',
    content: i % 2 ? section : `Question ${i}`, timestamp: 1700000000 + i,
  }));
  const sid = 'benchmark';
  const live = {id:'benchmark-run', status:'running', text:'', reasoning:'',
    tools:[], userText:'Benchmark question', userImages:[], baseHistoryLength:historyCount};
  const start = performance.now();
  update({active:sid, history, loading:false, historyHasMore:false,
    sessions:[{id:sid, title:'Benchmark', source:'api_server'}],
    sessionDetails:{id:sid, title:'Benchmark', source:'api_server'}, lives:{[sid]:live}});
  await paint();
  const historyMountMs = performance.now() - start;
  const longTasks = [];
  const observer = new PerformanceObserver(list => {
    longTasks.push(...list.getEntries().map(e=>e.duration));
  });
  observer.observe({type:'longtask'});
  let removedNodes = 0;
  const mutations = new MutationObserver(records => {
    for (const record of records) removedNodes += record.removedNodes.length;
  });
  mutations.observe(document.querySelector('.conversation-content'),
    {childList:true, subtree:true});
  const frames = [], delays = [];
  let last = performance.now(), monitoring = true;
  function tick(now) {
    frames.push(now-last); last=now; if(monitoring) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  const input = document.querySelector('textarea');
  const began = performance.now();
  for (let i=1; i<=chunks; i++) {
    const scheduled = performance.now();
    const inputTask = new Promise(resolve => setTimeout(() => {
      delays.push(performance.now()-scheduled);
      input.value = `Typing while streaming ${i}`;
      input.dispatchEvent(new Event('input', {bubbles:true}));
      resolve();
    }, 0));
    live.text = answer.slice(0, i*1024);
    update({lives:{...state.lives, [sid]:live}}, true);
    await paint();
    await inputTask;
  }
  const streamingMs = performance.now()-began;
  let completionMs;
  if(shape !== 'mixed') {
    const start=performance.now();
    live.status='completed';
    update({lives:{...state.lives,[sid]:live}});
    await paint();
    completionMs=performance.now()-start;
  }
  monitoring = false;
  await paint();
  observer.disconnect(); mutations.disconnect();
  const percentile = (values,p) =>
    [...values].sort((a,b)=>a-b)[Math.floor((values.length-1)*p)] || 0;
  return {shape, historyCount, answerBytes:answer.length, chunks, historyMountMs, streamingMs,
    completionMs,
    frameP95Ms:percentile(frames,.95), frameMaxMs:Math.max(...frames),
    inputTimerP95Ms:percentile(delays,.95), longTasks:longTasks.length,
    longTaskTotalMs:longTasks.reduce((a,b)=>a+b,0), removedNodes,
    domNodes:document.querySelectorAll('*').length,
    messages:document.querySelectorAll('.message').length};
}"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", action="store_true", help="Save Chromium CPU profiles")
    parser.add_argument("--stress", action="store_true", help="Single large code/paragraph blocks")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    results = {"platform": platform.platform(), "python": platform.python_version(), "cases": []}
    with tempfile.TemporaryDirectory(prefix="talaria-benchmark-") as folder:
        peer = FakeHermes()
        upstream, upstream_thread, upstream_url = serve(peer.app)
        settings = Settings(
            hermes_url=upstream_url,
            api_key=KEY,
            password_hash=hash_password("test-password"),
            signing_key="benchmark",
        )
        server, thread, url = serve(create_app(settings, Path(folder) / "config.json"))
        try:
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch()
                results["browser"] = browser.version
                for rate, width in ((1, 1440), (4, 390)):
                    scenarios = (
                        [(0, "code"), (0, "paragraph")]
                        if args.stress
                        else [
                            (0, "mixed"),
                            (100, "mixed"),
                            (1000, "mixed"),
                        ]
                    )
                    for count, shape in scenarios:
                        context = browser.new_context(viewport={"width": width, "height": 844})
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda error, log=errors: log.append(str(error)))
                        page.goto(url)
                        page.get_by_label("Password", exact=True).fill("test-password")
                        page.get_by_role("button", name="Step inside").click()
                        state = page.evaluate_handle(
                            "async () => (await import('/static/store.js')).state"
                        )
                        page.wait_for_function("s => s.readiness.status === 'ok'", arg=state)
                        state.dispose()
                        page.wait_for_timeout(250)
                        cdp = context.new_cdp_session(page)
                        cdp.send("Emulation.setCPUThrottlingRate", {"rate": rate})
                        cdp.send("Performance.enable")
                        if args.profile:
                            cdp.send("Profiler.enable")
                            cdp.send("Profiler.start")
                        before = {
                            m["name"]: m["value"]
                            for m in cdp.send("Performance.getMetrics")["metrics"]
                        }
                        result = page.evaluate(
                            WORKLOAD,
                            {
                                "historyCount": count,
                                "chunks": 40 if count or shape == "code" else 80,
                                "shape": shape,
                            },
                        )
                        assert not errors, errors
                        assert result["messages"] == count + 2, result
                        after = {
                            m["name"]: m["value"]
                            for m in cdp.send("Performance.getMetrics")["metrics"]
                        }
                        result.update(
                            cpuThrottle=rate,
                            width=width,
                            metrics={
                                k: after[k] - before[k]
                                for k in (
                                    "ScriptDuration",
                                    "LayoutDuration",
                                    "RecalcStyleDuration",
                                    "TaskDuration",
                                )
                            },
                            heapBytes=after["JSHeapUsedSize"],
                        )
                        if args.profile:
                            profile = cdp.send("Profiler.stop")["profile"]
                            args.output.with_name(
                                f"{args.output.stem}-{rate}x-{count}-{shape}.cpuprofile"
                            ).write_text(json.dumps(profile))
                        results["cases"].append(result)
                        print(json.dumps(result), flush=True)
                        args.output.write_text(json.dumps(results, indent=2) + "\n")
                        context.close()
                browser.close()
        finally:
            server.should_exit = True
            thread.join(6)
            upstream.should_exit = True
            upstream_thread.join(6)


if __name__ == "__main__":
    main()

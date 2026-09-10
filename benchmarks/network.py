"""Cold/warm startup on a simulated mobile connection, and run restoration latency.

Run: uv run python -m benchmarks.network --output test-results/network-before.json
"""

import argparse
import json
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from talaria.app import create_app
from talaria.auth import hash_password
from talaria.config import Settings
from tests.conftest import serve
from tests.fake_hermes import KEY, FakeHermes

RESTORE = """async () => {
  const {restoreRuns} = await import('/static/runs.js');
  const {state,update} = await import('/static/store.js');
  const {writeStorage} = await import('/static/lib.js');
  const originalFetch=window.fetch, originalSource=window.EventSource;
  const saved={};
  for(let i=0;i<8;i++) saved['restore-'+i]={id:'run-'+i, requestId:'request-'+i, userText:'Test'};
  writeStorage('runs',JSON.stringify(saved));
  update({lives:{},active:'restore-7'});
  let pending=0, peak=0, activeMs;
  const start=performance.now();
  window.EventSource=class {close(){}};
  window.fetch=async (url,options) => {
    if(!String(url).includes('/api/runs/')) return originalFetch(url,options);
    pending++; peak=Math.max(peak,pending);
    await new Promise(resolve=>setTimeout(resolve,100));
    pending--;
    if(String(url).includes('/run-7')) activeMs=performance.now()-start;
    return new Response(JSON.stringify({status:'running'}),
      {headers:{'Content-Type':'application/json'}});
  };
  try {
    await restoreRuns();
    return {eightRunsMs:performance.now()-start, activeRunMs:activeMs, peakRequests:peak};
  } finally {window.fetch=originalFetch; window.EventSource=originalSource;}
}"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    results = {"latencyMs": 100, "downloadBytesPerSecond": 750_000, "cpuThrottle": 4, "loads": []}
    with tempfile.TemporaryDirectory(prefix="talaria-network-") as folder:
        peer = FakeHermes()
        upstream, ut, upstream_url = serve(peer.app)
        app = create_app(
            Settings(
                hermes_url=upstream_url,
                api_key=KEY,
                password_hash=hash_password("test-password"),
                signing_key="benchmark",
            ),
            Path(folder) / "config.json",
        )
        server, thread, url = serve(app)
        try:
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch()
                page = browser.new_page(viewport={"width": 390, "height": 844})
                cdp = page.context.new_cdp_session(page)
                cdp.send("Network.enable")
                cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4})
                cdp.send(
                    "Network.emulateNetworkConditions",
                    {
                        "offline": False,
                        "latency": 100,
                        "downloadThroughput": 750_000,
                        "uploadThroughput": 250_000,
                    },
                )
                for cache in ("cold", "warm"):
                    start = time.perf_counter()
                    page.goto(url)
                    page.get_by_label("Password", exact=True).wait_for()
                    elapsed = (time.perf_counter() - start) * 1000
                    resources = page.evaluate(
                        "performance.getEntriesByType('resource').map(r=>({url:r.name,bytes:r.transferSize,duration:r.duration}))"
                    )
                    results["loads"].append(
                        {
                            "cache": cache,
                            "loginReadyMs": elapsed,
                            "transferBytes": sum(r["bytes"] for r in resources),
                            "requests": len(resources),
                        }
                    )
                cdp.send(
                    "Network.emulateNetworkConditions",
                    {
                        "offline": False,
                        "latency": 0,
                        "downloadThroughput": -1,
                        "uploadThroughput": -1,
                    },
                )
                cdp.send("Emulation.setCPUThrottlingRate", {"rate": 1})
                page.get_by_label("Password", exact=True).fill("test-password")
                page.get_by_role("button", name="Step inside").click()
                state = page.evaluate_handle(
                    "async () => (await import('/static/store.js')).state"
                )
                page.wait_for_function("s => s.readiness.status === 'ok'", arg=state)
                state.dispose()
                page.wait_for_timeout(250)
                results["restoration"] = page.evaluate(RESTORE)
                browser.close()
        finally:
            server.should_exit = True
            thread.join(6)
            upstream.should_exit = True
            ut.join(6)
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results), flush=True)


if __name__ == "__main__":
    main()

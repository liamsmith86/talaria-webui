"""Replay-buffer allocation soak, independent of Hermes and real conversations.

Run: uv run python -m benchmarks.server
"""

import asyncio
import gc
import json
import time
import tracemalloc

from talaria.relay import Channel


async def main():
    tracemalloc.start()
    channels = [Channel(f"synthetic-{i}") for i in range(16)]
    samples = []
    for cycle in range(3):
        start = time.perf_counter()
        for i in range(2000):
            for channel in channels:
                await channel.publish({"event": "message.delta", "delta": str(i) + "x" * 2048})
        gc.collect()
        allocated, peak = tracemalloc.get_traced_memory()
        samples.append(
            {
                "cycle": cycle + 1,
                "events": 32000,
                "seconds": time.perf_counter() - start,
                "pythonBytes": allocated,
                "peakPythonBytes": peak,
                "retainedReplayBytes": sum(c.size for c in channels),
            }
        )
        assert all(c.size <= 2 * 1024 * 1024 and len(c.events) <= 1500 for c in channels)
    del channel
    channels.clear()
    gc.collect()
    print(
        json.dumps({"cycles": samples, "releasedPythonBytes": tracemalloc.get_traced_memory()[0]})
    )


if __name__ == "__main__":
    asyncio.run(main())

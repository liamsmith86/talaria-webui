"""Native restart accounting with a synthetic API task; no services or signals."""

import asyncio
from types import SimpleNamespace

from gateway.config import Platform
from gateway.platforms.api_server import APIServerAdapter
from gateway.run_shutdown import GatewayShutdownMixin


class Runner(GatewayShutdownMixin):
    def __init__(self, adapter, timeout):
        self.adapters = {Platform.API_SERVER: adapter}
        self._running_agents = {}
        self._restart_task_started = False
        self._restart_after_turn_timeout = timeout
        self.stops = []

    def _running_agent_count(self):
        return 0

    def _active_cron_job_count(self):
        return 0

    def _wedged_agent_count(self):
        return 0

    def _scale_to_zero_status(self, *_):
        pass

    async def stop(self, **options):
        self.stops.append(options)


async def verify(drain_budget, finish):
    release = asyncio.Event()
    work = asyncio.create_task(release.wait())
    state = SimpleNamespace(
        _pending_agent_requests=0, _inflight_agent_runs=0, _active_run_tasks={"fixture": work}
    )
    adapter = SimpleNamespace(
        active_agent_work_count=lambda: APIServerAdapter.active_agent_work_count(state)
    )
    runner = Runner(adapter, drain_budget)
    try:
        assert runner._active_api_run_count() == 1
        assert runner.request_restart(via_service=True)
        assert runner._draining
        assert not runner.request_restart(via_service=True)
        if finish:
            await asyncio.sleep(0.15)
            assert not runner.stops and not work.done()
            release.set()
            await work
        await asyncio.wait_for(runner._restart_task, 3)
        assert runner.stops == [
            {"restart": True, "detached_restart": False, "service_restart": True}
        ]
        assert work.done() == finish
    finally:
        release.set()
        await work
        if not runner._restart_task.done():
            runner._restart_task.cancel()
            await asyncio.gather(runner._restart_task, return_exceptions=True)


async def main():
    await verify(2, True)
    # Respect the user's native timeout policy; do not invent an unbounded wait.
    await verify(0.01, False)


asyncio.run(main())

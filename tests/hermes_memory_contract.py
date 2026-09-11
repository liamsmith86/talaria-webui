"""Exercise Hermes's real memory lifecycle using an isolated, deterministic provider."""

import asyncio
import json
import threading
from unittest.mock import patch

from agent.memory_provider import MemoryProvider


class FixtureMemory(MemoryProvider):
    name = "talaria_contract"

    def __init__(self):
        self.initialized = None
        self.reads = []
        self.writes = []
        self.written = threading.Event()

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self.initialized = {"session_id": session_id, **kwargs}

    def get_tool_schemas(self):
        return []

    def system_prompt_block(self):
        return "CONTRACT_STATIC_MEMORY"

    def prefetch(self, query, *, session_id=""):
        self.reads.append((session_id, query))
        return "CONTRACT_RECALLED_MEMORY"

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
        self.writes.append((session_id, user_content, assistant_content))
        self.written.set()


async def verify_memory(run, db, seen, configure, home):
    providers = []

    def load(name):
        assert name == "talaria_contract"
        provider = FixtureMemory()
        providers.append(provider)
        return provider

    memories = home / "memories"
    memories.mkdir(exist_ok=True)
    (memories / "MEMORY.md").write_text("CONTRACT_BUILTIN_MEMORY")
    configure(memory={"provider": "talaria_contract", "memory_enabled": True})
    db.create_session("memory-contract", "api_server")
    with patch("plugins.memory.load_memory_provider", side_effect=load):
        for turn in (1, 2):
            prompt = f"Recall the fixture's favorite color on turn {turn}"
            await run(prompt, f"memory-{turn}", session_id="memory-contract")
            wire = json.dumps(seen[-1]["messages"])
            assert providers, "Native memory provider was not initialized"
            assert "CONTRACT_BUILTIN_MEMORY" in wire
            assert "CONTRACT_RECALLED_MEMORY" in wire, (
                providers[-1].initialized,
                providers[-1].reads,
            )
            provider = providers[-1]
            assert provider.initialized["session_id"] == "memory-contract"
            assert provider.initialized["hermes_home"] == str(home)
            assert provider.initialized["platform"] == "api_server"
            assert provider.reads == [("memory-contract", prompt)]
            assert await asyncio.to_thread(provider.written.wait, 5)
            assert provider.writes[0][0:2] == ("memory-contract", prompt)
            assert provider.writes[0][2]
    configure(debug_requests=False)

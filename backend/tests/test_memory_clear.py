import asyncio

from app.config import Settings
from app.integrations import MemoryStore
from app.memory_clear import clear_agent_memory


def test_clear_agent_memory_keeps_other_agents() -> None:
    async def run() -> None:
        memory = MemoryStore(Settings(mem0_enabled=False))
        first = await memory.add("alpha fact", "user-1", "agent-1")
        await memory.add("other agent", "user-1", "agent-2")
        await memory.add("other user", "user-2", "agent-1")

        result = await clear_agent_memory(memory, "agent-1")
        assert result["memory_remaining"] == 0
        assert await memory.get(first["id"]) is None
        assert len(await memory.get_all(agent_id="agent-2")) == 1

        await memory.reset()
        assert await memory.get_all() == []

    asyncio.run(run())

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.integrations import MemoryStore
from app.memory_scope import (
    MemoryScope,
    check_known_already,
    customer_namespace,
    extract_and_store_memories,
    prefetch_memories,
    rerank_memories,
)
from app.judgment import MemoryExtractVerdict, RetrievalPlanVerdict, RetrievalQuery


class FakeJudgment:
    def __init__(self, *, plan=None, extract=None, known=None, degraded=False) -> None:
        self.plan = plan
        self.extract = extract
        self.known = known
        self.degraded = degraded
        self.calls: list[str] = []

    async def judge(self, kind: str, payload: dict, **kwargs):
        self.calls.append(kind)
        if kind == "retrieval_plan":
            verdict = self.plan
        elif kind == "memory_extract":
            verdict = self.extract
        else:
            verdict = self.known
        return SimpleNamespace(
            verdict=verdict,
            degraded=self.degraded,
            record_id=1,
            value=lambda default="": getattr(verdict, "verdict", default),
        )


@pytest.mark.asyncio
async def test_search_scoped_includes_customer_namespace() -> None:
    memory = MemoryStore(Settings(mem0_enabled=False))
    await memory.add(
        "Customer wants invoices in Excel",
        "customer:acme",
        "1",
        {"customer_id": "acme", "category": "preference"},
    )
    await memory.add(
        "Other customer fact",
        "customer:beta",
        "1",
        {"customer_id": "beta", "category": "preference"},
    )
    hits = await memory.search_scoped(
        "invoices",
        user_id="chat-user",
        agent_id="1",
        customer_id="acme",
        include_global=True,
        limit=8,
    )
    texts = {item["memory"] for item in hits}
    assert "Customer wants invoices in Excel" in texts
    assert "Other customer fact" not in texts
    assert customer_namespace("acme") == "customer:acme"


@pytest.mark.asyncio
async def test_prefetch_uses_planned_queries_and_reranks() -> None:
    memory = MemoryStore(Settings(mem0_enabled=False))
    await memory.add("Export was confirmed", "u", "1", {"project_id": "p", "kind": "decision"})
    await memory.add("User: hi\nAssistant: hello", "u", "1", {"project_id": "p", "kind": "exchange"})
    plan = RetrievalPlanVerdict(
        verdict="planned",
        confidence=0.8,
        evidence=[],
        missing=[],
        reasoning="need decisions",
        queries=[RetrievalQuery(text="export decision", kinds=["decision"], reason="spec")],
    )
    hits = await prefetch_memories(
        memory,
        "hi",
        MemoryScope(user_id="u", agent_id="1", project_id="p"),
        limit=5,
        judgment=FakeJudgment(plan=plan),
        situation={"phase": "DISCUSSION", "message": "hi"},
    )
    assert hits[0]["memory"] == "Export was confirmed"
    assert rerank_memories(hits)[0]["metadata"]["kind"] == "decision"


@pytest.mark.asyncio
async def test_degraded_memory_fail_closed() -> None:
    memory = MemoryStore(Settings(mem0_enabled=False))
    memory.last_error = "qdrant down"
    scope = MemoryScope(user_id="u", agent_id="1", customer_id="acme")
    assert memory.degraded is True
    assert await prefetch_memories(memory, "anything", scope) == []
    stored = await extract_and_store_memories(
        memory,
        scope,
        user_text="we use 1C",
        assistant_text="ok",
        judgment=FakeJudgment(),
    )
    assert stored["degraded"] is True
    assert stored["stored"] == 0


@pytest.mark.asyncio
async def test_extract_stores_typed_facts() -> None:
    memory = MemoryStore(Settings(mem0_enabled=False))
    extract = MemoryExtractVerdict(
        verdict="extracted",
        confidence=0.8,
        evidence=[],
        missing=[],
        reasoning="new constraint",
        facts=[
            {
                "kind": "constraint",
                "text": "Must keep 1C integration",
                "subject": "1C",
                "confidence": 0.9,
                "supersedes_hint": None,
                "ttl_days": None,
            }
        ],
    )
    result = await extract_and_store_memories(
        memory,
        MemoryScope(user_id="u", agent_id="1", customer_id="acme", project_id="p"),
        user_text="оставьте 1С",
        assistant_text="запомнил",
        judgment=FakeJudgment(extract=extract),
    )
    assert result["stored"] == 1
    hits = await memory.search_scoped(
        "1C",
        user_id="u",
        agent_id="1",
        customer_id="acme",
        project_id="p",
        include_global=True,
    )
    assert any("1C" in item["memory"] or "1С" in item["memory"] for item in hits)


@pytest.mark.asyncio
async def test_known_already_forwards_payload() -> None:
    judgment = FakeJudgment(
        known=SimpleNamespace(verdict="known", answer="Excel export already agreed", model_dump=lambda: {})
    )
    result = await check_known_already(
        judgment,
        question="нужен ли экспорт?",
        spec={"in_scope": ["excel export"]},
        decisions=[{"topic": "export", "decision": "yes"}],
        memories=[{"memory": "export agreed"}],
    )
    assert result is not None
    assert "known_already" in judgment.calls

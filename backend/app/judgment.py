"""Judgment layer: LLM judges with strict structured output replace keyword heuristics.

Every semantic question the platform used to answer with word lists, regexes or
substring matching goes through a judge here. A judge returns a typed verdict with
confidence, verbatim evidence quotes and what is missing. Deterministic code keeps
only the rails on irreversible actions (dispatch to Cursor, DONE, message to the
customer) and those rails read verdicts, never text.

Modes per judge: ``off`` (legacy heuristic only), ``shadow`` (judge runs, result is
recorded and compared with the legacy heuristic, legacy still decides) and
``enforce`` (verdict decides once confidence reaches the threshold).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from .db import AgentJudgment, LlmProfile, RuntimeSettings, utcnow
from .trace import current_trace_id

logger = logging.getLogger(__name__)

JudgeMode = Literal["off", "shadow", "enforce"]
JudgeTier = Literal["cheap", "premium"]

CACHE_TTL = timedelta(hours=24)
MEMORY_CACHE_MAX = 2000
PAYLOAD_STORE_MAX_CHARS = 24_000


class JudgeUnavailable(RuntimeError):
    """The judge could not produce a valid verdict (transport, schema, no model)."""


# --------------------------------------------------------------------------- schemas


class Quote(BaseModel):
    """Verbatim fragment that supports the verdict. Never paraphrase."""

    source: str = Field(description="Where the quote comes from: customer_message, cursor_summary, spec, decision, files, verification, manager_message, memory")
    text: str = Field(description="Exact quoted fragment, copied verbatim from the source")


class BaseVerdict(BaseModel):
    verdict: str
    confidence: float = Field(ge=0.0, le=1.0, description="Calibrated probability that the verdict is right")
    evidence: list[Quote] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list, description="What information would make the verdict certain")
    reasoning: str = Field(default="", description="One or two sentences, no chain of thought dump")


class IntentVerdict(BaseVerdict):
    verdict: Literal[
        "work_request",
        "change_request",
        "bug_report",
        "question",
        "status_request",
        "approval",
        "rejection",
        "clarification_answer",
        "acknowledgement",
        "small_talk",
        "fragment",
        "operational_admin",
        "cancel",
        "other",
    ]
    is_work: bool = Field(description="True when the message asks for or changes deliverable work")
    continues_open_case: bool = Field(description="True when it clearly refers to the case described in context, not a new topic")
    wipe_scope: str | None = Field(default=None, description="For operational_admin wipe/reset orders: project slug or 'all'")
    summary: str = Field(default="", description="Neutral one-line restatement of what the sender wants")


class RoutingVerdict(BaseVerdict):
    verdict: Literal["matched", "ambiguous", "none"]
    customer_id: str | None = None
    project_id: str | None = None
    alternatives: list[str] = Field(default_factory=list, description="Other plausible customer ids when ambiguous")


class ApprovalVerdict(BaseVerdict):
    verdict: Literal["approved", "rejected", "partial", "not_an_approval", "unclear"]
    subject: Literal["spec", "cost", "slice", "development_start", "result", "other"]
    approved_by: Literal["customer", "manager", "unknown"]
    conditions: list[str] = Field(default_factory=list, description="Conditions or edits the approver attached")


class ManagerReplyVerdict(BaseVerdict):
    verdict: Literal["approved", "rejected", "answered", "unclear"]
    answer: str = Field(default="", description="The substantive answer or instruction, cleaned of pleasantries")


class ScopeVerdict(BaseVerdict):
    verdict: Literal["inside_spec", "outside_spec", "partially_inside", "spec_missing", "unclear"]
    size: Literal["trivial", "small", "medium", "large", "epic"]
    risk: Literal["low", "medium", "high"]
    outside_items: list[str] = Field(default_factory=list, description="Concrete requested items not covered by the confirmed spec")
    ready_to_execute: bool = Field(description="Goal, requirements and acceptance criteria are concrete enough to hand to an engineer")


class CriterionVerdict(BaseModel):
    criterion: str
    verdict: Literal["pass", "fail", "insufficient_evidence"]
    evidence: list[Quote] = Field(default_factory=list)
    note: str = ""


class QaVerdict(BaseVerdict):
    verdict: Literal["accept", "fix_required", "insufficient_evidence"]
    criteria: list[CriterionVerdict] = Field(default_factory=list)
    fix_request: str = Field(default="", description="Engineering-facing description of what must change when fix_required")
    customer_summary: str = Field(default="", description="Plain-language result for the customer, no internals, only when accept")


class CompletionVerdict(BaseVerdict):
    verdict: Literal["finished", "working", "needs_input", "failed", "foreign_result"]
    summary: str = Field(default="", description="What the executor reports as done, if finished")


class DeliveryVerdict(BaseVerdict):
    verdict: Literal["deliver", "hold", "redirect_to_manager"]
    reason: str = ""


class LaneMapping(BaseModel):
    lane: Literal["todo", "in_progress", "qa", "completed", "cancelled"]
    section_id: str | None = None
    section_name: str | None = None


class TrackerLaneMapVerdict(BaseVerdict):
    verdict: Literal["mapped", "partial", "unmapped"]
    lanes: list[LaneMapping] = Field(default_factory=list)


class MemoryFact(BaseModel):
    kind: Literal["preference", "constraint", "decision", "domain_fact", "person", "integration", "risk"]
    text: str = Field(description="Self-contained statement understandable without the conversation")
    subject: str = Field(default="", description="Entity the fact is about (module, person, integration)")
    confidence: float = Field(ge=0.0, le=1.0)
    supersedes_hint: str | None = Field(default=None, description="Earlier fact this one replaces, if the customer changed their mind")
    ttl_days: int | None = Field(default=None, description="Days until the fact likely goes stale; null for durable facts")


class MemoryExtractVerdict(BaseVerdict):
    verdict: Literal["extracted", "nothing_new"]
    facts: list[MemoryFact] = Field(default_factory=list)


class RetrievalQuery(BaseModel):
    text: str
    kinds: list[str] = Field(default_factory=list, description="Memory kinds worth searching for this query")
    reason: str = ""


class RetrievalPlanVerdict(BaseVerdict):
    verdict: Literal["planned", "not_needed"]
    queries: list[RetrievalQuery] = Field(default_factory=list)


class KnownAlreadyVerdict(BaseVerdict):
    verdict: Literal["known", "partially_known", "unknown"]
    answer: str = Field(default="", description="The answer as far as stored knowledge allows")


class ConsultationVerdict(BaseVerdict):
    verdict: Literal["approved", "rejected", "answered", "unclear"]
    answer: str = ""


VerdictT = TypeVar("VerdictT", bound=BaseVerdict)


# --------------------------------------------------------------------------- specs


@dataclass(frozen=True)
class JudgeSpec:
    kind: str
    schema: type[BaseVerdict]
    system: str
    tier: JudgeTier = "cheap"
    default_mode: JudgeMode = "enforce"
    default_threshold: float = 0.7
    irreversible: bool = False


_COMMON_RULES = (
    "You are a judge inside a project-management platform that mediates between a customer "
    "(usually writing in Russian) and an engineering executor. Answer ONLY with the JSON object "
    "for the schema you were given. Rules: (1) decide from meaning, never from the presence of "
    "particular words; (2) every claim in `evidence` must be a verbatim quote copied from the "
    "input, never paraphrased; (3) `confidence` is a calibrated probability, use values below 0.6 "
    "when the input is genuinely ambiguous; (4) list in `missing` what would make you certain; "
    "(5) keep `reasoning` to one or two sentences."
)

JUDGE_SPECS: dict[str, JudgeSpec] = {
    "message_intent": JudgeSpec(
        kind="message_intent",
        schema=IntentVerdict,
        system=_COMMON_RULES
        + " Task: classify one inbound message. `is_work` is true only when the sender asks for, "
        "changes or cancels deliverable work; thanks, short acknowledgements, small talk and "
        "'what next?' are not work. A short 'yes' that clearly answers a pending question is a "
        "clarification_answer or approval, not work. `fragment` means the sender is obviously mid-thought "
        "and more text is coming. `operational_admin` is only for a manager ordering the platform "
        "to reset/wipe/abort cases; then fill `wipe_scope`.",
        tier="cheap",
        default_threshold=0.7,
    ),
    "route_customer": JudgeSpec(
        kind="route_customer",
        schema=RoutingVerdict,
        system=_COMMON_RULES
        + " Task: pick which customer card the message belongs to, using the candidate list "
        "(ids, names, project slugs, notes, workspace names, recent case titles). Return `matched` "
        "only when one card clearly fits; `ambiguous` when several fit; `none` when nothing fits. "
        "Never pick a card because names share letters or a prefix.",
        tier="cheap",
        default_threshold=0.75,
    ),
    "approval_detect": JudgeSpec(
        kind="approval_detect",
        schema=ApprovalVerdict,
        system=_COMMON_RULES
        + " Task: decide whether the message is an explicit approval, rejection or partial approval "
        "of the subject described in context (the spec, a cost/price, a concrete slice of work, "
        "starting development, or a delivered result). Discussing a topic is not approving it. "
        "'Yes' only counts when the pending question in context is that approval. Fill `approved_by` "
        "from the sender role given in context, never guess from the text.",
        tier="premium",
        default_threshold=0.8,
        irreversible=True,
    ),
    "manager_reply": JudgeSpec(
        kind="manager_reply",
        schema=ManagerReplyVerdict,
        system=_COMMON_RULES
        + " Task: the manager replied to a consultation question from the agent. Decide whether "
        "they approved, rejected, answered with instructions, or were unclear. 'No, but let's try X' "
        "is `answered` with instructions, not a rejection. Put the actionable content into `answer`.",
        tier="cheap",
        default_threshold=0.75,
    ),
    "scope_judge": JudgeSpec(
        kind="scope_judge",
        schema=ScopeVerdict,
        system=_COMMON_RULES
        + " Task: compare a structured task (goal, requirements, acceptance criteria) with the "
        "customer's confirmed spec and recorded decisions. Decide whether it is inside the agreed "
        "scope, estimate size and risk for an engineer, list every requested item that the spec "
        "does not cover, and say whether it is concrete enough to execute. A whole product or a "
        "vague idea is `epic` and not ready.",
        tier="premium",
        default_threshold=0.8,
        irreversible=True,
    ),
    "qa_verifier": JudgeSpec(
        kind="qa_verifier",
        schema=QaVerdict,
        system=_COMMON_RULES
        + " Task: you are the QA gate. For EVERY acceptance criterion decide pass / fail / "
        "insufficient_evidence using only the executor's report (summary, changed files, "
        "verification rows, tests). A criterion passes only when the report shows concrete evidence "
        "it was implemented and verified; generic phrases like 'everything is done' or 'already "
        "implemented' are not evidence. `accept` only if all criteria pass. Use `fix_required` when "
        "any criterion failed or was skipped, and write a precise `fix_request`. Use "
        "`insufficient_evidence` when the report is too thin to judge. `customer_summary` must be "
        "plain language without code, file names or internal tooling.",
        tier="premium",
        default_threshold=0.85,
        irreversible=True,
    ),
    "cursor_completion": JudgeSpec(
        kind="cursor_completion",
        schema=CompletionVerdict,
        system=_COMMON_RULES
        + " Task: decide the state of the executor from machine signals (done flag, agent status, "
        "live activity, pending approvals, prompt landed, files touched, composer id vs expected) "
        "plus the visible summary text. `finished` only when signals show idle AND the summary is a "
        "closing report for THIS assignment. `foreign_result` when the report clearly belongs to "
        "another task. `needs_input` when the executor waits for a human click or a question. "
        "An idle executor with no report for this assignment is `working` (not started) or "
        "`foreign_result`, never `finished`.",
        tier="cheap",
        default_threshold=0.75,
    ),
    "delivery_gate": JudgeSpec(
        kind="delivery_gate",
        schema=DeliveryVerdict,
        system=_COMMON_RULES
        + " Task: an outbound message to the customer is about to be sent. Decide `deliver` when it "
        "is a finished result, an answer, a question or an agreed update; `hold` when it leaks "
        "internal process (tick reports, tool logs, 'handed to Cursor', intermediate progress) or "
        "claims completion that the case state does not support; `redirect_to_manager` when it is a "
        "service note meant for the manager.",
        tier="cheap",
        default_threshold=0.7,
        irreversible=True,
    ),
    "tracker_lane_map": JudgeSpec(
        kind="tracker_lane_map",
        schema=TrackerLaneMapVerdict,
        system=_COMMON_RULES
        + " Task: map the columns of a kanban board to the platform lanes todo / in_progress / qa / "
        "completed / cancelled by meaning of their names and order. Leave a lane null when no column "
        "fits; never map two lanes to one column unless the board has no better option.",
        tier="cheap",
        default_threshold=0.7,
    ),
    "memory_extract": JudgeSpec(
        kind="memory_extract",
        schema=MemoryExtractVerdict,
        system=_COMMON_RULES
        + " Task: extract durable facts worth remembering about this customer and project from one "
        "conversation exchange: preferences, constraints, decisions, domain facts, people, "
        "integrations, risks. Skip chit-chat, transient status and anything already listed in "
        "`known_facts`. Each fact must stand alone without the conversation. If the customer "
        "changed an earlier position, set `supersedes_hint` to the earlier fact.",
        tier="cheap",
        default_threshold=0.6,
    ),
    "retrieval_plan": JudgeSpec(
        kind="retrieval_plan",
        schema=RetrievalPlanVerdict,
        system=_COMMON_RULES
        + " Task: given the situation (phase, case goal, latest message), write 2-5 short search "
        "queries that would surface the memories the agent needs now: prior decisions on this "
        "topic, constraints, preferences, integrations, people. Return `not_needed` for pure "
        "small talk.",
        tier="cheap",
        default_threshold=0.5,
    ),
    "known_already": JudgeSpec(
        kind="known_already",
        schema=KnownAlreadyVerdict,
        system=_COMMON_RULES
        + " Task: the agent wants to ask the customer a question. Check the spec, recorded "
        "decisions and memories provided. `known` when they already answer it, `partially_known` "
        "when part is answered, `unknown` otherwise. Put the answer into `answer` with quotes in "
        "`evidence`.",
        tier="cheap",
        default_threshold=0.75,
    ),
}


# --------------------------------------------------------------------------- helpers


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema made compatible with strict structured outputs.

    Strict mode requires ``additionalProperties: false`` and every property listed in
    ``required`` for each object, recursively (including ``$defs``).
    """
    schema = model.model_json_schema()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                props = node.get("properties") or {}
                node["additionalProperties"] = False
                node["required"] = list(props.keys())
                for prop in props.values():
                    prop.pop("default", None)
            for key in ("properties", "$defs"):
                for child in (node.get(key) or {}).values():
                    walk(child)
            for key in ("items", "anyOf", "oneOf", "allOf"):
                child = node.get(key)
                if isinstance(child, list):
                    for entry in child:
                        walk(entry)
                elif isinstance(child, dict):
                    walk(child)
        elif isinstance(node, list):
            for entry in node:
                walk(entry)

    walk(schema)
    return schema


def payload_digest(kind: str, payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{kind}\n{blob}".encode()).hexdigest()


def _clip_payload(payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= PAYLOAD_STORE_MAX_CHARS:
        return payload
    return {"_truncated": True, "_preview": text[:PAYLOAD_STORE_MAX_CHARS]}


def _legacy_verdict_value(legacy: Any) -> str | None:
    if legacy is None:
        return None
    if isinstance(legacy, dict):
        value = legacy.get("verdict")
        return None if value is None else str(value)
    if isinstance(legacy, bool):
        return "true" if legacy else "false"
    return str(legacy)


def judge_mode_for(
    kind: str,
    runtime_settings: RuntimeSettings | None,
    project_config: dict[str, Any] | None = None,
) -> JudgeMode:
    spec = JUDGE_SPECS.get(kind)
    mode: str = spec.default_mode if spec else "shadow"
    global_modes = (getattr(runtime_settings, "judge_modes", None) or {}) if runtime_settings else {}
    if isinstance(global_modes, dict):
        mode = str(global_modes.get("*") or mode)
        mode = str(global_modes.get(kind) or mode)
    project_modes = ((project_config or {}).get("judge_modes") or {}) if project_config else {}
    if isinstance(project_modes, dict) and project_modes.get(kind):
        mode = str(project_modes[kind])
    return mode if mode in ("off", "shadow", "enforce") else "shadow"  # type: ignore[return-value]


def judge_threshold_for(
    kind: str,
    runtime_settings: RuntimeSettings | None,
    project_config: dict[str, Any] | None = None,
) -> float:
    spec = JUDGE_SPECS.get(kind)
    value: float = spec.default_threshold if spec else 0.7
    thresholds = (getattr(runtime_settings, "judge_thresholds", None) or {}) if runtime_settings else {}
    if isinstance(thresholds, dict):
        for key in ("*", kind):
            raw = thresholds.get(key)
            if raw is not None:
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    pass
    project_thresholds = ((project_config or {}).get("judge_thresholds") or {}) if project_config else {}
    if isinstance(project_thresholds, dict) and project_thresholds.get(kind) is not None:
        try:
            value = float(project_thresholds[kind])
        except (TypeError, ValueError):
            pass
    return max(0.0, min(1.0, value))


# --------------------------------------------------------------------------- result


@dataclass
class JudgmentResult:
    kind: str
    verdict: BaseVerdict | None
    mode: JudgeMode
    threshold: float
    cached: bool = False
    degraded: bool = False
    error: str | None = None
    record_id: int | None = None
    model: str | None = None
    latency_ms: int = 0
    legacy: Any = None
    agreed: bool | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.verdict is not None

    @property
    def confident(self) -> bool:
        return self.verdict is not None and float(self.verdict.confidence) >= self.threshold

    @property
    def enforce(self) -> bool:
        """The verdict is authoritative for rails: enforce mode and confident."""
        return self.mode == "enforce" and self.confident

    @property
    def active(self) -> bool:
        """Judge participates at all (shadow or enforce)."""
        return self.mode != "off"

    def value(self, default: str = "") -> str:
        return str(self.verdict.verdict) if self.verdict is not None else default

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "mode": self.mode,
            "threshold": self.threshold,
            "enforce": self.enforce,
            "confident": self.confident,
            "degraded": self.degraded,
            "cached": self.cached,
            "error": self.error,
            "record_id": self.record_id,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "agreed": self.agreed,
            "verdict": self.verdict.model_dump() if self.verdict is not None else None,
        }


# --------------------------------------------------------------------------- service


class JudgmentService:
    """Runs judges, caches verdicts, records them, and exposes mode/threshold policy."""

    def __init__(self, settings: Any, events: Any | None = None) -> None:
        self.settings = settings
        self.events = events
        self._clients: dict[str, Any] = {}
        self._models: dict[str, str] = {}
        self._runtime_settings: RuntimeSettings | None = None
        self._memory_cache: dict[str, tuple[float, dict[str, Any], dict[str, Any]]] = {}
        # AsyncSession is not safe for concurrent use: serialize DB work while LLM calls overlap.
        self._db_lock = asyncio.Lock()
        self.last_error: str | None = None
        self.turn_costs: dict[str, dict[str, int]] = {}

    # -- configuration ------------------------------------------------------------------

    async def configure(self, db: Any, runtime_settings: RuntimeSettings | None = None) -> None:
        """(Re)build judge clients from RuntimeSettings.judge_profile_id + model overrides."""
        from .integrations import LLMClient
        from .secrets import SecretStore

        if runtime_settings is None:
            runtime_settings = await db.get(RuntimeSettings, 1)
        self._runtime_settings = runtime_settings
        for client in list(self._clients.values()):
            try:
                await client.aclose()
            except Exception:
                pass
        self._clients = {}
        self._models = {}
        self.last_error = None
        self._sync_legacy_toggles()
        if runtime_settings is None or runtime_settings.judge_profile_id is None:
            return
        profile = await db.get(LlmProfile, runtime_settings.judge_profile_id)
        if profile is None or not profile.enabled:
            self.last_error = "judge profile missing or disabled"
            return
        api_key = SecretStore.from_settings(self.settings).decrypt(profile.api_key_ciphertext)
        if not api_key:
            self.last_error = "judge profile has no API key"
            return
        cheap_model = (runtime_settings.judge_model or profile.default_model or "").strip()
        premium_model = (runtime_settings.judge_premium_model or cheap_model).strip()
        for tier, model in (("cheap", cheap_model), ("premium", premium_model)):
            options: dict[str, Any] = dict(
                api_key=api_key,
                base_url=profile.base_url,
                model=model,
                max_rounds=1,
            )
            if profile.http_proxy:
                options["http_proxy"] = profile.http_proxy
            self._clients[tier] = LLMClient(**options)
            self._models[tier] = model
        self._sync_legacy_toggles()

    def bind_runtime_settings(self, runtime_settings: RuntimeSettings | None) -> None:
        if runtime_settings is not None:
            self._runtime_settings = runtime_settings
        self._sync_legacy_toggles()

    def _sync_legacy_toggles(self) -> None:
        """Cursor text heuristics stay on until an enforced judge is actually configured."""
        try:
            from .cursorremote_drive import set_text_heuristics

            enforced = self.configured and self.mode("cursor_completion") == "enforce"
            set_text_heuristics(not enforced)
        except Exception:  # pragma: no cover
            pass

    @property
    def configured(self) -> bool:
        return bool(self._clients)

    def mode(self, kind: str, project_config: dict[str, Any] | None = None) -> JudgeMode:
        return judge_mode_for(kind, self._runtime_settings, project_config)

    def threshold(self, kind: str, project_config: dict[str, Any] | None = None) -> float:
        return judge_threshold_for(kind, self._runtime_settings, project_config)

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "models": dict(self._models),
            "error": self.last_error,
            "judges": {
                kind: {
                    "mode": self.mode(kind),
                    "threshold": self.threshold(kind),
                    "tier": spec.tier,
                    "irreversible": spec.irreversible,
                }
                for kind, spec in JUDGE_SPECS.items()
            },
        }

    # -- execution -----------------------------------------------------------------------

    def _client_for(self, spec: JudgeSpec, fallback_client: Any | None) -> tuple[Any | None, str | None]:
        client = self._clients.get(spec.tier) or self._clients.get("cheap")
        if client is not None:
            return client, self._models.get(spec.tier) or self._models.get("cheap")
        if fallback_client is not None:
            return fallback_client, getattr(fallback_client, "model", None)
        return None, None

    def _cache_get(self, digest: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        entry = self._memory_cache.get(digest)
        if entry is None:
            return None
        stored_at, verdict, meta = entry
        if time.time() - stored_at > CACHE_TTL.total_seconds():
            self._memory_cache.pop(digest, None)
            return None
        return verdict, meta

    def _cache_put(self, digest: str, verdict: dict[str, Any], meta: dict[str, Any]) -> None:
        if len(self._memory_cache) >= MEMORY_CACHE_MAX:
            oldest = sorted(self._memory_cache.items(), key=lambda pair: pair[1][0])[: MEMORY_CACHE_MAX // 10]
            for key, _ in oldest:
                self._memory_cache.pop(key, None)
        self._memory_cache[digest] = (time.time(), verdict, meta)

    async def _db_cache_get(self, db: Any, kind: str, digest: str) -> AgentJudgment | None:
        if db is None:
            return None
        cutoff = utcnow() - CACHE_TTL
        try:
            return await db.scalar(
                select(AgentJudgment)
                .where(
                    AgentJudgment.kind == kind,
                    AgentJudgment.input_digest == digest,
                    AgentJudgment.error.is_(None),
                    AgentJudgment.created_at >= cutoff,
                )
                .order_by(AgentJudgment.id.desc())
                .limit(1)
            )
        except Exception as exc:  # pragma: no cover - defensive against schema drift
            logger.info("judgment cache lookup failed: %s", exc)
            return None

    async def judge(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        db: Any = None,
        agent_id: int | None = None,
        work_item_id: int | None = None,
        chat_id: Any = None,
        project_id: str | None = None,
        project_config: dict[str, Any] | None = None,
        legacy: Any = None,
        client: Any | None = None,
        use_cache: bool = True,
        force: bool = False,
    ) -> JudgmentResult:
        """Run one judge. Never raises: unavailable judges return ``degraded=True``.

        ``legacy`` is the old heuristic's answer (bool or dict with ``verdict``) used for
        shadow comparison. ``client`` is a fallback LLMClient (e.g. the agent's own) when
        no dedicated judge profile is configured.
        """
        spec = JUDGE_SPECS.get(kind)
        if spec is None:
            raise KeyError(f"unknown judge kind: {kind}")
        mode = self.mode(kind, project_config)
        threshold = self.threshold(kind, project_config)
        result = JudgmentResult(kind=kind, verdict=None, mode=mode, threshold=threshold, legacy=legacy)
        if mode == "off" and not force:
            return result
        digest = payload_digest(kind, payload)
        trace_id = current_trace_id()
        started = time.monotonic()
        verdict_dict: dict[str, Any] | None = None
        meta: dict[str, Any] = {}
        cached = False
        error: str | None = None

        if use_cache:
            hit = self._cache_get(digest)
            if hit is not None:
                verdict_dict, meta = hit
                cached = True
            else:
                async with self._db_lock:
                    row = await self._db_cache_get(db, kind, digest)
                if row is not None and isinstance(row.verdict_json, dict) and row.verdict_json:
                    verdict_dict = dict(row.verdict_json)
                    meta = {"model": row.model, "prompt_tokens": 0, "completion_tokens": 0}
                    cached = True
                    self._cache_put(digest, verdict_dict, meta)

        if verdict_dict is None:
            llm, model_name = self._client_for(spec, client)
            if llm is None:
                error = "no judge model configured"
            else:
                try:
                    text, meta = await llm.structured(
                        system=spec.system,
                        user=json.dumps(payload, ensure_ascii=False, default=str),
                        schema=strict_json_schema(spec.schema),
                        schema_name=kind,
                        model=model_name,
                    )
                    verdict_obj = spec.schema.model_validate_json(text)
                    verdict_dict = verdict_obj.model_dump()
                    self._cache_put(digest, verdict_dict, meta)
                except ValidationError as exc:
                    error = f"invalid verdict: {str(exc)[:400]}"
                except Exception as exc:
                    error = f"{type(exc).__name__}: {str(exc)[:400]}"

        latency_ms = int((time.monotonic() - started) * 1000)
        verdict_obj: BaseVerdict | None = None
        if verdict_dict is not None:
            try:
                verdict_obj = spec.schema.model_validate(verdict_dict)
            except ValidationError as exc:
                error = f"invalid cached verdict: {str(exc)[:400]}"
                verdict_obj = None

        result.verdict = verdict_obj
        result.cached = cached
        result.error = error
        result.degraded = verdict_obj is None
        result.model = meta.get("model") if meta else None
        result.latency_ms = latency_ms
        result.meta = meta or {}
        legacy_value = _legacy_verdict_value(legacy)
        if verdict_obj is not None and legacy_value is not None:
            result.agreed = str(verdict_obj.verdict).casefold() == legacy_value.casefold()
        if error:
            self.last_error = error
            logger.warning("judge.%s failed mode=%s: %s", kind, mode, error)

        self._account(kind, meta, cached)
        async with self._db_lock:
            await self._record(
                db,
                spec=spec,
                result=result,
                payload=payload,
                digest=digest,
                agent_id=agent_id,
                work_item_id=work_item_id,
                chat_id=chat_id,
                project_id=project_id,
                trace_id=trace_id,
            )
        return result

    async def judge_many(
        self,
        requests: list[tuple[str, dict[str, Any], dict[str, Any]]],
    ) -> list[JudgmentResult]:
        """Run independent judges in parallel. Each request is (kind, payload, kwargs)."""
        return list(
            await asyncio.gather(
                *(self.judge(kind, payload, **kwargs) for kind, payload, kwargs in requests)
            )
        )

    def _account(self, kind: str, meta: dict[str, Any], cached: bool) -> None:
        trace_id = current_trace_id() or "_"
        bucket = self.turn_costs.setdefault(trace_id, {"calls": 0, "cached": 0, "prompt_tokens": 0, "completion_tokens": 0})
        bucket["calls"] += 1
        if cached:
            bucket["cached"] += 1
        bucket["prompt_tokens"] += int((meta or {}).get("prompt_tokens") or 0)
        bucket["completion_tokens"] += int((meta or {}).get("completion_tokens") or 0)
        if len(self.turn_costs) > 500:
            for key in list(self.turn_costs.keys())[:100]:
                self.turn_costs.pop(key, None)

    def turn_cost(self, trace_id: str | None) -> dict[str, int]:
        return dict(self.turn_costs.get(trace_id or "_", {"calls": 0, "cached": 0, "prompt_tokens": 0, "completion_tokens": 0}))

    async def _record(
        self,
        db: Any,
        *,
        spec: JudgeSpec,
        result: JudgmentResult,
        payload: dict[str, Any],
        digest: str,
        agent_id: int | None,
        work_item_id: int | None,
        chat_id: Any,
        project_id: str | None,
        trace_id: str | None,
    ) -> None:
        if db is None:
            return
        verdict = result.verdict
        row = AgentJudgment(
            agent_id=agent_id,
            work_item_id=work_item_id,
            chat_id=str(chat_id) if chat_id not in (None, "") else None,
            project_id=project_id or None,
            decision_trace_id=trace_id,
            kind=spec.kind,
            input_digest=digest,
            payload_json=_clip_payload(payload),
            verdict=str(verdict.verdict) if verdict is not None else "",
            confidence=float(verdict.confidence) if verdict is not None else 0.0,
            verdict_json=verdict.model_dump() if verdict is not None else {},
            mode=result.mode,
            enforced=result.enforce,
            model=result.model,
            tier=spec.tier,
            prompt_tokens=int(result.meta.get("prompt_tokens") or 0),
            completion_tokens=int(result.meta.get("completion_tokens") or 0),
            latency_ms=result.latency_ms,
            cached=result.cached,
            legacy_json=(
                result.legacy
                if isinstance(result.legacy, dict)
                else ({"verdict": _legacy_verdict_value(result.legacy)} if result.legacy is not None else None)
            ),
            agreed=result.agreed,
            error=result.error,
        )
        try:
            db.add(row)
            await db.flush()
            result.record_id = row.id
        except Exception as exc:  # pragma: no cover - never break the turn on audit failure
            logger.warning("judgment record failed kind=%s: %s", spec.kind, exc)
            return
        if self.events is not None:
            try:
                await self.events.publish(
                    "judgment.recorded",
                    {
                        "id": row.id,
                        "kind": spec.kind,
                        "agent_id": agent_id,
                        "work_item_id": work_item_id,
                        "verdict": row.verdict,
                        "confidence": row.confidence,
                        "mode": row.mode,
                        "enforced": row.enforced,
                        "agreed": row.agreed,
                        "degraded": result.degraded,
                    },
                )
            except Exception:
                pass


def judgment_json(row: AgentJudgment) -> dict[str, Any]:
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "agent_id": row.agent_id,
        "work_item_id": row.work_item_id,
        "chat_id": row.chat_id,
        "project_id": row.project_id,
        "decision_trace_id": row.decision_trace_id,
        "kind": row.kind,
        "verdict": row.verdict,
        "confidence": row.confidence,
        "verdict_json": row.verdict_json or {},
        "mode": row.mode,
        "enforced": row.enforced,
        "model": row.model,
        "tier": row.tier,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "latency_ms": row.latency_ms,
        "cached": row.cached,
        "legacy_json": row.legacy_json,
        "agreed": row.agreed,
        "error": row.error,
        "overridden_by": row.overridden_by,
        "override_verdict": row.override_verdict,
        "payload_json": row.payload_json or {},
    }

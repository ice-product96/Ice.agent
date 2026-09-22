"""Outbound-to-customer rail: delivery_gate judge, not the Cursor-ready heuristic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .action_reports import (
    cursor_result_ready_for_customer,
    is_admin_peer,
    is_customer_origin_peer,
    is_internal_execution,
    pm_accept_succeeded,
    should_redirect_customer_outbound,
)
from .judgment import DeliveryVerdict, JudgmentResult, JudgmentService


@dataclass
class DeliveryDecision:
    redirect: bool
    verdict: str
    reason: str
    legacy: bool = False
    judgment: JudgmentResult | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "redirect": self.redirect,
            "verdict": self.verdict,
            "reason": self.reason,
            "legacy": self.legacy,
            "judgment": self.judgment.as_dict() if self.judgment is not None else None,
        }


def _legacy_redirect(
    context: dict[str, Any] | None,
    audit: list[dict[str, Any]] | None,
    entity: Any,
    admin_ids: set[int] | None,
) -> bool:
    return should_redirect_customer_outbound(
        context, audit, entity, admin_ids=admin_ids
    )


async def decide_customer_delivery(
    *,
    text: str,
    context: dict[str, Any] | None,
    audit: list[dict[str, Any]] | None,
    entity: Any,
    admin_ids: set[int] | None = None,
    judgment: JudgmentService | None = None,
    work_item: Any = None,
    client: Any | None = None,
    db: Any = None,
) -> DeliveryDecision:
    legacy = _legacy_redirect(context, audit, entity, admin_ids)
    ctx = context or {}
    if not is_internal_execution(ctx):
        return DeliveryDecision(redirect=False, verdict="not_internal", reason="", legacy=True)
    if is_admin_peer(entity, admin_ids):
        return DeliveryDecision(redirect=False, verdict="admin_peer", reason="", legacy=True)

    service = judgment
    if service is None:
        service = JudgmentService(settings=None)
    project_config = None
    if work_item is not None:
        project_config = (getattr(work_item, "context_json", None) or {}).get("_project_config")
    mode = service.mode("delivery_gate", project_config if isinstance(project_config, dict) else None)
    if mode == "off" or (not service.configured and client is None):
        return DeliveryDecision(
            redirect=legacy,
            verdict="legacy_redirect" if legacy else "legacy_deliver",
            reason="delivery heuristic",
            legacy=True,
        )

    payload = {
        "text": (text or "")[:3000],
        "pm_accept_succeeded": pm_accept_succeeded(audit),
        "cursor_result_ready": cursor_result_ready_for_customer(
            audit, cursor_was_in_flight=bool(ctx.get("_cursor_was_in_flight"))
        ),
        "is_customer_origin": is_customer_origin_peer(entity, ctx, admin_ids),
        "pm_mode": bool(ctx.get("_pm_mode")),
        "phase": getattr(work_item, "pm_phase", None),
        "status": getattr(work_item, "status", None),
        "source": ctx.get("source"),
    }
    result = await service.judge(
        "delivery_gate",
        payload,
        db=db,
        agent_id=getattr(work_item, "agent_id", None),
        work_item_id=getattr(work_item, "id", None),
        chat_id=ctx.get("chat_id"),
        project_id=getattr(work_item, "project_id", None),
        legacy={"verdict": "redirect_to_manager" if legacy else "deliver"},
        client=client,
    )
    verdict = result.verdict if isinstance(result.verdict, DeliveryVerdict) else None
    if mode == "shadow" or verdict is None or not result.confident:
        return DeliveryDecision(
            redirect=legacy,
            verdict="legacy_redirect" if legacy else "legacy_deliver",
            reason=verdict.reason if verdict is not None else "delivery heuristic (shadow/degraded)",
            legacy=True,
            judgment=result,
        )
    redirect = verdict.verdict in {"hold", "redirect_to_manager"}
    return DeliveryDecision(
        redirect=redirect,
        verdict=verdict.verdict,
        reason=verdict.reason or verdict.reasoning,
        judgment=result,
    )

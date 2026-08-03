"""Explicit, auditable model routes for bounded ApplyPilot tasks."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ModelRoute:
    """Requested route metadata; observed execution is logged separately."""

    task_class: str
    surface: str
    requested_model: str
    requested_effort: str
    routing_reason: str
    service_tier: str = "default"
    fallback_model: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


APPLY_FIELD_ROUTE = ModelRoute(
    task_class="schema_constrained_field_resolution",
    surface="codex_cli",
    requested_model="gpt-5.6-terra",
    requested_effort="medium",
    routing_reason="bounded truth-sensitive judgment after deterministic resolution",
)

APPLY_SUPERVISOR_ROUTE = ModelRoute(
    task_class="apply_harness_review",
    surface="codex_cli",
    requested_model="gpt-5.6-terra",
    requested_effort="high",
    routing_reason="bounded cross-check of application evidence and controller state",
)

DEV_WORKER_ROUTE = ModelRoute(
    task_class="mechanically_validated_implementation",
    surface="codex_cli",
    requested_model="gpt-5.6-luna",
    requested_effort="medium",
    routing_reason="localized work with declared files and deterministic validation",
    fallback_model="gpt-5.6-terra",
)

DEV_REVIEWER_ROUTE = ModelRoute(
    task_class="bounded_change_review",
    surface="codex_cli",
    requested_model="gpt-5.6-terra",
    requested_effort="high",
    routing_reason="integration judgment across proposal, guardrails, and test evidence",
)

CHATGPT_WEB_DISCOVERY_ROUTE = ModelRoute(
    task_class="semantic_live_role_research",
    surface="chatgpt_web",
    requested_model="",
    requested_effort="",
    routing_reason="send the bounded prompt to ChatGPT Web; model selection is unavailable",
    service_tier="",
)

CHATGPT_WEB_MATERIALS_ROUTE = ModelRoute(
    task_class="evidence_bound_role_materials",
    surface="chatgpt_web",
    requested_model="",
    requested_effort="",
    routing_reason="send the evidence-bound prompt to ChatGPT Web; model selection is unavailable",
    service_tier="",
)


def chatgpt_web_route(stage: str) -> ModelRoute:
    if stage == "discovery":
        return CHATGPT_WEB_DISCOVERY_ROUTE
    if stage == "materials":
        return CHATGPT_WEB_MATERIALS_ROUTE
    raise ValueError(f"unsupported ChatGPT Web route stage: {stage}")

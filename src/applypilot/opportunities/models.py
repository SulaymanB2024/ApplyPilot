"""Company-level opportunity contracts, intentionally separate from job candidates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any


class OpportunitySignal(StrEnum):
    RECENT_FUNDING = "recent_funding"
    FINANCING_NOTICE = "financing_notice"
    ACTIVELY_HIRING = "actively_hiring"
    GENERAL_GROWTH = "general_growth"


class OpportunityRoute(StrEnum):
    POSTED_JOB = "posted_job"
    GENERAL_INTEREST_APPLICATION = "general_interest_application"
    SPECULATIVE_OUTREACH = "speculative_outreach"


class OpportunityStatus(StrEnum):
    OBSERVED = "observed"
    NEEDS_CORROBORATION = "needs_corroboration"
    VERIFIED = "verified"
    REJECTED = "rejected"
    DRAFT_READY = "draft_ready"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    AUTHORIZED = "authorized"
    QUEUED = "queued"
    SEND_ATTEMPTED = "send_attempted"
    PROVIDER_ACCEPTED = "provider_accepted"
    SUBMITTED = "submitted"
    SEND_STATE_UNKNOWN = "send_state_unknown"
    SENT = "sent"
    DELIVERED = "delivered"
    BOUNCED = "bounced"
    REPLIED = "replied"


@dataclass(frozen=True)
class OpportunityEvidence:
    evidence_type: str
    source_url: str
    source_title: str
    publisher: str
    observed_at: str
    event_date: str = ""
    is_primary: bool = False
    claim: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpportunityLead:
    lead_id: str
    company_name: str
    company_url: str
    company_domain: str
    signal: OpportunitySignal
    route: OpportunityRoute
    status: OpportunityStatus
    evidence: tuple[OpportunityEvidence, ...]
    signal_date: str = ""
    funding_stage: str | None = None
    funding_amount: str | None = None
    careers_url: str = ""
    posted_job_url: str = ""
    general_application_url: str = ""
    open_role_count: int | None = None
    fit_hypothesis: str = ""
    contact_route: str = ""
    contact_evidence: tuple[OpportunityEvidence, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["signal"] = self.signal.value
        payload["route"] = self.route.value
        payload["status"] = self.status.value
        payload["evidence"] = [item.to_dict() for item in self.evidence]
        return payload

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()

    def with_status(self, status: OpportunityStatus) -> OpportunityLead:
        return replace(self, status=status)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OpportunityLead:
        evidence = payload.get("evidence")
        if not isinstance(evidence, list):
            raise ValueError("opportunity evidence must be a list")
        return cls(
            lead_id=str(payload.get("lead_id") or ""),
            company_name=str(payload.get("company_name") or ""),
            company_url=str(payload.get("company_url") or ""),
            company_domain=str(payload.get("company_domain") or ""),
            signal=OpportunitySignal(str(payload.get("signal") or "")),
            route=OpportunityRoute(str(payload.get("route") or "")),
            status=OpportunityStatus(str(payload.get("status") or "observed")),
            evidence=tuple(OpportunityEvidence(**item) for item in evidence),
            signal_date=str(payload.get("signal_date") or ""),
            funding_stage=(
                str(payload["funding_stage"]) if payload.get("funding_stage") is not None else None
            ),
            funding_amount=(
                str(payload["funding_amount"])
                if payload.get("funding_amount") is not None
                else None
            ),
            careers_url=str(payload.get("careers_url") or ""),
            posted_job_url=str(payload.get("posted_job_url") or ""),
            general_application_url=str(payload.get("general_application_url") or ""),
            open_role_count=(
                int(payload["open_role_count"])
                if payload.get("open_role_count") is not None
                else None
            ),
            fit_hypothesis=str(payload.get("fit_hypothesis") or ""),
            contact_route=str(payload.get("contact_route") or ""),
            contact_evidence=tuple(
                OpportunityEvidence(**item) for item in (payload.get("contact_evidence") or [])
            ),
        )


@dataclass(frozen=True)
class OpportunityDecision:
    status: OpportunityStatus
    reasons: tuple[str, ...]
    signal_date: str = ""
    score: int | None = None
    components: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload

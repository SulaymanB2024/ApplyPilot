"""Pure data contracts for the autonomous application funnel."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class Decision(StrEnum):
    """Machine-readable decision emitted by deterministic gates."""

    ACCEPT = "accept"
    REVIEW = "review"
    REJECT = "reject"


@dataclass(frozen=True)
class DateWindow:
    """Inclusive date range used for availability checks."""

    start: date
    end: date
    label: str = ""

    def overlaps(self, other: DateWindow) -> bool:
        return self.start <= other.end and other.start <= self.end


@dataclass(frozen=True)
class CandidateProfile:
    """Small factual profile used before any model or browser call."""

    graduation_month: int | None = None
    graduation_year: int | None = None
    max_required_experience_years: int = 2
    target_levels: tuple[str, ...] = (
        "intern",
        "internship",
        "junior",
        "entry level",
        "new grad",
        "graduate",
        "analyst",
        "associate",
        "0-2 years",
    )
    excluded_levels: tuple[str, ...] = (
        "senior",
        "staff",
        "principal",
        "director",
        "vice president",
        "vp",
        "head of",
    )
    preferred_locations: tuple[str, ...] = ("remote", "united states", "austin", "texas")
    commitments: tuple[DateWindow, ...] = ()


@dataclass(frozen=True)
class RoleCandidate:
    """Candidate role proposed by ChatGPT Web or a recorded fallback."""

    company: str
    title: str
    official_url: str
    source: str = "chatgpt_web"
    location: str = ""
    description: str = ""
    required_experience_min: int | None = None
    required_experience_max: int | None = None
    posted_date: date | None = None
    start_window: DateWindow | None = None
    evidence: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def candidate_id(self) -> str:
        parsed = urlsplit(self.official_url.strip())
        query = urlencode(
            sorted(
                (key, value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                if not key.lower().startswith("utm_")
                and key.lower() not in {"gh_src", "ref", "referrer", "source"}
            )
        )
        payload = urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                query,
                parsed.fragment,
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class GateDecision:
    """Decision plus stable reason codes and bounded evidence."""

    decision: Decision
    reason_codes: tuple[str, ...]
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class FreshnessEvidence:
    """Observed first-party status for one role."""

    official_url: str
    fetched_at: datetime
    first_party: bool
    resolved: bool
    open_state: bool | None
    posted_date: date | None = None
    updated_date: date | None = None
    start_window: DateWindow | None = None
    status_code: int | None = None
    title: str = ""
    description: str = ""
    evidence: tuple[str, ...] = ()
    provider_error: str = ""

    @classmethod
    def now(cls, **kwargs: Any) -> FreshnessEvidence:
        return cls(fetched_at=datetime.now(timezone.utc), **kwargs)


@dataclass(frozen=True)
class ApplicantClaim:
    """One exact prose assertion supported only by applicant evidence ids."""

    text: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class MaterialParagraph:
    """One model-written paragraph tied to factual evidence ids."""

    text: str
    evidence_ids: tuple[str, ...]
    applicant_claims: tuple[ApplicantClaim, ...] = ()


@dataclass(frozen=True)
class MaterialPacket:
    """Reviewable local material packet; never an external send."""

    candidate_id: str
    paragraphs: tuple[MaterialParagraph, ...]
    verification_gaps: tuple[str, ...] = ()
    artifact_paths: dict[str, str] = field(default_factory=dict, compare=False)
    derived_applicant_claim_count: int = 0

    @property
    def cover_letter(self) -> str:
        return "\n\n".join(paragraph.text.strip() for paragraph in self.paragraphs if paragraph.text.strip())

    @property
    def digest(self) -> str:
        payload = {
            "candidate_id": self.candidate_id,
            "paragraphs": [asdict(paragraph) for paragraph in self.paragraphs],
            "verification_gaps": list(self.verification_gaps),
        }
        if self.derived_applicant_claim_count:
            payload["derived_applicant_claim_count"] = self.derived_applicant_claim_count
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class AuthorizationGrant:
    """Narrow, expiring authorization for irreversible actions."""

    run_id: str
    candidate_id: str
    allowed_actions: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    fact_digest: str
    context_digest: str
    policy_digest: str
    packet_digest: str
    form_review_digest: str
    grant_id: str

    def permits(
        self,
        *,
        run_id: str,
        candidate_id: str,
        action: str,
        fact_digest: str,
        context_digest: str,
        policy_digest: str,
        packet_digest: str,
        form_review_digest: str,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(timezone.utc)
        return (
            self.run_id == run_id
            and self.candidate_id == candidate_id
            and action in self.allowed_actions
            and self.fact_digest == fact_digest
            and self.context_digest == context_digest
            and self.policy_digest == policy_digest
            and self.packet_digest == packet_digest
            and self.form_review_digest == form_review_digest
            and bool(self.grant_id)
            and self.issued_at <= current <= self.expires_at
        )


@dataclass
class BatchResult:
    """Serializable result ledger for one bounded autonomous batch."""

    run_id: str
    status: str
    pending_requests: list[dict[str, Any]] = field(default_factory=list)
    source_attempts: list[dict[str, Any]] = field(default_factory=list)
    discoveries: list[dict[str, Any]] = field(default_factory=list)
    eligibility: list[dict[str, Any]] = field(default_factory=list)
    freshness: list[dict[str, Any]] = field(default_factory=list)
    materials: list[dict[str, Any]] = field(default_factory=list)
    form_reviews: list[dict[str, Any]] = field(default_factory=list)
    final_actions: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

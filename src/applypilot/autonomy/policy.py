"""Deterministic source, eligibility, freshness, and action policies."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from applypilot.autonomy.models import (
    AuthorizationGrant,
    CandidateProfile,
    Decision,
    FreshnessEvidence,
    GateDecision,
    RoleCandidate,
)


@dataclass(frozen=True)
class SourcePolicy:
    """Primary and fallback source contract.

    Broad aggregators remain disabled even when ChatGPT Web fails. The only
    default fallback is direct first-party ATS discovery, and it requires a
    recorded primary failure.
    """

    primary: str = "chatgpt_web"
    disabled: tuple[str, ...] = (
        "linkedin",
        "indeed",
        "jobspy",
        "glassdoor",
        "zip_recruiter",
        "google_jobs",
        "aggregator",
    )
    fallbacks: tuple[str, ...] = ("direct_ats",)
    require_recorded_primary_failure: bool = True


@dataclass(frozen=True)
class FunnelBudget:
    """Hard caps for one autonomous run.

    Discovery is intentionally broad enough to build a useful candidate set.
    The funnel still narrows before material generation and form inspection,
    and final external actions remain governed by separate authorization.
    """

    discoveries: int = 30
    first_party_verifications: int = 15
    material_packets: int = 5
    form_dry_runs: int = 3
    model_calls: int = 8
    browser_navigations: int = 32
    external_calls: int = 50
    retries: int = 6
    artifacts: int = 40
    prompt_chars: int = 60_000
    response_chars: int = 160_000
    elapsed_seconds: int = 0
    no_progress_cycles: int = 3

    def validate(self) -> None:
        for name, value in asdict(self).items():
            if value < 0:
                raise ValueError(f"budget {name} must be non-negative")
        if not (
            self.form_dry_runs <= self.material_packets
            <= self.first_party_verifications
            <= self.discoveries
        ):
            raise ValueError("funnel budgets must narrow from discovery to form dry run")


@dataclass(frozen=True)
class RunPolicy:
    """Immutable behavior contract for a batch."""

    version: str = "applypilot-autonomy-v1"
    review_only: bool = True
    source: SourcePolicy = field(default_factory=SourcePolicy)
    budget: FunnelBudget = field(default_factory=FunnelBudget)
    allow_nested_model_processes: bool = False
    require_explicit_submit_authorization: bool = True
    max_post_age_days: int = 180

    def validate(self) -> None:
        self.budget.validate()
        if self.source.primary in self.source.disabled:
            raise ValueError("primary source cannot also be disabled")
        if set(self.source.fallbacks) & set(self.source.disabled):
            raise ValueError("fallback sources cannot be disabled")

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class SourceAttempt:
    source: str
    status: str
    reason: str = ""


def authorize_source(
    requested: str,
    *,
    policy: SourcePolicy,
    attempts: Iterable[SourceAttempt] = (),
) -> GateDecision:
    """Authorize a discovery source without silently broadening policy."""
    normalized = requested.strip().lower()
    if normalized in policy.disabled:
        return GateDecision(Decision.REJECT, ("source_disabled",), (normalized,))
    if normalized == policy.primary:
        return GateDecision(Decision.ACCEPT, ("primary_source",), (normalized,))
    if normalized not in policy.fallbacks:
        return GateDecision(Decision.REJECT, ("source_not_allowlisted",), (normalized,))
    primary_failed = any(
        attempt.source == policy.primary and attempt.status == "failed" for attempt in attempts
    )
    if policy.require_recorded_primary_failure and not primary_failed:
        return GateDecision(Decision.REJECT, ("primary_failure_not_recorded",), (normalized,))
    return GateDecision(Decision.ACCEPT, ("audited_fallback",), (normalized,))


def eligibility_gate(candidate: RoleCandidate, profile: CandidateProfile) -> GateDecision:
    """Reject clearly ineligible roles before browser or material work."""
    title = _normalize(candidate.title)
    body = _normalize(f"{candidate.title} {candidate.description}")
    reasons: list[str] = []
    evidence: list[str] = []

    if any(_phrase(title, marker) for marker in profile.excluded_levels):
        reasons.append("senior_title")
        evidence.append(candidate.title)

    required_min = candidate.required_experience_min
    inferred_required_min = _inferred_required_experience_min(body)
    if inferred_required_min is not None:
        required_min = max(required_min or 0, inferred_required_min)
    if required_min is not None and required_min > profile.max_required_experience_years:
        reasons.append("experience_requirement_exceeds_profile")
        evidence.append(f"required_min={required_min}")

    if candidate.start_window:
        conflicts = [
            commitment.label or f"{commitment.start.isoformat()}..{commitment.end.isoformat()}"
            for commitment in profile.commitments
            if commitment.overlaps(candidate.start_window)
        ]
        if conflicts:
            reasons.append("known_availability_conflict")
            evidence.extend(conflicts)

    if reasons:
        return GateDecision(Decision.REJECT, tuple(reasons), tuple(evidence))

    target_level = any(_phrase(body, marker) for marker in profile.target_levels)
    if not target_level and required_min is None:
        return GateDecision(
            Decision.REVIEW,
            ("level_or_experience_ambiguous",),
            (candidate.title,),
        )

    location = _normalize_location(candidate.location)
    if location and profile.preferred_locations and not any(
        _phrase(location, _normalize_location(preferred))
        for preferred in profile.preferred_locations
    ):
        return GateDecision(Decision.REVIEW, ("location_outside_preferences",), (candidate.location,))

    return GateDecision(Decision.ACCEPT, ("eligible_entry_level",), (candidate.title,))


def freshness_gate(
    evidence: FreshnessEvidence,
    *,
    today: date | None = None,
    max_post_age_days: int = 180,
) -> GateDecision:
    """Require authoritative open-state evidence before material generation."""
    current = today or datetime.now(timezone.utc).date()
    if evidence.provider_error:
        return GateDecision(Decision.REVIEW, ("provider_error",), (evidence.provider_error[:160],))
    if not evidence.first_party:
        return GateDecision(Decision.REJECT, ("not_first_party",), (evidence.official_url,))
    if not evidence.resolved or (evidence.status_code is not None and evidence.status_code >= 400):
        return GateDecision(Decision.REJECT, ("official_url_unresolved",), (evidence.official_url,))
    if evidence.open_state is False:
        return GateDecision(Decision.REJECT, ("posting_closed",), evidence.evidence)
    if evidence.start_window and evidence.start_window.end < current:
        return GateDecision(
            Decision.REJECT,
            ("start_window_elapsed",),
            (evidence.start_window.end.isoformat(),),
        )
    if evidence.open_state is None:
        return GateDecision(Decision.REVIEW, ("open_state_ambiguous",), evidence.evidence)

    observed_date = evidence.updated_date or evidence.posted_date
    if observed_date and (current - observed_date).days > max_post_age_days:
        if not evidence.start_window or evidence.start_window.start <= current:
            return GateDecision(
                Decision.REVIEW,
                ("potentially_stale_or_zombie",),
                (observed_date.isoformat(),),
            )
    if not observed_date and not evidence.start_window:
        return GateDecision(Decision.REVIEW, ("freshness_dates_missing",), evidence.evidence)
    return GateDecision(Decision.ACCEPT, ("first_party_open_and_plausible",), evidence.evidence)


def require_authorization(
    grant: AuthorizationGrant | None,
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
) -> None:
    """Raise unless an exact, unexpired action grant is present."""
    if grant is None or not grant.permits(
        run_id=run_id,
        candidate_id=candidate_id,
        action=action,
        fact_digest=fact_digest,
        context_digest=context_digest,
        policy_digest=policy_digest,
        packet_digest=packet_digest,
        form_review_digest=form_review_digest,
        now=now,
    ):
        raise PermissionError(
            f"explicit scoped authorization required for {action} on candidate {candidate_id}"
        )


def apply_ephemeral_overrides(base: dict[str, Any], overrides: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return a deep-copied run config and audit digest without writing files."""
    merged = deepcopy(base)
    _deep_merge(merged, deepcopy(overrides))
    digest = hashlib.sha256(
        json.dumps(merged, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return merged, digest


def assert_no_recursive_model_command(argv: Iterable[str]) -> None:
    """Reject nested Codex/Claude execution from the production batch."""
    command = [str(part) for part in argv]
    if not command:
        return
    executable = Path(command[0]).name.lower()
    if executable in {"codex", "claude"}:
        raise RuntimeError(f"nested model process is forbidden: {executable}")
    joined = " ".join(command).lower()
    if re.search(r"(^|\s)(codex|claude)\s+(exec|-p)(\s|$)", joined):
        raise RuntimeError("nested model process is forbidden")


def _deep_merge(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _inferred_required_experience_min(text: str) -> int | None:
    """Extract only explicit experience-floor phrases; false negatives are safer than guesses."""
    patterns = (
        r"\b(\d{1,2})\s*\+\s*years?\b",
        r"\b(?:at least|minimum(?: of)?|over)\s+(\d{1,2})\s+years?\b",
        r"\b(\d{1,2})\s+(?:or more)\s+years?\b",
        r"\b(\d{1,2})\s+years?\s+of\s+(?:professional\s+|relevant\s+)?experience\b",
    )
    values = [
        int(match.group(1))
        for pattern in patterns
        for match in re.finditer(pattern, text)
    ]
    return max(values) if values else None


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _normalize_location(value: str) -> str:
    normalized = _normalize(value)
    normalized = re.sub(r"\bu s a?\b", "united states", normalized)
    aliases = {
        "tx": "texas",
        "us": "united states",
        "usa": "united states",
    }
    return " ".join(aliases.get(token, token) for token in normalized.split())


def _phrase(haystack: str, needle: str) -> bool:
    normalized = _normalize(needle)
    return bool(normalized and re.search(rf"\b{re.escape(normalized)}\b", haystack))

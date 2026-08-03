"""Bounded browser-research contracts and deterministic opportunity verification."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from applypilot.autonomy.handoff import (
    HANDOFF_SCHEMA_VERSION,
    RunBindings,
    _active_handoffs_unlocked,
    _canonical_json,
    _handoff_queue_lock,
    _pending_for_active,
    _write_immutable_json,
)
from applypilot.autonomy.models import RoleCandidate
from applypilot.observability.events import EventJournal
from applypilot.opportunities.models import (
    OpportunityDecision,
    OpportunityEvidence,
    OpportunityLead,
    OpportunityRoute,
    OpportunitySignal,
    OpportunityStatus,
)

OPPORTUNITY_RESEARCH_SCHEMA_VERSION = "applypilot.opportunity-research.v1"
OPPORTUNITY_RESOURCE_LOCK = "authenticated_browser"
MAX_LEADS = 25
MAX_NAVIGATIONS = 60
MAX_SECONDS = 300
_SAFE_ID = re.compile(r"^[a-zA-Z0-9_.:-]{1,120}$")
_ATS_HOSTS = frozenset(
    {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "jobs.ashbyhq.com",
        "jobs.lever.co",
        "jobs.smartrecruiters.com",
    }
)
_ATS_SUFFIXES = (".myworkdayjobs.com", ".myworkdaysite.com", ".myworkday.com")
_PRIMARY_FUNDING_TYPES = frozenset({"company_announcement", "investor_announcement"})


def canonicalize_url(value: str) -> str:
    """Canonicalize one public HTTP(S) URL without fetching it."""
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("opportunity evidence URL must be public HTTP(S)")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or "." not in host:
        raise ValueError("opportunity evidence URL host is invalid")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("opportunity evidence URL host is not public")
    port = parsed.port
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def canonical_domain(value: str) -> str:
    candidate = value.strip().lower().rstrip(".")
    if "://" in candidate:
        candidate = (urlsplit(canonicalize_url(candidate)).hostname or "").lower()
    if candidate.startswith("www."):
        candidate = candidate[4:]
    if not candidate or "." not in candidate or any(char.isspace() for char in candidate):
        raise ValueError("company domain is invalid")
    return candidate


def opportunity_lead_id(company_domain: str, signal: OpportunitySignal) -> str:
    digest = hashlib.sha256(f"{canonical_domain(company_domain)}\n{signal.value}".encode()).hexdigest()
    return f"opp-{digest[:24]}"


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_observed(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _host_matches_company_or_ats(url: str, company_domain: str) -> bool:
    try:
        host = urlsplit(canonicalize_url(url)).hostname or ""
    except ValueError:
        return False
    return (
        host == company_domain
        or host.endswith(f".{company_domain}")
        or host in _ATS_HOSTS
        or any(host.endswith(suffix) for suffix in _ATS_SUFFIXES)
    )


def _valid_evidence(item: OpportunityEvidence) -> bool:
    if not item.evidence_type or len(item.evidence_type) > 80:
        return False
    if not item.publisher.strip() or len(item.publisher) > 160:
        return False
    if len(item.source_title) > 300 or len(item.claim) > 500:
        return False
    try:
        canonicalize_url(item.source_url)
    except ValueError:
        return False
    return _parse_observed(item.observed_at) is not None


def verify_opportunity(
    lead: OpportunityLead,
    *,
    now: datetime,
    recent_days: int = 45,
) -> OpportunityDecision:
    """Verify one company signal from explicit evidence, never model inference alone."""
    if not 1 <= recent_days <= 365:
        raise ValueError("opportunity recency window must be between 1 and 365 days")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        company_domain = canonical_domain(lead.company_domain)
        company_url_domain = canonical_domain(lead.company_url)
    except ValueError:
        return OpportunityDecision(OpportunityStatus.REJECTED, ("company_domain_invalid",))
    if company_domain != company_url_domain:
        return OpportunityDecision(OpportunityStatus.REJECTED, ("company_domain_conflict",))
    if not lead.company_name.strip() or not _SAFE_ID.fullmatch(lead.lead_id):
        return OpportunityDecision(OpportunityStatus.REJECTED, ("lead_identity_invalid",))
    if any(not _valid_evidence(item) for item in lead.evidence):
        return OpportunityDecision(OpportunityStatus.REJECTED, ("evidence_contract_invalid",))

    threshold = now.date() - timedelta(days=recent_days)
    if lead.signal is OpportunitySignal.RECENT_FUNDING:
        primary = [
            item
            for item in lead.evidence
            if item.is_primary
            and item.evidence_type in _PRIMARY_FUNDING_TYPES
            and _parse_date(item.event_date)
        ]
        independent = [
            item
            for item in lead.evidence
            if not item.is_primary
            and item.evidence_type not in {"sec_form_d", "hiring_directory"}
            and _parse_date(item.event_date)
        ]
        if not primary or not independent:
            return OpportunityDecision(
                OpportunityStatus.NEEDS_CORROBORATION,
                ("completed_raise_not_verified",),
            )
        primary_recent = [
            item
            for item in primary
            if threshold <= (_parse_date(item.event_date) or date.min) <= now.date()
        ]
        independent_recent = [
            item
            for item in independent
            if threshold <= (_parse_date(item.event_date) or date.min) <= now.date()
        ]
        dated = [
            _parse_date(item.event_date)
            for item in primary_recent + independent_recent
            if _parse_date(item.event_date) is not None
        ]
        if not primary_recent or not independent_recent:
            all_dates = [
                parsed
                for item in primary + independent
                if (parsed := _parse_date(item.event_date)) is not None
            ]
            return OpportunityDecision(
                OpportunityStatus.REJECTED,
                ("funding_signal_stale",),
                signal_date=max(all_dates).isoformat() if all_dates else "",
            )
        primary_publishers = {item.publisher.casefold().strip() for item in primary_recent}
        independent_publishers = {
            item.publisher.casefold().strip() for item in independent_recent
        }
        primary_urls = {canonicalize_url(item.source_url) for item in primary_recent}
        independent_urls = {canonicalize_url(item.source_url) for item in independent_recent}
        if (
            not independent_publishers
            or primary_publishers & independent_publishers
            or primary_urls & independent_urls
        ):
            return OpportunityDecision(
                OpportunityStatus.NEEDS_CORROBORATION,
                ("independent_source_missing",),
            )
        signal_date = max(dated).isoformat()
        return OpportunityDecision(
            OpportunityStatus.VERIFIED,
            ("dated_primary_and_independent_evidence",),
            signal_date=signal_date,
        )

    if lead.signal is OpportunitySignal.FINANCING_NOTICE:
        form_d = [item for item in lead.evidence if item.evidence_type == "sec_form_d"]
        if not form_d:
            return OpportunityDecision(
                OpportunityStatus.NEEDS_CORROBORATION,
                ("financing_notice_not_verified",),
            )
        form_dates = [
            parsed
            for item in form_d
            if (parsed := _parse_date(item.event_date)) is not None
        ]
        return OpportunityDecision(
            OpportunityStatus.NEEDS_CORROBORATION,
            ("completed_raise_not_verified",),
            signal_date=max(form_dates).isoformat() if form_dates else "",
        )

    if lead.signal is OpportunitySignal.ACTIVELY_HIRING:
        current = []
        for item in lead.evidence:
            observed = _parse_observed(item.observed_at)
            if (
                item.evidence_type in {"careers_page", "first_party_job"}
                and item.is_primary
                and observed is not None
                and observed.date() >= threshold
                and observed <= now
                and _host_matches_company_or_ats(item.source_url, company_domain)
            ):
                current.append(item)
        if (
            not current
            or not lead.careers_url
            or lead.open_role_count is None
            or lead.open_role_count <= 0
        ):
            return OpportunityDecision(
                OpportunityStatus.NEEDS_CORROBORATION,
                ("current_hiring_not_verified",),
            )
        if not _host_matches_company_or_ats(lead.careers_url, company_domain):
            return OpportunityDecision(
                OpportunityStatus.REJECTED,
                ("careers_domain_conflict",),
            )
        return OpportunityDecision(
            OpportunityStatus.VERIFIED,
            ("current_first_party_careers_evidence",),
            signal_date=max(_parse_observed(item.observed_at) for item in current).date().isoformat(),
        )

    if len(lead.evidence) < 2 or not any(item.is_primary for item in lead.evidence):
        return OpportunityDecision(
            OpportunityStatus.NEEDS_CORROBORATION,
            ("growth_signal_not_corroborated",),
        )
    return OpportunityDecision(
        OpportunityStatus.VERIFIED,
        ("growth_signal_corroborated",),
    )


def rank_verified_opportunity(
    lead: OpportunityLead,
    decision: OpportunityDecision,
    *,
    role_fit: int,
    location_fit: int,
    contact_relevance: int,
) -> OpportunityDecision:
    """Rank verified leads using visible bounded components; funding amount has no weight."""
    if decision.status is not OpportunityStatus.VERIFIED:
        raise ValueError("only verified opportunity leads can be ranked")
    for value in (role_fit, location_fit, contact_relevance):
        if not 0 <= value <= 20:
            raise ValueError("opportunity fit components must be between 0 and 20")
    freshness = 20 if decision.signal_date else 10
    hiring = 20 if lead.signal is OpportunitySignal.ACTIVELY_HIRING else 0
    evidence = min(20, len(lead.evidence) * 5)
    components = {
        "role_fit": role_fit,
        "location_fit": location_fit,
        "signal_freshness": freshness,
        "current_hiring": hiring,
        "contact_relevance": contact_relevance,
        "evidence_completeness": evidence,
    }
    return replace(decision, score=sum(components.values()), components=components)


def promote_posted_job(
    lead: OpportunityLead,
    *,
    title: str,
    location: str = "",
    description: str = "",
) -> RoleCandidate:
    """Promote only a verified first-party posting; never synthesize a role."""
    if lead.status is not OpportunityStatus.VERIFIED:
        raise ValueError("only verified opportunities can promote a job")
    if lead.route is not OpportunityRoute.POSTED_JOB or not lead.posted_job_url:
        raise ValueError("opportunity has no verified posted-job route")
    if not title.strip():
        raise ValueError("posted job title is required and cannot be synthesized")
    if not _host_matches_company_or_ats(lead.posted_job_url, canonical_domain(lead.company_domain)):
        raise ValueError("posted job URL is not first-party or a recognized ATS")
    return RoleCandidate(
        company=lead.company_name,
        title=title.strip(),
        official_url=canonicalize_url(lead.posted_job_url),
        source="opportunity_research",
        location=location,
        description=description,
        evidence=tuple(item.source_url for item in lead.evidence),
        metadata={"opportunity_lead_id": lead.lead_id, "signal": lead.signal.value},
    )


@dataclass(frozen=True)
class OpportunityResearchRequest:
    run_id: str
    request_id: str
    signals: tuple[OpportunitySignal, ...]
    recent_days: int
    preferences: dict[str, Any]
    preferences_digest: str
    max_leads: int = MAX_LEADS
    max_navigations: int = MAX_NAVIGATIONS
    max_seconds: int = MAX_SECONDS

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["signals"] = [item.value for item in self.signals]
        return payload


def privacy_minimized_preferences(profile: dict[str, Any]) -> dict[str, Any]:
    experience = profile.get("experience") if isinstance(profile.get("experience"), dict) else {}
    availability = (
        profile.get("availability") if isinstance(profile.get("availability"), dict) else {}
    )
    preferences = {
        "target_role": str(experience.get("target_role") or "")[:240],
        "preferred_locations": [
            str(item)[:120]
            for item in (availability.get("preferred_locations") or [])[:10]
        ],
        "industries": [str(item)[:120] for item in (experience.get("industries") or [])[:10]],
    }
    return preferences


def build_research_request(
    *, run_id: str, signals: tuple[OpportunitySignal, ...], recent_days: int, profile: dict[str, Any]
) -> OpportunityResearchRequest:
    if not signals or len(set(signals)) != len(signals):
        raise ValueError("opportunity signals must be non-empty and unique")
    if not 1 <= recent_days <= 365:
        raise ValueError("opportunity recency window must be between 1 and 365 days")
    preferences = privacy_minimized_preferences(profile)
    preferences_digest = hashlib.sha256(_canonical_json(preferences).encode()).hexdigest()
    basis = {
        "schema_version": OPPORTUNITY_RESEARCH_SCHEMA_VERSION,
        "run_id": run_id,
        "signals": [item.value for item in signals],
        "recent_days": recent_days,
        "preferences_digest": preferences_digest,
        "max_leads": MAX_LEADS,
        "max_navigations": MAX_NAVIGATIONS,
        "max_seconds": MAX_SECONDS,
    }
    request_id = hashlib.sha256(_canonical_json(basis).encode()).hexdigest()
    return OpportunityResearchRequest(
        run_id=run_id,
        request_id=request_id,
        signals=signals,
        recent_days=recent_days,
        preferences=preferences,
        preferences_digest=preferences_digest,
    )


def research_bindings(request: OpportunityResearchRequest) -> RunBindings:
    policy = {
        "signals": [item.value for item in request.signals],
        "recent_days": request.recent_days,
        "max_leads": request.max_leads,
        "max_navigations": request.max_navigations,
        "max_seconds": request.max_seconds,
    }
    return RunBindings(
        run_id=request.run_id,
        fact_digest=request.preferences_digest,
        context_digest=request.preferences_digest,
        policy_digest=hashlib.sha256(_canonical_json(policy).encode()).hexdigest(),
    )


def write_research_mission(
    *, run_dir: Path, request: OpportunityResearchRequest, journal: EventJournal
) -> Path:
    bindings = research_bindings(request)
    handoff_dir = run_dir.resolve() / "handoff"
    request_path = handoff_dir / "opportunity_research.request.json"
    response_path = handoff_dir / "opportunity_research.response.json"
    envelope = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "response_schema_version": OPPORTUNITY_RESEARCH_SCHEMA_VERSION,
        "run_id": request.run_id,
        "stage": "opportunity_research",
        "kind": "startup_opportunities",
        "request_id": request.request_id,
        "input_digest": request.preferences_digest,
        "candidate_id": None,
        "fact_digest": bindings.fact_digest,
        "context_digest": bindings.context_digest,
        "policy_digest": bindings.policy_digest,
        "resource_lock": OPPORTUNITY_RESOURCE_LOCK,
        "mission": request.to_dict(),
        "response_path": str(response_path.relative_to(run_dir)),
        "max_response_chars": 500_000,
        "response_format": "strict_json",
        "raw_transcript_required": False,
        "browser_instructions": [
            "Research public web surfaces using visible browser navigation only.",
            "For a recent raise, return a dated company or investor source and an independent source.",
            "Treat SEC Form D as a financing notice, never proof of a completed raise.",
            "Open the official company site or first-party careers/ATS page before claiming active hiring.",
            "Do not use hidden endpoints, export cookies, bypass challenges, contact anyone, or apply.",
            "Return bounded structured claims and URLs; do not copy articles or page dumps.",
        ],
    }
    created = False
    with _handoff_queue_lock(run_dir):
        if request_path.exists():
            _write_immutable_json(request_path, envelope)
            return request_path.resolve()
        current = _active_handoffs_unlocked(run_dir=run_dir, bindings=bindings)
        if len(current) > 1:
            raise ValueError("opportunity research has multiple active browser handoffs")
        if current:
            raise _pending_for_active(current[0])
        _write_immutable_json(request_path, envelope)
        created = True
    if created:
        journal.emit(
            component="browser_mission",
            phase="mission",
            status="queued",
            source="opportunity_research",
            counts={"max_leads": request.max_leads, "max_navigations": request.max_navigations},
            detail={"resource_lock": OPPORTUNITY_RESOURCE_LOCK, "recent_days": request.recent_days},
        )
    return request_path.resolve()


def validate_research_response(payload: Any, *, request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("opportunity response must be an object")
    expected = {
        "schema_version": OPPORTUNITY_RESEARCH_SCHEMA_VERSION,
        "kind": "startup_opportunities",
        "request_id": request.get("request_id"),
        "run_id": request.get("run_id"),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("opportunity response bindings changed")
    if set(payload) != {*expected, "items"}:
        raise ValueError("opportunity response fields are invalid")
    items = payload.get("items")
    max_leads = int((request.get("mission") or {}).get("max_leads") or 0)
    if not isinstance(items, list) or max_leads <= 0 or len(items) > max_leads:
        raise ValueError("opportunity response exceeds its result bound")
    canonical_items: list[dict[str, Any]] = []
    seen_domains: set[str] = set()
    allowed_signals = set((request.get("mission") or {}).get("signals") or [])
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("opportunity response item must be an object")
        lead = OpportunityLead.from_dict(raw)
        if lead.signal.value not in allowed_signals:
            raise ValueError("opportunity response signal was not requested")
        domain = canonical_domain(lead.company_domain)
        if domain in seen_domains:
            raise ValueError("opportunity response contains a duplicate company domain")
        seen_domains.add(domain)
        if canonical_domain(lead.company_url) != domain:
            raise ValueError("opportunity response company domain conflicts with its URL")
        if len(lead.evidence) > 12:
            raise ValueError("opportunity response has too much evidence")
        for evidence in lead.evidence:
            if not _valid_evidence(evidence):
                raise ValueError("opportunity response evidence is invalid")
        expected_id = opportunity_lead_id(domain, lead.signal)
        lead = replace(
            lead,
            lead_id=expected_id,
            company_domain=domain,
            company_url=canonicalize_url(lead.company_url),
            status=OpportunityStatus.OBSERVED,
        )
        canonical_items.append(lead.to_dict())
    return {**expected, "items": canonical_items}


def read_verified_response(path: Path, *, request: dict[str, Any]) -> list[OpportunityLead]:
    payload = validate_research_response(json.loads(path.read_text(encoding="utf-8")), request=request)
    return [OpportunityLead.from_dict(item) for item in payload["items"]]


def consume_research_response(
    *,
    request_path: Path,
    store: Any,
    journal: EventJournal,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify and persist one imported research response without contacting a company."""
    request_path = request_path.resolve(strict=True)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    response_path = request_path.with_name(
        request_path.name.replace(".request.json", ".response.json")
    )
    if not response_path.is_file() or response_path.is_symlink():
        raise ValueError("opportunity research response is not ready")
    leads = read_verified_response(response_path, request=request)
    current_time = now or datetime.now(timezone.utc)
    recent_days = int((request.get("mission") or {}).get("recent_days") or 45)
    counts = {status.value: 0 for status in OpportunityStatus}
    persisted: list[str] = []
    for item in leads:
        decision = verify_opportunity(item, now=current_time, recent_days=recent_days)
        stored = store.persist_lead(str(request["run_id"]), item, decision)
        counts[decision.status.value] += 1
        persisted.append(stored.lead_id)
    response_text = response_path.read_text(encoding="utf-8")
    response_sha256 = hashlib.sha256(response_text.encode("utf-8")).hexdigest()
    receipt_path = response_path.with_name(
        response_path.name.replace(".response.json", ".receipt.json")
    )
    _write_immutable_json(
        receipt_path,
        {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "request_id": request["request_id"],
            "response_sha256": response_sha256,
            "lead_ids": persisted,
        },
    )
    store.record_artifact(
        str(request["run_id"]),
        kind="research_response",
        path=response_path,
        sha256=response_sha256,
    )
    store.finish_run(str(request["run_id"]), status="complete")
    journal.emit(
        component="browser_mission",
        phase="mission",
        status="complete",
        source="opportunity_research",
        counts={
            "lead_count": len(leads),
            "verified_count": counts[OpportunityStatus.VERIFIED.value],
            "corroboration_count": counts[OpportunityStatus.NEEDS_CORROBORATION.value],
            "rejected_count": counts[OpportunityStatus.REJECTED.value],
        },
    )
    return {
        "run_id": str(request["run_id"]),
        "status": "complete",
        "lead_count": len(leads),
        "lead_ids": persisted,
        "counts": {key: value for key, value in counts.items() if value},
        "receipt_path": str(receipt_path),
    }

"""Normalize provider observations and merge exact job identities."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from urllib.parse import urlsplit

from applypilot.aggregation.models import (
    CanonicalJob,
    JobObservation,
    RawJob,
    SourceKind,
    VerificationState,
)
from applypilot.employment import (
    ApplicationSurface,
    OpportunityKind,
    classify_opportunity,
    infer_application_surface,
)
from applypilot.workflow import WorkflowError, canonicalize_url

_PORTAL_HOST_SUFFIXES = ("joinhandshake.com", "joinrunway.io")
_INTERACTIVE_SOURCES = frozenset(
    {
        SourceKind.HANDSHAKE_BROWSER,
        SourceKind.RUNWAY_BROWSER,
        SourceKind.HANDSHAKE_MANUAL,
        SourceKind.RUNWAY_MANUAL,
    }
)


def _canonical_url(value: str, *, field_name: str, required: bool = True) -> str:
    if not value.strip():
        if required:
            raise ValueError(f"{field_name} is required")
        return ""
    try:
        return canonicalize_url(value)
    except WorkflowError as exc:
        raise ValueError(f"{field_name} must be a public HTTP URL") from exc


def _is_portal_url(value: str) -> bool:
    host = (urlsplit(value).hostname or "").lower()
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _PORTAL_HOST_SUFFIXES)


def normalize_job(raw: RawJob) -> JobObservation:
    """Validate and normalize one raw source observation."""
    official_url = _canonical_url(raw.official_url, field_name="official_url", required=False)
    discovery_url = _canonical_url(raw.discovery_url, field_name="discovery_url")
    application_url = _canonical_url(
        raw.application_url or raw.official_url or raw.discovery_url,
        field_name="application_url",
    )
    if official_url and _is_portal_url(official_url):
        raise ValueError("official_url must resolve to an employer or ATS posting")
    if not official_url and raw.source not in _INTERACTIVE_SOURCES and raw.source is not SourceKind.JOBSPY:
        raise ValueError("non-portal observations require an official_url")
    source_job_id = raw.source_job_id.strip()
    title = raw.title.strip()
    company = raw.company.strip()
    if not source_job_id or not title or not company:
        raise ValueError("source job id, title, and company are required")
    if len(source_job_id) > 300:
        raise ValueError("source job id is too long")

    identity = official_url or f"{raw.source.value}:{source_job_id}:{discovery_url}"
    canonical_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    if official_url:
        verification_state = VerificationState.FIRST_PARTY_RESOLVED
    elif raw.source is SourceKind.JOBSPY:
        verification_state = VerificationState.BOARD_ONLY
    else:
        verification_state = VerificationState.PORTAL_ONLY

    metadata = {
        str(key)[:80]: value
        for key, value in list(raw.metadata.items())[:30]
        if value is None or isinstance(value, (str, int, float, bool))
    }
    surface_hint = infer_application_surface(official_url or application_url)
    if raw.source in {SourceKind.DIRECT_ATS, SourceKind.WORKDAY} and official_url:
        surface_hint = ApplicationSurface.PROVIDER_REQUISITION
    elif str(metadata.get("structured_data_type") or "").casefold() == "jobposting":
        surface_hint = ApplicationSurface.JOB_POSTING_STRUCTURED_DATA
    classification = classify_opportunity(
        title=title,
        description=raw.description,
        official_url=official_url or application_url,
        application_surface=surface_hint,
        requisition_id=(
            source_job_id
            if surface_hint is ApplicationSurface.PROVIDER_REQUISITION
            else ""
        ),
    )
    advanceable = bool(
        verification_state is VerificationState.FIRST_PARTY_RESOLVED
        and classification.kind is OpportunityKind.POSTED_EMPLOYMENT
    )
    return JobObservation(
        canonical_key=canonical_key,
        source=raw.source,
        source_job_id=source_job_id,
        title=title[:300],
        company=company[:300],
        location=raw.location.strip()[:300],
        official_url=official_url,
        application_url=application_url,
        discovery_url=discovery_url,
        description=raw.description.strip()[:20_000],
        observed_at=raw.observed_at.isoformat(),
        salary=raw.salary.strip()[:300],
        posted_at=raw.posted_at.strip()[:80],
        verification_state=verification_state,
        advanceable=advanceable,
        opportunity_kind=classification.kind,
        application_surface=classification.application_surface,
        routing_reasons=classification.reason_codes,
        metadata=metadata,
    )


def merge_observations(observations: Iterable[JobObservation]) -> CanonicalJob:
    """Merge observations that already share an exact canonical identity."""
    ordered = tuple(sorted(observations, key=lambda item: (item.observed_at, item.source.value)))
    if not ordered or len({item.canonical_key for item in ordered}) != 1:
        raise ValueError("merge requires observations for one canonical key")
    freshest = ordered[-1]
    advanceable = any(item.advanceable for item in ordered)
    strongest = next(
        (
            item
            for item in reversed(ordered)
            if item.opportunity_kind is OpportunityKind.POSTED_EMPLOYMENT
        ),
        freshest,
    )
    return CanonicalJob(
        canonical_key=freshest.canonical_key,
        title=freshest.title,
        company=freshest.company,
        location=freshest.location,
        official_url=next((item.official_url for item in reversed(ordered) if item.official_url), ""),
        application_url=next(
            (item.application_url for item in reversed(ordered) if item.application_url), ""
        ),
        description=max((item.description for item in ordered), key=len, default=""),
        salary=next((item.salary for item in reversed(ordered) if item.salary), ""),
        posted_at=next((item.posted_at for item in reversed(ordered) if item.posted_at), ""),
        verification_state=(
            VerificationState.FIRST_PARTY_RESOLVED if advanceable else freshest.verification_state
        ),
        advanceable=advanceable,
        opportunity_kind=strongest.opportunity_kind,
        application_surface=strongest.application_surface,
        routing_reasons=strongest.routing_reasons,
        observations=ordered,
    )

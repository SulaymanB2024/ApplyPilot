"""Deterministic opportunity-kind and application-surface classification.

This module answers a question that must precede fit scoring: whether a web
surface is a job-specific employment requisition, a separate off-posting route,
or a non-job marketplace/profile/task surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit


class OpportunityKind(StrEnum):
    POSTED_EMPLOYMENT = "posted_employment"
    GENERAL_INTEREST_APPLICATION = "general_interest_application"
    SPECULATIVE_OUTREACH = "speculative_outreach"
    MARKETPLACE_GIG = "marketplace_gig"
    MICROTASK_PLATFORM = "microtask_platform"
    ASSESSMENT_OR_PROFILE_SIGNUP = "assessment_or_profile_signup"
    UNKNOWN = "unknown"


class ApplicationSurface(StrEnum):
    PROVIDER_REQUISITION = "provider_requisition"
    JOB_POSTING_STRUCTURED_DATA = "job_posting_structured_data"
    JOB_APPLICATION_FORM = "job_application_form"
    JOB_DETAIL_PAGE = "job_detail_page"
    GENERAL_INTEREST_FORM = "general_interest_form"
    CONTACT_ROUTE = "contact_route"
    PROFILE_SIGNUP = "profile_signup"
    NONE = "none"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OpportunityClassification:
    kind: OpportunityKind
    application_surface: ApplicationSurface
    reason_codes: tuple[str, ...]
    evidence: tuple[str, ...] = ()


_ATS_HOSTS = frozenset(
    {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "jobs.ashbyhq.com",
        "jobs.lever.co",
        "jobs.smartrecruiters.com",
    }
)
_ATS_SUFFIXES = (
    ".avature.net",
    ".csod.com",
    ".icims.com",
    ".myworkdayjobs.com",
    ".myworkdaysite.com",
    ".oraclecloud.com",
)
_MARKETPLACE_HOSTS = frozenset(
    {
        "fiverr.com",
        "freelancer.com",
        "mercor.com",
        "toptal.com",
        "turing.com",
        "upwork.com",
    }
)
_MICROTASK_HOSTS = frozenset(
    {
        "alignerr.com",
        "clickworker.com",
        "dataannotation.tech",
        "oneforma.com",
        "outlier.ai",
        "remotasks.com",
        "toloka.ai",
    }
)
_GENERAL_INTEREST_MARKERS = (
    "general application",
    "general interest",
    "join our talent community",
    "join our talent network",
    "register your interest",
    "submit your resume for future",
    "future opportunities",
)
_PROFILE_MARKERS = (
    "build your profile",
    "complete your profile",
    "create a profile",
    "join our network",
    "skills assessment",
    "take an assessment",
    "talent marketplace",
)
_GIG_MARKERS = (
    "freelance",
    "freelancer",
    "independent contractor",
    "set your rate",
    "choose your own projects",
    "project marketplace",
    "contractor marketplace",
)
_MICROTASK_ROLE_MARKERS = (
    "ai rater",
    "ai trainer",
    "data annotator",
    "data annotation",
    "data labeler",
    "data labeling",
    "llm evaluator",
    "model response evaluator",
    "prompt evaluator",
    "search quality rater",
)
_MICROTASK_ARRANGEMENT_MARKERS = (
    "earn per task",
    "paid per task",
    "piece rate",
    "pick up tasks",
    "task based",
    "task-based",
    "work whenever you want",
    "work whenever you wish",
)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _contains_any(text: str, markers: tuple[str, ...]) -> str:
    return next((marker for marker in markers if _normalize(marker) in text), "")


def _host_matches(host: str, candidates: frozenset[str]) -> str:
    return next(
        (
            candidate
            for candidate in candidates
            if host == candidate or host.endswith(f".{candidate}")
        ),
        "",
    )


def infer_application_surface(url: str) -> ApplicationSurface:
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    path = "/" + parsed.path.strip("/")
    normalized_path = _normalize(path)
    if not host:
        return ApplicationSurface.NONE
    if _host_matches(host, _MARKETPLACE_HOSTS | _MICROTASK_HOSTS):
        return ApplicationSurface.PROFILE_SIGNUP
    if not path.strip("/"):
        return ApplicationSurface.NONE
    if host in _ATS_HOSTS or any(host.endswith(suffix) for suffix in _ATS_SUFFIXES):
        parts = [part for part in parsed.path.split("/") if part]
        if (
            host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}
            and len(parts) >= 3
            and parts[-2] == "jobs"
        ):
            return ApplicationSurface.PROVIDER_REQUISITION
        if host in {"jobs.lever.co", "jobs.ashbyhq.com"} and len(parts) >= 2:
            return ApplicationSurface.PROVIDER_REQUISITION
        if " job " in f" {normalized_path} ":
            return ApplicationSurface.PROVIDER_REQUISITION
    if _contains_any(normalized_path, _GENERAL_INTEREST_MARKERS):
        return ApplicationSurface.GENERAL_INTEREST_FORM
    if re.search(r"\b(?:jobs?|careers?|positions?|openings?|opportunities?)\b", normalized_path):
        return ApplicationSurface.JOB_DETAIL_PAGE
    return ApplicationSurface.UNKNOWN


def classify_opportunity(
    *,
    title: str,
    description: str,
    official_url: str,
    application_surface: ApplicationSurface | str | None = None,
    requisition_id: str = "",
) -> OpportunityClassification:
    """Classify a bounded surface without inferring missing employment facts."""
    parsed = urlsplit(official_url.strip())
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    text = _normalize(f"{title} {description}")
    if application_surface in (None, ""):
        surface = infer_application_surface(official_url)
    else:
        try:
            surface = ApplicationSurface(application_surface)
        except ValueError:
            surface = ApplicationSurface.UNKNOWN

    task_host = _host_matches(host, _MICROTASK_HOSTS)
    if task_host:
        return OpportunityClassification(
            OpportunityKind.MICROTASK_PLATFORM,
            ApplicationSurface.PROFILE_SIGNUP,
            ("known_microtask_platform",),
            (task_host,),
        )
    marketplace_host = _host_matches(host, _MARKETPLACE_HOSTS)
    if marketplace_host:
        return OpportunityClassification(
            OpportunityKind.MARKETPLACE_GIG,
            ApplicationSurface.PROFILE_SIGNUP,
            ("known_marketplace_host",),
            (marketplace_host,),
        )

    general_interest = _contains_any(text, _GENERAL_INTEREST_MARKERS)
    if surface is ApplicationSurface.GENERAL_INTEREST_FORM or general_interest:
        return OpportunityClassification(
            OpportunityKind.GENERAL_INTEREST_APPLICATION,
            ApplicationSurface.GENERAL_INTEREST_FORM,
            ("general_interest_surface",),
            tuple(value for value in (general_interest,) if value),
        )

    profile_marker = _contains_any(text, _PROFILE_MARKERS)
    if surface is ApplicationSurface.PROFILE_SIGNUP or profile_marker:
        return OpportunityClassification(
            OpportunityKind.ASSESSMENT_OR_PROFILE_SIGNUP,
            ApplicationSurface.PROFILE_SIGNUP,
            ("profile_or_assessment_surface",),
            tuple(value for value in (profile_marker,) if value),
        )

    gig_marker = _contains_any(text, _GIG_MARKERS)
    if gig_marker:
        return OpportunityClassification(
            OpportunityKind.MARKETPLACE_GIG,
            surface,
            ("gig_work_arrangement",),
            (gig_marker,),
        )

    task_role = _contains_any(text, _MICROTASK_ROLE_MARKERS)
    task_arrangement = _contains_any(text, _MICROTASK_ARRANGEMENT_MARKERS)
    if task_role and task_arrangement:
        return OpportunityClassification(
            OpportunityKind.MICROTASK_PLATFORM,
            surface,
            ("microtask_role_and_arrangement",),
            (task_role, task_arrangement),
        )

    if requisition_id or surface in {
        ApplicationSurface.PROVIDER_REQUISITION,
        ApplicationSurface.JOB_POSTING_STRUCTURED_DATA,
        ApplicationSurface.JOB_APPLICATION_FORM,
    }:
        return OpportunityClassification(
            OpportunityKind.POSTED_EMPLOYMENT,
            surface,
            ("job_specific_application_surface",),
            tuple(value for value in (requisition_id,) if value),
        )

    return OpportunityClassification(
        OpportunityKind.UNKNOWN,
        surface,
        ("opportunity_kind_unverified",),
    )


def is_accepted_job_surface(surface: ApplicationSurface | str) -> bool:
    try:
        normalized = ApplicationSurface(surface)
    except ValueError:
        return False
    return normalized in {
        ApplicationSurface.PROVIDER_REQUISITION,
        ApplicationSurface.JOB_POSTING_STRUCTURED_DATA,
        ApplicationSurface.JOB_APPLICATION_FORM,
    }


def is_data_labeling_function(title: str) -> str:
    """Return a title-level excluded function without penalizing incidental body text."""
    normalized = _normalize(title)
    return _contains_any(normalized, _MICROTASK_ROLE_MARKERS)

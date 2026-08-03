"""Truth-bound, local-only speculative outreach drafting."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from applypilot.autonomy.handoff import _write_immutable_json
from applypilot.opportunities.models import (
    OpportunityEvidence,
    OpportunityLead,
    OpportunityRoute,
    OpportunitySignal,
    OpportunityStatus,
)
from applypilot.opportunities.research import canonical_domain, canonicalize_url

OUTREACH_DRAFT_SCHEMA_VERSION = "applypilot.outreach-draft.v1"
DEFAULT_OUTREACH_SENDER = "sybatx@gmail.com"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EMAIL = re.compile(r"^[^\s@]+@([^\s@]+)$")
_DISALLOWED_LANGUAGE = (
    "your opening",
    "the opening",
    "my application",
    "following up on my application",
    "mutual connection",
    "referred me",
    "urgent",
    "act now",
)
_CONTACT_TYPES = frozenset(
    {
        "public_contact_page",
        "company_mailbox",
        "verified_named_person",
        "account_backed_profile",
        "general_interest_form",
    }
)


class OutreachGateError(PermissionError):
    """Raised when evidence or applicant facts do not support an outreach artifact."""


@dataclass(frozen=True)
class OutreachDraft:
    draft_id: str
    lead_id: str
    channel: str
    sender: str
    recipient: str
    subject: str
    body: str
    attachment_digests: tuple[str, ...]
    lead_evidence_digest: str
    fact_snapshot_digest: str
    cited_lead_evidence_ids: tuple[str, ...]
    cited_profile_fact_ids: tuple[str, ...]
    intent: str
    unsupported_claims: tuple[str, ...]
    created_at: str
    sha256: str

    def unsigned_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("sha256", None)
        payload.pop("draft_id", None)
        payload["schema_version"] = OUTREACH_DRAFT_SCHEMA_VERSION
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.unsigned_dict(),
            "draft_id": self.draft_id,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OutreachDraft:
        if payload.get("schema_version") != OUTREACH_DRAFT_SCHEMA_VERSION:
            raise OutreachGateError("unsupported outreach draft schema")
        values = dict(payload)
        values.pop("schema_version", None)
        for field_name in (
            "attachment_digests",
            "cited_lead_evidence_ids",
            "cited_profile_fact_ids",
            "unsupported_claims",
        ):
            values[field_name] = tuple(values.get(field_name) or [])
        draft = cls(**values)
        validate_outreach_draft(draft)
        return draft


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _evidence_id(item: OpportunityEvidence) -> str:
    return "LEAD-" + _digest(item.to_dict())[:16]


def outreach_profile_snapshot(profile: dict[str, Any]) -> dict[str, Any]:
    """Extract only bounded applicant facts that may appear in a speculative inquiry."""
    experience = profile.get("experience") if isinstance(profile.get("experience"), dict) else {}
    skills = profile.get("skills_boundary") if isinstance(profile.get("skills_boundary"), dict) else {}
    safe_skills: list[str] = []
    for values in skills.values():
        if isinstance(values, list):
            safe_skills.extend(str(item).strip()[:80] for item in values if str(item).strip())
    snapshot = {
        "target_role": str(experience.get("target_role") or "")[:160],
        "current_title": str(experience.get("current_title") or "")[:160],
        "education_level": str(experience.get("education_level") or "")[:160],
        "skills": sorted(set(safe_skills))[:12],
    }
    return {key: value for key, value in snapshot.items() if value}


def fact_snapshot_digest(snapshot: dict[str, Any]) -> str:
    return _digest(snapshot)


def _verified_contact(lead: OpportunityLead, *, channel: str) -> tuple[str, tuple[str, ...]]:
    if lead.route is OpportunityRoute.GENERAL_INTEREST_APPLICATION:
        if channel != "contact_form" or not lead.general_application_url:
            raise OutreachGateError(
                "general-interest applications require the verified contact_form channel"
            )
        expected = canonicalize_url(lead.general_application_url)
        form_evidence = tuple(
            item
            for item in (*lead.evidence, *lead.contact_evidence)
            if item.evidence_type == "general_interest_form"
            and item.is_primary
            and canonicalize_url(item.source_url) == expected
        )
        if not form_evidence:
            raise OutreachGateError("verified general-interest form required")
        return expected, tuple(_evidence_id(item) for item in form_evidence)
    route = lead.contact_route.strip()
    if not route or not lead.contact_evidence:
        raise OutreachGateError("verified contact route required")
    company_domain = canonical_domain(lead.company_domain)
    matching: list[OpportunityEvidence] = []
    for item in lead.contact_evidence:
        if item.evidence_type not in _CONTACT_TYPES or not item.source_url:
            continue
        try:
            source_host = urlsplit(canonicalize_url(item.source_url)).hostname or ""
        except ValueError:
            continue
        source_matches_company = source_host == company_domain or source_host.endswith(
            f".{company_domain}"
        )
        if item.evidence_type in {"public_contact_page", "company_mailbox"}:
            if not source_matches_company:
                continue
        elif item.evidence_type == "verified_named_person" and not source_matches_company:
            continue
        elif item.evidence_type == "account_backed_profile" and not item.is_primary:
            continue
        matching.append(item)
    if not matching:
        raise OutreachGateError("verified contact route required")
    if channel == "email":
        email_match = _EMAIL.fullmatch(route)
        if email_match is None or canonical_domain(email_match.group(1)) != company_domain:
            raise OutreachGateError("verified company-domain email required")
        if not any(route.casefold() in item.claim.casefold() for item in matching):
            raise OutreachGateError("contact evidence does not bind the exact mailbox")
    elif channel == "linkedin":
        if not route.startswith("https://www.linkedin.com/"):
            raise OutreachGateError("account-backed messaging route required")
        if not any(item.evidence_type == "account_backed_profile" for item in matching):
            raise OutreachGateError("account-backed messaging route required")
    elif channel == "contact_form":
        try:
            if canonical_domain(route) != company_domain:
                raise OutreachGateError("verified company contact form required")
        except ValueError as exc:
            raise OutreachGateError("verified company contact form required") from exc
    else:
        raise OutreachGateError("unsupported outreach channel")
    return route, tuple(_evidence_id(item) for item in matching)


def build_outreach_draft(
    lead: OpportunityLead,
    *,
    profile: dict[str, Any],
    channel: str = "email",
    sender: str = DEFAULT_OUTREACH_SENDER,
    attachment_digests: tuple[str, ...] = (),
    now: datetime | None = None,
) -> OutreachDraft:
    """Build a deterministic inquiry from verified evidence without mailbox access."""
    if lead.status is not OpportunityStatus.VERIFIED:
        raise OutreachGateError("verified opportunity required")
    if lead.route not in {
        OpportunityRoute.SPECULATIVE_OUTREACH,
        OpportunityRoute.GENERAL_INTEREST_APPLICATION,
    }:
        raise OutreachGateError("posted jobs must use the normal application workflow")
    recipient, contact_ids = _verified_contact(lead, channel=channel)
    if any(not _SHA256.fullmatch(item) for item in attachment_digests):
        raise OutreachGateError("attachment digest is invalid")
    snapshot = outreach_profile_snapshot(profile)
    snapshot_sha256 = fact_snapshot_digest(snapshot)
    skills = snapshot.get("skills") or []
    if skills:
        applicant_context = f"My background includes {', '.join(skills[:3])}."
        cited_profile = ("skills",)
    elif snapshot.get("current_title"):
        applicant_context = f"My current work is in {snapshot['current_title']}."
        cited_profile = ("current_title",)
    elif snapshot.get("education_level"):
        applicant_context = f"My education is {snapshot['education_level']}."
        cited_profile = ("education_level",)
    elif snapshot.get("target_role"):
        applicant_context = f"I am interested in {snapshot['target_role']} work."
        cited_profile = ("target_role",)
    else:
        raise OutreachGateError("a verified applicant capability is required")
    if lead.signal is OpportunitySignal.RECENT_FUNDING:
        company_context = "I saw your recent company update and its independent coverage."
    elif lead.signal is OpportunitySignal.ACTIVELY_HIRING:
        company_context = "I saw current roles on your official careers site."
    else:
        company_context = "I have been following the public information about your work."
    if lead.route is OpportunityRoute.GENERAL_INTEREST_APPLICATION:
        body = (
            f"Hello {lead.company_name} team,\n\n"
            f"{applicant_context} I am submitting this general-interest form because I would "
            "value consideration for a fitting internship or early-career role, including future "
            "opportunities. I understand this is not tied to a currently posted requisition.\n\n"
            "Thank you for your time and consideration.\n\nBest,"
        )
        subject = f"General interest in opportunities at {lead.company_name}"
        intent = "general_interest_application"
    elif channel == "linkedin":
        body = (
            f"Hello {lead.company_name} team — {company_context} {applicant_context} "
            "I would value learning about internships, short-term projects, or early-career "
            "ways to contribute. Thank you."
        )
        subject = f"Inquiry about contributing to {lead.company_name}"
        intent = "inquiry"
    else:
        body = (
            f"Hello {lead.company_name} team,\n\n"
            f"{company_context} {applicant_context} "
            "I would value learning whether there may be an internship, short-term project, "
            "or early-career way to contribute; I am not assuming a current role is available.\n\n"
            "Thank you for your time and consideration.\n\nBest,"
        )
        subject = f"Inquiry about contributing to {lead.company_name}"
        intent = "inquiry"
    created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    lead_digest = lead.digest
    basis = {
        "schema_version": OUTREACH_DRAFT_SCHEMA_VERSION,
        "lead_id": lead.lead_id,
        "channel": channel,
        "sender": sender,
        "recipient": recipient,
        "subject": subject,
        "body": body,
        "attachment_digests": list(attachment_digests),
        "lead_evidence_digest": lead_digest,
        "fact_snapshot_digest": snapshot_sha256,
        "cited_lead_evidence_ids": list(contact_ids),
        "cited_profile_fact_ids": list(cited_profile),
        "intent": intent,
        "unsupported_claims": [],
        "created_at": created_at,
    }
    digest = _digest(basis)
    draft = OutreachDraft(
        draft_id=f"draft-{digest[:24]}",
        lead_id=lead.lead_id,
        channel=channel,
        sender=sender,
        recipient=recipient,
        subject=subject,
        body=body,
        attachment_digests=attachment_digests,
        lead_evidence_digest=lead_digest,
        fact_snapshot_digest=snapshot_sha256,
        cited_lead_evidence_ids=contact_ids,
        cited_profile_fact_ids=cited_profile,
        intent=intent,
        unsupported_claims=(),
        created_at=created_at,
        sha256=digest,
    )
    validate_outreach_draft(draft)
    return draft


def validate_outreach_draft(draft: OutreachDraft) -> None:
    if draft.intent not in {"inquiry", "general_interest_application"} or draft.unsupported_claims:
        raise OutreachGateError("outreach draft contains an unsupported intent or claim")
    if draft.intent == "general_interest_application" and draft.channel != "contact_form":
        raise OutreachGateError("general-interest application must use a contact form")
    if draft.channel not in {"email", "linkedin", "contact_form"}:
        raise OutreachGateError("outreach draft channel is invalid")
    if not draft.sender or not draft.recipient or not draft.subject or not draft.body:
        raise OutreachGateError("outreach draft is incomplete")
    if draft.channel == "email" and len(draft.body.split()) > 180:
        raise OutreachGateError("email outreach body exceeds 180 words")
    if draft.channel == "linkedin" and len(draft.body) > 300:
        raise OutreachGateError("LinkedIn outreach note exceeds its character limit")
    lowered = draft.body.casefold()
    if any(fragment in lowered for fragment in _DISALLOWED_LANGUAGE):
        raise OutreachGateError("outreach draft uses unsupported or manipulative language")
    if "thank" not in lowered or (draft.channel != "linkedin" and "best," not in lowered):
        raise OutreachGateError("outreach draft lacks a respectful close")
    for digest in (
        draft.lead_evidence_digest,
        draft.fact_snapshot_digest,
        *draft.attachment_digests,
    ):
        if not _SHA256.fullmatch(digest):
            raise OutreachGateError("outreach draft digest binding is invalid")
    if draft.sha256 != _digest(draft.unsigned_dict()):
        raise OutreachGateError("outreach draft digest mismatch")
    if draft.draft_id != f"draft-{draft.sha256[:24]}":
        raise OutreachGateError("outreach draft id mismatch")


def persist_draft(path: Path, draft: OutreachDraft) -> Path:
    validate_outreach_draft(draft)
    _write_immutable_json(path.resolve(), draft.to_dict())
    return path.resolve()

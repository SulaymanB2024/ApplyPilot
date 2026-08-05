"""Compact, versioned fact packs for bounded model calls."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from applypilot.autonomy.facts import FactLedger
from applypilot.autonomy.models import CandidateProfile, RoleCandidate

CONTEXT_VERSION = "applypilot-context-v1"
CONTACT_KEYS = {
    "address",
    "email",
    "password",
    "phone",
    "postal_code",
    "salary_expectation",
    "street_address",
}
MODEL_SAFE_FACT_PREFIXES = (
    "profile.education.",
    "profile.experience.",
    "profile.projects.",
    "profile.resume_facts.",
    "profile.skills_boundary.",
    "resume.line.",
)
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.-]{1,}")


@dataclass(frozen=True)
class CompactContextPack:
    """Sanitized model context with stable evidence identifiers."""

    version: str
    profile: dict[str, Any]
    evidence: tuple[dict[str, str], ...]
    digest: str
    serialized_chars: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "profile": self.profile,
            "evidence": list(self.evidence),
            "digest": self.digest,
        }


def build_context_pack(
    profile: dict[str, Any],
    *,
    resume_text: str = "",
    job_text: str = "",
    max_chars: int = 8_000,
    max_evidence: int = 10,
    fact_ledger: FactLedger | None = None,
) -> CompactContextPack:
    """Build a minimal fact pack without contact details or raw resume dumps."""
    compact_profile = _compact_profile(profile)
    if fact_ledger is not None:
        rejected = tuple(record.value for record in fact_ledger.rejected())
        compact_profile = _remove_rejected(compact_profile, rejected)
        evidence = _select_ledger_evidence(
            fact_ledger,
            job_text=job_text,
            limit=max_evidence,
        )
    else:
        evidence = _profile_evidence(profile)
        evidence.extend(_select_resume_evidence(resume_text, job_text, limit=max_evidence))
    evidence = _dedupe(evidence)[:max_evidence]

    while True:
        numbered = tuple(
            {"id": f"F{index:02d}", "fact": fact[:320]}
            for index, fact in enumerate(evidence, start=1)
        )
        core = {
            "version": CONTEXT_VERSION,
            "profile": compact_profile,
            "evidence": numbered,
        }
        serialized = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if len(serialized) <= max_chars or not evidence:
            break
        evidence.pop()

    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return CompactContextPack(
        version=CONTEXT_VERSION,
        profile=compact_profile,
        evidence=numbered,
        digest=digest,
        serialized_chars=len(serialized),
    )


def candidate_profile_from_data(profile: dict[str, Any]) -> CandidateProfile:
    """Derive the pre-model eligibility profile from factual profile data."""
    experience = profile.get("experience") or {}
    education = str(experience.get("education_level") or profile.get("education") or "")
    month, year = _graduation_date(education)
    locations = _string_tuple(
        (profile.get("preferences") or {}).get("locations")
        or (profile.get("availability") or {}).get("preferred_locations")
    )
    return CandidateProfile(
        graduation_month=month,
        graduation_year=year,
        max_required_experience_years=_safe_int(
            experience.get("max_required_experience_years"),
            default=2,
        ),
        preferred_locations=locations or CandidateProfile.preferred_locations,
    )


def build_discovery_prompt(
    pack: CompactContextPack,
    *,
    query: str,
    limit: int,
) -> str:
    """Build one strict-JSON ChatGPT Web role-discovery request."""
    payload = {
        "task": "Find currently open roles matching this sanitized candidate profile.",
        "query": query,
        "limit": limit,
        "source_rules": [
            "Use ChatGPT Web research as the discovery surface.",
            "Return official employer or ATS URLs only.",
            "Do not return LinkedIn, Indeed, JobSpy, Glassdoor, ZipRecruiter, or Google Jobs URLs.",
            "Treat every role as an unverified candidate; ApplyPilot will verify first-party status.",
        ],
        "candidate_profile": pack.profile,
        "output_contract": {
            "schema_version": "applypilot.chatgpt_web.v1",
            "kind": "role_candidates",
            "items": [
                {
                    "company": "string",
                    "title": "string",
                    "official_url": "https:// official employer or ATS URL",
                    "location": "string",
                    "description": "max 800 chars",
                    "required_experience_min": "integer or null",
                    "required_experience_max": "integer or null",
                    "posted_date": "YYYY-MM-DD or null",
                    "start_date": "YYYY-MM-DD or null",
                    "end_date": "YYYY-MM-DD or null",
                    "evidence": ["short source-backed observation"],
                }
            ],
        },
        "response_rule": "Return exactly one JSON object. No markdown or commentary.",
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_material_prompt(
    pack: CompactContextPack,
    candidate: RoleCandidate,
    *,
    verified_job_text: str,
) -> str:
    """Build a provenance-oriented cover-letter request."""
    payload = {
        "task": "Draft a concise cover letter for human review using only supplied facts.",
        "rules": [
            "Do not invent dates, metrics, employers, tools, education, authorization, or availability.",
            "Every paragraph must cite at least one supplied fact id or JOB as support.",
            "Use exact factual nouns and verbs from cited evidence; do not add unsupported factual vocabulary.",
            "Flag unsupported requirements as verification gaps.",
            "Do not include phone, email, street address, salary, demographics, or passwords.",
        ],
        "job": {
            "candidate_id": candidate.candidate_id,
            "company": candidate.company,
            "title": candidate.title,
            "official_url": candidate.official_url,
            "verified_description": verified_job_text[:6_000],
        },
        "context": pack.to_dict(),
        "output_contract": {
            "schema_version": "applypilot.chatgpt_web.v1",
            "kind": "material_packet",
            "candidate_id": candidate.candidate_id,
            "paragraphs": [
                {"text": "string", "evidence_ids": ["F01", "JOB"]}
            ],
            "verification_gaps": ["string"],
        },
        "response_rule": "Return exactly one JSON object. No markdown or commentary.",
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _compact_profile(profile: dict[str, Any]) -> dict[str, Any]:
    experience = profile.get("experience") or {}
    skills = profile.get("skills_boundary") or {}
    personal = profile.get("personal") or {}
    preferences = profile.get("preferences") or {}
    result = {
        "education": experience.get("education_level") or profile.get("education"),
        "target_role": experience.get("target_role"),
        "current_title": experience.get("current_title"),
        "city_region": ", ".join(
            part for part in (personal.get("city"), personal.get("province_state"), personal.get("country")) if part
        ),
        "skills": {
            str(category): [str(item)[:100] for item in values[:20]]
            for category, values in skills.items()
            if isinstance(values, list)
        },
        "preferences": _strip_contact_values(preferences),
    }
    return {key: value for key, value in result.items() if value not in (None, "", {}, [])}


def _profile_evidence(profile: dict[str, Any]) -> list[str]:
    facts = profile.get("resume_facts") or {}
    evidence: list[str] = []
    for key in ("preserved_companies", "preserved_projects", "real_metrics"):
        values = facts.get(key) or []
        if isinstance(values, list):
            evidence.extend(str(value) for value in values if value)
    preserved_school = facts.get("preserved_school")
    if preserved_school:
        evidence.append(str(preserved_school))
    return evidence


def _select_resume_evidence(resume_text: str, job_text: str, *, limit: int) -> list[str]:
    if not resume_text.strip():
        return []
    job_tokens = _tokens(job_text)
    candidates: list[tuple[int, int, str]] = []
    for index, raw_line in enumerate(resume_text.splitlines()):
        line = re.sub(r"^[\s•*-]+", "", raw_line).strip()
        if len(line) < 20 or len(line) > 500 or _looks_like_contact_line(line):
            continue
        overlap = len(_tokens(line) & job_tokens)
        is_bullet = raw_line.lstrip().startswith(("•", "-", "*"))
        score = overlap * 10 + (3 if is_bullet else 0)
        if score:
            candidates.append((score, -index, line))
    candidates.sort(reverse=True)
    return [line for _, _, line in candidates[:limit]]


def _select_ledger_evidence(
    ledger: FactLedger,
    *,
    job_text: str,
    limit: int,
) -> list[str]:
    job_tokens = _tokens(job_text)
    candidates: list[tuple[int, str, str]] = []
    for record in ledger.confirmed():
        if not record.fact_id.startswith(MODEL_SAFE_FACT_PREFIXES):
            continue
        if _fact_id_is_contact(record.fact_id) or _looks_like_contact_line(record.value):
            continue
        value_tokens = _tokens(record.value)
        overlap = len(value_tokens & job_tokens)
        profile_priority = 3 if record.fact_id.startswith("profile.") else 0
        bullet_priority = 2 if len(record.value) >= 20 else 0
        score = overlap * 10 + profile_priority + bullet_priority
        if overlap or record.fact_id.startswith("profile.experience."):
            candidates.append((score, record.fact_id, record.value))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [value for _, _, value in candidates[:limit]]


def _strip_contact_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _strip_contact_values(item)
            for key, item in value.items()
            if str(key).lower() not in CONTACT_KEYS
        }
    if isinstance(value, list):
        return [_strip_contact_values(item) for item in value]
    return value


def _remove_rejected(value: Any, rejected: tuple[str, ...]) -> Any:
    rejected_normalized = {re.sub(r"\s+", " ", item).strip().lower() for item in rejected if item}
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            cleaned = _remove_rejected(item, rejected)
            if cleaned not in (None, "", {}, []):
                result[key] = cleaned
        return result
    if isinstance(value, list):
        return [
            cleaned
            for item in value
            if (cleaned := _remove_rejected(item, rejected)) not in (None, "", {}, [])
        ]
    if isinstance(value, str):
        normalized = re.sub(r"\s+", " ", value).strip().lower()
        if any(rejected_item and rejected_item in normalized for rejected_item in rejected_normalized):
            return None
    return value


def _fact_id_is_contact(fact_id: str) -> bool:
    parts = set(fact_id.lower().split("."))
    return bool(parts & CONTACT_KEYS)


def _looks_like_contact_line(line: str) -> bool:
    lowered = line.lower()
    return bool(
        "@" in line
        or "linkedin.com" in lowered
        or "github.com" in lowered
        or "http://" in lowered
        or "https://" in lowered
        or re.search(r"\b\d{3}[-.) ]\d{3}[- ]\d{4}\b", line)
    )


def _tokens(value: str) -> set[str]:
    stop = {"and", "for", "from", "that", "the", "this", "with", "your"}
    return {token for token in TOKEN_RE.findall(value.lower()) if token not in stop}


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = re.sub(r"\s+", " ", value).strip()
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _graduation_date(value: str) -> tuple[int | None, int | None]:
    month_map = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    match = re.search(
        r"(?:expected\s+)?(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)?\s*"
        r"(20\d{2})",
        value,
        re.I,
    )
    if not match:
        return None, None
    month_text = (match.group(1) or "").lower()[:3]
    return month_map.get(month_text), int(match.group(2))


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value if item)
    return ()

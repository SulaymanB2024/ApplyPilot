"""Compact, versioned fact packs for bounded model calls."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from applypilot.autonomy.facts import FactLedger
from applypilot.autonomy.models import CandidateProfile, RoleCandidate

CONTEXT_VERSION = "applypilot-context-v2"
SENSITIVE_KEYS = {
    "address",
    "api_key",
    "apikey",
    "birth_date",
    "compensation",
    "contact",
    "date_of_birth",
    "demographics",
    "disability_status",
    "email",
    "ethnicity",
    "full_name",
    "gender",
    "password",
    "pay",
    "phone",
    "postal_code",
    "preferred_name",
    "race",
    "race_ethnicity",
    "salary",
    "salary_expectation",
    "secret",
    "social_security_number",
    "ssn",
    "street_address",
    "token",
    "veteran_status",
    "wage",
    "zip",
    "zipcode",
}
SAFE_PREFERENCE_KEYS = {
    "company_size",
    "employment_type",
    "industries",
    "locations",
    "remote",
    "roles",
    "target_companies",
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
    max_chars: int = 24_000,
    max_evidence: int = 72,
    fact_ledger: FactLedger | None = None,
) -> CompactContextPack:
    """Build a rich bounded fact pack without contact details or raw resume dumps."""
    personal = profile.get("personal") or {}
    identity_values = tuple(
        str(value)
        for value in (
            personal.get("full_name"),
            personal.get("preferred_name"),
        )
        if value
    )
    compact_profile = _compact_profile(
        profile,
        fact_ledger=fact_ledger,
        identity_values=identity_values,
    )
    if fact_ledger is not None:
        rejected = tuple(record.value for record in fact_ledger.rejected())
        compact_profile = _remove_rejected(compact_profile, rejected)
        evidence = _select_ledger_evidence(
            fact_ledger,
            job_text=job_text,
            limit=max_evidence,
            identity_values=identity_values,
        )
    else:
        evidence = _profile_evidence(profile, identity_values=identity_values)
        evidence.extend(
            _select_resume_evidence(
                resume_text,
                job_text,
                limit=max_evidence,
                identity_values=identity_values,
            )
        )
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
        if len(serialized) <= max_chars:
            break
        if not evidence:
            raise ValueError("confirmed profile exceeds context character budget")
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
    request_id: str = "",
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
        "reasoning_guidance": [
            "Use as much internal analysis and web research as needed before answering.",
            "Consider the candidate's full supplied background, adjacent strengths, trajectory, and preferences rather than matching only title keywords.",
            "Cross-check promising roles against official employer or ATS pages.",
            "Do not expose chain-of-thought or research notes; return only the final contract object.",
        ],
        "candidate_context": pack.to_dict(),
        "output_contract": {
            "schema_version": "applypilot.chatgpt_web.v1",
            "kind": "role_candidates",
            "request_id": request_id or "omit",
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
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def build_material_prompt(
    pack: CompactContextPack,
    candidate: RoleCandidate,
    *,
    verified_job_text: str,
    request_id: str = "",
) -> str:
    """Build a provenance-oriented cover-letter request."""
    ranked_context = {
        **pack.to_dict(),
        "evidence": _rank_evidence(
            pack.evidence,
            " ".join(
                (
                    candidate.company,
                    candidate.title,
                    candidate.location,
                    verified_job_text,
                )
            ),
        ),
    }
    payload = {
        "task": "Draft a concise cover letter for human review using only supplied facts.",
        "rules": [
            "Do not invent dates, metrics, employers, tools, education, authorization, or availability.",
            "Every paragraph must cite at least one supplied fact id or JOB as support.",
            "You may paraphrase for persuasive writing, but every concrete applicant claim must remain supported by the cited evidence.",
            "For every sentence asserting something about the applicant in any voice (including I/me/my, we/our, candidate/applicant, project achievements, skills, background, experience, expertise, or strengths), copy that exact full sentence into applicant_claims and cite applicant F ids only; JOB is forbidden as applicant-claim evidence.",
            "Do not refer to the applicant as the candidate, applicant, we, or our; write applicant assertions in first person so coverage is unambiguous.",
            "Keep applicant assertions as simple evidence-grounded sentences; put job requirements or role fit in separate sentences.",
            "Flag unsupported requirements as verification gaps.",
            "Do not include phone, email, street address, salary, demographics, or passwords.",
            "Use no more than four short paragraphs and 450 words total.",
        ],
        "reasoning_guidance": [
            "Think deeply about the strongest truthful narrative connecting this candidate to this specific role.",
            "Internally compare multiple narrative angles and choose the most persuasive evidence-backed one.",
            "Use the full supplied context and verified job description, not only surface keyword overlap.",
            "Do not expose chain-of-thought; return only the final contract object.",
        ],
        "job": {
            "candidate_id": candidate.candidate_id,
            "company": candidate.company,
            "title": candidate.title,
            "official_url": candidate.official_url,
            "verified_description": verified_job_text[:12_000],
        },
        "context": ranked_context,
        "output_contract": {
            "schema_version": "applypilot.chatgpt_web.v1",
            "kind": "material_packet",
            "request_id": request_id or "omit",
            "candidate_id": candidate.candidate_id,
            "paragraphs": [
                {
                    "text": "string",
                    "evidence_ids": ["F01", "JOB"],
                    "applicant_claims": [
                        {"text": "exact full sentence from text", "evidence_ids": ["F01"]}
                    ],
                }
            ],
            "verification_gaps": ["string"],
        },
        "response_rule": "Return exactly one JSON object. No markdown or commentary.",
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _compact_profile(
    profile: dict[str, Any],
    *,
    fact_ledger: FactLedger | None,
    identity_values: tuple[str, ...],
) -> dict[str, Any]:
    if fact_ledger is not None:
        return _strip_contact_values(
            _compact_confirmed_profile(fact_ledger),
            identity_values=identity_values,
        )
    experience = profile.get("experience") or {}
    skills = profile.get("skills_boundary") or {}
    personal = profile.get("personal") or {}
    preferences = profile.get("preferences") or {}
    availability = profile.get("availability") or {}
    resume_facts = profile.get("resume_facts") or {}
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
        "work_samples_and_results": _strip_contact_values(
            resume_facts,
            identity_values=identity_values,
        ),
        "preferred_locations": _strip_contact_values(
            availability.get("preferred_locations") or [],
            identity_values=identity_values,
        ),
        "preferences": _strip_contact_values(
            {
                key: value
                for key, value in preferences.items()
                if str(key).lower() in SAFE_PREFERENCE_KEYS
            },
            identity_values=identity_values,
        ),
    }
    return {key: value for key, value in result.items() if value not in (None, "", {}, [])}


def _compact_confirmed_profile(ledger: FactLedger) -> dict[str, Any]:
    confirmed = {record.fact_id: record.value for record in ledger.confirmed()}

    def value(fact_id: str) -> str | None:
        return confirmed.get(fact_id)

    def values(prefix: str) -> list[str]:
        return [
            item
            for fact_id, item in sorted(confirmed.items())
            if fact_id.startswith(prefix) and not _fact_id_is_contact(fact_id)
        ]

    city_region = ", ".join(
        item
        for item in (
            value("profile.personal.city"),
            value("profile.personal.province_state"),
            value("profile.personal.country"),
        )
        if item
    )
    skills: dict[str, list[str]] = {}
    for fact_id, item in sorted(confirmed.items()):
        prefix = "profile.skills_boundary."
        if not fact_id.startswith(prefix) or _fact_id_is_contact(fact_id):
            continue
        category = fact_id[len(prefix) :].split(".", 1)[0]
        skills.setdefault(category, []).append(item)

    work_samples = _confirmed_grouped_values(confirmed, "profile.resume_facts.")
    preferred_locations = values("profile.availability.preferred_locations.") or values(
        "profile.preferences.locations."
    )
    preferences: dict[str, Any] = {}
    for key in SAFE_PREFERENCE_KEYS:
        items = values(f"profile.preferences.{key}.")
        scalar = value(f"profile.preferences.{key}")
        if items:
            preferences[key] = items
        elif scalar is not None:
            preferences[key] = scalar

    availability = {
        "earliest_start_date": value("profile.availability.earliest_start_date"),
    }
    work_authorization = {
        "legally_authorized_to_work": value(
            "profile.work_authorization.legally_authorized_to_work"
        ),
        "require_sponsorship": value("profile.work_authorization.require_sponsorship"),
        "work_permit_type": value("profile.work_authorization.work_permit_type"),
    }
    result = {
        "education": value("profile.experience.education_level") or value("profile.education"),
        "target_role": value("profile.experience.target_role"),
        "current_title": value("profile.experience.current_title"),
        "current_company": value("profile.experience.current_company"),
        "years_of_experience_total": value("profile.experience.years_of_experience_total"),
        "city_region": city_region,
        "skills": skills,
        "work_samples_and_results": work_samples,
        "preferred_locations": preferred_locations,
        "preferences": preferences,
        "availability": availability,
        "work_authorization": work_authorization,
    }
    return {
        key: _drop_empty(value)
        for key, value in result.items()
        if _drop_empty(value) not in (None, "", {}, [])
    }


def _confirmed_grouped_values(
    confirmed: dict[str, str],
    prefix: str,
) -> dict[str, Any]:
    grouped: dict[str, list[str]] = {}
    scalars: dict[str, str] = {}
    for fact_id, value in sorted(confirmed.items()):
        if not fact_id.startswith(prefix) or _fact_id_is_contact(fact_id):
            continue
        remainder = fact_id[len(prefix) :]
        key, separator, _tail = remainder.partition(".")
        if _sensitive_profile_key(key):
            continue
        if separator:
            grouped.setdefault(key, []).append(value)
        else:
            scalars[key] = value
    return {**scalars, **grouped}


def _profile_evidence(
    profile: dict[str, Any],
    *,
    identity_values: tuple[str, ...],
) -> list[str]:
    facts = profile.get("resume_facts") or {}
    evidence: list[str] = []
    for key in ("preserved_companies", "preserved_projects", "real_metrics"):
        values = facts.get(key) or []
        if isinstance(values, list):
            evidence.extend(
                str(value)
                for value in values
                if value
                and not _looks_like_contact_line(str(value))
                and not _matches_identity(str(value), identity_values)
            )
    preserved_school = facts.get("preserved_school")
    if (
        preserved_school
        and not _looks_like_contact_line(str(preserved_school))
        and not _matches_identity(str(preserved_school), identity_values)
    ):
        evidence.append(str(preserved_school))
    return evidence


def _select_resume_evidence(
    resume_text: str,
    job_text: str,
    *,
    limit: int,
    identity_values: tuple[str, ...],
) -> list[str]:
    if not resume_text.strip():
        return []
    job_tokens = _tokens(job_text)
    candidates: list[tuple[int, int, str]] = []
    for index, raw_line in enumerate(resume_text.splitlines()):
        line = re.sub(r"^[\s•*-]+", "", raw_line).strip()
        if (
            len(line) < 20
            or len(line) > 500
            or _looks_like_contact_line(line)
            or _matches_identity(line, identity_values)
        ):
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
    identity_values: tuple[str, ...] = (),
) -> list[str]:
    job_tokens = _tokens(job_text)
    candidates: list[tuple[int, str, str]] = []
    for record in ledger.confirmed():
        if not record.fact_id.startswith(MODEL_SAFE_FACT_PREFIXES):
            continue
        if _fact_id_is_contact(record.fact_id) or _looks_like_contact_line(record.value):
            continue
        if _matches_identity(record.value, identity_values):
            continue
        value_tokens = _tokens(record.value)
        overlap = len(value_tokens & job_tokens)
        core_profile_fact = record.fact_id.startswith(
            (
                "profile.education.",
                "profile.experience.",
                "profile.projects.",
                "profile.resume_facts.",
                "profile.skills_boundary.",
            )
        )
        profile_priority = 8 if core_profile_fact else 0
        bullet_priority = 2 if len(record.value) >= 20 else 0
        score = overlap * 10 + profile_priority + bullet_priority
        broad_resume_fact = record.fact_id.startswith("resume.line.") and len(record.value) >= 20
        if overlap or core_profile_fact or broad_resume_fact:
            candidates.append((score, record.fact_id, record.value))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [value for _, _, value in candidates[:limit]]


def _strip_contact_values(
    value: Any,
    *,
    identity_values: tuple[str, ...] = (),
) -> Any:
    if isinstance(value, dict):
        return {
            str(key): cleaned
            for key, item in value.items()
            if not _sensitive_profile_key(str(key))
            and (
                cleaned := _strip_contact_values(
                    item,
                    identity_values=identity_values,
                )
            )
            not in (None, "", {}, [])
        }
    if isinstance(value, list):
        return [
            cleaned
            for item in value
            if (
                cleaned := _strip_contact_values(
                    item,
                    identity_values=identity_values,
                )
            )
            not in (None, "", {}, [])
        ]
    if isinstance(value, str) and (
        _looks_like_contact_line(value) or _matches_identity(value, identity_values)
    ):
        return None
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
    return any(_sensitive_profile_key(part) for part in fact_id.split("."))


def _sensitive_profile_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    compact = normalized.replace("_", "")
    return any(
        normalized == sensitive
        or compact == sensitive.replace("_", "")
        or normalized.startswith(f"{sensitive}_")
        or normalized.endswith(f"_{sensitive}")
        for sensitive in SENSITIVE_KEYS
    )


def _looks_like_contact_line(line: str) -> bool:
    lowered = line.lower()
    return bool(
        "@" in line
        or "linkedin.com" in lowered
        or "github.com" in lowered
        or "http://" in lowered
        or "https://" in lowered
        or re.search(r"\b\d{3}[-.) ]\d{3}[- ]\d{4}\b", line)
        or re.search(
            r"\b\d{1,6}\s+(?:[A-Za-z0-9.'-]+\s+){0,5}"
            r"(?:street|st|road|rd|avenue|ave|boulevard|blvd|lane|ln|drive|dr|"
            r"court|ct|way|parkway|pkwy|place|pl)\b",
            line,
            re.I,
        )
        or re.search(
            r"\b(?:salary|compensation|desired\s+pay|pay\s+expectation|expected\s+pay|"
            r"salary\s+expectation)\b",
            line,
            re.I,
        )
    )


def _matches_identity(value: str, identity_values: tuple[str, ...]) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    value_tokens = set(normalized.split())
    for identity in identity_values:
        normalized_identity = re.sub(r"[^a-z0-9]+", " ", identity.lower()).strip()
        identity_tokens = set(normalized_identity.split())
        if not normalized_identity:
            continue
        if re.search(rf"\b{re.escape(normalized_identity)}\b", normalized):
            return True
        if len(identity_tokens) >= 2 and identity_tokens.issubset(value_tokens):
            return True
        if len(identity_tokens) == 1:
            token = next(iter(identity_tokens))
            if len(token) >= 4 and token in value_tokens:
                return True
    return False


def _drop_empty(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: cleaned
            for key, item in value.items()
            if (cleaned := _drop_empty(item)) not in (None, "", {}, [])
        }
    if isinstance(value, list):
        return [
            cleaned
            for item in value
            if (cleaned := _drop_empty(item)) not in (None, "", {}, [])
        ]
    return value


def _tokens(value: str) -> set[str]:
    stop = {"and", "for", "from", "that", "the", "this", "with", "your"}
    return {token for token in TOKEN_RE.findall(value.lower()) if token not in stop}


def _rank_evidence(
    evidence: tuple[dict[str, str], ...],
    relevance_text: str,
) -> list[dict[str, str]]:
    """Put role-relevant facts first while preserving the complete bounded pack."""
    relevance_tokens = _tokens(relevance_text)
    ranked = sorted(
        enumerate(evidence),
        key=lambda item: (
            -len(_tokens(item[1].get("fact", "")) & relevance_tokens),
            item[0],
        ),
    )
    return [dict(item) for _, item in ranked]


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

"""Privacy-preserving status reporting for the applicant autofill cache."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PROFILE_CACHE_VERSION = "applypilot-profile-cache-v1"
PLACEHOLDER_VALUES = {"", "n/a", "none", "null", "tbd", "unconfirmed", "unknown"}
PROHIBITED_CUSTOM_QUESTION_TERMS = (
    "account number",
    "arbitration",
    "attest",
    "bank account",
    "certify",
    "consent",
    "credit card",
    "driver license",
    "driver's license",
    "government id",
    "one time code",
    "passport",
    "password",
    "payment",
    "privacy policy",
    "routing number",
    "signature",
    "social security",
    "ssn",
    "tax id",
    "terms and conditions",
)


@dataclass(frozen=True)
class ProfileCacheField:
    path: str
    label: str
    required_for_form_work: bool
    source: str


@dataclass(frozen=True)
class CustomAnswer:
    """One exact, user-owned answer for a recurring application question."""

    source_id: str
    question: str
    aliases: tuple[str, ...]
    value: str | bool
    ats: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileCacheQuestion:
    """One applicant-owned fact that can be collected into the private cache."""

    path: str
    prompt: str
    value_kind: str


PROFILE_CACHE_FIELDS = (
    ProfileCacheField("personal.full_name", "Legal name", True, "resume_or_user"),
    ProfileCacheField("personal.email", "Email", True, "resume_or_user"),
    ProfileCacheField("personal.phone", "Phone", True, "user"),
    ProfileCacheField("personal.address", "Street address", False, "user"),
    ProfileCacheField("personal.city", "City", True, "resume_or_user"),
    ProfileCacheField("personal.province_state", "State or province", True, "resume_or_user"),
    ProfileCacheField("personal.postal_code", "Postal or ZIP code", False, "user"),
    ProfileCacheField("personal.country", "Country", True, "resume_or_user"),
    ProfileCacheField(
        "work_authorization.legally_authorized_to_work",
        "Work authorization",
        True,
        "user",
    ),
    ProfileCacheField(
        "work_authorization.require_sponsorship",
        "Current or future sponsorship need",
        True,
        "user",
    ),
    ProfileCacheField("work_authorization.work_permit_type", "Work permit or citizenship status", False, "user"),
    ProfileCacheField("eligibility.is_at_least_18", "Age 18 status", False, "user"),
    ProfileCacheField("availability.earliest_start_date", "Earliest start date", True, "user"),
    ProfileCacheField(
        "availability.available_for_full_internship_period",
        "Full internship-period availability",
        False,
        "user",
    ),
    ProfileCacheField("availability.internship_period", "Available internship dates", False, "user"),
    ProfileCacheField("availability.preferred_locations", "Willing work locations", True, "user"),
    ProfileCacheField("availability.willing_to_relocate", "Relocation willingness", False, "user"),
    ProfileCacheField("availability.willing_to_travel", "Travel willingness", False, "user"),
    ProfileCacheField("compensation.salary_expectation", "Compensation expectation", False, "user"),
    ProfileCacheField("compensation.hourly_rate_min", "Minimum hourly rate", False, "user"),
    ProfileCacheField("education.expected_graduation_date", "Expected graduation date", False, "resume_or_user"),
)

PROFILE_CACHE_QUESTIONS = (
    ProfileCacheQuestion("personal.phone", "Phone number", "text"),
    ProfileCacheQuestion("personal.address", "Street address", "text"),
    ProfileCacheQuestion("personal.postal_code", "Postal or ZIP code", "text"),
    ProfileCacheQuestion(
        "work_authorization.legally_authorized_to_work",
        "Legally authorized to work in the US",
        "boolean",
    ),
    ProfileCacheQuestion(
        "work_authorization.require_sponsorship",
        "Require visa sponsorship now or in the future",
        "boolean",
    ),
    ProfileCacheQuestion(
        "work_authorization.work_permit_type",
        "Work permit or citizenship status",
        "text",
    ),
    ProfileCacheQuestion("eligibility.is_at_least_18", "Currently at least 18 years old", "boolean"),
    ProfileCacheQuestion("availability.earliest_start_date", "Earliest start date", "text"),
    ProfileCacheQuestion(
        "availability.available_for_full_internship_period",
        "Available for the full Summer 2027 internship period",
        "boolean",
    ),
    ProfileCacheQuestion(
        "availability.internship_period",
        "Exact available Summer 2027 dates",
        "text",
    ),
    ProfileCacheQuestion(
        "availability.preferred_locations",
        "Willing work locations, comma-separated",
        "list",
    ),
    ProfileCacheQuestion("availability.willing_to_relocate", "Willing to relocate", "boolean"),
    ProfileCacheQuestion("availability.willing_to_travel", "Willing to travel", "boolean"),
    ProfileCacheQuestion(
        "compensation.salary_expectation",
        'Compensation answer, such as a range or "open to market rate"',
        "text",
    ),
    ProfileCacheQuestion(
        "compensation.hourly_rate_min",
        "Minimum hourly rate as a number",
        "text",
    ),
)
PROFILE_CACHE_QUESTION_BY_PATH = {question.path: question for question in PROFILE_CACHE_QUESTIONS}


def build_profile_cache_report(
    profile: dict[str, Any],
    *,
    resume_text: str,
    resume_pdf_path: Path | None = None,
) -> dict[str, Any]:
    """Return cache completeness and source digests without exposing fact values."""
    fields = []
    for field in PROFILE_CACHE_FIELDS:
        present = _value_is_present(_value_at_path(profile, field.path))
        fields.append({**asdict(field), "present": present})

    missing_required = [field["path"] for field in fields if field["required_for_form_work"] and not field["present"]]
    missing_recommended = [
        field["path"] for field in fields if not field["required_for_form_work"] and not field["present"]
    ]
    resume_pdf_sha256 = ""
    if resume_pdf_path is not None and resume_pdf_path.is_file():
        resume_pdf_sha256 = hashlib.sha256(resume_pdf_path.read_bytes()).hexdigest()
    custom_answers, invalid_custom_answers = load_custom_answers(profile)
    pending_verification = pending_profile_answer_paths(profile)
    return {
        "version": PROFILE_CACHE_VERSION,
        "ready_for_form_work": not missing_required,
        "missing_required": missing_required,
        "missing_recommended": missing_recommended,
        "fields": fields,
        "resume_text_sha256": hashlib.sha256(resume_text.encode("utf-8")).hexdigest(),
        "resume_pdf_sha256": resume_pdf_sha256,
        "custom_answer_count": len(custom_answers),
        "invalid_custom_answer_count": invalid_custom_answers,
        "pending_verification": list(pending_verification),
    }


def missing_profile_cache_questions(
    profile: dict[str, Any],
    *,
    required_only: bool = False,
) -> tuple[ProfileCacheQuestion, ...]:
    """Return applicant-owned questions whose cache values are still missing."""
    field_by_path = {field.path: field for field in PROFILE_CACHE_FIELDS}
    return tuple(
        question
        for question in PROFILE_CACHE_QUESTIONS
        if not _value_is_present(_value_at_path(profile, question.path))
        and (
            not required_only
            or field_by_path.get(
                question.path,
                ProfileCacheField(question.path, question.prompt, False, "user"),
            ).required_for_form_work
        )
    )


def update_profile_cache(
    profile: dict[str, Any],
    answers: dict[str, Any],
) -> dict[str, Any]:
    """Return a copied profile with validated applicant answers applied."""
    updated = deepcopy(profile)
    for path, value in answers.items():
        question = PROFILE_CACHE_QUESTION_BY_PATH.get(path)
        if question is None:
            raise ValueError(f"unsupported profile-cache field: {path}")
        normalized = _normalize_collected_value(question, value)
        current = updated
        parts = path.split(".")
        for part in parts[:-1]:
            nested = current.setdefault(part, {})
            if not isinstance(nested, dict):
                raise ValueError(f"profile path is not an object: {'.'.join(parts[:-1])}")
            current = nested
        current[parts[-1]] = normalized
    autofill = updated.get("autofill")
    if isinstance(autofill, dict) and isinstance(autofill.get("pending_answers"), list):
        autofill["pending_answers"] = [
            item
            for item in autofill["pending_answers"]
            if not isinstance(item, dict) or item.get("path") not in answers
        ]
    return updated


def write_private_profile(path: Path, profile: dict[str, Any]) -> Path | None:
    """Back up and atomically replace a private profile with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    backup_path: Path | None = None
    if path.is_file():
        backup_dir = path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_dir.chmod(0o700)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = backup_dir / f"profile-before-collect-{timestamp}.json"
        shutil.copyfile(path, backup_path)
        backup_path.chmod(0o600)

    payload = json.dumps(profile, indent=2, ensure_ascii=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return backup_path


def match_custom_answer(
    profile: dict[str, Any],
    *,
    question_texts: tuple[str, ...],
    ats: str = "",
    url: str = "",
) -> CustomAnswer | None:
    """Match one exact cached answer without fuzzy inference."""
    normalized_questions = {normalized for text in question_texts if (normalized := normalize_question(text))}
    if not normalized_questions or any(custom_question_is_prohibited(text) for text in question_texts):
        return None

    host = (urlparse(url).hostname or "").lower()
    matches: list[CustomAnswer] = []
    answers, _invalid = load_custom_answers(profile)
    for answer in answers:
        if answer.ats and ats.lower() not in answer.ats:
            continue
        if answer.domains and not any(host == domain or host.endswith(f".{domain}") for domain in answer.domains):
            continue
        answer_questions = {
            normalize_question(text) for text in (answer.question, *answer.aliases) if normalize_question(text)
        }
        if normalized_questions & answer_questions:
            matches.append(answer)
    if not matches:
        return None
    values = {(type(answer.value).__name__, str(answer.value)) for answer in matches}
    return matches[0] if len(values) == 1 else None


def pending_profile_answer_paths(profile: dict[str, Any]) -> tuple[str, ...]:
    """Return valid profile paths retained privately pending user verification."""
    raw_answers = profile.get("autofill", {}).get("pending_answers", [])
    if not isinstance(raw_answers, list):
        return ()
    paths = {
        str(item.get("path") or "").strip()
        for item in raw_answers
        if isinstance(item, dict)
        and str(item.get("path") or "").strip() in PROFILE_CACHE_QUESTION_BY_PATH
        and _value_is_present(item.get("value"))
    }
    return tuple(sorted(paths))


def load_custom_answers(profile: dict[str, Any]) -> tuple[tuple[CustomAnswer, ...], int]:
    """Load valid custom answers and count malformed or prohibited entries."""
    raw_answers = profile.get("autofill", {}).get("custom_answers", [])
    if not isinstance(raw_answers, list):
        return (), 1
    valid: list[CustomAnswer] = []
    invalid = 0
    for index, raw in enumerate(raw_answers):
        if not isinstance(raw, dict):
            invalid += 1
            continue
        question = str(raw.get("question") or "").strip()
        value = raw.get("value")
        aliases = _string_tuple(raw.get("aliases"))
        ats = tuple(item.lower() for item in _string_tuple(raw.get("ats")))
        domains = tuple(item.lower().lstrip(".") for item in _string_tuple(raw.get("domains")))
        if (
            not question
            or not isinstance(value, str | bool)
            or (isinstance(value, str) and not _value_is_present(value))
            or custom_question_is_prohibited(question)
            or any(custom_question_is_prohibited(alias) for alias in aliases)
        ):
            invalid += 1
            continue
        valid.append(
            CustomAnswer(
                source_id=f"autofill.custom_answers.{index}",
                question=question,
                aliases=aliases,
                value=value,
                ats=ats,
                domains=domains,
            )
        )
    return tuple(valid), invalid


def custom_question_is_prohibited(text: str) -> bool:
    normalized = normalize_question(text)
    return any(term in normalized for term in PROHIBITED_CUSTOM_QUESTION_TERMS)


def normalize_question(text: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return re.sub(r"\b(required|optional)\b", "", normalized).strip()


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _normalize_collected_value(question: ProfileCacheQuestion, value: Any) -> Any:
    if question.value_kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{question.path} requires a boolean")
        return value
    if question.value_kind == "list":
        if not isinstance(value, list | tuple):
            raise ValueError(f"{question.path} requires a list")
        normalized = [str(item).strip() for item in value if str(item).strip()]
        if not normalized:
            raise ValueError(f"{question.path} requires at least one value")
        return normalized
    if question.value_kind == "text":
        normalized = str(value).strip()
        if not _value_is_present(normalized):
            raise ValueError(f"{question.path} requires a non-placeholder value")
        return normalized
    raise ValueError(f"unsupported profile-cache value kind: {question.value_kind}")


def _value_at_path(profile: dict[str, Any], path: str) -> Any:
    current: Any = profile
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _value_is_present(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in PLACEHOLDER_VALUES
    if isinstance(value, list | tuple | set | dict):
        return bool(value)
    return True

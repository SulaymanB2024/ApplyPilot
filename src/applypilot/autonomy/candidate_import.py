"""Strict, non-submitting import bridge for browser-verified job candidates."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from applypilot.apply.runtime import domain_from_job_url, normalized_url_value
from applypilot.database import store_jobs


IMPORT_SCHEMA_VERSION = 1
IMPORT_STRATEGY = "chatgpt_web_verified"
_REQUIRED_FIELDS = frozenset({"official_url", "title", "company"})
_OPTIONAL_FIELDS = frozenset(
    {"location", "description", "salary", "requisition", "application_url"}
)
_ALLOWED_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS
_ALLOWED_DOCUMENT_FIELDS = frozenset({"schema_version", "candidates"})

# Kept next to the runtime validator so browser/model producers have one exact
# contract to follow. Runtime validation below does not depend on jsonschema.
CANDIDATE_IMPORT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "candidates"],
    "properties": {
        "schema_version": {"const": IMPORT_SCHEMA_VERSION},
        "candidates": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(_REQUIRED_FIELDS),
                "properties": {
                    "official_url": {"type": "string", "format": "uri", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "company": {"type": "string", "minLength": 1},
                    "location": {"type": "string", "minLength": 1},
                    "description": {"type": "string", "minLength": 1},
                    "salary": {"type": "string", "minLength": 1},
                    "requisition": {"type": "string", "minLength": 1},
                    "application_url": {"type": "string", "format": "uri", "minLength": 1},
                },
            },
        },
    },
}


class CandidateImportError(ValueError):
    """Raised when a candidate document does not match the strict contract."""


@dataclass(frozen=True)
class VerifiedCandidate:
    """Validated candidate fields that may be written to the jobs database."""

    official_url: str
    title: str
    company: str
    location: str | None = None
    description: str | None = None
    salary: str | None = None
    requisition: str | None = None
    application_url: str | None = None


@dataclass(frozen=True)
class CandidateImportResult:
    """Counts from a candidate import. No application action is represented."""

    total: int
    imported: int
    duplicates: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "status": "candidates_imported",
            "total": self.total,
            "imported": self.imported,
            "duplicates": self.duplicates,
        }


def load_candidate_document(path: Path | str) -> tuple[VerifiedCandidate, ...]:
    """Read and fully validate a versioned candidate JSON document."""
    candidate_path = Path(path)
    try:
        raw = json.loads(candidate_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CandidateImportError(f"candidate file not found: {candidate_path}") from exc
    except OSError as exc:
        raise CandidateImportError(f"candidate file could not be read: {candidate_path}") from exc
    except json.JSONDecodeError as exc:
        raise CandidateImportError(
            f"candidate file is not valid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc

    if not isinstance(raw, dict):
        raise CandidateImportError("candidate document must be a JSON object")

    unknown_document_fields = set(raw) - _ALLOWED_DOCUMENT_FIELDS
    if unknown_document_fields:
        raise CandidateImportError(
            "candidate document has unknown field(s): "
            + ", ".join(sorted(str(field) for field in unknown_document_fields))
        )
    missing_document_fields = _ALLOWED_DOCUMENT_FIELDS - set(raw)
    if missing_document_fields:
        raise CandidateImportError(
            "candidate document is missing field(s): "
            + ", ".join(sorted(missing_document_fields))
        )
    if type(raw["schema_version"]) is not int or raw["schema_version"] != IMPORT_SCHEMA_VERSION:
        raise CandidateImportError(
            f"schema_version must be the integer {IMPORT_SCHEMA_VERSION}"
        )

    candidate_values = raw["candidates"]
    if not isinstance(candidate_values, list):
        raise CandidateImportError("candidates must be a JSON array")
    if not candidate_values:
        raise CandidateImportError("candidates must contain at least one item")

    return tuple(
        _validate_candidate(value, index=index)
        for index, value in enumerate(candidate_values)
    )


def import_candidate_file(
    conn: sqlite3.Connection,
    path: Path | str,
) -> CandidateImportResult:
    """Import verified candidates without applying, submitting, or setting status."""
    candidates = load_candidate_document(path)
    imported = 0
    duplicates = 0

    for candidate in candidates:
        job = {
            "url": candidate.official_url,
            "title": candidate.title,
            "salary": candidate.salary,
            "description": candidate.description,
            "location": candidate.location,
            "application_url": candidate.application_url,
        }
        new_count, duplicate_count = store_jobs(
            conn,
            [job],
            site=candidate.company or domain_from_job_url(candidate.official_url),
            strategy=IMPORT_STRATEGY,
        )
        imported += new_count
        duplicates += duplicate_count

        if new_count:
            # Preserve only supplied facts. These columns are descriptive and
            # cannot mark, queue, or submit an application.
            conn.execute(
                """
                UPDATE jobs
                SET full_description = ?, application_url = ?, requisition = ?
                WHERE url = ?
                """,
                (
                    candidate.description,
                    candidate.application_url,
                    candidate.requisition,
                    candidate.official_url,
                ),
            )
            conn.commit()

    return CandidateImportResult(
        total=len(candidates),
        imported=imported,
        duplicates=duplicates,
    )


def _validate_candidate(value: object, *, index: int) -> VerifiedCandidate:
    label = f"candidates[{index}]"
    if not isinstance(value, dict):
        raise CandidateImportError(f"{label} must be a JSON object")

    unknown_fields = set(value) - _ALLOWED_FIELDS
    if unknown_fields:
        raise CandidateImportError(
            f"{label} has unknown field(s): "
            + ", ".join(sorted(str(field) for field in unknown_fields))
        )
    missing_fields = _REQUIRED_FIELDS - set(value)
    if missing_fields:
        raise CandidateImportError(
            f"{label} is missing field(s): " + ", ".join(sorted(missing_fields))
        )

    official_url = _validated_url(value["official_url"], label=f"{label}.official_url")
    application_url = None
    if "application_url" in value:
        application_url = _validated_url(
            value["application_url"],
            label=f"{label}.application_url",
        )

    return VerifiedCandidate(
        official_url=official_url,
        title=_validated_text(value["title"], label=f"{label}.title"),
        company=_validated_text(value["company"], label=f"{label}.company"),
        location=_optional_text(value, "location", label=label),
        description=_optional_text(value, "description", label=label),
        salary=_optional_text(value, "salary", label=label),
        requisition=_optional_text(value, "requisition", label=label),
        application_url=application_url,
    )


def _optional_text(value: dict[object, object], field: str, *, label: str) -> str | None:
    if field not in value:
        return None
    return _validated_text(value[field], label=f"{label}.{field}")


def _validated_text(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise CandidateImportError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized:
        raise CandidateImportError(f"{label} must not be blank")
    return normalized


def _validated_url(value: object, *, label: str) -> str:
    normalized = normalized_url_value(_validated_text(value, label=label))
    if any(character.isspace() for character in normalized):
        raise CandidateImportError(f"{label} must be an absolute HTTP(S) URL")

    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise CandidateImportError(f"{label} must be an absolute HTTP(S) URL") from exc

    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise CandidateImportError(f"{label} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise CandidateImportError(f"{label} must not contain URL credentials")

    # Fragments never affect job identity. Keep the path and query intact so
    # canonical_job_id/store_jobs remain the single deduplication authority.
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            "",
        )
    )

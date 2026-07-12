"""Versioned applicant fact ledger and correction enforcement."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable


FACT_LEDGER_VERSION = "applypilot-facts-v1"
REQUIRED_AUTONOMY_FACT_IDS = (
    "profile.personal.phone",
    "profile.work_authorization.legally_authorized_to_work",
    "profile.work_authorization.require_sponsorship",
    "profile.availability.earliest_start_date",
)
PREFERRED_LOCATION_FACT_PREFIXES = (
    "profile.availability.preferred_locations.",
    "profile.preferences.locations.",
)


class FactState(StrEnum):
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"
    REJECTED = "rejected"


@dataclass(frozen=True)
class FactCorrection:
    """User-owned correction applied to every source snapshot."""

    match: str
    state: FactState
    reason: str
    fact_id: str = ""


@dataclass(frozen=True)
class FactRecord:
    fact_id: str
    value: str
    state: FactState
    source: str
    source_sha256: str
    reason: str = ""


@dataclass(frozen=True)
class FactLedger:
    version: str
    profile_sha256: str
    resume_sha256: str
    records: tuple[FactRecord, ...]
    digest: str

    def confirmed(self) -> tuple[FactRecord, ...]:
        return tuple(record for record in self.records if record.state is FactState.CONFIRMED)

    def rejected(self) -> tuple[FactRecord, ...]:
        return tuple(record for record in self.records if record.state is FactState.REJECTED)

    def unknown(self) -> tuple[FactRecord, ...]:
        return tuple(record for record in self.records if record.state is FactState.UNKNOWN)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "profile_sha256": self.profile_sha256,
            "resume_sha256": self.resume_sha256,
            "digest": self.digest,
            "records": [asdict(record) for record in self.records],
        }


def build_fact_ledger(
    profile: dict[str, Any],
    *,
    resume_text: str,
    corrections: Iterable[FactCorrection] = (),
) -> FactLedger:
    """Build a deterministic ledger without mutating source files."""
    profile_raw = json.dumps(profile, sort_keys=True, ensure_ascii=False)
    profile_hash = _hash(profile_raw)
    resume_hash = _hash(resume_text)
    correction_list = tuple(corrections)
    records: list[FactRecord] = []

    for path, value in _flatten(profile):
        if _fact_path_is_secret(path):
            continue
        text = _scalar_text(value)
        if text is None:
            continue
        state = FactState.UNKNOWN if _unknown_value(text) else FactState.CONFIRMED
        reason = "blank, null, unconfirmed, or placeholder value" if state is FactState.UNKNOWN else ""
        fact_id = f"profile.{path}"
        state, reason = _apply_corrections(fact_id, text, state, reason, correction_list)
        records.append(
            FactRecord(
                fact_id=fact_id,
                value=text,
                state=state,
                source="profile.json",
                source_sha256=profile_hash,
                reason=reason,
            )
        )

    for index, line in enumerate(resume_text.splitlines(), start=1):
        text = re.sub(r"\s+", " ", line).strip()
        if not text:
            continue
        fact_id = f"resume.line.{index:04d}"
        state = FactState.UNKNOWN if _unknown_value(text) else FactState.CONFIRMED
        reason = "placeholder or unconfirmed resume line" if state is FactState.UNKNOWN else ""
        state, reason = _apply_corrections(fact_id, text, state, reason, correction_list)
        records.append(
            FactRecord(
                fact_id=fact_id,
                value=text,
                state=state,
                source="resume.txt",
                source_sha256=resume_hash,
                reason=reason,
            )
        )

    correction_hash = _hash(
        json.dumps([asdict(correction) for correction in correction_list], sort_keys=True, default=str)
    )
    for index, correction in enumerate(correction_list, start=1):
        match = correction.match.strip()
        if correction.state is not FactState.REJECTED or not match:
            continue
        normalized_match = _normalize(match)
        if any(
            record.state is FactState.REJECTED
            and normalized_match
            and normalized_match in _normalize(record.value)
            for record in records
        ):
            continue
        records.append(
            FactRecord(
                fact_id=correction.fact_id or f"correction.rejected.{index:04d}",
                value=match,
                state=FactState.REJECTED,
                source="fact_corrections.json",
                source_sha256=correction_hash,
                reason=correction.reason,
            )
        )

    digest = _ledger_digest(
        version=FACT_LEDGER_VERSION,
        profile_sha256=profile_hash,
        resume_sha256=resume_hash,
        records=records,
    )
    return FactLedger(
        version=FACT_LEDGER_VERSION,
        profile_sha256=profile_hash,
        resume_sha256=resume_hash,
        records=tuple(records),
        digest=digest,
    )


def load_corrections(path: Path) -> tuple[FactCorrection, ...]:
    if not path.exists():
        raise FileNotFoundError(f"fact corrections file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("corrections", []) if isinstance(payload, dict) else []
    result: list[FactCorrection] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        result.append(
            FactCorrection(
                match=str(row.get("match") or ""),
                state=FactState(str(row.get("state") or "unknown")),
                reason=str(row.get("reason") or ""),
                fact_id=str(row.get("fact_id") or ""),
            )
        )
    return tuple(result)


def fact_ledger_from_dict(payload: dict[str, Any]) -> FactLedger:
    """Load an immutable fact-ledger snapshot and verify its content digest."""
    if payload.get("version") != FACT_LEDGER_VERSION:
        raise ValueError("unsupported fact ledger version")
    records_payload = payload.get("records")
    if not isinstance(records_payload, list):
        raise ValueError("fact ledger records must be a list")
    records: list[FactRecord] = []
    for raw in records_payload:
        if not isinstance(raw, dict):
            raise ValueError("fact ledger record must be an object")
        records.append(
            FactRecord(
                fact_id=str(raw.get("fact_id") or ""),
                value=str(raw.get("value") or ""),
                state=FactState(str(raw.get("state") or "unknown")),
                source=str(raw.get("source") or ""),
                source_sha256=str(raw.get("source_sha256") or ""),
                reason=str(raw.get("reason") or ""),
            )
        )
    profile_sha256 = str(payload.get("profile_sha256") or "")
    resume_sha256 = str(payload.get("resume_sha256") or "")
    if not profile_sha256 or not resume_sha256:
        raise ValueError("fact ledger source digests are missing")
    for record in records:
        if not record.fact_id or not record.source or not record.source_sha256:
            raise ValueError("fact ledger record bindings are incomplete")
        expected_source_hash = {
            "profile.json": profile_sha256,
            "resume.txt": resume_sha256,
        }.get(record.source)
        if expected_source_hash is not None and record.source_sha256 != expected_source_hash:
            raise ValueError("fact ledger record source digest mismatch")
    expected_digest = _ledger_digest(
        version=FACT_LEDGER_VERSION,
        profile_sha256=profile_sha256,
        resume_sha256=resume_sha256,
        records=records,
    )
    if payload.get("digest") != expected_digest:
        raise ValueError("fact ledger content digest mismatch")
    return FactLedger(
        version=FACT_LEDGER_VERSION,
        profile_sha256=profile_sha256,
        resume_sha256=resume_sha256,
        records=tuple(records),
        digest=expected_digest,
    )


def validate_artifact_against_ledger(text: str, ledger: FactLedger) -> list[str]:
    """Return deterministic artifact blockers for rejected facts/placeholders."""
    blockers: list[str] = []
    normalized = _normalize(text)
    placeholders = sorted(set(re.findall(r"\[[A-Za-z][^\]\n]{0,40}\]", text)))
    if placeholders:
        blockers.append(f"artifact_contains_placeholders:{','.join(placeholders)}")
    for record in ledger.rejected():
        rejected = _normalize(record.value)
        if rejected and rejected in normalized:
            blockers.append(f"artifact_contains_rejected_fact:{record.fact_id}")
    return blockers


def require_confirmed_facts(ledger: FactLedger, fact_ids: Iterable[str]) -> list[str]:
    """Return missing/unknown/rejected fact ids required by a runtime step."""
    by_id = {record.fact_id: record for record in ledger.records}
    blockers: list[str] = []
    for fact_id in fact_ids:
        record = by_id.get(fact_id)
        if record is None:
            blockers.append(f"missing:{fact_id}")
        elif record.state is not FactState.CONFIRMED:
            blockers.append(f"{record.state.value}:{fact_id}")
    return blockers


def confirmed_preferred_location_fact_ids(ledger: FactLedger) -> tuple[str, ...]:
    """Return exact confirmed location fact IDs accepted by live approval."""
    return tuple(
        sorted(
            record.fact_id
            for record in ledger.records
            if record.state is FactState.CONFIRMED
            and any(
                record.fact_id.startswith(prefix)
                for prefix in PREFERRED_LOCATION_FACT_PREFIXES
            )
        )
    )


def _apply_corrections(
    fact_id: str,
    value: str,
    state: FactState,
    reason: str,
    corrections: tuple[FactCorrection, ...],
) -> tuple[FactState, str]:
    normalized = _normalize(value)
    for correction in corrections:
        if correction.fact_id and correction.fact_id != fact_id:
            continue
        match = _normalize(correction.match)
        if match and match not in normalized:
            continue
        if correction.fact_id or match:
            state = correction.state
            reason = correction.reason
    return state, reason


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key in sorted(value):
            path = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_flatten(value[key], path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            rows.extend(_flatten(item, f"{prefix}.{index}"))
    else:
        rows.append((prefix, value))
    return rows


def _scalar_text(value: Any) -> str | None:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | str):
        return str(value).strip()
    return None


def _unknown_value(value: str) -> bool:
    normalized = value.strip().lower()
    return (
        normalized in {"", "null", "none", "unknown", "unconfirmed", "tbd", "n/a"}
        or bool(re.fullmatch(r"\[[^\]]+\]", value.strip()))
    )


def _fact_path_is_secret(path: str) -> bool:
    leaf = path.rsplit(".", 1)[-1].lower()
    return leaf in {"api_key", "apikey", "password", "secret", "token"}


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ledger_digest(
    *,
    version: str,
    profile_sha256: str,
    resume_sha256: str,
    records: Iterable[FactRecord],
) -> str:
    payload = {
        "version": version,
        "profile_sha256": profile_sha256,
        "resume_sha256": resume_sha256,
        "records": [asdict(record) for record in records],
    }
    return _hash(
        json.dumps(
            payload,
            sort_keys=True,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

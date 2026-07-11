"""One-time, candidate-scoped authorization manifests for final submission."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from applypilot import config
from applypilot.apply.runtime import canonical_job_id

MANIFEST_VERSION = "applypilot-submit-authorization-v1"


def candidate_id_for_job(job: dict[str, Any]) -> str:
    url = str(job.get("application_url") or job.get("url") or "")
    if not url:
        raise ValueError("submission candidate URL is missing")
    return canonical_job_id(url)


def material_digest(job: dict[str, Any]) -> str:
    """Hash the exact reviewable text and upload bytes for a job."""
    hasher = hashlib.sha256()
    resume_path = job.get("tailored_resume_path")
    if not resume_path:
        raise ValueError("tailored resume path is missing")
    paths: list[tuple[str, Path, bool]] = [
        ("resume_text", Path(resume_path).with_suffix(".txt"), True),
        ("resume_pdf", Path(resume_path).with_suffix(".pdf"), True),
    ]
    cover_path = job.get("cover_letter_path")
    if cover_path:
        paths.extend(
            [
                ("cover_text", Path(cover_path).with_suffix(".txt"), True),
                ("cover_pdf", Path(cover_path).with_suffix(".pdf"), True),
            ]
        )
    for label, path, required in paths:
        if not path.exists():
            if required:
                raise FileNotFoundError(f"submission material missing: {label}")
            continue
        hasher.update(label.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def form_review_digest(fields: Iterable[Any]) -> str:
    """Hash form structure and filled values without persisting raw values."""
    rows: list[dict[str, Any]] = []
    for field in fields:
        raw = asdict(field) if is_dataclass(field) else dict(field)
        value = str(raw.pop("value", ""))
        raw["value_sha256"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
        rows.append(raw)
    rows.sort(key=lambda row: str(row.get("selector") or row.get("name") or ""))
    return _digest(rows)


def submission_policy_digest(settings: Any) -> str:
    return _digest(
        {
            "version": "deterministic-submit-policy-v1",
            "deterministic_controller": bool(settings.deterministic_controller),
            "field_model_call_budget": int(settings.field_model_call_budget),
            "allow_account_creation": bool(settings.allow_account_creation),
            "credential_provider": str(settings.credential_provider),
        }
    )


def write_submit_manifest(
    *,
    job: dict[str, Any],
    fact_digest: str,
    material_sha256: str,
    form_sha256: str,
    policy_sha256: str,
    output_dir: Path,
    ttl_minutes: int = 30,
) -> Path:
    now = datetime.now(timezone.utc)
    nonce = secrets.token_hex(16)
    payload = {
        "version": MANIFEST_VERSION,
        "nonce": nonce,
        "candidate_id": candidate_id_for_job(job),
        "fact_digest": fact_digest,
        "material_digest": material_sha256,
        "form_review_digest": form_sha256,
        "policy_digest": policy_sha256,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=ttl_minutes)).isoformat(),
        "allowed_action": "submit_application",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{nonce}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def consume_submit_manifest(
    path: Path,
    *,
    job: dict[str, Any],
    fact_digest: str,
    material_sha256: str,
    form_sha256: str,
    policy_sha256: str,
    now: datetime | None = None,
    authorization_dir: Path | None = None,
) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    nonce = str(payload.get("nonce") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise PermissionError("submission authorization nonce invalid")
    store = authorization_dir or (config.APP_DIR / "submit-authorizations")
    canonical_path = store / f"{nonce}.json"
    if not canonical_path.exists():
        raise PermissionError("canonical submission authorization not found")
    canonical_payload = json.loads(canonical_path.read_text(encoding="utf-8"))
    if payload != canonical_payload:
        raise PermissionError("submission authorization differs from canonical manifest")
    expected = {
        "version": MANIFEST_VERSION,
        "candidate_id": candidate_id_for_job(job),
        "fact_digest": fact_digest,
        "material_digest": material_sha256,
        "form_review_digest": form_sha256,
        "policy_digest": policy_sha256,
        "allowed_action": "submit_application",
    }
    mismatches = [key for key, value in expected.items() if payload.get(key) != value]
    if mismatches:
        raise PermissionError("submission authorization mismatch:" + ",".join(mismatches))
    current = now or datetime.now(timezone.utc)
    expires_at = datetime.fromisoformat(str(payload.get("expires_at") or ""))
    if expires_at.tzinfo is None or current > expires_at:
        raise PermissionError("submission authorization expired")
    consumed_dir = store / "consumed"
    consumed_dir.mkdir(parents=True, exist_ok=True)
    marker = consumed_dir / nonce
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise PermissionError("submission authorization already consumed") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(current.isoformat())


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

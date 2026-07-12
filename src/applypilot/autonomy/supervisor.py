"""Redacted runtime observations for unattended autonomy supervisors.

The browser connector and Chronicle recorder live outside ApplyPilot's process.
This module gives those external tools one deliberately small diagnostic
contract.  Observations are untrusted, expire quickly, and can never count as
application or submission evidence.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


RUNTIME_OBSERVATION_SCHEMA_VERSION = "applypilot-runtime-observation-v1"
RUNTIME_STATUS_SCHEMA_VERSION = "applypilot-runtime-status-v1"
RUNTIME_OBSERVATION_NAME = "runtime_observation.json"
RUNTIME_OBSERVATION_TRUST = "externally_observed_untrusted"
DEFAULT_RUNTIME_TTL_SECONDS = 360
MIN_RUNTIME_TTL_SECONDS = 30
MAX_RUNTIME_TTL_SECONDS = 600
MAX_FRESH_FRAME_AGE_SECONDS = 30
MAX_RUNTIME_OBSERVATION_BYTES = 16_384

CHRONICLE_STATES = frozenset(
    {"capturing", "idle_paused", "stale", "unavailable", "unknown"}
)
CHRONICLE_EVIDENCE_CODES = frozenset(
    {
        "fresh_frame_observed",
        "system_idle_reported",
        "frame_stale",
        "process_unavailable",
        "not_observed",
    }
)
BROWSER_SURFACES = frozenset(
    {"codex_chrome_connector", "wrong_surface", "unavailable", "unknown"}
)
BROWSER_READINESS_STATES = frozenset(
    {"ready", "unauthenticated", "unavailable", "unknown"}
)
SCOPE_KINDS = frozenset({"run", "campaign"})

_SAFE_SCOPE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_OBSERVATION_FIELDS = frozenset(
    {
        "schema_version",
        "scope_kind",
        "scope_id",
        "observed_at",
        "ttl_seconds",
        "chronicle_state",
        "chronicle_evidence_code",
        "latest_frame_at",
        "browser_surface",
        "browser_readiness",
        "state_trust",
    }
)


def record_runtime_observation(
    *,
    root: Path,
    scope_kind: str,
    scope_id: str,
    chronicle_state: str,
    chronicle_evidence_code: str,
    browser_surface: str,
    browser_readiness: str,
    latest_frame_at: str | datetime | None = None,
    ttl_seconds: int = DEFAULT_RUNTIME_TTL_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Atomically record one exact-schema external diagnostic observation."""
    resolved_root = _require_runtime_root(root)
    current = _aware_utc(now)
    frame_time = _optional_datetime(latest_frame_at, field="latest Chronicle frame")
    payload = {
        "schema_version": RUNTIME_OBSERVATION_SCHEMA_VERSION,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "observed_at": current.isoformat(),
        "ttl_seconds": ttl_seconds,
        "chronicle_state": chronicle_state,
        "chronicle_evidence_code": chronicle_evidence_code,
        "latest_frame_at": frame_time.isoformat() if frame_time is not None else None,
        "browser_surface": browser_surface,
        "browser_readiness": browser_readiness,
        "state_trust": RUNTIME_OBSERVATION_TRUST,
    }
    _validate_observation_payload(
        payload,
        expected_scope_kind=scope_kind,
        expected_scope_id=scope_id,
        current=current,
        require_fresh_observation=True,
    )
    _atomic_write_json(resolved_root / RUNTIME_OBSERVATION_NAME, payload)
    return runtime_observation_snapshot(
        root=resolved_root,
        scope_kind=scope_kind,
        scope_id=scope_id,
        now=current,
    )


def runtime_observation_snapshot(
    *,
    root: Path,
    scope_kind: str,
    scope_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read one bounded effective runtime status, failing closed on corruption."""
    resolved_root = _require_runtime_root(root)
    _validate_scope(scope_kind=scope_kind, scope_id=scope_id)
    current = _aware_utc(now)
    path = resolved_root / RUNTIME_OBSERVATION_NAME
    if not path.exists():
        return _missing_runtime_status(
            scope_kind=scope_kind,
            scope_id=scope_id,
        )
    if path.is_symlink() or not path.is_file():
        raise ValueError("runtime observation must be a regular non-symlink file")
    if path.stat().st_size > MAX_RUNTIME_OBSERVATION_BYTES:
        raise ValueError("runtime observation exceeds its bounded size")
    payload = json.loads(path.read_text(encoding="utf-8"))
    _validate_observation_payload(
        payload,
        expected_scope_kind=scope_kind,
        expected_scope_id=scope_id,
        current=current,
        require_fresh_observation=False,
    )

    observed_at = _parse_datetime(str(payload["observed_at"]), field="runtime observation")
    observation_age_seconds = max(0, int((current - observed_at).total_seconds()))
    ttl_seconds = int(payload["ttl_seconds"])
    observation_expired = current > observed_at + timedelta(seconds=ttl_seconds)

    frame_at = _optional_datetime(payload["latest_frame_at"], field="latest Chronicle frame")
    frame_age_seconds = (
        max(0, int((current - frame_at).total_seconds()))
        if frame_at is not None
        else None
    )
    chronicle_state = str(payload["chronicle_state"])
    frame_expired = chronicle_state == "capturing" and (
        frame_at is None
        or current
        > frame_at + timedelta(seconds=MAX_FRESH_FRAME_AGE_SECONDS)
    )
    observation_state = "stale" if observation_expired or frame_expired else "fresh"
    effective_chronicle_state = (
        "stale" if observation_expired or frame_expired else chronicle_state
    )
    browser_surface = str(payload["browser_surface"])
    browser_readiness = str(payload["browser_readiness"])
    runtime_ready = (
        observation_state == "fresh"
        and effective_chronicle_state == "capturing"
        and browser_surface == "codex_chrome_connector"
        and browser_readiness == "ready"
    )
    return {
        "schema_version": RUNTIME_STATUS_SCHEMA_VERSION,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "observation_state": observation_state,
        "runtime_ready": runtime_ready,
        "chronicle_state": effective_chronicle_state,
        "chronicle_evidence_code": str(payload["chronicle_evidence_code"]),
        "latest_frame_at": frame_at.isoformat() if frame_at is not None else None,
        "latest_frame_age_seconds": frame_age_seconds,
        "browser_surface": browser_surface,
        "browser_readiness": browser_readiness,
        "observed_at": observed_at.isoformat(),
        "observation_age_seconds": observation_age_seconds,
        "ttl_seconds": ttl_seconds,
        "state_trust": RUNTIME_OBSERVATION_TRUST,
    }


def runtime_semantic_state(status: Mapping[str, Any]) -> dict[str, Any]:
    """Return timestamp-free runtime fields suitable for progress hashing."""
    fields = (
        "observation_state",
        "runtime_ready",
        "chronicle_state",
        "chronicle_evidence_code",
        "browser_surface",
        "browser_readiness",
    )
    if status.get("schema_version") != RUNTIME_STATUS_SCHEMA_VERSION:
        raise ValueError("runtime status schema is invalid")
    if any(field not in status for field in fields):
        raise ValueError("runtime status is missing semantic fields")
    return {field: status[field] for field in fields}


def runtime_gated_decision(
    *,
    next_action_owner: str,
    next_action_code: str,
    browser_required: bool,
    runtime_status: Mapping[str, Any],
) -> tuple[str, str, bool]:
    """Hold browser work until the correct fresh external runtime is observed."""
    if not browser_required:
        return next_action_owner, next_action_code, False
    semantic = runtime_semantic_state(runtime_status)
    if semantic["runtime_ready"]:
        return next_action_owner, next_action_code, True
    if semantic["observation_state"] != "fresh":
        return "controller", "refresh_runtime_observation", False
    if semantic["browser_surface"] == "wrong_surface":
        return "system_admin", "activate_codex_chrome_connector", False
    if semantic["browser_readiness"] == "unauthenticated":
        return "applicant", "authenticate_chatgpt_web", False
    if semantic["browser_surface"] == "unavailable" or semantic[
        "browser_readiness"
    ] == "unavailable":
        return "controller", "restore_codex_chrome_connector", False
    if semantic["chronicle_state"] in {
        "idle_paused",
        "stale",
        "unavailable",
    }:
        return "controller", "restore_chronicle_capture", False
    return "controller", "refresh_runtime_observation", False


def require_browser_runtime(
    *,
    root: Path,
    scope_kind: str,
    scope_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Require a fresh correct connector/capture observation before response use."""
    status = runtime_observation_snapshot(
        root=root,
        scope_kind=scope_kind,
        scope_id=scope_id,
        now=now,
    )
    _owner, action_code, allowed = runtime_gated_decision(
        next_action_owner="browser_connector",
        next_action_code="execute_browser_handoff",
        browser_required=True,
        runtime_status=status,
    )
    if not allowed:
        raise PermissionError(f"browser handoff blocked: {action_code}")
    return status


def _missing_runtime_status(*, scope_kind: str, scope_id: str) -> dict[str, Any]:
    return {
        "schema_version": RUNTIME_STATUS_SCHEMA_VERSION,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "observation_state": "missing",
        "runtime_ready": False,
        "chronicle_state": "unknown",
        "chronicle_evidence_code": "not_observed",
        "latest_frame_at": None,
        "latest_frame_age_seconds": None,
        "browser_surface": "unknown",
        "browser_readiness": "unknown",
        "observed_at": None,
        "observation_age_seconds": None,
        "ttl_seconds": None,
        "state_trust": RUNTIME_OBSERVATION_TRUST,
    }


def _validate_observation_payload(
    payload: Any,
    *,
    expected_scope_kind: str,
    expected_scope_id: str,
    current: datetime,
    require_fresh_observation: bool,
) -> None:
    _validate_scope(scope_kind=expected_scope_kind, scope_id=expected_scope_id)
    if not isinstance(payload, dict) or set(payload) != _OBSERVATION_FIELDS:
        raise ValueError("runtime observation must use the exact schema")
    if payload.get("schema_version") != RUNTIME_OBSERVATION_SCHEMA_VERSION:
        raise ValueError("runtime observation schema is invalid")
    if payload.get("scope_kind") != expected_scope_kind or payload.get(
        "scope_id"
    ) != expected_scope_id:
        raise ValueError("runtime observation scope binding is invalid")
    if payload.get("state_trust") != RUNTIME_OBSERVATION_TRUST:
        raise ValueError("runtime observation trust label is invalid")

    observed_raw = payload.get("observed_at")
    if not isinstance(observed_raw, str):
        raise ValueError("runtime observation timestamp is invalid")
    observed_at = _parse_datetime(observed_raw, field="runtime observation")
    if observed_raw != observed_at.isoformat():
        raise ValueError("runtime observation timestamp must use canonical UTC")
    if observed_at > current:
        raise ValueError("runtime observation timestamp is in the future")

    ttl_seconds = payload.get("ttl_seconds")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise ValueError("runtime observation TTL must be an integer")
    if not MIN_RUNTIME_TTL_SECONDS <= ttl_seconds <= MAX_RUNTIME_TTL_SECONDS:
        raise ValueError("runtime observation TTL is outside its bounded range")
    if require_fresh_observation and current > observed_at + timedelta(
        seconds=ttl_seconds
    ):
        raise ValueError("cannot record an already-expired runtime observation")

    chronicle_state = payload.get("chronicle_state")
    evidence_code = payload.get("chronicle_evidence_code")
    if chronicle_state not in CHRONICLE_STATES:
        raise ValueError("Chronicle state is invalid")
    if evidence_code not in CHRONICLE_EVIDENCE_CODES:
        raise ValueError("Chronicle evidence code is invalid")
    expected_evidence = {
        "capturing": "fresh_frame_observed",
        "idle_paused": "system_idle_reported",
        "stale": "frame_stale",
        "unavailable": "process_unavailable",
        "unknown": "not_observed",
    }[str(chronicle_state)]
    if evidence_code != expected_evidence:
        raise ValueError("Chronicle state and evidence code disagree")

    frame_at = _optional_datetime(payload.get("latest_frame_at"), field="latest Chronicle frame")
    if frame_at is not None and frame_at > observed_at:
        raise ValueError("latest Chronicle frame is newer than its observation")
    frame_is_fresh = frame_at is not None and observed_at <= frame_at + timedelta(
        seconds=MAX_FRESH_FRAME_AGE_SECONDS
    )
    if chronicle_state == "capturing" and not frame_is_fresh:
        raise ValueError("capturing requires an explicitly observed fresh Chronicle frame")
    if chronicle_state == "stale" and (frame_at is None or frame_is_fresh):
        raise ValueError("stale Chronicle state requires an old observed frame")
    if chronicle_state in {"unavailable", "unknown"} and frame_at is not None:
        raise ValueError("unavailable or unknown Chronicle state cannot claim a frame")

    browser_surface = payload.get("browser_surface")
    browser_readiness = payload.get("browser_readiness")
    if browser_surface not in BROWSER_SURFACES:
        raise ValueError("browser surface is invalid")
    if browser_readiness not in BROWSER_READINESS_STATES:
        raise ValueError("browser readiness is invalid")
    if browser_surface == "unavailable" and browser_readiness != "unavailable":
        raise ValueError("unavailable browser surface must report unavailable readiness")
    if browser_surface == "unknown" and browser_readiness != "unknown":
        raise ValueError("unknown browser surface must report unknown readiness")


def _validate_scope(*, scope_kind: str, scope_id: str) -> None:
    if scope_kind not in SCOPE_KINDS:
        raise ValueError("runtime observation scope kind is invalid")
    if not isinstance(scope_id, str) or not _SAFE_SCOPE_ID.fullmatch(scope_id):
        raise ValueError("runtime observation scope id is invalid")


def _require_runtime_root(root: Path) -> Path:
    candidate = root.expanduser()
    if candidate.is_symlink():
        raise ValueError("runtime observation root must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise FileNotFoundError("runtime observation root does not exist") from exc
    if not resolved.is_dir():
        raise NotADirectoryError("runtime observation root is not a directory")
    return resolved


def _aware_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("runtime timestamp must be timezone-aware")
    return current.astimezone(timezone.utc)


def _parse_datetime(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _optional_datetime(
    value: str | datetime | None,
    *,
    field: str,
) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware_utc(value)
    if not isinstance(value, str):
        raise ValueError(f"{field} timestamp is invalid")
    parsed = _parse_datetime(value, field=field)
    if value != parsed.isoformat():
        raise ValueError(f"{field} timestamp must use canonical UTC")
    return parsed


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    data = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")
    if len(data) > MAX_RUNTIME_OBSERVATION_BYTES:
        raise ValueError("runtime observation exceeds its bounded size")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("runtime observation target must be a regular non-symlink file")
    temp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp.exists():
            temp.unlink()

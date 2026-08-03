"""Append-only, privacy-bounded lifecycle telemetry."""

from __future__ import annotations

import fcntl
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVENT_SCHEMA_VERSION = "applypilot.run-event.v1"
_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_.:-]{1,120}$")
_SENSITIVE_FRAGMENTS = frozenset(
    {
        "authorization",
        "body",
        "cookie",
        "credential",
        "description",
        "email",
        "message",
        "otp",
        "password",
        "phone",
        "prompt",
        "recipient",
        "response",
        "secret",
        "selector",
        "subject",
        "token",
    }
)


@dataclass(frozen=True)
class RunEvent:
    """One safe lifecycle observation."""

    schema_version: str
    run_id: str
    sequence: int
    timestamp: str
    component: str
    phase: str
    status: str
    elapsed_ms: int
    source: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    detail: dict[str, str | int | float | bool | None] = field(default_factory=dict)


def _validate_name(value: str, *, field_name: str, allow_empty: bool = False) -> str:
    normalized = value.strip()
    if allow_empty and not normalized:
        return ""
    if not _SAFE_NAME.fullmatch(normalized):
        raise ValueError(f"event {field_name} is invalid")
    return normalized


def _event_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("event timestamp must include a timezone")
    return parsed


def _validate_mapping(
    value: dict[str, Any], *, counts: bool
) -> dict[str, int] | dict[str, str | int | float | bool | None]:
    if len(value) > 40:
        raise ValueError("event mapping is too large")
    result: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = _validate_name(str(key), field_name="mapping key")
        lowered = normalized_key.lower()
        if any(fragment in lowered for fragment in _SENSITIVE_FRAGMENTS):
            raise ValueError(f"event detail key is sensitive: {normalized_key}")
        if counts:
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError("event counts must be non-negative integers")
            result[normalized_key] = item
            continue
        if item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError("event detail values must be scalar")
        if isinstance(item, str) and len(item) > 240:
            raise ValueError("event detail value is too long")
        result[normalized_key] = item
    return result


class EventJournal:
    """Append-only NDJSON journal that stores lifecycle metadata, not content."""

    def __init__(self, path: Path, *, run_id: str) -> None:
        self.path = path.resolve()
        self.run_id = _validate_name(run_id, field_name="run_id")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("event journal must not be a symbolic link")
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        existing = self.read()
        self._sequence = existing[-1].sequence if existing else 0
        self._started_at = (
            _event_timestamp(existing[0].timestamp)
            if existing
            else datetime.now(timezone.utc)
        )

    def emit(
        self,
        *,
        component: str,
        phase: str,
        status: str,
        source: str = "",
        counts: dict[str, int] | None = None,
        detail: dict[str, str | int | float | bool | None] | None = None,
    ) -> RunEvent:
        safe_counts = _validate_mapping(counts or {}, counts=True)
        safe_detail = _validate_mapping(detail or {}, counts=False)
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            last_sequence = 0
            first_timestamp: datetime | None = None
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    if payload.get("run_id") != self.run_id:
                        raise ValueError("event run binding changed")
                    if first_timestamp is None:
                        first_timestamp = _event_timestamp(str(payload["timestamp"]))
                    last_sequence = int(payload["sequence"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid event journal line {line_number}") from exc
            self._sequence = last_sequence + 1
            observed_at = datetime.now(timezone.utc)
            started_at = first_timestamp or self._started_at
            event = RunEvent(
                schema_version=EVENT_SCHEMA_VERSION,
                run_id=self.run_id,
                sequence=self._sequence,
                timestamp=observed_at.isoformat(),
                component=_validate_name(component, field_name="component"),
                phase=_validate_name(phase, field_name="phase"),
                status=_validate_name(status, field_name="status"),
                source=_validate_name(source, field_name="source", allow_empty=True),
                elapsed_ms=max(0, int((observed_at - started_at).total_seconds() * 1000)),
                counts=dict(safe_counts),
                detail=dict(safe_detail),
            )
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(asdict(event), sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return event

    def read(self) -> list[RunEvent]:
        if not self.path.exists():
            return []
        events: list[RunEvent] = []
        with self.path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    event = RunEvent(**payload)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid event journal line {line_number}") from exc
                if event.schema_version != EVENT_SCHEMA_VERSION or event.run_id != self.run_id:
                    raise ValueError(f"invalid event binding on line {line_number}")
                if event.sequence != len(events) + 1:
                    raise ValueError(f"invalid event sequence on line {line_number}")
                events.append(event)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return events

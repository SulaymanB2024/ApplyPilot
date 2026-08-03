"""Explicit, local portal-link import fallback."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from applypilot.aggregation.models import (
    AggregationRequest,
    RawJob,
    SourceCapability,
    SourceKind,
)


class ManualImportSource:
    capability = SourceCapability.USER_IMPORT_ONLY
    kind = SourceKind.HANDSHAKE_MANUAL

    def __init__(self, path: Path) -> None:
        self.path = path.resolve(strict=True)
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("manual import must be a regular file")

    async def search(self, request: AggregationRequest) -> AsyncIterator[RawJob]:
        del request
        allowed = {
            "handshake_manual": SourceKind.HANDSHAKE_MANUAL,
            "runway_manual": SourceKind.RUNWAY_MANUAL,
        }
        if self.path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("manual import file is too large")
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            if line_number > 500:
                raise ValueError("manual import exceeds 500 rows")
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"manual import line {line_number} must be an object")
            source = allowed.get(str(payload.get("source") or ""))
            if source is None:
                raise ValueError(f"unsupported manual source on line {line_number}")
            for key in ("source_job_id", "title", "company", "official_url", "discovery_url"):
                if not str(payload.get(key) or "").strip():
                    raise ValueError(f"manual import line {line_number} is missing {key}")
            yield RawJob(
                source=source,
                source_job_id=str(payload["source_job_id"]),
                title=str(payload["title"]),
                company=str(payload["company"]),
                location=str(payload.get("location") or ""),
                official_url=str(payload["official_url"]),
                application_url=str(payload.get("application_url") or payload["official_url"]),
                discovery_url=str(payload["discovery_url"]),
                description=str(payload.get("description") or ""),
                observed_at=datetime.now(timezone.utc),
                salary=str(payload.get("salary") or ""),
                posted_at=str(payload.get("posted_at") or ""),
                metadata={"capture_mode": "user_copy"},
            )

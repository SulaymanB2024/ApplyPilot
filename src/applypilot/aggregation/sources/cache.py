"""Read-only adapter over the legacy local jobs cache."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote

from applypilot.aggregation.models import (
    AggregationRequest,
    RawJob,
    SourceCapability,
    SourceKind,
)


def _observed_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)


class CacheSource:
    kind = SourceKind.CACHE
    capability = SourceCapability.LOCAL_CACHE

    def __init__(self, *, db_path: Path, max_age_days: int = 14) -> None:
        self.db_path = db_path.resolve()
        if not 1 <= max_age_days <= 365:
            raise ValueError("cache max age must be between 1 and 365 days")
        self.max_age_days = max_age_days

    async def search(self, request: AggregationRequest) -> AsyncIterator[RawJob]:
        if not self.db_path.is_file():
            return
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.max_age_days)).isoformat()
        uri = f"file:{quote(str(self.db_path), safe='/')}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT rowid, * FROM jobs WHERE discovered_at >= ? "
                "ORDER BY discovered_at DESC LIMIT ?",
                (cutoff, request.limit * 3),
            ).fetchall()
        finally:
            connection.close()
        terms = tuple(term.lower() for term in request.query_terms)
        for row in rows:
            title = str(row["title"] or "")
            if terms and not any(term in title.lower() for term in terms):
                continue
            official_url = str(row["application_url"] or row["url"] or "").strip()
            discovery_url = str(row["url"] or official_url).strip()
            if not official_url or not discovery_url:
                continue
            yield RawJob(
                source=self.kind,
                source_job_id=str(row["rowid"]),
                title=title,
                company=str(row["site"] or "Unknown employer"),
                location=str(row["location"] or ""),
                official_url=official_url,
                application_url=official_url,
                discovery_url=discovery_url,
                description=str(row["full_description"] or row["description"] or ""),
                observed_at=_observed_at(str(row["discovered_at"] or "")),
                salary=str(row["salary"] or ""),
                metadata={"cache_strategy": str(row["strategy"] or "")[:80]},
            )

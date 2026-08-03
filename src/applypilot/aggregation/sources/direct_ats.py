"""Bounded concurrent adapter for employer-owned ATS boards."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import AsyncIterator

from applypilot.aggregation.models import (
    AggregationRequest,
    RawJob,
    SourceCapability,
    SourceKind,
)
from applypilot.discovery.direct_ats import _fetch_source_jobs, load_direct_ats_sources


def _matches_terms(title: str, terms: tuple[str, ...]) -> bool:
    lowered = title.lower()
    if any(term.lower() in lowered for term in terms):
        return True
    title_tokens = set(re.findall(r"[a-z0-9]+", lowered))
    for term in terms:
        meaningful = {token for token in re.findall(r"[a-z0-9]+", term.lower()) if len(token) > 3}
        if meaningful and meaningful <= title_tokens:
            return True
    return False


class DirectATSSource:
    kind = SourceKind.DIRECT_ATS
    capability = SourceCapability.AUTOMATIC_PUBLIC

    def __init__(self, *, sources: list[dict] | None = None, concurrency: int = 4) -> None:
        if not 1 <= concurrency <= 8:
            raise ValueError("direct ATS concurrency must be between 1 and 8")
        self.sources = sources
        self.concurrency = concurrency
        self.errors: list[dict[str, str]] = []

    async def search(self, request: AggregationRequest) -> AsyncIterator[RawJob]:
        sources = self.sources if self.sources is not None else load_direct_ats_sources()
        semaphore = asyncio.Semaphore(self.concurrency)

        async def fetch(source: dict) -> tuple[dict, list[dict]]:
            async with semaphore:
                try:
                    return source, await asyncio.to_thread(_fetch_source_jobs, source)
                except Exception as exc:
                    self.errors.append(
                        {
                            "source": str(source.get("name") or source.get("slug") or "unknown")[:120],
                            "error_class": type(exc).__name__,
                        }
                    )
                    return source, []

        tasks = [asyncio.create_task(fetch(source)) for source in sources]
        emitted = 0
        try:
            for task in asyncio.as_completed(tasks):
                source, jobs = await task
                company = str(source.get("name") or source.get("slug") or "Unknown employer")
                slug = str(source.get("slug") or company)
                ats = str(source.get("ats") or "unknown")
                for index, job in enumerate(jobs):
                    title = str(job.get("title") or "").strip()
                    if not title or not _matches_terms(title, request.query_terms):
                        continue
                    official_url = str(job.get("application_url") or job.get("url") or "").strip()
                    discovery_url = str(job.get("url") or official_url).strip()
                    if not official_url or not discovery_url:
                        continue
                    yield RawJob(
                        source=self.kind,
                        source_job_id=str(job.get("id") or f"{slug}-{index}"),
                        title=title,
                        company=company,
                        location=str(job.get("location") or ""),
                        official_url=official_url,
                        application_url=official_url,
                        discovery_url=discovery_url,
                        description=str(job.get("full_description") or job.get("description") or ""),
                        observed_at=datetime.now(timezone.utc),
                        salary=str(job.get("salary") or ""),
                        metadata={"ats": ats[:80]},
                    )
                    emitted += 1
                    if emitted >= request.limit:
                        return
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

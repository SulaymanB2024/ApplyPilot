"""Bounded adapter for employer-owned careers pages using SmartExtract."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import AsyncIterator
from urllib.parse import urljoin

from applypilot.aggregation.models import (
    AggregationRequest,
    RawJob,
    SourceCapability,
    SourceKind,
)
from applypilot.discovery.smartextract import _run_one_site, load_sites


class SmartExtractSource:
    """Own only explicitly configured employer-careers targets."""

    kind = SourceKind.SMART_EXTRACT
    capability = SourceCapability.AUTOMATIC_PUBLIC

    def __init__(
        self,
        *,
        sites: list[dict] | None = None,
        max_targets: int = 6,
        concurrency: int = 2,
    ) -> None:
        if not 1 <= max_targets <= 12:
            raise ValueError("SmartExtract target cap must be between 1 and 12")
        if not 1 <= concurrency <= 4:
            raise ValueError("SmartExtract concurrency must be between 1 and 4")
        self.sites = sites
        self.max_targets = max_targets
        self.concurrency = concurrency
        self.errors: list[str] = []

    async def search(self, request: AggregationRequest) -> AsyncIterator[RawJob]:
        configured = self.sites if self.sites is not None else load_sites()
        targets = [
            site
            for site in configured
            if site.get("direct_source") is True
            and str(site.get("source_kind") or "").lower() == "employer_careers"
        ][: self.max_targets]
        semaphore = asyncio.Semaphore(self.concurrency)

        async def fetch(site: dict) -> tuple[dict, dict, str]:
            async with semaphore:
                try:
                    result = await asyncio.to_thread(
                        _run_one_site, str(site["name"]), str(site["url"])
                    )
                    return site, result, ""
                except Exception as exc:
                    return site, {}, type(exc).__name__

        tasks = [asyncio.create_task(fetch(site)) for site in targets]
        emitted = 0
        terms = tuple(term.lower() for term in request.query_terms)
        try:
            for task in asyncio.as_completed(tasks):
                site, result, error_class = await task
                if error_class:
                    self.errors.append(error_class)
                    continue
                site_url = str(site["url"])
                for index, job in enumerate(result.get("jobs") or []):
                    title = str(job.get("title") or "").strip()
                    if not title or (terms and not any(term in title.lower() for term in terms)):
                        continue
                    raw_url = str(job.get("application_url") or job.get("url") or "").strip()
                    official_url = urljoin(site_url, raw_url)
                    if not raw_url or not official_url.startswith(("https://", "http://")):
                        continue
                    source_id = str(
                        job.get("id")
                        or hashlib.sha256(official_url.encode("utf-8")).hexdigest()[:20]
                    )
                    yield RawJob(
                        source=self.kind,
                        source_job_id=source_id or f"{site['name']}-{index}",
                        title=title,
                        company=str(job.get("company") or site["name"]),
                        location=str(job.get("location") or ""),
                        official_url=official_url,
                        application_url=official_url,
                        discovery_url=site_url,
                        description=str(job.get("description") or ""),
                        observed_at=datetime.now(timezone.utc),
                        salary=str(job.get("salary") or ""),
                        posted_at=str(job.get("posted_at") or job.get("posted") or ""),
                        metadata={"strategy": str(result.get("strategy") or "")[:80]},
                    )
                    emitted += 1
                    if emitted >= request.limit:
                        return
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


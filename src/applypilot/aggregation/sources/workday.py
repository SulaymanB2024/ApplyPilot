"""Bounded, read-only Workday discovery adapter."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from typing import AsyncIterator
from urllib.parse import urljoin

from applypilot.aggregation.models import (
    AggregationRequest,
    RawJob,
    SourceCapability,
    SourceKind,
)
from applypilot.discovery.workday import load_employers, search_employer

WorkdaySearch = Callable[[str, dict, str], list[dict]]


def _public_url(employer: dict, job: dict) -> str:
    direct = str(
        job.get("external_url")
        or job.get("apply_url")
        or job.get("application_url")
        or job.get("url")
        or ""
    ).strip()
    if direct:
        return direct
    external_path = str(job.get("external_path") or "").strip()
    base_url = str(employer.get("base_url") or "").strip()
    if not external_path or not base_url:
        return ""
    if external_path.startswith(("https://", "http://")):
        return external_path
    site_id = str(employer.get("site_id") or "").strip("/")
    public_base = f"{base_url.rstrip('/')}/{site_id}/" if site_id else f"{base_url.rstrip('/')}/"
    return urljoin(public_base, external_path.lstrip("/"))


class WorkdaySource:
    """Search a bounded employer/term matrix without writing the legacy jobs DB."""

    kind = SourceKind.WORKDAY
    capability = SourceCapability.AUTOMATIC_PUBLIC

    def __init__(
        self,
        *,
        employers: dict[str, dict] | None = None,
        search_one: WorkdaySearch | None = None,
        max_employers: int = 12,
        max_query_terms: int = 3,
        concurrency: int = 6,
        max_results_per_unit: int = 20,
    ) -> None:
        if not 1 <= max_employers <= 24:
            raise ValueError("Workday employer cap must be between 1 and 24")
        if not 1 <= max_query_terms <= 4:
            raise ValueError("Workday query-term cap must be between 1 and 4")
        if not 1 <= concurrency <= 8:
            raise ValueError("Workday concurrency must be between 1 and 8")
        if not 1 <= max_results_per_unit <= 50:
            raise ValueError("Workday per-unit result cap must be between 1 and 50")
        self.employers = employers
        self.search_one = search_one
        self.max_employers = max_employers
        self.max_query_terms = max_query_terms
        self.concurrency = concurrency
        self.max_results_per_unit = max_results_per_unit
        self.errors: list[str] = []

    async def search(self, request: AggregationRequest) -> AsyncIterator[RawJob]:
        employers = self.employers if self.employers is not None else load_employers()
        bounded_employers = list(employers.items())[: self.max_employers]
        bounded_terms = request.query_terms[: self.max_query_terms]
        semaphore = asyncio.Semaphore(self.concurrency)

        async def fetch(
            employer_key: str, employer: dict, term: str
        ) -> tuple[str, dict, list[dict], str]:
            async with semaphore:
                try:
                    if self.search_one is not None:
                        jobs = await asyncio.to_thread(
                            self.search_one, employer_key, employer, term
                        )
                    else:
                        jobs = await asyncio.to_thread(
                            search_employer,
                            employer_key,
                            employer,
                            term,
                            location_filter=bool(request.locations),
                            title_filter=False,
                            max_results=self.max_results_per_unit,
                            accept_locs=list(request.locations),
                            reject_locs=[],
                        )
                    return employer_key, employer, list(jobs)[: self.max_results_per_unit], ""
                except Exception as exc:
                    return employer_key, employer, [], type(exc).__name__

        tasks = [
            asyncio.create_task(fetch(key, employer, term))
            for key, employer in bounded_employers
            for term in bounded_terms
        ]
        emitted = 0
        try:
            for task in asyncio.as_completed(tasks):
                employer_key, employer, jobs, error_class = await task
                if error_class:
                    self.errors.append(error_class)
                    continue
                for index, job in enumerate(jobs):
                    official_url = _public_url(employer, job)
                    title = str(job.get("title") or "").strip()
                    if not official_url or not title:
                        continue
                    source_id = str(
                        job.get("id")
                        or job.get("job_req_id")
                        or job.get("external_path")
                        or hashlib.sha256(official_url.encode("utf-8")).hexdigest()[:20]
                    )
                    yield RawJob(
                        source=self.kind,
                        source_job_id=source_id or f"{employer_key}-{index}",
                        title=title,
                        company=str(
                            job.get("employer_name")
                            or employer.get("name")
                            or employer_key
                        ),
                        location=str(job.get("location") or ""),
                        official_url=official_url,
                        application_url=official_url,
                        discovery_url=official_url,
                        description=str(
                            job.get("full_description") or job.get("description") or ""
                        ),
                        observed_at=datetime.now(timezone.utc),
                        salary=str(job.get("salary") or ""),
                        posted_at=str(job.get("posted_at") or job.get("posted") or ""),
                        metadata={"employer_key": employer_key[:120]},
                    )
                    emitted += 1
                    if emitted >= request.limit:
                        return
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


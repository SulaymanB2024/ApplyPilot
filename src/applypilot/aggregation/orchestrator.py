"""Concurrent progressive aggregation coordinator."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from applypilot.aggregation.models import AggregationRequest, SourceAdapter, SourceKind
from applypilot.aggregation.normalization import normalize_job
from applypilot.aggregation.store import AggregationStore
from applypilot.observability.events import EventJournal


def _adapter_error_class(source: SourceAdapter) -> str:
    errors = getattr(source, "errors", None)
    if not errors:
        return ""
    first = errors[0]
    if isinstance(first, str):
        return first[:120]
    if isinstance(first, dict):
        return str(first.get("error_class") or "AdapterPartial")[:120]
    return type(first).__name__[:120]


class Aggregator:
    """Run source families concurrently and publish immutable fast snapshots."""

    def __init__(
        self,
        *,
        store: AggregationStore,
        journal: EventJournal,
        sources: Sequence[SourceAdapter],
        pending_enrichment: tuple[str, ...] = (),
        heartbeat_interval_seconds: float = 1.0,
    ) -> None:
        if not sources:
            raise ValueError("at least one aggregation source is required")
        kinds = [source.kind for source in sources]
        if any(not isinstance(kind, SourceKind) for kind in kinds):
            raise TypeError("aggregation source kind is invalid")
        if len(set(kinds)) != len(kinds):
            raise ValueError("aggregation source kinds must be unique")
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self.store = store
        self.journal = journal
        self.sources = tuple(sources)
        self.pending_enrichment = tuple(sorted(set(pending_enrichment)))
        self.heartbeat_interval_seconds = heartbeat_interval_seconds

    async def _consume(
        self,
        run_id: str,
        request: AggregationRequest,
        source: SourceAdapter,
    ) -> str:
        self.journal.emit(
            component="aggregation",
            phase="source",
            status="started",
            source=source.kind.value,
        )
        observed = 0
        try:
            async with asyncio.timeout(request.per_source_timeout_seconds):
                async for raw in source.search(request):
                    inserted = self.store.record_observation(run_id, normalize_job(raw))
                    observed += int(inserted)
                    snapshot = self.store.snapshot(run_id)
                    self.journal.emit(
                        component="aggregation",
                        phase="candidate",
                        status="observed" if inserted else "duplicate",
                        source=source.kind.value,
                        counts={
                            "source_observed": observed,
                            "unique": snapshot["candidate_count"],
                            "observations": snapshot["observation_count"],
                        },
                    )
            error_class = _adapter_error_class(source)
            status = "partial" if error_class else "complete"
        except TimeoutError:
            status, error_class = "timed_out", "TimeoutError"
        except Exception as exc:
            status, error_class = "failed", type(exc).__name__
        self.store.finish_source(
            run_id,
            source.kind,
            status=status,
            error_class=error_class,
        )
        self.journal.emit(
            component="aggregation",
            phase="source",
            status=status,
            source=source.kind.value,
            counts={"observed": observed},
            detail={"error_class": error_class} if error_class else {},
        )
        return status

    async def _heartbeat(self, run_id: str) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval_seconds)
            snapshot = self.store.snapshot(run_id)
            self.journal.emit(
                component="aggregation",
                phase="run",
                status="heartbeat",
                counts={
                    "candidates": snapshot["candidate_count"],
                    "observations": snapshot["observation_count"],
                },
            )

    async def run(self, run_id: str, request: AggregationRequest) -> dict[str, Any]:
        request.validate()
        self.store.start_run(run_id, request)
        self.journal.emit(
            component="aggregation",
            phase="run",
            status="started",
            counts={"sources": len(self.sources)},
            detail={"mode": request.mode},
        )
        source_semaphore = asyncio.Semaphore(request.max_concurrency)

        for source in self.sources:
            self.store.start_source(run_id, source.kind)

        async def bounded(source: SourceAdapter) -> str:
            async with source_semaphore:
                return await self._consume(run_id, request, source)

        source_tasks = [asyncio.create_task(bounded(source)) for source in self.sources]
        heartbeat_task = asyncio.create_task(self._heartbeat(run_id))
        try:
            done, pending = await asyncio.wait(
                source_tasks,
                timeout=request.global_deadline_seconds,
                return_when=asyncio.ALL_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                current = self.store.snapshot(run_id)["sources"]
                running = {row["source"] for row in current if row["status"] == "running"}
                for source in self.sources:
                    if source.kind.value in running:
                        self.store.finish_source(
                            run_id,
                            source.kind,
                            status="cancelled",
                            error_class="GlobalDeadline",
                        )
                        self.journal.emit(
                            component="aggregation",
                            phase="source",
                            status="cancelled",
                            source=source.kind.value,
                            detail={"error_class": "GlobalDeadline"},
                        )
            await asyncio.gather(*done, return_exceptions=True)
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

        source_rows = self.store.snapshot(run_id)["sources"]
        statuses = [str(row["status"]) for row in source_rows]
        terminal = (
            "complete"
            if statuses
            and all(status == "complete" for status in statuses)
            and not self.pending_enrichment
            else "partial"
        )
        self.store.complete_run(run_id, status=terminal)
        published = self.store.publish_snapshot(
            run_id,
            reason="fast_lane",
            status=terminal,
            pending_enrichment=self.pending_enrichment,
        )
        self.journal.emit(
            component="aggregation",
            phase="snapshot",
            status="published",
            counts={
                "revision": int(published["revision"]),
                "candidates": int(published["candidate_count"]),
                "observations": int(published["observation_count"]),
            },
            detail={"run_status": terminal},
        )
        self.journal.emit(
            component="aggregation",
            phase="run",
            status=terminal,
            counts={
                "candidates": int(published["candidate_count"]),
                "observations": int(published["observation_count"]),
            },
        )
        return published

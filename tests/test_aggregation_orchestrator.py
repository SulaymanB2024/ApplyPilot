from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from applypilot.aggregation.models import (
    AggregationRequest,
    RawJob,
    SourceCapability,
    SourceKind,
)
from applypilot.aggregation.orchestrator import Aggregator
from applypilot.aggregation.store import AggregationStore
from applypilot.observability.events import EventJournal


class FixtureSource:
    capability = SourceCapability.AUTOMATIC_PUBLIC

    def __init__(
        self,
        kind: SourceKind,
        *,
        delay: float,
        url: str = "https://jobs.example.com/123",
        fail: bool = False,
    ) -> None:
        self.kind = kind
        self.delay = delay
        self.url = url
        self.fail = fail

    async def search(self, request):
        del request
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("fixture provider failure")
        yield RawJob(
            source=self.kind,
            source_job_id=self.kind.value,
            title="Product Intern",
            company="Example Labs",
            location="Austin, TX",
            official_url=self.url,
            discovery_url=self.url,
            description=f"Fixture from {self.kind.value}",
            observed_at=datetime.now(timezone.utc),
        )


def _run(tmp_path, sources, **request_overrides):
    store = AggregationStore(tmp_path / "aggregation.sqlite3", run_dir=tmp_path / "runs")
    journal = EventJournal(tmp_path / "events.ndjson", run_id="agg-1")
    request = AggregationRequest(
        query="product internships",
        query_terms=("product intern",),
        **request_overrides,
    )
    snapshot = asyncio.run(
        Aggregator(
            store=store,
            journal=journal,
            sources=sources,
            heartbeat_interval_seconds=0.01,
        ).run("agg-1", request)
    )
    return store, journal, snapshot


def test_orchestrator_is_progressive_and_deduplicates(tmp_path):
    store, journal, snapshot = _run(
        tmp_path,
        [
            FixtureSource(SourceKind.CACHE, delay=0.01),
            FixtureSource(SourceKind.DIRECT_ATS, delay=0.10),
        ],
    )
    assert snapshot["revision"] == 1
    assert snapshot["status"] == "complete"
    assert snapshot["candidate_count"] == 1
    assert snapshot["observation_count"] == 2
    events = journal.read()
    first_observed = next(
        index
        for index, event in enumerate(events)
        if event.phase == "candidate" and event.status == "observed"
    )
    slow_complete = next(
        index
        for index, event in enumerate(events)
        if event.source == "direct_ats" and event.status == "complete"
    )
    assert first_observed < slow_complete
    assert any(event.status == "heartbeat" for event in events)
    assert store.latest_revision("agg-1") == 1


def test_orchestrator_isolates_failed_source_and_publishes_partial(tmp_path):
    _, journal, snapshot = _run(
        tmp_path,
        [
            FixtureSource(SourceKind.CACHE, delay=0.01),
            FixtureSource(SourceKind.DIRECT_ATS, delay=0.02, fail=True),
        ],
    )
    assert snapshot["status"] == "partial"
    assert snapshot["candidate_count"] == 1
    failed = next(event for event in journal.read() if event.status == "failed")
    assert failed.detail == {"error_class": "RuntimeError"}


def test_orchestrator_cancels_only_unfinished_sources_at_global_deadline(tmp_path):
    _, _, snapshot = _run(
        tmp_path,
        [
            FixtureSource(SourceKind.CACHE, delay=0.01),
            FixtureSource(SourceKind.WORKDAY, delay=1.0),
        ],
        global_deadline_seconds=0.05,
        per_source_timeout_seconds=2.0,
    )
    assert snapshot["status"] == "partial"
    states = {row["source"]: row for row in snapshot["sources"]}
    assert states["cache"]["status"] == "complete"
    assert states["workday"]["status"] == "cancelled"
    assert states["workday"]["error_class"] == "GlobalDeadline"


def test_orchestrator_enforces_per_source_timeout(tmp_path):
    _, _, snapshot = _run(
        tmp_path,
        [FixtureSource(SourceKind.CACHE, delay=0.10)],
        global_deadline_seconds=1.0,
        per_source_timeout_seconds=0.02,
    )
    assert snapshot["status"] == "partial"
    assert snapshot["sources"][0]["status"] == "timed_out"
    assert snapshot["sources"][0]["error_class"] == "TimeoutError"


def test_orchestrator_records_adapter_internal_errors_as_partial(tmp_path):
    source = FixtureSource(SourceKind.DIRECT_ATS, delay=0.01)
    source.errors = [{"source": "bad", "error_class": "RuntimeError"}]
    _, _, snapshot = _run(tmp_path, [source])
    assert snapshot["status"] == "partial"
    assert snapshot["sources"][0]["status"] == "partial"
    assert snapshot["sources"][0]["error_class"] == "RuntimeError"

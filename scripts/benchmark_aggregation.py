#!/usr/bin/env python3
"""Deterministic local latency gate for progressive aggregation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

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

    def __init__(self, kind: SourceKind, delay: float) -> None:
        self.kind = kind
        self.delay = delay

    async def search(self, request: AggregationRequest):
        del request
        await asyncio.sleep(self.delay)
        for index in range(20):
            yield RawJob(
                source=self.kind,
                source_job_id=f"{self.kind.value}-{index}",
                title="Product Intern",
                company=f"Example {index}",
                location="Austin, TX",
                official_url=f"https://jobs.example.com/{index}",
                discovery_url=f"https://jobs.example.com/{index}",
                description=f"Fixture from {self.kind.value}",
                observed_at=datetime.now(timezone.utc),
            )


def _write_private_json(path: Path, payload: dict) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


async def benchmark(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = EventJournal(output / "events.ndjson", run_id="benchmark")
    request = AggregationRequest(
        query="product internships",
        query_terms=("product intern",),
        global_deadline_seconds=15,
        per_source_timeout_seconds=10,
    )
    store = AggregationStore(
        output / "aggregation.sqlite3", run_dir=output / "aggregation-runs"
    )
    started = time.monotonic()
    try:
        snapshot = await Aggregator(
            store=store,
            journal=journal,
            sources=[
                FixtureSource(SourceKind.CACHE, 0.05),
                FixtureSource(SourceKind.DIRECT_ATS, 0.25),
                FixtureSource(SourceKind.WORKDAY, 0.50),
            ],
            pending_enrichment=("jobspy", "handshake", "runway"),
        ).run("benchmark", request)
    finally:
        store.close()
    elapsed_ms = int((time.monotonic() - started) * 1000)
    events = journal.read()
    first_candidate_ms = next(
        event.elapsed_ms
        for event in events
        if event.phase == "candidate" and event.status == "observed"
    )
    revision_1_ms = next(
        event.elapsed_ms
        for event in events
        if event.phase == "snapshot"
        and event.status == "published"
        and event.counts.get("revision") == 1
    )
    report = {
        "elapsed_ms": elapsed_ms,
        "first_candidate_ms": first_candidate_ms,
        "revision_1_ms": revision_1_ms,
        "candidate_count": snapshot["candidate_count"],
        "observation_count": snapshot["observation_count"],
        "event_count": len(events),
        "snapshot_revision": snapshot["revision"],
        "pending_enrichment": snapshot["pending_enrichment"],
    }
    report["pass"] = (
        first_candidate_ms < 1000
        and revision_1_ms < 3000
        and elapsed_ms < 3000
        and report["candidate_count"] == 20
        and report["observation_count"] == 60
        and report["snapshot_revision"] == 1
        and report["pending_enrichment"] == ["handshake", "jobspy", "runway"]
    )
    _write_private_json(output / "benchmark.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output is None:
        with tempfile.TemporaryDirectory(
            prefix="applypilot-aggregation-benchmark-"
        ) as directory:
            report = asyncio.run(benchmark(Path(directory)))
    else:
        report = asyncio.run(benchmark(args.output.expanduser().resolve()))
    print(json.dumps(report, sort_keys=True))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from applypilot.aggregation.models import AggregationRequest, RawJob, SourceKind
from applypilot.aggregation.normalization import normalize_job
from applypilot.aggregation.store import AggregationStore, snapshot_digest


def _observation(source: SourceKind):
    return normalize_job(
        RawJob(
            source=source,
            source_job_id=source.value,
            title="Product Intern",
            company="Example Labs",
            location="Austin, TX",
            official_url="https://jobs.example.com/123",
            discovery_url="https://jobs.example.com/123",
            description="First-party description" if source is SourceKind.DIRECT_ATS else "Cached",
            observed_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
        )
    )


def test_store_projects_observations_and_publishes_revisions(tmp_path):
    store = AggregationStore(tmp_path / "aggregation.sqlite3", run_dir=tmp_path / "runs")
    request = AggregationRequest(query="product internships", query_terms=("product intern",))
    store.start_run("agg-1", request)
    for source in (SourceKind.CACHE, SourceKind.DIRECT_ATS):
        store.start_source("agg-1", source)
        assert store.record_observation("agg-1", _observation(source))
        store.finish_source("agg-1", source, status="complete")

    snapshot = store.complete_run("agg-1")
    assert snapshot["candidate_count"] == 1
    assert snapshot["observation_count"] == 2
    assert snapshot["duplicate_count"] == 1
    assert snapshot["jobs"][0]["source_count"] == 2
    assert snapshot["jobs"][0]["description"] == "First-party description"

    first = store.publish_snapshot("agg-1", reason="fast_lane", status="partial")
    second = store.publish_snapshot("agg-1", reason="enrichment", status="complete")
    assert first["revision"] == 1
    assert second["revision"] == 2
    assert second["parent_sha256"] == first["sha256"]
    assert snapshot_digest(second) == second["sha256"]
    path, restored = store.get_snapshot("agg-1", 2)
    assert path.is_file()
    assert restored == second


def test_store_tracks_distinct_source_units(tmp_path):
    store = AggregationStore(tmp_path / "aggregation.sqlite3")
    store.start_run("agg-1", AggregationRequest(query="internships", query_terms=("intern",)))
    source_id = "jobspy:indeed:abc123"
    store.start_source("agg-1", SourceKind.JOBSPY, source_id=source_id)
    board_only = normalize_job(
        RawJob(
            source=SourceKind.JOBSPY,
            source_job_id="indeed-1",
            title="Product Intern",
            company="Example Labs",
            location="Austin, TX",
            official_url="",
            discovery_url="https://www.indeed.com/viewjob?jk=1",
            description="",
            observed_at=datetime.now(timezone.utc),
        )
    )
    store.record_observation("agg-1", board_only, source_id=source_id)
    store.finish_source("agg-1", SourceKind.JOBSPY, source_id=source_id, status="complete")
    [source] = store.snapshot("agg-1")["sources"]
    assert source["source_id"] == source_id
    assert source["observed_count"] == 1


def test_store_rejects_unsafe_source_identifier(tmp_path):
    store = AggregationStore(tmp_path / "aggregation.sqlite3")
    store.start_run("agg-1", AggregationRequest(query="internships", query_terms=("intern",)))
    with pytest.raises(ValueError, match="source id"):
        store.start_source("agg-1", SourceKind.JOBSPY, source_id="jobspy:raw query with spaces")

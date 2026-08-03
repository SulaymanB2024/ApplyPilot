from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from applypilot.aggregation.models import AggregationRequest, RawJob, SourceKind
from applypilot.aggregation.normalization import normalize_job
from applypilot.aggregation.portal_handoff import (
    PortalMissionRequest,
    consume_portal_response,
    initialize_portal_queue,
)
from applypilot.aggregation.store import AggregationStore
from applypilot.autonomy.handoff import import_response_artifact
from applypilot.observability.events import EventJournal


def _setup(tmp_path):
    request = AggregationRequest(
        query="product internships",
        query_terms=("product intern",),
        locations=("Austin, TX",),
    )
    runs = tmp_path / "runs"
    run_dir = runs / "agg-1"
    run_dir.mkdir(parents=True)
    store = AggregationStore(tmp_path / "aggregation.sqlite3", run_dir=runs)
    store.start_run("agg-1", request)
    store.start_source("agg-1", SourceKind.CACHE)
    store.record_observation(
        "agg-1",
        normalize_job(
            RawJob(
                source=SourceKind.CACHE,
                source_job_id="cache-1",
                title="Product Intern",
                company="Example Labs",
                location="Austin, TX",
                official_url="https://jobs.example.com/123",
                discovery_url="https://jobs.example.com/123",
                description="Cached",
                observed_at=datetime.now(timezone.utc),
            )
        ),
    )
    store.finish_source("agg-1", SourceKind.CACHE, status="complete")
    store.complete_run("agg-1", status="partial")
    first = store.publish_snapshot(
        "agg-1",
        reason="fast_lane",
        status="partial",
        pending_enrichment=("handshake", "runway"),
    )
    journal = EventJournal(run_dir / "events.ndjson", run_id="agg-1")
    first_request = initialize_portal_queue(
        store=store,
        journal=journal,
        run_dir=run_dir,
        run_id="agg-1",
        aggregation_request=request,
        portals=("handshake", "runway"),
    )
    return request, store, journal, run_dir, first, first_request


def _portal_response(request_path, *, official_url=""):
    envelope = json.loads(request_path.read_text(encoding="utf-8"))
    request = PortalMissionRequest.from_dict(envelope["mission"])
    discovery_url = (
        "https://app.joinhandshake.com/stu/jobs/987"
        if request.portal.value == "handshake"
        else "https://app.joinrunway.io/jobs/654"
    )
    return {
        "schema_version": "applypilot.portal-mission-response.v1",
        "run_id": request.run_id,
        "request_id": request.request_id,
        "request_sha256": request.sha256,
        "query_digest": request.query_digest,
        "portal": request.portal.value,
        "status": "complete",
        "navigation_count": 3,
        "elapsed_seconds": 12,
        "safe_hostname": request.permitted_hosts[0],
        "observations": [
            {
                "source_job_id": "987" if request.portal.value == "handshake" else "654",
                "title": "Product Intern",
                "company": "Example Labs",
                "location": "Austin, TX",
                "discovery_url": discovery_url,
                "application_url": official_url or discovery_url,
                "official_url": official_url,
                "description": "Visible portal evidence",
            }
        ],
    }


def _import_and_consume(store, journal, run_dir, request_path, payload):
    input_path = run_dir / f"{payload['portal']}-input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    import_response_artifact(request_path=request_path, input_path=input_path)
    return consume_portal_response(
        store=store,
        journal=journal,
        run_dir=run_dir,
        run_id="agg-1",
        request_path=request_path,
    )


def test_portal_responses_publish_monotonic_immutable_revisions(tmp_path):
    _, store, journal, run_dir, first, handshake_request = _setup(tmp_path)
    first_path, _ = store.get_snapshot("agg-1", 1)
    first_bytes = first_path.read_bytes()
    missions = store.portal_missions("agg-1")
    assert [row["status"] for row in missions] == ["awaiting_response", "queued"]

    second = _import_and_consume(
        store,
        journal,
        run_dir,
        handshake_request,
        _portal_response(handshake_request),
    )
    assert second["revision"] == 2
    assert second["parent_sha256"] == first["sha256"]
    assert first_path.read_bytes() == first_bytes
    missions = store.portal_missions("agg-1")
    assert [row["status"] for row in missions] == ["complete", "awaiting_response"]

    runway_request = Path(missions[1]["request_path"])
    third = _import_and_consume(
        store,
        journal,
        run_dir,
        runway_request,
        _portal_response(runway_request, official_url="https://jobs.example.com/456"),
    )
    assert third["revision"] == 3
    assert third["parent_sha256"] == second["sha256"]
    assert third["pending_enrichment"] == []
    assert store.latest_revision("agg-1") == 3


def test_reimporting_same_portal_response_is_idempotent(tmp_path):
    _, store, journal, run_dir, _, handshake_request = _setup(tmp_path)
    payload = _portal_response(handshake_request)
    second = _import_and_consume(
        store, journal, run_dir, handshake_request, payload
    )
    imported = consume_portal_response(
        store=store,
        journal=journal,
        run_dir=run_dir,
        run_id="agg-1",
        request_path=handshake_request,
    )
    assert imported["sha256"] == second["sha256"]
    assert store.latest_revision("agg-1") == 2


def test_exact_portal_identity_upgrade_aliases_without_fuzzy_merge(tmp_path):
    request = AggregationRequest(query="internships", query_terms=("intern",))
    store = AggregationStore(tmp_path / "aggregation.sqlite3")
    store.start_run("agg-1", request)
    store.start_source("agg-1", SourceKind.HANDSHAKE_BROWSER)
    portal_only = normalize_job(
        RawJob(
            source=SourceKind.HANDSHAKE_BROWSER,
            source_job_id="987",
            title="Product Intern",
            company="Example Labs",
            location="Austin, TX",
            official_url="",
            discovery_url="https://app.joinhandshake.com/stu/jobs/987",
            description="Portal",
            observed_at=datetime.now(timezone.utc),
        )
    )
    resolved = normalize_job(
        RawJob(
            source=SourceKind.HANDSHAKE_BROWSER,
            source_job_id="987",
            title="Product Intern",
            company="Example Labs",
            location="Austin, TX",
            official_url="https://jobs.example.com/123",
            discovery_url="https://app.joinhandshake.com/stu/jobs/987",
            description="Official",
            observed_at=datetime.now(timezone.utc),
        )
    )
    store.record_observation("agg-1", portal_only)
    store.record_observation("agg-1", resolved)
    snapshot = store.snapshot("agg-1")
    assert snapshot["candidate_count"] == 1
    assert snapshot["jobs"][0]["advanceable"] is True
    assert snapshot["jobs"][0]["source_count"] == 1

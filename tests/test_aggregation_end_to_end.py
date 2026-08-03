from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from applypilot.aggregation.models import (
    AggregationRequest,
    RawJob,
    SourceCapability,
    SourceKind,
)
from applypilot.aggregation.normalization import normalize_job
from applypilot.aggregation.orchestrator import Aggregator
from applypilot.aggregation.portal_handoff import (
    PortalMissionRequest,
    consume_portal_response,
    initialize_portal_queue,
    write_portal_checkpoint,
)
from applypilot.aggregation.snapshot import SnapshotDiscovery
from applypilot.aggregation.store import AggregationStore
from applypilot.autonomy.handoff import import_response_artifact
from applypilot.cli import app
from applypilot.observability.events import EventJournal
from applypilot.opportunities.models import (
    OpportunityDecision,
    OpportunityEvidence,
    OpportunityLead,
    OpportunityRoute,
    OpportunitySignal,
    OpportunityStatus,
)
from applypilot.opportunities.research import opportunity_lead_id
from applypilot.opportunities.store import OpportunityStore


class FixtureSource:
    capability = SourceCapability.AUTOMATIC_PUBLIC

    def __init__(self, kind: SourceKind, delay: float) -> None:
        self.kind = kind
        self.delay = delay

    async def search(self, request):
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
                posted_at="2026-08-01",
            )


def _portal_payload(request_path: Path, *, official_url: str = "") -> dict:
    request = PortalMissionRequest.from_dict(
        json.loads(request_path.read_text(encoding="utf-8"))["mission"]
    )
    discovery_url = (
        "https://app.joinhandshake.com/stu/jobs/fixture"
        if request.portal.value == "handshake"
        else "https://app.joinrunway.io/jobs/fixture"
    )
    return {
        "schema_version": "applypilot.portal-mission-response.v1",
        "run_id": request.run_id,
        "request_id": request.request_id,
        "request_sha256": request.sha256,
        "query_digest": request.query_digest,
        "portal": request.portal.value,
        "status": "complete",
        "navigation_count": 2,
        "elapsed_seconds": 4,
        "safe_hostname": request.permitted_hosts[0],
        "observations": [
            {
                "source_job_id": f"{request.portal.value}-fixture",
                "title": "Product Operations Intern",
                "company": "Portal Example",
                "location": "Austin, TX",
                "discovery_url": discovery_url,
                "application_url": official_url or discovery_url,
                "official_url": official_url,
                "description": "Bounded visible portal fixture.",
            }
        ],
    }


def _import_portal(
    *,
    store: AggregationStore,
    journal: EventJournal,
    run_dir: Path,
    request_path: Path,
    official_url: str = "",
) -> dict:
    payload = _portal_payload(request_path, official_url=official_url)
    raw_path = run_dir / f"{payload['portal']}-fixture-response.json"
    raw_path.write_text(json.dumps(payload), encoding="utf-8")
    import_response_artifact(request_path=request_path, input_path=raw_path)
    return consume_portal_response(
        store=store,
        journal=journal,
        run_dir=run_dir,
        run_id="agg-1",
        request_path=request_path,
    )


def test_progressive_multi_lane_revision_and_operator_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    run_root = tmp_path / "aggregation-runs"
    run_dir = run_root / "agg-1"
    run_dir.mkdir(parents=True)
    store = AggregationStore(tmp_path / "aggregation.sqlite3", run_dir=run_root)
    journal = EventJournal(run_dir / "events.ndjson", run_id="agg-1")
    request = AggregationRequest(
        query="product internships",
        query_terms=("product intern",),
        locations=("Austin, TX",),
        global_deadline_seconds=3,
        per_source_timeout_seconds=2,
    )
    started = time.monotonic()
    first = asyncio.run(
        Aggregator(
            store=store,
            journal=journal,
            sources=(
                FixtureSource(SourceKind.CACHE, 0.05),
                FixtureSource(SourceKind.DIRECT_ATS, 0.25),
                FixtureSource(SourceKind.WORKDAY, 0.50),
            ),
            pending_enrichment=("jobspy", "handshake", "runway"),
        ).run("agg-1", request)
    )
    revision_one_elapsed = time.monotonic() - started
    first_candidate_ms = next(
        event.elapsed_ms
        for event in journal.read()
        if event.phase == "candidate" and event.status == "observed"
    )
    assert first_candidate_ms < 1000
    assert revision_one_elapsed < 3
    assert first["revision"] == 1
    assert first["candidate_count"] == 20
    assert first["observation_count"] == 60

    store.start_source("agg-1", SourceKind.JOBSPY, source_id="jobspy:indeed:fixture")
    store.record_observation(
        "agg-1",
        normalize_job(
            RawJob(
                source=SourceKind.JOBSPY,
                source_job_id="board-only",
                title="Product Intern",
                company="Board Only",
                location="Austin, TX",
                official_url="",
                discovery_url="https://www.indeed.com/viewjob?jk=fixture",
                description="Board discovery fixture.",
                observed_at=datetime.now(timezone.utc),
            )
        ),
        source_id="jobspy:indeed:fixture",
    )
    store.finish_source(
        "agg-1",
        SourceKind.JOBSPY,
        source_id="jobspy:indeed:fixture",
        status="complete",
    )
    second = store.publish_snapshot(
        "agg-1",
        reason="jobspy_enrichment",
        status="partial",
        pending_enrichment=("handshake", "runway"),
    )
    assert second["parent_sha256"] == first["sha256"]
    assert second["advanceable_count"] == 20
    assert second["candidate_count"] == 21

    handshake_request = initialize_portal_queue(
        store=store,
        journal=journal,
        run_dir=run_dir,
        run_id="agg-1",
        aggregation_request=request,
        portals=("handshake", "runway"),
    )
    assert handshake_request is not None
    write_portal_checkpoint(
        request_path=handshake_request,
        state="page_observed",
        sequence=1,
        navigation_count=1,
        result_count=0,
        elapsed_seconds=2,
        safe_hostname="app.joinhandshake.com",
    )
    status_result = CliRunner().invoke(
        app,
        ["aggregate-status", "--run-id", "agg-1", "--watch", "--json"],
    )
    assert status_result.exit_code == 0, status_result.output
    status = json.loads(status_result.stdout)
    assert status["fast_snapshot"]["revision"] == 1
    assert status["fast_snapshot"]["elapsed_ms"] < 3000
    assert status["source_states"]["cache"] == "complete"
    assert status["source_states"]["jobspy"] == "complete"
    assert status["browser_queue"]["active"] == "handshake"
    assert status["browser_queue"]["queued"] == ["runway"]
    assert status["browser_queue"]["state"] == "page_observed"
    assert status["browser_queue"]["last_checkpoint_age_seconds"] >= 0
    assert status["opportunities"] == {
        "observed": 0,
        "verified": 0,
        "draft_ready": 0,
        "sent": 0,
    }

    third = _import_portal(
        store=store,
        journal=journal,
        run_dir=run_dir,
        request_path=handshake_request,
    )
    runway_request = Path(store.portal_missions("agg-1")[1]["request_path"])
    fourth = _import_portal(
        store=store,
        journal=journal,
        run_dir=run_dir,
        request_path=runway_request,
        official_url="https://jobs.example.com/portal-official",
    )
    assert third["parent_sha256"] == second["sha256"]
    assert fourth["parent_sha256"] == third["sha256"]
    assert store.verify_snapshot_chain("agg-1", 4)[1]["sha256"] == fourth["sha256"]

    first_path, _ = store.get_snapshot("agg-1", 1)
    exact = SnapshotDiscovery(
        first_path,
        expected_query=request.query,
        expected_revision=1,
        expected_sha256=first["sha256"],
    )
    assert len(exact.find_roles(pack=None, query=request.query, limit=100)) == 20
    assert store.latest_revision("agg-1") == 4
    fourth_path, _ = store.get_snapshot("agg-1", 4)
    fourth_bytes = fourth_path.read_bytes()

    opportunity_request = tmp_path / "opportunity.request.json"
    opportunity_request.write_text("{}", encoding="utf-8")
    with OpportunityStore(tmp_path / "opportunities.sqlite3") as opportunity_store:
        opportunity_store.start_run(
            "opportunity-run", {"fixture": True}, request_path=opportunity_request
        )
        careers = OpportunityEvidence(
            evidence_type="careers_page",
            source_url="https://startup.example/careers",
            source_title="Careers",
            publisher="Startup Example",
            observed_at=datetime.now(timezone.utc).isoformat(),
            is_primary=True,
            claim="Three current roles.",
        )
        lead = OpportunityLead(
            lead_id=opportunity_lead_id(
                "startup.example", OpportunitySignal.ACTIVELY_HIRING
            ),
            company_name="Startup Example",
            company_url="https://startup.example",
            company_domain="startup.example",
            signal=OpportunitySignal.ACTIVELY_HIRING,
            route=OpportunityRoute.SPECULATIVE_OUTREACH,
            status=OpportunityStatus.OBSERVED,
            evidence=(careers,),
            careers_url="https://startup.example/careers",
            open_role_count=3,
        )
        opportunity_store.persist_lead(
            "opportunity-run",
            lead,
            OpportunityDecision(
                status=OpportunityStatus.VERIFIED,
                reasons=("fixture_verified",),
            ),
        )
        assert opportunity_store.sent_count() == 0
    assert fourth_path.read_bytes() == fourth_bytes
    assert store.latest_revision("agg-1") == 4
    store.close()

    updated_status = CliRunner().invoke(
        app, ["aggregate-status", "--run-id", "agg-1", "--watch", "--json"]
    )
    assert updated_status.exit_code == 0, updated_status.output
    updated = json.loads(updated_status.stdout)
    assert updated["latest_snapshot"]["revision"] == 4
    assert updated["opportunities"]["verified"] == 1
    assert updated["opportunities"]["sent"] == 0
    serialized = json.dumps(updated)
    assert "@example.com" not in serialized
    assert "prompt" not in serialized.lower()

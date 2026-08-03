from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from applypilot import config
from applypilot.aggregation.models import AggregationRequest, RawJob, SourceKind
from applypilot.aggregation.normalization import normalize_job
from applypilot.aggregation.snapshot import SnapshotDiscovery
from applypilot.aggregation.store import AggregationStore
from applypilot.autonomy.models import FreshnessEvidence
from applypilot.autonomy.runner import advance_artifact_run, prepare_run
from applypilot.employment import ApplicationSurface, OpportunityKind


QUERY = "product analytics internships in Austin"


def _snapshot(tmp_path):
    store = AggregationStore(tmp_path / "aggregation.sqlite3", run_dir=tmp_path / "aggregation-runs")
    request = AggregationRequest(query=QUERY, query_terms=("product intern",))
    store.start_run("agg-1", request)
    store.start_source("agg-1", SourceKind.DIRECT_ATS)
    store.record_observation(
        "agg-1",
        normalize_job(
            RawJob(
                source=SourceKind.DIRECT_ATS,
                source_job_id="official-1",
                title="Product Analytics Intern",
                company="Example Labs",
                location="Austin, TX",
                official_url="https://jobs.example.com/123",
                discovery_url="https://jobs.example.com/123",
                description="Entry-level product analytics internship using Python and SQL.",
                salary="$45-$55/hour",
                observed_at=datetime.now(timezone.utc),
                posted_at="2026-08-01",
            )
        ),
    )
    store.finish_source("agg-1", SourceKind.DIRECT_ATS, status="complete")
    store.start_source("agg-1", SourceKind.JOBSPY, source_id="jobspy:indeed:fixture")
    store.record_observation(
        "agg-1",
        normalize_job(
            RawJob(
                source=SourceKind.JOBSPY,
                source_job_id="board-1",
                title="Product Intern",
                company="Board Only Labs",
                location="Austin, TX",
                official_url="",
                discovery_url="https://www.indeed.com/viewjob?jk=board-1",
                description="Board-only result",
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
    store.complete_run("agg-1", status="complete")
    payload = store.publish_snapshot("agg-1", reason="fixture", status="complete")
    path, _ = store.get_snapshot("agg-1", 1)
    return path, payload


def _profile(monkeypatch, tmp_path):
    profile = {
        "experience": {
            "target_role": "product analytics intern",
            "education_level": "Bachelor of Business Administration, May 2027",
            "max_required_experience_years": 2,
        },
        "availability": {"preferred_locations": ["Austin, TX"]},
        "skills_boundary": {"technical": ["Python", "SQL"]},
        "work_authorization": {
            "legally_authorized_to_work": True,
            "require_sponsorship": False,
        },
        "eligibility": {"is_at_least_18": True},
    }
    profile_path = tmp_path / "profile.json"
    resume_path = tmp_path / "resume.txt"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    resume_path.write_text("Built Python and SQL analytics tools.\n", encoding="utf-8")
    monkeypatch.setattr(config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(config, "RESUME_PATH", resume_path)
    monkeypatch.setattr(config, "load_search_config", lambda: {})
    return profile_path, resume_path


def test_snapshot_discovery_returns_only_advanceable_candidates(tmp_path):
    path, payload = _snapshot(tmp_path)
    adapter = SnapshotDiscovery(
        path,
        expected_query=QUERY,
        expected_revision=1,
        expected_sha256=payload["sha256"],
    )
    candidates = adapter.find_roles(pack=None, query=QUERY, limit=30)
    assert len(candidates) == 1
    assert candidates[0].source == "aggregation_snapshot"
    assert candidates[0].compensation == "$45-$55/hour"
    assert candidates[0].metadata["aggregation_run_id"] == "agg-1"
    assert adapter.find_roles(pack=None, query=QUERY, limit=0) == []


def test_snapshot_discovery_rejects_wrong_query_revision_or_digest(tmp_path):
    path, payload = _snapshot(tmp_path)
    with pytest.raises(ValueError, match="query mismatch"):
        SnapshotDiscovery(path, expected_query="different")
    with pytest.raises(ValueError, match="revision mismatch"):
        SnapshotDiscovery(path, expected_query=QUERY, expected_revision=2)
    with pytest.raises(ValueError, match="digest mismatch"):
        SnapshotDiscovery(path, expected_query=QUERY, expected_sha256="0" * 64)


def test_prepare_binds_exact_snapshot_without_discovery_handoff(monkeypatch, tmp_path):
    _profile(monkeypatch, tmp_path)
    path, payload = _snapshot(tmp_path)
    paths = prepare_run(
        query=QUERY,
        output_dir=tmp_path / "autonomy-runs",
        aggregation_snapshot_path=path,
        aggregation_snapshot_revision=1,
        aggregation_snapshot_sha256=payload["sha256"],
        legacy_web_discovery=False,
    )
    run_dir = tmp_path / "autonomy-runs" / paths["run_dir"].split("/")[-1]
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert "request" not in paths
    assert not (run_dir / "handoff" / "discovery.request.json").exists()
    assert manifest["aggregation_run_id"] == "agg-1"
    assert manifest["aggregation_snapshot_revision"] == 1
    assert manifest["aggregation_snapshot_sha256"] == payload["sha256"]
    assert stat.S_IMODE((run_dir / "aggregation_snapshot.json").stat().st_mode) == 0o600


class _Verifier:
    def verify(self, candidate):
        return FreshnessEvidence.now(
            official_url=candidate.official_url,
            first_party=True,
            resolved=True,
            open_state=True,
            title=candidate.title,
            description=candidate.description,
            posted_date=candidate.posted_date,
            status_code=200,
            evidence=("fixture first-party response",),
            opportunity_kind=OpportunityKind.POSTED_EMPLOYMENT,
            application_surface=ApplicationSurface.PROVIDER_REQUISITION,
            requisition_id="official-1",
        )


def test_snapshot_advance_reaches_material_handoff_with_zero_discovery_model_calls(
    monkeypatch, tmp_path
):
    _profile(monkeypatch, tmp_path)
    path, payload = _snapshot(tmp_path)
    paths = prepare_run(
        query=QUERY,
        output_dir=tmp_path / "autonomy-runs",
        aggregation_snapshot_path=path,
        aggregation_snapshot_revision=1,
        aggregation_snapshot_sha256=payload["sha256"],
        legacy_web_discovery=False,
    )
    run_dir = Path(paths["run_dir"])
    result = advance_artifact_run(
        run_dir=run_dir,
        approved_fact_digest=paths["fact_digest"],
        verifier=_Verifier(),
    )
    assert result["status"] == "awaiting_chatgpt_web", json.dumps(result, indent=2)
    assert result["pending_requests"][0]["kind"] == "material_packet"
    assert result["usage"]["counts"]["model_calls"] == 0

from __future__ import annotations

import asyncio
import json
import stat
import sys
from datetime import datetime, timezone

import pytest

from applypilot.aggregation.models import AggregationRequest, RawJob, SourceKind, VerificationState
from applypilot.aggregation.normalization import normalize_job
from applypilot.aggregation.store import AggregationStore
from applypilot.aggregation.sources.jobspy import (
    JobSpyContractError,
    JobSpySettings,
    build_jobspy_units,
    enrich_jobspy_run,
    normalize_jobspy_row,
    run_jobspy_unit,
)
from applypilot.observability.events import EventJournal


def test_jobspy_units_are_bounded_and_do_not_enable_bypass_features():
    settings = JobSpySettings(
        boards=("indeed", "google", "zip_recruiter"),
        max_query_terms=2,
        max_locations=2,
        results_per_board=25,
        max_workers=3,
        deadline_seconds=30,
    )
    units = build_jobspy_units(
        terms=("product intern", "data analyst intern", "ignored third term"),
        locations=("Austin, TX", "Remote US", "ignored third location"),
        settings=settings,
    )
    assert len(units) == 12
    assert {unit.board for unit in units} == {"indeed", "google", "zip_recruiter"}
    assert all(unit.results_wanted == 25 for unit in units)
    assert all(unit.proxies == () for unit in units)
    assert all(unit.linkedin_fetch_description is False for unit in units)
    assert len({unit.request_digest for unit in units}) == 12


def test_jobspy_rejects_unconfigured_or_duplicated_boards():
    with pytest.raises(ValueError, match="board allowlist"):
        JobSpySettings(boards=("indeed", "indeed", "unknown_board")).validate()


def test_jobspy_normalization_requires_first_party_direct_url():
    resolved = normalize_job(
        normalize_jobspy_row(
            {
                "id": "indeed-1",
                "title": "Product Intern",
                "company": "Example Labs",
                "location": "Austin, TX",
                "job_url": "https://www.indeed.com/viewjob?jk=1",
                "job_url_direct": "https://jobs.example.com/123",
                "description": "Fixture",
            },
            board="indeed",
        )
    )
    board_only = normalize_job(
        normalize_jobspy_row(
            {
                "id": "indeed-2",
                "title": "Product Intern",
                "company": "Example Labs",
                "location": "Austin, TX",
                "job_url": "https://www.indeed.com/viewjob?jk=2",
                "job_url_direct": "https://www.indeed.com/rc/clk?jk=2",
                "description": "Fixture",
            },
            board="indeed",
        )
    )
    assert resolved.verification_state is VerificationState.FIRST_PARTY_RESOLVED
    assert resolved.advanceable is True
    assert board_only.verification_state is VerificationState.BOARD_ONLY
    assert board_only.advanceable is False


def _write_fake_worker(path, *, wrong_digest: bool = False, sleep_seconds: float = 0) -> None:
    path.write_text(
        "\n".join(
            [
                "import json, os, pathlib, sys, time",
                "request_path, response_path = map(pathlib.Path, sys.argv[1:3])",
                f"time.sleep({sleep_seconds!r})",
                "request = json.loads(request_path.read_text())",
                "if os.environ.get('HTTP_PROXY') or os.environ.get('JOBSPY_TEST_SECRET'): raise SystemExit(9)",
                "payload = {",
                "  'schema_version': 'applypilot.jobspy-response.v1',",
                ("  'request_digest': '0' * 64," if wrong_digest else "  'request_digest': request['request_digest'],"),
                "  'unit_id': request['unit_id'],",
                "  'board': request['board'],",
                "  'status': 'complete',",
                "  'rows': [{",
                "    'id': 'fixture-1', 'site': request['board'], 'title': 'Product Intern',",
                "    'company': 'Example Labs', 'location': 'Austin, TX',",
                "    'job_url': 'https://www.indeed.com/viewjob?jk=1',",
                "    'job_url_direct': 'https://jobs.example.com/123', 'description': 'Fixture'",
                "  }]",
                "}",
                "response_path.write_text(json.dumps(payload))",
                "os.chmod(response_path, 0o600)",
            ]
        ),
        encoding="utf-8",
    )


def test_jobspy_parent_uses_private_files_and_scrubbed_environment(monkeypatch, tmp_path):
    worker = tmp_path / "fake_worker.py"
    _write_fake_worker(worker)
    monkeypatch.setenv("HTTP_PROXY", "http://secret.invalid")
    monkeypatch.setenv("JOBSPY_TEST_SECRET", "secret")
    [unit] = build_jobspy_units(
        terms=("product intern",),
        locations=("Austin, TX",),
        settings=JobSpySettings(boards=("indeed",), results_per_board=1),
    )
    result = asyncio.run(
        run_jobspy_unit(
            unit,
            work_dir=tmp_path / "unit",
            timeout_seconds=2,
            command_builder=lambda request, response: (
                sys.executable,
                str(worker),
                str(request),
                str(response),
            ),
        )
    )
    assert result.status == "complete"
    assert len(result.rows) == 1
    request_path = tmp_path / "unit" / "request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert stat.S_IMODE(request_path.stat().st_mode) == 0o600
    assert not ({"proxies", "cookies", "token", "profile_path"} & set(request))


def test_jobspy_parent_rejects_wrong_response_binding(tmp_path):
    worker = tmp_path / "wrong_worker.py"
    _write_fake_worker(worker, wrong_digest=True)
    [unit] = build_jobspy_units(
        terms=("product intern",),
        locations=("Austin, TX",),
        settings=JobSpySettings(boards=("indeed",), results_per_board=1),
    )
    with pytest.raises(JobSpyContractError, match="request digest"):
        asyncio.run(
            run_jobspy_unit(
                unit,
                work_dir=tmp_path / "unit",
                timeout_seconds=2,
                command_builder=lambda request, response: (
                    sys.executable,
                    str(worker),
                    str(request),
                    str(response),
                ),
            )
        )


def test_jobspy_parent_terminates_timed_out_worker(tmp_path):
    worker = tmp_path / "slow_worker.py"
    _write_fake_worker(worker, sleep_seconds=2)
    [unit] = build_jobspy_units(
        terms=("product intern",),
        locations=("Austin, TX",),
        settings=JobSpySettings(boards=("indeed",), results_per_board=1),
    )
    result = asyncio.run(
        run_jobspy_unit(
            unit,
            work_dir=tmp_path / "unit",
            timeout_seconds=0.05,
            command_builder=lambda request, response: (
                sys.executable,
                str(worker),
                str(request),
                str(response),
            ),
        )
    )
    assert result.status == "timed_out"
    assert result.error_class == "TimeoutError"


def test_jobspy_enrichment_publishes_digest_linked_revision(tmp_path):
    worker = tmp_path / "fake_worker.py"
    _write_fake_worker(worker)
    request = AggregationRequest(
        query="product internships",
        query_terms=("product intern",),
        locations=("Austin, TX",),
    )
    store = AggregationStore(tmp_path / "aggregation.sqlite3", run_dir=tmp_path / "runs")
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
        "agg-1", reason="fast_lane", status="partial", pending_enrichment=("jobspy",)
    )
    journal = EventJournal(tmp_path / "events.ndjson", run_id="agg-1")
    second = asyncio.run(
        enrich_jobspy_run(
            store=store,
            journal=journal,
            run_id="agg-1",
            request=store.get_request("agg-1"),
            work_dir=tmp_path / "jobspy",
            settings=JobSpySettings(
                boards=("indeed",),
                results_per_board=1,
                max_workers=1,
                deadline_seconds=2,
            ),
            command_builder=lambda request_path, response_path: (
                sys.executable,
                str(worker),
                str(request_path),
                str(response_path),
            ),
        )
    )
    assert second["revision"] == 2
    assert second["parent_sha256"] == first["sha256"]
    assert second["status"] == "complete"
    assert second["candidate_count"] == 1
    assert second["observation_count"] == 2
    assert second["jobs"][0]["source_count"] == 2
    jobspy_rows = [row for row in second["sources"] if row["source"] == "jobspy"]
    assert len(jobspy_rows) == 1
    assert jobspy_rows[0]["status"] == "complete"

from __future__ import annotations

import json
from datetime import datetime, timezone

from typer.testing import CliRunner

from applypilot.aggregation.models import RawJob, SourceCapability, SourceKind
from applypilot.cli import app

runner = CliRunner()


class OneJobSource:
    kind = SourceKind.CACHE
    capability = SourceCapability.LOCAL_CACHE

    async def search(self, request):
        del request
        yield RawJob(
            source=self.kind,
            source_job_id="cache-1",
            title="Product Intern",
            company="Example Labs",
            location="Austin, TX",
            official_url="https://jobs.example.com/123",
            discovery_url="https://jobs.example.com/123",
            description="Fixture",
            observed_at=datetime.now(timezone.utc),
        )


def test_aggregate_status_returns_unknown_run(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    result = runner.invoke(app, ["aggregate-status", "--run-id", "missing", "--json"])
    assert result.exit_code == 1
    assert "unknown aggregation run" in result.stdout


def test_aggregate_requires_portal_flag_for_browser_source(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    result = runner.invoke(
        app,
        [
            "aggregate",
            "--query",
            "product internships",
            "--term",
            "product intern",
            "--source",
            "handshake",
            "--no-watch",
        ],
    )
    assert result.exit_code == 1
    assert "use --portal handshake for a browser mission" in result.stdout


def test_aggregate_prints_machine_json_and_persists_revision(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "applypilot.cli._build_aggregation_sources",
        lambda **kwargs: [OneJobSource()],
    )
    result = runner.invoke(
        app,
        [
            "aggregate",
            "--query",
            "product internships",
            "--term",
            "product intern",
            "--source",
            "cache",
            "--no-watch",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "complete"
    assert payload["candidate_count"] == 1
    assert payload["snapshot_revision"] == 1
    assert payload["pending_enrichment"] == []
    assert (tmp_path / "aggregation.sqlite3").is_file()


def test_aggregate_records_enrichment_without_delaying_revision_one(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "applypilot.cli._build_aggregation_sources",
        lambda **kwargs: [OneJobSource()],
    )
    result = runner.invoke(
        app,
        [
            "aggregate",
            "--query",
            "product internships",
            "--term",
            "product intern",
            "--source",
            "cache",
            "--enrich",
            "jobspy",
            "--portal",
            "handshake",
            "--no-watch",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "partial"
    assert payload["snapshot_revision"] == 1
    assert payload["pending_enrichment"] == ["handshake", "jobspy"]

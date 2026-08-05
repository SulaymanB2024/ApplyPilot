from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from applypilot import database
from applypilot.autonomy.candidate_import import (
    CANDIDATE_IMPORT_SCHEMA,
    CandidateImportError,
    import_candidate_file,
    load_candidate_document,
)
from applypilot.cli import app


CLI_RUNNER = CliRunner()


def _write_document(path, candidates, **overrides):
    document = {"schema_version": 1, "candidates": candidates}
    document.update(overrides)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def jobs_db(tmp_path):
    db_path = tmp_path / "applypilot.db"
    conn = database.init_db(db_path)
    yield conn
    database.close_connection(db_path)


def test_import_candidate_file_preserves_supplied_facts_without_application_state(
    jobs_db,
    tmp_path,
):
    candidate_file = _write_document(
        tmp_path / "candidates.json",
        [
            {
                "official_url": "HTTPS://Jobs.Example.com/roles/analyst#details",
                "title": "Product Analyst Intern",
                "company": "Example Labs",
                "location": "Austin, TX",
                "description": "Build product analytics with Python and SQL.",
                "salary": "$25/hour",
                "requisition": "REQ-123",
                "application_url": "https://apply.example.com/jobs/REQ-123?source=chatgpt",
            }
        ],
    )

    result = import_candidate_file(jobs_db, candidate_file)

    assert result.as_dict() == {
        "status": "candidates_imported",
        "total": 1,
        "imported": 1,
        "duplicates": 0,
    }
    row = jobs_db.execute("SELECT * FROM jobs").fetchone()
    assert row["url"] == "https://jobs.example.com/roles/analyst"
    assert row["title"] == "Product Analyst Intern"
    assert row["site"] == "Example Labs"
    assert row["strategy"] == "chatgpt_web_verified"
    assert row["location"] == "Austin, TX"
    assert row["salary"] == "$25/hour"
    assert row["description"] == "Build product analytics with Python and SQL."
    assert row["full_description"] == row["description"]
    assert row["requisition"] == "REQ-123"
    assert row["application_url"] == "https://apply.example.com/jobs/REQ-123?source=chatgpt"
    assert row["apply_domain"] == "apply.example.com"
    assert row["applied_at"] is None
    assert row["apply_status"] is None
    assert row["apply_attempts"] == 0


def test_import_candidate_file_deduplicates_with_existing_canonical_helpers(
    jobs_db,
    tmp_path,
):
    candidate_file = _write_document(
        tmp_path / "candidates.json",
        [
            {
                "official_url": "https://boards.greenhouse.io/example/jobs/456?gh_src=one",
                "title": "AI Product Intern",
                "company": "Example",
            },
            {
                "official_url": "https://boards.greenhouse.io/example/jobs/456?gh_src=two",
                "title": "AI Product Intern",
                "company": "Example",
            },
        ],
    )

    result = import_candidate_file(jobs_db, candidate_file)

    assert (result.total, result.imported, result.duplicates) == (2, 1, 1)
    assert jobs_db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert jobs_db.execute("SELECT canonical_job_id FROM jobs").fetchone()[0] == "greenhouse:456"


def test_document_is_fully_validated_before_any_database_write(jobs_db, tmp_path):
    candidate_file = _write_document(
        tmp_path / "candidates.json",
        [
            {
                "official_url": "https://jobs.example.com/roles/valid",
                "title": "Valid Role",
                "company": "Example",
            },
            {
                "official_url": "https://jobs.example.com/roles/invalid",
                "title": "Invalid Role",
                "company": "Example",
                "made_up_fit_score": 10,
            },
        ],
    )

    with pytest.raises(CandidateImportError, match=r"candidates\[1\] has unknown field"):
        import_candidate_file(jobs_db, candidate_file)

    assert jobs_db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "document, error",
    [
        ({"schema_version": True, "candidates": [{}]}, "schema_version must be the integer 1"),
        ({"schema_version": 1.0, "candidates": [{}]}, "schema_version must be the integer 1"),
        ({"schema_version": 1, "candidates": []}, "at least one item"),
        (
            {
                "schema_version": 1,
                "candidates": [
                    {"official_url": "https://jobs.example.com/1", "title": "Role"}
                ],
            },
            "missing field(s): company",
        ),
        (
            {
                "schema_version": 1,
                "candidates": [
                    {
                        "official_url": "javascript:alert(1)",
                        "title": "Role",
                        "company": "Example",
                    }
                ],
            },
            "absolute HTTP(S) URL",
        ),
        (
            {
                "schema_version": 1,
                "candidates": [
                    {
                        "official_url": "https://jobs.example.com/1",
                        "title": 123,
                        "company": "Example",
                    }
                ],
            },
            "title must be a string",
        ),
    ],
)
def test_candidate_document_contract_is_strict(tmp_path, document, error):
    candidate_file = tmp_path / "candidates.json"
    candidate_file.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CandidateImportError, match=re.escape(error)):
        load_candidate_document(candidate_file)


def test_schema_for_browser_producers_forbids_extra_fields():
    assert CANDIDATE_IMPORT_SCHEMA["additionalProperties"] is False
    item_schema = CANDIDATE_IMPORT_SCHEMA["properties"]["candidates"]["items"]
    assert item_schema["additionalProperties"] is False
    assert set(item_schema["required"]) == {"official_url", "title", "company"}


def test_import_candidates_cli_writes_only_to_jobs_db(monkeypatch, tmp_path):
    db_path = tmp_path / "cli.db"
    database.init_db(db_path)
    database.close_connection(db_path)
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr("applypilot.cli._bootstrap", lambda: database.init_db(db_path))
    candidate_file = _write_document(
        tmp_path / "candidates.json",
        [
            {
                "official_url": "https://jobs.example.com/roles/cli",
                "title": "Business Analytics Intern",
                "company": "Example",
            }
        ],
    )

    result = CLI_RUNNER.invoke(
        app,
        ["autonomy", "import-candidates", "--file", str(candidate_file)],
    )

    assert result.exit_code == 0, result.output
    assert '"status": "candidates_imported"' in result.output
    conn = database.get_connection(db_path)
    row = conn.execute(
        "SELECT strategy, applied_at, apply_status, apply_attempts FROM jobs"
    ).fetchone()
    assert tuple(row) == ("chatgpt_web_verified", None, None, 0)
    database.close_connection(db_path)

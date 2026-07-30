import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from applypilot.cli import app
from applypilot.profile_cache import (
    build_profile_cache_report,
    load_custom_answers,
    missing_profile_cache_questions,
    update_profile_cache,
    write_private_profile,
)

runner = CliRunner()


def test_profile_cache_report_exposes_status_without_values(tmp_path: Path):
    resume_pdf = tmp_path / "resume.pdf"
    resume_pdf.write_bytes(b"resume")
    profile = {
        "personal": {
            "full_name": "Test Candidate",
            "email": "candidate@example.com",
            "phone": "",
            "city": "Austin",
            "province_state": "TX",
            "country": "USA",
        },
        "work_authorization": {
            "legally_authorized_to_work": True,
            "require_sponsorship": False,
        },
        "availability": {
            "earliest_start_date": "May 2027",
            "preferred_locations": ["Austin", "Remote US"],
        },
    }

    report = build_profile_cache_report(
        profile,
        resume_text="Test Candidate resume",
        resume_pdf_path=resume_pdf,
    )

    assert report["ready_for_form_work"] is False
    assert report["missing_required"] == ["personal.phone"]
    assert report["resume_pdf_sha256"]
    assert "candidate@example.com" not in str(report)


def test_profile_cache_boolean_false_counts_as_present():
    profile = {
        "personal": {
            "full_name": "Test Candidate",
            "email": "candidate@example.com",
            "phone": "555-0100",
            "city": "Austin",
            "province_state": "TX",
            "country": "USA",
        },
        "work_authorization": {
            "legally_authorized_to_work": True,
            "require_sponsorship": False,
        },
        "availability": {
            "earliest_start_date": "May 2027",
            "preferred_locations": ["Austin"],
        },
    }

    report = build_profile_cache_report(profile, resume_text="resume")

    assert report["ready_for_form_work"] is True
    sponsorship = next(field for field in report["fields"] if field["path"] == "work_authorization.require_sponsorship")
    assert sponsorship["present"] is True


def test_profile_cache_counts_only_valid_non_reserved_custom_answers():
    profile = {
        "autofill": {
            "custom_answers": [
                {
                    "question": "How did you hear about us?",
                    "aliases": ["Referral source?"],
                    "value": "Company website",
                },
                {
                    "question": "Type your signature",
                    "value": "Test Candidate",
                },
                {
                    "question": "",
                    "value": "Missing question",
                },
            ]
        }
    }

    answers, invalid = load_custom_answers(profile)
    report = build_profile_cache_report(profile, resume_text="resume")

    assert len(answers) == 1
    assert invalid == 2
    assert report["custom_answer_count"] == 1
    assert report["invalid_custom_answer_count"] == 2
    assert report["pending_verification"] == []


def test_missing_profile_cache_questions_are_resumable_and_can_be_required_only():
    profile = {
        "personal": {"phone": "555-0100"},
        "work_authorization": {
            "legally_authorized_to_work": True,
            "require_sponsorship": None,
        },
        "availability": {
            "earliest_start_date": "unconfirmed",
            "preferred_locations": [],
        },
    }

    all_missing = {question.path for question in missing_profile_cache_questions(profile)}
    required_missing = {question.path for question in missing_profile_cache_questions(profile, required_only=True)}

    assert "personal.phone" not in all_missing
    assert "personal.address" in all_missing
    assert required_missing == {
        "work_authorization.require_sponsorship",
        "availability.earliest_start_date",
        "availability.preferred_locations",
    }


def test_update_profile_cache_validates_types_and_preserves_existing_facts():
    profile = {
        "personal": {"full_name": "Test Candidate"},
        "autofill": {
            "pending_answers": [
                {
                    "path": "availability.earliest_start_date",
                    "value": "May 2027",
                }
            ]
        },
    }
    original = json.loads(json.dumps(profile))

    updated = update_profile_cache(
        profile,
        {
            "work_authorization.legally_authorized_to_work": True,
            "availability.preferred_locations": ["Austin", "Remote US"],
            "availability.earliest_start_date": "May 2027",
        },
    )

    assert profile == original
    assert updated["personal"]["full_name"] == "Test Candidate"
    assert updated["work_authorization"]["legally_authorized_to_work"] is True
    assert updated["availability"]["preferred_locations"] == [
        "Austin",
        "Remote US",
    ]
    assert updated["autofill"]["pending_answers"] == []
    with pytest.raises(ValueError, match="requires a boolean"):
        update_profile_cache(profile, {"eligibility.is_at_least_18": "yes"})
    with pytest.raises(ValueError, match="unsupported"):
        update_profile_cache(profile, {"personal.ssn": "000-00-0000"})


def test_write_private_profile_is_atomic_private_and_backed_up(tmp_path: Path):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"personal":{"full_name":"Original"}}', encoding="utf-8")
    profile_path.chmod(0o644)

    backup = write_private_profile(
        profile_path,
        {"personal": {"full_name": "Updated"}},
    )

    assert backup is not None
    assert backup.read_text(encoding="utf-8") == '{"personal":{"full_name":"Original"}}'
    assert profile_path.stat().st_mode & 0o777 == 0o600
    assert backup.stat().st_mode & 0o777 == 0o600
    assert profile_path.parent.stat().st_mode & 0o777 == 0o700
    assert '"Updated"' in profile_path.read_text(encoding="utf-8")


def test_profile_cache_reports_pending_answer_paths_without_values():
    profile = {
        "autofill": {
            "pending_answers": [
                {
                    "path": "personal.address",
                    "value": "123 Private Street",
                    "reason": "confirm current",
                },
                {
                    "path": "personal.ssn",
                    "value": "not allowed",
                },
            ]
        }
    }

    report = build_profile_cache_report(profile, resume_text="resume")

    assert report["pending_verification"] == ["personal.address"]
    assert "123 Private Street" not in str(report)


def test_profile_cache_collect_cli_fills_required_facts_privately(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from applypilot import config
    from applypilot import cli

    profile_path = tmp_path / "profile.json"
    resume_path = tmp_path / "resume.txt"
    resume_pdf_path = tmp_path / "resume.pdf"
    profile_path.write_text(
        json.dumps(
            {
                "personal": {
                    "full_name": "Test Candidate",
                    "email": "candidate@example.com",
                    "phone": "",
                    "city": "Austin",
                    "province_state": "TX",
                    "country": "USA",
                },
                "work_authorization": {
                    "legally_authorized_to_work": None,
                    "require_sponsorship": None,
                },
                "availability": {
                    "earliest_start_date": "",
                    "preferred_locations": [],
                },
            }
        ),
        encoding="utf-8",
    )
    resume_path.write_text("Test Candidate resume", encoding="utf-8")
    resume_pdf_path.write_bytes(b"resume")
    monkeypatch.setattr(cli, "_bootstrap_config_only", lambda: None)
    monkeypatch.setattr(config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(config, "RESUME_PATH", resume_path)
    monkeypatch.setattr(config, "RESUME_PDF_PATH", resume_pdf_path)

    result = runner.invoke(
        app,
        ["profile-cache", "--collect", "--required-only", "--json"],
        input="555-0100\nyes\nno\nMay 2027\nAustin, Remote US\n",
    )

    assert result.exit_code == 0, result.output
    updated = json.loads(profile_path.read_text(encoding="utf-8"))
    assert updated["personal"]["phone"] == "555-0100"
    assert updated["work_authorization"]["legally_authorized_to_work"] is True
    assert updated["work_authorization"]["require_sponsorship"] is False
    assert updated["availability"]["earliest_start_date"] == "May 2027"
    assert updated["availability"]["preferred_locations"] == [
        "Austin",
        "Remote US",
    ]
    assert profile_path.stat().st_mode & 0o777 == 0o600
    assert '"ready_for_form_work": true' in result.output

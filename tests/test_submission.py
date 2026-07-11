from types import SimpleNamespace

import pytest

from applypilot.apply.submission import (
    ResponseEvidence,
    SubmissionEvidence,
    classify_submission,
)
from applypilot.apply.submission_auth import (
    consume_submit_manifest,
    form_review_digest,
    material_digest,
    submission_policy_digest,
    write_submit_manifest,
)


def test_post_success_plus_confirmation_dom_is_confirmed():
    result = classify_submission(
        SubmissionEvidence(
            response=ResponseEvidence(
                url="https://ats.example.com/applications",
                method="POST",
                status=201,
                body={"data": {"submitApplication": {"id": "app123"}}},
            ),
            after_url="https://ats.example.com/apply/confirmation",
            dom_text="Application submitted. Confirmation number: ABC12345",
        )
    )

    assert result.status == "submitted_confirmed"
    assert result.confidence == "confirmed"
    assert result.confirmation_number == "ABC12345"


def test_graphql_errors_are_not_submitted_even_with_http_200():
    result = classify_submission(
        SubmissionEvidence(
            response=ResponseEvidence(
                url="https://ats.example.com/graphql",
                method="POST",
                status=200,
                body={"errors": [{"message": "Required field missing"}]},
            ),
            dom_text="Application submitted",
        )
    )

    assert result.status == "not_submitted"
    assert result.reason == "submit_response_failed"


def test_validation_errors_are_not_submitted():
    result = classify_submission(
        SubmissionEvidence(
            response=ResponseEvidence(url="https://ats.example.com/applications", method="POST", status=201),
            validation_errors=("aria_invalid=2",),
        )
    )

    assert result.status == "not_submitted"
    assert result.reason == "validation_errors"


def test_confirmation_text_alone_is_unconfirmed_not_applied():
    result = classify_submission(
        SubmissionEvidence(
            after_url="https://ats.example.com/apply",
            dom_text="Thank you for applying",
        )
    )

    assert result.status == "submitted_unconfirmed"
    assert result.confidence == "unconfirmed"


def test_submit_manifest_is_candidate_material_form_policy_bound_and_one_time(tmp_path):
    resume = tmp_path / "resume.txt"
    resume.write_text("Reviewed resume", encoding="utf-8")
    resume.with_suffix(".pdf").write_bytes(b"%PDF-reviewed")
    job = {
        "url": "https://jobs.example.com/apply?jobId=123",
        "application_url": "https://jobs.example.com/apply?jobId=123",
        "tailored_resume_path": str(resume),
    }
    settings = SimpleNamespace(
        deterministic_controller=True,
        field_model_call_budget=0,
        allow_account_creation=False,
        credential_provider="google_password_manager",
    )
    material_sha = material_digest(job)
    form_sha = form_review_digest(
        [
            {
                "selector": "#email",
                "name": "email",
                "type": "email",
                "required": True,
                "value": "candidate@example.com",
            }
        ]
    )
    policy_sha = submission_policy_digest(settings)
    manifest = write_submit_manifest(
        job=job,
        fact_digest="facts",
        material_sha256=material_sha,
        form_sha256=form_sha,
        policy_sha256=policy_sha,
        output_dir=tmp_path / "authorizations",
    )

    consume_submit_manifest(
        manifest,
        job=job,
        fact_digest="facts",
        material_sha256=material_sha,
        form_sha256=form_sha,
        policy_sha256=policy_sha,
        authorization_dir=tmp_path / "authorizations",
    )
    with pytest.raises(PermissionError, match="already consumed"):
        consume_submit_manifest(
            manifest,
            job=job,
            fact_digest="facts",
            material_sha256=material_sha,
            form_sha256=form_sha,
            policy_sha256=policy_sha,
            authorization_dir=tmp_path / "authorizations",
        )

    copied_manifest = tmp_path / "copied-manifest.json"
    copied_manifest.write_bytes(manifest.read_bytes())
    with pytest.raises(PermissionError, match="already consumed"):
        consume_submit_manifest(
            copied_manifest,
            job=job,
            fact_digest="facts",
            material_sha256=material_sha,
            form_sha256=form_sha,
            policy_sha256=policy_sha,
            authorization_dir=tmp_path / "authorizations",
        )


def test_submit_manifest_rejects_changed_material_before_consumption(tmp_path):
    resume = tmp_path / "resume.txt"
    resume.write_text("Reviewed resume", encoding="utf-8")
    resume.with_suffix(".pdf").write_bytes(b"%PDF-reviewed")
    job = {
        "url": "https://jobs.example.com/roles/123",
        "tailored_resume_path": str(resume),
    }
    manifest = write_submit_manifest(
        job=job,
        fact_digest="facts",
        material_sha256=material_digest(job),
        form_sha256="form",
        policy_sha256="policy",
        output_dir=tmp_path / "authorizations",
    )
    resume.with_suffix(".pdf").write_bytes(b"%PDF-changed")

    with pytest.raises(PermissionError, match="material_digest"):
        consume_submit_manifest(
            manifest,
            job=job,
            fact_digest="facts",
            material_sha256=material_digest(job),
            form_sha256="form",
            policy_sha256="policy",
            authorization_dir=tmp_path / "authorizations",
        )

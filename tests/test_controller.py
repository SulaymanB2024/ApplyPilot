import pytest

from applypilot.apply import controller as controller_mod
from applypilot.apply.controller import (
    DeterministicApplyController,
    FieldSpec,
    classify_page_state,
    field_value_for,
    first_email,
    is_email_only_posting,
)
from applypilot.apply.harness import load_settings
from applypilot.autonomy.facts import FactCorrection, FactState, build_fact_ledger
from applypilot.apply.safety import PageInput, PageState
from applypilot.apply.onepassword import OnePasswordLogin


PROFILE = {
    "personal": {
        "full_name": "Test Candidate",
        "email": "candidate@example.com",
        "phone": "555-0100",
        "address": "123 Main St",
        "city": "Austin",
        "province_state": "TX",
        "postal_code": "78701",
        "country": "USA",
        "linkedin_url": "https://linkedin.com/in/test",
        "github_url": "https://github.com/test",
        "portfolio_url": "https://example.dev",
    },
    "work_authorization": {
        "legally_authorized_to_work": True,
        "require_sponsorship": False,
    },
    "compensation": {"salary_expectation": "120000"},
    "availability": {"earliest_start_date": "Immediately"},
    "screening": {"accept_application_terms": True},
    "eeo_voluntary": {},
}


def resolve(label, field_type="text", credential=None):
    return field_value_for(
        FieldSpec(selector="#x", tag="input", type=field_type, label=label),
        profile=PROFILE,
        job={"title": "Software Engineer"},
        credential=credential,
    )


def test_account_only_page_requires_explicit_account_permission(monkeypatch, tmp_path):
    class Body:
        @staticmethod
        def inner_text(**_kwargs):
            return "Create account to continue"

    class Page:
        url = "https://jobs.example.com/register"

        @staticmethod
        def locator(_selector):
            return Body()

    monkeypatch.setattr(controller_mod.config, "load_profile", lambda: PROFILE)
    denied = DeterministicApplyController(
        job={"url": Page.url, "title": "Analyst"},
        port=9222,
        worker_dir=tmp_path,
        settings=load_settings(allow_account_creation=False),
    )
    allowed = DeterministicApplyController(
        job={"url": Page.url, "title": "Analyst"},
        port=9222,
        worker_dir=tmp_path,
        settings=load_settings(allow_account_creation=True),
    )

    with pytest.raises(RuntimeError, match="account_required"):
        denied._credential_for_page(Page())
    assert allowed._credential_for_page(Page()) is None


def test_live_controller_requires_approved_clean_fact_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(controller_mod.config, "load_profile", lambda: PROFILE)
    resume_path = tmp_path / "tailored.txt"
    resume_path.write_text("Fabricated Employer", encoding="utf-8")
    profile = {
        **PROFILE,
        "availability": {"earliest_start_date": "Immediately"},
    }
    ledger = build_fact_ledger(
        profile,
        resume_text="Fabricated Employer\nCanonical resume",
        corrections=(
            FactCorrection(
                match="Fabricated Employer",
                state=FactState.REJECTED,
                reason="not an applicant fact",
            ),
        ),
    )
    controller = DeterministicApplyController(
        job={
            "url": "https://jobs.example.com/apply",
            "title": "Analyst",
            "tailored_resume_path": str(resume_path),
        },
        port=9222,
        worker_dir=tmp_path,
        settings=load_settings(),
        dry_run=False,
        fact_ledger=ledger,
    )

    with pytest.raises(RuntimeError, match="artifact_fact_validation_failed"):
        controller._preflight()


def test_classify_page_state_fails_closed_for_sso_and_verification():
    assert classify_page_state("https://accounts.google.com/o/oauth", "") == "sso_required"
    assert (
        classify_page_state(
            PageState(
                url="https://example.com",
                inputs=(PageInput(type="text", autocomplete="one-time-code"),),
            )
        )
        == "mfa_required"
    )
    assert classify_page_state("https://example.com", "Allow camera to continue") == "unsafe_permissions"


def test_email_only_detection_and_recipient_extraction():
    text = "To apply, send your resume to hiring@example.com by Friday."

    assert is_email_only_posting(text)
    assert first_email(text) == "hiring@example.com"


def test_field_value_for_profile_facts():
    assert resolve("First name").value == "Test"
    assert resolve("Last name").value == "Candidate"
    assert resolve("Email address").value == "candidate@example.com"
    assert resolve("Phone").value == "555-0100"
    assert resolve("Will you require sponsorship?").value == "No"
    assert resolve("Are you authorized to work?").value == "Yes"
    assert resolve("Salary expectation").value == "120000"


def test_field_value_for_password_uses_1password_credential():
    credential = OnePasswordLogin(
        item_id="item123",
        title="Example",
        username="candidate@example.com",
        password="pw-test",
        url="https://example.com",
        domain="example.com",
    )
    resolved = resolve("Password", field_type="password", credential=credential)

    assert resolved.value == "pw-test"
    assert resolved.sensitive is True
    assert resolved.source == "1password"


def test_required_terms_checkbox_can_be_checked_deterministically():
    resolved = field_value_for(
        FieldSpec(
            selector="#terms",
            tag="input",
            type="checkbox",
            label="I agree to the privacy policy and certify this is accurate",
            required=True,
        ),
        profile=PROFILE,
        job={"title": "Software Engineer"},
    )

    assert resolved.value is True

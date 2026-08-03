import json
import subprocess

from applypilot.apply.field_resolver import (
    CodexResolver,
    FieldSpec,
    field_value_for,
    model_field_profile_from_ledger,
    needs_llm_fallback,
    validate_resolved_value,
)
from applypilot.autonomy.facts import FactCorrection, FactState, build_fact_ledger


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
        "website_url": "https://example.dev",
        "password": "do-not-send",
    },
    "work_authorization": {
        "legally_authorized_to_work": True,
        "require_sponsorship": False,
        "work_permit_type": "US citizen",
    },
    "compensation": {
        "salary_expectation": "Open to market-competitive compensation",
        "hourly_rate_min": "30",
    },
    "availability": {
        "earliest_start_date": "Immediately",
        "available_for_full_internship_period": True,
        "internship_period": "May 24 through August 13, 2027",
        "preferred_locations": ["Austin", "Remote US"],
        "willing_to_relocate": False,
        "willing_to_travel": True,
    },
    "eligibility": {"is_at_least_18": True},
    "experience": {
        "current_company": "Example Labs",
        "current_title": "Product Analytics Intern",
    },
    "education": {
        "school": "Example University",
        "primary_degree": "Bachelor of Business Administration",
        "expected_graduation_date": "May 2028",
        "current_student": True,
    },
    "autofill": {
        "custom_answers": [
            {
                "question": "How did you hear about this opportunity?",
                "aliases": ["How did you hear about us?"],
                "value": "Company website",
                "ats": ["greenhouse"],
            },
            {
                "question": "Preferred interview format?",
                "value": "Video",
                "domains": ["jobs.example.com"],
            },
        ]
    },
    "eeo_voluntary": {},
}


def value_for(**kwargs):
    spec = FieldSpec(selector="#x", tag="input", type="text", **kwargs)
    resolved = field_value_for(spec, profile=PROFILE, job={"title": "Software Engineer"})
    return resolved.value if resolved else None


def test_autocomplete_tokens_resolve_profile_facts():
    assert value_for(autocomplete="given-name") == "Test"
    assert value_for(autocomplete="family-name") == "Candidate"
    assert value_for(autocomplete="email") == "candidate@example.com"
    assert value_for(autocomplete="tel") == "555-0100"
    assert value_for(autocomplete="postal-code") == "78701"


def test_salary_history_authorization_does_not_receive_salary_expectation():
    assert (
        value_for(
            label="Salary history authorization",
            options=("Yes", "No"),
        )
        is None
    )


def test_compensation_answers_distinguish_general_expectation_and_hourly_floor():
    assert value_for(label="Compensation expectation") == "Open to market-competitive compensation"
    assert value_for(label="Minimum hourly rate") == "30"


def test_sponsorship_and_work_authorization_polarity():
    assert value_for(label="Will you now or in the future require sponsorship?", options=("Yes", "No")) == "No"
    assert value_for(label="Are you legally authorized to work?", options=("Yes", "No")) == "Yes"


def test_cached_age_availability_relocation_and_travel_answers():
    assert value_for(label="Are you at least 18 years old?", options=("Yes", "No")) == "Yes"
    assert value_for(label="Are you available for the full internship period?", options=("Yes", "No")) == "Yes"
    assert value_for(label="What dates are you available?") == "May 24 through August 13, 2027"
    assert value_for(label="Preferred work locations") == "Austin, Remote US"
    assert value_for(label="Are you willing to relocate?", options=("Yes", "No")) == "No"
    assert value_for(label="Are you willing to travel?", options=("Yes", "No")) == "Yes"


def test_cached_work_permit_status():
    assert value_for(label="Current work permit type") == "US citizen"


def test_resume_backed_education_and_current_employment_answers():
    assert value_for(label="Current employer") == "Example Labs"
    assert value_for(label="Current job title") == "Product Analytics Intern"
    assert value_for(label="University name") == "Example University"
    assert value_for(label="Degree program") == "Bachelor of Business Administration"
    assert value_for(label="Expected graduation date") == "May 2028"
    assert value_for(label="Are you currently enrolled?", options=("Yes", "No")) == "Yes"


def test_exact_custom_answer_cache_honors_alias_and_ats_scope():
    spec = FieldSpec(
        selector="#source",
        tag="select",
        type="select",
        label="How did you hear about us? (Required)",
        required=True,
        options=("Company website", "Employee referral"),
        ats="greenhouse",
    )

    resolved = field_value_for(
        spec,
        profile=PROFILE,
        job={"url": "https://boards.greenhouse.io/example/jobs/123"},
    )

    assert resolved is not None
    assert resolved.value == "Company website"
    assert resolved.source == "profile_cache:autofill.custom_answers.0"
    assert resolved.confidence == 0.99


def test_custom_answer_cache_rejects_wrong_scope_fuzzy_match_and_conflicts():
    scoped = FieldSpec(
        selector="#format",
        tag="input",
        type="text",
        label="Preferred interview format?",
    )
    fuzzy = FieldSpec(
        selector="#source",
        tag="input",
        type="text",
        label="Briefly explain how you heard about us?",
        ats="greenhouse",
    )

    assert (
        field_value_for(
            scoped,
            profile=PROFILE,
            job={"url": "https://other.example.org/apply"},
        )
        is None
    )
    assert field_value_for(fuzzy, profile=PROFILE, job={}) is None

    conflicting = {
        **PROFILE,
        "autofill": {
            "custom_answers": [
                {"question": "Preferred interview format?", "value": "Video"},
                {"question": "Preferred interview format?", "value": "Phone"},
            ]
        },
    }
    assert field_value_for(scoped, profile=conflicting, job={}) is None


def test_custom_answer_cache_and_model_refuse_reserved_attestations():
    profile = {
        **PROFILE,
        "autofill": {
            "custom_answers": [
                {
                    "question": "Type your full name as your signature",
                    "value": "Test Candidate",
                }
            ]
        },
    }
    spec = FieldSpec(
        selector="#signature",
        tag="input",
        type="text",
        label="Type your full name as your signature",
        required=True,
    )

    assert field_value_for(spec, profile=profile, job={}) is None
    assert needs_llm_fallback(spec) is False


def test_work_authorization_strings_are_not_treated_as_truthy():
    profile = {
        **PROFILE,
        "work_authorization": {
            "legally_authorized_to_work": "Yes",
            "require_sponsorship": "No",
        },
    }
    auth = FieldSpec(
        selector="#auth",
        tag="select",
        type="select",
        label="Are you legally authorized to work?",
        options=("Yes", "No"),
    )
    sponsor = FieldSpec(
        selector="#sponsor",
        tag="select",
        type="select",
        label="Will you now or in the future require sponsorship?",
        options=("Yes", "No"),
    )

    assert field_value_for(auth, profile=profile, job={"title": "Software Engineer"}).value == "Yes"
    assert field_value_for(sponsor, profile=profile, job={"title": "Software Engineer"}).value == "No"


def test_unconfirmed_work_authorization_abstains():
    profile = {**PROFILE, "work_authorization": {}}
    auth = FieldSpec(
        selector="#auth",
        tag="select",
        type="select",
        label="Are you legally authorized to work?",
        options=("Yes", "No"),
    )

    assert field_value_for(auth, profile=profile, job={"title": "Software Engineer"}) is None


def test_missing_start_date_and_terms_consent_abstain():
    profile = {
        **PROFILE,
        "availability": {},
        "screening": {},
    }
    start = FieldSpec(
        selector="#start",
        tag="input",
        type="text",
        label="Earliest start date",
    )
    consent = FieldSpec(
        selector="#terms",
        tag="input",
        type="checkbox",
        label="I accept the application terms",
    )

    assert field_value_for(start, profile=profile, job={"title": "Engineer"}) is None
    assert field_value_for(consent, profile=profile, job={"title": "Engineer"}) is None


def test_select_options_must_match_real_options():
    spec = FieldSpec(
        selector="#country",
        tag="select",
        type="select",
        label="Country",
        options=("Canada", "Mexico"),
    )

    assert field_value_for(spec, profile=PROFILE, job={"title": "Software Engineer"}) is None


def test_required_ambiguous_field_uses_validated_codex_fallback(tmp_path):
    spec = FieldSpec(
        selector="#question",
        tag="select",
        type="select",
        label="Choose one",
        required=True,
        options=("Yes", "No"),
    )
    resolver = CodexResolver(model="unused", worker_dir=tmp_path)
    key = resolver._cache_key(spec=spec, profile=PROFILE, job={"title": "Software Engineer"})
    resolver._store_cache(
        {},
        key,
        {
            "value": "Maybe",
            "confidence": 0.9,
            "abstain": False,
            "reason": "test",
            "support_fact_ids": ["work_authorization.legally_authorized_to_work"],
        },
    )

    assert needs_llm_fallback(spec)
    assert resolver.resolve_field(spec, profile=PROFILE, job={"title": "Software Engineer"}) is None

    resolver._store_cache(
        {},
        key,
        {
            "value": "Yes",
            "confidence": 0.9,
            "abstain": False,
            "reason": "test",
            "support_fact_ids": ["work_authorization.legally_authorized_to_work"],
        },
    )
    resolved = resolver.resolve_field(spec, profile=PROFILE, job={"title": "Software Engineer"})

    assert resolved is not None
    assert validate_resolved_value(spec, resolved).value == "Yes"


def test_codex_resolver_uses_current_exec_flags(monkeypatch, tmp_path):
    spec = FieldSpec(
        selector="#question",
        tag="select",
        type="select",
        label="Choose one",
        required=True,
        options=("Yes", "No"),
    )

    def fake_run(cmd, input, capture_output, text, timeout):
        assert "--ask-for-approval" not in cmd
        assert 'approval_policy="never"' in cmd
        assert 'web_search="disabled"' in cmd
        assert 'model_reasoning_effort="medium"' in cmd
        assert 'service_tier="default"' in cmd
        assert "web_search=false" not in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
        assert cmd[cmd.index("--sandbox") + 1] == "read-only"
        assert "do-not-send" not in input
        assert timeout is None
        output_path = tmp_path / "unused.json"
        if "--output-last-message" in cmd:
            output_path = tmp_path / cmd[cmd.index("--output-last-message") + 1].split("/")[-1]
        output_path.write_text(
            json.dumps(
                {
                    "value": "Yes",
                    "confidence": 0.9,
                    "abstain": False,
                    "reason": "test",
                    "support_fact_ids": ["work_authorization.legally_authorized_to_work"],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("applypilot.apply.field_resolver.subprocess.run", fake_run)

    resolved = CodexResolver(model="gpt-5.5", worker_dir=tmp_path).resolve_field(
        spec,
        profile=PROFILE,
        job={"title": "Software Engineer"},
    )

    assert resolved is not None
    assert resolved.value == "Yes"


def test_codex_resolver_batches_required_fields_into_one_call(monkeypatch, tmp_path):
    specs = [
        FieldSpec(
            selector=f"#question-{index}",
            tag="input",
            type="text",
            label=f"Required custom question {index}",
            required=True,
        )
        for index in range(8)
    ]
    calls = []

    def fake_run(cmd, input, capture_output, text, timeout):
        calls.append(cmd)
        assert timeout is None
        assert 'approval_policy="never"' in cmd
        assert 'web_search="disabled"' in cmd
        assert "web_search=false" not in cmd
        assert cmd[cmd.index("--sandbox") + 1] == "read-only"
        prompt = json.loads(input)
        assert len(prompt["fields"]) == 8
        output_path = tmp_path / cmd[cmd.index("--output-last-message") + 1].split("/")[-1]
        output_path.write_text(
            json.dumps(
                {
                    "answers": [
                        {
                            "field_id": item["field_id"],
                            "value": "Test Candidate",
                            "confidence": 0.9,
                            "abstain": False,
                            "reason": "supported",
                            "support_fact_ids": ["personal.full_name"],
                        }
                        for index, item in enumerate(prompt["fields"])
                    ]
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("applypilot.apply.field_resolver.subprocess.run", fake_run)
    resolver = CodexResolver(model="gpt-5.5", worker_dir=tmp_path, max_calls=1)

    resolved = resolver.resolve_fields(specs, profile=PROFILE, job={"title": "Engineer"})

    assert len(calls) == 1
    assert len(resolved) == 8
    assert resolved["#question-7"].value == "Test Candidate"
    assert resolver.calls == 1


def test_model_field_profile_uses_only_confirmed_ledger_facts():
    profile = {
        **PROFILE,
        "personal": {
            **PROFILE["personal"],
            "phone": "unknown",
            "password": "do-not-send",
        },
        "compensation": {"salary_expectation": "120000"},
    }
    ledger = build_fact_ledger(
        profile,
        resume_text="Test Candidate",
        corrections=(
            FactCorrection(
                fact_id="profile.compensation.salary_expectation",
                match="120000",
                state=FactState.REJECTED,
                reason="applicant rejected stale salary expectation",
            ),
        ),
    )

    model_profile = model_field_profile_from_ledger(ledger)
    serialized = json.dumps(model_profile)

    assert "unknown" not in serialized
    assert "120000" not in serialized
    assert "do-not-send" not in serialized
    assert model_profile["personal"]["email"] == PROFILE["personal"]["email"]
    assert model_profile["eligibility"]["is_at_least_18"] == "true"
    assert model_profile["education"]["expected_graduation_date"] == "May 2028"


def test_codex_resolver_rejects_value_not_derived_from_cited_fact(monkeypatch, tmp_path):
    spec = FieldSpec(
        selector="#question",
        tag="input",
        type="text",
        label="Required custom question",
        required=True,
    )

    def fake_run(cmd, input, capture_output, text, timeout):
        prompt = json.loads(input)
        output_path = tmp_path / cmd[cmd.index("--output-last-message") + 1].split("/")[-1]
        output_path.write_text(
            json.dumps(
                {
                    "answers": [
                        {
                            "field_id": prompt["fields"][0]["field_id"],
                            "value": "Fabricated answer",
                            "confidence": 0.99,
                            "abstain": False,
                            "reason": "unsupported",
                            "support_fact_ids": ["personal.full_name"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("applypilot.apply.field_resolver.subprocess.run", fake_run)
    resolver = CodexResolver(model="gpt-5.5", worker_dir=tmp_path, max_calls=1)

    assert resolver.resolve_fields([spec], profile=PROFILE, job={"title": "Engineer"}) == {}


def test_codex_resolver_zero_call_budget_fails_closed(monkeypatch, tmp_path):
    spec = FieldSpec(
        selector="#question",
        tag="input",
        type="text",
        label="Required custom question",
        required=True,
    )

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("model process must not run")

    monkeypatch.setattr("applypilot.apply.field_resolver.subprocess.run", unexpected_run)

    assert (
        CodexResolver(model="gpt-5.5", worker_dir=tmp_path, max_calls=0).resolve_fields(
            [spec],
            profile=PROFILE,
            job={"title": "Engineer"},
        )
        == {}
    )

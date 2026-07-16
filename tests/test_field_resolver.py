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
    },
    "work_authorization": {
        "legally_authorized_to_work": True,
        "require_sponsorship": False,
    },
    "compensation": {"salary_expectation": "120000"},
    "availability": {"earliest_start_date": "Immediately"},
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


def test_sponsorship_and_work_authorization_polarity():
    assert value_for(label="Will you now or in the future require sponsorship?", options=("Yes", "No")) == "No"
    assert value_for(label="Are you legally authorized to work?", options=("Yes", "No")) == "Yes"


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
        "personal": {**PROFILE["personal"], "phone": "unknown"},
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
    assert model_profile["personal"]["email"] == PROFILE["personal"]["email"]


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

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from applypilot import config
from applypilot.autonomy.batch import AutonomousBatch, BatchDependencies, _mapping_digest
from applypilot.autonomy.chatgpt_web import (
    ChatGPTWebClient,
    ChatGPTWebConfig,
    ChatGPTContractError,
    material_packet_from_payload,
    parse_chatgpt_json,
    temporary_chat_is_active,
    validate_material_provenance,
)
from applypilot.autonomy import direct_ats as autonomy_direct_ats
from applypilot.autonomy.direct_ats import DirectATSDiscovery
from applypilot.autonomy.context import (
    CompactContextPack,
    build_context_pack,
    build_discovery_prompt,
    build_material_prompt,
    candidate_profile_from_data,
)
from applypilot.autonomy.facts import (
    FactCorrection,
    FactState,
    build_fact_ledger,
    fact_ledger_from_dict,
    load_corrections,
    require_confirmed_facts,
    validate_artifact_against_ledger,
)
from applypilot.autonomy.form_review import ReadOnlyFormReviewer
from applypilot.autonomy.form_handoff import validate_form_review_response
from applypilot.autonomy.first_party import (
    FetchResponse,
    FirstPartyVerifier,
    TrustedFirstPartySource,
    configured_trusted_sources,
)
from applypilot.autonomy import handoff as autonomy_handoff
from applypilot.autonomy import runner as autonomy_runner
from applypilot.autonomy.handoff import (
    ArtifactChatGPTClient,
    ChatGPTArtifactPending,
    RunBindings,
    import_response_artifact,
)
from applypilot.autonomy.models import (
    ApplicantClaim,
    AuthorizationGrant,
    CandidateProfile,
    DateWindow,
    Decision,
    FreshnessEvidence,
    MaterialPacket,
    MaterialParagraph,
    RoleCandidate,
)
from applypilot.autonomy.policy import (
    FunnelBudget,
    SourceAttempt,
    SourcePolicy,
    RunPolicy,
    apply_ephemeral_overrides,
    assert_no_recursive_model_command,
    authorize_source,
    eligibility_gate,
    freshness_gate,
    require_authorization,
)
from applypilot.autonomy.telemetry import BudgetExceeded, UsageLedger
from applypilot.autonomy.runner import (
    advance_artifact_run,
    record_run_heartbeat,
    require_approved_fact_digest,
    run_status_snapshot,
)
from applypilot.cli import app


PROFILE = {
    "personal": {
        "full_name": "Test Candidate",
        "email": "candidate@example.com",
        "phone": "555-0100",
        "address": "123 Main Street",
        "city": "Austin",
        "province_state": "TX",
        "country": "USA",
        "password": "secret-sentinel",
    },
    "experience": {
        "education_level": "BBA candidate, expected May 2028",
        "current_title": "Student product analyst",
        "target_role": "AI product, data, and analytics internship",
    },
    "work_authorization": {
        "legally_authorized_to_work": True,
        "require_sponsorship": False,
    },
    "availability": {
        "earliest_start_date": "2027-05-15",
    },
    "skills_boundary": {
        "programming_languages": ["Python", "SQL", "JavaScript"],
        "analytics": ["SQLite", "Tableau", "GA4"],
    },
    "resume_facts": {
        "preserved_companies": ["Example Labs"],
        "preserved_projects": ["Atlas Engine"],
        "real_metrics": ["$50K in collected revenue"],
        "preserved_school": "The University of Texas at Austin",
    },
}
CLI_RUNNER = CliRunner()


def test_legacy_cdp_probe_requires_explicit_opt_in():
    with pytest.raises(PermissionError, match="legacy CDP transport is disabled by default"):
        autonomy_runner.probe_chatgpt_cdp(cdp_port=9222)


def test_legacy_cdp_run_requires_explicit_opt_in(tmp_path):
    with pytest.raises(PermissionError, match="legacy CDP transport is disabled by default"):
        autonomy_runner.run_with_cdp(
            query="product analyst intern",
            cdp_port=9222,
            output_dir=tmp_path,
            approved_fact_digest="unused-before-opt-in",
        )


def test_legacy_cdp_cli_requires_visible_acknowledgement():
    result = CLI_RUNNER.invoke(app, ["autonomy", "probe-chatgpt"])

    assert result.exit_code == 1
    assert "--allow-legacy-cdp" in result.output


def role(**overrides):
    values = {
        "company": "Example",
        "title": "Product Analyst Intern",
        "official_url": "https://job-boards.greenhouse.io/example/jobs/123",
        "location": "Remote, United States",
        "description": "Internship using Python and SQL. 0-2 years accepted.",
        "required_experience_min": 0,
        "required_experience_max": 2,
    }
    values.update(overrides)
    return RoleCandidate(**values)


def fresh(candidate, **overrides):
    values = {
        "official_url": candidate.official_url,
        "fetched_at": datetime.now(timezone.utc),
        "first_party": True,
        "resolved": True,
        "open_state": True,
        "posted_date": date(2026, 7, 1),
        "status_code": 200,
        "title": candidate.title,
        "description": candidate.description,
        "evidence": ("official ATS response",),
    }
    values.update(overrides)
    return FreshnessEvidence(**values)


def test_source_policy_requires_recorded_chatgpt_failure_for_fallback():
    policy = SourcePolicy()

    assert authorize_source("chatgpt_web", policy=policy).decision is Decision.ACCEPT
    assert authorize_source("linkedin", policy=policy).decision is Decision.REJECT
    assert authorize_source("direct_ats", policy=policy).decision is Decision.REJECT
    assert (
        authorize_source(
            "direct_ats",
            policy=policy,
            attempts=(SourceAttempt("chatgpt_web", "failed", "unavailable"),),
        ).decision
        is Decision.ACCEPT
    )


def test_candidate_id_ignores_tracking_but_keeps_job_identity_parameters():
    base = role(official_url="https://jobs.example.com/roles/123")
    tracked = role(official_url="https://jobs.example.com/roles/123?utm_source=chatgpt")
    first = role(official_url="https://jobs.example.com/apply?jobId=111")
    second = role(official_url="https://jobs.example.com/apply?jobId=222")

    assert base.candidate_id == tracked.candidate_id
    assert first.candidate_id != second.candidate_id


def test_eligibility_rejects_senior_experience_and_conflict_before_materials():
    profile = CandidateProfile(
        graduation_month=5,
        graduation_year=2028,
        commitments=(DateWindow(date(2027, 5, 1), date(2027, 8, 31), "summer commitment"),),
    )

    assert eligibility_gate(role(title="Senior Product Manager"), profile).decision is Decision.REJECT
    assert (
        eligibility_gate(role(required_experience_min=3), profile).decision is Decision.REJECT
    )
    assert (
        eligibility_gate(
            role(start_window=DateWindow(date(2027, 6, 1), date(2027, 8, 1))),
            profile,
        ).decision
        is Decision.REJECT
    )
    assert eligibility_gate(role(), profile).decision is Decision.ACCEPT
    assert (
        eligibility_gate(role(location="Dallas, TX, USA"), profile).decision
        is Decision.ACCEPT
    )
    assert (
        eligibility_gate(
            role(description="Requires 3+ years of relevant experience."),
            profile,
        ).decision
        is Decision.REJECT
    )


def test_eligibility_holds_ambiguous_level_for_review():
    candidate = role(
        title="Product Owner",
        description="Own the product roadmap.",
        required_experience_min=None,
        required_experience_max=None,
    )
    assert eligibility_gate(candidate, CandidateProfile()).decision is Decision.REVIEW


def test_freshness_requires_first_party_open_and_plausible_dates():
    candidate = role()
    assert freshness_gate(fresh(candidate), today=date(2026, 7, 10)).decision is Decision.ACCEPT
    assert (
        freshness_gate(
            fresh(candidate, resolved=False, open_state=False, status_code=404),
            today=date(2026, 7, 10),
        ).decision
        is Decision.REJECT
    )
    assert (
        freshness_gate(
            fresh(candidate, posted_date=date(2025, 12, 1), updated_date=date(2025, 12, 1)),
            today=date(2026, 7, 10),
        ).decision
        is Decision.REVIEW
    )


def test_first_party_requires_exact_employer_or_ats_tenant_identity():
    class StaticTransport:
        @staticmethod
        def get(url, **_kwargs):
            return FetchResponse(200, url, "<html><body>Product Analyst Intern</body></html>")

    ledger = UsageLedger(run_id="verify-domain", budget=FunnelBudget())
    verifier = FirstPartyVerifier(
        ledger=ledger,
        transport=StaticTransport(),
        trusted_sources=(
            TrustedFirstPartySource(
                company="Example Labs",
                host="careers.examplelabs.com",
                path_prefix="/jobs",
                source_kind="employer_careers",
            ),
        ),
    )
    matching = role(
        company="Example Labs",
        official_url="https://careers.examplelabs.com/jobs/123",
    )
    deceptive = role(
        company="Example Labs",
        official_url="https://examplelabs.jobs-portal.com/jobs/123",
    )
    typosquat = role(
        company="OpenAI",
        official_url="https://openai-careers.example/jobs/123",
    )
    wrong_greenhouse_tenant = role(
        company="Company A",
        official_url="https://job-boards.greenhouse.io/companyb/jobs/123",
    )
    misleading_greenhouse_tenant = role(
        company="OpenAI",
        official_url="https://job-boards.greenhouse.io/openaicareers/jobs/123",
    )
    hosted_ats = role(
        company="Capital One",
        official_url=(
            "https://capitalone.wd12.myworkdayjobs.com/en-US/Capital_One/"
            "job/Product-Analyst-Intern_R123"
        ),
    )

    assert verifier.verify(matching).first_party is True
    assert verifier.verify(hosted_ats).first_party is True
    assert verifier.verify(deceptive).first_party is False
    assert verifier.verify(typosquat).first_party is False
    assert verifier.verify(wrong_greenhouse_tenant).first_party is False
    assert verifier.verify(misleading_greenhouse_tenant).first_party is False


def test_configured_first_party_sources_exclude_account_backed_recruiters():
    sources = configured_trusted_sources()

    assert sources
    assert all(source.host != "app.joinrunway.io" for source in sources)
    assert all(source.source_kind != "account_backed_recruiter" for source in sources)


def test_first_party_verifier_rejects_private_targets_and_generic_homepages():
    class CountingTransport:
        def __init__(self):
            self.calls = 0

        def get(self, url, **_kwargs):
            self.calls += 1
            return FetchResponse(200, url, "<html><body>Company homepage</body></html>")

    transport = CountingTransport()
    ledger = UsageLedger(run_id="ssrf", budget=FunnelBudget())
    verifier = FirstPartyVerifier(ledger=ledger, transport=transport)
    private = role(company="Local", official_url="http://127.0.0.1:8080/jobs/1")
    homepage = role(company="Example Labs", official_url="https://examplelabs.com/")
    greenhouse_confusion = role(
        company="Example",
        official_url="https://boards.greenhouse.io.evil.example/example/jobs/123",
    )

    assert verifier.verify(private).resolved is False
    assert transport.calls == 0
    homepage_evidence = verifier.verify(homepage)
    assert homepage_evidence.resolved is False
    assert homepage_evidence.provider_error == "untrusted_company_host"
    assert verifier.verify(greenhouse_confusion).first_party is False
    assert transport.calls == 0


def test_ephemeral_overrides_never_mutate_persistent_config():
    base = {"queries": [{"query": "product analyst"}], "defaults": {"limit": 10}}
    original = deepcopy(base)

    merged, digest = apply_ephemeral_overrides(
        base,
        {"queries": [{"query": "product analyst intern"}], "defaults": {"limit": 5}},
    )

    assert base == original
    assert merged["queries"][0]["query"] == "product analyst intern"
    assert merged["defaults"]["limit"] == 5
    assert len(digest) == 64


def test_recursive_model_processes_are_forbidden():
    with pytest.raises(RuntimeError, match="nested model process"):
        assert_no_recursive_model_command(["codex", "exec", "-"])
    with pytest.raises(RuntimeError, match="nested model process"):
        assert_no_recursive_model_command(["sh", "-c", "claude -p prompt"])
    assert_no_recursive_model_command(["python", "-m", "applypilot"])


def test_context_pack_is_compact_relevant_and_contact_free():
    resume = """
    Test Candidate | candidate@example.com | 555-0100
    • Built a Python and SQLite role-analysis tool for product research.
    • Performed unrelated orchestral arranging and rehearsal planning.
    """

    pack = build_context_pack(
        PROFILE,
        resume_text=resume,
        job_text="Product analytics with Python SQL and experimentation",
        max_chars=2_500,
    )
    serialized = str(pack.to_dict())

    assert pack.serialized_chars <= 2_500
    assert "candidate@example.com" not in serialized
    assert "555-0100" not in serialized
    assert "secret-sentinel" not in serialized
    assert "Python and SQLite" in serialized
    derived = candidate_profile_from_data(PROFILE)
    assert derived.graduation_month == 5
    assert derived.graduation_year == 2028


def test_context_pack_default_keeps_broad_confirmed_background():
    resume = "\n".join(
        f"Built verified project capability number {index} using Python and analytics."
        for index in range(1, 61)
    )
    ledger = build_fact_ledger(PROFILE, resume_text=resume)

    pack = build_context_pack(
        PROFILE,
        resume_text=resume,
        job_text="entry level product roles",
        fact_ledger=ledger,
    )

    assert len(pack.evidence) > 30
    assert len(pack.evidence) <= 72
    assert pack.serialized_chars <= 24_000


def test_context_pack_fails_closed_when_confirmed_profile_exceeds_budget():
    oversized = deepcopy(PROFILE)
    oversized["skills_boundary"] = {
        "tools": [f"verified-capability-{index}-" + ("x" * 80) for index in range(20)]
    }
    ledger = build_fact_ledger(oversized, resume_text="")

    with pytest.raises(ValueError, match="profile exceeds context character budget"):
        build_context_pack(
            oversized,
            max_chars=200,
            fact_ledger=ledger,
        )


def test_discovery_prompt_includes_full_evidence_and_deep_reasoning_contract():
    pack = CompactContextPack(
        version="test-context",
        profile={"target_role": "Product analyst"},
        evidence=(
            {"id": "F01", "fact": "Built a verified analytics system."},
            {"id": "F02", "fact": "Led a verified product research project."},
        ),
        digest="context-digest",
        serialized_chars=100,
    )

    payload = json.loads(build_discovery_prompt(pack, query="early career roles", limit=5))

    assert payload["candidate_context"] == pack.to_dict()
    assert any("as much internal analysis" in rule for rule in payload["reasoning_guidance"])
    assert payload["response_rule"].startswith("Return exactly one JSON object")


def test_material_prompt_prioritizes_relevant_facts_without_dropping_context():
    pack = CompactContextPack(
        version="test-context",
        profile={"target_role": "Product analyst"},
        evidence=(
            {"id": "F01", "fact": "Coordinated community music rehearsals."},
            {"id": "F02", "fact": "Built Python and SQL product analytics tooling."},
            {"id": "F03", "fact": "Wrote a verified market research report."},
        ),
        digest="context-digest",
        serialized_chars=200,
    )

    payload = json.loads(
        build_material_prompt(
            pack,
            role(description="Python SQL product analytics"),
            verified_job_text="Build product analytics workflows using Python and SQL.",
        )
    )

    ranked = payload["context"]["evidence"]
    assert ranked[0]["id"] == "F02"
    assert {item["id"] for item in ranked} == {"F01", "F02", "F03"}
    assert any("Think deeply" in rule for rule in payload["reasoning_guidance"])


def test_fact_ledger_context_excludes_identity_and_eeo_facts():
    profile = {
        **PROFILE,
        "eeo_voluntary": {
            "gender": "Decline to self-identify",
            "race_ethnicity": "Sensitive demographic response",
            "veteran_status": "Sensitive veteran response",
        },
    }
    ledger = build_fact_ledger(
        profile,
        resume_text="Test Candidate\nBuilt Python product analytics tools.",
    )

    pack = build_context_pack(
        profile,
        resume_text="Test Candidate\nBuilt Python product analytics tools.",
        job_text="Test Candidate Python product analytics",
        fact_ledger=ledger,
    )
    serialized = json.dumps(pack.to_dict())

    assert "Test Candidate" not in serialized
    assert "Sensitive demographic" not in serialized
    assert "Sensitive veteran" not in serialized
    assert "Python product analytics" in serialized


def test_context_uses_confirmed_allowlist_and_redacts_identity_address_and_salary():
    profile = deepcopy(PROFILE)
    profile["experience"]["current_title"] = "Test Candidate — Product Analyst"
    profile["experience"]["target_role"] = "unknown"
    profile["preferences"] = {
        "locations": ["Austin"],
        "salary_min": 90_000,
        "target_companies": ["Example Labs"],
    }
    profile["eeo_voluntary"] = {"gender": "Sensitive demographic response"}
    resume = """Test Candidate — Product Analyst
123 Main Street
Salary expectation: $90,000
Built Python and SQL product analytics tools.
"""
    ledger = build_fact_ledger(profile, resume_text=resume)

    pack = build_context_pack(
        profile,
        resume_text=resume,
        job_text="Python SQL product analytics in Austin",
        fact_ledger=ledger,
    )
    serialized = json.dumps(pack.to_dict())

    assert "Test Candidate" not in serialized
    assert "123 Main Street" not in serialized
    assert "90,000" not in serialized
    assert "salary_min" not in serialized
    assert "Sensitive demographic" not in serialized
    assert "unknown" not in serialized
    assert "Python and SQL product analytics" in serialized
    assert "Austin" in serialized

    discovery_prompt = build_discovery_prompt(
        pack,
        query="Python SQL product analytics in Austin",
        limit=5,
    )
    assert "Test Candidate" not in discovery_prompt
    assert "123 Main Street" not in discovery_prompt
    assert "90,000" not in discovery_prompt
    assert "Sensitive demographic" not in discovery_prompt
    assert "unknown" not in discovery_prompt


def test_fact_ledger_prevents_rejected_experience_and_placeholders_from_returning():
    resume = """Test Candidate | [email] | [phone]
SEO & Digital Marketing Analytics Intern
Built Python product analytics tools.
"""
    ledger = build_fact_ledger(
        PROFILE,
        resume_text=resume,
        corrections=(
            FactCorrection(
                match="SEO & Digital Marketing Analytics Intern",
                state=FactState.REJECTED,
                reason="user explicitly rejected this experience",
            ),
        ),
    )

    assert any("SEO & Digital" in record.value for record in ledger.rejected())
    blockers = validate_artifact_against_ledger(
        "Candidate | [email]\nSEO & Digital Marketing Analytics Intern",
        ledger,
    )
    assert any(blocker.startswith("artifact_contains_placeholders") for blocker in blockers)
    assert any(blocker.startswith("artifact_contains_rejected_fact") for blocker in blockers)
    pack = build_context_pack(
        PROFILE,
        resume_text=resume,
        job_text="product analytics",
        fact_ledger=ledger,
    )
    assert "SEO & Digital Marketing" not in str(pack.to_dict())


def test_rejected_correction_remains_a_tombstone_when_source_fact_is_absent():
    ledger = build_fact_ledger(
        PROFILE,
        resume_text="Built Python product analytics tools.",
        corrections=(
            FactCorrection(
                match="Withdrawn Employer Experience",
                state=FactState.REJECTED,
                reason="applicant rejected this claim",
            ),
        ),
    )

    assert any(
        record.value == "Withdrawn Employer Experience" for record in ledger.rejected()
    )
    assert validate_artifact_against_ledger(
        "I worked in Withdrawn Employer Experience.",
        ledger,
    ) == ["artifact_contains_rejected_fact:correction.rejected.0001"]


def test_required_fact_gate_preserves_unknown_state():
    ledger = build_fact_ledger(
        {"personal": {"phone": "", "email": "candidate@example.com"}},
        resume_text="Candidate | [phone]",
    )

    blockers = require_confirmed_facts(
        ledger,
        ["profile.personal.phone", "profile.personal.email", "profile.work_authorization"],
    )
    assert "unknown:profile.personal.phone" in blockers
    assert "missing:profile.work_authorization" in blockers
    assert not any("personal.email" in blocker for blocker in blockers)


def test_fact_id_correction_and_review_digest_are_exact():
    ledger = build_fact_ledger(
        {"personal": {"phone": "555-0100"}},
        resume_text="Candidate",
        corrections=(
            FactCorrection(
                fact_id="profile.personal.phone",
                match="",
                state=FactState.UNKNOWN,
                reason="needs review",
            ),
        ),
    )

    phone = next(record for record in ledger.records if record.fact_id == "profile.personal.phone")
    assert phone.state is FactState.UNKNOWN
    require_approved_fact_digest(ledger.digest, ledger.digest)
    with pytest.raises(PermissionError, match="approved fact digest"):
        require_approved_fact_digest(ledger.digest, "wrong")
    with pytest.raises(FileNotFoundError, match="corrections file not found"):
        load_corrections(Path("/definitely/missing/corrections.json"))


def test_fact_ledger_digest_binds_top_level_and_per_record_source_hashes():
    ledger = build_fact_ledger(
        PROFILE,
        resume_text="Built Python product analytics tools.",
    )
    payload = ledger.to_dict()
    payload["profile_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="record source digest mismatch"):
        fact_ledger_from_dict(payload)

    rebound = ledger.to_dict()
    rebound["profile_sha256"] = "1" * 64
    for record in rebound["records"]:
        if record["source"] == "profile.json":
            record["source_sha256"] = "1" * 64
    with pytest.raises(ValueError, match="content digest mismatch"):
        fact_ledger_from_dict(rebound)


def test_chatgpt_contract_accepts_only_bare_versioned_json():
    raw = (
        '{"schema_version":"applypilot.chatgpt_web.v1",'
        '"kind":"role_candidates","items":[]}'
    )
    assert parse_chatgpt_json(raw, expected_kind="role_candidates")["items"] == []
    with pytest.raises(ChatGPTContractError):
        parse_chatgpt_json(f"```json\n{raw}\n```", expected_kind="role_candidates")
    with pytest.raises(ChatGPTContractError):
        parse_chatgpt_json(raw, expected_kind="material_packet")
    with pytest.raises(ChatGPTContractError, match="unexpected role_candidates fields"):
        parse_chatgpt_json(
            '{"schema_version":"applypilot.chatgpt_web.v1",'
            '"kind":"role_candidates","items":[],"reasoning":"hidden notes"}',
            expected_kind="role_candidates",
        )


def test_temporary_chat_requires_positive_ui_indicator():
    class Locator:
        def __init__(self, count):
            self._count = count

        def count(self):
            return self._count

    class Page:
        def __init__(self, *, off=0, label=0):
            self.off = off
            self.label = label

        def get_by_role(self, *_args, **_kwargs):
            return Locator(self.off)

        def get_by_text(self, *_args, **_kwargs):
            return Locator(self.label)

    assert temporary_chat_is_active(Page(off=1)) is True
    assert temporary_chat_is_active(Page(label=1)) is True
    assert temporary_chat_is_active(Page()) is False


def test_chatgpt_response_uses_dom_text_content_not_rendered_inner_text():
    raw = (
        '{"schema_version":"applypilot.chatgpt_web.v1",'
        '"kind":"role_candidates","items":[]}'
    )

    class Locator:
        def __init__(self, *, count=0, text=""):
            self._count = count
            self._text = text

        def count(self):
            return self._count

        def is_visible(self):
            return False

        def nth(self, _index):
            return self

        def text_content(self, **_kwargs):
            return self._text

        def inner_text(self, **_kwargs):
            raise AssertionError("rendered innerText can corrupt JSON URLs")

    class Page:
        def __init__(self):
            self.assistant = Locator(count=1, text=raw)

        def locator(self, _selector):
            return self.assistant

        def get_by_role(self, *_args, **_kwargs):
            return Locator()

        def wait_for_timeout(self, _timeout):
            raise AssertionError("completed response should not poll")

    client = ChatGPTWebClient(
        page=Page(),
        ledger=UsageLedger(run_id="text-content", budget=FunnelBudget()),
    )

    assert client._wait_for_assistant(0) == raw


def test_chatgpt_default_wait_has_no_generation_deadline():
    raw = (
        '{"schema_version":"applypilot.chatgpt_web.v1",'
        '"kind":"role_candidates","items":[]}'
    )

    class Locator:
        def __init__(self, page, *, assistant=False):
            self.page = page
            self.assistant = assistant

        def count(self):
            return 1 if self.assistant and self.page.polls >= 4 else 0

        def is_visible(self):
            return False

        def nth(self, _index):
            return self

        def text_content(self, **_kwargs):
            return raw

    class Page:
        def __init__(self):
            self.polls = 0

        def locator(self, _selector):
            return Locator(self, assistant=True)

        def get_by_role(self, *_args, **_kwargs):
            return Locator(self)

        def wait_for_timeout(self, _timeout):
            self.polls += 1

    page = Page()
    client = ChatGPTWebClient(
        page=page,
        ledger=UsageLedger(run_id="unbounded-wait", budget=FunnelBudget()),
        config=ChatGPTWebConfig(),
    )

    assert client.config.timeout_ms is None
    assert client._wait_for_assistant(0) == raw
    assert page.polls == 4


def test_material_packet_rejects_oversized_final_output():
    candidate = role()
    pack = build_context_pack(PROFILE, job_text=candidate.description)
    payload = {
        "candidate_id": candidate.candidate_id,
        "paragraphs": [
            {
                "text": "role " * 451,
                "evidence_ids": ["JOB"],
                "applicant_claims": [],
            }
        ],
        "verification_gaps": [],
    }

    with pytest.raises(ChatGPTContractError, match="word limit"):
        material_packet_from_payload(
            payload,
            pack=pack,
            candidate=candidate,
            verified_job_text="role",
        )


def test_chatgpt_discovery_rejects_aggregator_urls():
    class StubClient(ChatGPTWebClient):
        def ask_json(self, *_args, **_kwargs):
            return {
                "schema_version": "applypilot.chatgpt_web.v1",
                "kind": "role_candidates",
                "items": [
                    {
                        "company": "Example",
                        "title": "Product Analyst Intern",
                        "official_url": "https://www.linkedin.com/jobs/view/123",
                    }
                ],
            }

    pack = build_context_pack(PROFILE, job_text="product analyst")
    client = StubClient(
        page=None,
        ledger=UsageLedger(run_id="blocked-host", budget=FunnelBudget()),
    )

    with pytest.raises(ChatGPTContractError, match="disallowed discovery host"):
        client.find_roles(pack=pack, query="product analyst", limit=10)


def test_direct_ats_fallback_is_read_only_and_bounded(monkeypatch):
    monkeypatch.setattr(
        autonomy_direct_ats.config,
        "load_search_config",
        lambda: {
            "direct_ats_sources": [
                {"name": "Example", "ats": "greenhouse", "slug": "example"}
            ],
            "direct_ats_filter_titles": False,
        },
    )
    monkeypatch.setattr(
        autonomy_direct_ats,
        "_fetch_source_jobs",
        lambda _source: [
            {
                "title": "Product Analyst Intern",
                "url": "https://job-boards.greenhouse.io/example/jobs/123",
                "application_url": "https://job-boards.greenhouse.io/example/jobs/123",
                "location": "Remote",
                "description": "Internship",
            }
        ],
    )
    ledger = UsageLedger(run_id="direct-fallback", budget=FunnelBudget())

    candidates = DirectATSDiscovery(ledger=ledger).find_roles(
        pack=build_context_pack(PROFILE, job_text="product analyst"),
        query="product analyst",
        limit=1,
    )

    assert len(candidates) == 1
    assert candidates[0].source == "direct_ats"
    assert ledger.counts["external_calls"] == 1


def test_material_provenance_rejects_fabricated_numeric_claims():
    pack = build_context_pack(PROFILE, job_text="Python product analytics")
    candidate = role()
    year_evidence = next(item for item in pack.evidence if "50K" in item["fact"])
    supported_year = MaterialPacket(
        candidate_id=candidate.candidate_id,
        paragraphs=(
            MaterialParagraph(
                f"{year_evidence['fact']},",
                (year_evidence["id"],),
            ),
        ),
    )
    validate_material_provenance(
        supported_year,
        pack=pack,
        candidate=candidate,
        job_text=candidate.description,
    )
    packet = MaterialPacket(
        candidate_id=candidate.candidate_id,
        paragraphs=(
            MaterialParagraph(
                "I increased revenue by 987 percent while using Python.",
                (pack.evidence[0]["id"],),
            ),
        ),
    )

    with pytest.raises(ChatGPTContractError, match="unsupported numeric"):
        validate_material_provenance(
            packet,
            pack=pack,
            candidate=candidate,
            job_text=candidate.description,
        )

    fabricated = MaterialPacket(
        candidate_id=candidate.candidate_id,
        paragraphs=(
            MaterialParagraph(
                "I led an international launch at NASA.",
                (pack.evidence[0]["id"],),
            ),
        ),
    )
    with pytest.raises(ChatGPTContractError, match="unsupported factual terms"):
        validate_material_provenance(
            fabricated,
            pack=pack,
            candidate=candidate,
            job_text=candidate.description,
        )

    requirement_as_claim = MaterialPacket(
        candidate_id=candidate.candidate_id,
        paragraphs=(
            MaterialParagraph(
                "I have 5 years of Python experience.",
                ("JOB",),
            ),
        ),
    )
    with pytest.raises(ChatGPTContractError, match="unsupported numeric"):
        validate_material_provenance(
            requirement_as_claim,
            pack=pack,
            candidate=candidate,
            job_text="Applicants must have 5 years of Python experience.",
        )

    implied_requirement = MaterialPacket(
        candidate_id=candidate.candidate_id,
        paragraphs=(
            MaterialParagraph(
                "Can bring 5 years of Python experience.",
                ("JOB",),
            ),
        ),
    )
    with pytest.raises(ChatGPTContractError, match="unsupported numeric"):
        validate_material_provenance(
            implied_requirement,
            pack=pack,
            candidate=candidate,
            job_text="Applicants must have 5 years of Python experience.",
        )

    job_only_skill = MaterialPacket(
        candidate_id=candidate.candidate_id,
        paragraphs=(
            MaterialParagraph(
                "I have Kubernetes expertise.",
                ("JOB",),
            ),
        ),
    )
    with pytest.raises(ChatGPTContractError, match="no applicant evidence"):
        validate_material_provenance(
            job_only_skill,
            pack=pack,
            candidate=candidate,
            job_text="Applicants need Kubernetes expertise.",
        )

    target_company = role(company="Acme")
    job_only_employment = MaterialPacket(
        candidate_id=target_company.candidate_id,
        paragraphs=(MaterialParagraph("I have experience at acme.", ("JOB",)),),
    )
    with pytest.raises(ChatGPTContractError, match="no applicant evidence"):
        validate_material_provenance(
            job_only_employment,
            pack=pack,
            candidate=target_company,
            job_text="Join Acme as a product analyst.",
        )

    unrelated_fact = MaterialPacket(
        candidate_id=target_company.candidate_id,
        paragraphs=(
            MaterialParagraph(
                "I have experience at acme.",
                (pack.evidence[0]["id"], "JOB"),
                (
                    ApplicantClaim(
                        "I have experience at acme.",
                        (pack.evidence[0]["id"],),
                    ),
                ),
            ),
        ),
    )
    with pytest.raises(ChatGPTContractError, match="unsupported factual"):
        validate_material_provenance(
            unrelated_fact,
            pack=pack,
            candidate=target_company,
            job_text="Join Acme as a product analyst.",
        )

    ordinary_job_skill = MaterialPacket(
        candidate_id=candidate.candidate_id,
        paragraphs=(
            MaterialParagraph(
                "I have advanced forecasting skills.",
                (pack.evidence[0]["id"], "JOB"),
                (
                    ApplicantClaim(
                        "I have advanced forecasting skills.",
                        (pack.evidence[0]["id"],),
                    ),
                ),
            ),
        ),
    )
    with pytest.raises(ChatGPTContractError, match="unsupported factual"):
        validate_material_provenance(
            ordinary_job_skill,
            pack=pack,
            candidate=candidate,
            job_text="Candidates need advanced forecasting skills.",
        )

    for inverted in (
        "Advanced forecasting is among my skills.",
        "Advanced forecasting is part of my background.",
        "Through my work, advanced forecasting became a strength.",
    ):
        inverted_packet = MaterialPacket(
            candidate_id=candidate.candidate_id,
            paragraphs=(
                MaterialParagraph(
                    inverted,
                    (pack.evidence[0]["id"], "JOB"),
                    (ApplicantClaim(inverted, (pack.evidence[0]["id"],)),),
                ),
            ),
        )
        with pytest.raises(ChatGPTContractError, match="unsupported factual"):
            validate_material_provenance(
                inverted_packet,
                pack=pack,
                candidate=candidate,
                job_text="Candidates need advanced forecasting skills.",
            )

    for third_person in (
        "The candidate has advanced forecasting skills.",
        "Advanced forecasting skills are part of this candidate's background.",
        "Our background includes advanced forecasting skills.",
    ):
        with pytest.raises(ChatGPTContractError, match="structured applicant claim"):
            validate_material_provenance(
                MaterialPacket(
                    candidate_id=candidate.candidate_id,
                    paragraphs=(
                        MaterialParagraph(
                            third_person,
                            (pack.evidence[0]["id"], "JOB"),
                        ),
                    ),
                ),
                pack=pack,
                candidate=candidate,
                job_text="Candidates need advanced forecasting skills.",
            )

    for modal_assertion in (
        "Advanced forecasting is a capability I would bring.",
        "Advanced forecasting is something I can offer.",
        "I can contribute advanced forecasting know-how.",
    ):
        with pytest.raises(ChatGPTContractError, match="structured applicant claim"):
            validate_material_provenance(
                MaterialPacket(
                    candidate_id=candidate.candidate_id,
                    paragraphs=(
                        MaterialParagraph(
                            modal_assertion,
                            (pack.evidence[0]["id"], "JOB"),
                        ),
                    ),
                ),
                pack=pack,
                candidate=candidate,
                job_text="Candidates need advanced forecasting skills.",
            )

    validate_material_provenance(
        MaterialPacket(
            candidate_id=candidate.candidate_id,
            paragraphs=(
                MaterialParagraph(
                    "The role emphasizes advanced forecasting.",
                    ("JOB",),
                ),
            ),
        ),
        pack=pack,
        candidate=candidate,
        job_text="The role emphasizes advanced forecasting.",
    )
    validate_material_provenance(
        MaterialPacket(
            candidate_id=candidate.candidate_id,
            paragraphs=(
                MaterialParagraph(
                    "The role requires experience with Python.",
                    ("JOB",),
                ),
            ),
        ),
        pack=pack,
        candidate=candidate,
        job_text="The role requires experience with Python.",
    )

    supported_pack = build_context_pack(
        PROFILE,
        resume_text="Built a synthetic SQL dashboard.",
        job_text="SQL dashboard",
    )
    supported_fact = next(
        item for item in supported_pack.evidence if "Built a synthetic SQL dashboard" in item["fact"]
    )
    supported_claim = "I built a synthetic SQL dashboard."
    validate_material_provenance(
        MaterialPacket(
            candidate_id=candidate.candidate_id,
            paragraphs=(
                MaterialParagraph(
                    supported_claim,
                    (supported_fact["id"],),
                    (ApplicantClaim(supported_claim, (supported_fact["id"],)),),
                ),
            ),
        ),
        pack=supported_pack,
        candidate=candidate,
        job_text=candidate.description,
    )


def test_budget_and_no_progress_circuit_breakers():
    budget = FunnelBudget(
        discoveries=2,
        first_party_verifications=1,
        material_packets=1,
        form_dry_runs=1,
        model_calls=1,
        no_progress_cycles=2,
    )
    ledger = UsageLedger(run_id="run", budget=budget)
    ledger.reserve("discoveries", 2)
    with pytest.raises(BudgetExceeded, match="discoveries budget"):
        ledger.reserve("discoveries")
    ledger.record_cycle(material_progress=False)
    with pytest.raises(BudgetExceeded, match="circuit breaker"):
        ledger.record_cycle(material_progress=False)


def test_form_review_is_structurally_read_only():
    class ReadOnlyPage:
        url = "about:blank"

        def __init__(self):
            self.goto_calls = []

        def goto(self, url, **_kwargs):
            self.url = url
            self.goto_calls.append(url)

        def evaluate(self, _script):
            return {
                "text": "Application form",
                "inputs": [
                    {
                        "selector": "#email",
                        "type": "email",
                        "name": "email",
                        "label": "Email",
                        "autocomplete": "email",
                        "accept": "",
                        "required": True,
                    }
                ],
                "iframeOrigins": [],
            }

        def fill(self, *_args, **_kwargs):
            raise AssertionError("read-only review must not fill")

        def click(self, *_args, **_kwargs):
            raise AssertionError("read-only review must not click")

        def set_input_files(self, *_args, **_kwargs):
            raise AssertionError("read-only review must not upload")

    page = ReadOnlyPage()
    ledger = UsageLedger(run_id="form-review", budget=FunnelBudget())
    candidate = role()
    packet = MaterialPacket(candidate_id=candidate.candidate_id, paragraphs=())

    review = ReadOnlyFormReviewer(page=page, ledger=ledger, url_guard=lambda _url: None).dry_run(
        candidate=candidate,
        packet=packet,
    )

    assert page.goto_calls == [candidate.official_url]
    assert review["status"] == "form_surface_reviewed"
    assert review["required_field_count"] == 1
    assert review["form_filled"] is False
    assert review["file_uploaded"] is False
    assert review["submitted"] is False


def test_form_review_blocks_when_dom_inspection_fails():
    class BrokenPage:
        url = "about:blank"

        def goto(self, url, **_kwargs):
            self.url = url

        @staticmethod
        def evaluate(_script):
            raise RuntimeError("page crashed")

    candidate = role()
    review = ReadOnlyFormReviewer(
        page=BrokenPage(),
        ledger=UsageLedger(run_id="broken-form", budget=FunnelBudget()),
        url_guard=lambda _url: None,
    ).dry_run(
        candidate=candidate,
        packet=MaterialPacket(candidate_id=candidate.candidate_id, paragraphs=()),
    )

    assert review["status"] == "blocked"
    assert review["reason"] == "inspection_failed"


def test_form_handoff_rejects_field_values_and_side_effects():
    request = {
        "request_id": "request-1",
        "candidate_id": "candidate-1",
        "packet_digest": "packet-1",
        "official_url": "https://jobs.example.com/roles/1",
    }
    payload = {
        "schema_version": "applypilot.form_review.v1",
        "kind": "form_review",
        "request_id": "request-1",
        "candidate_id": "candidate-1",
        "packet_digest": "packet-1",
        "status": "form_surface_reviewed",
        "observed_url": "https://jobs.example.com/apply/1",
        "required_fields": [
            {"label": "Email", "name": "email", "type": "email", "required": True}
        ],
        "iframe_origins": [],
        "captcha_visible": False,
        "login_required": False,
        "account_creation_required": False,
        "form_filled": False,
        "file_uploaded": False,
        "submitted": False,
    }

    assert validate_form_review_response(payload, request=request)["status"] == (
        "form_surface_reviewed"
    )
    with pytest.raises(ValueError, match="submitted=false"):
        validate_form_review_response({**payload, "submitted": True}, request=request)
    leaked = deepcopy(payload)
    leaked["required_fields"] = [
        {**payload["required_fields"][0], "value": "candidate@example.com"}
    ]
    with pytest.raises(ValueError, match="unexpected form-review field metadata"):
        validate_form_review_response(leaked, request=request)
    with pytest.raises(ValueError, match="cannot require a challenge"):
        validate_form_review_response({**payload, "captcha_visible": True}, request=request)
    with pytest.raises(ValueError, match="unrelated to official_url"):
        validate_form_review_response(
            {**payload, "observed_url": "https://attacker.example/apply"},
            request=request,
        )
    with pytest.raises(ValueError, match="unexpected form-review fields"):
        validate_form_review_response({**payload, "notes": "surprise"}, request=request)
    with pytest.raises(ValueError, match="untrusted iframe origin"):
        validate_form_review_response(
            {**payload, "iframe_origins": ["https://attacker.example"]},
            request=request,
        )


def test_authorization_is_exact_expiring_and_action_scoped():
    now = datetime.now(timezone.utc)
    grant = AuthorizationGrant(
        run_id="run-1",
        candidate_id="candidate-1",
        allowed_actions=("submit_application",),
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=10),
        fact_digest="facts",
        context_digest="context",
        policy_digest="policy",
        packet_digest="packet",
        form_review_digest="form",
        grant_id="grant-1",
    )
    require_authorization(
        grant,
        run_id="run-1",
        candidate_id="candidate-1",
        action="submit_application",
        fact_digest="facts",
        context_digest="context",
        policy_digest="policy",
        packet_digest="packet",
        form_review_digest="form",
        now=now,
    )
    with pytest.raises(PermissionError):
        require_authorization(
            grant,
            run_id="run-1",
            candidate_id="candidate-2",
            action="submit_application",
            fact_digest="facts",
            context_digest="context",
            policy_digest="policy",
            packet_digest="packet",
            form_review_digest="form",
            now=now,
        )
    with pytest.raises(PermissionError):
        require_authorization(
            grant,
            run_id="run-1",
            candidate_id="candidate-1",
            action="send_email",
            fact_digest="facts",
            context_digest="context",
            policy_digest="policy",
            packet_digest="packet",
            form_review_digest="form",
            now=now,
        )
    with pytest.raises(PermissionError):
        require_authorization(
            grant,
            run_id="run-1",
            candidate_id="candidate-1",
            action="submit_application",
            fact_digest="changed",
            context_digest="context",
            policy_digest="policy",
            packet_digest="packet",
            form_review_digest="form",
            now=now,
        )


class FakeDiscovery:
    def __init__(self, candidates, *, fail=False):
        self.candidates = candidates
        self.fail = fail
        self.calls = 0

    def find_roles(self, **_kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("chatgpt unavailable")
        return list(self.candidates)


class FakeVerifier:
    def __init__(self, evidence):
        self.evidence = evidence
        self.calls = []

    def verify(self, candidate):
        self.calls.append(candidate.candidate_id)
        return self.evidence[candidate.candidate_id]


class FakeMaterials:
    def __init__(self, pack):
        self.pack = pack
        self.calls = []

    def draft_material(self, *, candidate, **_kwargs):
        self.calls.append(candidate.candidate_id)
        return MaterialPacket(
            candidate_id=candidate.candidate_id,
            paragraphs=(
                MaterialParagraph(
                    "Example Labs.",
                    (self.pack.evidence[0]["id"],),
                ),
            ),
        )


class FakeFormReview:
    def __init__(self):
        self.calls = []

    def dry_run(self, *, candidate, packet):
        self.calls.append((candidate.candidate_id, packet.candidate_id))
        return {"status": "dry_run_verified", "fields_filled": 4}


def test_autonomous_batch_uses_hard_gates_and_one_form_dry_run(tmp_path):
    accepted = role()
    senior = role(
        company="SeniorCo",
        title="Senior Product Manager",
        official_url="https://jobs.ashbyhq.com/seniorco/1",
        required_experience_min=5,
    )
    stale = role(
        company="StaleCo",
        official_url="https://jobs.lever.co/staleco/2",
    )
    pack = build_context_pack(PROFILE, job_text=accepted.description)
    discovery = FakeDiscovery([accepted, senior, stale])
    verifier = FakeVerifier(
        {
            accepted.candidate_id: fresh(accepted),
            stale.candidate_id: fresh(stale, updated_date=date(2025, 1, 1)),
        }
    )
    materials = FakeMaterials(pack)
    form = FakeFormReview()
    batch = AutonomousBatch(
        run_id="run-1",
        profile=CandidateProfile(),
        context_pack=pack,
        dependencies=BatchDependencies(
            discovery=discovery,
            verifier=verifier,
            materials=materials,
            form_review=form,
        ),
        output_dir=tmp_path,
    )

    result = batch.run(query="product analyst internships")

    assert result.status == "review_ready"
    assert materials.calls == [accepted.candidate_id]
    assert form.calls == [(accepted.candidate_id, accepted.candidate_id)]
    assert not result.final_actions
    assert result.usage["counts"]["model_calls"] == 0
    assert result.usage["counts"]["form_dry_runs"] == 1
    assert result.usage["counts"]["artifacts"] == 3
    assert (tmp_path / "run-1" / "result_ledger.json").exists()


def test_batch_reports_blocked_form_as_blocked_not_review_ready():
    candidate = role()
    pack = build_context_pack(PROFILE, job_text=candidate.description)

    class BlockedFormReview:
        @staticmethod
        def dry_run(**_kwargs):
            return {"status": "blocked", "reason": "posting_not_found_in_rendered_browser"}

    result = AutonomousBatch(
        run_id="blocked-form",
        profile=CandidateProfile(),
        context_pack=pack,
        dependencies=BatchDependencies(
            discovery=FakeDiscovery([candidate]),
            verifier=FakeVerifier({candidate.candidate_id: fresh(candidate)}),
            materials=FakeMaterials(pack),
            form_review=BlockedFormReview(),
        ),
    ).run(query="product analyst internships")

    assert result.status == "form_review_blocked"
    assert result.final_actions == []


def test_batch_rechecks_eligibility_from_first_party_description():
    candidate = role(description="Entry-level analyst role. 0-2 years accepted.")
    pack = build_context_pack(PROFILE, job_text=candidate.description)
    materials = FakeMaterials(pack)
    evidence = fresh(
        candidate,
        title="Senior Product Analyst",
        description="Senior role requiring 5+ years of relevant experience.",
    )

    result = AutonomousBatch(
        run_id="verified-gate",
        profile=CandidateProfile(),
        context_pack=pack,
        dependencies=BatchDependencies(
            discovery=FakeDiscovery([candidate]),
            verifier=FakeVerifier({candidate.candidate_id: evidence}),
            materials=materials,
        ),
    ).run(query="product analyst internships")

    assert result.status == "no_eligible_verified_roles"
    assert materials.calls == []
    assert any(
        item["basis"] == "first_party" and item["decision"] == "reject"
        for item in result.eligibility
    )
    assert any(item["stage"] == "verified_eligibility" for item in result.blockers)


def test_batch_keeps_soft_review_candidates_for_materials_but_marks_them():
    candidate = role(location="London, United Kingdom")
    pack = build_context_pack(PROFILE, job_text=candidate.description)
    result = AutonomousBatch(
        run_id="review-candidate",
        profile=CandidateProfile(),
        context_pack=pack,
        dependencies=BatchDependencies(
            discovery=FakeDiscovery([candidate]),
            verifier=FakeVerifier({candidate.candidate_id: fresh(candidate)}),
            materials=FakeMaterials(pack),
        ),
    ).run(query="product analyst internships")

    assert result.status == "review_ready"
    assert result.materials[0]["human_review_required"] == [
        "location_outside_preferences"
    ]
    assert result.final_actions == []


def test_canonical_url_change_cannot_drop_soft_review_before_live_action():
    discovered = role(
        location="London, United Kingdom",
        official_url="https://job-boards.greenhouse.io/example/jobs/123?ref=discovery",
    )
    canonical = replace(
        discovered,
        official_url="https://job-boards.greenhouse.io/example/jobs/123-canonical",
    )
    pack = build_context_pack(PROFILE, job_text=canonical.description)
    packet = MaterialPacket(
        candidate_id=canonical.candidate_id,
        paragraphs=(MaterialParagraph("Python product analysis.", ("JOB",)),),
    )
    fact_ledger = build_fact_ledger(
        PROFILE,
        resume_text="Built Python product analytics tools.",
    )
    policy = RunPolicy(review_only=False)
    review = {
        "candidate_id": canonical.candidate_id,
        "status": "dry_run_verified",
        "fields_filled": 4,
    }

    class FixedMaterials:
        @staticmethod
        def draft_material(**_kwargs):
            return packet

    class FinalAction:
        calls = 0

        @classmethod
        def submit(cls, **_kwargs):
            cls.calls += 1
            return {"status": "submitted_confirmed"}

    class Store:
        @staticmethod
        def consume(_grant):
            return True

    now = datetime.now(timezone.utc)
    grant = AuthorizationGrant(
        run_id="redirect-review",
        candidate_id=canonical.candidate_id,
        allowed_actions=("submit_application",),
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=10),
        fact_digest=fact_ledger.digest,
        context_digest=pack.digest,
        policy_digest=policy.digest,
        packet_digest=packet.digest,
        form_review_digest=_mapping_digest(review),
        grant_id="redirect-review-grant",
    )
    evidence = fresh(discovered, official_url=canonical.official_url)

    result = AutonomousBatch(
        run_id="redirect-review",
        profile=CandidateProfile(),
        context_pack=pack,
        dependencies=BatchDependencies(
            discovery=FakeDiscovery([discovered]),
            verifier=FakeVerifier({discovered.candidate_id: evidence}),
            materials=FixedMaterials(),
            form_review=FakeFormReview(),
            final_action=FinalAction(),
            authorization_store=Store(),
        ),
        policy=policy,
        authorization=grant,
        fact_ledger=fact_ledger,
    ).run(query="product analyst internships")

    assert result.status == "failed_closed"
    assert FinalAction.calls == 0
    assert result.materials[0]["human_review_required"] == [
        "location_outside_preferences"
    ]
    assert any("eligibility review" in item.get("detail", "") for item in result.blockers)


def test_batch_keeps_open_first_party_role_when_only_freshness_date_is_missing():
    candidate = role()
    pack = build_context_pack(PROFILE, job_text=candidate.description)
    evidence = fresh(candidate, posted_date=None, updated_date=None)
    result = AutonomousBatch(
        run_id="missing-date-review",
        profile=CandidateProfile(),
        context_pack=pack,
        dependencies=BatchDependencies(
            discovery=FakeDiscovery([candidate]),
            verifier=FakeVerifier({candidate.candidate_id: evidence}),
            materials=FakeMaterials(pack),
        ),
    ).run(query="product analyst internships")

    assert result.status == "review_ready"
    assert result.materials[0]["human_review_required"] == [
        "freshness_dates_missing"
    ]


def test_batch_fallback_runs_only_after_primary_failure():
    candidate = role()
    pack = build_context_pack(PROFILE, job_text=candidate.description)
    primary = FakeDiscovery([], fail=True)
    fallback = FakeDiscovery([candidate])
    materials = FakeMaterials(pack)
    result = AutonomousBatch(
        run_id="fallback-run",
        profile=CandidateProfile(),
        context_pack=pack,
        dependencies=BatchDependencies(
            discovery=primary,
            fallback_discovery=fallback,
            verifier=FakeVerifier({candidate.candidate_id: fresh(candidate)}),
            materials=materials,
        ),
    ).run(query="analyst internships")

    assert result.status == "review_ready"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_live_batch_requires_digest_bound_one_time_grant():
    candidate = role()
    pack = build_context_pack(PROFILE, job_text=candidate.description)
    packet = MaterialPacket(
        candidate_id=candidate.candidate_id,
        paragraphs=(MaterialParagraph("Python product analysis.", ("JOB",)),),
    )
    fact_ledger = build_fact_ledger(
        PROFILE,
        resume_text="Built Python product analytics tools.",
    )
    policy = RunPolicy(review_only=False)
    review = {
        "candidate_id": candidate.candidate_id,
        "status": "dry_run_verified",
        "fields_filled": 4,
    }

    class FixedMaterials:
        @staticmethod
        def draft_material(**_kwargs):
            return packet

    class FinalAction:
        def __init__(self):
            self.calls = 0

        def submit(self, **_kwargs):
            self.calls += 1
            return {"status": "submitted_confirmed"}

    class OneTimeStore:
        def __init__(self):
            self.consumed = set()

        def consume(self, grant):
            if grant.grant_id in self.consumed:
                return False
            self.consumed.add(grant.grant_id)
            return True

    now = datetime.now(timezone.utc)
    grant = AuthorizationGrant(
        run_id="live-run",
        candidate_id=candidate.candidate_id,
        allowed_actions=("submit_application",),
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=10),
        fact_digest=fact_ledger.digest,
        context_digest=pack.digest,
        policy_digest=policy.digest,
        packet_digest=packet.digest,
        form_review_digest=_mapping_digest(review),
        grant_id="one-time-grant",
    )
    final = FinalAction()
    store = OneTimeStore()

    def make_batch():
        return AutonomousBatch(
            run_id="live-run",
            profile=CandidateProfile(),
            context_pack=pack,
            dependencies=BatchDependencies(
                discovery=FakeDiscovery([candidate]),
                verifier=FakeVerifier({candidate.candidate_id: fresh(candidate)}),
                materials=FixedMaterials(),
                form_review=FakeFormReview(),
                final_action=final,
                authorization_store=store,
            ),
            policy=policy,
            authorization=grant,
            fact_ledger=fact_ledger,
        )

    first = make_batch().run(query="product analyst")
    second = make_batch().run(query="product analyst")

    assert first.status == "submitted"
    assert final.calls == 1
    assert second.status == "failed_closed"
    assert any("already consumed" in item.get("detail", "") for item in second.blockers)


def test_autonomy_plan_cli_writes_compact_secret_free_request(monkeypatch, tmp_path):
    app_dir = tmp_path / "app-data"
    app_dir.mkdir()
    profile_path = app_dir / "profile.json"
    resume_path = app_dir / "resume.txt"
    profile_path.write_text(json.dumps(PROFILE), encoding="utf-8")
    resume_path.write_text(
        "Test Candidate | candidate@example.com | 555-0100\n"
        "• Built Python and SQL product analytics tools.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "APP_DIR", app_dir)
    monkeypatch.setattr(config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(config, "RESUME_PATH", resume_path)
    monkeypatch.setattr(config, "ENV_PATH", app_dir / ".env")

    out = tmp_path / "runs"
    result = CLI_RUNNER.invoke(
        app,
        ["autonomy", "plan", "--query", "product analytics internships", "--out", str(out)],
    )

    assert result.exit_code == 0, result.output
    request_path = next(out.glob("*/handoff/discovery.request.json"))
    request_text = request_path.read_text(encoding="utf-8")
    fact_text = next(out.glob("*/fact_ledger.json")).read_text(encoding="utf-8")
    request_payload = json.loads(request_text)
    assert len(request_text) < 12_000
    assert request_payload["response_path"] == "handoff/discovery.response.json"
    assert not Path(request_payload["response_path"]).is_absolute()
    assert next(out.glob("*/run_manifest.json")).exists()
    assert "candidate@example.com" not in request_text
    assert "555-0100" not in request_text
    assert "secret-sentinel" not in request_text
    assert "secret-sentinel" not in fact_text
    assert "personal.password" not in fact_text

    facts = json.loads(fact_text)
    campaign_dir = tmp_path / "campaign"
    created = CLI_RUNNER.invoke(
        app,
        [
            "campaign",
            "create",
            "--run-dir",
            str(request_path.parents[1]),
            "--approved-fact-digest",
            facts["digest"],
            "--campaign-id",
            "review-campaign",
            "--code-revision",
            "a" * 40,
            "--out",
            str(campaign_dir),
        ],
    )
    assert created.exit_code == 0, created.output
    campaign_manifest = json.loads((campaign_dir / "manifest.json").read_text(encoding="utf-8"))
    assert campaign_manifest["source_run_id"] == request_payload["run_id"]
    assert campaign_manifest["submit_authorized"] is False
    assert campaign_manifest["target_confirmed"] == 100

    status = CLI_RUNNER.invoke(
        app,
        ["campaign", "status", "--campaign-dir", str(campaign_dir)],
    )
    assert status.exit_code == 0, status.output
    assert "private" not in status.output.lower()
    assert '"submitted_confirmed": 0' in status.output


def test_pre_campaign_status_and_heartbeat_are_fixed_name_and_redacted(
    monkeypatch,
    tmp_path,
):
    from applypilot.autonomy import approval

    app_dir = tmp_path / "app-data"
    app_dir.mkdir()
    profile_path = app_dir / "profile.json"
    resume_path = app_dir / "resume.txt"
    profile_path.write_text(json.dumps(PROFILE), encoding="utf-8")
    resume_path.write_text(
        "Test Candidate | candidate@example.com | 555-0100\n"
        "Built Python and SQL product analytics tools.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "APP_DIR", app_dir)
    monkeypatch.setattr(config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(config, "RESUME_PATH", resume_path)
    monkeypatch.setattr(config, "ENV_PATH", app_dir / ".env")
    monkeypatch.setattr(
        approval,
        "require_system_approval_trust_store",
        lambda: tmp_path / "allowed_signers",
    )

    out = tmp_path / "runs"
    planned = CLI_RUNNER.invoke(
        app,
        ["autonomy", "plan", "--query", "product analytics internships", "--out", str(out)],
    )
    assert planned.exit_code == 0, planned.output
    run_dir = next(path for path in out.iterdir() if path.is_dir())
    now = datetime.fromisoformat(
        str(json.loads((run_dir / "run_manifest.json").read_text())["created_at"])
    )

    status = run_status_snapshot(run_dir=run_dir, now=now)

    assert status["review_phase"] == "awaiting_role_candidates"
    assert status["live_gate"] == "preferred_location"
    assert status["submitted_confirmed"] == 0
    assert status["target_confirmed"] == 100
    assert status["preferred_location_fact_count"] == 0
    assert status["heartbeat_due"] is True
    serialized = json.dumps(status, sort_keys=True)
    for private in (
        "candidate@example.com",
        "555-0100",
        "123 Main Street",
        "secret-sentinel",
        "product analytics internships",
    ):
        assert private not in serialized

    recorded = record_run_heartbeat(run_dir=run_dir, now=now)
    heartbeat_path = run_dir / "heartbeat.json"
    first_text = heartbeat_path.read_text(encoding="utf-8")
    assert recorded["heartbeat_due"] is False
    assert run_status_snapshot(
        run_dir=run_dir,
        now=now + timedelta(seconds=299),
    )["heartbeat_due"] is False
    assert run_status_snapshot(
        run_dir=run_dir,
        now=now + timedelta(seconds=300),
    )["heartbeat_due"] is True

    record_run_heartbeat(run_dir=run_dir, now=now + timedelta(microseconds=1))
    second_text = heartbeat_path.read_text(encoding="utf-8")
    assert first_text != second_text
    assert list(run_dir.glob("heartbeat*.json")) == [heartbeat_path]
    assert "candidate@example.com" not in second_text

    cli_status = CLI_RUNNER.invoke(
        app,
        ["autonomy", "status", "--run-dir", str(run_dir)],
    )
    assert cli_status.exit_code == 0, cli_status.output
    assert '"submitted_confirmed": 0' in cli_status.output
    assert "candidate@example.com" not in cli_status.output
    cli_heartbeat = CLI_RUNNER.invoke(
        app,
        ["autonomy", "heartbeat", "--run-dir", str(run_dir)],
    )
    assert cli_heartbeat.exit_code == 0, cli_heartbeat.output
    assert '"heartbeat_due": false' in cli_heartbeat.output
    assert "candidate@example.com" not in cli_heartbeat.output

    valid_heartbeat_text = heartbeat_path.read_text(encoding="utf-8")
    last_recorded = datetime.fromisoformat(
        str(json.loads(valid_heartbeat_text)["recorded_at"])
    )
    attack_now = last_recorded + timedelta(seconds=600)
    unbound = json.loads(valid_heartbeat_text)
    unbound["recorded_at"] = (attack_now + timedelta(days=1)).isoformat()
    heartbeat_path.write_text(json.dumps(unbound), encoding="utf-8")
    with pytest.raises(ValueError, match="heartbeat bindings are invalid"):
        run_status_snapshot(run_dir=run_dir, now=attack_now)
    heartbeat_path.write_text(valid_heartbeat_text, encoding="utf-8")

    victim = tmp_path / "must-not-be-overwritten.txt"
    victim.write_text("preserve me", encoding="utf-8")
    monkeypatch.setattr(autonomy_runner.secrets, "token_hex", lambda _size: "fixed")
    malicious_temporary = run_dir / f".heartbeat.json.{os.getpid()}.fixed.tmp"
    malicious_temporary.symlink_to(victim)
    with pytest.raises(FileExistsError):
        record_run_heartbeat(run_dir=run_dir, now=attack_now)
    assert victim.read_text(encoding="utf-8") == "preserve me"
    malicious_temporary.unlink()

    before_replace_failure = heartbeat_path.read_bytes()

    def fail_replace(*_args):
        raise OSError("synthetic replace failure")

    with monkeypatch.context() as replace_patch:
        replace_patch.setattr(autonomy_runner.os, "replace", fail_replace)
        with pytest.raises(OSError, match="synthetic replace failure"):
            record_run_heartbeat(run_dir=run_dir, now=attack_now)
    assert heartbeat_path.read_bytes() == before_replace_failure
    assert list(run_dir.glob(".heartbeat.json.*.tmp")) == []

    wrong_interval = json.loads(valid_heartbeat_text)
    wrong_interval["status"]["heartbeat_interval_seconds"] = 299
    wrong_interval["status_sha256"] = hashlib.sha256(
        json.dumps(
            wrong_interval["status"],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    heartbeat_path.write_text(json.dumps(wrong_interval), encoding="utf-8")
    with pytest.raises(ValueError, match="heartbeat bindings are invalid"):
        run_status_snapshot(run_dir=run_dir, now=attack_now)
    heartbeat_path.write_text(valid_heartbeat_text, encoding="utf-8")

    heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    future = (attack_now + timedelta(days=1)).isoformat()
    heartbeat["recorded_at"] = future
    heartbeat["status"]["recorded_at"] = future
    heartbeat["status"]["last_heartbeat_at"] = future
    heartbeat["status_sha256"] = hashlib.sha256(
        json.dumps(
            heartbeat["status"],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    heartbeat_path.write_text(json.dumps(heartbeat), encoding="utf-8")
    with pytest.raises(ValueError, match="timestamp is in the future"):
        run_status_snapshot(run_dir=run_dir, now=attack_now)
    heartbeat_path.unlink()

    original_request = json.loads(
        (run_dir / "handoff" / "discovery.request.json").read_text(encoding="utf-8")
    )
    extra_request = dict(original_request)
    extra_request["kind"] = "material_packet"
    extra_request["response_path"] = "handoff/extra.response.json"
    extra_request_path = run_dir / "handoff" / "extra.request.json"
    extra_request_path.write_text(json.dumps(extra_request), encoding="utf-8")
    with pytest.raises(ValueError, match="more than one active handoff exchange"):
        run_status_snapshot(run_dir=run_dir, now=attack_now)
    extra_request_path.unlink()

    base_result = {
        "run_id": status["run_id"],
        "status": "candidate@example.com",
        "pending_requests": [],
        "source_attempts": [],
        "discoveries": [],
        "eligibility": [],
        "freshness": [],
        "materials": [],
        "form_reviews": [],
        "final_actions": [],
        "blockers": [],
        "usage": {"run_id": status["run_id"]},
    }
    (run_dir / "result_ledger.json").write_text(json.dumps(base_result), encoding="utf-8")
    with pytest.raises(ValueError, match="result ledger status is invalid"):
        run_status_snapshot(run_dir=run_dir, now=attack_now)

    mismatched_pending = dict(base_result)
    mismatched_pending["status"] = "awaiting_chatgpt_web"
    mismatched_pending["pending_requests"] = [
        {
            "surface": "browser_tool",
            "kind": "form_review",
            "request_id": "request-1",
            "request_path": "request.json",
            "response_path": "response.json",
        }
    ]
    (run_dir / "result_ledger.json").write_text(
        json.dumps(mismatched_pending),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="ChatGPT wait has no matching request"):
        run_status_snapshot(run_dir=run_dir, now=attack_now)

    budget_exhausted = dict(base_result)
    budget_exhausted["status"] = "budget_exhausted"
    budget_exhausted["pending_requests"] = [
        {
            "surface": "chatgpt_web",
            "kind": "role_candidates",
            "request_id": "request-1",
            "request_path": "request.json",
            "response_path": "response.json",
        }
    ]
    (run_dir / "result_ledger.json").write_text(
        json.dumps(budget_exhausted),
        encoding="utf-8",
    )
    budget_status = run_status_snapshot(run_dir=run_dir, now=attack_now)
    assert budget_status["review_phase"] == "reported_budget_exhausted"

    forged_submitted = dict(base_result)
    forged_submitted["status"] = "submitted"
    forged_submitted["final_actions"] = [
        {"status": "submitted_confirmed", "candidate_id": "candidate-1"}
    ]
    (run_dir / "result_ledger.json").write_text(
        json.dumps(forged_submitted),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="result ledger status is invalid"):
        run_status_snapshot(run_dir=run_dir, now=attack_now)


def test_artifact_advance_blocks_unreviewed_required_facts_before_tools(monkeypatch, tmp_path):
    app_dir = tmp_path / "app-data"
    app_dir.mkdir()
    profile = deepcopy(PROFILE)
    profile["work_authorization"]["require_sponsorship"] = "unknown"
    profile_path = app_dir / "profile.json"
    resume_path = app_dir / "resume.txt"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    resume_path.write_text("Built Python product analytics tools.", encoding="utf-8")
    monkeypatch.setattr(config, "APP_DIR", app_dir)
    monkeypatch.setattr(config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(config, "RESUME_PATH", resume_path)
    monkeypatch.setattr(config, "ENV_PATH", app_dir / ".env")

    out = tmp_path / "runs"
    planned = CLI_RUNNER.invoke(
        app,
        ["autonomy", "plan", "--query", "analyst internships", "--out", str(out)],
    )
    assert planned.exit_code == 0, planned.output
    run_dir = next(path for path in out.iterdir() if path.is_dir())
    facts = json.loads((run_dir / "fact_ledger.json").read_text(encoding="utf-8"))

    class MustNotVerify:
        @staticmethod
        def verify(_candidate):
            raise AssertionError("verifier must not run before fact readiness")

    with pytest.raises(PermissionError, match="require_sponsorship"):
        advance_artifact_run(
            run_dir=run_dir,
            approved_fact_digest=facts["digest"],
            verifier=MustNotVerify(),
        )


def test_artifact_handoff_advances_to_review_ready_without_browser(monkeypatch, tmp_path):
    app_dir = tmp_path / "app-data"
    app_dir.mkdir()
    profile_path = app_dir / "profile.json"
    resume_path = app_dir / "resume.txt"
    profile_path.write_text(json.dumps(PROFILE), encoding="utf-8")
    resume_path.write_text(
        "Test Candidate | candidate@example.com | 555-0100\n"
        "Built Python and SQL product analytics tools.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "APP_DIR", app_dir)
    monkeypatch.setattr(config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(config, "RESUME_PATH", resume_path)
    monkeypatch.setattr(config, "ENV_PATH", app_dir / ".env")

    out = tmp_path / "runs"
    planned = CLI_RUNNER.invoke(
        app,
        ["autonomy", "plan", "--query", "product analytics internships", "--out", str(out)],
    )
    assert planned.exit_code == 0, planned.output
    run_dir = next(path for path in out.iterdir() if path.is_dir())
    facts = json.loads((run_dir / "fact_ledger.json").read_text(encoding="utf-8"))
    candidate = role()
    verifier = FakeVerifier({candidate.candidate_id: fresh(candidate)})

    pending = advance_artifact_run(
        run_dir=run_dir,
        approved_fact_digest=facts["digest"],
        verifier=verifier,
    )
    assert pending["status"] == "awaiting_chatgpt_web", pending
    assert pending["pending_requests"][0]["kind"] == "role_candidates"
    pending_status = run_status_snapshot(run_dir=run_dir)
    assert pending_status["review_phase"] == "awaiting_role_candidates"
    assert pending_status["result"]["trust"] == "validated_but_mutable_untrusted"
    assert pending_status["result"]["reported_status"] == "awaiting_chatgpt_web"

    discovery_request = json.loads(
        (run_dir / "handoff" / "discovery.request.json").read_text(encoding="utf-8")
    )
    discovery_input = tmp_path / "discovery.input.json"
    discovery_input.write_text(
        json.dumps(
            {
                "schema_version": "applypilot.chatgpt_web.v1",
                "kind": "role_candidates",
                "request_id": discovery_request["request_id"],
                "items": [
                    {
                        "company": candidate.company,
                        "title": candidate.title,
                        "official_url": candidate.official_url,
                        "location": candidate.location,
                        "description": candidate.description,
                        "required_experience_min": 0,
                        "required_experience_max": 2,
                        "posted_date": "2026-07-01",
                        "start_date": None,
                        "end_date": None,
                        "evidence": ["synthetic official ATS fixture"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    import_response_artifact(
        request_path=run_dir / "handoff" / "discovery.request.json",
        input_path=discovery_input,
    )
    assert run_status_snapshot(run_dir=run_dir)["review_phase"] == (
        "response_ready_to_advance"
    )

    awaiting_material = advance_artifact_run(
        run_dir=run_dir,
        approved_fact_digest=facts["digest"],
        verifier=verifier,
    )
    assert awaiting_material["status"] == "awaiting_chatgpt_web"
    assert awaiting_material["pending_requests"][0]["kind"] == "material_packet"

    material_request_path = Path(awaiting_material["pending_requests"][0]["request_path"])
    material_request = json.loads(material_request_path.read_text(encoding="utf-8"))
    context = json.loads((run_dir / "context_pack.json").read_text(encoding="utf-8"))
    evidence = context["evidence"][0]
    material_input = tmp_path / "material.input.json"
    material_input.write_text(
        json.dumps(
            {
                "schema_version": "applypilot.chatgpt_web.v1",
                "kind": "material_packet",
                "request_id": material_request["request_id"],
                "candidate_id": candidate.candidate_id,
                "paragraphs": [
                    {
                        "text": evidence["fact"],
                        "evidence_ids": [evidence["id"]],
                        "applicant_claims": [],
                    }
                ],
                "verification_gaps": [],
            }
        ),
        encoding="utf-8",
    )
    import_response_artifact(
        request_path=material_request_path,
        input_path=material_input,
    )

    awaiting_form = advance_artifact_run(
        run_dir=run_dir,
        approved_fact_digest=facts["digest"],
        verifier=verifier,
    )
    assert awaiting_form["status"] == "awaiting_browser_tool"
    assert awaiting_form["pending_requests"][0]["kind"] == "form_review"
    form_request_path = Path(awaiting_form["pending_requests"][0]["request_path"])
    form_request = json.loads(form_request_path.read_text(encoding="utf-8"))
    form_input = tmp_path / "form.input.json"
    form_input.write_text(
        json.dumps(
            {
                "schema_version": "applypilot.form_review.v1",
                "kind": "form_review",
                "request_id": form_request["request_id"],
                "candidate_id": candidate.candidate_id,
                "packet_digest": form_request["packet_digest"],
                "status": "form_surface_reviewed",
                "observed_url": candidate.official_url,
                "required_fields": [
                    {
                        "label": "Email",
                        "name": "email",
                        "type": "email",
                        "required": True,
                    }
                ],
                "iframe_origins": [],
                "captcha_visible": False,
                "login_required": False,
                "account_creation_required": False,
                "form_filled": False,
                "file_uploaded": False,
                "submitted": False,
                "reason": "synthetic DOM fixture",
            }
        ),
        encoding="utf-8",
    )
    import_response_artifact(request_path=form_request_path, input_path=form_input)

    complete = advance_artifact_run(
        run_dir=run_dir,
        approved_fact_digest=facts["digest"],
        verifier=verifier,
    )
    assert complete["status"] == "review_ready"
    assert complete["usage"]["counts"]["model_calls"] == 2
    assert complete["usage"]["counts"]["browser_navigations"] == 3
    assert complete["usage"]["counts"]["external_calls"] == 3
    assert complete["usage"]["counts"]["form_dry_runs"] == 1
    assert complete["form_reviews"][0]["status"] == "form_surface_reviewed"
    assert complete["final_actions"] == []
    assert Path(complete["materials"][0]["artifact_paths"]["cover_letter"]).exists()

    monkeypatch.setattr(
        autonomy_handoff,
        "build_material_prompt",
        lambda *_args, **_kwargs: "new code would generate a different prompt",
    )
    resumed = advance_artifact_run(
        run_dir=run_dir,
        approved_fact_digest=facts["digest"],
        verifier=verifier,
    )
    assert resumed["status"] == "review_ready"

    material_response_path = Path(material_request["response_path"])
    if not material_response_path.is_absolute():
        material_response_path = run_dir / material_response_path
    changed = json.loads(material_response_path.read_text(encoding="utf-8"))
    changed["verification_gaps"] = ["response edited after consumption"]
    material_response_path.write_text(json.dumps(changed), encoding="utf-8")
    replay = advance_artifact_run(
        run_dir=run_dir,
        approved_fact_digest=facts["digest"],
        verifier=verifier,
    )
    assert replay["status"] == "failed_closed"
    assert any(
        "response receipt mismatch" in item.get("detail", "")
        for item in replay["blockers"]
    )


def test_artifact_import_rejects_swapped_request_id(monkeypatch, tmp_path):
    app_dir = tmp_path / "app-data"
    app_dir.mkdir()
    profile_path = app_dir / "profile.json"
    resume_path = app_dir / "resume.txt"
    profile_path.write_text(json.dumps(PROFILE), encoding="utf-8")
    resume_path.write_text("Built Python product analytics tools.", encoding="utf-8")
    monkeypatch.setattr(config, "APP_DIR", app_dir)
    monkeypatch.setattr(config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(config, "RESUME_PATH", resume_path)
    monkeypatch.setattr(config, "ENV_PATH", app_dir / ".env")

    out = tmp_path / "runs"
    planned = CLI_RUNNER.invoke(
        app,
        ["autonomy", "plan", "--query", "analyst internships", "--out", str(out)],
    )
    assert planned.exit_code == 0, planned.output
    request_path = next(out.glob("*/handoff/discovery.request.json"))
    bad_input = tmp_path / "bad.json"
    bad_input.write_text(
        json.dumps(
            {
                "schema_version": "applypilot.chatgpt_web.v1",
                "kind": "role_candidates",
                "request_id": "wrong-run-or-request",
                "items": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ChatGPTContractError, match="request_id mismatch"):
        import_response_artifact(request_path=request_path, input_path=bad_input)
    rejected_imports = list(request_path.parent.glob("discovery.rejected.*.json"))
    assert len(rejected_imports) == 1
    assert "wrong-run-or-request" not in rejected_imports[0].read_text(encoding="utf-8")

    request_payload = json.loads(request_path.read_text(encoding="utf-8"))
    rendered_copy = tmp_path / "rendered-copy.json"
    rendered_copy.write_text(
        json.dumps(
            {
                "schema_version": "applypilot.chatgpt_web.v1",
                "kind": "role_candidates",
                "request_id": request_payload["request_id"],
                "items": [
                    {
                        "company": "Example",
                        "title": "Product Analyst Intern",
                        "official_url": "[https://jobs.example.com/roles/1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ChatGPTContractError, match="invalid official_url"):
        import_response_artifact(request_path=request_path, input_path=rendered_copy)


def test_artifact_handoff_binds_dynamic_inputs_and_allows_corrected_material(tmp_path):
    run_dir = tmp_path / "run"
    pack = build_context_pack(PROFILE, job_text="Python product analytics")
    client = ArtifactChatGPTClient(
        run_dir=run_dir,
        bindings=RunBindings(
            run_id="bound-inputs",
            fact_digest="facts",
            context_digest=pack.digest,
            policy_digest="policy",
        ),
        ledger=UsageLedger(run_id="bound-inputs", budget=FunnelBudget()),
    )

    with pytest.raises(ChatGPTArtifactPending) as discovery_pending:
        client.find_roles(pack=pack, query="product analytics", limit=1)
    discovery_request_path = discovery_pending.value.request_path
    discovery_request = json.loads(discovery_request_path.read_text(encoding="utf-8"))
    discovery_input = tmp_path / "discovery.json"
    candidate = role()
    discovery_input.write_text(
        json.dumps(
            {
                "schema_version": "applypilot.chatgpt_web.v1",
                "kind": "role_candidates",
                "request_id": discovery_request["request_id"],
                "items": [
                    {
                        "company": candidate.company,
                        "title": candidate.title,
                        "official_url": candidate.official_url,
                        "location": candidate.location,
                        "description": candidate.description,
                        "required_experience_min": 0,
                        "required_experience_max": 2,
                        "posted_date": "2026-07-01",
                        "start_date": None,
                        "end_date": None,
                        "evidence": ["synthetic fixture"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    import_response_artifact(
        request_path=discovery_request_path,
        input_path=discovery_input,
    )
    discovered = client.find_roles(pack=pack, query="product analytics", limit=1)
    assert len(discovered) == 1

    with pytest.raises(ValueError, match="inputs or bindings changed"):
        client.find_roles(pack=pack, query="different query", limit=1)
    with pytest.raises(ValueError, match="inputs or bindings changed"):
        client.find_roles(pack=pack, query="product analytics", limit=2)

    candidate = discovered[0]
    with pytest.raises(ChatGPTArtifactPending) as material_pending:
        client.draft_material(
            pack=pack,
            candidate=candidate,
            verified_job_text=candidate.description,
        )
    material_request_path = material_pending.value.request_path
    material_request = json.loads(material_request_path.read_text(encoding="utf-8"))
    invalid_input = tmp_path / "invalid-material.json"
    invalid_input.write_text(
        json.dumps(
            {
                "schema_version": "applypilot.chatgpt_web.v1",
                "kind": "material_packet",
                "request_id": material_request["request_id"],
                "candidate_id": candidate.candidate_id,
                "paragraphs": [
                    {
                        "text": "I increased revenue by 999 percent.",
                        "evidence_ids": [pack.evidence[0]["id"]],
                        "applicant_claims": [
                            {
                                "text": "I increased revenue by 999 percent.",
                                "evidence_ids": [pack.evidence[0]["id"]],
                            }
                        ],
                    }
                ],
                "verification_gaps": [],
            }
        ),
        encoding="utf-8",
    )
    import_response_artifact(request_path=material_request_path, input_path=invalid_input)

    with pytest.raises(ChatGPTContractError, match="unsupported numeric"):
        client.draft_material(
            pack=pack,
            candidate=candidate,
            verified_job_text=candidate.description,
        )
    material_response_path = run_dir / material_request["response_path"]
    material_receipt_path = material_response_path.with_name(
        material_response_path.name.replace(".response.json", ".receipt.json")
    )
    assert not material_response_path.exists()
    assert not material_receipt_path.exists()
    assert list(material_response_path.parent.glob("materials.*.rejected.*.json"))
    restored = UsageLedger(run_id="bound-inputs", budget=FunnelBudget())
    autonomy_runner._restore_artifact_usage(restored, run_dir)
    assert restored.counts["model_calls"] == 2
    assert restored.counts["retries"] == 1

    corrected_input = tmp_path / "corrected-material.json"
    corrected_input.write_text(
        json.dumps(
            {
                "schema_version": "applypilot.chatgpt_web.v1",
                "kind": "material_packet",
                "request_id": material_request["request_id"],
                "candidate_id": candidate.candidate_id,
                "paragraphs": [
                    {
                        "text": pack.evidence[0]["fact"],
                        "evidence_ids": [pack.evidence[0]["id"]],
                        "applicant_claims": [],
                    }
                ],
                "verification_gaps": [],
            }
        ),
        encoding="utf-8",
    )
    import_response_artifact(request_path=material_request_path, input_path=corrected_input)
    packet = client.draft_material(
        pack=pack,
        candidate=candidate,
        verified_job_text=candidate.description,
    )
    assert packet.candidate_id == candidate.candidate_id
    assert material_receipt_path.exists()

    with pytest.raises(ValueError, match="inputs or bindings changed"):
        client.draft_material(
            pack=pack,
            candidate=candidate,
            verified_job_text=candidate.description + " changed",
        )

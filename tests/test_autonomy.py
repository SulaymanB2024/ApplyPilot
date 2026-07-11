from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from applypilot import config
from applypilot.autonomy.batch import AutonomousBatch, BatchDependencies, _mapping_digest
from applypilot.autonomy.chatgpt_web import (
    ChatGPTWebClient,
    ChatGPTContractError,
    parse_chatgpt_json,
    temporary_chat_is_active,
    validate_material_provenance,
)
from applypilot.autonomy import direct_ats as autonomy_direct_ats
from applypilot.autonomy.direct_ats import DirectATSDiscovery
from applypilot.autonomy.context import build_context_pack, candidate_profile_from_data
from applypilot.autonomy.facts import (
    FactCorrection,
    FactState,
    build_fact_ledger,
    load_corrections,
    require_confirmed_facts,
    validate_artifact_against_ledger,
)
from applypilot.autonomy.form_review import ReadOnlyFormReviewer
from applypilot.autonomy.first_party import FetchResponse, FirstPartyVerifier
from applypilot.autonomy.models import (
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
from applypilot.autonomy.runner import require_approved_fact_digest
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


def test_generic_employer_url_requires_conservative_company_domain_match():
    class StaticTransport:
        @staticmethod
        def get(url, **_kwargs):
            return FetchResponse(200, url, "<html><body>Product Analyst Intern</body></html>")

    ledger = UsageLedger(run_id="verify-domain", budget=FunnelBudget())
    verifier = FirstPartyVerifier(
        ledger=ledger,
        transport=StaticTransport(),
        trusted_hosts={"careers.examplelabs.com"},
    )
    matching = role(
        company="Example Labs",
        official_url="https://careers.examplelabs.com/jobs/123",
    )
    deceptive = role(
        company="Example Labs",
        official_url="https://examplelabs.jobs-portal.com/jobs/123",
    )

    assert verifier.verify(matching).first_party is True
    assert verifier.verify(deceptive).first_party is False


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
        job_text="Python product analytics",
        fact_ledger=ledger,
    )
    serialized = json.dumps(pack.to_dict())

    assert "Test Candidate" not in serialized
    assert "Sensitive demographic" not in serialized
    assert "Sensitive veteran" not in serialized
    assert "Python product analytics" in serialized


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
    request_path = next(out.glob("*/chatgpt_discovery_request.json"))
    request_text = request_path.read_text(encoding="utf-8")
    fact_text = next(out.glob("*/fact_ledger.json")).read_text(encoding="utf-8")
    assert len(request_text) < 12_000
    assert "candidate@example.com" not in request_text
    assert "555-0100" not in request_text
    assert "secret-sentinel" not in request_text
    assert "secret-sentinel" not in fact_text
    assert "personal.password" not in fact_text

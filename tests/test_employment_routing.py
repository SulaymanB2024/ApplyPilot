from __future__ import annotations

from datetime import datetime, timezone

from applypilot.autonomy.batch import AutonomousBatch, BatchDependencies
from applypilot.autonomy.context import build_context_pack
from applypilot.autonomy.first_party import FetchResponse, FirstPartyVerifier, TrustedFirstPartySource
from applypilot.autonomy.models import CandidateProfile, RoleCandidate
from applypilot.autonomy.policy import Decision, FunnelBudget, eligibility_gate, freshness_gate
from applypilot.autonomy.telemetry import UsageLedger
from applypilot.cli import _ensure_off_posting_research
from applypilot.employment import ApplicationSurface, OpportunityKind, classify_opportunity
from applypilot.opportunities.models import (
    OpportunityEvidence,
    OpportunityLead,
    OpportunityRoute,
    OpportunitySignal,
    OpportunityStatus,
)
from applypilot.opportunities.outreach import build_outreach_draft
from applypilot.opportunities.research import verify_opportunity


def test_known_task_platform_is_not_a_job_even_with_an_intern_title() -> None:
    role = RoleCandidate(
        company="Outlier",
        title="AI Product Analytics Intern",
        official_url="https://app.outlier.ai/signup",
        description="Create a profile and pick up paid tasks.",
        location="Remote, United States",
    )

    classification = classify_opportunity(
        title=role.title,
        description=role.description,
        official_url=role.official_url,
    )

    assert classification.kind is OpportunityKind.MICROTASK_PLATFORM
    assert eligibility_gate(role, CandidateProfile()).decision is Decision.REJECT


def test_real_employee_human_data_role_is_not_blocked_by_incidental_words() -> None:
    role = RoleCandidate(
        company="Example",
        title="Product Manager Intern, Human Data",
        official_url="https://job-boards.greenhouse.io/example/jobs/123",
        description="Paid product management internship for an early-career employee.",
        location="Remote, United States",
        opportunity_kind=OpportunityKind.POSTED_EMPLOYMENT,
        application_surface=ApplicationSurface.PROVIDER_REQUISITION,
        requisition_id="123",
    )

    assert eligibility_gate(role, CandidateProfile()).decision is Decision.ACCEPT


def test_data_labeling_function_is_rejected_even_on_a_real_ats() -> None:
    role = RoleCandidate(
        company="Example",
        title="Data Labeling Intern",
        official_url="https://job-boards.greenhouse.io/example/jobs/456",
        description="Paid employee internship.",
        location="Remote, United States",
        opportunity_kind=OpportunityKind.POSTED_EMPLOYMENT,
        application_surface=ApplicationSurface.PROVIDER_REQUISITION,
        requisition_id="456",
    )

    decision = eligibility_gate(role, CandidateProfile())

    assert decision.decision is Decision.REJECT
    assert decision.reason_codes == ("excluded_data_labeling_function",)


def test_html_title_match_requires_jobposting_or_application_surface() -> None:
    role = RoleCandidate(
        company="Example Labs",
        title="Product Analytics Intern",
        official_url="https://careers.examplelabs.com/jobs/123",
        description="Product analytics internship.",
    )

    class Transport:
        def __init__(self, page: str) -> None:
            self.page = page

        def get(self, url: str, **_kwargs: object) -> FetchResponse:
            return FetchResponse(200, url, self.page)

    trusted = (
        TrustedFirstPartySource(
            company="Example Labs",
            host="careers.examplelabs.com",
            path_prefix="/jobs",
            source_kind="employer_careers",
        ),
    )
    plain = FirstPartyVerifier(
        ledger=UsageLedger(run_id="plain", budget=FunnelBudget()),
        transport=Transport("<html><body>Product Analytics Intern</body></html>"),
        trusted_sources=trusted,
    ).verify(role)
    structured = FirstPartyVerifier(
        ledger=UsageLedger(run_id="structured", budget=FunnelBudget()),
        transport=Transport(
            '<html><script type="application/ld+json">'
            '{"@type":"JobPosting","title":"Product Analytics Intern"}'
            "</script><body>Product Analytics Intern</body></html>"
        ),
        trusted_sources=trusted,
    ).verify(role)

    assert plain.resolved is True
    assert freshness_gate(plain).decision is Decision.REJECT
    assert structured.application_surface is ApplicationSurface.JOB_POSTING_STRUCTURED_DATA
    assert freshness_gate(structured).decision is Decision.ACCEPT
    assert freshness_gate(structured).reason_codes == ("live_application_surface_without_dates",)


def test_empty_discovery_is_a_valid_batch_outcome() -> None:
    class EmptyDiscovery:
        @staticmethod
        def find_roles(**_kwargs: object) -> list[RoleCandidate]:
            return []

    class UnusedVerifier:
        @staticmethod
        def verify(_candidate: RoleCandidate):
            raise AssertionError("empty discovery must not invoke verification")

    class UnusedMaterials:
        @staticmethod
        def draft_material(**_kwargs: object):
            raise AssertionError("empty discovery must not invoke materials")

    pack = build_context_pack({}, job_text="product analytics internship")
    result = AutonomousBatch(
        run_id="empty-valid",
        profile=CandidateProfile(),
        context_pack=pack,
        dependencies=BatchDependencies(
            discovery=EmptyDiscovery(),
            verifier=UnusedVerifier(),
            materials=UnusedMaterials(),
        ),
    ).run(query="product analytics internships")

    assert result.status == "no_eligible_verified_roles"
    assert result.discoveries == []
    assert result.blockers == []


def test_general_interest_form_has_a_separate_application_intent() -> None:
    now = datetime.now(timezone.utc)
    form_url = "https://example.com/careers/general-interest"
    evidence = OpportunityEvidence(
        evidence_type="general_interest_form",
        source_url=form_url,
        source_title="General interest",
        publisher="Example",
        observed_at=now.isoformat(),
        is_primary=True,
        claim="Official general-interest application form.",
    )
    lead = OpportunityLead(
        lead_id="opp-general-interest",
        company_name="Example",
        company_url="https://example.com",
        company_domain="example.com",
        signal=OpportunitySignal.GENERAL_GROWTH,
        route=OpportunityRoute.GENERAL_INTEREST_APPLICATION,
        status=OpportunityStatus.OBSERVED,
        evidence=(evidence,),
        general_application_url=form_url,
    )

    decision = verify_opportunity(lead, now=now)
    draft = build_outreach_draft(
        lead.with_status(decision.status),
        profile={"experience": {"target_role": "product analytics internship"}},
        channel="contact_form",
        now=now,
    )

    assert decision.status is OpportunityStatus.VERIFIED
    assert draft.intent == "general_interest_application"
    assert draft.recipient == form_url
    assert "not tied to a currently posted requisition" in draft.body


def test_empty_posted_funnel_queues_one_idempotent_off_posting_route(
    monkeypatch,
    tmp_path,
) -> None:
    from applypilot import config

    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path / "app-data"))
    monkeypatch.setattr(
        config,
        "load_profile",
        lambda: {"experience": {"target_role": "product analytics internship"}},
    )

    first = _ensure_off_posting_research(parent_run_id="posted-run-1")
    second = _ensure_off_posting_research(parent_run_id="posted-run-1")

    assert first == second
    assert first["status"] == "awaiting_browser"
    assert first["route_priority"] == [
        "general_interest_application",
        "speculative_outreach",
    ]
    assert first["external_contact_attempted"] is False

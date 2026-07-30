from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from applypilot.autonomy.context import candidate_profile_from_data
from applypilot.autonomy.first_party import CachedFirstPartyVerifier
from applypilot.autonomy.matching import assess_fit, location_preference_match
from applypilot.autonomy.materials import build_evidence_bound_resume
from applypilot.autonomy.models import CandidateProfile, FreshnessEvidence, RoleCandidate
from applypilot.autonomy.policy import Decision, eligibility_gate


def candidate(**overrides: object) -> RoleCandidate:
    values = {
        "company": "Example",
        "title": "Business Analytics Intern - Summer 2027",
        "official_url": "https://jobs.example.com/123",
        "location": "New York, NY",
        "description": "Use Python and SQL for business analytics.",
    }
    values.update(overrides)
    return RoleCandidate(**values)


def evidence(role: RoleCandidate) -> FreshnessEvidence:
    return FreshnessEvidence(
        official_url=role.official_url,
        fetched_at=datetime.now(timezone.utc),
        first_party=True,
        resolved=True,
        open_state=True,
        title=role.title,
        description=role.description,
    )


def test_search_locations_do_not_become_confirmed_profile_preferences() -> None:
    profile = candidate_profile_from_data(
        {
            "experience": {"target_role": "data analytics internship"},
            "skills_boundary": {"languages": ["Python", "SQL"]},
        },
        search_config={
            "locations": [
                {"location": "Remote", "remote": True},
                {"location": "Austin", "remote": False},
                {"location": "New York", "remote": False},
            ]
        },
    )

    assert profile.preferred_locations == ()
    assert profile.target_families == ("data_analytics",)
    assert profile.skills == ("python", "sql")


def test_missing_location_preferences_do_not_gain_defaults() -> None:
    profile = candidate_profile_from_data(
        {"experience": {"target_role": "data analytics internship"}},
        search_config={"discovery_mode": "direct_sources"},
    )

    assert profile.preferred_locations == ()


def test_location_state_names_and_postal_abbreviations_match() -> None:
    assert location_preference_match(
        "San Francisco, California",
        ("San Francisco, CA",),
    ) == (True, "san francisco california")
    assert location_preference_match(
        "New York, New York; Chicago, Illinois",
        ("New York, NY", "Chicago, IL"),
    )[0] is True
    assert location_preference_match(
        "Chicago, IL",
        ("Major U.S. markets for strong opportunities",),
    )[0] is True
    assert location_preference_match(
        "Jersey City, NJ",
        ("Major U.S. markets for strong opportunities",),
    )[0] is True
    assert location_preference_match(
        "Dallas, TX",
        ("Elsewhere in Texas",),
    )[0] is True
    assert location_preference_match(
        "Kalamazoo, MI",
        ("Major U.S. markets for strong opportunities",),
    )[0] is False
    assert location_preference_match("Austin, TX", ()) == (None, "")


def test_explicit_technical_degree_requirement_stays_review_only() -> None:
    role = candidate(
        title="Associate Product Manager Intern - Summer 2027",
        description=(
            "Pursuing a BS or MS in Computer Science or a similar technical field. "
            "Own product roadmaps and customer research."
        ),
    )
    profile = CandidateProfile(
        preferred_locations=("new york",),
        education_evidence=("BBA and BA candidate", "McCombs School of Business"),
    )

    decision = eligibility_gate(role, profile)

    assert decision.decision is Decision.REVIEW
    assert decision.reason_codes == ("education_requirement_unverified",)


def test_matching_business_degree_requirement_can_proceed() -> None:
    role = candidate(
        description=(
            "Pursuing a degree with a major such as Business Management, Economics, "
            "or Marketing. Business analytics internship."
        ),
    )
    profile = CandidateProfile(
        preferred_locations=("new york",),
        education_evidence=("BBA candidate",),
    )

    assert eligibility_gate(role, profile).decision is Decision.ACCEPT


def test_age_requirement_is_not_treated_as_experience() -> None:
    role = candidate(
        description=(
            "Applicants must be at least 18 years old and 18 or more years of age. "
            "No experience required."
        )
    )

    decision = eligibility_gate(role, CandidateProfile(preferred_locations=("new york",)))

    assert decision.decision is Decision.REVIEW
    assert decision.reason_codes == ("age_18_status_unconfirmed",)
    assert "experience_requirement_exceeds_profile" not in decision.reason_codes

    confirmed = eligibility_gate(
        role,
        CandidateProfile(preferred_locations=("new york",), is_at_least_18=True),
    )
    assert confirmed.decision is Decision.ACCEPT


def test_explicit_sponsorship_restriction_is_fact_gated() -> None:
    role = candidate(
        description=(
            "Business analytics internship. We are unable to consider candidates that "
            "will require visa sponsorship now or in the future."
        )
    )

    unknown = eligibility_gate(role, CandidateProfile(preferred_locations=("new york",)))
    incompatible = eligibility_gate(
        role,
        CandidateProfile(preferred_locations=("new york",), require_sponsorship=True),
    )
    compatible = eligibility_gate(
        role,
        CandidateProfile(preferred_locations=("new york",), require_sponsorship=False),
    )

    assert unknown.reason_codes == ("sponsorship_status_unconfirmed",)
    assert incompatible.decision is Decision.REJECT
    assert incompatible.reason_codes == ("sponsorship_requirement_incompatible",)
    assert compatible.decision is Decision.ACCEPT


def test_explicit_work_authorization_requirement_is_fact_gated() -> None:
    role = candidate(
        description="Business analytics internship. Must be authorized to work in the United States."
    )

    unknown = eligibility_gate(role, CandidateProfile(preferred_locations=("new york",)))
    incompatible = eligibility_gate(
        role,
        CandidateProfile(
            preferred_locations=("new york",),
            legally_authorized_to_work=False,
        ),
    )

    assert unknown.reason_codes == ("work_authorization_unconfirmed",)
    assert incompatible.decision is Decision.REJECT
    assert incompatible.reason_codes == ("work_authorization_incompatible",)


def test_generic_finance_analyst_is_rejected_without_target_family() -> None:
    role = candidate(
        title="2027 Finance Summer Analyst",
        description="Corporate finance and accounting rotation.",
    )

    decision = eligibility_gate(role, CandidateProfile(preferred_locations=("new york",)))

    assert decision.decision is Decision.REJECT
    assert decision.reason_codes == ("out_of_scope_function",)


def test_operations_finance_intern_is_supported_technical_business_role() -> None:
    role = candidate(
        title="Operations Finance Rotational Program Summer Internship",
        location="Seattle, WA",
        description=(
            "Use data-intensive analysis, capital planning, reporting, and process "
            "improvement to support finance and operations teams."
        ),
    )

    decision = eligibility_gate(
        role,
        CandidateProfile(
            preferred_locations=("seattle",),
            target_families=("technical_business",),
        ),
    )

    assert decision.decision is Decision.ACCEPT
    assert "target_role_family" in decision.reason_codes


def test_investment_banking_analyst_internship_is_supported_venture_role() -> None:
    role = candidate(
        title="Investment Banking Analyst Internship | Summer 2027",
        location="Raleigh, NC",
        description=(
            "Full-time summer internship for an undergraduate pursuing a bachelor's "
            "degree in finance, accounting, or business. Perform company research, "
            "financial analysis, financial modeling, and transaction marketing materials."
        ),
    )

    decision = eligibility_gate(
        role,
        CandidateProfile(
            preferred_locations=("raleigh",),
            target_families=("venture",),
            education_evidence=("BBA candidate",),
        ),
    )

    assert decision.decision is Decision.ACCEPT
    assert "target_role_family" in decision.reason_codes


def test_manager_title_is_rejected_even_when_product_matches() -> None:
    role = candidate(title="Product Analytics Manager", description="Product analytics leadership role.")

    decision = eligibility_gate(role, CandidateProfile(preferred_locations=("new york",)))

    assert decision.decision is Decision.REJECT
    assert "senior_title" in decision.reason_codes


def test_product_manager_intern_is_not_mistaken_for_people_manager() -> None:
    role = candidate(
        title="Associate Product Manager Intern - Summer 2027",
        description="Own a roadmap using data and customer research.",
    )

    decision = eligibility_gate(role, CandidateProfile(preferred_locations=("new york",)))

    assert decision.decision is Decision.ACCEPT
    assert "senior_title" not in decision.reason_codes


def test_strategy_intern_is_a_supported_technical_business_role() -> None:
    role = candidate(
        title="Strategy Intern - Summer 2027",
        location="Chicago, IL",
        description="Evaluate market opportunities and build cost and scenario models.",
    )

    decision = eligibility_gate(role, CandidateProfile(preferred_locations=("chicago",)))

    assert decision.decision is Decision.ACCEPT
    assert "target_role_family" in decision.reason_codes


def test_commercial_strategy_internship_is_a_supported_technical_business_role() -> None:
    role = candidate(
        title="Summer 2027 Intern - Americas Division [UG/Masters]",
        location="Chicago, IL",
        description=(
            "Commercial-growth consulting internship supporting commercial strategy, "
            "go-to-market analysis, pricing models, and customer segmentation."
        ),
    )

    decision = eligibility_gate(role, CandidateProfile(preferred_locations=("chicago",)))

    assert decision.decision is Decision.ACCEPT
    assert "target_role_family" in decision.reason_codes


def test_turnaround_restructuring_internship_is_a_supported_finance_strategy_role() -> None:
    role = candidate(
        company="AlixPartners",
        title="Turnaround & Restructuring Summer Analyst",
        location="New York, NY",
        description=(
            "Summer 2027 restructuring advisory internship supporting financial and "
            "operational analyses."
        ),
    )

    decision = eligibility_gate(role, CandidateProfile(preferred_locations=("new york",)))

    assert decision.decision is Decision.ACCEPT
    assert "target_role_family" in decision.reason_codes


def test_marketing_leadership_development_is_a_supported_growth_role() -> None:
    role = candidate(
        title="Marketing Leadership Development Program - Summer Analyst",
        location="New York, NY",
        description="Analyze campaign performance, customer trends, and marketing data.",
    )

    decision = eligibility_gate(role, CandidateProfile(preferred_locations=("new york",)))

    assert decision.decision is Decision.ACCEPT
    assert "target_role_family" in decision.reason_codes


def test_business_operations_intern_is_a_supported_technical_business_role() -> None:
    role = candidate(
        title="Business Operations Intern (Summer 2027)",
        location="New York, NY",
        description="Analyze operational processes and support cross-functional improvements.",
    )

    decision = eligibility_gate(role, CandidateProfile(preferred_locations=("new york",)))

    assert decision.decision is Decision.ACCEPT
    assert "target_role_family" in decision.reason_codes


def test_business_analysts_internship_is_a_supported_technical_business_role() -> None:
    role = candidate(
        title="2027 Summer Analyst Internship - Corporate Functions, Technology",
        location="Jersey City, NJ",
        description=(
            "All majors accepted. Business Analysts analyze products and workflows "
            "and convert them into specifications for developers."
        ),
    )

    decision = eligibility_gate(role, CandidateProfile(preferred_locations=("jersey city",)))

    assert decision.decision is Decision.ACCEPT
    assert "target_role_family" in decision.reason_codes


def test_investor_relations_intern_is_supported_venture_family() -> None:
    role = candidate(
        title="Investor Relations Intern (Summer 2027)",
        location="New York, NY",
        description=(
            "Support investor communications, financial-market reporting, CRM workflows, "
            "and capital development materials."
        ),
    )

    decision = eligibility_gate(
        role,
        CandidateProfile(
            preferred_locations=("new york",),
            target_families=("venture",),
        ),
    )

    assert decision.decision is Decision.ACCEPT
    assert "target_role_family" in decision.reason_codes


def test_risk_technology_intern_is_outside_target_even_with_analyst_language() -> None:
    role = candidate(
        title="Risk Technology Analyst Intern - Summer 2027",
        description="Technology risk analytics and controls.",
    )

    decision = eligibility_gate(role, CandidateProfile(preferred_locations=("new york",)))

    assert decision.decision is Decision.REJECT
    assert decision.reason_codes == ("out_of_scope_function",)


def test_fit_score_has_human_readable_components() -> None:
    role = candidate()
    profile = CandidateProfile(
        preferred_locations=("new york",),
        target_families=("data_analytics",),
        skills=("python", "sql", "tableau"),
    )

    fit = assess_fit(role, evidence(role), profile)

    assert fit.qualifies
    assert fit.score >= 90
    assert fit.matched_families == ("data_analytics",)
    assert fit.matched_skills == ("python", "sql")
    assert any("target role family" in reason for reason in fit.inclusion_reasons)


def test_first_party_cache_prevents_duplicate_fetches(tmp_path) -> None:
    role = candidate()

    class Delegate:
        calls = 0

        def verify(self, candidate: RoleCandidate) -> FreshnessEvidence:
            self.calls += 1
            return evidence(candidate)

    delegate = Delegate()
    verifier = CachedFirstPartyVerifier(delegate, cache_dir=tmp_path / "verification")

    first = verifier.verify(role)
    second = verifier.verify(role)

    assert first == second
    assert delegate.calls == 1
    cache_path = tmp_path / "verification" / f"{role.candidate_id}.v3.json"
    assert cache_path.stat().st_mode & 0o777 == 0o600


def test_role_resume_only_reorders_exact_source_claims() -> None:
    source = "\n".join(
        (
            "TEST CANDIDATE",
            "EXPERIENCE",
            "Example Labs",
            "• Built a customer research program.",
            "• Built Python and SQL analytics dashboards.",
        )
    )

    output, provenance = build_evidence_bound_resume(
        source,
        verified_job_text="Python SQL data analytics internship",
    )

    assert output.splitlines()[-2:] == [
        "• Built Python and SQL analytics dashboards.",
        "• Built a customer research program.",
    ]
    assert Counter(output.splitlines()) == Counter(source.splitlines())
    assert provenance["claims_rewritten"] is False
    assert provenance["claims_added"] is False
    assert provenance["line_order_changed"] is True

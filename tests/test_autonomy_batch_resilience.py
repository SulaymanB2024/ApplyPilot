from __future__ import annotations

from datetime import date, datetime, timezone

from applypilot.autonomy.batch import AutonomousBatch, BatchDependencies
from applypilot.autonomy.context import build_context_pack
from applypilot.autonomy.models import (
    CandidateProfile,
    FreshnessEvidence,
    MaterialPacket,
    MaterialParagraph,
    RoleCandidate,
)
from applypilot.autonomy.policy import FunnelBudget, RunPolicy


PROFILE = {
    "experience": {"current_title": "Student product analyst"},
    "skills_boundary": {"programming_languages": ["Python", "SQL"]},
}


def _candidate(index: int, *, source: str = "chatgpt_web") -> RoleCandidate:
    return RoleCandidate(
        company=f"Example {index}",
        title="Product Analyst Intern",
        official_url=f"https://jobs.example.com/roles/{index}",
        source=source,
        location="Remote, United States",
        description="Entry-level internship using Python and SQL. 0-2 years accepted.",
        required_experience_min=0,
        required_experience_max=2,
    )


def _fresh(candidate: RoleCandidate) -> FreshnessEvidence:
    return FreshnessEvidence(
        official_url=candidate.official_url,
        fetched_at=datetime.now(timezone.utc),
        first_party=True,
        resolved=True,
        open_state=True,
        posted_date=date.today(),
        status_code=200,
        title=candidate.title,
        description=candidate.description,
        evidence=("official ATS response",),
    )


class Discovery:
    def __init__(self, candidates: list[RoleCandidate]) -> None:
        self.candidates = candidates
        self.calls = 0

    def find_roles(self, **_kwargs) -> list[RoleCandidate]:
        self.calls += 1
        return list(self.candidates)


class Verifier:
    def __init__(self, *, fail_candidate_id: str = "") -> None:
        self.fail_candidate_id = fail_candidate_id
        self.calls: list[str] = []

    def verify(self, candidate: RoleCandidate) -> FreshnessEvidence:
        self.calls.append(candidate.candidate_id)
        if candidate.candidate_id == self.fail_candidate_id:
            raise RuntimeError("temporary verification outage " + "x" * 400)
        return _fresh(candidate)


class Materials:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.calls: list[str] = []

    def draft_material(self, *, candidate: RoleCandidate, **_kwargs) -> MaterialPacket:
        self.calls.append(candidate.candidate_id)
        if self.fail_first and len(self.calls) == 1:
            raise ValueError("material provider returned malformed JSON")
        return MaterialPacket(
            candidate_id=candidate.candidate_id,
            paragraphs=(MaterialParagraph("Python product analysis.", ("F01",)),),
        )


class RaisingThenSuccessfulFormReview:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def dry_run(self, *, candidate: RoleCandidate, **_kwargs) -> dict[str, object]:
        self.calls.append(candidate.candidate_id)
        if len(self.calls) == 1:
            raise RuntimeError("form browser crashed")
        return {"status": "dry_run_verified", "submitted": False}


class BlockedThenSuccessfulFormReview:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def dry_run(self, *, candidate: RoleCandidate, **_kwargs) -> dict[str, object]:
        self.calls.append(candidate.candidate_id)
        if len(self.calls) == 1:
            return {"status": "blocked", "reason": "captcha_detected"}
        return {"status": "form_surface_reviewed", "submitted": False}


def _resilience_policy() -> RunPolicy:
    return RunPolicy(
        budget=FunnelBudget(
            discoveries=4,
            first_party_verifications=4,
            material_packets=3,
            form_dry_runs=2,
        )
    )


def test_empty_primary_discovery_uses_audited_direct_ats_fallback() -> None:
    candidate = _candidate(1, source="direct_ats")
    primary = Discovery([])
    fallback = Discovery([candidate])
    pack = build_context_pack(PROFILE, job_text=candidate.description)

    result = AutonomousBatch(
        run_id="empty-primary-fallback",
        profile=CandidateProfile(),
        context_pack=pack,
        dependencies=BatchDependencies(
            discovery=primary,
            fallback_discovery=fallback,
            verifier=Verifier(),
            materials=Materials(),
        ),
    ).run(query="quality product internships")

    assert result.status == "review_ready"
    assert primary.calls == 1
    assert fallback.calls == 1
    assert result.discoveries[0]["discovery_source"] == "direct_ats"
    assert "primary_empty_result" in result.discoveries[0]["fallback_reason"]
    assert result.discoveries[0]["source_attempts"] == [
        {
            "source": "chatgpt_web",
            "status": "failed",
            "reason": "primary_empty_result",
        },
        {
            "source": "direct_ats",
            "status": "ok",
            "reason": "trigger=primary_empty_result;candidate_count=1",
        },
    ]
    assert any(
        blocker["reason_codes"] == ["primary_discovery_empty"]
        for blocker in result.blockers
    )


def test_candidate_failures_are_bounded_and_batch_continues_within_budgets() -> None:
    candidates = [_candidate(index) for index in range(1, 5)]
    verifier = Verifier(fail_candidate_id=candidates[0].candidate_id)
    materials = Materials(fail_first=True)
    forms = RaisingThenSuccessfulFormReview()
    pack = build_context_pack(PROFILE, job_text=candidates[0].description)

    result = AutonomousBatch(
        run_id="candidate-failure-resilience",
        profile=CandidateProfile(),
        context_pack=pack,
        dependencies=BatchDependencies(
            discovery=Discovery(candidates),
            verifier=verifier,
            materials=materials,
            form_review=forms,
        ),
        policy=_resilience_policy(),
    ).run(query="quality product internships")

    assert result.status == "review_ready"
    assert len(verifier.calls) == 4
    assert len(materials.calls) == 3
    assert len(forms.calls) == 2
    assert len(result.materials) == 2
    assert len(result.form_reviews) == 1
    assert result.form_reviews[0]["status"] == "dry_run_verified"
    assert result.final_actions == []
    assert result.usage["counts"]["first_party_verifications"] == 4
    assert result.usage["counts"]["material_packets"] == 3
    assert result.usage["counts"]["form_dry_runs"] == 2

    blockers_by_stage = {blocker["stage"]: blocker for blocker in result.blockers}
    assert blockers_by_stage["verification"]["reason_codes"] == ["verification_failed"]
    assert blockers_by_stage["materials"]["reason_codes"] == [
        "material_generation_failed"
    ]
    assert blockers_by_stage["form_review"]["reason_codes"] == ["form_review_failed"]
    assert all(len(blocker.get("detail", "")) <= 240 for blocker in result.blockers)


def test_blocked_form_is_logged_and_next_packet_is_reviewed() -> None:
    candidates = [_candidate(index) for index in range(1, 3)]
    forms = BlockedThenSuccessfulFormReview()
    pack = build_context_pack(PROFILE, job_text=candidates[0].description)
    policy = RunPolicy(
        budget=FunnelBudget(
            discoveries=2,
            first_party_verifications=2,
            material_packets=2,
            form_dry_runs=2,
        )
    )

    result = AutonomousBatch(
        run_id="blocked-form-resilience",
        profile=CandidateProfile(),
        context_pack=pack,
        dependencies=BatchDependencies(
            discovery=Discovery(candidates),
            verifier=Verifier(),
            materials=Materials(),
            form_review=forms,
        ),
        policy=policy,
    ).run(query="quality product internships")

    assert result.status == "review_ready"
    assert len(forms.calls) == 2
    assert [review["status"] for review in result.form_reviews] == [
        "blocked",
        "form_surface_reviewed",
    ]
    assert any(
        blocker["stage"] == "form_review"
        and blocker["reason_codes"] == ["form_review_incomplete"]
        and "captcha_detected" in blocker["detail"]
        for blocker in result.blockers
    )

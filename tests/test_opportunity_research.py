from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from applypilot import config
from applypilot.autonomy.handoff import active_handoffs, import_response_artifact
from applypilot.autonomy.models import RoleCandidate
from applypilot.observability.events import EventJournal
from applypilot.opportunities.models import (
    OpportunityEvidence,
    OpportunityLead,
    OpportunityRoute,
    OpportunitySignal,
    OpportunityStatus,
)
from applypilot.opportunities.research import (
    OPPORTUNITY_RESEARCH_SCHEMA_VERSION,
    build_research_request,
    consume_research_response,
    opportunity_lead_id,
    promote_posted_job,
    research_bindings,
    validate_research_response,
    verify_opportunity,
    write_research_mission,
)
from applypilot.opportunities.store import OpportunityStore
from applypilot.cli import app

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def evidence(
    evidence_type: str,
    *,
    url: str,
    publisher: str,
    event_date: str = "",
    primary: bool = False,
    observed_at: str = "2026-08-02T10:00:00+00:00",
) -> OpportunityEvidence:
    return OpportunityEvidence(
        evidence_type=evidence_type,
        source_url=url,
        source_title=f"{publisher} evidence",
        publisher=publisher,
        observed_at=observed_at,
        event_date=event_date,
        is_primary=primary,
        claim="Bounded fixture claim.",
    )


def lead(
    *,
    signal: OpportunitySignal = OpportunitySignal.RECENT_FUNDING,
    evidence_items: tuple[OpportunityEvidence, ...] = (),
    route: OpportunityRoute = OpportunityRoute.SPECULATIVE_OUTREACH,
    domain: str = "example.com",
    company_url: str = "https://example.com",
    careers_url: str = "",
    posted_job_url: str = "",
    open_role_count: int | None = None,
) -> OpportunityLead:
    return OpportunityLead(
        lead_id=opportunity_lead_id(domain, signal),
        company_name="Example Labs",
        company_url=company_url,
        company_domain=domain,
        signal=signal,
        route=route,
        status=OpportunityStatus.OBSERVED,
        evidence=evidence_items,
        careers_url=careers_url,
        posted_job_url=posted_job_url,
        open_role_count=open_role_count,
    )


def company_announcement(*, event_date: str = "2026-07-20") -> OpportunityEvidence:
    return evidence(
        "company_announcement",
        url="https://example.com/news/funding",
        publisher="Example Labs",
        event_date=event_date,
        primary=True,
    )


def independent_report(*, event_date: str = "2026-07-20") -> OpportunityEvidence:
    return evidence(
        "independent_report",
        url="https://credible-news.example/report/example-labs",
        publisher="Credible News",
        event_date=event_date,
    )


def form_d_evidence() -> OpportunityEvidence:
    return evidence(
        "sec_form_d",
        url="https://www.sec.gov/Archives/example-form-d",
        publisher="SEC",
        event_date="2026-07-20",
        primary=True,
    )


def test_company_signal_is_not_a_job_candidate():
    opportunity = lead(evidence_items=(company_announcement(), independent_report()))
    assert isinstance(opportunity, OpportunityLead)
    assert not isinstance(opportunity, RoleCandidate)
    assert opportunity.posted_job_url == ""
    assert opportunity.route is OpportunityRoute.SPECULATIVE_OUTREACH


def test_form_d_alone_cannot_claim_company_raised():
    opportunity = lead(evidence_items=(form_d_evidence(),))
    decision = verify_opportunity(opportunity, now=NOW)
    assert decision.status is OpportunityStatus.NEEDS_CORROBORATION
    assert "completed_raise_not_verified" in decision.reasons


def test_recent_raise_requires_dated_primary_and_independent_evidence():
    opportunity = lead(evidence_items=(company_announcement(), independent_report()))
    decision = verify_opportunity(opportunity, now=NOW)
    assert decision.status is OpportunityStatus.VERIFIED
    assert datetime.fromisoformat(decision.signal_date).date() >= (
        NOW.date() - timedelta(days=45)
    )
    assert opportunity.funding_amount is None
    assert opportunity.funding_stage is None


def test_hiring_signal_requires_current_careers_evidence():
    careers = evidence(
        "careers_page",
        url="https://example.com/careers",
        publisher="Example Labs",
        primary=True,
    )
    opportunity = lead(
        signal=OpportunitySignal.ACTIVELY_HIRING,
        evidence_items=(careers,),
        careers_url="https://example.com/careers",
        open_role_count=3,
    )
    assert verify_opportunity(opportunity, now=NOW).status is OpportunityStatus.VERIFIED


def test_directory_only_stale_and_domain_conflicts_cannot_promote():
    directory_only = lead(
        signal=OpportunitySignal.ACTIVELY_HIRING,
        evidence_items=(
            evidence(
                "hiring_directory",
                url="https://www.ycombinator.com/companies/example/jobs",
                publisher="YC",
            ),
        ),
        careers_url="https://example.com/careers",
        open_role_count=3,
    )
    assert verify_opportunity(directory_only, now=NOW).status is OpportunityStatus.NEEDS_CORROBORATION

    stale = lead(
        evidence_items=(
            company_announcement(event_date="2026-05-01"),
            independent_report(event_date="2026-05-01"),
        )
    )
    assert "funding_signal_stale" in verify_opportunity(stale, now=NOW).reasons

    conflict = lead(
        company_url="https://different.example",
        evidence_items=(company_announcement(), independent_report()),
    )
    assert "company_domain_conflict" in verify_opportunity(conflict, now=NOW).reasons


def test_only_verified_real_posting_promotes_to_role_candidate():
    verified = replace(
        lead(
            evidence_items=(company_announcement(), independent_report()),
            route=OpportunityRoute.POSTED_JOB,
            posted_job_url="https://jobs.ashbyhq.com/example/real-role",
        ),
        status=OpportunityStatus.VERIFIED,
    )
    with pytest.raises(ValueError, match="cannot be synthesized"):
        promote_posted_job(verified, title="")
    role = promote_posted_job(verified, title="Product Analytics Intern")
    assert isinstance(role, RoleCandidate)
    assert role.official_url == "https://jobs.ashbyhq.com/example/real-role"

    speculative = replace(verified, route=OpportunityRoute.SPECULATIVE_OUTREACH)
    with pytest.raises(ValueError, match="no verified posted-job route"):
        promote_posted_job(speculative, title="Product Analytics Intern")


def test_research_mission_is_bounded_browser_locked_and_importable(tmp_path):
    request = build_research_request(
        run_id="opportunity-run-1",
        signals=(OpportunitySignal.RECENT_FUNDING, OpportunitySignal.ACTIVELY_HIRING),
        recent_days=45,
        profile={
            "experience": {"target_role": "AI product intern", "industries": ["AI"]},
            "availability": {"preferred_locations": ["Austin, TX"]},
            "personal": {"email": "must-not-leak@example.com"},
        },
    )
    run_dir = tmp_path / request.run_id
    journal = EventJournal(run_dir / "events.ndjson", run_id=request.run_id)
    request_path = write_research_mission(run_dir=run_dir, request=request, journal=journal)
    envelope = json.loads(request_path.read_text(encoding="utf-8"))
    assert envelope["resource_lock"] == "authenticated_browser"
    assert envelope["mission"]["max_leads"] == 25
    assert envelope["mission"]["max_navigations"] == 60
    assert envelope["mission"]["max_seconds"] == 300
    assert "email" not in json.dumps(envelope)
    assert stat.S_IMODE(request_path.stat().st_mode) == 0o600
    assert active_handoffs(run_dir=run_dir, bindings=research_bindings(request))[0].kind == (
        "startup_opportunities"
    )

    raw = lead(evidence_items=(company_announcement(), independent_report())).to_dict()
    raw["lead_id"] = "browser-supplied-id-is-not-trusted"
    response = tmp_path / "response.json"
    response.write_text(
        json.dumps(
            {
                "schema_version": OPPORTUNITY_RESEARCH_SCHEMA_VERSION,
                "kind": "startup_opportunities",
                "request_id": request.request_id,
                "run_id": request.run_id,
                "items": [raw],
            }
        ),
        encoding="utf-8",
    )
    imported = import_response_artifact(request_path=request_path, input_path=response)
    canonical = json.loads(Path(imported["response_path"]).read_text(encoding="utf-8"))
    assert canonical["items"][0]["lead_id"] == opportunity_lead_id(
        "example.com", OpportunitySignal.RECENT_FUNDING
    )
    with OpportunityStore(tmp_path / "opportunities.sqlite3") as store:
        store.start_run(request.run_id, request.to_dict(), request_path=request_path)
        consumed = consume_research_response(
            request_path=request_path,
            store=store,
            journal=journal,
            now=NOW,
        )
        assert consumed["counts"] == {"verified": 1}
        assert store.run_status(request.run_id)["status"] == "complete"
    assert active_handoffs(run_dir=run_dir, bindings=research_bindings(request)) == ()


def test_response_deduplicates_domains_and_store_persists_decision_history(tmp_path):
    request = build_research_request(
        run_id="opportunity-run-2",
        signals=(OpportunitySignal.RECENT_FUNDING,),
        recent_days=45,
        profile={},
    )
    envelope = {
        "request_id": request.request_id,
        "run_id": request.run_id,
        "mission": request.to_dict(),
    }
    item = lead(evidence_items=(company_announcement(), independent_report())).to_dict()
    payload = {
        "schema_version": OPPORTUNITY_RESEARCH_SCHEMA_VERSION,
        "kind": "startup_opportunities",
        "request_id": request.request_id,
        "run_id": request.run_id,
        "items": [item, item],
    }
    with pytest.raises(ValueError, match="duplicate company domain"):
        validate_research_response(payload, request=envelope)

    with OpportunityStore(tmp_path / "opportunities.sqlite3") as store:
        request_path = tmp_path / "mission.request.json"
        request_path.write_text("{}", encoding="utf-8")
        store.start_run(request.run_id, request.to_dict(), request_path=request_path)
        opportunity = lead(evidence_items=(company_announcement(), independent_report()))
        decision = verify_opportunity(opportunity, now=NOW)
        persisted = store.persist_lead(request.run_id, opportunity, decision)
        store.finish_run(request.run_id, status="complete")
        record = store.get_lead(persisted.lead_id)
        assert record["status"] == "verified"
        assert len(record["evidence"]) == 2
        assert record["transitions"][0]["to_status"] == "verified"
        assert store.run_status(request.run_id)["lead_counts"] == {"verified": 1}


def test_opportunity_cli_creates_read_only_research_mission(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path / "app-data"))
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(
        config,
        "load_profile",
        lambda: {
            "experience": {"target_role": "AI product intern"},
            "availability": {"preferred_locations": ["Austin, TX"]},
        },
    )
    runner = CliRunner()
    created = runner.invoke(
        app,
        [
            "opportunities",
            "discover",
            "--signal",
            "recently-funded",
            "--recent-days",
            "45",
        ],
    )
    assert created.exit_code == 0, created.output
    result = json.loads(created.stdout)
    assert result["status"] == "awaiting_browser"
    assert result["external_contact_attempted"] is False
    assert (tmp_path / "app-data" / "opportunities.sqlite3").exists()

    observed = runner.invoke(app, ["opportunities", "status", result["run_id"]])
    assert observed.exit_code == 0, observed.output
    status = json.loads(observed.stdout)
    assert status["browser_state"] == "awaiting_response"

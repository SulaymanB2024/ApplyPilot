from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from applypilot.observability.events import EventJournal
from applypilot.opportunities.models import (
    OpportunityDecision,
    OpportunityEvidence,
    OpportunityLead,
    OpportunityRoute,
    OpportunitySignal,
    OpportunityStatus,
)
from applypilot.opportunities.outreach import (
    DEFAULT_OUTREACH_SENDER,
    OutreachGateError,
    build_outreach_draft,
    persist_draft,
)
from applypilot.opportunities.research import opportunity_lead_id
from applypilot.opportunities.send_handoff import (
    OutreachAuthorizationError,
    build_outreach_authorization,
    consume_send_response,
    execute_send_handoff,
    queue_send_handoff,
    write_outreach_authorization,
)
from applypilot.opportunities.store import OpportunityStore

PROFILE = {
    "experience": {"target_role": "AI product intern"},
    "skills_boundary": {"technical": ["Python", "SQL", "product research"]},
}


def _evidence(
    evidence_type: str,
    *,
    url: str,
    claim: str,
    primary: bool,
) -> OpportunityEvidence:
    return OpportunityEvidence(
        evidence_type=evidence_type,
        source_url=url,
        source_title="Fixture evidence",
        publisher="Example Labs" if primary else "Independent News",
        observed_at="2026-08-02T12:00:00+00:00",
        event_date="2026-07-20",
        is_primary=primary,
        claim=claim,
    )


def verified_lead(
    *,
    contact_route: str = "careers@example.com",
    contact_evidence: tuple[OpportunityEvidence, ...] | None = None,
) -> OpportunityLead:
    contacts = (
        (
            _evidence(
                "company_mailbox",
                url="https://example.com/contact",
                claim="Public contact mailbox: careers@example.com",
                primary=True,
            ),
        )
        if contact_evidence is None
        else contact_evidence
    )
    return OpportunityLead(
        lead_id=opportunity_lead_id("example.com", OpportunitySignal.RECENT_FUNDING),
        company_name="Example Labs",
        company_url="https://example.com",
        company_domain="example.com",
        signal=OpportunitySignal.RECENT_FUNDING,
        route=OpportunityRoute.SPECULATIVE_OUTREACH,
        status=OpportunityStatus.VERIFIED,
        evidence=(
            _evidence(
                "company_announcement",
                url="https://example.com/news/funding",
                claim="The company announced financing.",
                primary=True,
            ),
            _evidence(
                "independent_report",
                url="https://independent.example/example-funding",
                claim="Independent reporting corroborated the financing.",
                primary=False,
            ),
        ),
        signal_date="2026-07-20",
        funding_amount=None,
        funding_stage=None,
        contact_route=contact_route,
        contact_evidence=contacts,
    )


def _persist_verified_lead(store: OpportunityStore, tmp_path: Path) -> OpportunityLead:
    opportunity = verified_lead()
    request_path = tmp_path / "research.request.json"
    request_path.write_text("{}", encoding="utf-8")
    store.start_run("research-run", {"fixture": True}, request_path=request_path)
    decision = OpportunityDecision(
        status=OpportunityStatus.VERIFIED,
        reasons=("fixture_verified",),
        signal_date=opportunity.signal_date,
    )
    return store.persist_lead("research-run", opportunity, decision)


def _persist_draft(
    store: OpportunityStore,
    tmp_path: Path,
    *,
    lead: OpportunityLead,
    now: datetime | None = None,
):
    draft = build_outreach_draft(lead, profile=PROFILE, now=now)
    path = tmp_path / "drafts" / f"{draft.draft_id}.json"
    persist_draft(path, draft)
    store.persist_draft(draft, artifact_path=path)
    return draft, path


def test_unverified_or_guessed_contact_is_not_draftable():
    opportunity = verified_lead(
        contact_route="first.last@example.com",
        contact_evidence=(),
    )
    with pytest.raises(OutreachGateError, match="verified contact route required"):
        build_outreach_draft(opportunity, profile=PROFILE)


def test_draft_cannot_claim_an_opening_or_unverified_funding():
    draft = build_outreach_draft(verified_lead(), profile=PROFILE)
    assert "opening" not in draft.body.lower()
    assert "$" not in draft.body
    assert draft.intent == "inquiry"
    assert draft.unsupported_claims == ()


def test_draft_is_local_private_and_not_a_send(tmp_path):
    with OpportunityStore(tmp_path / "opportunities.sqlite3") as store:
        opportunity = _persist_verified_lead(store, tmp_path)
        draft, path = _persist_draft(store, tmp_path, lead=opportunity)
        assert store.get_draft(draft.draft_id)["status"] == "draft_ready"
        assert store.sent_count() == 0
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_changed_or_consumed_authorization_cannot_queue_send(tmp_path):
    now = datetime.now(timezone.utc)
    with OpportunityStore(tmp_path / "opportunities.sqlite3") as store:
        opportunity = _persist_verified_lead(store, tmp_path)
        draft, _ = _persist_draft(store, tmp_path, lead=opportunity, now=now)
        authorization = build_outreach_authorization(
            (draft,), sender=DEFAULT_OUTREACH_SENDER, channel="email", now=now
        )
        authorization_path = write_outreach_authorization(
            tmp_path / "authorization.json", authorization
        )
        assert stat.S_IMODE(authorization_path.stat().st_mode) == 0o600
        store.record_authorization(authorization, artifact_path=authorization_path)
        run_dir = tmp_path / "send-run"
        journal = EventJournal(
            run_dir / "events.ndjson", run_id=authorization.authorization_id
        )
        request_path = queue_send_handoff(
            authorization_path=authorization_path,
            store=store,
            run_dir=run_dir,
            journal=journal,
            now=now,
        )
        assert request_path.exists()
        assert store.authorization_status(authorization.authorization_id)["status"] == "consumed"
        with pytest.raises(OutreachAuthorizationError, match="already consumed"):
            queue_send_handoff(
                authorization_path=authorization_path,
                store=store,
                run_dir=run_dir,
                journal=journal,
                now=now,
            )

        tampered = json.loads(authorization_path.read_text(encoding="utf-8"))
        tampered["items"][0][2] = "0" * 64
        tampered_path = tmp_path / "tampered-authorization.json"
        tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(OutreachAuthorizationError, match="digest mismatch"):
            queue_send_handoff(
                authorization_path=tampered_path,
                store=store,
                run_dir=tmp_path / "other-send-run",
                journal=EventJournal(
                    tmp_path / "other-send-run" / "events.ndjson",
                    run_id=authorization.authorization_id,
                ),
                now=now,
            )


class _FakeAdapter:
    def __init__(self, *, timeout: bool = False) -> None:
        self.timeout = timeout
        self.calls = 0

    def send(self, _draft):
        self.calls += 1
        if self.timeout:
            raise TimeoutError("ambiguous provider boundary")
        return {
            "status": "provider_accepted",
            "provider_receipt_id": "provider-sensitive-message-id",
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }


def test_sender_adapter_requires_consumed_exact_authorization(tmp_path):
    request_path = tmp_path / "handoff" / "forged.request.json"
    request_path.parent.mkdir()
    request_path.write_text(
        json.dumps(
            {
                "schema_version": "applypilot.handoff.v1",
                "kind": "outreach_send",
                "resource_lock": "outbound_communication",
                "consumption_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    adapter = _FakeAdapter()
    with pytest.raises(OutreachAuthorizationError, match="artifacts escaped"):
        execute_send_handoff(request_path=request_path, adapter=adapter)
    assert adapter.calls == 0


def test_provider_acceptance_is_not_delivery_and_receipt_id_is_hashed(tmp_path):
    now = datetime.now(timezone.utc)
    with OpportunityStore(tmp_path / "opportunities.sqlite3") as store:
        opportunity = _persist_verified_lead(store, tmp_path)
        draft, _ = _persist_draft(store, tmp_path, lead=opportunity, now=now)
        authorization = build_outreach_authorization(
            (draft,), sender=draft.sender, channel=draft.channel, now=now
        )
        authorization_path = write_outreach_authorization(
            tmp_path / "authorization.json", authorization
        )
        store.record_authorization(authorization, artifact_path=authorization_path)
        run_dir = tmp_path / "send-run"
        journal = EventJournal(
            run_dir / "events.ndjson", run_id=authorization.authorization_id
        )
        request_path = queue_send_handoff(
            authorization_path=authorization_path,
            store=store,
            run_dir=run_dir,
            journal=journal,
            now=now,
        )
        adapter = _FakeAdapter()
        execute_send_handoff(request_path=request_path, adapter=adapter)
        result = consume_send_response(
            request_path=request_path,
            store=store,
            journal=journal,
        )
        assert result["counts"] == {"provider_accepted": 1}
        assert "delivered" not in result["counts"]
        assert adapter.calls == 1
        assert store.sent_count() == 1
        transitions = [
            item["to_status"] for item in store.get_draft(draft.draft_id)["transitions"]
        ]
        assert transitions == [
            "draft_ready",
            "authorized",
            "queued",
            "send_attempted",
            "provider_accepted",
        ]
        database_bytes = (tmp_path / "opportunities.sqlite3").read_bytes()
        assert b"provider-sensitive-message-id" not in database_bytes
        event_bytes = (run_dir / "events.ndjson").read_bytes()
        assert b"careers@example.com" not in event_bytes
        assert b"provider-sensitive-message-id" not in event_bytes


def test_ambiguous_timeout_stops_batch_without_retrying_siblings(tmp_path):
    now = datetime.now(timezone.utc)
    with OpportunityStore(tmp_path / "opportunities.sqlite3") as store:
        opportunity = _persist_verified_lead(store, tmp_path)
        first, _ = _persist_draft(store, tmp_path, lead=opportunity, now=now)
        second, _ = _persist_draft(
            store,
            tmp_path,
            lead=opportunity,
            now=now + timedelta(seconds=1),
        )
        authorization = build_outreach_authorization(
            (first, second), sender=first.sender, channel=first.channel, now=now
        )
        authorization_path = write_outreach_authorization(
            tmp_path / "authorization.json", authorization
        )
        store.record_authorization(authorization, artifact_path=authorization_path)
        run_dir = tmp_path / "send-run"
        journal = EventJournal(
            run_dir / "events.ndjson", run_id=authorization.authorization_id
        )
        request_path = queue_send_handoff(
            authorization_path=authorization_path,
            store=store,
            run_dir=run_dir,
            journal=journal,
            now=now,
        )
        adapter = _FakeAdapter(timeout=True)
        execute_send_handoff(request_path=request_path, adapter=adapter)
        result = consume_send_response(
            request_path=request_path,
            store=store,
            journal=journal,
        )
        assert result["status"] == "send_state_unknown"
        assert result["counts"] == {"send_state_unknown": 1, "not_attempted": 1}
        assert adapter.calls == 1
        assert store.sent_count() == 0
        assert [
            item["to_status"] for item in store.get_draft(first.draft_id)["transitions"]
        ] == ["draft_ready", "authorized", "queued", "send_attempted", "send_state_unknown"]
        assert [
            item["to_status"] for item in store.get_draft(second.draft_id)["transitions"]
        ] == ["draft_ready", "authorized", "queued", "not_attempted"]

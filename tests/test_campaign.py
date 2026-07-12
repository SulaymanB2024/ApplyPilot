import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from applypilot.autonomy import campaign as campaign_module
from applypilot.autonomy.campaign import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    CandidateState,
    CampaignCorruptionError,
    CampaignError,
    CampaignLeaseError,
    CampaignManifest,
    CampaignStore,
    CampaignStatus,
    CampaignTargetReached,
    EvidenceKind,
    SubmissionBindings,
    SubmissionConfirmation,
)
from applypilot.autonomy.supervisor import record_runtime_observation


NOW = datetime(2026, 7, 12, 18, 0, tzinfo=timezone.utc)
FACT_DIGEST = "1" * 64
CONTEXT_DIGEST = "2" * 64
POLICY_DIGEST = "3" * 64
PACKET_DIGEST = "4" * 64
FORM_DIGEST = "5" * 64
AUTHORIZATION_DIGEST = "6" * 64
FACT_APPROVAL_DIGEST = "7" * 64
SUBMISSION_POLICY_DIGEST = "a" * 64


def _manifest(
    *,
    campaign_id="campaign-1",
    target=100,
    heartbeat=300,
    submit_authorized=True,
    now=NOW,
):
    approval_bindings = (
        {
            "fact_approval_receipt_sha256": FACT_APPROVAL_DIGEST,
            "fact_approval_signature_sha256": "8" * 64,
            "approval_issuer": "applicant@example.com",
            "approval_trust_store_sha256": "9" * 64,
            "fact_approval_expires_at": (NOW + timedelta(days=1)).isoformat(),
        }
        if submit_authorized
        else {}
    )
    return CampaignManifest.new(
        campaign_id=campaign_id,
        source_run_id="run-1",
        query="entry-level product and data roles",
        fact_digest=FACT_DIGEST,
        context_digest=CONTEXT_DIGEST,
        policy_digest=POLICY_DIGEST,
        code_revision="a" * 40,
        submit_authorized=submit_authorized,
        **approval_bindings,
        target_confirmed=target,
        heartbeat_interval_seconds=heartbeat,
        now=now,
    )


def _bindings(
    *,
    packet=PACKET_DIGEST,
    authorization_artifact_id="authorization-grant",
    authorization_digest=AUTHORIZATION_DIGEST,
):
    return SubmissionBindings(
        fact_digest=FACT_DIGEST,
        context_digest=CONTEXT_DIGEST,
        policy_digest=POLICY_DIGEST,
        fact_approval_receipt_sha256=FACT_APPROVAL_DIGEST,
        submission_policy_digest=SUBMISSION_POLICY_DIGEST,
        packet_digest=packet,
        form_review_digest=FORM_DIGEST,
        authorization_artifact_id=authorization_artifact_id,
        authorization_grant_sha256=authorization_digest,
    )


def _artifact_id(prefix, job_id):
    return f"{prefix}-{hashlib.sha256(job_id.encode()).hexdigest()[:12]}"


def _persist_confirmation_evidence(store, job_id, *, now=NOW, consumption_at=NOW):
    result = {}
    for field, kind in (
        ("authorization_consumption", EvidenceKind.AUTHORIZATION_CONSUMPTION),
        ("submission_response", EvidenceKind.SUBMISSION_RESPONSE),
        ("confirmation_evidence", EvidenceKind.CONFIRMATION_EVIDENCE),
        ("controller_result", EvidenceKind.CONTROLLER_RESULT),
        ("jobs_row", EvidenceKind.JOBS_ROW),
    ):
        artifact_id = _artifact_id(field, job_id)
        data = (
            consumption_at.isoformat().encode()
            if field == "authorization_consumption"
            else f"{field}:{job_id}".encode()
        )
        result[field] = store.persist_evidence_artifact(
            artifact_id,
            kind=kind,
            data=data,
            canonical_job_id=job_id,
            now=now,
        )
    return result


def _confirmation(store, job_id, *, packet=PACKET_DIGEST, submitted_at=NOW, evidence_now=NOW):
    candidate = store.candidate(job_id)
    assert candidate is not None
    bindings = candidate["bindings"]
    assert bindings is not None
    evidence = _persist_confirmation_evidence(
        store,
        job_id,
        now=evidence_now,
        consumption_at=submitted_at,
    )
    return SubmissionConfirmation(
        canonical_job_id=job_id,
        status="submitted_confirmed",
        fact_digest=FACT_DIGEST,
        context_digest=CONTEXT_DIGEST,
        policy_digest=POLICY_DIGEST,
        fact_approval_receipt_sha256=FACT_APPROVAL_DIGEST,
        submission_policy_digest=SUBMISSION_POLICY_DIGEST,
        packet_digest=packet,
        form_review_digest=FORM_DIGEST,
        authorization_artifact_id=bindings["authorization_artifact_id"],
        authorization_grant_sha256=bindings["authorization_grant_sha256"],
        authorization_consumption_artifact_id=evidence["authorization_consumption"][
            "artifact_id"
        ],
        authorization_consumption_sha256=evidence["authorization_consumption"]["sha256"],
        submission_response_artifact_id=evidence["submission_response"]["artifact_id"],
        submission_response_sha256=evidence["submission_response"]["sha256"],
        confirmation_evidence_artifact_id=evidence["confirmation_evidence"]["artifact_id"],
        confirmation_evidence_sha256=evidence["confirmation_evidence"]["sha256"],
        controller_result_artifact_id=evidence["controller_result"]["artifact_id"],
        controller_result_sha256=evidence["controller_result"]["sha256"],
        jobs_row_artifact_id=evidence["jobs_row"]["artifact_id"],
        jobs_row_sha256=evidence["jobs_row"]["sha256"],
        submitted_at=submitted_at.isoformat(),
    )


def _advance_to_submitting(
    store,
    job_id,
    *,
    packet=PACKET_DIGEST,
    authorization_expires_at=None,
):
    assert store.register_candidate(job_id, now=NOW)
    store.transition_candidate(job_id, CandidateState.ELIGIBLE, now=NOW)
    store.transition_candidate(job_id, CandidateState.VERIFIED, now=NOW)
    store.transition_candidate(job_id, CandidateState.MATERIALS_READY, now=NOW)
    store.transition_candidate(job_id, CandidateState.FORM_REVIEWED, now=NOW)
    authorization_payload = {
        "version": "applypilot-submit-authorization-v1",
        "nonce": "0" * 32,
        "candidate_id": job_id,
        "fact_digest": FACT_DIGEST,
        "material_digest": packet,
        "form_review_digest": FORM_DIGEST,
        "policy_digest": SUBMISSION_POLICY_DIGEST,
        "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (
            authorization_expires_at or NOW + timedelta(minutes=30)
        ).isoformat(),
        "allowed_action": "submit_application",
    }
    authorization = store.persist_evidence_artifact(
        _artifact_id("authorization", job_id),
        kind=EvidenceKind.AUTHORIZATION_GRANT,
        data=json.dumps(authorization_payload, sort_keys=True).encode(),
        canonical_job_id=job_id,
        now=NOW,
    )
    store.transition_candidate(
        job_id,
        CandidateState.AUTHORIZED,
        bindings=_bindings(
            packet=packet,
            authorization_artifact_id=authorization["artifact_id"],
            authorization_digest=authorization["sha256"],
        ),
        now=NOW,
    )
    store.transition_candidate(job_id, CandidateState.SUBMITTING, now=NOW)


def test_manifest_is_immutable_and_default_target_is_100(tmp_path):
    manifest = _manifest()
    assert manifest.target_confirmed == 100
    assert manifest.heartbeat_interval_seconds == DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    store = CampaignStore.create(tmp_path / "campaign", manifest)
    reopened = CampaignStore.open(store.root)
    assert reopened.manifest == manifest

    changed = _manifest(target=99)
    with pytest.raises(FileExistsError, match="manifest already differs"):
        CampaignStore.create(store.root, changed)
    with pytest.raises(ValueError, match="between 1 and 100"):
        _manifest(target=101)


def test_create_recovers_event_first_initialization_and_serializes_creators(tmp_path):
    root = tmp_path / "campaign"
    manifest = _manifest()
    store = CampaignStore.create(root, manifest)
    (root / store.STATE_NAME).unlink()
    retry_manifest = _manifest(now=NOW + timedelta(minutes=1))
    recovered = CampaignStore.create(root, retry_manifest)
    assert recovered.snapshot()["sequence"] == 0
    assert recovered.manifest.created_at == manifest.created_at

    concurrent_root = tmp_path / "concurrent"
    with ThreadPoolExecutor(max_workers=2) as pool:
        stores = list(pool.map(lambda _: CampaignStore.create(concurrent_root, manifest), range(2)))
    assert all(item.manifest == manifest for item in stores)
    event_lines = (concurrent_root / store.EVENTS_NAME).read_text(encoding="utf-8").splitlines()
    assert len(event_lines) == 1


def test_mutations_require_one_exclusive_lease(tmp_path):
    root = tmp_path / "campaign"
    first = CampaignStore.create(root, _manifest())
    second = CampaignStore.open(root)

    with pytest.raises(CampaignLeaseError, match="requires"):
        first.register_candidate("greenhouse:1")
    with first.acquire_lease("controller-1"):
        with pytest.raises(CampaignLeaseError, match="already held"):
            second.acquire_lease("controller-2")
        assert first.register_candidate("greenhouse:1", now=NOW)
    with second.acquire_lease("controller-2"):
        assert second.register_candidate("greenhouse:2", now=NOW)
    reopened = CampaignStore.open(root)
    assert reopened.candidate("greenhouse:1") is not None
    assert reopened.candidate("greenhouse:2") is not None


def test_candidate_dedupe_and_exact_transition_graph(tmp_path):
    store = CampaignStore.create(tmp_path / "campaign", _manifest())
    with store.acquire_lease("controller"):
        assert store.register_candidate("lever:acme:role", now=NOW)
        assert not store.register_candidate("lever:acme:role", now=NOW)
        with pytest.raises(CampaignError, match="invalid candidate transition"):
            store.transition_candidate("lever:acme:role", CandidateState.AUTHORIZED, now=NOW)
        store.transition_candidate("lever:acme:role", CandidateState.ELIGIBLE, now=NOW)
        with pytest.raises(CampaignError, match="only be attached at authorization"):
            store.transition_candidate(
                "lever:acme:role",
                CandidateState.VERIFIED,
                bindings=_bindings(),
                now=NOW,
            )
        with pytest.raises(CampaignError, match="dedicated evidence"):
            store.transition_candidate(
                "lever:acme:role",
                CandidateState.SUBMITTED_CONFIRMED,
                now=NOW,
            )
    assert len(store.snapshot()["candidates"]) == 1


def test_review_only_manifest_cannot_enter_authorized_state(tmp_path):
    store = CampaignStore.create(
        tmp_path / "campaign",
        _manifest(submit_authorized=False),
    )
    with store.acquire_lease("controller"):
        assert store.register_candidate("greenhouse:review-only", now=NOW)
        store.transition_candidate("greenhouse:review-only", CandidateState.ELIGIBLE, now=NOW)
        store.transition_candidate("greenhouse:review-only", CandidateState.VERIFIED, now=NOW)
        store.transition_candidate(
            "greenhouse:review-only",
            CandidateState.MATERIALS_READY,
            now=NOW,
        )
        store.transition_candidate(
            "greenhouse:review-only",
            CandidateState.FORM_REVIEWED,
            now=NOW,
        )
        with pytest.raises(CampaignError, match="does not authorize submission"):
            store.transition_candidate(
                "greenhouse:review-only",
                CandidateState.AUTHORIZED,
                bindings=_bindings(),
                now=NOW,
            )


def test_authorization_requires_real_exact_expiring_submit_manifest(tmp_path):
    store = CampaignStore.create(tmp_path / "campaign", _manifest())
    job_id = "greenhouse:invalid-authorization"
    with store.acquire_lease("controller"):
        assert store.register_candidate(job_id, now=NOW)
        store.transition_candidate(job_id, CandidateState.ELIGIBLE, now=NOW)
        store.transition_candidate(job_id, CandidateState.VERIFIED, now=NOW)
        store.transition_candidate(job_id, CandidateState.MATERIALS_READY, now=NOW)
        store.transition_candidate(job_id, CandidateState.FORM_REVIEWED, now=NOW)
        artifact = store.persist_evidence_artifact(
            "fake-authorization",
            kind=EvidenceKind.AUTHORIZATION_GRANT,
            data=b"authorization bytes without a grant schema",
            canonical_job_id=job_id,
            now=NOW,
        )
        with pytest.raises(CampaignError, match="invalid or expired"):
            store.transition_candidate(
                job_id,
                CandidateState.AUTHORIZED,
                bindings=_bindings(
                    authorization_artifact_id=artifact["artifact_id"],
                    authorization_digest=artifact["sha256"],
                ),
                now=NOW,
            )


def test_only_matching_confirmed_evidence_increments_count(tmp_path):
    store = CampaignStore.create(tmp_path / "campaign", _manifest(target=2))
    with store.acquire_lease("controller"):
        _advance_to_submitting(store, "greenhouse:1")
        with pytest.raises(CampaignError, match="does not match"):
            store.confirm_submission(
                _confirmation(store, "greenhouse:1", packet="f" * 64),
                now=NOW,
            )
        assert store.confirmed_count == 0
        result = store.confirm_submission(_confirmation(store, "greenhouse:1"), now=NOW)
        assert result["state"] == CandidateState.SUBMITTED_CONFIRMED
        assert store.confirmed_count == 1
        assert store.confirm_submission(_confirmation(store, "greenhouse:1"), now=NOW) == result


def test_confirmation_requires_existing_typed_evidence_with_matching_digest(tmp_path):
    store = CampaignStore.create(tmp_path / "campaign", _manifest())
    with store.acquire_lease("controller"):
        _advance_to_submitting(store, "greenhouse:evidence")
        confirmation = _confirmation(store, "greenhouse:evidence")
        with pytest.raises(CampaignError, match="digest mismatch"):
            store.confirm_submission(
                replace(confirmation, jobs_row_sha256="0" * 64),
                now=NOW,
            )
        with pytest.raises(CampaignError, match="missing"):
            store.confirm_submission(
                replace(confirmation, jobs_row_artifact_id="missing-jobs-row"),
                now=NOW,
            )
    assert store.confirmed_count == 0


def test_target_reached_stops_campaign_without_exceeding_it(tmp_path):
    store = CampaignStore.create(tmp_path / "campaign", _manifest(target=1))
    with store.acquire_lease("controller"):
        _advance_to_submitting(store, "workday:tenant:req-1")
        store.confirm_submission(_confirmation(store, "workday:tenant:req-1"), now=NOW)
        assert store.target_reached
        assert store.confirmed_count == 1
        with pytest.raises(CampaignTargetReached, match="target"):
            store.register_candidate("workday:tenant:req-2", now=NOW)
    reopened = CampaignStore.open(store.root)
    assert reopened.target_reached
    assert reopened.heartbeat_snapshot(now=NOW)["remaining"] == 0


def test_outcome_unknown_is_fail_closed_but_can_be_reconciled_with_evidence(tmp_path):
    store = CampaignStore.create(tmp_path / "campaign", _manifest(target=1))
    with store.acquire_lease("controller"):
        _advance_to_submitting(store, "greenhouse:unknown")
        unknown_at = NOW + timedelta(minutes=1)
        record = store.record_outcome_unknown(
            "greenhouse:unknown",
            reason_code="browser_disconnected_after_submit",
            now=unknown_at,
        )
        assert record["state"] == CandidateState.OUTCOME_UNKNOWN
        assert store.confirmed_count == 0
        assert store.status is CampaignStatus.OUTCOME_REVIEW_REQUIRED
        with pytest.raises(CampaignError, match="unknown submission outcome"):
            store.register_candidate("greenhouse:next", now=NOW)
        with pytest.raises(CampaignError, match="unknown submission outcome"):
            store.transition_candidate(
                "greenhouse:unknown",
                CandidateState.AUTHORIZED,
                now=NOW,
            )
        store.confirm_submission(
            _confirmation(
                store,
                "greenhouse:unknown",
                submitted_at=NOW + timedelta(seconds=15),
                evidence_now=unknown_at,
            ),
            now=unknown_at + timedelta(minutes=1),
        )
        assert store.confirmed_count == 1
        assert store.status is CampaignStatus.TARGET_REACHED


def test_outcome_unknown_can_be_reconciled_as_not_submitted(tmp_path):
    store = CampaignStore.create(tmp_path / "campaign", _manifest(target=2))
    with store.acquire_lease("controller"):
        _advance_to_submitting(store, "greenhouse:not-submitted")
        store.record_outcome_unknown(
            "greenhouse:not-submitted",
            reason_code="browser_disconnected_after_submit",
            now=NOW + timedelta(minutes=1),
        )
        evidence = store.persist_evidence_artifact(
            _artifact_id("not-submitted", "greenhouse:not-submitted"),
            kind=EvidenceKind.NOT_SUBMITTED_EVIDENCE,
            data=b"authoritative form remains open",
            canonical_job_id="greenhouse:not-submitted",
            now=NOW + timedelta(minutes=2),
        )
        resolved = store.record_not_submitted(
            "greenhouse:not-submitted",
            outcome_evidence_artifact_id=evidence["artifact_id"],
            reason_code="authoritative_form_still_open",
            now=NOW + timedelta(minutes=2),
        )
        assert resolved["state"] == CandidateState.NOT_SUBMITTED
        assert store.confirmed_count == 0
        assert store.status is CampaignStatus.ACTIVE
        assert store.register_candidate("greenhouse:next", now=NOW + timedelta(minutes=2))


def test_confirmation_timestamp_must_fall_inside_attempt_window(tmp_path):
    store = CampaignStore.create(tmp_path / "campaign", _manifest())
    with store.acquire_lease("controller"):
        _advance_to_submitting(store, "greenhouse:outside-window")
        with pytest.raises(CampaignError, match="outside the submission attempt"):
            store.confirm_submission(
                _confirmation(
                    store,
                    "greenhouse:outside-window",
                    submitted_at=NOW - timedelta(seconds=1),
                ),
                now=NOW + timedelta(minutes=1),
            )
    assert store.confirmed_count == 0


def test_confirmation_rejects_submission_after_candidate_grant_expiry(tmp_path):
    store = CampaignStore.create(tmp_path / "campaign", _manifest())
    with store.acquire_lease("controller"):
        _advance_to_submitting(
            store,
            "greenhouse:expired-grant",
            authorization_expires_at=NOW + timedelta(minutes=1),
        )
        submitted_at = NOW + timedelta(minutes=2)
        with pytest.raises(CampaignError, match="invalid or expired"):
            store.confirm_submission(
                _confirmation(
                    store,
                    "greenhouse:expired-grant",
                    submitted_at=submitted_at,
                    evidence_now=submitted_at,
                ),
                now=submitted_at,
            )
    assert store.confirmed_count == 0


def test_pending_artifact_survives_restart_but_heartbeat_redacts_contents(tmp_path):
    store = CampaignStore.create(tmp_path / "campaign", _manifest())
    raw_values = {
        "prompt": "Write using private profile narrative",
        "email": "private@example.com",
        "phone": "+1-555-private",
    }
    with store.acquire_lease("controller"):
        metadata = store.persist_pending_artifact(
            "request-1",
            kind="chatgpt_web_request",
            payload=raw_values,
            now=NOW,
        )
    assert metadata["sha256"]

    reopened = CampaignStore.open(store.root)
    assert reopened.read_pending_artifact("request-1") == raw_values
    heartbeat_text = json.dumps(reopened.heartbeat_snapshot(now=NOW), sort_keys=True)
    assert "private profile" not in heartbeat_text
    assert "private@example.com" not in heartbeat_text
    assert "+1-555-private" not in heartbeat_text
    assert reopened.heartbeat_snapshot(now=NOW)["pending_artifact_count"] == 1

    with reopened.acquire_lease("controller"):
        reopened.resolve_pending_artifact("request-1", now=NOW)
    assert CampaignStore.open(store.root).heartbeat_snapshot(now=NOW)["pending_artifact_count"] == 0
    assert not (store.root / metadata["relative_path"]).exists()


def test_pending_artifact_cleanup_recovers_after_unlink_failure(monkeypatch, tmp_path):
    store = CampaignStore.create(tmp_path / "campaign", _manifest())
    with store.acquire_lease("controller"):
        metadata = store.persist_pending_artifact(
            "request-failure",
            kind="chatgpt_web_request",
            payload={"private": "do not retain"},
            now=NOW,
        )
        original_delete = campaign_module._delete_file_and_fsync

        def fail_delete(path):
            raise OSError("simulated unlink failure")

        monkeypatch.setattr(campaign_module, "_delete_file_and_fsync", fail_delete)
        with pytest.raises(OSError, match="simulated unlink failure"):
            store.resolve_pending_artifact("request-failure", now=NOW)

    artifact_path = store.root / metadata["relative_path"]
    assert artifact_path.exists()
    reopened = CampaignStore.open(store.root)
    assert reopened.snapshot()["resolved_pending_artifacts"]
    monkeypatch.setattr(campaign_module, "_delete_file_and_fsync", original_delete)
    with reopened.acquire_lease("controller-recovery"):
        pass
    assert not artifact_path.exists()
    assert CampaignStore.open(store.root).snapshot()["resolved_pending_artifacts"] == {}


def test_heartbeat_defaults_to_five_minutes_and_persists_timestamp(tmp_path):
    store = CampaignStore.create(tmp_path / "campaign", _manifest())
    initial = store.heartbeat_snapshot(now=NOW)
    assert initial["heartbeat_interval_seconds"] == 300
    assert initial["heartbeat_due"] is True
    assert initial["runtime_ready"] is False
    assert initial["runtime_observation_state"] == "missing"
    record_runtime_observation(
        root=store.root,
        scope_kind="campaign",
        scope_id=store.manifest.campaign_id,
        chronicle_state="capturing",
        chronicle_evidence_code="fresh_frame_observed",
        latest_frame_at=NOW,
        browser_surface="codex_chrome_connector",
        browser_readiness="ready",
        now=NOW,
    )
    with store.acquire_lease("controller"):
        recorded = store.record_heartbeat(now=NOW)
    assert recorded["heartbeat_due"] is False
    assert recorded["runtime_ready"] is True
    assert recorded["browser_surface"] == "codex_chrome_connector"
    assert store.heartbeat_snapshot(now=NOW + timedelta(seconds=299))["heartbeat_due"] is False
    assert store.heartbeat_snapshot(now=NOW + timedelta(seconds=300))["heartbeat_due"] is True
    assert CampaignStore.open(store.root).heartbeat_snapshot(now=NOW)["last_heartbeat_at"] == NOW.isoformat()


def test_event_log_is_append_only_and_recovers_state_ahead_of_projection(tmp_path):
    store = CampaignStore.create(tmp_path / "campaign", _manifest())
    with store.acquire_lease("controller"):
        store.register_candidate("greenhouse:1", now=NOW)
    events_path = store.root / "events.jsonl"
    lines_before = events_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["sequence"] for line in lines_before] == [0, 1]

    last_event = json.loads(lines_before[-1])
    stale_state = json.loads(lines_before[0])["state_after"]
    (store.root / "state.json").write_text(json.dumps(stale_state), encoding="utf-8")
    recovered = CampaignStore.open(store.root)
    assert recovered.snapshot() == last_event["state_after"]
    assert recovered.candidate("greenhouse:1") is not None


def test_tampered_confirmed_count_fails_closed(tmp_path):
    store = CampaignStore.create(tmp_path / "campaign", _manifest())
    event = json.loads((store.root / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])
    event["state_after"]["confirmed_count"] = 1
    event["state_sha256"] = "0" * 64
    (store.root / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    with pytest.raises(CampaignCorruptionError):
        CampaignStore.open(store.root)

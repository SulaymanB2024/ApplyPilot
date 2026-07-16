from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from applypilot.autonomy.facts import build_fact_ledger
from applypilot.autonomy.first_party import CachedFirstPartyVerifier
from applypilot.autonomy.models import RoleCandidate
from applypilot.workflow import (
    BROWSER_ACTION_SCHEMA_VERSION,
    WorkflowError,
    WorkflowStore,
    canonicalize_url,
)


RUN_ID = "workflow-run-1"
CANDIDATE_ID = "candidate-1"
URL = "https://job-boards.greenhouse.io/example/jobs/123?utm_source=test"
BASE_PROFILE = {
    "personal": {"phone": "Unconfirmed"},
    "work_authorization": {
        "legally_authorized_to_work": None,
        "require_sponsorship": None,
    },
    "availability": {"earliest_start_date": "Unconfirmed"},
}
FORM_PROFILE = {
    "personal": {"phone": "555-0100"},
    "work_authorization": {
        "legally_authorized_to_work": True,
        "require_sponsorship": False,
    },
    "availability": {"earliest_start_date": "2027-06-01"},
}
BASE_FACT_LEDGER = build_fact_ledger(
    BASE_PROFILE,
    resume_text="Truthful resume.\n",
)
FORM_FACT_LEDGER = build_fact_ledger(FORM_PROFILE, resume_text="Truthful resume.\n")
FORM_FACT_DIGEST = FORM_FACT_LEDGER.digest
OTHER_FACT_LEDGER = build_fact_ledger(
    {
        "personal": {"phone": "555-0199"},
        "work_authorization": {
            "legally_authorized_to_work": True,
            "require_sponsorship": False,
        },
        "availability": {"earliest_start_date": "2027-06-01"},
    },
    resume_text="Truthful resume.\n",
)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def prepared_store(tmp_path: Path) -> tuple[WorkflowStore, Path]:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_json(run_dir / "fact_ledger.json", BASE_FACT_LEDGER.to_dict())
    write_json(
        run_dir / "run_manifest.json",
        {
            "run_id": RUN_ID,
            "query": "data analytics internships in Austin",
            "fact_digest": BASE_FACT_LEDGER.digest,
            "context_digest": "c" * 64,
            "policy_digest": "p" * 64,
        },
    )
    material_dir = run_dir / CANDIDATE_ID
    material_dir.mkdir()
    cover = material_dir / "cover.md"
    packet = material_dir / "packet.json"
    cover.write_text("Truthful cover letter.\n", encoding="utf-8")
    packet.write_text("{}\n", encoding="utf-8")
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    store.sync_batch_result(
        run_dir=run_dir,
        result={
            "run_id": RUN_ID,
            "status": "review_ready",
            "discoveries": [
                {
                    "candidate_id": CANDIDATE_ID,
                    "company": "Example",
                    "title": "Business Analytics Intern",
                    "official_url": URL,
                    "location": "Austin, TX",
                    "description": "Python and SQL analytics internship.",
                    "source": "chatgpt_web",
                }
            ],
            "eligibility": [
                {
                    "candidate_id": CANDIDATE_ID,
                    "decision": "accept",
                    "reason_codes": ["eligible_entry_level"],
                }
            ],
            "freshness": [
                {
                    "candidate_id": CANDIDATE_ID,
                    "decision": "accept",
                    "reason_codes": ["first_party_open_and_plausible"],
                }
            ],
            "rankings": [
                {
                    "candidate_id": CANDIDATE_ID,
                    "company": "Example",
                    "title": "Business Analytics Intern",
                    "official_url": URL,
                    "location": "Austin, TX",
                    "fit_score": 94,
                    "qualifies": True,
                    "inclusion_reasons": ["target role family: data_analytics"],
                    "exclusion_reasons": [],
                }
            ],
            "materials": [
                {
                    "candidate_id": CANDIDATE_ID,
                    "fit_score": 94,
                    "inclusion_reasons": ["target role family: data_analytics"],
                    "exclusion_reasons": [],
                    "artifact_paths": {"cover_letter": str(cover), "packet": str(packet)},
                }
            ],
        },
    )
    store.persist_fact_snapshot(RUN_ID, FORM_FACT_LEDGER.to_dict())
    return store, run_dir


def dry_run_candidate(store: WorkflowStore, run_dir: Path, tmp_path: Path) -> Path:
    request_path = store.create_dry_run_requests(
        run_id=RUN_ID,
        candidate_ids=[CANDIDATE_ID],
        form_fact_digest=FORM_FACT_DIGEST,
    )[0]
    request = json.loads(request_path.read_text(encoding="utf-8"))
    screenshot = tmp_path / "dry-run.png"
    screenshot.write_bytes(b"png evidence")
    response_path = tmp_path / "dry-run.response-input.json"
    write_json(
        response_path,
        {
            "schema_version": BROWSER_ACTION_SCHEMA_VERSION,
            "request_id": request["request_id"],
            "run_id": RUN_ID,
            "candidate_id": CANDIDATE_ID,
            "mode": "dry_run",
            "material_digest": request["material_digest"],
            "form_fact_digest": FORM_FACT_DIGEST,
            "status": "dry_run_verified",
            "final_submission_performed": False,
            "review_page_reached": True,
            "ats_family": "greenhouse",
            "evidence_artifacts": [str(screenshot)],
        },
    )
    store.import_browser_response(request_path=request_path, input_path=response_path)
    return request_path


def write_verified_cache(run_dir: Path, description: str) -> None:
    official_url = URL.split("?")[0]
    role = RoleCandidate(
        company="Example",
        title="Business Analytics Intern",
        official_url=official_url,
        location="Austin, TX",
        description="Python and SQL analytics internship.",
    )
    write_json(
        run_dir / "verification" / f"{role.candidate_id}.v3.json",
        {
            "schema_version": CachedFirstPartyVerifier.SCHEMA_VERSION,
            "candidate_id": role.candidate_id,
            "requested_url": official_url,
            "evidence": {
                "official_url": official_url,
                "fetched_at": "2026-07-15T12:00:00+00:00",
                "first_party": True,
                "resolved": True,
                "open_state": True,
                "posted_date": "2026-07-01",
                "updated_date": None,
                "start_window": None,
                "status_code": 200,
                "title": "Business Analytics Intern",
                "description": description,
                "evidence": ["official posting rendered open"],
                "provider_error": "",
            },
        },
    )


def test_result_sync_creates_ranked_canonical_candidate(tmp_path: Path) -> None:
    store, _ = prepared_store(tmp_path)
    try:
        status = store.status(RUN_ID)
        assert status["candidate_counts"] == {"materials_ready": 1}
        assert status["shortlist"][0]["fit_score"] == 94
        assert status["shortlist"][0]["official_url"] == URL.split("?")[0]
    finally:
        store.close()


def test_later_review_decision_removes_previously_prepared_candidate(
    tmp_path: Path,
) -> None:
    store, run_dir = prepared_store(tmp_path)
    try:
        status = store.sync_batch_result(
            run_dir=run_dir,
            result={
                "run_id": RUN_ID,
                "status": "review_required",
                "eligibility": [
                    {
                        "candidate_id": CANDIDATE_ID,
                        "decision": "review",
                        "reason_codes": ["missing_required_fact"],
                    }
                ],
                "freshness": [
                    {
                        "candidate_id": CANDIDATE_ID,
                        "decision": "accept",
                        "reason_codes": ["first_party_open_and_plausible"],
                    }
                ],
            },
        )
        assert status["candidate_counts"] == {"review_required": 1}
        assert status["shortlist"] == []
    finally:
        store.close()


def test_corrected_preapproval_result_can_reopen_excluded_candidate(
    tmp_path: Path,
) -> None:
    store, run_dir = prepared_store(tmp_path)
    try:
        store.sync_batch_result(
            run_dir=run_dir,
            result={
                "run_id": RUN_ID,
                "status": "no_eligible_verified_roles",
                "eligibility": [
                    {
                        "candidate_id": CANDIDATE_ID,
                        "decision": "reject",
                        "reason_codes": ["matcher_policy_defect"],
                    }
                ],
            },
        )
        assert store.status(RUN_ID)["candidate_counts"] == {"excluded": 1}

        cover = run_dir / CANDIDATE_ID / "cover.md"
        packet = run_dir / CANDIDATE_ID / "packet.json"
        status = store.sync_batch_result(
            run_dir=run_dir,
            result={
                "run_id": RUN_ID,
                "status": "review_ready",
                "eligibility": [
                    {
                        "candidate_id": CANDIDATE_ID,
                        "decision": "accept",
                        "reason_codes": ["eligible_entry_level"],
                    }
                ],
                "freshness": [
                    {
                        "candidate_id": CANDIDATE_ID,
                        "decision": "accept",
                        "reason_codes": ["first_party_open_and_plausible"],
                    }
                ],
                "rankings": [
                    {
                        "candidate_id": CANDIDATE_ID,
                        "company": "Example",
                        "title": "Business Analytics Intern",
                        "official_url": URL,
                        "location": "Austin, TX",
                        "fit_score": 94,
                        "qualifies": True,
                        "inclusion_reasons": ["target role family: data_analytics"],
                        "exclusion_reasons": [],
                    }
                ],
                "materials": [
                    {
                        "candidate_id": CANDIDATE_ID,
                        "fit_score": 94,
                        "inclusion_reasons": ["target role family: data_analytics"],
                        "exclusion_reasons": [],
                        "artifact_paths": {
                            "cover_letter": str(cover),
                            "packet": str(packet),
                        },
                    }
                ],
            },
        )
        assert status["candidate_counts"] == {"materials_ready": 1}
        assert status["shortlist"][0]["candidate_id"] == CANDIDATE_ID
    finally:
        store.close()


def test_reconciliation_uses_verified_text_and_promotes_newly_confirmed_facts(
    tmp_path: Path,
) -> None:
    store, run_dir = prepared_store(tmp_path)
    try:
        write_verified_cache(
            run_dir,
            "Business analytics internship. We are unable to consider candidates who "
            "will require visa sponsorship now or in the future. Applicants must be at "
            "least 18 years old.",
        )
        unknown_profile = {
            **FORM_PROFILE,
            "availability": {
                "earliest_start_date": "2027-06-01",
                "preferred_locations": ["Austin"],
            },
            "work_authorization": {
                "legally_authorized_to_work": True,
                "require_sponsorship": None,
            },
            "eligibility": {"is_at_least_18": None},
        }
        unknown_ledger = build_fact_ledger(
            unknown_profile,
            resume_text="Truthful resume.\n",
        )
        store.persist_fact_snapshot(RUN_ID, unknown_ledger.to_dict())
        status = store.reconcile_candidate_eligibility(
            run_id=RUN_ID,
            profile=unknown_profile,
            fact_digest=unknown_ledger.digest,
        )

        assert status["candidate_counts"] == {"review_required": 1}
        reasons = json.loads(
            store.connection.execute(
                "SELECT eligibility_reasons_json FROM workflow_candidates "
                "WHERE run_id = ? AND candidate_id = ?",
                (RUN_ID, CANDIDATE_ID),
            ).fetchone()[0]
        )
        assert reasons == ["sponsorship_status_unconfirmed", "age_18_status_unconfirmed"]

        eligible_profile = {
            **unknown_profile,
            "work_authorization": {
                "legally_authorized_to_work": True,
                "require_sponsorship": False,
            },
            "eligibility": {"is_at_least_18": True},
        }
        eligible_ledger = build_fact_ledger(
            eligible_profile,
            resume_text="Truthful resume.\n",
        )
        store.persist_fact_snapshot(RUN_ID, eligible_ledger.to_dict())
        status = store.reconcile_candidate_eligibility(
            run_id=RUN_ID,
            profile=eligible_profile,
            fact_digest=eligible_ledger.digest,
        )

        assert status["candidate_counts"] == {"materials_ready": 1}
    finally:
        store.close()


def test_dry_run_cannot_claim_submission(tmp_path: Path) -> None:
    store, _ = prepared_store(tmp_path)
    try:
        request_path = store.create_dry_run_requests(
            run_id=RUN_ID,
            candidate_ids=[CANDIDATE_ID],
            form_fact_digest=FORM_FACT_DIGEST,
        )[0]
        request = json.loads(request_path.read_text(encoding="utf-8"))
        response_path = tmp_path / "bad-response.json"
        write_json(
            response_path,
            {
                "schema_version": BROWSER_ACTION_SCHEMA_VERSION,
                "request_id": request["request_id"],
                "run_id": RUN_ID,
                "candidate_id": CANDIDATE_ID,
                "mode": "dry_run",
                "material_digest": request["material_digest"],
                "form_fact_digest": FORM_FACT_DIGEST,
                "status": "dry_run_verified",
                "final_submission_performed": True,
                "review_page_reached": True,
                "evidence_artifacts": [],
            },
        )
        with pytest.raises(WorkflowError, match="no final submission"):
            store.import_browser_response(request_path=request_path, input_path=response_path)
    finally:
        store.close()


def test_dry_run_packet_binds_private_fact_snapshot(tmp_path: Path) -> None:
    store, run_dir = prepared_store(tmp_path)
    try:
        request_path = store.create_dry_run_requests(
            run_id=RUN_ID,
            candidate_ids=[CANDIDATE_ID],
            form_fact_digest=FORM_FACT_DIGEST,
        )[0]
        request = json.loads(request_path.read_text(encoding="utf-8"))
        snapshot_path = Path(request["fact_snapshot_path"])

        assert snapshot_path.parent == run_dir / "workflow-facts"
        assert snapshot_path.name == f"facts.{FORM_FACT_DIGEST}.json"
        assert snapshot_path.stat().st_mode & 0o777 == 0o600
        assert request["fact_use_policy"] == [
            "use_only_records_whose_state_is_confirmed",
            "abstain_on_unknown_rejected_or_missing_answers",
            "never_infer_screening_identity_tax_payment_or_ssn_answers",
        ]
    finally:
        store.close()


def test_exact_approval_and_confirmation_are_durable_and_one_time(tmp_path: Path) -> None:
    store, run_dir = prepared_store(tmp_path)
    try:
        dry_run_candidate(store, run_dir, tmp_path)
        approval = store.create_approval(
            run_id=RUN_ID,
            candidate_ids=[CANDIDATE_ID],
            form_fact_digest=FORM_FACT_DIGEST,
            max_submissions=1,
        )
        request_path = store.create_submission_request(
            approval_id=approval["approval_id"],
            form_fact_digest=FORM_FACT_DIGEST,
        )
        assert request_path is not None
        resumed_request_path = store.create_submission_request(
            approval_id=approval["approval_id"],
            form_fact_digest=FORM_FACT_DIGEST,
        )
        assert resumed_request_path == request_path
        assert json.loads(resumed_request_path.read_text(encoding="utf-8")) == json.loads(
            request_path.read_text(encoding="utf-8")
        )

        request = json.loads(request_path.read_text(encoding="utf-8"))
        confirmation = tmp_path / "confirmation.html"
        confirmation.write_text("Application received", encoding="utf-8")
        response_path = tmp_path / "submit.response-input.json"
        write_json(
            response_path,
            {
                "schema_version": BROWSER_ACTION_SCHEMA_VERSION,
                "request_id": request["request_id"],
                "run_id": RUN_ID,
                "candidate_id": CANDIDATE_ID,
                "mode": "submit",
                "material_digest": request["material_digest"],
                "form_fact_digest": FORM_FACT_DIGEST,
                "status": "submitted_confirmed",
                "final_submission_performed": True,
                "confirmation_kind": "confirmation_page",
                "confirmation_text": "Application received",
                "evidence_artifacts": [str(confirmation)],
            },
        )
        result = store.import_browser_response(request_path=request_path, input_path=response_path)
        assert result["state"] == "submitted_confirmed"
        durable_evidence = Path(result["evidence_path"])
        assert durable_evidence.parent == run_dir / "workflow-evidence"
        assert durable_evidence.read_text(encoding="utf-8") == "Application received"
        assert durable_evidence.stat().st_mode & 0o777 == 0o600
        repeated = store.import_browser_response(
            request_path=request_path,
            input_path=response_path,
        )
        assert repeated["state"] == "submitted_confirmed"
        consumed = store.connection.execute(
            "SELECT consumed_count FROM workflow_approvals WHERE approval_id = ?",
            (approval["approval_id"],),
        ).fetchone()[0]
        assert consumed == 1
        assert (
            store.create_submission_request(
                approval_id=approval["approval_id"],
                form_fact_digest=FORM_FACT_DIGEST,
            )
            is None
        )

        with pytest.raises(WorkflowError, match="already has submission state"):
            store.create_approval(
                run_id=RUN_ID,
                candidate_ids=[CANDIDATE_ID],
                form_fact_digest=FORM_FACT_DIGEST,
                max_submissions=1,
            )
    finally:
        store.close()


def test_ambiguous_submission_is_terminal_and_not_retried(tmp_path: Path) -> None:
    store, run_dir = prepared_store(tmp_path)
    try:
        dry_run_candidate(store, run_dir, tmp_path)
        approval = store.create_approval(
            run_id=RUN_ID,
            candidate_ids=[CANDIDATE_ID],
            form_fact_digest=FORM_FACT_DIGEST,
            max_submissions=1,
        )
        request_path = store.create_submission_request(
            approval_id=approval["approval_id"],
            form_fact_digest=FORM_FACT_DIGEST,
        )
        assert request_path is not None
        request = json.loads(request_path.read_text(encoding="utf-8"))
        evidence = tmp_path / "ambiguous.png"
        evidence.write_bytes(b"ambiguous state")
        response_path = tmp_path / "ambiguous.response.json"
        write_json(
            response_path,
            {
                "schema_version": BROWSER_ACTION_SCHEMA_VERSION,
                "request_id": request["request_id"],
                "run_id": RUN_ID,
                "candidate_id": CANDIDATE_ID,
                "mode": "submit",
                "material_digest": request["material_digest"],
                "form_fact_digest": FORM_FACT_DIGEST,
                "status": "submitted_unconfirmed",
                "final_submission_performed": True,
                "evidence_artifacts": [str(evidence)],
            },
        )
        result = store.import_browser_response(request_path=request_path, input_path=response_path)
        assert result["state"] == "submitted_unconfirmed"
        assert (
            store.create_submission_request(
                approval_id=approval["approval_id"],
                form_fact_digest=FORM_FACT_DIGEST,
            )
            is None
        )
    finally:
        store.close()


def test_submission_request_rejects_fact_drift_after_approval(tmp_path: Path) -> None:
    store, run_dir = prepared_store(tmp_path)
    try:
        dry_run_candidate(store, run_dir, tmp_path)
        approval = store.create_approval(
            run_id=RUN_ID,
            candidate_ids=[CANDIDATE_ID],
            form_fact_digest=FORM_FACT_DIGEST,
            max_submissions=1,
        )
        store.persist_fact_snapshot(RUN_ID, OTHER_FACT_LEDGER.to_dict())

        with pytest.raises(WorkflowError, match="approved candidate changed"):
            store.create_submission_request(
                approval_id=approval["approval_id"],
                form_fact_digest=OTHER_FACT_LEDGER.digest,
            )
    finally:
        store.close()


def test_approval_fails_when_form_facts_changed_after_dry_run(tmp_path: Path) -> None:
    store, run_dir = prepared_store(tmp_path)
    try:
        dry_run_candidate(store, run_dir, tmp_path)
        store.persist_fact_snapshot(RUN_ID, OTHER_FACT_LEDGER.to_dict())

        with pytest.raises(WorkflowError, match="form facts changed"):
            store.create_approval(
                run_id=RUN_ID,
                candidate_ids=[CANDIDATE_ID],
                form_fact_digest=OTHER_FACT_LEDGER.digest,
                max_submissions=1,
            )
    finally:
        store.close()


def test_approval_fails_when_reviewed_material_changed(tmp_path: Path) -> None:
    store, run_dir = prepared_store(tmp_path)
    try:
        dry_run_candidate(store, run_dir, tmp_path)
        (run_dir / CANDIDATE_ID / "cover.md").write_text(
            "Changed after review.\n",
            encoding="utf-8",
        )

        with pytest.raises(WorkflowError, match="materials changed"):
            store.create_approval(
                run_id=RUN_ID,
                candidate_ids=[CANDIDATE_ID],
                form_fact_digest=FORM_FACT_DIGEST,
                max_submissions=1,
            )
    finally:
        store.close()


def test_tampered_browser_request_is_rejected(tmp_path: Path) -> None:
    store, _ = prepared_store(tmp_path)
    try:
        request_path = store.create_dry_run_requests(
            run_id=RUN_ID,
            candidate_ids=[CANDIDATE_ID],
            form_fact_digest=FORM_FACT_DIGEST,
        )[0]
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["official_url"] = "https://jobs.example.com/different"
        write_json(request_path, request)
        response_path = tmp_path / "unused-response.json"
        write_json(response_path, {})

        with pytest.raises(WorkflowError, match="URL binding"):
            store.import_browser_response(
                request_path=request_path,
                input_path=response_path,
            )
    finally:
        store.close()


def test_legacy_applied_row_blocks_new_approval(tmp_path: Path) -> None:
    store, run_dir = prepared_store(tmp_path)
    try:
        dry_run_candidate(store, run_dir, tmp_path)
        legacy = sqlite3.connect(tmp_path / "applypilot.db")
        legacy.execute(
            "CREATE TABLE jobs(url TEXT, application_url TEXT, apply_status TEXT, applied_at TEXT)"
        )
        legacy.execute(
            "INSERT INTO jobs VALUES(?, '', 'applied', '2026-07-15T00:00:00+00:00')",
            (URL.split("?")[0],),
        )
        legacy.commit()
        legacy.close()

        with pytest.raises(WorkflowError, match="legacy submission state applied"):
            store.create_approval(
                run_id=RUN_ID,
                candidate_ids=[CANDIDATE_ID],
                form_fact_digest=FORM_FACT_DIGEST,
                max_submissions=1,
            )
    finally:
        store.close()


def test_private_candidate_urls_are_rejected() -> None:
    with pytest.raises(WorkflowError, match="private address"):
        canonicalize_url("http://127.0.0.1/apply")
    with pytest.raises(WorkflowError, match="public HTTP"):
        canonicalize_url("https://user:secret@jobs.example.com/apply")

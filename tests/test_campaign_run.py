from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from applypilot import cli, config
from applypilot.autonomy.facts import build_fact_ledger
from applypilot.campaign_run import (
    MAX_CHECKPOINT_BYTES,
    CampaignRunController,
    CampaignRunError,
)
from applypilot.workflow import WorkflowStore


CAMPAIGN_ID = "campaign-test"
RUN_ID = "campaign-workflow-run"
FORM_LEDGER = build_fact_ledger(
    {
        "personal": {"phone": "555-0100"},
        "work_authorization": {
            "legally_authorized_to_work": True,
            "require_sponsorship": False,
        },
        "availability": {"earliest_start_date": "2027-06-01"},
    },
    resume_text="Truthful resume.\n",
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _search_config(tmp_path: Path) -> Path:
    path = tmp_path / "searches.yaml"
    path.write_text(
        """
discovery_mode: direct_sources
direct_sources:
  workday: true
  direct_ats: true
  smartextract: true
jobspy_enabled: false
queries:
  - query: product analyst intern
    tier: 1
  - query: strategy analyst intern
    tier: 2
  - query: operations associate
    tier: 3
locations:
  - location: Remote
""".lstrip(),
        encoding="utf-8",
    )
    return path


def _prepared_store(
    tmp_path: Path,
    *,
    candidate_count: int,
) -> tuple[WorkflowStore, Path, list[str]]:
    run_dir = tmp_path / "workflow-run"
    run_dir.mkdir()
    _write_json(run_dir / "fact_ledger.json", FORM_LEDGER.to_dict())
    _write_json(
        run_dir / "run_manifest.json",
        {
            "run_id": RUN_ID,
            "query": "campaign test query",
            "fact_digest": FORM_LEDGER.digest,
            "context_digest": "c" * 64,
            "policy_digest": "p" * 64,
        },
    )
    candidate_ids = [f"candidate-{index}" for index in range(1, candidate_count + 1)]
    discoveries = []
    eligibility = []
    freshness = []
    rankings = []
    materials = []
    for index, candidate_id in enumerate(candidate_ids, start=1):
        material_dir = run_dir / candidate_id
        material_dir.mkdir()
        cover = material_dir / "cover.md"
        packet = material_dir / "packet.json"
        cover.write_text(f"Truthful cover letter {index}.\n", encoding="utf-8")
        packet.write_text("{}\n", encoding="utf-8")
        common = {
            "candidate_id": candidate_id,
            "company": f"Example {index}",
            "title": f"Analytics Intern {index}",
            "official_url": f"https://job-boards.greenhouse.io/example/jobs/{1000 + index}",
            "location": "Remote",
        }
        discoveries.append(
            {
                **common,
                "description": "Early-career Python and SQL analytics internship.",
                "source": "direct_ats",
                "record_type": "discovery_lead",
                "opportunity_kind": "posted_employment",
                "application_surface": "provider_requisition",
                "requisition_id": str(1000 + index),
            }
        )
        eligibility.append(
            {
                "candidate_id": candidate_id,
                "decision": "accept",
                "reason_codes": ["eligible_entry_level"],
            }
        )
        freshness.append(
            {
                "candidate_id": candidate_id,
                "decision": "accept",
                "reason_codes": ["first_party_open_and_plausible"],
            }
        )
        rankings.append(
            {
                **common,
                "fit_score": 94 - index,
                "qualifies": True,
                "inclusion_reasons": ["early career analytics"],
                "exclusion_reasons": [],
            }
        )
        materials.append(
            {
                "candidate_id": candidate_id,
                "fit_score": 94 - index,
                "inclusion_reasons": ["early career analytics"],
                "exclusion_reasons": [],
                "artifact_paths": {
                    "cover_letter": str(cover),
                    "packet": str(packet),
                },
            }
        )
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    store.sync_batch_result(
        run_dir=run_dir,
        result={
            "run_id": RUN_ID,
            "status": "review_ready",
            "discoveries": discoveries,
            "eligibility": eligibility,
            "freshness": freshness,
            "rankings": rankings,
            "materials": materials,
        },
    )
    store.persist_fact_snapshot(RUN_ID, FORM_LEDGER.to_dict())
    return store, run_dir, candidate_ids


def _start(
    store: WorkflowStore,
    tmp_path: Path,
    *,
    review_size: int,
    max_submissions: int,
    target: int,
    history_ledgers: tuple[Path, ...] = (),
    host_id: str = "host-a",
) -> CampaignRunController:
    controller = CampaignRunController(
        store,
        data_root=tmp_path / "app-data",
        host_id=host_id,
    )
    controller.start(
        campaign_id=CAMPAIGN_ID,
        target_confirmed=target,
        review_size=review_size,
        max_submissions=max_submissions,
        search_config_path=_search_config(tmp_path),
        history_ledgers=history_ledgers,
        include_runs=(RUN_ID,),
    )
    return controller


def _fact_digest(_run_id: str) -> str:
    return FORM_LEDGER.digest


def _record_dry_run(
    controller: CampaignRunController,
    tmp_path: Path,
    request_path: Path,
    *,
    marker: str,
    ats_family: str = "greenhouse",
) -> dict:
    evidence = tmp_path / f"dry-run-{marker}.png"
    evidence.write_bytes(f"dry run evidence {marker}".encode())
    return controller.record_browser_result(
        campaign_id=CAMPAIGN_ID,
        request_path=request_path,
        status="dry_run_verified",
        evidence_paths=(evidence,),
        ats_family=ats_family,
    )


def _prepare_review_packet(
    controller: CampaignRunController,
    tmp_path: Path,
    *,
    count: int,
) -> dict:
    for index in range(count):
        action = controller.step(
            campaign_id=CAMPAIGN_ID,
            fact_digest_resolver=_fact_digest,
        )
        assert action["action"] == "browser_action_required"
        assert action["browser_mode"] == "dry_run"
        _record_dry_run(
            controller,
            tmp_path,
            Path(action["request_path"]),
            marker=str(index),
            ats_family=("greenhouse", "ashby", "workday")[index % 3],
        )
    packet_action = controller.step(
        campaign_id=CAMPAIGN_ID,
        fact_digest_resolver=_fact_digest,
    )
    assert packet_action["action"] == "approval_required"
    packet = json.loads(Path(packet_action["review_packet_path"]).read_text())
    assert len(packet["candidates"]) == count
    return {**packet_action, "packet": packet}


def test_schema_host_ownership_history_tiers_and_checkpoint(tmp_path: Path) -> None:
    store, _run_dir, _candidate_ids = _prepared_store(tmp_path, candidate_count=2)
    receipt = tmp_path / "history.md"
    receipt.write_text(
        """
## Verified submissions

1. Example 1 — Analytics Intern 1
   - Evidence: ATS displayed Application submitted.
2. Example 2 — Analytics Intern 2
   - Evidence: Outcome was ambiguous and unconfirmed.
""".lstrip(),
        encoding="utf-8",
    )
    try:
        controller = _start(
            store,
            tmp_path,
            review_size=2,
            max_submissions=2,
            target=30,
            history_ledgers=(receipt,),
        )
        status = controller.status(CAMPAIGN_ID)
        assert status["counts"]["historical_duplicate"] == 1
        assert status["counts"]["historical_ambiguous"] == 1
        tiers = [
            row["tier"]
            for row in store.connection.execute(
                """
                SELECT tier FROM workflow_campaign_discovery_batches
                WHERE campaign_id = ? ORDER BY batch_index
                """,
                (CAMPAIGN_ID,),
            )
        ]
        assert tiers == [1, 2, 3]
        checkpoint = Path(status["checkpoint_path"])
        assert checkpoint.stat().st_size <= MAX_CHECKPOINT_BYTES
        checkpoint_text = checkpoint.read_text(encoding="utf-8")
        assert "official_url" not in checkpoint_text
        assert "selector" not in checkpoint_text

        observer = CampaignRunController(
            store,
            data_root=tmp_path / "app-data",
            host_id="host-b",
        )
        assert observer.status(CAMPAIGN_ID)["submitted_confirmed"] == 0
        with pytest.raises(CampaignRunError, match="another host"):
            observer.step(
                campaign_id=CAMPAIGN_ID,
                fact_digest_resolver=_fact_digest,
            )

        read_only_root = tmp_path / "first-mac-observer"
        observed = CampaignRunController.observe(
            database_path=store.path,
            data_root=read_only_root,
            campaign_id=CAMPAIGN_ID,
        )
        assert observed["submitted_confirmed"] == 0
        assert not read_only_root.exists()

        restarted = CampaignRunController(
            store,
            data_root=tmp_path / "app-data",
            host_id="host-a",
        )
        action = restarted.step(
            campaign_id=CAMPAIGN_ID,
            fact_digest_resolver=_fact_digest,
        )
        assert action["action"] == "progressed"
        assert action["next_action"] == "service_discovery_plan"
        discovery_plan = json.loads(Path(action["discovery_plan_path"]).read_text())
        assert discovery_plan["sources"]["board_leads"] == []
        assert discovery_plan["sources"]["portal_leads"] == []
        assert store.connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        store.close()


def test_five_candidate_review_three_confirmed_and_restart_recovery(
    tmp_path: Path,
) -> None:
    store, _run_dir, _candidate_ids = _prepared_store(tmp_path, candidate_count=5)
    try:
        controller = _start(
            store,
            tmp_path,
            review_size=5,
            max_submissions=3,
            target=3,
        )
        first = controller.step(
            campaign_id=CAMPAIGN_ID,
            fact_digest_resolver=_fact_digest,
        )
        restarted = CampaignRunController(
            store,
            data_root=tmp_path / "app-data",
            host_id="host-a",
        )
        recovered = restarted.step(
            campaign_id=CAMPAIGN_ID,
            fact_digest_resolver=_fact_digest,
        )
        assert recovered["request_id"] == first["request_id"]
        assert recovered["request_path"] == first["request_path"]
        _record_dry_run(
            restarted,
            tmp_path,
            Path(first["request_path"]),
            marker="restart",
        )

        # Fault injection: workflow import committed, campaign job update was lost.
        with store.connection:
            store.connection.execute(
                """
                UPDATE workflow_campaign_browser_jobs
                SET state = 'claimed', result_status = '' WHERE request_id = ?
                """,
                (first["request_id"],),
            )
        _record_dry_run(
            restarted,
            tmp_path,
            Path(first["request_path"]),
            marker="restart",
        )

        for index in range(1, 5):
            action = restarted.step(
                campaign_id=CAMPAIGN_ID,
                fact_digest_resolver=_fact_digest,
            )
            assert action["browser_mode"] == "dry_run"
            _record_dry_run(
                restarted,
                tmp_path,
                Path(action["request_path"]),
                marker=str(index),
                ats_family=("greenhouse", "ashby", "workday")[index % 3],
            )

        packet_action = restarted.step(
            campaign_id=CAMPAIGN_ID,
            fact_digest_resolver=_fact_digest,
        )
        packet = json.loads(Path(packet_action["review_packet_path"]).read_text())
        members = [item["member_id"] for item in packet["candidates"]]
        with pytest.raises(CampaignRunError, match="every review-packet"):
            restarted.review(
                campaign_id=CAMPAIGN_ID,
                packet_digest=packet_action["review_packet_digest"],
                approve=members[:3],
                reject=members[3:4],
                defer=(),
                fact_digest_resolver=_fact_digest,
            )
        with pytest.raises(CampaignRunError, match="more candidates"):
            restarted.review(
                campaign_id=CAMPAIGN_ID,
                packet_digest=packet_action["review_packet_digest"],
                approve=members[:4],
                reject=members[4:],
                defer=(),
                fact_digest_resolver=_fact_digest,
            )
        restarted.review(
            campaign_id=CAMPAIGN_ID,
            packet_digest=packet_action["review_packet_digest"],
            approve=members[:3],
            reject=members[3:],
            defer=(),
            fact_digest_resolver=_fact_digest,
        )

        for index in range(3):
            action = restarted.step(
                campaign_id=CAMPAIGN_ID,
                fact_digest_resolver=_fact_digest,
            )
            assert action["browser_mode"] == "submit"
            evidence = tmp_path / f"receipt-{index}.png"
            evidence.write_bytes(f"receipt {index}".encode())
            restarted.record_browser_result(
                campaign_id=CAMPAIGN_ID,
                request_path=Path(action["request_path"]),
                status="submitted_confirmed",
                evidence_paths=(evidence,),
                confirmation_kind="confirmation_page",
                confirmation_text=f"Application received {index}",
            )
        complete = restarted.step(
            campaign_id=CAMPAIGN_ID,
            fact_digest_resolver=_fact_digest,
        )
        assert complete["action"] == "complete"
        assert complete["submitted_confirmed"] == 3
        assert complete["counts"]["submitted_unconfirmed"] == 0
        assert store.connection.execute(
            "SELECT COUNT(DISTINCT canonical_url) FROM workflow_submission_registry "
            "WHERE outcome = 'submitted_confirmed'"
        ).fetchone()[0] == 3
    finally:
        store.close()


def test_approval_expiry_requires_a_new_form_generation(tmp_path: Path) -> None:
    store, _run_dir, _candidate_ids = _prepared_store(tmp_path, candidate_count=1)
    try:
        controller = _start(
            store,
            tmp_path,
            review_size=1,
            max_submissions=1,
            target=1,
        )
        packet_action = _prepare_review_packet(controller, tmp_path, count=1)
        member = packet_action["packet"]["candidates"][0]["member_id"]
        review = controller.review(
            campaign_id=CAMPAIGN_ID,
            packet_digest=packet_action["review_packet_digest"],
            approve=(member,),
            reject=(),
            defer=(),
            fact_digest_resolver=_fact_digest,
        )
        approval_id = review["approval_ids"][0]
        old_request_id = store.connection.execute(
            """
            SELECT request_id FROM workflow_campaign_browser_jobs
            WHERE campaign_id = ? AND mode = 'dry_run'
            """,
            (CAMPAIGN_ID,),
        ).fetchone()[0]
        with store.connection:
            store.connection.execute(
                "UPDATE workflow_approvals SET expires_at = ? WHERE approval_id = ?",
                (
                    (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                    approval_id,
                ),
            )
        action = controller.step(
            campaign_id=CAMPAIGN_ID,
            fact_digest_resolver=_fact_digest,
        )
        assert action["action"] == "browser_action_required"
        assert action["browser_mode"] == "dry_run"
        assert action["request_id"] != old_request_id
        request = json.loads(Path(action["request_path"]).read_text())
        assert request["form_review_generation"] == 1
        assert store.candidate_status(RUN_ID, "candidate-1")["state"] == "materials_ready"
    finally:
        store.close()


def test_ambiguous_submission_and_lost_submit_worker_never_retry(
    tmp_path: Path,
) -> None:
    store, _run_dir, _candidate_ids = _prepared_store(tmp_path, candidate_count=1)
    try:
        controller = _start(
            store,
            tmp_path,
            review_size=1,
            max_submissions=1,
            target=1,
        )
        packet_action = _prepare_review_packet(controller, tmp_path, count=1)
        member = packet_action["packet"]["candidates"][0]["member_id"]
        controller.review(
            campaign_id=CAMPAIGN_ID,
            packet_digest=packet_action["review_packet_digest"],
            approve=(member,),
            reject=(),
            defer=(),
            fact_digest_resolver=_fact_digest,
        )
        submit = controller.step(
            campaign_id=CAMPAIGN_ID,
            fact_digest_resolver=_fact_digest,
        )
        evidence = tmp_path / "ambiguous.png"
        evidence.write_bytes(b"ambiguous browser state")
        blocked = controller.record_browser_result(
            campaign_id=CAMPAIGN_ID,
            request_path=Path(submit["request_path"]),
            status="submitted_unconfirmed",
            evidence_paths=(evidence,),
        )
        assert blocked["campaign_status"] == "outcome_review_required"
        assert blocked["blocker_code"] == "ambiguous_submission_outcome"
        again = controller.step(
            campaign_id=CAMPAIGN_ID,
            fact_digest_resolver=_fact_digest,
        )
        assert again["action"] == "human_blocked"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM workflow_campaign_browser_jobs WHERE mode = 'submit'"
        ).fetchone()[0] == 1
    finally:
        store.close()

    other_root = tmp_path / "lost-worker"
    other_root.mkdir()
    store, _run_dir, _candidate_ids = _prepared_store(other_root, candidate_count=1)
    try:
        controller = _start(
            store,
            other_root,
            review_size=1,
            max_submissions=1,
            target=1,
        )
        packet_action = _prepare_review_packet(controller, other_root, count=1)
        member = packet_action["packet"]["candidates"][0]["member_id"]
        controller.review(
            campaign_id=CAMPAIGN_ID,
            packet_digest=packet_action["review_packet_digest"],
            approve=(member,),
            reject=(),
            defer=(),
            fact_digest_resolver=_fact_digest,
        )
        submit = controller.step(
            campaign_id=CAMPAIGN_ID,
            fact_digest_resolver=_fact_digest,
        )
        with store.connection:
            store.connection.execute(
                """
                UPDATE workflow_campaign_browser_jobs SET claim_expires_at = ?
                WHERE request_id = ?
                """,
                (
                    (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                    submit["request_id"],
                ),
            )
        blocked = controller.step(
            campaign_id=CAMPAIGN_ID,
            fact_digest_resolver=_fact_digest,
        )
        assert blocked["campaign_status"] == "outcome_review_required"
        assert blocked["blocker_code"] == "submit_worker_lost_after_request"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM workflow_campaign_browser_jobs WHERE mode = 'submit'"
        ).fetchone()[0] == 1
    finally:
        store.close()


def test_two_no_progress_cycles_stop_with_exact_blocker(tmp_path: Path) -> None:
    store, _run_dir, _candidate_ids = _prepared_store(tmp_path, candidate_count=0)
    try:
        controller = _start(
            store,
            tmp_path,
            review_size=1,
            max_submissions=1,
            target=1,
        )
        issued = controller.step(
            campaign_id=CAMPAIGN_ID,
            fact_digest_resolver=_fact_digest,
        )
        assert issued["action"] == "progressed"
        first = controller.step(
            campaign_id=CAMPAIGN_ID,
            fact_digest_resolver=_fact_digest,
        )
        assert first["action"] == "no_progress"
        assert first["no_progress_cycles"] == 1
        stopped = controller.step(
            campaign_id=CAMPAIGN_ID,
            fact_digest_resolver=_fact_digest,
        )
        assert stopped["action"] == "human_blocked"
        assert stopped["campaign_status"] == "blocked"
        assert stopped["blocker_code"] == "discovery_plan_pending"
    finally:
        store.close()


def test_campaign_never_admits_senior_sales_or_general_interest_surfaces(
    tmp_path: Path,
) -> None:
    store, _run_dir, _candidate_ids = _prepared_store(tmp_path, candidate_count=2)
    try:
        with store.connection:
            store.connection.execute(
                """
                UPDATE workflow_candidates SET title = 'Senior Sales Director'
                WHERE run_id = ? AND candidate_id = 'candidate-1'
                """,
                (RUN_ID,),
            )
            store.connection.execute(
                """
                UPDATE workflow_candidates
                SET application_surface = 'general_interest_form'
                WHERE run_id = ? AND candidate_id = 'candidate-2'
                """,
                (RUN_ID,),
            )
        controller = _start(
            store,
            tmp_path,
            review_size=1,
            max_submissions=1,
            target=1,
        )
        status = controller.status(CAMPAIGN_ID)
        assert status["counts"]["blocked"] == 1
        assert status["counts"]["eligible_inventory"] == 0
        admitted = store.connection.execute(
            """
            SELECT candidate_id FROM workflow_campaign_run_candidates
            WHERE campaign_id = ? ORDER BY candidate_id
            """,
            (CAMPAIGN_ID,),
        ).fetchall()
        assert [row["candidate_id"] for row in admitted] == ["candidate-1"]
    finally:
        store.close()


def test_historical_url_blocks_compatibility_approval(tmp_path: Path) -> None:
    store, _run_dir, _candidate_ids = _prepared_store(tmp_path, candidate_count=1)
    try:
        controller = _start(
            store,
            tmp_path,
            review_size=1,
            max_submissions=1,
            target=1,
        )
        packet_action = _prepare_review_packet(controller, tmp_path, count=1)
        candidate = store.connection.execute(
            "SELECT canonical_url FROM workflow_candidates WHERE run_id = ?",
            (RUN_ID,),
        ).fetchone()
        with store.connection:
            store.connection.execute(
                """
                INSERT INTO workflow_historical_applications(
                    identity_key, canonical_url, provider, requisition_id,
                    source_digest, evidence_state, source_path, imported_at
                ) VALUES(?, ?, '', '', ?, 'receipt_supported', ?, ?)
                """,
                (
                    "url:" + candidate["canonical_url"],
                    candidate["canonical_url"],
                    "h" * 64,
                    str(tmp_path / "history.md"),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        member = packet_action["packet"]["candidates"][0]["member_id"]
        with pytest.raises(CampaignRunError, match="review packet is stale"):
            controller.review(
                campaign_id=CAMPAIGN_ID,
                packet_digest=packet_action["review_packet_digest"],
                approve=(member,),
                reject=(),
                defer=(),
                fact_digest_resolver=_fact_digest,
            )
    finally:
        store.close()


def test_campaign_run_cli_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    search_config = _search_config(tmp_path)
    campaign_dir = tmp_path / "campaigns"
    campaign_dir.mkdir()
    monkeypatch.setattr(cli, "_bootstrap_config_only", lambda: None)
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "CAMPAIGN_DIR", campaign_dir)
    monkeypatch.setattr(config, "SEARCH_CONFIG_PATH", search_config)
    runner = CliRunner()

    help_result = runner.invoke(cli.app, ["campaign-run", "--help"])
    assert help_result.exit_code == 0
    for command in (
        "start",
        "step",
        "status",
        "record-browser-result",
        "review",
        "pause",
    ):
        assert command in help_result.stdout

    started = runner.invoke(
        cli.app,
        [
            "campaign-run",
            "start",
            "--campaign-id",
            "cli-campaign",
            "--target",
            "1",
            "--review-size",
            "1",
            "--max-submissions",
            "1",
        ],
    )
    assert started.exit_code == 0, started.stdout
    database_path = tmp_path / "workflow.sqlite3"
    database_path.chmod(0o400)
    try:
        observed = runner.invoke(
            cli.app,
            ["campaign-run", "status", "--campaign-id", "cli-campaign"],
        )
    finally:
        database_path.chmod(0o600)
    assert observed.exit_code == 0, observed.stdout
    assert '"target_confirmed": 1' in observed.stdout

"""Durable controller for a replaceable Codex goal campaign operator.

The controller owns no browser.  It advances canonical workflow state one
transition at a time, emits one exact browser request, and checkpoints enough
state for a new Codex task on the same host to resume without thread history.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping
from urllib.parse import urlsplit

import yaml

from applypilot.apply.browser_actions import BrowserInterventionPolicy
from applypilot.workflow import WorkflowError, WorkflowStore, canonicalize_url


CAMPAIGN_RUN_SCHEMA_VERSION = "applypilot-campaign-run-v1"
CHECKPOINT_SCHEMA_VERSION = "applypilot-campaign-checkpoint-v1"
DISCOVERY_PLAN_SCHEMA_VERSION = "applypilot-campaign-discovery-plan-v1"
REVIEW_PACKET_SCHEMA_VERSION = "applypilot-campaign-review-packet-v1"
DEFAULT_CAMPAIGN_ID = "second-mac-30-confirmed-202608"
DEFAULT_TARGET_CONFIRMED = 30
DEFAULT_REVIEW_SIZE = 5
DEFAULT_MAX_SUBMISSIONS = 3
MAX_CHECKPOINT_BYTES = 16 * 1024
CONTROLLER_LEASE_SECONDS = 60
BROWSER_CLAIM_SECONDS = 20 * 60
QUERY_BATCH_SIZE = 5

ACTIVE_CAMPAIGN_STATUSES = frozenset({"active"})
TERMINAL_CAMPAIGN_STATUSES = frozenset(
    {"complete", "blocked", "outcome_review_required"}
)
COUNTED_OUTCOME = "submitted_confirmed"
AMBIGUOUS_OUTCOME = "submitted_unconfirmed"
ELIGIBLE_CAMPAIGN_STATES = frozenset(
    {"verified", "materials_ready", "dry_run_ready", "authorized", "submitting"}
)
BLOCKED_CANDIDATE_STATES = frozenset(
    {"excluded", "blocked", "not_submitted"}
)
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")


class CampaignRunError(RuntimeError):
    """Raised when a campaign transition is invalid or unsafe."""


class StepAction(StrEnum):
    """Stable action returned by one campaign-run step."""

    PROGRESSED = "progressed"
    BROWSER_ACTION_REQUIRED = "browser_action_required"
    APPROVAL_REQUIRED = "approval_required"
    HUMAN_BLOCKED = "human_blocked"
    NO_PROGRESS = "no_progress"
    COMPLETE = "complete"


@dataclass(frozen=True)
class HistoricalIdentity:
    identity_key: str
    canonical_url: str
    provider: str
    requisition_id: str
    evidence_state: str
    source_digest: str


class CampaignRunController:
    """State machine stored additively inside canonical ``workflow.sqlite3``."""

    def __init__(
        self,
        workflow: WorkflowStore,
        *,
        data_root: Path,
        host_id: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.workflow = workflow
        self.connection = workflow.connection
        self.data_root = data_root.expanduser().resolve()
        self.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._host_id_override = host_id
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._initialize()

    @classmethod
    def observe(
        cls,
        *,
        database_path: Path,
        data_root: Path,
        campaign_id: str,
    ) -> dict[str, Any]:
        """Read campaign status through a literal read-only SQLite connection."""
        path = database_path.expanduser().resolve()
        if not path.is_file():
            raise CampaignRunError("canonical workflow database does not exist")
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        observer = object.__new__(cls)
        observer.connection = connection
        observer.data_root = data_root.expanduser().resolve()
        try:
            return observer.status(campaign_id, repair_checkpoint=False)
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        *,
        campaign_id: str = DEFAULT_CAMPAIGN_ID,
        target_confirmed: int = DEFAULT_TARGET_CONFIRMED,
        review_size: int = DEFAULT_REVIEW_SIZE,
        max_submissions: int = DEFAULT_MAX_SUBMISSIONS,
        search_config_path: Path,
        history_ledgers: Iterable[Path] = (),
        include_runs: Iterable[str] = (),
    ) -> dict[str, Any]:
        _require_safe_id(campaign_id, "campaign id")
        if target_confirmed <= 0:
            raise CampaignRunError("target confirmed count must be positive")
        if not 1 <= review_size <= 5:
            raise CampaignRunError("review size must be between one and five")
        if not 1 <= max_submissions <= min(3, review_size):
            raise CampaignRunError(
                "max submissions must be one to three and no larger than review size"
            )
        scope, config_digest = _search_scope(search_config_path)
        host_id = self._host_id(create=True)
        now = self._timestamp()
        existing = self._campaign_optional(campaign_id)
        created = existing is None
        if existing is not None:
            self._require_owner(existing)
            expected = (
                int(existing["target_confirmed"]),
                int(existing["review_size"]),
                int(existing["max_submissions"]),
                str(existing["search_config_sha256"]),
            )
            actual = (target_confirmed, review_size, max_submissions, config_digest)
            if expected != actual:
                raise CampaignRunError(
                    "existing campaign contract differs from requested start contract"
                )
        else:
            campaign_dir = self._campaign_dir(campaign_id)
            campaign_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO workflow_campaign_runs(
                        campaign_id, owner_host_id, target_confirmed, review_size,
                        max_submissions, status, search_config_sha256,
                        search_scope_json, search_tier, next_query_batch,
                        active_run_id, active_approval_id, active_review_digest,
                        active_request_id, blocker_code, no_progress_cycles,
                        checkpoint_sequence, progress_fingerprint,
                        last_progress_at, started_at, updated_at,
                        lease_owner, lease_expires_at
                    ) VALUES(?, ?, ?, ?, ?, 'active', ?, ?, 1, 0,
                             '', '', '', '', '', 0, 0, '', ?, ?, ?, '', '')
                    """,
                    (
                        campaign_id,
                        host_id,
                        target_confirmed,
                        review_size,
                        max_submissions,
                        config_digest,
                        _canonical_json(scope),
                        now,
                        now,
                        now,
                    ),
                )
                for batch in scope["query_batches"]:
                    self.connection.execute(
                        """
                        INSERT INTO workflow_campaign_discovery_batches(
                            campaign_id, batch_index, tier, terms_json,
                            locations_json, state, plan_path, workflow_run_id,
                            created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, 'pending', '', '', ?, ?)
                        """,
                        (
                            campaign_id,
                            int(batch["batch_index"]),
                            int(batch["tier"]),
                            _canonical_json(batch["terms"]),
                            _canonical_json(scope["locations"]),
                            now,
                            now,
                        ),
                    )

        imported = self.import_history_ledgers(
            history_ledgers,
            campaign_id=campaign_id,
        )
        for run_id in include_runs:
            self.attach_workflow_run(
                campaign_id=campaign_id,
                run_id=run_id,
                tier=self._current_search_tier(campaign_id),
            )
        self._enroll_new_workflow_runs(campaign_id)
        if created or imported:
            self._emit(
                campaign_id,
                action=StepAction.PROGRESSED,
                next_action="advance_campaign",
                blocker_code="",
                made_progress=True,
                waiting_external=False,
            )
        return self.status(campaign_id)

    def step(
        self,
        *,
        campaign_id: str,
        workflow_run_id: str = "",
        fact_digest_resolver: Callable[[str], str] | None = None,
    ) -> dict[str, Any]:
        _require_safe_id(campaign_id, "campaign id")
        owner = self._lease_owner()
        with self._controller_lease(campaign_id, owner):
            campaign = self._campaign(campaign_id)
            self._require_owner(campaign)
            if campaign["status"] == "paused":
                return self._emit(
                    campaign_id,
                    action=StepAction.HUMAN_BLOCKED,
                    next_action="resume_campaign",
                    blocker_code="campaign_paused",
                    made_progress=False,
                    waiting_external=True,
                )
            if campaign["status"] in TERMINAL_CAMPAIGN_STATUSES:
                action = (
                    StepAction.COMPLETE
                    if campaign["status"] == "complete"
                    else StepAction.HUMAN_BLOCKED
                )
                return self._emit(
                    campaign_id,
                    action=action,
                    next_action="none" if action == StepAction.COMPLETE else "resolve_blocker",
                    blocker_code=str(campaign["blocker_code"] or ""),
                    made_progress=False,
                    waiting_external=True,
                )

            if workflow_run_id:
                _require_safe_id(workflow_run_id, "workflow run id")
                self.attach_workflow_run(
                    campaign_id=campaign_id,
                    run_id=workflow_run_id,
                    tier=self._current_search_tier(campaign_id),
                )
                self._attach_issued_discovery_batch(campaign_id, workflow_run_id)

            self._enroll_new_workflow_runs(campaign_id)
            self._reconcile_discovery_batches(campaign_id)
            self._reconcile_candidates(campaign_id)

            counts = self._counts(campaign_id)
            if counts[COUNTED_OUTCOME] >= int(campaign["target_confirmed"]):
                with self.connection:
                    self.connection.execute(
                        """
                        UPDATE workflow_campaign_runs
                        SET status = 'complete', blocker_code = '', updated_at = ?
                        WHERE campaign_id = ?
                        """,
                        (self._timestamp(), campaign_id),
                    )
                return self._emit(
                    campaign_id,
                    action=StepAction.COMPLETE,
                    next_action="none",
                    blocker_code="",
                    made_progress=True,
                    waiting_external=False,
                )

            if counts[AMBIGUOUS_OUTCOME] > 0:
                return self._block(
                    campaign_id,
                    status="outcome_review_required",
                    blocker_code="ambiguous_submission_outcome",
                    next_action="reconcile_unknown_submission_outcome",
                )

            pending_browser = self._pending_browser_job(campaign_id)
            if pending_browser is not None:
                return self._resume_browser_job(campaign_id, pending_browser)

            review = self._open_review_packet(campaign_id)
            if review is not None:
                return self._emit(
                    campaign_id,
                    action=StepAction.APPROVAL_REQUIRED,
                    next_action="review_exact_candidate_packet",
                    blocker_code="approval_required",
                    made_progress=False,
                    waiting_external=True,
                    review_packet_path=str(review["packet_path"]),
                    review_packet_digest=str(review["packet_digest"]),
                )

            approval = self._next_approval(campaign_id)
            while approval is not None:
                approval_state = self._approval_state(campaign_id, approval)
                if approval_state == "expired":
                    approval = self._next_approval(campaign_id)
                    continue
                if approval_state == "complete":
                    approval = self._next_approval(campaign_id)
                    continue
                if fact_digest_resolver is None:
                    return self._block(
                        campaign_id,
                        status="blocked",
                        blocker_code="form_fact_resolver_unavailable",
                        next_action="restore_confirmed_form_facts",
                    )
                run_id = str(approval["run_id"])
                try:
                    digest = fact_digest_resolver(run_id)
                    request = self.workflow.create_submission_request(
                        approval_id=str(approval["approval_id"]),
                        form_fact_digest=digest,
                    )
                except (WorkflowError, ValueError) as exc:
                    return self._block(
                        campaign_id,
                        status="blocked",
                        blocker_code=_error_code("submission_request", exc),
                        next_action="repair_submission_preflight",
                    )
                if request is None:
                    self._finish_approval(campaign_id, str(approval["approval_id"]))
                    approval = self._next_approval(campaign_id)
                    continue
                job = self._register_browser_job(campaign_id, request)
                return self._emit_browser_request(campaign_id, job, made_progress=True)

            review_candidates = self._review_candidates(campaign_id)
            if len(review_candidates) >= int(campaign["review_size"]) or (
                review_candidates and self._search_exhausted(campaign_id)
            ):
                packet = self._create_review_packet(
                    campaign_id,
                    review_candidates[: int(campaign["review_size"])],
                )
                return self._emit(
                    campaign_id,
                    action=StepAction.APPROVAL_REQUIRED,
                    next_action="review_exact_candidate_packet",
                    blocker_code="approval_required",
                    made_progress=True,
                    waiting_external=True,
                    review_packet_path=str(packet["packet_path"]),
                    review_packet_digest=str(packet["packet_digest"]),
                )

            material_candidate = self._next_material_candidate(campaign_id)
            if material_candidate is not None:
                if fact_digest_resolver is None:
                    return self._block(
                        campaign_id,
                        status="blocked",
                        blocker_code="form_fact_resolver_unavailable",
                        next_action="restore_confirmed_form_facts",
                    )
                run_id = str(material_candidate["run_id"])
                candidate_id = str(material_candidate["candidate_id"])
                try:
                    digest = fact_digest_resolver(run_id)
                    current = self.connection.execute(
                        """
                        SELECT state FROM workflow_candidates
                        WHERE run_id = ? AND candidate_id = ?
                        """,
                        (run_id, candidate_id),
                    ).fetchone()
                    if current is None or current["state"] != "materials_ready":
                        self._reconcile_candidates(campaign_id)
                        return self._emit(
                            campaign_id,
                            action=StepAction.PROGRESSED,
                            next_action="advance_campaign",
                            blocker_code="candidate_eligibility_changed",
                            made_progress=True,
                            waiting_external=False,
                        )
                    request = self.workflow.create_dry_run_requests(
                        run_id=run_id,
                        candidate_ids=[candidate_id],
                        form_fact_digest=digest,
                        action_policy=BrowserInterventionPolicy.for_application_handoff(
                            autonomous_auth=True,
                        ),
                    )[0]
                except (WorkflowError, ValueError) as exc:
                    return self._block(
                        campaign_id,
                        status="blocked",
                        blocker_code=_error_code("dry_run_request", exc),
                        next_action="repair_form_preflight",
                    )
                job = self._register_browser_job(campaign_id, request)
                return self._emit_browser_request(campaign_id, job, made_progress=True)

            workflow_action = self._workflow_action_required(campaign_id)
            if workflow_action is not None:
                return self._emit(
                    campaign_id,
                    action=StepAction.NO_PROGRESS,
                    next_action="advance_workflow_run",
                    blocker_code="workflow_run_requires_advancement",
                    made_progress=False,
                    waiting_external=False,
                    workflow_run_id=workflow_action,
                )

            issued = self._issued_discovery_batch(campaign_id)
            if issued is not None:
                return self._emit(
                    campaign_id,
                    action=StepAction.NO_PROGRESS,
                    next_action="service_discovery_plan",
                    blocker_code="discovery_plan_pending",
                    made_progress=False,
                    waiting_external=False,
                    discovery_plan_path=str(issued["plan_path"]),
                )

            next_batch = self._next_discovery_batch(campaign_id)
            if next_batch is not None:
                plan_path = self._issue_discovery_plan(campaign_id, next_batch)
                return self._emit(
                    campaign_id,
                    action=StepAction.PROGRESSED,
                    next_action="service_discovery_plan",
                    blocker_code="",
                    made_progress=True,
                    waiting_external=True,
                    discovery_plan_path=str(plan_path),
                )

            return self._emit(
                campaign_id,
                action=StepAction.NO_PROGRESS,
                next_action="expand_verified_candidate_inventory",
                blocker_code="candidate_inventory_exhausted",
                made_progress=False,
                waiting_external=False,
            )

    def review(
        self,
        *,
        campaign_id: str,
        packet_digest: str,
        approve: Iterable[str],
        reject: Iterable[str],
        defer: Iterable[str],
        applicant_confirmations: Iterable[str] = (),
        fact_digest_resolver: Callable[[str], str],
    ) -> dict[str, Any]:
        _require_safe_id(campaign_id, "campaign id")
        if not re.fullmatch(r"[a-f0-9]{64}", packet_digest):
            raise CampaignRunError("review packet digest is invalid")
        with self._controller_lease(campaign_id, self._lease_owner()):
            campaign = self._campaign(campaign_id)
            self._require_owner(campaign)
            if campaign["status"] != "active":
                raise CampaignRunError("campaign is not active")
            packet = self.connection.execute(
                """
                SELECT * FROM workflow_campaign_review_packets
                WHERE campaign_id = ? AND packet_digest = ? AND status = 'open'
                """,
                (campaign_id, packet_digest),
            ).fetchone()
            if packet is None or campaign["active_review_digest"] != packet_digest:
                raise CampaignRunError("review packet is not the active packet")
            packet_items = json.loads(packet["candidates_json"])
            packet_candidates = {
                _member_key(str(item["run_id"]), str(item["candidate_id"]))
                for item in packet_items
            }
            approved = _unique_member_keys(approve)
            rejected = _unique_member_keys(reject)
            deferred = _unique_member_keys(defer)
            if approved & rejected or approved & deferred or rejected & deferred:
                raise CampaignRunError("a candidate cannot have multiple review decisions")
            if approved | rejected | deferred != packet_candidates:
                raise CampaignRunError(
                    "every review-packet candidate must be approved, rejected, or deferred"
                )
            if len(approved) > int(campaign["max_submissions"]):
                raise CampaignRunError("review approves more candidates than the campaign limit")
            for item in packet_items:
                row = self.connection.execute(
                    """
                    SELECT c.*, m.decision
                    FROM workflow_campaign_run_candidates m
                    JOIN workflow_candidates c
                      ON c.run_id = m.run_id AND c.candidate_id = m.candidate_id
                    WHERE m.campaign_id = ? AND m.run_id = ? AND m.candidate_id = ?
                    """,
                    (campaign_id, item["run_id"], item["candidate_id"]),
                ).fetchone()
                stale = (
                    row is None
                    or row["decision"] not in {"pending", "deferred"}
                    or row["state"] != "dry_run_ready"
                    or row["material_digest"] != item["material_digest"]
                    or row["form_review_digest"] != item["form_review_digest"]
                )
                if row is not None and not stale:
                    duplicate = self.connection.execute(
                        """
                        SELECT 1 FROM workflow_submission_registry
                        WHERE canonical_url = ?
                        """,
                        (row["canonical_url"],),
                    ).fetchone()
                    try:
                        self.workflow._validated_material_paths(row)
                        self.workflow._validate_persisted_evidence(
                            json.loads(str(row["form_review_json"]))
                        )
                    except (WorkflowError, json.JSONDecodeError):
                        stale = True
                    stale = stale or bool(
                        duplicate
                        or self._historical_match_state(row)
                        or self.workflow._legacy_outcome_for_url(row["canonical_url"])
                    )
                if stale:
                    self._invalidate_review_packet(
                        campaign_id,
                        packet_digest,
                        reason="candidate_bindings_changed",
                    )
                    raise CampaignRunError(
                        "review packet is stale; generate a new exact candidate packet"
                    )

            grouped: dict[str, list[str]] = defaultdict(list)
            for member in approved:
                run_id, candidate_id = _split_member_key(member)
                grouped[run_id].append(candidate_id)
            policy = BrowserInterventionPolicy.for_application_handoff(
                autonomous_auth=True,
                applicant_confirmations=applicant_confirmations,
            )
            fact_digests = {
                run_id: fact_digest_resolver(run_id) for run_id in sorted(grouped)
            }
            fact_snapshot_paths = {
                run_id: self.workflow._validated_fact_snapshot(
                    run_id,
                    fact_digests[run_id],
                )
                for run_id in sorted(grouped)
            }
            for member in approved:
                run_id, candidate_id = _split_member_key(member)
                row = self.connection.execute(
                    """
                    SELECT form_review_json FROM workflow_candidates
                    WHERE run_id = ? AND candidate_id = ?
                    """,
                    (run_id, candidate_id),
                ).fetchone()
                form_review = json.loads(str(row["form_review_json"]))
                if (
                    form_review.get("form_fact_digest") != fact_digests[run_id]
                    or form_review.get("fact_snapshot_path")
                    != str(fact_snapshot_paths[run_id])
                ):
                    self._invalidate_review_packet(
                        campaign_id,
                        packet_digest,
                        reason="form_facts_changed",
                    )
                    raise CampaignRunError(
                        "review packet facts changed; a fresh form dry-run is required"
                    )
            approvals: list[dict[str, Any]] = []
            for run_id, candidate_ids in sorted(grouped.items()):
                approval = self.workflow.create_approval(
                    run_id=run_id,
                    candidate_ids=candidate_ids,
                    form_fact_digest=fact_digests[run_id],
                    max_submissions=len(candidate_ids),
                    valid_hours=24,
                    action_policy=policy,
                )
                approvals.append(approval)

            now = self._timestamp()
            with self.connection:
                for decision, members in (
                    ("approved", approved),
                    ("rejected", rejected),
                    ("deferred", deferred),
                ):
                    for member in members:
                        run_id, candidate_id = _split_member_key(member)
                        self.connection.execute(
                            """
                            UPDATE workflow_campaign_run_candidates
                            SET decision = ?, review_packet_digest = ?, updated_at = ?
                            WHERE campaign_id = ? AND run_id = ? AND candidate_id = ?
                            """,
                            (
                                decision,
                                packet_digest,
                                now,
                                campaign_id,
                                run_id,
                                candidate_id,
                            ),
                        )
                for position, approval in enumerate(approvals, start=1):
                    self.connection.execute(
                        """
                        INSERT INTO workflow_campaign_review_approvals(
                            campaign_id, packet_digest, approval_id, run_id,
                            position, status, created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, 'queued', ?, ?)
                        """,
                        (
                            campaign_id,
                            packet_digest,
                            approval["approval_id"],
                            approval["run_id"],
                            position,
                            now,
                            now,
                        ),
                    )
                self.connection.execute(
                    """
                    UPDATE workflow_campaign_review_packets
                    SET status = 'completed', completed_at = ?
                    WHERE campaign_id = ? AND packet_digest = ?
                    """,
                    (now, campaign_id, packet_digest),
                )
                self.connection.execute(
                    """
                    UPDATE workflow_campaign_runs
                    SET active_review_digest = '', active_approval_id = ?,
                        blocker_code = '', updated_at = ?
                    WHERE campaign_id = ?
                    """,
                    (
                        approvals[0]["approval_id"] if approvals else "",
                        now,
                        campaign_id,
                    ),
                )
            return self._emit(
                campaign_id,
                action=StepAction.PROGRESSED,
                next_action="advance_campaign",
                blocker_code="",
                made_progress=True,
                waiting_external=False,
                approval_ids=[item["approval_id"] for item in approvals],
            )

    def record_browser_result(
        self,
        *,
        campaign_id: str,
        request_path: Path,
        status: str,
        evidence_paths: Iterable[Path] = (),
        detail: str = "",
        ats_family: str = "",
        confirmation_kind: str = "",
        confirmation_text: str = "",
        performed_interventions: Iterable[str] = (),
        accepted_confirmation_sha256: Iterable[str] = (),
        auth_blocker_code: str = "",
        account_created_with_google_password_manager: bool = False,
    ) -> dict[str, Any]:
        _require_safe_id(campaign_id, "campaign id")
        request_path = request_path.expanduser().resolve()
        with self._controller_lease(campaign_id, self._lease_owner()):
            campaign = self._campaign(campaign_id)
            self._require_owner(campaign)
            request = _read_json(request_path)
            request_id = str(request.get("request_id") or "")
            job = self.connection.execute(
                """
                SELECT * FROM workflow_campaign_browser_jobs
                WHERE campaign_id = ? AND request_id = ?
                """,
                (campaign_id, request_id),
            ).fetchone()
            if job is None or Path(str(job["request_path"])).resolve() != request_path:
                raise CampaignRunError("browser request is not owned by this campaign")
            if str(job["state"]) == "imported":
                if str(job["result_status"]) != status:
                    raise CampaignRunError("browser result was already imported differently")
                return self.status(campaign_id)

            mode = str(request.get("mode") or "")
            if mode == "dry_run":
                allowed = {"dry_run_verified", "blocked", "not_started"}
                final_performed = False
            elif mode == "submit":
                allowed = {
                    "submitted_confirmed",
                    "submitted_unconfirmed",
                    "not_submitted",
                    "blocked",
                }
                final_performed = status in {
                    "submitted_confirmed",
                    "submitted_unconfirmed",
                }
            else:
                raise CampaignRunError("browser request mode is invalid")
            if status not in allowed:
                raise CampaignRunError("browser result status is invalid for its mode")

            response: dict[str, Any] = {
                "schema_version": request["schema_version"],
                "request_id": request_id,
                "run_id": request["run_id"],
                "candidate_id": request["candidate_id"],
                "mode": mode,
                "material_digest": request["material_digest"],
                "form_fact_digest": request["form_fact_digest"],
                "status": status,
                "final_submission_performed": final_performed,
                "evidence_artifacts": [
                    str(path.expanduser().resolve()) for path in evidence_paths
                ],
                "performed_interventions": list(dict.fromkeys(performed_interventions)),
                "accepted_confirmation_sha256": list(
                    dict.fromkeys(accepted_confirmation_sha256)
                ),
            }
            if detail:
                response["detail"] = str(detail)[:500]
            if ats_family:
                response["ats_family"] = str(ats_family)[:80]
            if mode == "dry_run" and status == "dry_run_verified":
                response["review_page_reached"] = True
            if confirmation_kind:
                response["confirmation_kind"] = confirmation_kind
            if confirmation_text:
                response["confirmation_text"] = confirmation_text[:500]
            if auth_blocker_code:
                response["auth_blocker_code"] = auth_blocker_code
            if account_created_with_google_password_manager:
                response["account_creation_evidence"] = {
                    "provider": "google_password_manager",
                    "password_fields_populated_without_reading": True,
                    "account_continuation_activated": True,
                    "account_gate_cleared": True,
                }

            input_dir = self._campaign_dir(campaign_id) / "browser-response-inputs"
            input_path = input_dir / f"{request_id}.{_sha256_json(response)[:16]}.json"
            _write_private_json(input_path, response)
            imported = self._existing_browser_import(request=request, response=response)
            if imported is None:
                try:
                    imported = self.workflow.import_browser_response(
                        request_path=request_path,
                        input_path=input_path,
                    )
                except (WorkflowError, ValueError) as exc:
                    raise CampaignRunError(f"browser result rejected: {exc}") from exc

            now = self._timestamp()
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE workflow_campaign_browser_jobs
                    SET state = 'imported', result_status = ?, response_path = ?,
                        updated_at = ? WHERE request_id = ?
                    """,
                    (
                        status,
                        str(request.get("response_path") or ""),
                        now,
                        request_id,
                    ),
                )
                self.connection.execute(
                    """
                    UPDATE workflow_campaign_runs
                    SET active_request_id = '', blocker_code = '', updated_at = ?
                    WHERE campaign_id = ? AND active_request_id = ?
                    """,
                    (now, campaign_id, request_id),
                )
            self._reconcile_candidates(campaign_id)
            if status == AMBIGUOUS_OUTCOME:
                return self._block(
                    campaign_id,
                    status="outcome_review_required",
                    blocker_code="ambiguous_submission_outcome",
                    next_action="reconcile_unknown_submission_outcome",
                )
            return self._emit(
                campaign_id,
                action=StepAction.PROGRESSED,
                next_action="advance_campaign",
                blocker_code="",
                made_progress=True,
                waiting_external=False,
                imported=imported,
            )

    def _existing_browser_import(
        self,
        *,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Reconcile a crash after workflow import but before job finalization."""
        destination = Path(str(request.get("response_path") or "")).resolve()
        if not destination.is_file():
            return None
        existing = _read_json(destination)
        exact_fields = (
            "schema_version",
            "request_id",
            "run_id",
            "candidate_id",
            "mode",
            "material_digest",
            "form_fact_digest",
            "status",
            "final_submission_performed",
            "performed_interventions",
            "accepted_confirmation_sha256",
            "detail",
            "ats_family",
            "review_page_reached",
            "confirmation_kind",
            "confirmation_text",
            "auth_blocker_code",
            "account_creation_evidence",
        )
        if any(existing.get(field) != response.get(field) for field in exact_fields):
            raise CampaignRunError(
                "durable browser response differs from the result being recorded"
            )
        expected_evidence: list[str] = []
        for raw_path in response.get("evidence_artifacts") or []:
            path = Path(str(raw_path)).expanduser().resolve()
            if not path.is_file():
                raise CampaignRunError("browser evidence artifact is missing")
            expected_evidence.append(hashlib.sha256(path.read_bytes()).hexdigest())
        imported_digests = existing.get("evidence_sha256") or {}
        if not isinstance(imported_digests, Mapping) or sorted(expected_evidence) != sorted(
            str(value) for value in imported_digests.values()
        ):
            raise CampaignRunError(
                "durable browser evidence differs from the result being recorded"
            )
        return self.workflow.candidate_status(
            str(request["run_id"]),
            str(request["candidate_id"]),
        )

    def pause(self, *, campaign_id: str, reason: str = "operator_requested") -> dict[str, Any]:
        with self._controller_lease(campaign_id, self._lease_owner()):
            campaign = self._campaign(campaign_id)
            self._require_owner(campaign)
            if campaign["status"] != "active":
                raise CampaignRunError("only an active campaign can be paused")
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE workflow_campaign_runs
                    SET status = 'paused', blocker_code = ?, updated_at = ?
                    WHERE campaign_id = ?
                    """,
                    (
                        str(reason).strip()[:120] or "operator_requested",
                        self._timestamp(),
                        campaign_id,
                    ),
                )
            return self._emit(
                campaign_id,
                action=StepAction.HUMAN_BLOCKED,
                next_action="resume_campaign",
                blocker_code=str(reason).strip()[:120] or "operator_requested",
                made_progress=True,
                waiting_external=True,
            )

    def resume(self, *, campaign_id: str) -> dict[str, Any]:
        with self._controller_lease(campaign_id, self._lease_owner()):
            campaign = self._campaign(campaign_id)
            self._require_owner(campaign)
            if campaign["status"] not in {"paused", "blocked"}:
                raise CampaignRunError(
                    "only a paused or non-ambiguous blocked campaign can be resumed directly"
                )
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE workflow_campaign_runs
                    SET status = 'active', blocker_code = '', no_progress_cycles = 0,
                        updated_at = ? WHERE campaign_id = ?
                    """,
                    (self._timestamp(), campaign_id),
                )
            return self._emit(
                campaign_id,
                action=StepAction.PROGRESSED,
                next_action="advance_campaign",
                blocker_code="",
                made_progress=True,
                waiting_external=False,
            )

    def status(
        self,
        campaign_id: str,
        *,
        repair_checkpoint: bool = True,
    ) -> dict[str, Any]:
        campaign = self._campaign(campaign_id)
        counts = self._counts(campaign_id)
        latest = self.connection.execute(
            """
            SELECT sequence, action, next_action, blocker_code, snapshot_json,
                   snapshot_sha256, created_at
            FROM workflow_campaign_checkpoints
            WHERE campaign_id = ? ORDER BY sequence DESC LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()
        if (
            repair_checkpoint
            and latest is not None
            and self._host_id(create=False) == str(campaign["owner_host_id"])
        ):
            snapshot_json = str(latest["snapshot_json"])
            if hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest() != str(
                latest["snapshot_sha256"]
            ):
                raise CampaignRunError("stored campaign checkpoint digest is invalid")
            snapshot = json.loads(snapshot_json)
            checkpoint_path = self._checkpoint_path(campaign_id)
            current_digest = ""
            if checkpoint_path.is_file():
                try:
                    current_digest = _sha256_json(_read_json(checkpoint_path))
                except (CampaignRunError, json.JSONDecodeError, OSError):
                    current_digest = ""
            if current_digest != str(latest["snapshot_sha256"]):
                _write_private_json(checkpoint_path, snapshot)
        return {
            "schema_version": CAMPAIGN_RUN_SCHEMA_VERSION,
            "campaign_id": campaign_id,
            "status": str(campaign["status"]),
            "owner_host_id": str(campaign["owner_host_id"]),
            "target_confirmed": int(campaign["target_confirmed"]),
            "submitted_confirmed": counts[COUNTED_OUTCOME],
            "remaining": max(
                0, int(campaign["target_confirmed"]) - counts[COUNTED_OUTCOME]
            ),
            "counts": counts,
            "search_tier": int(campaign["search_tier"]),
            "active_run_id": str(campaign["active_run_id"] or ""),
            "active_approval_id": str(campaign["active_approval_id"] or ""),
            "active_review_digest": str(campaign["active_review_digest"] or ""),
            "active_request_id": str(campaign["active_request_id"] or ""),
            "blocker_code": str(campaign["blocker_code"] or ""),
            "no_progress_cycles": int(campaign["no_progress_cycles"]),
            "checkpoint_sequence": int(campaign["checkpoint_sequence"]),
            "last_progress_at": str(campaign["last_progress_at"]),
            "checkpoint_path": str(self._checkpoint_path(campaign_id)),
            "latest_checkpoint": (
                {
                    "sequence": int(latest["sequence"]),
                    "action": str(latest["action"]),
                    "next_action": str(latest["next_action"]),
                    "blocker_code": str(latest["blocker_code"]),
                    "created_at": str(latest["created_at"]),
                }
                if latest is not None
                else None
            ),
        }

    # ------------------------------------------------------------------
    # Durable setup and ownership
    # ------------------------------------------------------------------

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS workflow_campaign_runs (
                campaign_id TEXT PRIMARY KEY,
                owner_host_id TEXT NOT NULL,
                target_confirmed INTEGER NOT NULL,
                review_size INTEGER NOT NULL,
                max_submissions INTEGER NOT NULL,
                status TEXT NOT NULL,
                search_config_sha256 TEXT NOT NULL,
                search_scope_json TEXT NOT NULL,
                search_tier INTEGER NOT NULL,
                next_query_batch INTEGER NOT NULL,
                active_run_id TEXT NOT NULL DEFAULT '',
                active_approval_id TEXT NOT NULL DEFAULT '',
                active_review_digest TEXT NOT NULL DEFAULT '',
                active_request_id TEXT NOT NULL DEFAULT '',
                blocker_code TEXT NOT NULL DEFAULT '',
                no_progress_cycles INTEGER NOT NULL DEFAULT 0,
                checkpoint_sequence INTEGER NOT NULL DEFAULT 0,
                progress_fingerprint TEXT NOT NULL DEFAULT '',
                last_progress_at TEXT NOT NULL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS workflow_campaign_discovery_batches (
                campaign_id TEXT NOT NULL,
                batch_index INTEGER NOT NULL,
                tier INTEGER NOT NULL,
                terms_json TEXT NOT NULL,
                locations_json TEXT NOT NULL,
                state TEXT NOT NULL,
                plan_path TEXT NOT NULL DEFAULT '',
                workflow_run_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (campaign_id, batch_index),
                FOREIGN KEY (campaign_id) REFERENCES workflow_campaign_runs(campaign_id)
            );

            CREATE TABLE IF NOT EXISTS workflow_campaign_run_workflows (
                campaign_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                tier INTEGER NOT NULL,
                attached_at TEXT NOT NULL,
                PRIMARY KEY (campaign_id, run_id),
                FOREIGN KEY (campaign_id) REFERENCES workflow_campaign_runs(campaign_id),
                FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS workflow_campaign_run_candidates (
                campaign_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                review_packet_digest TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (campaign_id, run_id, candidate_id),
                FOREIGN KEY (campaign_id) REFERENCES workflow_campaign_runs(campaign_id),
                FOREIGN KEY (run_id, candidate_id)
                    REFERENCES workflow_candidates(run_id, candidate_id)
            );

            CREATE TABLE IF NOT EXISTS workflow_campaign_browser_jobs (
                request_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                request_path TEXT NOT NULL,
                response_path TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,
                claim_expires_at TEXT NOT NULL,
                result_status TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (campaign_id) REFERENCES workflow_campaign_runs(campaign_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_campaign_one_active_browser_job
                ON workflow_campaign_browser_jobs(campaign_id)
                WHERE state IN ('pending', 'claimed');

            CREATE TABLE IF NOT EXISTS workflow_campaign_review_packets (
                campaign_id TEXT NOT NULL,
                packet_digest TEXT NOT NULL,
                packet_path TEXT NOT NULL,
                candidates_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (campaign_id, packet_digest),
                FOREIGN KEY (campaign_id) REFERENCES workflow_campaign_runs(campaign_id)
            );

            CREATE TABLE IF NOT EXISTS workflow_campaign_review_approvals (
                campaign_id TEXT NOT NULL,
                packet_digest TEXT NOT NULL,
                approval_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (campaign_id, packet_digest, approval_id),
                FOREIGN KEY (campaign_id) REFERENCES workflow_campaign_runs(campaign_id),
                FOREIGN KEY (approval_id) REFERENCES workflow_approvals(approval_id)
            );

            CREATE TABLE IF NOT EXISTS workflow_campaign_checkpoints (
                campaign_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                action TEXT NOT NULL,
                next_action TEXT NOT NULL,
                blocker_code TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                snapshot_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (campaign_id, sequence),
                FOREIGN KEY (campaign_id) REFERENCES workflow_campaign_runs(campaign_id)
            );

            CREATE TABLE IF NOT EXISTS workflow_historical_applications (
                identity_key TEXT PRIMARY KEY,
                canonical_url TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                requisition_id TEXT NOT NULL DEFAULT '',
                source_digest TEXT NOT NULL,
                evidence_state TEXT NOT NULL,
                source_path TEXT NOT NULL,
                imported_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_historical_application_url
                ON workflow_historical_applications(canonical_url);
            """
        )
        existing = self.connection.execute(
            "SELECT value FROM workflow_meta WHERE key = 'campaign_run_schema_version'"
        ).fetchone()
        if existing is not None and existing["value"] != CAMPAIGN_RUN_SCHEMA_VERSION:
            raise CampaignRunError("unsupported campaign-run schema")
        self.connection.execute(
            """
            INSERT OR IGNORE INTO workflow_meta(key, value)
            VALUES('campaign_run_schema_version', ?)
            """,
            (CAMPAIGN_RUN_SCHEMA_VERSION,),
        )
        self.connection.commit()

    def _host_id(self, *, create: bool) -> str:
        if self._host_id_override:
            _require_safe_id(self._host_id_override, "host id")
            return self._host_id_override
        path = self.data_root / "campaign-run-host-id"
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            _require_safe_id(value, "host id")
            return value
        if not create:
            return ""
        value = "host-" + secrets.token_hex(16)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return value

    def _require_owner(self, campaign: sqlite3.Row) -> None:
        if str(campaign["owner_host_id"]) != self._host_id(create=True):
            raise CampaignRunError("campaign mutations are bound to another host")

    @contextmanager
    def _controller_lease(self, campaign_id: str, owner: str) -> Iterator[None]:
        now = self._now_utc()
        expires = now + timedelta(seconds=CONTROLLER_LEASE_SECONDS)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._campaign(campaign_id)
            current_owner = str(row["lease_owner"] or "")
            current_expiry = _optional_time(str(row["lease_expires_at"] or ""))
            if current_owner and current_owner != owner and current_expiry and current_expiry > now:
                raise CampaignRunError("campaign controller lease is held by another process")
            self.connection.execute(
                """
                UPDATE workflow_campaign_runs
                SET lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE campaign_id = ?
                """,
                (owner, expires.isoformat(), now.isoformat(), campaign_id),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        try:
            yield
        finally:
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE workflow_campaign_runs
                    SET lease_owner = '', lease_expires_at = '', updated_at = ?
                    WHERE campaign_id = ? AND lease_owner = ?
                    """,
                    (self._timestamp(), campaign_id, owner),
                )

    # ------------------------------------------------------------------
    # Candidate inventory, discovery, and history
    # ------------------------------------------------------------------

    def attach_workflow_run(self, *, campaign_id: str, run_id: str, tier: int) -> None:
        self._require_owner(self._campaign(campaign_id))
        _require_safe_id(run_id, "workflow run id")
        if self.connection.execute(
            "SELECT 1 FROM workflow_runs WHERE run_id = ?", (run_id,)
        ).fetchone() is None:
            raise CampaignRunError("workflow run does not exist")
        now = self._timestamp()
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO workflow_campaign_run_workflows(
                    campaign_id, run_id, tier, attached_at
                ) VALUES(?, ?, ?, ?)
                """,
                (campaign_id, run_id, max(1, min(3, int(tier))), now),
            )
        self._enroll_candidates(campaign_id, run_id)

    def import_history_ledgers(
        self,
        paths: Iterable[Path],
        *,
        campaign_id: str,
    ) -> int:
        self._require_owner(self._campaign(campaign_id))
        imported = 0
        for raw_path in paths:
            path = raw_path.expanduser().resolve()
            if not path.is_file():
                raise CampaignRunError(f"historical ledger does not exist: {path}")
            for identity in _historical_identities(path):
                with self.connection:
                    cursor = self.connection.execute(
                        """
                        INSERT OR IGNORE INTO workflow_historical_applications(
                            identity_key, canonical_url, provider, requisition_id,
                            source_digest, evidence_state, source_path, imported_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            identity.identity_key,
                            identity.canonical_url,
                            identity.provider,
                            identity.requisition_id,
                            identity.source_digest,
                            identity.evidence_state,
                            str(path),
                            self._timestamp(),
                        ),
                    )
                imported += max(cursor.rowcount, 0)
        return imported

    def _enroll_new_workflow_runs(self, campaign_id: str) -> None:
        campaign = self._campaign(campaign_id)
        rows = self.connection.execute(
            """
            SELECT run_id FROM workflow_runs
            WHERE created_at >= ?
              AND run_id NOT IN (
                SELECT run_id FROM workflow_campaign_run_workflows
                WHERE campaign_id = ?
              )
            ORDER BY created_at, run_id
            """,
            (campaign["started_at"], campaign_id),
        ).fetchall()
        for row in rows:
            self.attach_workflow_run(
                campaign_id=campaign_id,
                run_id=str(row["run_id"]),
                tier=self._current_search_tier(campaign_id),
            )
        attached = self.connection.execute(
            "SELECT run_id FROM workflow_campaign_run_workflows WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchall()
        for row in attached:
            self._enroll_candidates(campaign_id, str(row["run_id"]))

    def _enroll_candidates(self, campaign_id: str, run_id: str) -> None:
        rows = self.connection.execute(
            """
            SELECT * FROM workflow_candidates
            WHERE run_id = ? AND canonical_url != ''
              AND opportunity_kind = 'posted_employment'
              AND application_surface IN (
                  'provider_requisition',
                  'job_posting_structured_data',
                  'job_application_form'
              )
              AND COALESCE(fit_score, 0) >= 70
            ORDER BY fit_score DESC, candidate_id
            """,
            (run_id,),
        ).fetchall()
        now = self._timestamp()
        for row in rows:
            decision = "pending"
            historical_state = self._historical_match_state(row)
            if historical_state == "ambiguous_claim":
                decision = "historical_ambiguous"
            elif historical_state:
                decision = "historical_duplicate"
            elif not _campaign_title_allowed(str(row["title"] or "")):
                decision = "blocked"
            elif str(row["state"]) in BLOCKED_CANDIDATE_STATES:
                decision = "blocked"
            with self.connection:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO workflow_campaign_run_candidates(
                        campaign_id, run_id, candidate_id, decision,
                        review_packet_digest, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, '', ?, ?)
                    """,
                    (
                        campaign_id,
                        run_id,
                        row["candidate_id"],
                        decision,
                        now,
                        now,
                    ),
                )
                if decision in {"historical_duplicate", "historical_ambiguous"}:
                    self.connection.execute(
                        """
                        UPDATE workflow_campaign_run_candidates
                        SET decision = ?, updated_at = ?
                        WHERE campaign_id = ? AND run_id = ? AND candidate_id = ?
                          AND decision IN ('pending', 'deferred')
                        """,
                        (
                            decision,
                            now,
                            campaign_id,
                            run_id,
                            row["candidate_id"],
                        ),
                    )

    def _historical_match_state(self, candidate: sqlite3.Row) -> str:
        keys = _candidate_identity_keys(candidate)
        if not keys:
            return ""
        placeholders = ",".join("?" for _ in keys)
        row = self.connection.execute(
            f"""
            SELECT evidence_state FROM workflow_historical_applications
            WHERE identity_key IN ({placeholders})
              AND evidence_state IN ('receipt_supported', 'ambiguous_claim')
            ORDER BY CASE evidence_state
                WHEN 'ambiguous_claim' THEN 0 ELSE 1 END
            LIMIT 1
            """,
            tuple(keys),
        ).fetchone()
        return str(row["evidence_state"]) if row is not None else ""

    def _reconcile_candidates(self, campaign_id: str) -> None:
        rows = self.connection.execute(
            """
            SELECT m.run_id, m.candidate_id, m.decision, c.state, c.outcome
            FROM workflow_campaign_run_candidates m
            JOIN workflow_candidates c
              ON c.run_id = m.run_id AND c.candidate_id = m.candidate_id
            WHERE m.campaign_id = ?
            """,
            (campaign_id,),
        ).fetchall()
        now = self._timestamp()
        for row in rows:
            if row["outcome"] == COUNTED_OUTCOME:
                continue
            if row["state"] in BLOCKED_CANDIDATE_STATES and row["decision"] not in {
                "rejected",
                "historical_duplicate",
            }:
                with self.connection:
                    self.connection.execute(
                        """
                        UPDATE workflow_campaign_run_candidates
                        SET decision = 'blocked', updated_at = ?
                        WHERE campaign_id = ? AND run_id = ? AND candidate_id = ?
                        """,
                        (now, campaign_id, row["run_id"], row["candidate_id"]),
                    )

    def _next_material_candidate(self, campaign_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT m.run_id, m.candidate_id, c.fit_score
            FROM workflow_campaign_run_candidates m
            JOIN workflow_candidates c
              ON c.run_id = m.run_id AND c.candidate_id = m.candidate_id
            WHERE m.campaign_id = ? AND m.decision IN ('pending', 'deferred')
              AND c.state = 'materials_ready'
              AND NOT EXISTS (
                SELECT 1 FROM workflow_campaign_browser_jobs b
                WHERE b.campaign_id = m.campaign_id
                  AND b.run_id = m.run_id AND b.candidate_id = m.candidate_id
                  AND b.mode = 'dry_run' AND b.state != 'imported'
              )
            ORDER BY c.fit_score DESC, m.run_id, m.candidate_id LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()

    def _review_candidates(self, campaign_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT m.run_id, m.candidate_id, c.company, c.title,
                   c.canonical_url, c.fit_score, c.material_digest,
                   c.form_review_digest, c.form_review_json
            FROM workflow_campaign_run_candidates m
            JOIN workflow_candidates c
              ON c.run_id = m.run_id AND c.candidate_id = m.candidate_id
            WHERE m.campaign_id = ? AND m.decision IN ('pending', 'deferred')
              AND c.state = 'dry_run_ready'
            ORDER BY CASE m.decision WHEN 'pending' THEN 0 ELSE 1 END,
                     c.fit_score DESC, m.run_id, m.candidate_id
            """,
            (campaign_id,),
        ).fetchall()

    def _workflow_action_required(self, campaign_id: str) -> str | None:
        row = self.connection.execute(
            """
            SELECT w.run_id, r.status
            FROM workflow_campaign_run_workflows w
            JOIN workflow_runs r ON r.run_id = w.run_id
            WHERE w.campaign_id = ?
              AND (
                r.status IN ('awaiting_discovery', 'awaiting_chatgpt_web')
                OR EXISTS (
                    SELECT 1 FROM workflow_campaign_run_candidates m
                    JOIN workflow_candidates c
                      ON c.run_id = m.run_id AND c.candidate_id = m.candidate_id
                    WHERE m.campaign_id = w.campaign_id AND m.run_id = w.run_id
                      AND m.decision IN ('pending', 'deferred')
                      AND c.state = 'verified'
                )
              )
            ORDER BY r.created_at, w.run_id LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()
        return str(row["run_id"]) if row is not None else None

    # ------------------------------------------------------------------
    # Browser, approvals, and review packets
    # ------------------------------------------------------------------

    def _register_browser_job(self, campaign_id: str, request_path: Path) -> sqlite3.Row:
        request = _read_json(request_path)
        request_id = str(request.get("request_id") or "")
        _require_safe_id(request_id, "browser request id")
        now = self._now_utc()
        request_sha = hashlib.sha256(request_path.read_bytes()).hexdigest()
        response_path = str(request.get("response_path") or "")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO workflow_campaign_browser_jobs(
                    request_id, campaign_id, run_id, candidate_id, mode,
                    request_path, response_path, request_sha256, state,
                    claim_expires_at, result_status, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'claimed', ?, '', ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    claim_expires_at = CASE
                        WHEN workflow_campaign_browser_jobs.state = 'imported'
                        THEN workflow_campaign_browser_jobs.claim_expires_at
                        ELSE excluded.claim_expires_at END,
                    updated_at = excluded.updated_at
                """,
                (
                    request_id,
                    campaign_id,
                    request["run_id"],
                    request["candidate_id"],
                    request["mode"],
                    str(request_path.resolve()),
                    response_path,
                    request_sha,
                    (now + timedelta(seconds=BROWSER_CLAIM_SECONDS)).isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self.connection.execute(
                """
                UPDATE workflow_campaign_runs
                SET active_request_id = ?, active_run_id = ?, updated_at = ?
                WHERE campaign_id = ?
                """,
                (request_id, request["run_id"], now.isoformat(), campaign_id),
            )
        return self.connection.execute(
            "SELECT * FROM workflow_campaign_browser_jobs WHERE request_id = ?",
            (request_id,),
        ).fetchone()

    def _pending_browser_job(self, campaign_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM workflow_campaign_browser_jobs
            WHERE campaign_id = ? AND state IN ('pending', 'claimed')
            ORDER BY created_at, request_id LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()

    def _resume_browser_job(self, campaign_id: str, job: sqlite3.Row) -> dict[str, Any]:
        request_path = Path(str(job["request_path"])).resolve()
        if not request_path.is_file() or hashlib.sha256(request_path.read_bytes()).hexdigest() != job[
            "request_sha256"
        ]:
            return self._block(
                campaign_id,
                status="blocked",
                blocker_code="browser_request_missing_or_changed",
                next_action="restore_exact_browser_request",
            )
        expires = _optional_time(str(job["claim_expires_at"] or ""))
        if expires is not None and expires <= self._now_utc():
            if job["mode"] == "submit":
                return self._block(
                    campaign_id,
                    status="outcome_review_required",
                    blocker_code="submit_worker_lost_after_request",
                    next_action="reconcile_unknown_submission_outcome",
                )
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE workflow_campaign_browser_jobs
                    SET state = 'claimed', claim_expires_at = ?, updated_at = ?
                    WHERE request_id = ?
                    """,
                    (
                        (self._now_utc() + timedelta(seconds=BROWSER_CLAIM_SECONDS)).isoformat(),
                        self._timestamp(),
                        job["request_id"],
                    ),
                )
            job = self.connection.execute(
                "SELECT * FROM workflow_campaign_browser_jobs WHERE request_id = ?",
                (job["request_id"],),
            ).fetchone()
        return self._emit_browser_request(campaign_id, job, made_progress=False)

    def _emit_browser_request(
        self,
        campaign_id: str,
        job: sqlite3.Row,
        *,
        made_progress: bool,
    ) -> dict[str, Any]:
        return self._emit(
            campaign_id,
            action=StepAction.BROWSER_ACTION_REQUIRED,
            next_action=f"service_{job['mode']}_browser_request",
            blocker_code="",
            made_progress=made_progress,
            waiting_external=made_progress,
            request_id=str(job["request_id"]),
            request_path=str(job["request_path"]),
            browser_mode=str(job["mode"]),
            browser_claim_expires_at=str(job["claim_expires_at"]),
        )

    def _create_review_packet(
        self,
        campaign_id: str,
        candidates: Iterable[sqlite3.Row],
    ) -> dict[str, str]:
        rows: list[dict[str, Any]] = []
        for candidate in candidates:
            review = json.loads(str(candidate["form_review_json"] or "{}"))
            rows.append(
                {
                    "member_id": _member_key(
                        str(candidate["run_id"]), str(candidate["candidate_id"])
                    ),
                    "run_id": str(candidate["run_id"]),
                    "candidate_id": str(candidate["candidate_id"]),
                    "company": str(candidate["company"]),
                    "title": str(candidate["title"]),
                    "official_url": str(candidate["canonical_url"]),
                    "fit_score": int(candidate["fit_score"] or 0),
                    "material_digest": str(candidate["material_digest"]),
                    "form_review_digest": str(candidate["form_review_digest"]),
                    "ats_family": str(review.get("ats_family") or ""),
                    "evidence_artifacts": list(review.get("evidence_artifacts") or []),
                }
            )
        if not rows:
            raise CampaignRunError("cannot create an empty review packet")
        payload = {
            "schema_version": REVIEW_PACKET_SCHEMA_VERSION,
            "campaign_id": campaign_id,
            "created_at": self._timestamp(),
            "classification_contract": {
                "required_decisions": ["approved", "rejected", "deferred"],
                "max_approved": int(self._campaign(campaign_id)["max_submissions"]),
                "all_candidates_must_be_classified": True,
                "approval_valid_hours": 24,
            },
            "candidates": rows,
        }
        digest = _sha256_json(payload)
        packet_dir = self._campaign_dir(campaign_id) / "review-packets"
        packet_path = packet_dir / f"review.{digest[:16]}.json"
        _write_private_json(packet_path, payload)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO workflow_campaign_review_packets(
                    campaign_id, packet_digest, packet_path, candidates_json,
                    status, created_at, completed_at
                ) VALUES(?, ?, ?, ?, 'open', ?, '')
                """,
                (
                    campaign_id,
                    digest,
                    str(packet_path),
                    _canonical_json(rows),
                    payload["created_at"],
                ),
            )
            self.connection.execute(
                """
                UPDATE workflow_campaign_runs
                SET active_review_digest = ?, updated_at = ? WHERE campaign_id = ?
                """,
                (digest, self._timestamp(), campaign_id),
            )
        return {"packet_path": str(packet_path), "packet_digest": digest}

    def _open_review_packet(self, campaign_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM workflow_campaign_review_packets
            WHERE campaign_id = ? AND status = 'open'
            ORDER BY created_at LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()

    def _invalidate_review_packet(
        self,
        campaign_id: str,
        packet_digest: str,
        *,
        reason: str,
    ) -> None:
        now = self._timestamp()
        with self.connection:
            self.connection.execute(
                """
                UPDATE workflow_campaign_review_packets
                SET status = ?, completed_at = ?
                WHERE campaign_id = ? AND packet_digest = ? AND status = 'open'
                """,
                (f"invalidated:{reason}"[:120], now, campaign_id, packet_digest),
            )
            self.connection.execute(
                """
                UPDATE workflow_campaign_runs
                SET active_review_digest = CASE
                        WHEN active_review_digest = ? THEN ''
                        ELSE active_review_digest
                    END,
                    blocker_code = ?, updated_at = ?
                WHERE campaign_id = ?
                """,
                (packet_digest, str(reason)[:120], now, campaign_id),
            )

    def _next_approval(self, campaign_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT q.*, a.expires_at, a.status AS approval_status,
                   a.candidate_ids_json, a.consumed_count, a.max_submissions
            FROM workflow_campaign_review_approvals q
            JOIN workflow_approvals a ON a.approval_id = q.approval_id
            WHERE q.campaign_id = ? AND q.status IN ('queued', 'active')
            ORDER BY q.created_at, q.position, q.approval_id LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()

    def _approval_state(self, campaign_id: str, approval: sqlite3.Row) -> str:
        approval_id = str(approval["approval_id"])
        expires = datetime.fromisoformat(str(approval["expires_at"])).astimezone(
            timezone.utc
        )
        if expires <= self._now_utc():
            candidate_ids = json.loads(str(approval["candidate_ids_json"]))
            for candidate_id in candidate_ids:
                row = self.connection.execute(
                    """
                    SELECT c.canonical_url, c.state
                    FROM workflow_candidates c WHERE c.run_id = ? AND c.candidate_id = ?
                    """,
                    (approval["run_id"], candidate_id),
                ).fetchone()
                if row is None:
                    continue
                attempted = self.connection.execute(
                    "SELECT 1 FROM workflow_submission_registry WHERE canonical_url = ?",
                    (row["canonical_url"],),
                ).fetchone()
                if attempted is None:
                    self.workflow.invalidate_form_review(
                        run_id=str(approval["run_id"]),
                        candidate_id=str(candidate_id),
                        reason="exact_candidate_approval_expired",
                    )
                    with self.connection:
                        self.connection.execute(
                            """
                            UPDATE workflow_campaign_run_candidates
                            SET decision = 'pending', review_packet_digest = '', updated_at = ?
                            WHERE campaign_id = ? AND run_id = ? AND candidate_id = ?
                            """,
                            (
                                self._timestamp(),
                                campaign_id,
                                approval["run_id"],
                                candidate_id,
                            ),
                        )
            with self.connection:
                self.connection.execute(
                    "UPDATE workflow_approvals SET status = 'expired' WHERE approval_id = ?",
                    (approval_id,),
                )
                self.connection.execute(
                    """
                    UPDATE workflow_campaign_review_approvals
                    SET status = 'expired', updated_at = ? WHERE approval_id = ?
                    """,
                    (self._timestamp(), approval_id),
                )
            return "expired"
        if approval["approval_status"] == "consumed" or int(
            approval["consumed_count"]
        ) >= int(approval["max_submissions"]):
            self._finish_approval(campaign_id, approval_id)
            return "complete"
        with self.connection:
            self.connection.execute(
                """
                UPDATE workflow_campaign_review_approvals
                SET status = 'active', updated_at = ? WHERE approval_id = ?
                """,
                (self._timestamp(), approval_id),
            )
            self.connection.execute(
                """
                UPDATE workflow_campaign_runs
                SET active_approval_id = ?, active_run_id = ?, updated_at = ?
                WHERE campaign_id = ?
                """,
                (approval_id, approval["run_id"], self._timestamp(), campaign_id),
            )
        return "active"

    def _finish_approval(self, campaign_id: str, approval_id: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE workflow_campaign_review_approvals
                SET status = 'completed', updated_at = ? WHERE approval_id = ?
                """,
                (self._timestamp(), approval_id),
            )
            self.connection.execute(
                """
                UPDATE workflow_campaign_runs
                SET active_approval_id = CASE WHEN active_approval_id = ? THEN ''
                                              ELSE active_approval_id END,
                    updated_at = ? WHERE campaign_id = ?
                """,
                (approval_id, self._timestamp(), campaign_id),
            )

    # ------------------------------------------------------------------
    # Discovery plan state
    # ------------------------------------------------------------------

    def _next_discovery_batch(self, campaign_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM workflow_campaign_discovery_batches
            WHERE campaign_id = ? AND state = 'pending'
            ORDER BY batch_index LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()

    def _issued_discovery_batch(self, campaign_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM workflow_campaign_discovery_batches
            WHERE campaign_id = ? AND state = 'issued'
            ORDER BY batch_index LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()

    def _issue_discovery_plan(self, campaign_id: str, batch: sqlite3.Row) -> Path:
        campaign = self._campaign(campaign_id)
        scope = json.loads(str(campaign["search_scope_json"]))
        plan = {
            "schema_version": DISCOVERY_PLAN_SCHEMA_VERSION,
            "campaign_id": campaign_id,
            "batch_index": int(batch["batch_index"]),
            "tier": int(batch["tier"]),
            "terms": json.loads(str(batch["terms_json"])),
            "locations": json.loads(str(batch["locations_json"])),
            "sources": scope["sources"],
            "candidate_contract": {
                "minimum_fit_score": 70,
                "posted_employment_only": True,
                "job_specific_first_party_resolution_required": True,
                "early_career_required": True,
                "talent_pools_counted": False,
                "general_interest_counted": False,
                "speculative_outreach_counted": False,
            },
            "operator_next_step": (
                "Run bounded aggregation and prepare from its immutable snapshot; "
                "then call campaign-run step with --workflow-run-id."
            ),
        }
        plan_dir = self._campaign_dir(campaign_id) / "discovery-plans"
        path = plan_dir / f"discovery.{int(batch['batch_index']):03d}.json"
        _write_private_json(path, plan)
        with self.connection:
            self.connection.execute(
                """
                UPDATE workflow_campaign_discovery_batches
                SET state = 'issued', plan_path = ?, updated_at = ?
                WHERE campaign_id = ? AND batch_index = ?
                """,
                (str(path), self._timestamp(), campaign_id, batch["batch_index"]),
            )
            self.connection.execute(
                """
                UPDATE workflow_campaign_runs
                SET search_tier = ?, next_query_batch = ?, updated_at = ?
                WHERE campaign_id = ?
                """,
                (
                    int(batch["tier"]),
                    int(batch["batch_index"]),
                    self._timestamp(),
                    campaign_id,
                ),
            )
        return path

    def _attach_issued_discovery_batch(self, campaign_id: str, run_id: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE workflow_campaign_discovery_batches
                SET state = 'attached', workflow_run_id = ?, updated_at = ?
                WHERE campaign_id = ? AND batch_index = (
                    SELECT batch_index FROM workflow_campaign_discovery_batches
                    WHERE campaign_id = ? AND state = 'issued'
                    ORDER BY batch_index LIMIT 1
                )
                """,
                (run_id, self._timestamp(), campaign_id, campaign_id),
            )

    def _reconcile_discovery_batches(self, campaign_id: str) -> None:
        rows = self.connection.execute(
            """
            SELECT b.batch_index, b.workflow_run_id, r.status
            FROM workflow_campaign_discovery_batches b
            JOIN workflow_runs r ON r.run_id = b.workflow_run_id
            WHERE b.campaign_id = ? AND b.state = 'attached'
            """,
            (campaign_id,),
        ).fetchall()
        terminal = {
            "review_ready",
            "no_eligible_verified_roles",
            "form_review_blocked",
            "failed_closed",
        }
        for row in rows:
            if row["status"] in terminal:
                with self.connection:
                    self.connection.execute(
                        """
                        UPDATE workflow_campaign_discovery_batches
                        SET state = 'completed', updated_at = ?
                        WHERE campaign_id = ? AND batch_index = ?
                        """,
                        (self._timestamp(), campaign_id, row["batch_index"]),
                    )

    def _search_exhausted(self, campaign_id: str) -> bool:
        return self.connection.execute(
            """
            SELECT 1 FROM workflow_campaign_discovery_batches
            WHERE campaign_id = ? AND state != 'completed' LIMIT 1
            """,
            (campaign_id,),
        ).fetchone() is None

    # ------------------------------------------------------------------
    # Checkpointing and counters
    # ------------------------------------------------------------------

    def _emit(
        self,
        campaign_id: str,
        *,
        action: StepAction,
        next_action: str,
        blocker_code: str,
        made_progress: bool,
        waiting_external: bool,
        **extra: Any,
    ) -> dict[str, Any]:
        campaign = self._campaign(campaign_id)
        cycles = int(campaign["no_progress_cycles"])
        now = self._timestamp()
        if made_progress:
            cycles = 0
            last_progress = now
        elif waiting_external:
            last_progress = str(campaign["last_progress_at"])
        else:
            cycles += 1
            last_progress = str(campaign["last_progress_at"])
        if cycles >= 2 and campaign["status"] == "active":
            action = StepAction.HUMAN_BLOCKED
            next_action = "resolve_no_progress_blocker"
            blocker_code = blocker_code or "two_no_progress_cycles"
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE workflow_campaign_runs SET status = 'blocked'
                    WHERE campaign_id = ?
                    """,
                    (campaign_id,),
                )
        with self.connection:
            self.connection.execute(
                """
                UPDATE workflow_campaign_runs
                SET no_progress_cycles = ?, blocker_code = ?,
                    last_progress_at = ?, updated_at = ?
                WHERE campaign_id = ?
                """,
                (cycles, blocker_code, last_progress, now, campaign_id),
            )

        status = self.status(campaign_id)
        fingerprint = self._progress_fingerprint(campaign_id)
        sequence = int(status["checkpoint_sequence"]) + 1
        payload = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "campaign_id": campaign_id,
            "sequence": sequence,
            "recorded_at": now,
            "campaign_status": status["status"],
            "target_confirmed": status["target_confirmed"],
            "submitted_confirmed": status["submitted_confirmed"],
            "remaining": status["remaining"],
            "counts": status["counts"],
            "search_tier": status["search_tier"],
            "active_run_id": status["active_run_id"],
            "active_approval_id": status["active_approval_id"],
            "active_request_id": status["active_request_id"],
            "action": action.value,
            "last_verified_transition": action.value,
            "next_action": next_action,
            "blocker_code": blocker_code,
            "no_progress_cycles": cycles,
            "last_progress_at": last_progress,
            "progress_fingerprint": fingerprint,
        }
        for key in (
            "request_id",
            "request_path",
            "browser_mode",
            "browser_claim_expires_at",
            "review_packet_path",
            "review_packet_digest",
            "discovery_plan_path",
            "workflow_run_id",
            "approval_ids",
        ):
            if key in extra:
                payload[key] = extra[key]
        encoded = _canonical_json(payload).encode("utf-8")
        if len(encoded) > MAX_CHECKPOINT_BYTES:
            raise CampaignRunError("compact checkpoint exceeds 16 KiB")
        snapshot_sha = hashlib.sha256(encoded).hexdigest()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO workflow_campaign_checkpoints(
                    campaign_id, sequence, action, next_action, blocker_code,
                    snapshot_json, snapshot_sha256, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    sequence,
                    action.value,
                    next_action,
                    blocker_code,
                    encoded.decode("utf-8"),
                    snapshot_sha,
                    now,
                ),
            )
            self.connection.execute(
                """
                UPDATE workflow_campaign_runs
                SET checkpoint_sequence = ?, progress_fingerprint = ?, updated_at = ?
                WHERE campaign_id = ?
                """,
                (sequence, fingerprint, now, campaign_id),
            )
        _write_private_json(self._checkpoint_path(campaign_id), payload)
        result = dict(payload)
        result.update(extra)
        result["checkpoint_path"] = str(self._checkpoint_path(campaign_id))
        result["checkpoint_sha256"] = snapshot_sha
        return result

    def _block(
        self,
        campaign_id: str,
        *,
        status: str,
        blocker_code: str,
        next_action: str,
    ) -> dict[str, Any]:
        with self.connection:
            self.connection.execute(
                """
                UPDATE workflow_campaign_runs
                SET status = ?, blocker_code = ?, updated_at = ?
                WHERE campaign_id = ?
                """,
                (status, blocker_code, self._timestamp(), campaign_id),
            )
        return self._emit(
            campaign_id,
            action=StepAction.HUMAN_BLOCKED,
            next_action=next_action,
            blocker_code=blocker_code,
            made_progress=True,
            waiting_external=True,
        )

    def _counts(self, campaign_id: str) -> dict[str, int]:
        campaign = self._campaign(campaign_id)
        state_counts = {
            str(row["state"]): int(row["count"])
            for row in self.connection.execute(
                """
                SELECT c.state, COUNT(*) AS count
                FROM workflow_campaign_run_candidates m
                JOIN workflow_candidates c
                  ON c.run_id = m.run_id AND c.candidate_id = m.candidate_id
                WHERE m.campaign_id = ?
                  AND m.decision IN ('pending', 'deferred', 'approved')
                GROUP BY c.state
                """,
                (campaign_id,),
            ).fetchall()
        }
        decision_counts = {
            str(row["decision"]): int(row["count"])
            for row in self.connection.execute(
                """
                SELECT decision, COUNT(*) AS count
                FROM workflow_campaign_run_candidates
                WHERE campaign_id = ? GROUP BY decision
                """,
                (campaign_id,),
            ).fetchall()
        }
        outcomes = {
            str(row["outcome"]): int(row["count"])
            for row in self.connection.execute(
                """
                SELECT r.outcome, COUNT(DISTINCT r.canonical_url) AS count
                FROM workflow_submission_registry r
                JOIN workflow_campaign_run_candidates m
                  ON m.run_id = r.run_id AND m.candidate_id = r.candidate_id
                WHERE m.campaign_id = ? AND m.decision = 'approved'
                  AND r.attempted_at >= ?
                GROUP BY r.outcome
                """,
                (campaign_id, campaign["started_at"]),
            ).fetchall()
        }
        return {
            COUNTED_OUTCOME: outcomes.get(COUNTED_OUTCOME, 0),
            AMBIGUOUS_OUTCOME: outcomes.get(AMBIGUOUS_OUTCOME, 0),
            "not_submitted": outcomes.get("not_submitted", 0),
            "reserved": outcomes.get("reserved", 0),
            "eligible_inventory": sum(
                state_counts.get(state, 0) for state in ELIGIBLE_CAMPAIGN_STATES
            ),
            "materials_ready": state_counts.get("materials_ready", 0),
            "dry_run_ready": state_counts.get("dry_run_ready", 0),
            "approved": decision_counts.get("approved", 0),
            "rejected": decision_counts.get("rejected", 0),
            "deferred": decision_counts.get("deferred", 0),
            "blocked": decision_counts.get("blocked", 0),
            "historical_duplicate": decision_counts.get("historical_duplicate", 0),
            "historical_ambiguous": decision_counts.get("historical_ambiguous", 0),
            "pending_browser_jobs": int(
                self.connection.execute(
                    """
                    SELECT COUNT(*) FROM workflow_campaign_browser_jobs
                    WHERE campaign_id = ? AND state IN ('pending', 'claimed')
                    """,
                    (campaign_id,),
                ).fetchone()[0]
            ),
        }

    def _progress_fingerprint(self, campaign_id: str) -> str:
        campaign = self._campaign(campaign_id)
        batches = [
            (int(row["batch_index"]), str(row["state"]), str(row["workflow_run_id"]))
            for row in self.connection.execute(
                """
                SELECT batch_index, state, workflow_run_id
                FROM workflow_campaign_discovery_batches
                WHERE campaign_id = ? ORDER BY batch_index
                """,
                (campaign_id,),
            ).fetchall()
        ]
        semantic = {
            "status": campaign["status"],
            "counts": self._counts(campaign_id),
            "active_run_id": campaign["active_run_id"],
            "active_approval_id": campaign["active_approval_id"],
            "active_review_digest": campaign["active_review_digest"],
            "active_request_id": campaign["active_request_id"],
            "batches": batches,
        }
        return _sha256_json(semantic)

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _campaign(self, campaign_id: str) -> sqlite3.Row:
        row = self._campaign_optional(campaign_id)
        if row is None:
            raise CampaignRunError("campaign run does not exist")
        return row

    def _campaign_optional(self, campaign_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM workflow_campaign_runs WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()

    def _campaign_dir(self, campaign_id: str) -> Path:
        _require_safe_id(campaign_id, "campaign id")
        return self.data_root / "campaign-runs" / campaign_id

    def _checkpoint_path(self, campaign_id: str) -> Path:
        return self._campaign_dir(campaign_id) / "checkpoint.json"

    def _current_search_tier(self, campaign_id: str) -> int:
        return int(self._campaign(campaign_id)["search_tier"])

    def _lease_owner(self) -> str:
        return f"{self._host_id(create=True)}:pid-{os.getpid()}"

    def _now_utc(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise CampaignRunError("campaign clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _timestamp(self) -> str:
        return self._now_utc().isoformat()


def _search_scope(path: Path) -> tuple[dict[str, Any], str]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise CampaignRunError("search configuration does not exist")
    raw_bytes = path.read_bytes()
    payload = yaml.safe_load(raw_bytes.decode("utf-8")) or {}
    queries = payload.get("queries") or []
    grouped: dict[int, list[str]] = defaultdict(list)
    for item in queries:
        if not isinstance(item, Mapping):
            continue
        term = str(item.get("query") or "").strip()
        tier = int(item.get("tier") or 1)
        if term and 1 <= tier <= 3 and term not in grouped[tier]:
            grouped[tier].append(term)
    batches: list[dict[str, Any]] = []
    index = 0
    for tier in (1, 2, 3):
        terms = grouped.get(tier, [])
        for offset in range(0, len(terms), QUERY_BATCH_SIZE):
            batches.append(
                {
                    "batch_index": index,
                    "tier": tier,
                    "terms": terms[offset : offset + QUERY_BATCH_SIZE],
                }
            )
            index += 1
    if not batches:
        raise CampaignRunError("search configuration has no tiered queries")
    locations = [
        str(item.get("location") or "").strip()
        for item in payload.get("locations") or []
        if isinstance(item, Mapping) and str(item.get("location") or "").strip()
    ]
    discovery_mode = str(payload.get("discovery_mode") or "hybrid").strip().lower()
    direct_config = payload.get("direct_sources") or {}
    direct_first_party = [
        source
        for source, config_key in (
            ("workday", "workday"),
            ("direct_ats", "direct_ats"),
            ("smart_extract", "smartextract"),
        )
        if bool(direct_config.get(config_key, True))
    ]
    sources = {
        "discovery_mode": discovery_mode,
        "direct_first_party": direct_first_party,
        "board_leads": (
            ["jobspy"]
            if bool(payload.get("jobspy_enabled", discovery_mode != "direct_sources"))
            else []
        ),
        "portal_leads": (
            [] if discovery_mode == "direct_sources" else ["handshake", "runway"]
        ),
        "lead_policy": "resolve_to_job_specific_first_party_before_admission",
    }
    scope = {
        "query_batches": batches,
        "locations": locations,
        "sources": sources,
    }
    return scope, hashlib.sha256(raw_bytes).hexdigest()


def _historical_identities(path: Path) -> list[HistoricalIdentity]:
    text = path.read_text(encoding="utf-8")
    identities: list[HistoricalIdentity] = []
    sections = re.finditer(
        r"(?ms)^##\s+(Verified submissions|Submitted and verified|Submission ledger)\s*"
        r"(.*?)(?=^##\s|\Z)",
        text,
    )
    for section_match in sections:
        heading = section_match.group(1).lower()
        section = section_match.group(2)
        if heading == "submitted and verified":
            verification = re.search(r"(?mi)^Verification:\s*(.+)$", section)
            if verification is None:
                continue
            for bullet in re.finditer(r"(?m)^-\s+(.+)$", section):
                headline = str(bullet.group(1) or "").strip()
                block = f"{headline}\nVerification: {verification.group(1)}"
                identities.extend(_historical_block_identities(headline, block))
            continue
        starts = list(re.finditer(r"(?m)^\s*(\d+)\.\s+(.+)$", section))
        for index, start in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(section)
            block = section[start.start() : end].strip()
            if not re.search(r"(?mi)^\s*-\s*(?:Evidence|Receipt):\s*\S", block):
                continue
            identities.extend(
                _historical_block_identities(
                    str(start.group(2) or "").strip(),
                    block,
                )
            )
    unique: dict[str, HistoricalIdentity] = {}
    for identity in identities:
        unique.setdefault(identity.identity_key, identity)
    return list(unique.values())


def _historical_block_identities(
    headline: str,
    block: str,
) -> list[HistoricalIdentity]:
    source_digest = hashlib.sha256(block.encode("utf-8")).hexdigest()
    evidence_state = (
        "ambiguous_claim"
        if re.search(r"(?i)unconfirmed|could not confirm|ambiguous", block)
        else "receipt_supported"
    )
    identities: list[HistoricalIdentity] = []
    urls = re.findall(r"https?://[^\s)>]+", block)
    for raw_url in urls:
        try:
            url = canonicalize_url(raw_url.rstrip(".,"))
        except WorkflowError:
            continue
        identities.append(
            HistoricalIdentity(
                identity_key="url:" + url,
                canonical_url=url,
                provider=urlsplit(url).hostname or "",
                requisition_id="",
                evidence_state=evidence_state,
                source_digest=source_digest,
            )
        )
    provider_patterns = (
        ("greenhouse", r"(?i)greenhouse[^\n]*?\bjob\s+([A-Za-z0-9-]{3,})"),
        ("ashby", r"(?i)ashby[^\n]*?\bjob\s+([A-Za-z0-9-]{8,})"),
        ("handshake", r"(?i)handshake[^\n]*?\bjob\s+([0-9]{4,})"),
        (
            "workday",
            r"(?i)workday[^\n]*?\b(?:job|requisition)\s+([A-Za-z0-9_-]{4,})",
        ),
    )
    found_provider = False
    for provider, pattern in provider_patterns:
        for req_match in re.finditer(pattern, block):
            requisition_id = req_match.group(1).lower()
            for key in (f"{provider}:{requisition_id}", f"req:{requisition_id}"):
                identities.append(
                    HistoricalIdentity(
                        identity_key=key,
                        canonical_url="",
                        provider=provider,
                        requisition_id=requisition_id,
                        evidence_state=evidence_state,
                        source_digest=source_digest,
                    )
                )
            found_provider = True
    if not urls and not found_provider:
        parts = re.split(r"\s+[—–]\s+", headline)
        if len(parts) < 2 and " - " in headline:
            parts = headline.split(" - ", 1)
        if parts and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0].strip()):
            parts = parts[1:]
        if len(parts) >= 2:
            role_key = "role:" + "|".join(
                _normalized_identity_text(part) for part in parts[:2]
            )
            identities.append(
                HistoricalIdentity(
                    identity_key=role_key,
                    canonical_url="",
                    provider="role_fallback",
                    requisition_id="",
                    evidence_state=evidence_state,
                    source_digest=source_digest,
                )
            )
        else:
            identities.append(
                HistoricalIdentity(
                    identity_key="historical:" + source_digest,
                    canonical_url="",
                    provider="unknown",
                    requisition_id="",
                    evidence_state=evidence_state,
                    source_digest=source_digest,
                )
            )
    return identities


def _candidate_identity_keys(candidate: sqlite3.Row) -> set[str]:
    keys: set[str] = set()
    url = str(candidate["canonical_url"] or "")
    if url:
        keys.add("url:" + url)
    company = _normalized_identity_text(str(candidate["company"] or ""))
    title = _normalized_identity_text(str(candidate["title"] or ""))
    if company and title:
        keys.add(f"role:{company}|{title}")
    requisition_id = str(candidate["requisition_id"] or "").strip().lower()
    if not requisition_id:
        return keys
    keys.add("req:" + requisition_id)
    host = (urlsplit(url).hostname or "").lower()
    if "greenhouse" in host:
        keys.add("greenhouse:" + requisition_id)
    elif "ashby" in host:
        keys.add("ashby:" + requisition_id)
    elif "joinhandshake" in host or "handshake" in host:
        keys.add("handshake:" + requisition_id)
    elif "workday" in host:
        keys.add("workday:" + requisition_id)
    return keys


def _normalized_identity_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _campaign_title_allowed(title: str) -> bool:
    normalized = _normalized_identity_text(title)
    if re.search(
        r"\b(?:sales|business development|account executive|senior|sr|staff|"
        r"principal|director|vice president|vp|chief|lead|architect)\b",
        normalized,
    ):
        return False
    return bool(
        re.search(
            r"\b(?:intern|internship|co op|new grad|graduate|early career|"
            r"apprentice|fellow|associate|coordinator|analyst)\b",
            normalized,
        )
    )


def _member_key(run_id: str, candidate_id: str) -> str:
    _require_safe_id(run_id, "workflow run id")
    _require_safe_id(candidate_id, "candidate id")
    return f"{run_id}/{candidate_id}"


def _split_member_key(value: str) -> tuple[str, str]:
    parts = str(value).split("/", 1)
    if len(parts) != 2:
        raise CampaignRunError("candidate member id must be RUN_ID/CANDIDATE_ID")
    _require_safe_id(parts[0], "workflow run id")
    _require_safe_id(parts[1], "candidate id")
    return parts[0], parts[1]


def _unique_member_keys(values: Iterable[str]) -> set[str]:
    result: set[str] = set()
    for value in values:
        run_id, candidate_id = _split_member_key(str(value))
        result.add(_member_key(run_id, candidate_id))
    return result


def _require_safe_id(value: str, label: str) -> None:
    if not _SAFE_ID.fullmatch(str(value)):
        raise CampaignRunError(f"{label} is invalid")


def _optional_time(value: str) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise CampaignRunError("stored campaign timestamp is timezone-naive")
    return parsed.astimezone(timezone.utc)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _write_private_json(path: Path, payload: Any) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    temporary = path.with_name(path.name + f".tmp-{secrets.token_hex(6)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CampaignRunError("expected a JSON object")
    return value


def _error_code(prefix: str, exc: Exception) -> str:
    detail = re.sub(r"[^a-z0-9]+", "_", str(exc).lower()).strip("_")[:80]
    return f"{prefix}_{detail or 'failed'}"

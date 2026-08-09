"""Canonical single-user workflow state for reviewed job applications.

Autonomy artifacts and browser packets are transport envelopes.  This SQLite
store is the product source of truth for candidate state, exact approvals,
duplicate prevention, and durable outcomes.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from applypilot.apply.browser_actions import BrowserInterventionPolicy


WORKFLOW_SCHEMA_VERSION = "applypilot-workflow-v1"
LEGACY_BROWSER_ACTION_SCHEMA_VERSION = "applypilot-browser-action-v1"
BROWSER_ACTION_SCHEMA_VERSION = "applypilot-browser-action-v2"
SUPPORTED_BROWSER_ACTION_SCHEMA_VERSIONS = frozenset(
    {LEGACY_BROWSER_ACTION_SCHEMA_VERSION, BROWSER_ACTION_SCHEMA_VERSION}
)
MAX_EVIDENCE_ARTIFACTS = 5
MAX_EVIDENCE_BYTES = 25 * 1024 * 1024
CAMPAIGN_SEASONS = frozenset({"summer_2027", "fall_2026"})

TERMINAL_STATES = {
    "excluded",
    "blocked",
    "not_submitted",
    "submitted_unconfirmed",
    "submitted_confirmed",
}
STATE_ORDER = {
    "discovered": 0,
    "review_required": 1,
    "eligible": 2,
    "verified": 3,
    "materials_ready": 4,
    "dry_run_ready": 5,
    "authorized": 6,
    "submitting": 7,
    "not_submitted": 8,
    "blocked": 8,
    "submitted_unconfirmed": 9,
    "submitted_confirmed": 10,
    "excluded": 10,
}


def _browser_action_contract(
    *,
    policy: BrowserInterventionPolicy,
    mode: str,
) -> dict[str, list[str]]:
    """Render deterministic instructions for one visible-browser request."""
    if mode == "dry_run":
        base_allowed = [
            "navigate_visible_chrome",
            "fill_confirmed_fields",
            "upload_bound_materials",
            "reach_review_page",
            "capture_local_evidence",
        ]
        base_forbidden = ["submit_application"]
    elif mode == "submit":
        base_allowed = [
            "submit_application_once",
            "capture_confirmation_evidence",
        ]
        base_forbidden = []
    else:
        raise WorkflowError("unknown browser action mode")
    return {
        "allowed_actions": [
            *base_allowed,
            *policy.allowed_interventions(mode=mode),
        ],
        "forbidden_actions": [
            *base_forbidden,
            *policy.forbidden_actions(mode=mode),
        ],
        "intervention_rules": list(policy.handling_rules(mode=mode)),
        "response_requirements": [
            "Report every performed intervention in performed_interventions using an allowed action name.",
            "Report each accepted applicant confirmation digest in accepted_confirmation_sha256.",
            "When account creation is reported, include exact account_creation_evidence proving Google Password Manager populated the fields without reading them, the continuation was activated once, and the account gate cleared.",
            "Never ask the applicant for authentication input or takeover; if an allowed auth path cannot complete, return status blocked and one of these auth_blocker_code values: browser_managed_login_unavailable, credential_manager_unavailable, email_otp_unavailable, human_only_authentication_required, account_creation_unconfirmed.",
            "Never include a password, OTP value, mailbox body, cookie, token, or other secret in the response or evidence.",
        ],
    }


class WorkflowError(RuntimeError):
    """Raised when a workflow operation would violate a product boundary."""


class WorkflowStore:
    """Transactional candidate, approval, and outcome store."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "WorkflowStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS workflow_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS workflow_runs (
                run_id TEXT PRIMARY KEY,
                source_run_dir TEXT NOT NULL UNIQUE,
                query TEXT NOT NULL,
                fact_digest TEXT NOT NULL,
                context_digest TEXT NOT NULL,
                policy_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS workflow_candidates (
                run_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                company TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                compensation TEXT NOT NULL DEFAULT '',
                record_type TEXT NOT NULL DEFAULT 'discovery_lead',
                opportunity_kind TEXT NOT NULL DEFAULT 'unknown',
                application_surface TEXT NOT NULL DEFAULT 'unknown',
                requisition_id TEXT NOT NULL DEFAULT '',
                verified_description_digest TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                eligibility_decision TEXT NOT NULL DEFAULT '',
                eligibility_reasons_json TEXT NOT NULL DEFAULT '[]',
                freshness_decision TEXT NOT NULL DEFAULT '',
                freshness_reasons_json TEXT NOT NULL DEFAULT '[]',
                fit_score INTEGER,
                inclusion_reasons_json TEXT NOT NULL DEFAULT '[]',
                exclusion_reasons_json TEXT NOT NULL DEFAULT '[]',
                quality_gaps_json TEXT NOT NULL DEFAULT '[]',
                score_components_json TEXT NOT NULL DEFAULT '{}',
                material_paths_json TEXT NOT NULL DEFAULT '{}',
                material_digest TEXT NOT NULL DEFAULT '',
                form_review_json TEXT NOT NULL DEFAULT '{}',
                form_review_digest TEXT NOT NULL DEFAULT '',
                form_review_generation INTEGER NOT NULL DEFAULT 0,
                form_action_policy_json TEXT NOT NULL DEFAULT '{}',
                outcome TEXT NOT NULL DEFAULT '',
                evidence_path TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, candidate_id),
                FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_workflow_candidates_shortlist
                ON workflow_candidates(run_id, fit_score DESC, candidate_id);
            CREATE INDEX IF NOT EXISTS idx_workflow_candidates_url
                ON workflow_candidates(canonical_url);

            CREATE TABLE IF NOT EXISTS workflow_approvals (
                approval_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                candidate_ids_json TEXT NOT NULL,
                bindings_json TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                max_submissions INTEGER NOT NULL,
                consumed_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                campaign_id TEXT NOT NULL DEFAULT '',
                season TEXT NOT NULL DEFAULT '',
                action_policy_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS workflow_submission_registry (
                canonical_url TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                approval_id TEXT NOT NULL,
                attempted_at TEXT NOT NULL,
                evidence_path TEXT NOT NULL DEFAULT '',
                confirmation_kind TEXT NOT NULL DEFAULT '',
                confirmation_text TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS workflow_campaigns (
                campaign_id TEXT PRIMARY KEY,
                summer_target INTEGER NOT NULL,
                fall_target INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS workflow_campaign_entries (
                campaign_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                season TEXT NOT NULL,
                approval_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (campaign_id, run_id, candidate_id),
                FOREIGN KEY (campaign_id) REFERENCES workflow_campaigns(campaign_id),
                FOREIGN KEY (run_id, candidate_id) REFERENCES workflow_candidates(run_id, candidate_id)
            );
            CREATE INDEX IF NOT EXISTS idx_workflow_campaign_entries_campaign
                ON workflow_campaign_entries(campaign_id, season);

            CREATE TABLE IF NOT EXISTS workflow_campaign_replacements (
                campaign_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                season TEXT NOT NULL,
                category TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (campaign_id, run_id, candidate_id),
                FOREIGN KEY (campaign_id) REFERENCES workflow_campaigns(campaign_id),
                FOREIGN KEY (run_id, candidate_id) REFERENCES workflow_candidates(run_id, candidate_id)
            );
            CREATE INDEX IF NOT EXISTS idx_workflow_campaign_replacements_campaign
                ON workflow_campaign_replacements(campaign_id, season, category);

            CREATE TABLE IF NOT EXISTS workflow_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL,
                from_state TEXT NOT NULL DEFAULT '',
                to_state TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id)
            );
            """
        )
        candidate_columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(workflow_candidates)"
            ).fetchall()
        }
        candidate_column_defaults = {
            "verified_description_digest": "TEXT NOT NULL DEFAULT ''",
            "compensation": "TEXT NOT NULL DEFAULT ''",
            "quality_gaps_json": "TEXT NOT NULL DEFAULT '[]'",
            "score_components_json": "TEXT NOT NULL DEFAULT '{}'",
            "record_type": "TEXT NOT NULL DEFAULT 'discovery_lead'",
            "opportunity_kind": "TEXT NOT NULL DEFAULT 'unknown'",
            "application_surface": "TEXT NOT NULL DEFAULT 'unknown'",
            "requisition_id": "TEXT NOT NULL DEFAULT ''",
            "form_review_generation": "INTEGER NOT NULL DEFAULT 0",
            "form_action_policy_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for column, declaration in candidate_column_defaults.items():
            if column not in candidate_columns:
                self.connection.execute(
                    f"ALTER TABLE workflow_candidates ADD COLUMN {column} {declaration}"
                )
        approval_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(workflow_approvals)").fetchall()
        }
        approval_column_defaults = {
            "campaign_id": "TEXT NOT NULL DEFAULT ''",
            "season": "TEXT NOT NULL DEFAULT ''",
            "action_policy_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for column, declaration in approval_column_defaults.items():
            if column not in approval_columns:
                self.connection.execute(
                    f"ALTER TABLE workflow_approvals ADD COLUMN {column} {declaration}"
                )
        registry_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(workflow_submission_registry)").fetchall()
        }
        for column in ("confirmation_kind", "confirmation_text"):
            if column not in registry_columns:
                self.connection.execute(
                    f"ALTER TABLE workflow_submission_registry ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        existing = self.connection.execute(
            "SELECT value FROM workflow_meta WHERE key = 'schema_version'"
        ).fetchone()
        if existing is not None and existing["value"] != WORKFLOW_SCHEMA_VERSION:
            raise WorkflowError("unsupported workflow database schema")
        self.connection.execute(
            "INSERT OR IGNORE INTO workflow_meta(key, value) VALUES('schema_version', ?)",
            (WORKFLOW_SCHEMA_VERSION,),
        )
        self.connection.commit()
        self.path.chmod(0o600)

    def register_run(self, run_dir: Path) -> str:
        run_dir = run_dir.resolve()
        manifest = _read_json(run_dir / "run_manifest.json")
        run_id = _required(manifest, "run_id")
        now = _now()
        values = (
            run_id,
            str(run_dir),
            str(manifest.get("query") or ""),
            _required(manifest, "fact_digest"),
            _required(manifest, "context_digest"),
            _required(manifest, "policy_digest"),
            "awaiting_discovery",
            now,
            now,
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO workflow_runs(
                    run_id, source_run_dir, query, fact_digest, context_digest,
                    policy_digest, status, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                values,
            )
        return run_id

    def sync_batch_result(self, *, run_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
        """Idempotently project one validated autonomy result into canonical state."""
        run_id = self.register_run(run_dir)
        if str(result.get("run_id") or "") != run_id:
            raise WorkflowError("batch result run binding mismatch")

        rankings_by_candidate = {
            str(item.get("candidate_id") or ""): item
            for item in result.get("rankings") or []
            if str(item.get("candidate_id") or "")
        }

        with self.connection:
            for item in result.get("discoveries") or []:
                self._upsert_candidate(run_id, item, state="discovered")

            for item in result.get("eligibility") or []:
                candidate_id = str(item.get("candidate_id") or "")
                if not candidate_id:
                    continue
                self._ensure_candidate(run_id, candidate_id)
                decision = str(item.get("decision") or "")
                target_state = {
                    "accept": "eligible",
                    "review": "review_required",
                    "reject": "excluded",
                }.get(decision, "review_required")
                self._update_candidate_fields(
                    run_id,
                    candidate_id,
                    eligibility_decision=decision,
                    eligibility_reasons_json=_json(item.get("reason_codes") or []),
                )
                self._transition(run_id, candidate_id, target_state, "eligibility", item)

            for item in result.get("freshness") or []:
                candidate_id = str(item.get("candidate_id") or "")
                if not candidate_id:
                    continue
                self._ensure_candidate(run_id, candidate_id)
                if item.get("official_url"):
                    self._upsert_candidate(
                        run_id,
                        item,
                        state=str(self._candidate(run_id, candidate_id)["state"]),
                    )
                decision = str(item.get("decision") or "")
                eligibility = str(
                    self._candidate(run_id, candidate_id)["eligibility_decision"] or ""
                )
                if decision == "reject" or eligibility == "reject":
                    target_state = "excluded"
                elif decision == "accept" and eligibility == "accept":
                    target_state = "verified"
                else:
                    target_state = "review_required"
                self._update_candidate_fields(
                    run_id,
                    candidate_id,
                    freshness_decision=decision,
                    freshness_reasons_json=_json(item.get("reason_codes") or []),
                    **(
                        {
                            "verified_description_digest": _sha256_text(
                                str(item.get("description") or "")
                            )
                        }
                        if decision == "accept" and str(item.get("description") or "").strip()
                        else {}
                    ),
                )
                self._transition(run_id, candidate_id, target_state, "freshness", item)

            for item in result.get("rankings") or []:
                candidate_id = str(item.get("candidate_id") or "")
                if not candidate_id:
                    continue
                self._ensure_candidate(run_id, candidate_id)
                candidate = self._candidate(run_id, candidate_id)
                if (
                    candidate["eligibility_decision"] != "accept"
                    or candidate["freshness_decision"] != "accept"
                ):
                    raise WorkflowError(
                        f"ranking candidate {candidate_id} lacks accepted eligibility and freshness"
                    )
                self._upsert_candidate(run_id, item, state="verified")
                self._update_candidate_fields(
                    run_id,
                    candidate_id,
                    fit_score=int(item.get("fit_score") or 0),
                    inclusion_reasons_json=_json(item.get("inclusion_reasons") or []),
                    exclusion_reasons_json=_json(item.get("exclusion_reasons") or []),
                    quality_gaps_json=_json(item.get("quality_gaps") or []),
                    score_components_json=_json(item.get("score_components") or {}),
                )
                if not bool(item.get("qualifies")):
                    self._transition(run_id, candidate_id, "excluded", "ranking", item)

            for item in result.get("materials") or []:
                candidate_id = str(item.get("candidate_id") or "")
                self._ensure_candidate(run_id, candidate_id)
                candidate = self._candidate(run_id, candidate_id)
                ranking = rankings_by_candidate.get(candidate_id) or {}
                if (
                    candidate["eligibility_decision"] != "accept"
                    or candidate["freshness_decision"] != "accept"
                    or ranking.get("qualifies") is not True
                    or candidate["state"]
                    not in {"verified", "materials_ready", "dry_run_ready", "authorized"}
                ):
                    raise WorkflowError(
                        f"material candidate {candidate_id} lacks a qualifying verified ranking"
                    )
                paths = item.get("artifact_paths") or {}
                material_digest = _artifact_bundle_digest(paths)
                if not material_digest:
                    raise WorkflowError(f"material candidate {candidate_id} has no durable artifacts")
                if (
                    candidate["state"] in {"dry_run_ready", "authorized"}
                    and candidate["material_digest"] != material_digest
                ):
                    raise WorkflowError(
                        f"material candidate {candidate_id} changed after form review"
                    )
                self._update_candidate_fields(
                    run_id,
                    candidate_id,
                    material_paths_json=_json(paths),
                    material_digest=material_digest,
                    fit_score=int(item.get("fit_score") or 0),
                    inclusion_reasons_json=_json(item.get("inclusion_reasons") or []),
                    exclusion_reasons_json=_json(item.get("exclusion_reasons") or []),
                    quality_gaps_json=_json(
                        item.get("quality_gaps") or ranking.get("quality_gaps") or []
                    ),
                    score_components_json=_json(
                        item.get("score_components") or ranking.get("score_components") or {}
                    ),
                )
                self._transition(run_id, candidate_id, "materials_ready", "materials", item)

            for item in result.get("form_reviews") or []:
                candidate_id = str(item.get("candidate_id") or "")
                self._ensure_candidate(run_id, candidate_id)
                status = str(item.get("status") or "")
                target = (
                    "dry_run_ready"
                    if status == "dry_run_verified"
                    else "materials_ready"
                    if status == "form_surface_reviewed"
                    else "blocked"
                )
                self._record_form_review(run_id, candidate_id, item, target_state=target)

            status = str(result.get("status") or "unknown")
            self.connection.execute(
                "UPDATE workflow_runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (status, _now(), run_id),
            )
        return self.status(run_id)

    def status(self, run_id: str) -> dict[str, Any]:
        run = self.connection.execute(
            "SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise WorkflowError(f"unknown workflow run: {run_id}")
        rows = self.connection.execute(
            """
            SELECT state, COUNT(*) AS count
            FROM workflow_candidates WHERE run_id = ? GROUP BY state ORDER BY state
            """,
            (run_id,),
        ).fetchall()
        return {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "run_id": run_id,
            "source_run_dir": run["source_run_dir"],
            "status": run["status"],
            "candidate_counts": {row["state"]: row["count"] for row in rows},
            "record_counts": self._record_counts(run_id),
            "shortlist": self.shortlist(run_id, limit=10),
        }

    def source_run_dir(self, run_id: str) -> Path:
        """Return the registered immutable autonomy run directory."""
        return self._run_dir(run_id)

    def run_query(self, run_id: str) -> str:
        row = self.connection.execute(
            "SELECT query FROM workflow_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise WorkflowError(f"unknown workflow run: {run_id}")
        return str(row["query"])

    def persist_fact_snapshot(self, run_id: str, payload: dict[str, Any]) -> Path:
        """Persist one digest-addressed, private applicant fact snapshot."""
        try:
            from applypilot.autonomy.facts import (
                fact_ledger_from_dict,
                validate_monotonic_fact_extension,
            )

            ledger = fact_ledger_from_dict(payload)
            base = fact_ledger_from_dict(_read_json(self._run_dir(run_id) / "fact_ledger.json"))
            validate_monotonic_fact_extension(base, ledger)
        except (ValueError, TypeError) as exc:
            raise WorkflowError("cannot persist an invalid workflow fact snapshot") from exc
        fact_dir = self._run_dir(run_id) / "workflow-facts"
        fact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fact_dir.chmod(0o700)
        path = fact_dir / f"facts.{ledger.digest}.json"
        _write_private_json(path, payload)
        return self._validated_fact_snapshot(run_id, ledger.digest)

    def reconcile_candidate_eligibility(
        self,
        *,
        run_id: str,
        profile: dict[str, Any],
        fact_digest: str,
    ) -> dict[str, Any]:
        """Re-evaluate stored verified postings after unknown facts are confirmed."""
        snapshot_path = self._validated_fact_snapshot(run_id, fact_digest)
        from applypilot.autonomy.context import candidate_profile_from_data
        from applypilot.autonomy.facts import fact_ledger_from_dict
        from applypilot.autonomy.models import Decision, RoleCandidate
        from applypilot.autonomy.policy import eligibility_gate

        ledger = fact_ledger_from_dict(_read_json(snapshot_path))
        profile_sha256 = hashlib.sha256(
            json.dumps(profile, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        if profile_sha256 != ledger.profile_sha256:
            raise WorkflowError("eligibility profile does not match the bound fact snapshot")
        eligibility_profile = candidate_profile_from_data(
            profile,
            query=self.run_query(run_id),
        )

        rows = self.connection.execute(
            """
            SELECT * FROM workflow_candidates
            WHERE run_id = ? AND fit_score IS NOT NULL
            ORDER BY fit_score DESC, candidate_id
            """,
            (run_id,),
        ).fetchall()
        with self.connection:
            for row in rows:
                if row["outcome"] or row["state"] in {
                    "authorized",
                    "submitting",
                    "submitted_unconfirmed",
                    "submitted_confirmed",
                }:
                    continue
                candidate_id = str(row["candidate_id"])
                hydrated = self._hydrate_verified_candidate(run_id, row)
                if hydrated is None:
                    self._update_candidate_fields(
                        run_id,
                        candidate_id,
                        eligibility_decision="review",
                        eligibility_reasons_json=_json(["verified_description_missing"]),
                    )
                    self._transition(
                        run_id,
                        candidate_id,
                        "review_required",
                        "fact_reconciliation",
                        {
                            "fact_digest": fact_digest,
                            "decision": "review",
                            "reason_codes": ["verified_description_missing"],
                        },
                    )
                    continue
                row = hydrated
                candidate = RoleCandidate(
                    company=row["company"],
                    title=row["title"],
                    official_url=row["canonical_url"],
                    source=row["source"],
                    location=row["location"],
                    description=row["description"],
                    compensation=row["compensation"],
                    opportunity_kind=row["opportunity_kind"],
                    application_surface=row["application_surface"],
                    requisition_id=row["requisition_id"],
                )
                decision = eligibility_gate(candidate, eligibility_profile)
                self._update_candidate_fields(
                    run_id,
                    row["candidate_id"],
                    eligibility_decision=decision.decision.value,
                    eligibility_reasons_json=_json(decision.reason_codes),
                )
                if decision.decision is Decision.ACCEPT:
                    target = "materials_ready" if row["material_digest"] else "verified"
                elif decision.decision is Decision.REVIEW:
                    target = "review_required"
                else:
                    target = "excluded"
                self._transition(
                    run_id,
                    row["candidate_id"],
                    target,
                    "fact_reconciliation",
                    {
                        "fact_digest": fact_digest,
                        "decision": decision.decision.value,
                        "reason_codes": list(decision.reason_codes),
                        "evidence": list(decision.evidence),
                    },
                )
        return self.status(run_id)

    def shortlist(self, run_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT candidate_id, company, title, canonical_url, location, state,
                   fit_score, inclusion_reasons_json, exclusion_reasons_json,
                   quality_gaps_json, score_components_json, compensation,
                   record_type, opportunity_kind, application_surface, requisition_id,
                   material_paths_json, form_review_json, outcome
            FROM workflow_candidates
            WHERE run_id = ? AND fit_score IS NOT NULL
              AND state IN ('verified', 'materials_ready', 'dry_run_ready', 'authorized')
              AND opportunity_kind = 'posted_employment'
            ORDER BY fit_score DESC, candidate_id
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
        return [
            {
                "candidate_id": row["candidate_id"],
                "company": row["company"],
                "title": row["title"],
                "official_url": row["canonical_url"],
                "location": row["location"],
                "state": row["state"],
                "fit_score": row["fit_score"],
                "inclusion_reasons": json.loads(row["inclusion_reasons_json"]),
                "exclusion_reasons": json.loads(row["exclusion_reasons_json"]),
                "quality_gaps": json.loads(row["quality_gaps_json"]),
                "score_components": json.loads(row["score_components_json"]),
                "compensation": row["compensation"],
                "record_type": row["record_type"],
                "opportunity_kind": row["opportunity_kind"],
                "application_surface": row["application_surface"],
                "requisition_id": row["requisition_id"],
                "material_paths": json.loads(row["material_paths_json"]),
                "form_review": json.loads(row["form_review_json"]),
                "outcome": row["outcome"],
            }
            for row in rows
        ]

    def create_campaign(
        self,
        *,
        campaign_id: str,
        summer_target: int = 80,
        fall_target: int = 20,
    ) -> dict[str, Any]:
        """Create the durable cross-run season ledger used by exact campaigns."""
        _safe_file_identifier(campaign_id, "campaign id")
        if summer_target < 0 or fall_target < 0 or summer_target + fall_target <= 0:
            raise WorkflowError("campaign season targets must be non-negative and non-empty")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO workflow_campaigns(campaign_id, summer_target, fall_target, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (campaign_id, summer_target, fall_target, _now()),
            )
        return self.campaign_status(campaign_id)

    def campaign_status(self, campaign_id: str) -> dict[str, Any]:
        """Return durable, evidence-bound entries and exact season counters."""
        _safe_file_identifier(campaign_id, "campaign id")
        campaign = self.connection.execute(
            "SELECT campaign_id, summer_target, fall_target, created_at FROM workflow_campaigns WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
        if campaign is None:
            raise WorkflowError("campaign does not exist")
        rows = self.connection.execute(
            """
            SELECT e.campaign_id, e.run_id, e.candidate_id, e.season, e.approval_id,
                   c.company, c.title, c.canonical_url,
                   r.outcome, r.attempted_at, r.evidence_path,
                   r.confirmation_kind, r.confirmation_text
            FROM workflow_campaign_entries e
            JOIN workflow_candidates c ON c.run_id = e.run_id AND c.candidate_id = e.candidate_id
            LEFT JOIN workflow_submission_registry r
              ON r.canonical_url = c.canonical_url AND r.approval_id = e.approval_id
            WHERE e.campaign_id = ?
            ORDER BY e.season, e.created_at, e.run_id, e.candidate_id
            """,
            (campaign_id,),
        ).fetchall()
        entries = [
            {
                "campaign_id": row["campaign_id"],
                "company": row["company"],
                "title": row["title"],
                "season": row["season"],
                "official_application_url": row["canonical_url"],
                "submitted_at": row["attempted_at"] or "",
                "confirmation_evidence_type": row["confirmation_kind"] or "",
                "confirmation_evidence_value": row["confirmation_text"] or "",
                "confirmation_evidence_path": row["evidence_path"] or "",
                "workflow_run_id": row["run_id"],
                "workflow_candidate_id": row["candidate_id"],
                "approval_id": row["approval_id"],
                "dedupe_key": row["canonical_url"],
                "outcome": row["outcome"] or "not_attempted",
            }
            for row in rows
        ]
        counters = {
            season: sum(
                entry["outcome"] == "submitted_confirmed" and entry["season"] == season
                for entry in entries
            )
            for season in CAMPAIGN_SEASONS
        }
        targets = {
            "summer_2027": int(campaign["summer_target"]),
            "fall_2026": int(campaign["fall_target"]),
        }
        replacement_rows = self.connection.execute(
            """
            SELECT r.campaign_id, r.run_id, r.candidate_id, r.season, r.category, r.reason,
                   r.created_at, c.company, c.title, c.canonical_url
            FROM workflow_campaign_replacements r
            JOIN workflow_candidates c ON c.run_id = r.run_id AND c.candidate_id = r.candidate_id
            WHERE r.campaign_id = ?
            ORDER BY r.season, r.created_at, r.run_id, r.candidate_id
            """,
            (campaign_id,),
        ).fetchall()
        replacements = [
            {
                "campaign_id": row["campaign_id"],
                "company": row["company"],
                "title": row["title"],
                "season": row["season"],
                "official_application_url": row["canonical_url"],
                "workflow_run_id": row["run_id"],
                "workflow_candidate_id": row["candidate_id"],
                "category": row["category"],
                "reason": row["reason"],
                "recorded_at": row["created_at"],
            }
            for row in replacement_rows
        ]
        return {
            "campaign_id": campaign["campaign_id"],
            "created_at": campaign["created_at"],
            "targets": targets,
            "confirmed": counters,
            "total_confirmed": sum(counters.values()),
            "failed_or_blocked": sum(
                replacement["category"] in {"failed", "blocked"}
                for replacement in replacements
            ),
            "duplicate": sum(replacement["category"] == "duplicate" for replacement in replacements),
            "replacement_needed": len(replacements)
            + sum(entry["outcome"] in {"blocked", "not_submitted", "submitted_unconfirmed"} for entry in entries),
            "entries": entries,
            "replacements": replacements,
        }

    def record_campaign_replacement(
        self,
        *,
        campaign_id: str,
        run_id: str,
        candidate_id: str,
        season: str,
        category: str,
        reason: str,
    ) -> dict[str, Any]:
        """Persist a verified non-submission so a campaign can replace it without counting it."""
        _safe_file_identifier(campaign_id, "campaign id")
        _safe_file_identifier(run_id, "run id")
        _safe_file_identifier(candidate_id, "candidate id")
        if season not in CAMPAIGN_SEASONS:
            raise WorkflowError("campaign season is invalid")
        if category not in {"blocked", "duplicate", "failed", "unqualified"}:
            raise WorkflowError("campaign replacement category is invalid")
        clean_reason = str(reason).strip()
        if not clean_reason or len(clean_reason) > 1_000:
            raise WorkflowError("campaign replacement reason must be 1-1000 characters")
        if self.connection.execute(
            "SELECT 1 FROM workflow_campaigns WHERE campaign_id = ?", (campaign_id,)
        ).fetchone() is None:
            raise WorkflowError("campaign does not exist")
        if self.connection.execute(
            "SELECT 1 FROM workflow_candidates WHERE run_id = ? AND candidate_id = ?",
            (run_id, candidate_id),
        ).fetchone() is None:
            raise WorkflowError("campaign replacement candidate does not exist")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO workflow_campaign_replacements(
                    campaign_id, run_id, candidate_id, season, category, reason, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(campaign_id, run_id, candidate_id) DO NOTHING
                """,
                (campaign_id, run_id, candidate_id, season, category, clean_reason, _now()),
            )
        return self.campaign_status(campaign_id)

    def create_dry_run_requests(
        self,
        *,
        run_id: str,
        candidate_ids: Iterable[str],
        form_fact_digest: str,
        action_policy: BrowserInterventionPolicy | None = None,
    ) -> list[Path]:
        if len(form_fact_digest) != 64:
            raise WorkflowError("confirmed form fact digest is required")
        policy = action_policy or BrowserInterventionPolicy.create()
        policy_payload = policy.to_dict()
        action_contract = _browser_action_contract(policy=policy, mode="dry_run")
        run_dir = self._run_dir(run_id)
        fact_snapshot_path = self._validated_fact_snapshot(run_id, form_fact_digest)
        request_dir = run_dir / "workflow-handoff"
        request_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        paths: list[Path] = []
        for candidate_id in tuple(dict.fromkeys(candidate_ids))[:5]:
            _safe_file_identifier(candidate_id, "candidate id")
            row = self._candidate(run_id, candidate_id)
            if row["state"] not in {"materials_ready", "dry_run_ready"}:
                raise WorkflowError(f"candidate {candidate_id} does not have reviewed materials")
            material_paths = self._validated_material_paths(row)
            generation = int(row["form_review_generation"] or 0)
            request_path = request_dir / f"dry-run.{candidate_id}.request.json"
            response_path = request_path.with_name(request_path.name.replace(".request.json", ".response.json"))
            request = {
                "schema_version": BROWSER_ACTION_SCHEMA_VERSION,
                "request_id": _request_id(
                    run_id,
                    candidate_id,
                    "dry_run",
                    _dry_run_binding(str(row["material_digest"]), generation),
                ),
                "run_id": run_id,
                "candidate_id": candidate_id,
                "mode": "dry_run",
                "official_url": row["canonical_url"],
                "company": row["company"],
                "title": row["title"],
                "material_paths": material_paths,
                "material_digest": row["material_digest"],
                "form_fact_digest": form_fact_digest,
                "form_review_generation": generation,
                "fact_snapshot_path": str(fact_snapshot_path),
                "fact_use_policy": [
                    "use_only_records_whose_state_is_confirmed",
                    "abstain_on_unknown_rejected_or_missing_answers",
                    "never_infer_screening_identity_tax_payment_or_ssn_answers",
                ],
                "action_policy": policy_payload,
                **action_contract,
                "response_path": str(response_path),
            }
            with self.connection:
                self._update_candidate_fields(
                    run_id,
                    candidate_id,
                    form_action_policy_json=_json(policy_payload),
                )
            _write_private_json(request_path, request)
            paths.append(request_path)
        return paths

    def import_browser_response(self, *, request_path: Path, input_path: Path) -> dict[str, Any]:
        request = _read_json(request_path)
        response = _read_json(input_path)
        mode = str(request.get("mode") or "")
        run_id = str(request.get("run_id") or "")
        candidate_id = str(request.get("candidate_id") or "")
        row = self._candidate(run_id, candidate_id)
        self._validate_browser_request(
            request_path=request_path,
            request=request,
            row=row,
        )
        for key in ("schema_version", "request_id", "run_id", "candidate_id", "mode"):
            if response.get(key) != request.get(key):
                raise WorkflowError(f"browser response {key} binding mismatch")
        if response.get("material_digest") != row["material_digest"]:
            raise WorkflowError("browser response material binding mismatch")
        if response.get("form_fact_digest") != request.get("form_fact_digest"):
            raise WorkflowError("browser response form-fact binding mismatch")
        if request.get("schema_version") == BROWSER_ACTION_SCHEMA_VERSION:
            try:
                BrowserInterventionPolicy.from_dict(
                    request.get("action_policy")
                ).validate_response(response, mode=mode)
            except ValueError as exc:
                raise WorkflowError(str(exc)) from exc

        status = str(response.get("status") or "")
        final_performed = response.get("final_submission_performed")
        raw_evidence = response.get("evidence_artifacts") or []
        if not isinstance(raw_evidence, list):
            raise WorkflowError("browser evidence artifacts must be a list")
        persisted_evidence = self._persist_evidence(
            run_id=run_id,
            candidate_id=candidate_id,
            mode=mode,
            raw_paths=raw_evidence,
        )
        evidence_paths = [path for path, _digest in persisted_evidence]
        response = dict(response)
        response["fact_snapshot_path"] = request["fact_snapshot_path"]
        response["evidence_artifacts"] = [str(path) for path in evidence_paths]
        response["evidence_sha256"] = {
            str(path): digest for path, digest in persisted_evidence
        }

        with self.connection:
            if mode == "dry_run":
                if final_performed is not False:
                    raise WorkflowError("dry-run response must prove no final submission")
                if status not in {"dry_run_verified", "blocked", "not_started"}:
                    raise WorkflowError("invalid dry-run result status")
                if status == "dry_run_verified":
                    if response.get("review_page_reached") is not True or not evidence_paths:
                        raise WorkflowError("dry-run verification requires review state and evidence")
                    target = "dry_run_ready"
                else:
                    target = "blocked" if status == "blocked" else row["state"]
                self._record_form_review(run_id, candidate_id, response, target_state=target)
            elif mode == "submit":
                self._import_submission_response(
                    request=request,
                    response=response,
                    evidence_paths=evidence_paths,
                )
            else:
                raise WorkflowError("unknown browser action mode")

            destination = Path(str(request.get("response_path") or "")).resolve()
            if destination.parent != request_path.resolve().parent:
                raise WorkflowError("browser response path escaped handoff directory")
            _write_private_json(destination, response)
        return self.candidate_status(run_id, candidate_id)

    def create_approval(
        self,
        *,
        run_id: str,
        candidate_ids: Iterable[str],
        form_fact_digest: str,
        max_submissions: int = 3,
        valid_hours: int = 24,
        campaign_id: str = "",
        season: str = "",
        action_policy: BrowserInterventionPolicy | None = None,
    ) -> dict[str, Any]:
        ids = tuple(dict.fromkeys(str(value) for value in candidate_ids if str(value)))
        if not 1 <= len(ids) <= 5:
            raise WorkflowError("approval must bind one to five exact candidates")
        if not 1 <= max_submissions <= min(3, len(ids)):
            raise WorkflowError("max submissions must be one to three and no larger than the batch")
        if not 1 <= valid_hours <= 72:
            raise WorkflowError("approval validity must be between one and 72 hours")
        if len(form_fact_digest) != 64:
            raise WorkflowError("confirmed form fact digest is required")
        policy = action_policy or BrowserInterventionPolicy.create()
        policy_payload = policy.to_dict()
        if bool(campaign_id) != bool(season):
            raise WorkflowError("campaign approval requires both campaign id and season")
        if campaign_id:
            _safe_file_identifier(campaign_id, "campaign id")
            if season not in CAMPAIGN_SEASONS:
                raise WorkflowError("campaign season is invalid")
            campaign = self.campaign_status(campaign_id)
            if campaign["confirmed"][season] + max_submissions > campaign["targets"][season]:
                raise WorkflowError("approval could exceed the campaign season target")
        fact_snapshot_path = self._validated_fact_snapshot(run_id, form_fact_digest)
        bindings: dict[str, Any] = {}
        for candidate_id in ids:
            _safe_file_identifier(candidate_id, "candidate id")
            row = self._candidate(run_id, candidate_id)
            duplicate = self.connection.execute(
                "SELECT outcome FROM workflow_submission_registry WHERE canonical_url = ?",
                (row["canonical_url"],),
            ).fetchone()
            if duplicate is not None:
                raise WorkflowError(
                    f"candidate {candidate_id} already has submission state {duplicate['outcome']}"
                )
            legacy_outcome = self._legacy_outcome_for_url(row["canonical_url"])
            if legacy_outcome:
                raise WorkflowError(
                    f"candidate {candidate_id} already has legacy submission state {legacy_outcome}"
                )
            if row["state"] != "dry_run_ready":
                raise WorkflowError(f"candidate {candidate_id} lacks a verified dry-run")
            if not row["material_digest"] or not row["form_review_digest"]:
                raise WorkflowError(f"candidate {candidate_id} approval bindings are incomplete")
            self._validated_material_paths(row)
            form_review = json.loads(row["form_review_json"])
            if form_review.get("form_fact_digest") != form_fact_digest:
                raise WorkflowError(f"candidate {candidate_id} form facts changed after dry-run")
            if form_review.get("fact_snapshot_path") != str(fact_snapshot_path):
                raise WorkflowError(f"candidate {candidate_id} fact snapshot changed after dry-run")
            self._validate_persisted_evidence(form_review)
            bindings[candidate_id] = {
                "canonical_url": row["canonical_url"],
                "material_digest": row["material_digest"],
                "form_review_digest": row["form_review_digest"],
                "form_fact_digest": form_fact_digest,
                "fact_snapshot_path": str(fact_snapshot_path),
            }
        issued = datetime.now(timezone.utc)
        approval_id = "approval-" + secrets.token_hex(12)
        approval = {
            "approval_id": approval_id,
            "run_id": run_id,
            "candidate_ids": list(ids),
            "bindings": bindings,
            "issued_at": issued.isoformat(),
            "expires_at": (issued + timedelta(hours=valid_hours)).isoformat(),
            "max_submissions": max_submissions,
            "consumed_count": 0,
            "status": "active",
            "campaign_id": campaign_id,
            "season": season,
            "action_policy": policy_payload,
        }
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO workflow_approvals(
                    approval_id, run_id, candidate_ids_json, bindings_json,
                    issued_at, expires_at, max_submissions, consumed_count, status,
                    campaign_id, season, action_policy_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 0, 'active', ?, ?, ?)
                """,
                (
                    approval_id,
                    run_id,
                    _json(ids),
                    _json(bindings),
                    approval["issued_at"],
                    approval["expires_at"],
                    max_submissions,
                    campaign_id,
                    season,
                    _json(policy_payload),
                ),
            )
            if campaign_id:
                for candidate_id in ids:
                    self.connection.execute(
                        """
                        INSERT INTO workflow_campaign_entries(
                            campaign_id, run_id, candidate_id, season, approval_id, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (campaign_id, run_id, candidate_id, season, approval_id, _now()),
                    )
            self._event(run_id, "", "batch_authorized", "", "", approval)
        return approval

    def create_submission_request(
        self,
        *,
        approval_id: str,
        form_fact_digest: str,
    ) -> Path | None:
        _safe_file_identifier(approval_id, "approval id")
        if len(form_fact_digest) != 64:
            raise WorkflowError("current confirmed form fact digest is required")
        approval = self._approval(approval_id)
        if approval["status"] == "consumed":
            return None
        if approval["status"] != "active":
            raise WorkflowError("approval is not active")
        if datetime.now(timezone.utc) > datetime.fromisoformat(approval["expires_at"]):
            with self.connection:
                self.connection.execute(
                    "UPDATE workflow_approvals SET status = 'expired' WHERE approval_id = ?",
                    (approval_id,),
                )
            raise WorkflowError("approval expired")
        if approval["consumed_count"] >= approval["max_submissions"]:
            return None

        run_id = approval["run_id"]
        try:
            policy = BrowserInterventionPolicy.from_dict(
                json.loads(str(approval["action_policy_json"] or "{}"))
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise WorkflowError("approval browser intervention policy is invalid") from exc
        action_contract = _browser_action_contract(policy=policy, mode="submit")
        fact_snapshot_path = self._validated_fact_snapshot(run_id, form_fact_digest)
        candidate_ids = json.loads(approval["candidate_ids_json"])
        bindings = json.loads(approval["bindings_json"])
        run_dir = self._run_dir(run_id)
        request_dir = run_dir / "workflow-handoff"
        request_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for candidate_id in candidate_ids:
            _safe_file_identifier(candidate_id, "candidate id")
            row = self._candidate(run_id, candidate_id)
            if row["state"] in TERMINAL_STATES:
                continue
            existing = self.connection.execute(
                "SELECT outcome, approval_id FROM workflow_submission_registry WHERE canonical_url = ?",
                (row["canonical_url"],),
            ).fetchone()
            resuming_reservation = bool(
                existing is not None
                and existing["outcome"] == "reserved"
                and existing["approval_id"] == approval_id
            )
            if existing is not None and not resuming_reservation:
                continue
            legacy_outcome = self._legacy_outcome_for_url(row["canonical_url"])
            if legacy_outcome:
                raise WorkflowError(
                    f"candidate {candidate_id} already has legacy submission state {legacy_outcome}"
                )
            binding = bindings.get(candidate_id) or {}
            if (
                binding.get("canonical_url") != row["canonical_url"]
                or binding.get("material_digest") != row["material_digest"]
                or binding.get("form_review_digest") != row["form_review_digest"]
                or binding.get("form_fact_digest") != form_fact_digest
                or binding.get("fact_snapshot_path") != str(fact_snapshot_path)
            ):
                raise WorkflowError("approved candidate changed after review")
            material_paths = self._validated_material_paths(row)
            request_path = request_dir / f"submit.{candidate_id}.{approval_id}.request.json"
            response_path = request_path.with_name(request_path.name.replace(".request.json", ".response.json"))
            request = {
                "schema_version": BROWSER_ACTION_SCHEMA_VERSION,
                "request_id": _request_id(run_id, candidate_id, "submit", approval_id),
                "run_id": run_id,
                "candidate_id": candidate_id,
                "mode": "submit",
                "approval_id": approval_id,
                "campaign_id": approval["campaign_id"],
                "season": approval["season"],
                "official_url": row["canonical_url"],
                "company": row["company"],
                "title": row["title"],
                "material_paths": material_paths,
                "material_digest": row["material_digest"],
                "form_review_digest": row["form_review_digest"],
                "form_fact_digest": binding["form_fact_digest"],
                "fact_snapshot_path": str(fact_snapshot_path),
                "fact_use_policy": [
                    "use_only_records_whose_state_is_confirmed",
                    "abstain_on_unknown_rejected_or_missing_answers",
                    "never_infer_screening_identity_tax_payment_or_ssn_answers",
                ],
                "action_policy": policy.to_dict(),
                **action_contract,
                "response_path": str(response_path),
            }
            if not resuming_reservation:
                with self.connection:
                    self.connection.execute(
                        """
                        INSERT INTO workflow_submission_registry(
                            canonical_url, run_id, candidate_id, outcome, approval_id,
                            attempted_at, evidence_path
                        ) VALUES(?, ?, ?, 'reserved', ?, ?, '')
                        """,
                        (row["canonical_url"], run_id, candidate_id, approval_id, _now()),
                    )
                    self._transition(run_id, candidate_id, "authorized", "submit_reserved", request)
            _write_private_json(request_path, request)
            return request_path
        return None

    def candidate_status(self, run_id: str, candidate_id: str) -> dict[str, Any]:
        row = self._candidate(run_id, candidate_id)
        return {
            "run_id": run_id,
            "candidate_id": candidate_id,
            "official_url": row["canonical_url"],
            "state": row["state"],
            "outcome": row["outcome"],
            "evidence_path": row["evidence_path"],
            "last_error": row["last_error"],
        }

    def invalidate_form_review(
        self,
        *,
        run_id: str,
        candidate_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Return an unsubmitted candidate to materials after approval expiry.

        This is the one intentional backwards transition in the canonical
        workflow.  It is limited to candidates without any submission
        reservation or outcome, clears every form-review binding, and records
        why a fresh visible-browser dry-run is required.
        """
        _safe_file_identifier(candidate_id, "candidate id")
        row = self._candidate(run_id, candidate_id)
        if row["state"] in {"submitting", "submitted_unconfirmed", "submitted_confirmed"}:
            raise WorkflowError("submission state cannot be invalidated")
        existing = self.connection.execute(
            "SELECT outcome FROM workflow_submission_registry WHERE canonical_url = ?",
            (row["canonical_url"],),
        ).fetchone()
        if existing is not None:
            raise WorkflowError("reserved or attempted candidate cannot be invalidated")
        if not row["material_digest"]:
            raise WorkflowError("candidate lacks reviewed materials")
        detail = {
            "reason": str(reason).strip()[:200] or "form_review_expired",
            "previous_form_review_digest": str(row["form_review_digest"] or ""),
        }
        response_path = (
            self._run_dir(run_id)
            / "workflow-handoff"
            / f"dry-run.{candidate_id}.response.json"
        )
        if response_path.is_file():
            archive_path = (
                self._run_dir(run_id)
                / "workflow-evidence"
                / (
                    f"expired-form-review.{candidate_id}."
                    f"{detail['previous_form_review_digest'][:16]}.response.json"
                )
            )
            archive_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if archive_path.exists():
                if archive_path.read_bytes() != response_path.read_bytes():
                    raise WorkflowError("expired form-review archive collision")
                response_path.unlink()
            else:
                response_path.replace(archive_path)
                archive_path.chmod(0o600)
        with self.connection:
            self.connection.execute(
                """
                UPDATE workflow_candidates
                SET state = 'materials_ready', form_review_json = '{}',
                    form_review_digest = '',
                    form_review_generation = form_review_generation + 1,
                    form_action_policy_json = '{}',
                    last_error = ?, updated_at = ?
                WHERE run_id = ? AND candidate_id = ?
                """,
                (detail["reason"], _now(), run_id, candidate_id),
            )
            self._event(
                run_id,
                candidate_id,
                "form_review_invalidated",
                str(row["state"]),
                "materials_ready",
                detail,
            )
        return self.candidate_status(run_id, candidate_id)

    def approval_status(self, approval_id: str) -> dict[str, Any]:
        row = self._approval(approval_id)
        try:
            action_policy = BrowserInterventionPolicy.from_dict(
                json.loads(str(row["action_policy_json"] or "{}"))
            ).to_dict()
        except (json.JSONDecodeError, ValueError) as exc:
            raise WorkflowError("approval browser intervention policy is invalid") from exc
        return {
            "approval_id": row["approval_id"],
            "run_id": row["run_id"],
            "status": row["status"],
            "consumed_count": row["consumed_count"],
            "max_submissions": row["max_submissions"],
            "campaign_id": row["campaign_id"],
            "season": row["season"],
            "action_policy": action_policy,
        }

    def _validate_browser_request(
        self,
        *,
        request_path: Path,
        request: dict[str, Any],
        row: sqlite3.Row,
    ) -> None:
        run_id = str(request.get("run_id") or "")
        candidate_id = str(request.get("candidate_id") or "")
        mode = str(request.get("mode") or "")
        _safe_file_identifier(candidate_id, "candidate id")
        schema_version = request.get("schema_version")
        if schema_version not in SUPPORTED_BROWSER_ACTION_SCHEMA_VERSIONS:
            raise WorkflowError("unsupported browser request schema")
        if run_id != row["run_id"] or candidate_id != row["candidate_id"]:
            raise WorkflowError("browser request candidate binding mismatch")
        material_paths = self._validated_material_paths(row)
        if request.get("official_url") != row["canonical_url"]:
            raise WorkflowError("browser request URL binding mismatch")
        if request.get("material_digest") != row["material_digest"]:
            raise WorkflowError("browser request material binding mismatch")
        if request.get("material_paths") != material_paths:
            raise WorkflowError("browser request material paths changed")
        fact_snapshot_path = self._validated_fact_snapshot(
            run_id,
            str(request.get("form_fact_digest") or ""),
        )
        if request.get("fact_snapshot_path") != str(fact_snapshot_path):
            raise WorkflowError("browser request fact snapshot binding mismatch")

        policy: BrowserInterventionPolicy | None = None
        if schema_version == BROWSER_ACTION_SCHEMA_VERSION:
            try:
                policy = BrowserInterventionPolicy.from_dict(
                    request.get("action_policy")
                )
            except ValueError as exc:
                raise WorkflowError("browser request action policy is invalid") from exc
            expected_contract = _browser_action_contract(policy=policy, mode=mode)
            for field in (
                "allowed_actions",
                "forbidden_actions",
                "intervention_rules",
                "response_requirements",
            ):
                if request.get(field) != expected_contract[field]:
                    raise WorkflowError(f"browser request {field} binding mismatch")

        request_dir = (self._run_dir(run_id) / "workflow-handoff").resolve()
        request_path = request_path.resolve()
        if request_path.parent != request_dir:
            raise WorkflowError("browser request escaped the workflow handoff directory")
        if mode == "dry_run":
            expected_request = request_dir / f"dry-run.{candidate_id}.request.json"
            generation = int(row["form_review_generation"] or 0)
            if int(request.get("form_review_generation") or 0) != generation:
                raise WorkflowError("browser request form-review generation mismatch")
            expected_request_id = _request_id(
                run_id,
                candidate_id,
                "dry_run",
                _dry_run_binding(str(row["material_digest"]), generation),
            )
            if schema_version == BROWSER_ACTION_SCHEMA_VERSION:
                try:
                    stored_policy = BrowserInterventionPolicy.from_dict(
                        json.loads(str(row["form_action_policy_json"] or "{}"))
                    )
                except (json.JSONDecodeError, ValueError) as exc:
                    raise WorkflowError("stored dry-run action policy is invalid") from exc
                if policy != stored_policy:
                    raise WorkflowError("browser request action policy binding mismatch")
        elif mode == "submit":
            approval_id = str(request.get("approval_id") or "")
            _safe_file_identifier(approval_id, "approval id")
            approval = self._approval(approval_id)
            if approval["run_id"] != run_id:
                raise WorkflowError("browser request approval run mismatch")
            bindings = json.loads(approval["bindings_json"])
            binding = bindings.get(candidate_id) or {}
            if (
                binding.get("canonical_url") != row["canonical_url"]
                or binding.get("material_digest") != row["material_digest"]
                or binding.get("form_review_digest") != row["form_review_digest"]
                or binding.get("form_fact_digest") != request.get("form_fact_digest")
                or binding.get("fact_snapshot_path") != request.get("fact_snapshot_path")
                or request.get("form_review_digest") != row["form_review_digest"]
                or request.get("campaign_id") != approval["campaign_id"]
                or request.get("season") != approval["season"]
            ):
                raise WorkflowError("browser request approval binding mismatch")
            if schema_version == BROWSER_ACTION_SCHEMA_VERSION:
                try:
                    approved_policy = BrowserInterventionPolicy.from_dict(
                        json.loads(str(approval["action_policy_json"] or "{}"))
                    )
                except (json.JSONDecodeError, ValueError) as exc:
                    raise WorkflowError("approval browser intervention policy is invalid") from exc
                if policy != approved_policy:
                    raise WorkflowError("browser request action policy binding mismatch")
            expected_request = (
                request_dir / f"submit.{candidate_id}.{approval_id}.request.json"
            )
            expected_request_id = _request_id(
                run_id,
                candidate_id,
                "submit",
                approval_id,
            )
        else:
            raise WorkflowError("unknown browser action mode")
        expected_response = expected_request.with_name(
            expected_request.name.replace(".request.json", ".response.json")
        )
        if request_path != expected_request.resolve():
            raise WorkflowError("browser request filename binding mismatch")
        if request.get("request_id") != expected_request_id:
            raise WorkflowError("browser request id binding mismatch")
        if Path(str(request.get("response_path") or "")).resolve() != expected_response.resolve():
            raise WorkflowError("browser response destination binding mismatch")

    def _persist_evidence(
        self,
        *,
        run_id: str,
        candidate_id: str,
        mode: str,
        raw_paths: list[Any],
    ) -> list[tuple[Path, str]]:
        if len(raw_paths) > MAX_EVIDENCE_ARTIFACTS:
            raise WorkflowError("too many browser evidence artifacts")
        evidence_dir = self._run_dir(run_id) / "workflow-evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        persisted: list[tuple[Path, str]] = []
        for index, raw_path in enumerate(raw_paths, start=1):
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise WorkflowError("invalid browser evidence artifact path")
            source = Path(raw_path).expanduser().resolve()
            if not source.is_file():
                raise WorkflowError("browser evidence artifact is missing")
            size = source.stat().st_size
            if size <= 0 or size > MAX_EVIDENCE_BYTES:
                raise WorkflowError("browser evidence artifact has an invalid size")
            payload = source.read_bytes()
            if not payload or len(payload) > MAX_EVIDENCE_BYTES:
                raise WorkflowError("browser evidence artifact has an invalid size")
            digest = hashlib.sha256(payload).hexdigest()
            suffix = source.suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
                suffix = ".bin"
            destination = evidence_dir / (
                f"{mode}.{candidate_id}.{index}.{digest[:16]}{suffix}"
            )
            if destination.exists():
                if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                    raise WorkflowError("durable evidence digest collision")
            else:
                _write_private_bytes(destination, payload)
            persisted.append((destination, digest))
        return persisted

    def _validated_material_paths(self, row: sqlite3.Row) -> dict[str, Any]:
        paths = json.loads(row["material_paths_json"])
        if not isinstance(paths, dict) or not paths:
            raise WorkflowError("candidate material paths are incomplete")
        if _artifact_bundle_digest(paths) != row["material_digest"]:
            raise WorkflowError("candidate materials changed after review")
        return paths

    def _validate_persisted_evidence(self, payload: dict[str, Any]) -> None:
        paths = payload.get("evidence_artifacts") or []
        digests = payload.get("evidence_sha256") or {}
        if not isinstance(paths, list) or not paths or not isinstance(digests, dict):
            raise WorkflowError("form review lacks durable evidence")
        for raw_path in paths:
            path = Path(str(raw_path)).resolve()
            expected = str(digests.get(str(path)) or "")
            if (
                not path.is_file()
                or len(expected) != 64
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise WorkflowError("form review evidence changed after review")

    def _import_submission_response(
        self,
        *,
        request: dict[str, Any],
        response: dict[str, Any],
        evidence_paths: list[Path],
    ) -> None:
        approval_id = str(request.get("approval_id") or "")
        approval = self._approval(approval_id)
        run_id = str(request["run_id"])
        candidate_id = str(request["candidate_id"])
        row = self._candidate(run_id, candidate_id)
        status = str(response.get("status") or "")
        performed = response.get("final_submission_performed")
        allowed = {"submitted_confirmed", "submitted_unconfirmed", "not_submitted", "blocked"}
        if status not in allowed:
            raise WorkflowError("invalid submission result status")
        if status in {"submitted_confirmed", "submitted_unconfirmed"} and performed is not True:
            raise WorkflowError("submission outcome requires a performed final action")
        if status in {"not_submitted", "blocked"} and performed is not False:
            raise WorkflowError("non-submission outcome must prove no final action")
        if status == "submitted_confirmed":
            if not evidence_paths:
                raise WorkflowError("confirmed submission requires durable evidence")
            if str(response.get("confirmation_kind") or "") not in {
                "ats_receipt",
                "confirmation_page",
                "confirmation_id",
            }:
                raise WorkflowError("confirmed submission lacks authoritative confirmation kind")
            if not str(response.get("confirmation_text") or "").strip():
                raise WorkflowError("confirmed submission lacks confirmation text")
            if approval["campaign_id"]:
                campaign = self.campaign_status(approval["campaign_id"])
                season = str(approval["season"])
                if campaign["confirmed"][season] >= campaign["targets"][season]:
                    raise WorkflowError("campaign season target already reached")
        if status == "submitted_unconfirmed" and not evidence_paths:
            raise WorkflowError("ambiguous submission requires evidence for reconciliation")

        existing = self.connection.execute(
            """
            SELECT outcome FROM workflow_submission_registry
            WHERE canonical_url = ? AND approval_id = ?
            """,
            (row["canonical_url"], approval_id),
        ).fetchone()
        if existing is None:
            raise WorkflowError("submission request reservation is missing")
        if existing["outcome"] != "reserved":
            if existing["outcome"] == status:
                return
            raise WorkflowError("submission outcome was already finalized differently")

        evidence_path = str(evidence_paths[0]) if evidence_paths else ""
        self.connection.execute(
            """
            UPDATE workflow_submission_registry
            SET outcome = ?, attempted_at = ?, evidence_path = ?, confirmation_kind = ?, confirmation_text = ?
            WHERE canonical_url = ? AND approval_id = ?
            """,
            (
                status,
                _now(),
                evidence_path,
                str(response.get("confirmation_kind") or ""),
                str(response.get("confirmation_text") or "")[:500],
                row["canonical_url"],
                approval_id,
            ),
        )
        self._update_candidate_fields(
            run_id,
            candidate_id,
            outcome=status,
            evidence_path=evidence_path,
            last_error=str(response.get("detail") or "")[:500],
        )
        self._transition(run_id, candidate_id, status, "submission_outcome", response)
        if performed is True:
            consumed = approval["consumed_count"] + 1
            approval_status = (
                "consumed" if consumed >= approval["max_submissions"] else "active"
            )
            self.connection.execute(
                """
                UPDATE workflow_approvals
                SET consumed_count = ?, status = ? WHERE approval_id = ?
                """,
                (consumed, approval_status, approval_id),
            )

    def _record_form_review(
        self,
        run_id: str,
        candidate_id: str,
        review: dict[str, Any],
        *,
        target_state: str,
    ) -> None:
        digest = hashlib.sha256(_json(review).encode("utf-8")).hexdigest()
        self._update_candidate_fields(
            run_id,
            candidate_id,
            form_review_json=_json(review),
            form_review_digest=digest,
            last_error=str(review.get("detail") or "")[:500],
        )
        self._transition(run_id, candidate_id, target_state, "form_review", review)

    def _upsert_candidate(self, run_id: str, item: dict[str, Any], *, state: str) -> None:
        candidate_id = str(item.get("candidate_id") or "")
        if not candidate_id:
            raise WorkflowError("candidate result is missing candidate_id")
        _safe_file_identifier(candidate_id, "candidate id")
        url = str(item.get("official_url") or "")
        now = _now()
        self.connection.execute(
            """
            INSERT INTO workflow_candidates(
                run_id, candidate_id, canonical_url, company, title, location,
                description, compensation, source, record_type, opportunity_kind,
                application_surface, requisition_id, state, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, candidate_id) DO UPDATE SET
                canonical_url = CASE WHEN excluded.canonical_url != '' THEN excluded.canonical_url ELSE canonical_url END,
                company = CASE WHEN excluded.company != '' THEN excluded.company ELSE company END,
                title = CASE WHEN excluded.title != '' THEN excluded.title ELSE title END,
                location = CASE WHEN excluded.location != '' THEN excluded.location ELSE location END,
                description = CASE WHEN excluded.description != '' THEN excluded.description ELSE description END,
                compensation = CASE WHEN excluded.compensation != '' THEN excluded.compensation ELSE compensation END,
                source = CASE WHEN excluded.source != '' THEN excluded.source ELSE source END,
                record_type = CASE WHEN excluded.record_type != '' THEN excluded.record_type ELSE record_type END,
                opportunity_kind = CASE WHEN excluded.opportunity_kind != '' THEN excluded.opportunity_kind ELSE opportunity_kind END,
                application_surface = CASE WHEN excluded.application_surface != '' THEN excluded.application_surface ELSE application_surface END,
                requisition_id = CASE WHEN excluded.requisition_id != '' THEN excluded.requisition_id ELSE requisition_id END,
                updated_at = excluded.updated_at
            """,
            (
                run_id,
                candidate_id,
                canonicalize_url(url) if url else "",
                str(item.get("company") or ""),
                str(item.get("title") or ""),
                str(item.get("location") or ""),
                str(item.get("description") or ""),
                str(item.get("compensation") or "")[:300],
                str(item.get("source") or ""),
                str(item.get("record_type") or ""),
                str(item.get("opportunity_kind") or ""),
                str(item.get("application_surface") or ""),
                str(item.get("requisition_id") or "")[:300],
                state,
                now,
                now,
            ),
        )

    def _record_counts(self, run_id: str) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT record_type, COUNT(*) AS count FROM workflow_candidates "
            "WHERE run_id = ? GROUP BY record_type ORDER BY record_type",
            (run_id,),
        ).fetchall()
        return {str(row["record_type"]): int(row["count"]) for row in rows}

    def _ensure_candidate(self, run_id: str, candidate_id: str) -> None:
        if self.connection.execute(
            "SELECT 1 FROM workflow_candidates WHERE run_id = ? AND candidate_id = ?",
            (run_id, candidate_id),
        ).fetchone() is None:
            self._upsert_candidate(
                run_id,
                {"candidate_id": candidate_id},
                state="discovered",
            )

    def _transition(
        self,
        run_id: str,
        candidate_id: str,
        target: str,
        event_type: str,
        detail: dict[str, Any],
    ) -> None:
        row = self._candidate(run_id, candidate_id)
        current = str(row["state"])
        reopening_excluded = (
            current == "excluded"
            and target in {"review_required", "eligible", "verified", "materials_ready"}
            and not row["outcome"]
            and self.connection.execute(
                "SELECT 1 FROM workflow_submission_registry WHERE canonical_url = ?",
                (row["canonical_url"],),
            ).fetchone()
            is None
        )
        if current in TERMINAL_STATES and current != target and not reopening_excluded:
            return
        if target not in STATE_ORDER:
            raise WorkflowError(f"unknown candidate state: {target}")
        if (
            STATE_ORDER[target] < STATE_ORDER.get(current, -1)
            and target not in TERMINAL_STATES
            and target != "review_required"
            and not reopening_excluded
        ):
            return
        if current == target:
            return
        self.connection.execute(
            "UPDATE workflow_candidates SET state = ?, updated_at = ? WHERE run_id = ? AND candidate_id = ?",
            (target, _now(), run_id, candidate_id),
        )
        self._event(run_id, candidate_id, event_type, current, target, detail)

    def _event(
        self,
        run_id: str,
        candidate_id: str,
        event_type: str,
        from_state: str,
        to_state: str,
        detail: dict[str, Any],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO workflow_events(
                run_id, candidate_id, event_type, from_state, to_state, detail_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, candidate_id, event_type, from_state, to_state, _json(detail), _now()),
        )

    def _update_candidate_fields(self, run_id: str, candidate_id: str, **fields: Any) -> None:
        allowed = {
            "eligibility_decision",
            "eligibility_reasons_json",
            "freshness_decision",
            "freshness_reasons_json",
            "fit_score",
            "inclusion_reasons_json",
            "exclusion_reasons_json",
            "quality_gaps_json",
            "score_components_json",
            "material_paths_json",
            "material_digest",
            "form_review_json",
            "form_review_digest",
            "form_action_policy_json",
            "outcome",
            "evidence_path",
            "last_error",
            "verified_description_digest",
        }
        if not fields or set(fields) - allowed:
            raise WorkflowError("invalid candidate field update")
        columns = ", ".join(f"{name} = ?" for name in fields)
        self.connection.execute(
            f"UPDATE workflow_candidates SET {columns}, updated_at = ? WHERE run_id = ? AND candidate_id = ?",
            (*fields.values(), _now(), run_id, candidate_id),
        )

    def _candidate(self, run_id: str, candidate_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM workflow_candidates WHERE run_id = ? AND candidate_id = ?",
            (run_id, candidate_id),
        ).fetchone()
        if row is None:
            raise WorkflowError(f"unknown workflow candidate: {candidate_id}")
        return row

    def _approval(self, approval_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM workflow_approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise WorkflowError("unknown approval")
        return row

    def _run_dir(self, run_id: str) -> Path:
        row = self.connection.execute(
            "SELECT source_run_dir FROM workflow_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise WorkflowError(f"unknown workflow run: {run_id}")
        path = Path(row["source_run_dir"]).resolve()
        if not path.is_dir():
            raise WorkflowError("workflow source run directory is missing")
        return path

    def _validated_fact_snapshot(self, run_id: str, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise WorkflowError("confirmed form fact digest must be lowercase sha256")
        fact_dir = (self._run_dir(run_id) / "workflow-facts").resolve()
        snapshot_path = (fact_dir / f"facts.{digest}.json").resolve()
        if snapshot_path.parent != fact_dir or not snapshot_path.is_file():
            raise WorkflowError("bound workflow fact snapshot is missing")
        try:
            from applypilot.autonomy.facts import fact_ledger_from_dict

            ledger = fact_ledger_from_dict(_read_json(snapshot_path))
        except (OSError, ValueError, TypeError) as exc:
            raise WorkflowError("bound workflow fact snapshot is invalid") from exc
        if ledger.digest != digest:
            raise WorkflowError("bound workflow fact snapshot digest mismatch")
        if snapshot_path.stat().st_mode & 0o077:
            raise WorkflowError("bound workflow fact snapshot permissions are too broad")
        return snapshot_path

    def _hydrate_verified_candidate(
        self,
        run_id: str,
        row: sqlite3.Row,
    ) -> sqlite3.Row | None:
        """Load only schema- and URL-bound first-party text from the run cache."""
        if (
            row["freshness_decision"] == "accept"
            and row["description"]
            and row["verified_description_digest"] == _sha256_text(row["description"])
        ):
            return row

        from applypilot.autonomy.first_party import CachedFirstPartyVerifier
        from applypilot.autonomy.models import RoleCandidate

        role = RoleCandidate(
            company=row["company"],
            title=row["title"],
            official_url=row["canonical_url"],
            source=row["source"],
            location=row["location"],
            description=row["description"],
            compensation=row["compensation"],
            opportunity_kind=row["opportunity_kind"],
            application_surface=row["application_surface"],
            requisition_id=row["requisition_id"],
        )
        cache_dir = self._run_dir(run_id) / "verification"
        cache_path = cache_dir / f"{role.candidate_id}.v4.json"
        if cache_path.is_file():
            try:
                evidence = CachedFirstPartyVerifier(None, cache_dir=cache_dir).verify(role)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return None
            if (
                evidence.first_party
                and evidence.resolved
                and evidence.open_state is True
                and evidence.description.strip()
                and canonicalize_url(evidence.official_url) == row["canonical_url"]
            ):
                self._upsert_candidate(
                    run_id,
                    {
                        "candidate_id": row["candidate_id"],
                        "official_url": evidence.official_url,
                        "company": row["company"],
                        "title": evidence.title or row["title"],
                        "location": row["location"],
                        "description": evidence.description,
                        "source": row["source"],
                        "record_type": "verified_job_evidence",
                        "opportunity_kind": evidence.opportunity_kind.value,
                        "application_surface": evidence.application_surface.value,
                        "requisition_id": evidence.requisition_id,
                    },
                    state=row["state"],
                )
                self._update_candidate_fields(
                    run_id,
                    str(row["candidate_id"]),
                    verified_description_digest=_sha256_text(evidence.description),
                )
                return self._candidate(run_id, str(row["candidate_id"]))
            if (
                not evidence.first_party
                or not evidence.resolved
                or evidence.open_state is not True
                or canonicalize_url(evidence.official_url) != row["canonical_url"]
            ):
                return None

        if self._visible_csod_form_review_matches(row):
            self._update_candidate_fields(
                run_id,
                str(row["candidate_id"]),
                verified_description_digest=_sha256_text(row["description"]),
            )
            return self._candidate(run_id, str(row["candidate_id"]))
        return None

    @staticmethod
    def _visible_csod_form_review_matches(row: sqlite3.Row) -> bool:
        """Permit browser-bound text only for the same client-rendered CSOD requisition."""
        if not str(row["description"] or "").strip():
            return False
        try:
            review = json.loads(str(row["form_review_json"] or "{}"))
            official = urlsplit(str(row["canonical_url"] or ""))
            observed = urlsplit(str(review.get("observed_url") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if (
            review.get("status") != "form_surface_reviewed"
            or not official.hostname
            or official.hostname != observed.hostname
            or not official.hostname.endswith(".csod.com")
        ):
            return False
        official_match = re.search(r"/(?:home/)?requisition/([^/?]+)", official.path)
        observed_match = re.search(r"/requisition/([^/?]+)(?:/|$)", observed.path)
        return bool(official_match and observed_match and official_match.group(1) == observed_match.group(1))

    def _legacy_outcome_for_url(self, canonical_url: str) -> str:
        """Treat legacy applied/in-flight rows as duplicate-submission evidence."""
        historical_table = self.connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'workflow_historical_applications'
            """
        ).fetchone()
        if historical_table is not None:
            historical = self.connection.execute(
                """
                SELECT evidence_state FROM workflow_historical_applications
                WHERE identity_key = ? OR canonical_url = ?
                ORDER BY CASE evidence_state
                    WHEN 'ambiguous_claim' THEN 0 ELSE 1 END
                LIMIT 1
                """,
                ("url:" + canonical_url, canonical_url),
            ).fetchone()
            if historical is not None:
                return "historical_" + str(historical["evidence_state"])
        legacy_path = self.path.parent / "applypilot.db"
        if not legacy_path.is_file():
            return ""
        connection = sqlite3.connect(f"file:{legacy_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if not {"url", "apply_status"}.issubset(columns):
                return ""
            selected = ["url", "apply_status"]
            if "application_url" in columns:
                selected.append("application_url")
            if "applied_at" in columns:
                selected.append("applied_at")
            rows = connection.execute(
                f"SELECT {', '.join(selected)} FROM jobs "
                "WHERE COALESCE(apply_status, '') IN "
                "('applied', 'submitting', 'submitted_confirmed', 'submitted_unconfirmed', 'outcome_unknown')"
                + (" OR applied_at IS NOT NULL" if "applied_at" in columns else "")
            ).fetchall()
            for row in rows:
                urls = [str(row["url"] or "")]
                if "application_url" in columns:
                    urls.append(str(row["application_url"] or ""))
                if any(url and canonicalize_url(url) == canonical_url for url in urls):
                    return str(row["apply_status"] or "applied")
            return ""
        finally:
            connection.close()


def canonicalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise WorkflowError("candidate URL must be public HTTP(S)")
    hostname = parsed.hostname.lower().strip(".")
    if hostname == "localhost" or hostname.endswith(".local"):
        raise WorkflowError("candidate URL must not target a local host")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise WorkflowError("candidate URL must not target a private address")
    query = urlencode(
        sorted(
            (key, val)
            for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in {"gh_src", "ref", "referrer", "source"}
        )
    )
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            query,
            "",
        )
    )


def _artifact_bundle_digest(paths: Any) -> str:
    if not isinstance(paths, dict) or not paths:
        return ""
    rows: list[tuple[str, str]] = []
    for key, raw_path in sorted(paths.items()):
        path = Path(str(raw_path)).resolve()
        if not path.is_file():
            raise WorkflowError(f"material artifact missing: {key}")
        rows.append((str(key), hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(_json(rows).encode("utf-8")).hexdigest()


def _request_id(run_id: str, candidate_id: str, mode: str, binding: str) -> str:
    return hashlib.sha256(f"{run_id}\0{candidate_id}\0{mode}\0{binding}".encode()).hexdigest()


def _dry_run_binding(material_digest: str, generation: int) -> str:
    """Keep generation-zero request ids compatible while supporting re-review."""
    return material_digest if generation == 0 else f"{material_digest}:review-{generation}"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _required(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "")
    if not value:
        raise WorkflowError(f"run manifest missing {key}")
    return value


def _safe_file_identifier(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) or ".." in value:
        raise WorkflowError(f"invalid {label}")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise WorkflowError(f"expected JSON object: {path}")
    return payload


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    _write_private_bytes(path, (_json(payload) + "\n").encode("utf-8"))


def _write_private_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    temporary.write_bytes(payload)
    temporary.chmod(0o600)
    temporary.replace(path)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

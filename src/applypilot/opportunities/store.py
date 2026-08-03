"""Durable SQLite ledger for company-level opportunity intelligence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot.opportunities.models import (
    OpportunityDecision,
    OpportunityLead,
    OpportunityStatus,
)

SCHEMA_VERSION = "applypilot.opportunities-store.v1"
_SAFE_ID = re.compile(r"^[a-zA-Z0-9_.:-]{1,120}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunity_runs (
    run_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    request_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error_class TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS opportunity_leads (
    lead_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES opportunity_runs(run_id),
    company_domain TEXT NOT NULL,
    signal TEXT NOT NULL,
    route TEXT NOT NULL,
    status TEXT NOT NULL,
    lead_json TEXT NOT NULL,
    lead_sha256 TEXT NOT NULL,
    score INTEGER,
    components_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, company_domain, signal)
);
CREATE TABLE IF NOT EXISTS opportunity_evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id TEXT NOT NULL REFERENCES opportunity_leads(lead_id),
    ordinal INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    UNIQUE(lead_id, ordinal)
);
CREATE TABLE IF NOT EXISTS opportunity_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id TEXT NOT NULL REFERENCES opportunity_leads(lead_id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_artifacts (
    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES opportunity_runs(run_id),
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, kind, sha256)
);
CREATE TABLE IF NOT EXISTS opportunity_draft_bindings (
    draft_id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL REFERENCES opportunity_leads(lead_id),
    draft_sha256 TEXT NOT NULL,
    contact_sha256 TEXT NOT NULL,
    lead_evidence_digest TEXT NOT NULL,
    fact_snapshot_digest TEXT NOT NULL,
    draft_json TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_send_bindings (
    authorization_id TEXT PRIMARY KEY,
    manifest_sha256 TEXT NOT NULL,
    sender TEXT NOT NULL,
    status TEXT NOT NULL,
    authorization_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    consumed_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS opportunity_send_receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    authorization_id TEXT NOT NULL REFERENCES opportunity_send_bindings(authorization_id),
    lead_id TEXT NOT NULL REFERENCES opportunity_leads(lead_id),
    draft_id TEXT NOT NULL REFERENCES opportunity_draft_bindings(draft_id),
    status TEXT NOT NULL,
    provider_receipt_sha256 TEXT NOT NULL DEFAULT '',
    response_sha256 TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE(authorization_id, lead_id, draft_id)
);
CREATE TABLE IF NOT EXISTS opportunity_outreach_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT NOT NULL REFERENCES opportunity_draft_bindings(draft_id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value: str, *, name: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{name} is invalid")
    return value


class OpportunityStore:
    """Persist leads, evidence, decisions, and future outreach bindings separately."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("opportunity database must not be a symbolic link")
        self.connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(SCHEMA)
        self._migrate_schema()
        self.connection.commit()
        os.chmod(self.path, 0o600)
        self._lock = threading.RLock()

    def _migrate_schema(self) -> None:
        """Add outreach columns to databases created by earlier local iterations."""
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(opportunity_draft_bindings)")
        }
        additions = {
            "lead_evidence_digest": "TEXT NOT NULL DEFAULT ''",
            "fact_snapshot_digest": "TEXT NOT NULL DEFAULT ''",
            "draft_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE opportunity_draft_bindings ADD COLUMN {name} {definition}"
                )
        auth_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(opportunity_send_bindings)")
        }
        if "consumed_at" not in auth_columns:
            self.connection.execute(
                "ALTER TABLE opportunity_send_bindings ADD COLUMN consumed_at TEXT NOT NULL DEFAULT ''"
            )

    def __enter__(self) -> OpportunityStore:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def start_run(self, run_id: str, request: dict[str, Any], *, request_path: Path) -> None:
        run_id = _safe_id(run_id, name="opportunity run id")
        now = _now()
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO opportunity_runs(run_id,schema_version,status,request_json,"
                "request_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    run_id,
                    SCHEMA_VERSION,
                    "awaiting_browser",
                    json.dumps(request, sort_keys=True, separators=(",", ":")),
                    str(request_path.resolve()),
                    now,
                    now,
                ),
            )

    def finish_run(self, run_id: str, *, status: str, error_class: str = "") -> None:
        run_id = _safe_id(run_id, name="opportunity run id")
        if status not in {"awaiting_browser", "complete", "partial", "failed"}:
            raise ValueError("opportunity run status is invalid")
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE opportunity_runs SET status=?,error_class=?,updated_at=? WHERE run_id=?",
                (status, error_class[:120], _now(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown opportunity run: {run_id}")

    def run_status(self, run_id: str) -> dict[str, Any]:
        run_id = _safe_id(run_id, name="opportunity run id")
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM opportunity_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            counts = self.connection.execute(
                "SELECT status,COUNT(*) AS count FROM opportunity_leads WHERE run_id=? GROUP BY status",
                (run_id,),
            ).fetchall()
        if row is None:
            raise KeyError(f"unknown opportunity run: {run_id}")
        result = dict(row)
        result["request"] = json.loads(str(result.pop("request_json")))
        result["lead_counts"] = {str(item["status"]): int(item["count"]) for item in counts}
        return result

    def persist_lead(
        self,
        run_id: str,
        lead: OpportunityLead,
        decision: OpportunityDecision,
    ) -> OpportunityLead:
        run_id = _safe_id(run_id, name="opportunity run id")
        _safe_id(lead.lead_id, name="opportunity lead id")
        promoted = lead.with_status(decision.status)
        payload = promoted.to_dict()
        now = _now()
        with self._lock, self.connection:
            previous = self.connection.execute(
                "SELECT status,lead_sha256 FROM opportunity_leads WHERE lead_id=?",
                (lead.lead_id,),
            ).fetchone()
            self.connection.execute(
                "INSERT INTO opportunity_leads(lead_id,run_id,company_domain,signal,route,status,"
                "lead_json,lead_sha256,score,components_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(lead_id) DO UPDATE SET status=excluded.status,lead_json=excluded.lead_json,"
                "lead_sha256=excluded.lead_sha256,score=excluded.score,"
                "components_json=excluded.components_json,updated_at=excluded.updated_at",
                (
                    lead.lead_id,
                    run_id,
                    lead.company_domain,
                    lead.signal.value,
                    lead.route.value,
                    decision.status.value,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    promoted.digest,
                    decision.score,
                    json.dumps(decision.components, sort_keys=True, separators=(",", ":")),
                    now,
                    now,
                ),
            )
            self.connection.execute(
                "DELETE FROM opportunity_evidence WHERE lead_id=?", (lead.lead_id,)
            )
            self.connection.executemany(
                "INSERT INTO opportunity_evidence(lead_id,ordinal,evidence_json) VALUES(?,?,?)",
                [
                    (
                        lead.lead_id,
                        ordinal,
                        json.dumps(item.to_dict(), sort_keys=True, separators=(",", ":")),
                    )
                    for ordinal, item in enumerate(promoted.evidence, 1)
                ],
            )
            from_status = str(previous["status"]) if previous else OpportunityStatus.OBSERVED.value
            if previous is None or from_status != decision.status.value:
                self.connection.execute(
                    "INSERT INTO opportunity_transitions(lead_id,from_status,to_status,reasons_json,"
                    "decision_json,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        lead.lead_id,
                        from_status,
                        decision.status.value,
                        json.dumps(decision.reasons),
                        json.dumps(decision.to_dict(), sort_keys=True, separators=(",", ":")),
                        now,
                    ),
                )
        return promoted

    def get_lead(self, lead_id: str) -> dict[str, Any]:
        lead_id = _safe_id(lead_id, name="opportunity lead id")
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM opportunity_leads WHERE lead_id=?", (lead_id,)
            ).fetchone()
            evidence = self.connection.execute(
                "SELECT evidence_json FROM opportunity_evidence WHERE lead_id=? ORDER BY ordinal",
                (lead_id,),
            ).fetchall()
            transitions = self.connection.execute(
                "SELECT from_status,to_status,reasons_json,decision_json,created_at "
                "FROM opportunity_transitions WHERE lead_id=? ORDER BY transition_id",
                (lead_id,),
            ).fetchall()
        if row is None:
            raise KeyError(f"unknown opportunity lead: {lead_id}")
        result = dict(row)
        result["lead"] = json.loads(str(result.pop("lead_json")))
        result["components"] = json.loads(str(result.pop("components_json")))
        result["evidence"] = [json.loads(str(item["evidence_json"])) for item in evidence]
        result["transitions"] = [
            {
                **dict(item),
                "reasons": json.loads(str(item["reasons_json"])),
                "decision": json.loads(str(item["decision_json"])),
            }
            for item in transitions
        ]
        for item in result["transitions"]:
            item.pop("reasons_json", None)
            item.pop("decision_json", None)
        return result

    def list_leads(self, *, status: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("opportunity list limit must be between 1 and 100")
        parameters: list[Any] = []
        where = ""
        if status:
            status = OpportunityStatus(status).value
            where = "WHERE status=?"
            parameters.append(status)
        parameters.append(limit)
        with self._lock:
            rows = self.connection.execute(
                f"SELECT lead_id,run_id,company_domain,signal,route,status,score,lead_sha256,"
                f"created_at,updated_at FROM opportunity_leads {where} "
                "ORDER BY COALESCE(score,-1) DESC,updated_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def record_artifact(self, run_id: str, *, kind: str, path: Path, sha256: str) -> None:
        run_id = _safe_id(run_id, name="opportunity run id")
        if not _SAFE_ID.fullmatch(kind) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("opportunity artifact binding is invalid")
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO opportunity_artifacts(run_id,kind,path,sha256,created_at) "
                "VALUES(?,?,?,?,?)",
                (run_id, kind, str(path.resolve()), sha256, _now()),
            )

    def persist_draft(self, draft: Any, *, artifact_path: Path) -> None:
        from applypilot.opportunities.outreach import validate_outreach_draft

        validate_outreach_draft(draft)
        contact_sha256 = hashlib.sha256(draft.recipient.encode("utf-8")).hexdigest()
        now = _now()
        with self._lock, self.connection:
            lead = self.connection.execute(
                "SELECT status,lead_sha256 FROM opportunity_leads WHERE lead_id=?",
                (draft.lead_id,),
            ).fetchone()
            if lead is None or str(lead["status"]) != OpportunityStatus.VERIFIED.value:
                raise ValueError("outreach draft requires a persisted verified lead")
            if str(lead["lead_sha256"]) != draft.lead_evidence_digest:
                raise ValueError("outreach draft lead evidence binding changed")
            self.connection.execute(
                "INSERT INTO opportunity_draft_bindings(draft_id,lead_id,draft_sha256,"
                "contact_sha256,lead_evidence_digest,fact_snapshot_digest,draft_json,artifact_path,"
                "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    draft.draft_id,
                    draft.lead_id,
                    draft.sha256,
                    contact_sha256,
                    draft.lead_evidence_digest,
                    draft.fact_snapshot_digest,
                    json.dumps(draft.to_dict(), sort_keys=True, separators=(",", ":")),
                    str(artifact_path.resolve()),
                    OpportunityStatus.DRAFT_READY.value,
                    now,
                    now,
                ),
            )
            self._record_outreach_transition(
                draft.draft_id,
                from_status="",
                to_status=OpportunityStatus.DRAFT_READY.value,
                created_at=now,
            )

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        draft_id = _safe_id(draft_id, name="outreach draft id")
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM opportunity_draft_bindings WHERE draft_id=?", (draft_id,)
            ).fetchone()
            transitions = self.connection.execute(
                "SELECT from_status,to_status,created_at FROM opportunity_outreach_transitions "
                "WHERE draft_id=? ORDER BY transition_id",
                (draft_id,),
            ).fetchall()
        if row is None:
            raise KeyError(f"unknown outreach draft: {draft_id}")
        result = dict(row)
        result["draft"] = json.loads(str(result.pop("draft_json")))
        result["transitions"] = [dict(item) for item in transitions]
        return result

    def sent_count(self) -> int:
        with self._lock:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM opportunity_send_receipts WHERE status IN "
                "('provider_accepted','submitted','sent','delivered','replied')"
            ).fetchone()
        return int(row[0])

    def record_authorization(self, authorization: Any, *, artifact_path: Path) -> None:
        now = _now()
        with self._lock, self.connection:
            for lead_id, draft_id, draft_sha256 in authorization.items:
                row = self.connection.execute(
                    "SELECT lead_id,draft_sha256,status FROM opportunity_draft_bindings "
                    "WHERE draft_id=?",
                    (draft_id,),
                ).fetchone()
                if (
                    row is None
                    or str(row["lead_id"]) != lead_id
                    or str(row["draft_sha256"]) != draft_sha256
                    or str(row["status"]) != OpportunityStatus.DRAFT_READY.value
                ):
                    raise ValueError("outreach authorization item is not draft-ready")
            self.connection.execute(
                "INSERT INTO opportunity_send_bindings(authorization_id,manifest_sha256,sender,"
                "status,authorization_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    authorization.authorization_id,
                    authorization.sha256,
                    authorization.sender,
                    OpportunityStatus.AUTHORIZED.value,
                    json.dumps(authorization.to_dict(), sort_keys=True, separators=(",", ":")),
                    now,
                    now,
                ),
            )
            for _lead_id, draft_id, _draft_sha256 in authorization.items:
                self._record_outreach_transition(
                    draft_id,
                    from_status=OpportunityStatus.DRAFT_READY.value,
                    to_status=OpportunityStatus.AUTHORIZED.value,
                    created_at=now,
                )
                self.connection.execute(
                    "UPDATE opportunity_draft_bindings SET status=?,updated_at=? WHERE draft_id=?",
                    (OpportunityStatus.AUTHORIZED.value, now, draft_id),
                )
        self.record_artifact(
            self._run_id_for_lead(authorization.items[0][0]),
            kind="outreach_authorization",
            path=artifact_path,
            sha256=authorization.sha256,
        )

    def consume_authorization(self, authorization: Any, *, now: datetime) -> dict[str, Any]:
        """Atomically spend one exact authorization before an outbound handoff exists."""
        now_text = now.astimezone(timezone.utc).isoformat()
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.connection.execute(
                    "SELECT manifest_sha256,status,authorization_json FROM opportunity_send_bindings "
                    "WHERE authorization_id=?",
                    (authorization.authorization_id,),
                ).fetchone()
                if (
                    row is None
                    or str(row["manifest_sha256"]) != authorization.sha256
                    or str(row["status"]) != OpportunityStatus.AUTHORIZED.value
                ):
                    raise PermissionError("outreach authorization is unknown or already consumed")
                for lead_id, draft_id, draft_sha256 in authorization.items:
                    draft = self.connection.execute(
                        "SELECT lead_id,draft_sha256,status FROM opportunity_draft_bindings "
                        "WHERE draft_id=?",
                        (draft_id,),
                    ).fetchone()
                    if (
                        draft is None
                        or str(draft["lead_id"]) != lead_id
                        or str(draft["draft_sha256"]) != draft_sha256
                        or str(draft["status"]) != OpportunityStatus.AUTHORIZED.value
                    ):
                        raise PermissionError("authorized outreach draft binding changed")
                self.connection.execute(
                    "UPDATE opportunity_send_bindings SET status=?,updated_at=?,consumed_at=? "
                    "WHERE authorization_id=?",
                    ("consumed", now_text, now_text, authorization.authorization_id),
                )
                for _lead_id, draft_id, _draft_sha256 in authorization.items:
                    self._record_outreach_transition(
                        draft_id,
                        from_status=OpportunityStatus.AUTHORIZED.value,
                        to_status=OpportunityStatus.QUEUED.value,
                        created_at=now_text,
                    )
                    self.connection.execute(
                        "UPDATE opportunity_draft_bindings SET status=?,updated_at=? WHERE draft_id=?",
                        (OpportunityStatus.QUEUED.value, now_text, draft_id),
                    )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        payload = {
            "authorization_id": authorization.authorization_id,
            "authorization_sha256": authorization.sha256,
            "consumed_at": now_text,
            "item_count": len(authorization.items),
        }
        payload["sha256"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return payload

    def authorization_status(self, authorization_id: str) -> dict[str, Any]:
        authorization_id = _safe_id(authorization_id, name="outreach authorization id")
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM opportunity_send_bindings WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown outreach authorization: {authorization_id}")
        result = dict(row)
        result["authorization"] = json.loads(str(result.pop("authorization_json")))
        return result

    def record_send_receipts(
        self,
        authorization_id: str,
        *,
        receipts: list[dict[str, Any]],
        response_sha256: str,
    ) -> None:
        authorization_id = _safe_id(
            authorization_id, name="outreach authorization id"
        )
        if not re.fullmatch(r"[0-9a-f]{64}", response_sha256):
            raise ValueError("outreach response digest is invalid")
        with self._lock, self.connection:
            for receipt in receipts:
                provider_receipt = str(receipt.get("provider_receipt_id") or "")
                provider_hash = (
                    hashlib.sha256(provider_receipt.encode()).hexdigest()
                    if provider_receipt
                    else ""
                )
                cursor = self.connection.execute(
                    "INSERT INTO opportunity_send_receipts(authorization_id,lead_id,draft_id,status,"
                    "provider_receipt_sha256,response_sha256,observed_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(authorization_id,lead_id,draft_id) DO NOTHING",
                    (
                        authorization_id,
                        receipt["lead_id"],
                        receipt["draft_id"],
                        receipt["status"],
                        provider_hash,
                        response_sha256,
                        receipt["observed_at"],
                    ),
                )
                if cursor.rowcount == 0:
                    continue
                current_row = self.connection.execute(
                    "SELECT status FROM opportunity_draft_bindings WHERE draft_id=?",
                    (receipt["draft_id"],),
                ).fetchone()
                current_status = str(current_row["status"]) if current_row else ""
                if receipt["status"] != "not_attempted":
                    self._record_outreach_transition(
                        receipt["draft_id"],
                        from_status=current_status,
                        to_status=OpportunityStatus.SEND_ATTEMPTED.value,
                        created_at=str(receipt["observed_at"]),
                    )
                    current_status = OpportunityStatus.SEND_ATTEMPTED.value
                self._record_outreach_transition(
                    receipt["draft_id"],
                    from_status=current_status,
                    to_status=str(receipt["status"]),
                    created_at=str(receipt["observed_at"]),
                )
                self.connection.execute(
                    "UPDATE opportunity_draft_bindings SET status=?,updated_at=? WHERE draft_id=?",
                    (receipt["status"], _now(), receipt["draft_id"]),
                )

    def _record_outreach_transition(
        self,
        draft_id: str,
        *,
        from_status: str,
        to_status: str,
        created_at: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO opportunity_outreach_transitions(draft_id,from_status,to_status,created_at) "
            "VALUES(?,?,?,?)",
            (draft_id, from_status, to_status, created_at),
        )

    def _run_id_for_lead(self, lead_id: str) -> str:
        row = self.connection.execute(
            "SELECT run_id FROM opportunity_leads WHERE lead_id=?", (lead_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown opportunity lead: {lead_id}")
        return str(row["run_id"])

"""Durable SQLite ledger for company-level opportunity intelligence."""

from __future__ import annotations

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
    updated_at TEXT NOT NULL
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
        self.connection.commit()
        os.chmod(self.path, 0o600)
        self._lock = threading.RLock()

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

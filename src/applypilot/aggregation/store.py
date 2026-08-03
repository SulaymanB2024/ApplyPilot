"""Durable aggregation evidence and immutable snapshot revisions."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot.aggregation.models import (
    AggregationRequest,
    JobObservation,
    SourceKind,
    VerificationState,
)
from applypilot.aggregation.normalization import merge_observations

SNAPSHOT_SCHEMA_VERSION = "applypilot.aggregation-snapshot.v2"
_SAFE_ID = re.compile(r"^[a-zA-Z0-9_.:-]{1,120}$")
_TERMINAL_SOURCE_STATUSES = frozenset({"complete", "partial", "failed", "timed_out", "cancelled"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS aggregation_runs (
    run_id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    observation_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS aggregation_sources (
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    observed_count INTEGER NOT NULL DEFAULT 0,
    error_class TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, source_id),
    FOREIGN KEY (run_id) REFERENCES aggregation_runs(run_id)
);
CREATE TABLE IF NOT EXISTS job_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    source TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE (run_id, source, source_job_id, canonical_key),
    FOREIGN KEY (run_id) REFERENCES aggregation_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_job_observations_run_key
    ON job_observations(run_id, canonical_key);
CREATE TABLE IF NOT EXISTS canonical_key_aliases (
    run_id TEXT NOT NULL,
    old_key TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, old_key),
    FOREIGN KEY (run_id) REFERENCES aggregation_runs(run_id)
);
CREATE TABLE IF NOT EXISTS aggregation_snapshots (
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    parent_sha256 TEXT NOT NULL DEFAULT '',
    observation_high_watermark INTEGER NOT NULL,
    candidate_count INTEGER NOT NULL,
    advanceable_count INTEGER NOT NULL,
    reason TEXT NOT NULL,
    snapshot_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, revision),
    UNIQUE (sha256),
    FOREIGN KEY (run_id) REFERENCES aggregation_runs(run_id)
);
CREATE TABLE IF NOT EXISTS aggregation_portal_missions (
    run_id TEXT NOT NULL,
    portal TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    request_path TEXT NOT NULL DEFAULT '',
    response_path TEXT NOT NULL DEFAULT '',
    result_count INTEGER NOT NULL DEFAULT 0,
    error_class TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, portal),
    UNIQUE (run_id, ordinal),
    FOREIGN KEY (run_id) REFERENCES aggregation_runs(run_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise ValueError(f"invalid {field_name}")
    return normalized


def snapshot_digest(payload: dict[str, Any]) -> str:
    """Return the canonical digest, excluding the digest field itself."""
    unsigned = dict(payload)
    unsigned.pop("sha256", None)
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AggregationStore:
    """SQLite-backed run evidence with immutable snapshot publication."""

    def __init__(self, path: Path, *, run_dir: Path | None = None) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.run_dir = (run_dir or self.path.parent / "aggregation-runs").resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 10000")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        self.connection.commit()
        os.chmod(self.path, 0o600)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    @contextmanager
    def _snapshot_file_lock(self, run_id: str):
        lock_dir = self.run_dir / run_id
        lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(lock_dir / ".snapshot.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def get_request(self, run_id: str) -> AggregationRequest:
        run_id = _safe_id(run_id, field_name="aggregation run id")
        with self._lock:
            row = self.connection.execute(
                "SELECT request_json FROM aggregation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown aggregation run: {run_id}")
        payload = json.loads(str(row["request_json"]))
        payload["query_terms"] = tuple(payload.get("query_terms") or ())
        payload["locations"] = tuple(payload.get("locations") or ())
        request = AggregationRequest(**payload)
        request.validate()
        return request

    def start_run(self, run_id: str, request: AggregationRequest) -> None:
        run_id = _safe_id(run_id, field_name="aggregation run id")
        request.validate()
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO aggregation_runs(run_id, query, request_json, status, created_at) "
                "VALUES(?, ?, ?, 'running', ?)",
                (run_id, request.query, json.dumps(asdict(request), sort_keys=True), _now()),
            )

    def start_source(
        self, run_id: str, source: SourceKind, *, source_id: str | None = None
    ) -> str:
        run_id = _safe_id(run_id, field_name="aggregation run id")
        stable_id = _safe_id(source_id or source.value, field_name="aggregation source id")
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO aggregation_sources(run_id, source_id, source_kind, status, started_at) "
                "VALUES(?, ?, ?, 'running', ?)",
                (run_id, stable_id, source.value, _now()),
            )
        return stable_id

    def record_observation(
        self,
        run_id: str,
        observation: JobObservation,
        *,
        source_id: str | None = None,
    ) -> bool:
        run_id = _safe_id(run_id, field_name="aggregation run id")
        stable_id = _safe_id(source_id or observation.source.value, field_name="aggregation source id")
        payload = json.dumps(asdict(observation), sort_keys=True)
        with self._lock, self.connection:
            if observation.advanceable:
                prior_rows = self.connection.execute(
                    "SELECT DISTINCT canonical_key FROM job_observations "
                    "WHERE run_id = ? AND source = ? AND source_job_id = ?",
                    (run_id, observation.source.value, observation.source_job_id),
                ).fetchall()
                for row in prior_rows:
                    old_key = str(row["canonical_key"])
                    if old_key != observation.canonical_key:
                        self.connection.execute(
                            "INSERT INTO canonical_key_aliases("
                            "run_id, old_key, canonical_key, reason, created_at) "
                            "VALUES(?, ?, ?, 'exact_source_identity', ?) "
                            "ON CONFLICT(run_id, old_key) DO UPDATE SET "
                            "canonical_key=excluded.canonical_key, reason=excluded.reason",
                            (run_id, old_key, observation.canonical_key, _now()),
                        )
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO job_observations("
                "run_id, canonical_key, source, source_job_id, source_id, payload_json, observed_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    observation.canonical_key,
                    observation.source.value,
                    observation.source_job_id,
                    stable_id,
                    payload,
                    observation.observed_at,
                ),
            )
            if cursor.rowcount:
                self.connection.execute(
                    "UPDATE aggregation_sources SET observed_count = observed_count + 1 "
                    "WHERE run_id = ? AND source_id = ?",
                    (run_id, stable_id),
                )
            return bool(cursor.rowcount)

    def finish_source(
        self,
        run_id: str,
        source: SourceKind,
        *,
        status: str,
        error_class: str = "",
        source_id: str | None = None,
    ) -> None:
        if status not in _TERMINAL_SOURCE_STATUSES:
            raise ValueError("invalid aggregation source terminal status")
        run_id = _safe_id(run_id, field_name="aggregation run id")
        stable_id = _safe_id(source_id or source.value, field_name="aggregation source id")
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE aggregation_sources SET status = ?, error_class = ?, completed_at = ? "
                "WHERE run_id = ? AND source_id = ? AND source_kind = ?",
                (status, error_class[:120], _now(), run_id, stable_id, source.value),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown aggregation source: {stable_id}")

    def add_alias(self, run_id: str, *, old_key: str, canonical_key: str, reason: str) -> None:
        run_id = _safe_id(run_id, field_name="aggregation run id")
        if old_key == canonical_key:
            return
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO canonical_key_aliases(run_id, old_key, canonical_key, reason, created_at) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(run_id, old_key) DO UPDATE SET "
                "canonical_key=excluded.canonical_key, reason=excluded.reason",
                (run_id, old_key, canonical_key, reason[:120], _now()),
            )

    def _resolved_key(self, run_id: str, key: str) -> str:
        seen: set[str] = set()
        current = key
        while current not in seen:
            seen.add(current)
            row = self.connection.execute(
                "SELECT canonical_key FROM canonical_key_aliases WHERE run_id = ? AND old_key = ?",
                (run_id, current),
            ).fetchone()
            if row is None:
                return current
            current = str(row["canonical_key"])
        raise ValueError("canonical key alias cycle detected")

    def snapshot(self, run_id: str, *, high_watermark: int | None = None) -> dict[str, Any]:
        run_id = _safe_id(run_id, field_name="aggregation run id")
        with self._lock:
            run = self.connection.execute(
                "SELECT * FROM aggregation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"unknown aggregation run: {run_id}")
            if high_watermark is None:
                high_watermark = int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(observation_id), 0) FROM job_observations WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()[0]
                )
            rows = self.connection.execute(
                "SELECT canonical_key, payload_json FROM job_observations "
                "WHERE run_id = ? AND observation_id <= ? "
                "ORDER BY observation_id, source",
                (run_id, high_watermark),
            ).fetchall()
            grouped: dict[str, list[JobObservation]] = {}
            for row in rows:
                payload = json.loads(row["payload_json"])
                payload["source"] = SourceKind(payload["source"])
                payload["verification_state"] = VerificationState(payload["verification_state"])
                observation = JobObservation(**payload)
                resolved_key = self._resolved_key(run_id, observation.canonical_key)
                if resolved_key != observation.canonical_key:
                    observation = JobObservation(**{**asdict(observation), "canonical_key": resolved_key})
                grouped.setdefault(resolved_key, []).append(observation)
            jobs: list[dict[str, Any]] = []
            for key in sorted(grouped):
                merged = merge_observations(grouped[key])
                item = asdict(merged)
                item["source_count"] = merged.source_count
                item["observations"] = [asdict(row) for row in merged.observations]
                jobs.append(item)
            sources = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT source_id, source_kind AS source, status, observed_count, error_class, "
                    "started_at, completed_at FROM aggregation_sources "
                    "WHERE run_id = ? ORDER BY source_kind, source_id",
                    (run_id,),
                ).fetchall()
            ]
            return {
                "run_id": run_id,
                "query": run["query"],
                "status": run["status"],
                "observation_high_watermark": high_watermark,
                "candidate_count": len(jobs),
                "advanceable_count": sum(1 for job in jobs if job["advanceable"]),
                "observation_count": len(rows),
                "duplicate_count": len(rows) - len(jobs),
                "sources": sources,
                "jobs": jobs,
            }

    def complete_run(self, run_id: str, *, status: str = "complete") -> dict[str, Any]:
        if status not in {"complete", "partial", "failed"}:
            raise ValueError("invalid aggregation run terminal status")
        snapshot = self.snapshot(run_id)
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE aggregation_runs SET status = ?, candidate_count = ?, "
                "observation_count = ?, duplicate_count = ?, completed_at = ? WHERE run_id = ?",
                (
                    status,
                    snapshot["candidate_count"],
                    snapshot["observation_count"],
                    snapshot["duplicate_count"],
                    _now(),
                    run_id,
                ),
            )
        return self.snapshot(run_id)

    def publish_snapshot(
        self,
        run_id: str,
        *,
        reason: str,
        status: str = "partial",
        pending_enrichment: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if status not in {"complete", "partial"}:
            raise ValueError("published snapshot must be complete or partial")
        run_id = _safe_id(run_id, field_name="aggregation run id")
        with self._lock, self._snapshot_file_lock(run_id):
            base = self.snapshot(run_id)
            previous = self.connection.execute(
                "SELECT revision, sha256 FROM aggregation_snapshots WHERE run_id = ? "
                "ORDER BY revision DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            revision = int(previous["revision"]) + 1 if previous else 1
            parent_sha256 = str(previous["sha256"]) if previous else ""
            payload = {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                **base,
                "status": status,
                "revision": revision,
                "parent_sha256": parent_sha256,
                "pending_enrichment": list(pending_enrichment),
                "created_at": _now(),
                "reason": reason[:120],
            }
            payload["sha256"] = snapshot_digest(payload)
            directory = self.run_dir / run_id / "snapshots"
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / f"snapshot.{revision}.json"
            temporary = directory / f".snapshot.{revision}.{os.getpid()}.tmp"
            encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                view = memoryview(encoded)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("aggregation snapshot write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, path)
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            with self.connection:
                self.connection.execute(
                    "INSERT INTO aggregation_snapshots("
                    "run_id, revision, parent_sha256, observation_high_watermark, "
                    "candidate_count, advanceable_count, reason, snapshot_path, sha256, created_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        revision,
                        parent_sha256,
                        base["observation_high_watermark"],
                        base["candidate_count"],
                        base["advanceable_count"],
                        reason[:120],
                        str(path),
                        payload["sha256"],
                        payload["created_at"],
                    ),
                )
            return payload

    def queue_portal_missions(self, run_id: str, portals: tuple[str, ...]) -> None:
        run_id = _safe_id(run_id, field_name="aggregation run id")
        if not portals or len(set(portals)) != len(portals):
            raise ValueError("portal mission queue must be unique and non-empty")
        if not set(portals) <= {"handshake", "runway"}:
            raise ValueError("portal mission queue contains an unsupported portal")
        with self._lock, self.connection:
            existing = self.connection.execute(
                "SELECT COUNT(*) FROM aggregation_portal_missions WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            if existing:
                raise ValueError("portal mission queue already exists")
            for ordinal, portal in enumerate(portals, 1):
                self.connection.execute(
                    "INSERT INTO aggregation_portal_missions("
                    "run_id, portal, ordinal, status, created_at) VALUES(?, ?, ?, 'queued', ?)",
                    (run_id, portal, ordinal, _now()),
                )

    def portal_missions(self, run_id: str) -> list[dict[str, Any]]:
        run_id = _safe_id(run_id, field_name="aggregation run id")
        with self._lock:
            return [
                dict(row)
                for row in self.connection.execute(
                    "SELECT portal, ordinal, status, request_id, request_path, response_path, "
                    "result_count, error_class, created_at, started_at, completed_at "
                    "FROM aggregation_portal_missions WHERE run_id = ? ORDER BY ordinal",
                    (run_id,),
                ).fetchall()
            ]

    def activate_portal_mission(
        self,
        run_id: str,
        *,
        portal: str,
        request_id: str,
        request_path: Path,
        response_path: Path,
    ) -> None:
        run_id = _safe_id(run_id, field_name="aggregation run id")
        if portal not in {"handshake", "runway"}:
            raise ValueError("unsupported portal mission")
        if not re.fullmatch(r"[0-9a-f]{64}", request_id):
            raise ValueError("portal request id is invalid")
        with self._lock, self.connection:
            active = self.connection.execute(
                "SELECT portal FROM aggregation_portal_missions "
                "WHERE run_id = ? AND status IN ('awaiting_response', 'response_ready')",
                (run_id,),
            ).fetchall()
            if active:
                raise ValueError("authenticated browser resource is already locked")
            cursor = self.connection.execute(
                "UPDATE aggregation_portal_missions SET status='awaiting_response', "
                "request_id=?, request_path=?, response_path=?, started_at=? "
                "WHERE run_id=? AND portal=? AND status='queued'",
                (
                    request_id,
                    str(request_path.resolve()),
                    str(response_path.resolve()),
                    _now(),
                    run_id,
                    portal,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("portal mission is not queued")

    def finish_portal_mission(
        self,
        run_id: str,
        *,
        portal: str,
        status: str,
        result_count: int,
        error_class: str = "",
    ) -> None:
        allowed = {"complete", "partial", "auth_required", "blocked", "budget_exhausted"}
        if status not in allowed or result_count < 0:
            raise ValueError("portal mission terminal state is invalid")
        run_id = _safe_id(run_id, field_name="aggregation run id")
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE aggregation_portal_missions SET status=?, result_count=?, error_class=?, "
                "completed_at=? WHERE run_id=? AND portal=? "
                "AND status IN ('awaiting_response', 'response_ready')",
                (status, result_count, error_class[:120], _now(), run_id, portal),
            )
            if cursor.rowcount != 1:
                raise ValueError("portal mission is not active")

    def get_snapshot(self, run_id: str, revision: int) -> tuple[Path, dict[str, Any]]:
        run_id = _safe_id(run_id, field_name="aggregation run id")
        if revision < 1:
            raise ValueError("aggregation snapshot revision must be positive")
        with self._lock:
            row = self.connection.execute(
                "SELECT snapshot_path, sha256 FROM aggregation_snapshots "
                "WHERE run_id = ? AND revision = ?",
                (run_id, revision),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown aggregation snapshot: {run_id}@{revision}")
        path = Path(str(row["snapshot_path"])).resolve(strict=True)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("unsupported aggregation snapshot schema")
        if snapshot_digest(payload) != row["sha256"] or payload.get("sha256") != row["sha256"]:
            raise ValueError("aggregation snapshot digest mismatch")
        return path, payload

    def latest_revision(self, run_id: str) -> int:
        run_id = _safe_id(run_id, field_name="aggregation run id")
        with self._lock:
            row = self.connection.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM aggregation_snapshots WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row[0])

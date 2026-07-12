"""Durable, fail-closed state for a target-confirmed application campaign.

The store deliberately knows nothing about browsers, models, email, or the CLI.
It provides the small persistence boundary an outer controller needs:

* an immutable campaign manifest;
* one exclusive writer lease;
* a fsynced, append-only write-ahead event log;
* an atomically replaced state projection;
* canonical-job deduplication and exact candidate states; and
* evidence-bound confirmation accounting.

Raw prompts, profile values, and contact values may live in a pending artifact
when an operator explicitly persists one, but are never copied into state,
events, or heartbeat snapshots.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Self


CAMPAIGN_SCHEMA_VERSION = "applypilot-campaign-v2"
EVENT_SCHEMA_VERSION = "applypilot-campaign-event-v1"
HEARTBEAT_SCHEMA_VERSION = "applypilot-campaign-heartbeat-v1"
HEARTBEAT_RECORD_SCHEMA_VERSION = "applypilot-campaign-heartbeat-record-v1"
DEFAULT_TARGET_CONFIRMED = 100
MAX_TARGET_CONFIRMED = 100
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 300
MAX_EVIDENCE_ARTIFACT_BYTES = 10_000_000

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_SAFE_ISSUER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:+-]{0,199}\Z")
_SAFE_CODE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,99}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_REVISION = re.compile(r"[0-9a-f]{7,64}\Z")


class CampaignError(RuntimeError):
    """Base error for invalid or unsafe campaign operations."""


class CampaignCorruptionError(CampaignError):
    """Raised when durable campaign artifacts disagree or are malformed."""


class CampaignLeaseError(CampaignError):
    """Raised when an exclusive writer lease is absent or unavailable."""


class CampaignTargetReached(CampaignError):
    """Raised before work that could exceed the confirmed target."""


class CampaignStatus(StrEnum):
    """Exact campaign states persisted by the store."""

    ACTIVE = "active"
    OUTCOME_REVIEW_REQUIRED = "outcome_review_required"
    TARGET_REACHED = "target_reached"


class CandidateState(StrEnum):
    """Exact candidate states in the deterministic application funnel."""

    DISCOVERED = "discovered"
    ELIGIBLE = "eligible"
    VERIFIED = "verified"
    MATERIALS_READY = "materials_ready"
    FORM_REVIEWED = "form_reviewed"
    AUTHORIZED = "authorized"
    SUBMITTING = "submitting"
    SUBMITTED_CONFIRMED = "submitted_confirmed"
    NOT_SUBMITTED = "not_submitted"
    OUTCOME_UNKNOWN = "outcome_unknown"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class EvidenceKind(StrEnum):
    """Typed durable evidence accepted by campaign accounting."""

    AUTHORIZATION_GRANT = "authorization_grant"
    AUTHORIZATION_CONSUMPTION = "authorization_consumption"
    SUBMISSION_RESPONSE = "submission_response"
    CONFIRMATION_EVIDENCE = "confirmation_evidence"
    CONTROLLER_RESULT = "controller_result"
    JOBS_ROW = "jobs_row"
    NOT_SUBMITTED_EVIDENCE = "not_submitted_evidence"


_ALLOWED_TRANSITIONS: dict[CandidateState, frozenset[CandidateState]] = {
    CandidateState.DISCOVERED: frozenset(
        {
            CandidateState.ELIGIBLE,
            CandidateState.REJECTED,
            CandidateState.BLOCKED,
            CandidateState.SKIPPED,
        }
    ),
    CandidateState.ELIGIBLE: frozenset(
        {
            CandidateState.VERIFIED,
            CandidateState.REJECTED,
            CandidateState.BLOCKED,
            CandidateState.SKIPPED,
        }
    ),
    CandidateState.VERIFIED: frozenset(
        {
            CandidateState.MATERIALS_READY,
            CandidateState.REJECTED,
            CandidateState.BLOCKED,
            CandidateState.SKIPPED,
        }
    ),
    CandidateState.MATERIALS_READY: frozenset(
        {
            CandidateState.FORM_REVIEWED,
            CandidateState.BLOCKED,
            CandidateState.SKIPPED,
        }
    ),
    CandidateState.FORM_REVIEWED: frozenset(
        {
            CandidateState.AUTHORIZED,
            CandidateState.BLOCKED,
            CandidateState.SKIPPED,
        }
    ),
    CandidateState.AUTHORIZED: frozenset(
        {
            CandidateState.SUBMITTING,
            CandidateState.BLOCKED,
            CandidateState.SKIPPED,
        }
    ),
    CandidateState.SUBMITTING: frozenset(
        {
            CandidateState.OUTCOME_UNKNOWN,
        }
    ),
    CandidateState.SUBMITTED_CONFIRMED: frozenset(),
    CandidateState.NOT_SUBMITTED: frozenset(),
    CandidateState.OUTCOME_UNKNOWN: frozenset(),
    CandidateState.REJECTED: frozenset(),
    CandidateState.BLOCKED: frozenset(),
    CandidateState.SKIPPED: frozenset(),
}


@dataclass(frozen=True)
class CampaignManifest:
    """Immutable inputs that bind every counted confirmation."""

    campaign_id: str
    created_at: str
    source_run_id: str
    query: str
    fact_digest: str
    context_digest: str
    policy_digest: str
    code_revision: str
    submit_authorized: bool
    allow_account_creation: bool
    fact_approval_receipt_sha256: str = ""
    fact_approval_signature_sha256: str = ""
    approval_issuer: str = ""
    approval_trust_store_sha256: str = ""
    fact_approval_expires_at: str = ""
    target_confirmed: int = DEFAULT_TARGET_CONFIRMED
    heartbeat_interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    schema_version: str = CAMPAIGN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_safe_id(self.campaign_id, field="campaign_id")
        _require_safe_id(self.source_run_id, field="source_run_id")
        _require_aware_iso(self.created_at, field="created_at")
        _require_bounded_text(self.query, field="query", max_chars=2_000)
        for field_name in ("fact_digest", "context_digest", "policy_digest"):
            _require_sha256(getattr(self, field_name), field=field_name)
        if not _GIT_REVISION.fullmatch(self.code_revision):
            raise ValueError("code_revision must be a lowercase Git revision")
        if not isinstance(self.submit_authorized, bool):
            raise ValueError("submit_authorized must be boolean")
        if not isinstance(self.allow_account_creation, bool):
            raise ValueError("allow_account_creation must be boolean")
        if self.allow_account_creation:
            raise ValueError("account creation requires a separate action-scoped grant")
        approval_values = (
            self.fact_approval_receipt_sha256,
            self.fact_approval_signature_sha256,
            self.approval_issuer,
            self.approval_trust_store_sha256,
            self.fact_approval_expires_at,
        )
        if self.submit_authorized:
            for field_name in (
                "fact_approval_receipt_sha256",
                "fact_approval_signature_sha256",
                "approval_trust_store_sha256",
            ):
                _require_sha256(getattr(self, field_name), field=field_name)
            _require_safe_issuer(self.approval_issuer)
            _require_aware_iso(self.fact_approval_expires_at, field="fact_approval_expires_at")
        elif any(approval_values):
            raise ValueError("review-only campaigns cannot carry live approval bindings")
        if not 1 <= self.target_confirmed <= MAX_TARGET_CONFIRMED:
            raise ValueError(
                f"target_confirmed must be between 1 and {MAX_TARGET_CONFIRMED}"
            )
        if not 60 <= self.heartbeat_interval_seconds <= 86_400:
            raise ValueError("heartbeat_interval_seconds must be between 60 and 86400")
        if self.schema_version != CAMPAIGN_SCHEMA_VERSION:
            raise ValueError("unsupported campaign manifest schema")

    @classmethod
    def new(
        cls,
        *,
        campaign_id: str,
        source_run_id: str,
        query: str,
        fact_digest: str,
        context_digest: str,
        policy_digest: str,
        code_revision: str,
        submit_authorized: bool,
        allow_account_creation: bool = False,
        fact_approval_receipt_sha256: str = "",
        fact_approval_signature_sha256: str = "",
        approval_issuer: str = "",
        approval_trust_store_sha256: str = "",
        fact_approval_expires_at: str = "",
        target_confirmed: int = DEFAULT_TARGET_CONFIRMED,
        heartbeat_interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        now: datetime | None = None,
    ) -> Self:
        return cls(
            campaign_id=campaign_id,
            created_at=_iso_now(now),
            source_run_id=source_run_id,
            query=query,
            fact_digest=fact_digest,
            context_digest=context_digest,
            policy_digest=policy_digest,
            code_revision=code_revision,
            submit_authorized=submit_authorized,
            allow_account_creation=allow_account_creation,
            fact_approval_receipt_sha256=fact_approval_receipt_sha256,
            fact_approval_signature_sha256=fact_approval_signature_sha256,
            approval_issuer=approval_issuer,
            approval_trust_store_sha256=approval_trust_store_sha256,
            fact_approval_expires_at=fact_approval_expires_at,
            target_confirmed=target_confirmed,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        expected = {
            "campaign_id",
            "created_at",
            "source_run_id",
            "query",
            "fact_digest",
            "context_digest",
            "policy_digest",
            "code_revision",
            "submit_authorized",
            "allow_account_creation",
            "fact_approval_receipt_sha256",
            "fact_approval_signature_sha256",
            "approval_issuer",
            "approval_trust_store_sha256",
            "fact_approval_expires_at",
            "target_confirmed",
            "heartbeat_interval_seconds",
            "schema_version",
        }
        if set(payload) != expected:
            raise CampaignCorruptionError("campaign manifest fields differ from schema")
        try:
            return cls(**dict(payload))
        except (TypeError, ValueError) as exc:
            raise CampaignCorruptionError("campaign manifest is invalid") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def digest(self) -> str:
        return _sha256(_canonical_json_bytes(self.to_dict()))


@dataclass(frozen=True)
class SubmissionBindings:
    """Reviewed inputs that a later confirmation must match exactly."""

    fact_digest: str
    context_digest: str
    policy_digest: str
    fact_approval_receipt_sha256: str
    submission_policy_digest: str
    packet_digest: str
    form_review_digest: str
    authorization_artifact_id: str
    authorization_grant_sha256: str

    def __post_init__(self) -> None:
        _require_safe_id(self.authorization_artifact_id, field="authorization_artifact_id")
        for field_name in (
            "fact_digest",
            "context_digest",
            "policy_digest",
            "fact_approval_receipt_sha256",
            "submission_policy_digest",
            "packet_digest",
            "form_review_digest",
            "authorization_grant_sha256",
        ):
            _require_sha256(getattr(self, field_name), field=field_name)


@dataclass(frozen=True)
class SubmissionConfirmation:
    """Hashed submission evidence; no raw page or confirmation values."""

    canonical_job_id: str
    status: str
    fact_digest: str
    context_digest: str
    policy_digest: str
    fact_approval_receipt_sha256: str
    submission_policy_digest: str
    packet_digest: str
    form_review_digest: str
    authorization_artifact_id: str
    authorization_grant_sha256: str
    authorization_consumption_artifact_id: str
    authorization_consumption_sha256: str
    submission_response_artifact_id: str
    submission_response_sha256: str
    confirmation_evidence_artifact_id: str
    confirmation_evidence_sha256: str
    controller_result_artifact_id: str
    controller_result_sha256: str
    jobs_row_artifact_id: str
    jobs_row_sha256: str
    submitted_at: str

    def __post_init__(self) -> None:
        _require_canonical_job_id(self.canonical_job_id)
        if self.status != CandidateState.SUBMITTED_CONFIRMED:
            raise ValueError("confirmation status must be submitted_confirmed")
        for field_name in (
            "authorization_artifact_id",
            "authorization_consumption_artifact_id",
            "submission_response_artifact_id",
            "confirmation_evidence_artifact_id",
            "controller_result_artifact_id",
            "jobs_row_artifact_id",
        ):
            _require_safe_id(getattr(self, field_name), field=field_name)
        for field_name in (
            "fact_digest",
            "context_digest",
            "policy_digest",
            "fact_approval_receipt_sha256",
            "submission_policy_digest",
            "packet_digest",
            "form_review_digest",
            "authorization_grant_sha256",
            "authorization_consumption_sha256",
        ):
            _require_sha256(getattr(self, field_name), field=field_name)
        for field_name in (
            "submission_response_sha256",
            "confirmation_evidence_sha256",
            "controller_result_sha256",
            "jobs_row_sha256",
        ):
            _require_sha256(getattr(self, field_name), field=field_name)
        _require_aware_iso(self.submitted_at, field="submitted_at")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CampaignLease:
    """Process-bound exclusive writer lease returned by ``acquire_lease``."""

    def __init__(self, store: CampaignStore, *, descriptor: int, owner_id: str, token: str) -> None:
        self._store = store
        self._descriptor = descriptor
        self.owner_id = owner_id
        self.token = token
        self._released = False

    @property
    def active(self) -> bool:
        return not self._released

    def __enter__(self) -> Self:
        if self._released:
            raise CampaignLeaseError("campaign lease has already been released")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._store._release_lease(self)


class CampaignStore:
    """Filesystem-backed campaign state with one fail-closed writer."""

    MANIFEST_NAME = "manifest.json"
    STATE_NAME = "state.json"
    EVENTS_NAME = "events.jsonl"
    LEASE_NAME = "writer.lease"
    INITIALIZE_LOCK_NAME = ".initialize.lock"
    HEARTBEAT_NAME = "heartbeat.json"
    PENDING_DIR = "pending"
    EVIDENCE_DIR = "evidence"

    def __init__(self, root: Path, manifest: CampaignManifest, state: dict[str, Any]) -> None:
        self.root = root
        self.manifest = manifest
        self._state = state
        self._active_lease: CampaignLease | None = None

    @classmethod
    def create(cls, root: Path, manifest: CampaignManifest) -> Self:
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        initialize_path = root / cls.INITIALIZE_LOCK_NAME
        descriptor = os.open(initialize_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            manifest_path = root / cls.MANIFEST_NAME
            state_path = root / cls.STATE_NAME
            events_path = root / cls.EVENTS_NAME
            if not manifest_path.exists() and (state_path.exists() or events_path.exists()):
                raise CampaignCorruptionError("campaign state exists without its manifest")
            if manifest_path.exists():
                existing_manifest = CampaignManifest.from_dict(_read_json_object(manifest_path))
                existing_stable = existing_manifest.to_dict()
                requested_stable = manifest.to_dict()
                existing_stable.pop("created_at")
                requested_stable.pop("created_at")
                if existing_stable != requested_stable:
                    raise FileExistsError("immutable campaign manifest already differs")
                manifest = existing_manifest
            else:
                _write_immutable_bytes(manifest_path, _pretty_json_bytes(manifest.to_dict()))

            if state_path.exists() and events_path.exists():
                return cls.open(root)
            if state_path.exists() and not events_path.exists():
                raise CampaignCorruptionError("campaign state exists without its event log")
            if events_path.exists():
                recovered_state = cls._recover_state_from_events(events_path, manifest)
                _atomic_write_json(state_path, recovered_state)
                return cls.open(root)

            initial_state = {
                "schema_version": CAMPAIGN_SCHEMA_VERSION,
                "campaign_id": manifest.campaign_id,
                "manifest_sha256": manifest.digest,
                "sequence": 0,
                "status": CampaignStatus.ACTIVE,
                "confirmed_count": 0,
                "candidates": {},
                "pending_artifacts": {},
                "resolved_pending_artifacts": {},
                "evidence_artifacts": {},
                "updated_at": manifest.created_at,
            }
            event = cls._event_for_state(
                initial_state,
                event_type="campaign_created",
                timestamp=manifest.created_at,
            )
            _append_fsynced_json_line(events_path, event)
            _atomic_write_json(state_path, initial_state)
            return cls(root, manifest, initial_state)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @classmethod
    def open(cls, root: Path) -> Self:
        root = Path(root).resolve()
        manifest_path = root / cls.MANIFEST_NAME
        state_path = root / cls.STATE_NAME
        events_path = root / cls.EVENTS_NAME
        if not manifest_path.exists() or not state_path.exists() or not events_path.exists():
            raise FileNotFoundError("campaign store is incomplete")
        manifest = CampaignManifest.from_dict(_read_json_object(manifest_path))
        projected_state = _read_json_object(state_path)
        cls._validate_state(projected_state, manifest)
        recovered_state = cls._recover_state_from_events(events_path, manifest)
        projected_sequence = projected_state.get("sequence")
        recovered_sequence = recovered_state.get("sequence")
        if not isinstance(projected_sequence, int) or projected_sequence > recovered_sequence:
            raise CampaignCorruptionError("atomic campaign state is ahead of its event log")
        if projected_sequence == recovered_sequence and projected_state != recovered_state:
            raise CampaignCorruptionError("atomic campaign state disagrees with its event log")

        cls._validate_pending_artifacts(root, recovered_state)
        cls._validate_evidence_artifacts(root, recovered_state)
        cls._validate_bound_authorization_artifacts(root, recovered_state)
        return cls(root, manifest, copy.deepcopy(recovered_state))

    @classmethod
    def _recover_state_from_events(
        cls,
        events_path: Path,
        manifest: CampaignManifest,
    ) -> dict[str, Any]:
        events = _read_event_log(events_path)
        if not events:
            raise CampaignCorruptionError("campaign event log is empty")
        previous_sequence = -1
        recovered_state: dict[str, Any] | None = None
        for event in events:
            if set(event) != {
                "schema_version",
                "sequence",
                "timestamp",
                "event_type",
                "canonical_job_id",
                "artifact_id",
                "reason_code",
                "state_sha256",
                "state_after",
            }:
                raise CampaignCorruptionError("campaign event fields differ from schema")
            if event.get("schema_version") != EVENT_SCHEMA_VERSION:
                raise CampaignCorruptionError("campaign event schema is invalid")
            sequence = event.get("sequence")
            if not isinstance(sequence, int) or sequence != previous_sequence + 1:
                raise CampaignCorruptionError("campaign event sequence is not contiguous")
            state_after = event.get("state_after")
            if not isinstance(state_after, dict) or state_after.get("sequence") != sequence:
                raise CampaignCorruptionError("campaign event projection is invalid")
            cls._validate_state(state_after, manifest)
            try:
                _require_aware_iso(str(event.get("timestamp") or ""), field="event timestamp")
                _require_reason_code(str(event.get("event_type") or ""), required=True)
                _require_reason_code(str(event.get("reason_code") or ""))
                if event.get("canonical_job_id"):
                    _require_canonical_job_id(str(event["canonical_job_id"]))
                if event.get("artifact_id"):
                    _require_safe_id(str(event["artifact_id"]), field="artifact_id")
            except ValueError as exc:
                raise CampaignCorruptionError("campaign event metadata is invalid") from exc
            if event["timestamp"] != state_after["updated_at"]:
                raise CampaignCorruptionError("campaign event timestamp differs from projection")
            if event.get("state_sha256") != _digest_json(state_after):
                raise CampaignCorruptionError("campaign event state digest mismatch")
            recovered_state = state_after
            previous_sequence = sequence
        assert recovered_state is not None
        return recovered_state

    @property
    def confirmed_count(self) -> int:
        return int(self._state["confirmed_count"])

    @property
    def status(self) -> CampaignStatus:
        return CampaignStatus(self._state["status"])

    @property
    def target_reached(self) -> bool:
        return self.status is CampaignStatus.TARGET_REACHED

    def snapshot(self) -> dict[str, Any]:
        """Return a defensive copy of the full, non-artifact state projection."""
        return copy.deepcopy(self._state)

    def candidate(self, canonical_job_id: str) -> dict[str, Any] | None:
        canonical_job_id = _require_canonical_job_id(canonical_job_id)
        record = self._state["candidates"].get(canonical_job_id)
        return copy.deepcopy(record) if record is not None else None

    def acquire_lease(self, owner_id: str) -> CampaignLease:
        """Acquire the one non-blocking writer lease for this campaign."""
        _require_safe_id(owner_id, field="owner_id")
        if self._active_lease is not None and self._active_lease.active:
            raise CampaignLeaseError("this store already holds the campaign lease")
        lease_path = self.root / self.LEASE_NAME
        descriptor = os.open(lease_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise CampaignLeaseError("campaign writer lease is already held") from exc

        try:
            latest = type(self).open(self.root)
            if latest.manifest != self.manifest:
                raise CampaignCorruptionError("campaign manifest changed before lease acquisition")
            self._state = latest.snapshot()
        except Exception:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise

        token = secrets.token_hex(16)
        metadata = _pretty_json_bytes(
            {
                "schema_version": CAMPAIGN_SCHEMA_VERSION,
                "campaign_id": self.manifest.campaign_id,
                "owner_id": owner_id,
                "token_sha256": _sha256(token.encode("utf-8")),
                "acquired_at": _iso_now(),
                "pid": os.getpid(),
            }
        )
        try:
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            _write_all(descriptor, metadata)
            os.fsync(descriptor)
        except Exception:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise
        lease = CampaignLease(self, descriptor=descriptor, owner_id=owner_id, token=token)
        self._active_lease = lease
        try:
            self._finish_pending_artifact_cleanup()
        except Exception:
            lease.release()
            raise
        return lease

    def register_candidate(self, canonical_job_id: str, *, now: datetime | None = None) -> bool:
        """Register one canonical job id; return ``False`` for an existing id."""
        self._require_lease()
        self._require_active_campaign()
        canonical_job_id = _require_canonical_job_id(canonical_job_id)
        if canonical_job_id in self._state["candidates"]:
            return False
        timestamp = _iso_now(now)
        state = self.snapshot()
        state["candidates"][canonical_job_id] = {
            "canonical_job_id": canonical_job_id,
            "state": CandidateState.DISCOVERED,
            "bindings": None,
            "confirmation": None,
            "submission_attempt_started_at": None,
            "outcome_evidence_artifact_id": None,
            "outcome_evidence_sha256": None,
            "last_reason_code": "",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self._commit(
            state,
            event_type="candidate_registered",
            canonical_job_id=canonical_job_id,
            timestamp=timestamp,
        )
        return True

    def transition_candidate(
        self,
        canonical_job_id: str,
        to_state: CandidateState | str,
        *,
        bindings: SubmissionBindings | None = None,
        reason_code: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Apply one exact, allowed transition that is not a confirmation."""
        self._require_lease()
        self._require_active_campaign()
        canonical_job_id = _require_canonical_job_id(canonical_job_id)
        try:
            desired = CandidateState(to_state)
        except ValueError as exc:
            raise ValueError(f"unsupported candidate state: {to_state}") from exc
        if desired in {
            CandidateState.SUBMITTED_CONFIRMED,
            CandidateState.NOT_SUBMITTED,
            CandidateState.OUTCOME_UNKNOWN,
        }:
            raise CampaignError(f"{desired} requires its dedicated evidence operation")
        reason_code = _require_reason_code(reason_code)
        current = self._candidate_or_raise(canonical_job_id)
        current_state = CandidateState(current["state"])
        if desired not in _ALLOWED_TRANSITIONS[current_state]:
            raise CampaignError(f"invalid candidate transition: {current_state}->{desired}")
        if desired in {CandidateState.AUTHORIZED, CandidateState.SUBMITTING}:
            if not self.manifest.submit_authorized:
                raise CampaignError("campaign manifest does not authorize submission")
            transition_time = _aware_datetime(now)
            if transition_time > datetime.fromisoformat(self.manifest.fact_approval_expires_at):
                raise CampaignError("signed applicant fact approval has expired")
        else:
            transition_time = _aware_datetime(now)

        bound = current.get("bindings")
        if bindings is not None:
            if desired is not CandidateState.AUTHORIZED:
                raise CampaignError("submission bindings may only be attached at authorization")
            candidate_bindings = asdict(bindings)
            self._validate_bindings(candidate_bindings)
            if bound is not None and bound != candidate_bindings:
                raise CampaignError("candidate submission bindings are immutable")
            bound = candidate_bindings
        if desired in {CandidateState.AUTHORIZED, CandidateState.SUBMITTING} and bound is None:
            raise CampaignError("submission bindings are required before authorization")
        if desired in {CandidateState.AUTHORIZED, CandidateState.SUBMITTING}:
            self._require_valid_authorization_grant(
                canonical_job_id=canonical_job_id,
                bindings=bound,
                now=transition_time,
            )
        if desired is CandidateState.SUBMITTING:
            in_flight = [
                candidate_id
                for candidate_id, record in self._state["candidates"].items()
                if record["state"] == CandidateState.SUBMITTING and candidate_id != canonical_job_id
            ]
            if in_flight:
                raise CampaignError("another candidate already has an uncertain submission in flight")

        timestamp = transition_time.isoformat()
        state = self.snapshot()
        updated = state["candidates"][canonical_job_id]
        updated["state"] = desired
        updated["bindings"] = bound
        if desired is CandidateState.SUBMITTING:
            updated["submission_attempt_started_at"] = timestamp
        updated["last_reason_code"] = reason_code
        updated["updated_at"] = timestamp
        self._commit(
            state,
            event_type="candidate_transitioned",
            canonical_job_id=canonical_job_id,
            reason_code=reason_code,
            timestamp=timestamp,
        )
        return copy.deepcopy(updated)

    def record_not_submitted(
        self,
        canonical_job_id: str,
        *,
        outcome_evidence_artifact_id: str,
        reason_code: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Resolve a submit attempt as not submitted without incrementing the target."""
        self._require_lease()
        canonical_job_id = _require_canonical_job_id(canonical_job_id)
        evidence = self._require_evidence_reference(
            artifact_id=outcome_evidence_artifact_id,
            expected_kind=EvidenceKind.NOT_SUBMITTED_EVIDENCE,
            canonical_job_id=canonical_job_id,
        )
        reason_code = _require_reason_code(reason_code, required=True)
        current = self._candidate_or_raise(canonical_job_id)
        current_state = CandidateState(current["state"])
        if current_state is CandidateState.SUBMITTING:
            self._require_active_campaign()
        elif current_state is not CandidateState.OUTCOME_UNKNOWN:
            raise CampaignError(
                "not_submitted evidence requires a submitting or outcome_unknown candidate"
            )

        timestamp = _iso_now(now)
        attempt_started_at = str(current.get("submission_attempt_started_at") or "")
        if not attempt_started_at or datetime.fromisoformat(timestamp) < datetime.fromisoformat(
            attempt_started_at
        ):
            raise CampaignError("not_submitted evidence predates the submission attempt")
        state = self.snapshot()
        updated = state["candidates"][canonical_job_id]
        updated["state"] = CandidateState.NOT_SUBMITTED
        updated["outcome_evidence_artifact_id"] = outcome_evidence_artifact_id
        updated["outcome_evidence_sha256"] = evidence["sha256"]
        updated["last_reason_code"] = reason_code
        updated["updated_at"] = timestamp
        state["status"] = CampaignStatus.ACTIVE
        self._commit(
            state,
            event_type="submission_not_submitted",
            canonical_job_id=canonical_job_id,
            reason_code=reason_code,
            timestamp=timestamp,
        )
        return copy.deepcopy(updated)

    def record_outcome_unknown(
        self,
        canonical_job_id: str,
        *,
        reason_code: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Fail closed after an ambiguous submission attempt without incrementing."""
        self._require_lease()
        self._require_active_campaign()
        canonical_job_id = _require_canonical_job_id(canonical_job_id)
        reason_code = _require_reason_code(reason_code, required=True)
        current = self._candidate_or_raise(canonical_job_id)
        if current["state"] != CandidateState.SUBMITTING:
            raise CampaignError("outcome_unknown requires a submitting candidate")
        timestamp = _iso_now(now)
        state = self.snapshot()
        updated = state["candidates"][canonical_job_id]
        updated["state"] = CandidateState.OUTCOME_UNKNOWN
        updated["last_reason_code"] = reason_code
        updated["updated_at"] = timestamp
        state["status"] = CampaignStatus.OUTCOME_REVIEW_REQUIRED
        self._commit(
            state,
            event_type="submission_outcome_unknown",
            canonical_job_id=canonical_job_id,
            reason_code=reason_code,
            timestamp=timestamp,
        )
        return copy.deepcopy(updated)

    def confirm_submission(
        self,
        confirmation: SubmissionConfirmation,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Count exactly one evidence-bound, positively confirmed submission."""
        self._require_lease()
        canonical_job_id = confirmation.canonical_job_id
        current = self._candidate_or_raise(canonical_job_id)
        if current["state"] == CandidateState.SUBMITTED_CONFIRMED:
            if current.get("confirmation") == confirmation.to_dict():
                return copy.deepcopy(current)
            raise CampaignError("candidate already has different confirmation evidence")
        if current["state"] != CandidateState.OUTCOME_UNKNOWN:
            self._require_active_campaign()
        if current["state"] not in {
            CandidateState.SUBMITTING,
            CandidateState.OUTCOME_UNKNOWN,
        }:
            raise CampaignError("confirmation requires a submitting or outcome_unknown candidate")
        bindings = current.get("bindings")
        if not isinstance(bindings, dict):
            raise CampaignError("candidate has no submission bindings")
        expected = {
            "fact_digest": confirmation.fact_digest,
            "context_digest": confirmation.context_digest,
            "policy_digest": confirmation.policy_digest,
            "fact_approval_receipt_sha256": confirmation.fact_approval_receipt_sha256,
            "submission_policy_digest": confirmation.submission_policy_digest,
            "packet_digest": confirmation.packet_digest,
            "form_review_digest": confirmation.form_review_digest,
            "authorization_artifact_id": confirmation.authorization_artifact_id,
            "authorization_grant_sha256": confirmation.authorization_grant_sha256,
        }
        if bindings != expected:
            raise CampaignError("confirmation evidence does not match candidate bindings")
        self._validate_bindings(bindings)
        evidence_references = (
            (
                confirmation.authorization_artifact_id,
                EvidenceKind.AUTHORIZATION_GRANT,
                confirmation.authorization_grant_sha256,
            ),
            (
                confirmation.authorization_consumption_artifact_id,
                EvidenceKind.AUTHORIZATION_CONSUMPTION,
                confirmation.authorization_consumption_sha256,
            ),
            (
                confirmation.submission_response_artifact_id,
                EvidenceKind.SUBMISSION_RESPONSE,
                confirmation.submission_response_sha256,
            ),
            (
                confirmation.confirmation_evidence_artifact_id,
                EvidenceKind.CONFIRMATION_EVIDENCE,
                confirmation.confirmation_evidence_sha256,
            ),
            (
                confirmation.controller_result_artifact_id,
                EvidenceKind.CONTROLLER_RESULT,
                confirmation.controller_result_sha256,
            ),
            (
                confirmation.jobs_row_artifact_id,
                EvidenceKind.JOBS_ROW,
                confirmation.jobs_row_sha256,
            ),
        )
        for artifact_id, kind, digest in evidence_references:
            self._require_evidence_reference(
                artifact_id=artifact_id,
                expected_kind=kind,
                canonical_job_id=canonical_job_id,
                expected_sha256=digest,
            )
        submitted_at = datetime.fromisoformat(confirmation.submitted_at)
        attempt_started_at = str(current.get("submission_attempt_started_at") or "")
        if not attempt_started_at:
            raise CampaignError("candidate has no recorded submission attempt")
        candidate_started_at = datetime.fromisoformat(attempt_started_at)
        current_time = _aware_datetime(now)
        if submitted_at < candidate_started_at or submitted_at > current_time:
            raise CampaignError("confirmation timestamp is outside the submission attempt")
        if submitted_at > datetime.fromisoformat(self.manifest.fact_approval_expires_at):
            raise CampaignError("submission occurred after applicant fact approval expired")
        self._require_valid_authorization_grant(
            canonical_job_id=canonical_job_id,
            bindings=bindings,
            now=submitted_at,
        )
        consumption_time = _parse_consumption_timestamp(
            self.read_evidence_artifact(confirmation.authorization_consumption_artifact_id)
        )
        if not candidate_started_at <= consumption_time <= submitted_at:
            raise CampaignError("authorization consumption is outside the submission attempt")

        timestamp = current_time.isoformat()
        state = self.snapshot()
        updated = state["candidates"][canonical_job_id]
        updated["state"] = CandidateState.SUBMITTED_CONFIRMED
        updated["confirmation"] = confirmation.to_dict()
        updated["last_reason_code"] = "submitted_confirmed"
        updated["updated_at"] = timestamp
        state["confirmed_count"] = sum(
            record["state"] == CandidateState.SUBMITTED_CONFIRMED
            for record in state["candidates"].values()
        )
        state["status"] = CampaignStatus.ACTIVE
        if state["confirmed_count"] > self.manifest.target_confirmed:
            raise CampaignTargetReached("confirmation would exceed campaign target")
        if state["confirmed_count"] == self.manifest.target_confirmed:
            state["status"] = CampaignStatus.TARGET_REACHED
        self._commit(
            state,
            event_type="submission_confirmed",
            canonical_job_id=canonical_job_id,
            reason_code="submitted_confirmed",
            timestamp=timestamp,
        )
        return copy.deepcopy(updated)

    def persist_evidence_artifact(
        self,
        artifact_id: str,
        *,
        kind: EvidenceKind | str,
        data: bytes,
        canonical_job_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist exact immutable bytes before they can support campaign accounting."""
        self._require_lease()
        artifact_id = _require_safe_id(artifact_id, field="artifact_id")
        try:
            evidence_kind = EvidenceKind(kind)
        except ValueError as exc:
            raise ValueError(f"unsupported campaign evidence kind: {kind}") from exc
        canonical_job_id = _require_canonical_job_id(canonical_job_id)
        self._candidate_or_raise(canonical_job_id)
        if not isinstance(data, bytes) or not data:
            raise ValueError("campaign evidence must contain non-empty bytes")
        if len(data) > MAX_EVIDENCE_ARTIFACT_BYTES:
            raise ValueError("campaign evidence exceeds the size limit")

        digest = _sha256(data)
        relative_path = f"{self.EVIDENCE_DIR}/{artifact_id}.bin"
        artifact_path = self.root / relative_path
        _write_immutable_bytes(artifact_path, data)
        existing = self._state["evidence_artifacts"].get(artifact_id)
        if existing is not None:
            expected = {
                "kind": evidence_kind,
                "canonical_job_id": canonical_job_id,
                "sha256": digest,
            }
            if any(existing.get(key) != value for key, value in expected.items()):
                raise FileExistsError("evidence artifact id already has different bindings or content")
            return copy.deepcopy(existing)

        timestamp = _iso_now(now)
        metadata = {
            "artifact_id": artifact_id,
            "kind": evidence_kind,
            "canonical_job_id": canonical_job_id,
            "relative_path": relative_path,
            "sha256": digest,
            "size_bytes": len(data),
            "created_at": timestamp,
        }
        state = self.snapshot()
        state["evidence_artifacts"][artifact_id] = metadata
        self._commit(
            state,
            event_type="evidence_artifact_persisted",
            canonical_job_id=canonical_job_id,
            artifact_id=artifact_id,
            reason_code=evidence_kind,
            timestamp=timestamp,
        )
        return copy.deepcopy(metadata)

    def read_evidence_artifact(self, artifact_id: str) -> bytes:
        artifact_id = _require_safe_id(artifact_id, field="artifact_id")
        metadata = self._state["evidence_artifacts"].get(artifact_id)
        if not isinstance(metadata, dict):
            raise KeyError(f"evidence artifact not found: {artifact_id}")
        path = self.root / metadata["relative_path"]
        data = path.read_bytes()
        if len(data) != metadata["size_bytes"] or _sha256(data) != metadata["sha256"]:
            raise CampaignCorruptionError("campaign evidence artifact changed")
        return data

    def persist_pending_artifact(
        self,
        artifact_id: str,
        *,
        kind: str,
        payload: Mapping[str, Any],
        canonical_job_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist one immutable JSON artifact and reference only its metadata."""
        self._require_lease()
        artifact_id = _require_safe_id(artifact_id, field="artifact_id")
        kind = _require_reason_code(kind, required=True)
        if canonical_job_id is not None:
            canonical_job_id = _require_canonical_job_id(canonical_job_id)
            self._candidate_or_raise(canonical_job_id)
        artifact_bytes = _pretty_json_bytes(dict(payload))
        digest = _sha256(artifact_bytes)
        relative_path = f"{self.PENDING_DIR}/{artifact_id}.json"
        artifact_path = self.root / relative_path
        _write_immutable_bytes(artifact_path, artifact_bytes)

        existing = self._state["pending_artifacts"].get(artifact_id)
        if existing is not None:
            expected_existing = {
                "sha256": digest,
                "kind": kind,
                "canonical_job_id": canonical_job_id,
            }
            if any(existing.get(key) != value for key, value in expected_existing.items()):
                raise FileExistsError("pending artifact id already has different bindings or content")
            return copy.deepcopy(existing)

        timestamp = _iso_now(now)
        metadata = {
            "artifact_id": artifact_id,
            "kind": kind,
            "canonical_job_id": canonical_job_id,
            "relative_path": relative_path,
            "sha256": digest,
            "size_bytes": len(artifact_bytes),
            "created_at": timestamp,
        }
        state = self.snapshot()
        state["pending_artifacts"][artifact_id] = metadata
        self._commit(
            state,
            event_type="pending_artifact_persisted",
            canonical_job_id=canonical_job_id or "",
            artifact_id=artifact_id,
            timestamp=timestamp,
        )
        return copy.deepcopy(metadata)

    def read_pending_artifact(self, artifact_id: str) -> dict[str, Any]:
        artifact_id = _require_safe_id(artifact_id, field="artifact_id")
        metadata = self._state["pending_artifacts"].get(artifact_id)
        if not isinstance(metadata, dict):
            raise KeyError(f"pending artifact not found: {artifact_id}")
        path = self.root / metadata["relative_path"]
        data = path.read_bytes()
        if _sha256(data) != metadata["sha256"]:
            raise CampaignCorruptionError("pending artifact digest mismatch")
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise CampaignCorruptionError("pending artifact is not a JSON object")
        return payload

    def resolve_pending_artifact(
        self,
        artifact_id: str,
        *,
        reason_code: str = "artifact_consumed",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Resolve a pending reference and remove its raw private payload."""
        self._require_lease()
        artifact_id = _require_safe_id(artifact_id, field="artifact_id")
        reason_code = _require_reason_code(reason_code, required=True)
        metadata = self._state["pending_artifacts"].get(artifact_id)
        if not isinstance(metadata, dict):
            raise KeyError(f"pending artifact not found: {artifact_id}")
        timestamp = _iso_now(now)
        state = self.snapshot()
        state["resolved_pending_artifacts"][artifact_id] = state["pending_artifacts"].pop(
            artifact_id
        )
        self._commit(
            state,
            event_type="pending_artifact_resolution_started",
            canonical_job_id=str(metadata.get("canonical_job_id") or ""),
            artifact_id=artifact_id,
            reason_code=reason_code,
            timestamp=timestamp,
        )
        self._finish_pending_artifact_cleanup()
        return copy.deepcopy(metadata)

    def _finish_pending_artifact_cleanup(self) -> None:
        self._require_lease()
        resolved = self._state["resolved_pending_artifacts"]
        if not resolved:
            return
        for metadata in resolved.values():
            artifact_path = self.root / str(metadata["relative_path"])
            _delete_file_and_fsync(artifact_path)
        state = self.snapshot()
        state["resolved_pending_artifacts"] = {}
        cleanup_time = max(
            _aware_datetime(),
            datetime.fromisoformat(str(self._state["updated_at"])),
        )
        self._commit(
            state,
            event_type="pending_artifact_cleanup_completed",
            reason_code="private_payload_deleted",
            timestamp=_iso_now(cleanup_time),
        )

    def heartbeat_snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Return a bounded progress view containing no raw user or model values."""
        current = _aware_datetime(now)
        from applypilot.autonomy.supervisor import runtime_observation_snapshot

        runtime_status = runtime_observation_snapshot(
            root=self.root,
            scope_kind="campaign",
            scope_id=self.manifest.campaign_id,
            now=current,
        )
        last_heartbeat = self._read_last_heartbeat_at()
        due = True
        if isinstance(last_heartbeat, str):
            last = datetime.fromisoformat(last_heartbeat)
            due = current >= last + timedelta(seconds=self.manifest.heartbeat_interval_seconds)
        counts = Counter(record["state"] for record in self._state["candidates"].values())
        pending_counts = Counter(
            str(metadata["kind"])
            for metadata in self._state["pending_artifacts"].values()
        )
        evidence_counts = Counter(
            str(metadata["kind"])
            for metadata in self._state["evidence_artifacts"].values()
        )
        blocker_codes = sorted(
            {
                str(record["last_reason_code"])
                for record in self._state["candidates"].values()
                if record["state"] in {
                    CandidateState.BLOCKED,
                    CandidateState.OUTCOME_UNKNOWN,
                }
                and record["last_reason_code"]
            }
        )
        return {
            "schema_version": HEARTBEAT_SCHEMA_VERSION,
            "campaign_id": self.manifest.campaign_id,
            "campaign_status": self._state["status"],
            "target_confirmed": self.manifest.target_confirmed,
            "submitted_confirmed": self.confirmed_count,
            "remaining": max(0, self.manifest.target_confirmed - self.confirmed_count),
            "candidate_state_counts": {
                state.value: counts.get(state.value, 0) for state in CandidateState
            },
            "pending_artifact_count": len(self._state["pending_artifacts"]),
            "pending_artifact_counts_by_kind": dict(sorted(pending_counts.items())),
            "pending_artifact_cleanup_count": len(
                self._state["resolved_pending_artifacts"]
            ),
            "evidence_artifact_count": len(self._state["evidence_artifacts"]),
            "evidence_artifact_counts_by_kind": dict(sorted(evidence_counts.items())),
            "blocker_codes": blocker_codes,
            "sequence": self._state["sequence"],
            "heartbeat_interval_seconds": self.manifest.heartbeat_interval_seconds,
            "last_heartbeat_at": last_heartbeat,
            "heartbeat_due": due,
            "runtime_ready": runtime_status["runtime_ready"],
            "runtime_observation_state": runtime_status["observation_state"],
            "chronicle_state": runtime_status["chronicle_state"],
            "browser_surface": runtime_status["browser_surface"],
            "browser_readiness": runtime_status["browser_readiness"],
            "runtime_observation": runtime_status,
        }

    def record_heartbeat(self, *, now: datetime | None = None) -> dict[str, Any]:
        self._require_lease()
        timestamp = _iso_now(now)
        _atomic_write_json(
            self.root / self.HEARTBEAT_NAME,
            {
                "schema_version": HEARTBEAT_RECORD_SCHEMA_VERSION,
                "campaign_id": self.manifest.campaign_id,
                "manifest_sha256": self.manifest.digest,
                "sequence": self._state["sequence"],
                "recorded_at": timestamp,
            },
        )
        return self.heartbeat_snapshot(now=datetime.fromisoformat(timestamp))

    def _read_last_heartbeat_at(self) -> str | None:
        path = self.root / self.HEARTBEAT_NAME
        if not path.exists():
            return None
        payload = _read_json_object(path)
        if set(payload) != {
            "schema_version",
            "campaign_id",
            "manifest_sha256",
            "sequence",
            "recorded_at",
        }:
            raise CampaignCorruptionError("campaign heartbeat fields differ from schema")
        if (
            payload.get("schema_version") != HEARTBEAT_RECORD_SCHEMA_VERSION
            or payload.get("campaign_id") != self.manifest.campaign_id
            or payload.get("manifest_sha256") != self.manifest.digest
            or not isinstance(payload.get("sequence"), int)
            or payload["sequence"] > self._state["sequence"]
        ):
            raise CampaignCorruptionError("campaign heartbeat bindings are invalid")
        try:
            _require_aware_iso(str(payload.get("recorded_at") or ""), field="heartbeat recorded_at")
        except ValueError as exc:
            raise CampaignCorruptionError("campaign heartbeat timestamp is invalid") from exc
        return str(payload["recorded_at"])

    def _release_lease(self, lease: CampaignLease) -> None:
        if self._active_lease is not lease:
            raise CampaignLeaseError("campaign lease does not belong to this store")
        try:
            fcntl.flock(lease._descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lease._descriptor)
            self._active_lease = None

    def _require_lease(self) -> CampaignLease:
        lease = self._active_lease
        if lease is None or not lease.active:
            raise CampaignLeaseError("campaign mutation requires the exclusive writer lease")
        return lease

    def _require_active_campaign(self) -> None:
        if self.target_reached:
            raise CampaignTargetReached("campaign confirmed target has been reached")
        if self.status is CampaignStatus.OUTCOME_REVIEW_REQUIRED:
            raise CampaignError("campaign is paused for an unknown submission outcome")

    def _candidate_or_raise(self, canonical_job_id: str) -> dict[str, Any]:
        candidate = self._state["candidates"].get(canonical_job_id)
        if not isinstance(candidate, dict):
            raise KeyError(f"candidate not found: {canonical_job_id}")
        return candidate

    def _validate_bindings(self, bindings: Mapping[str, Any]) -> None:
        expected_global = {
            "fact_digest": self.manifest.fact_digest,
            "context_digest": self.manifest.context_digest,
            "policy_digest": self.manifest.policy_digest,
            "fact_approval_receipt_sha256": self.manifest.fact_approval_receipt_sha256,
        }
        for field_name, expected in expected_global.items():
            if bindings.get(field_name) != expected:
                raise CampaignError(f"candidate {field_name} differs from campaign manifest")
        _require_safe_id(
            str(bindings.get("authorization_artifact_id") or ""),
            field="authorization_artifact_id",
        )
        for field_name in (
            "submission_policy_digest",
            "packet_digest",
            "form_review_digest",
            "authorization_grant_sha256",
        ):
            _require_sha256(str(bindings.get(field_name) or ""), field=field_name)

    def _require_evidence_reference(
        self,
        *,
        artifact_id: str,
        expected_kind: EvidenceKind,
        canonical_job_id: str,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        artifact_id = _require_safe_id(artifact_id, field="artifact_id")
        metadata = self._state["evidence_artifacts"].get(artifact_id)
        if not isinstance(metadata, dict):
            raise CampaignError(f"required evidence artifact is missing: {artifact_id}")
        if metadata.get("kind") != expected_kind:
            raise CampaignError(f"evidence artifact has wrong kind: {artifact_id}")
        if metadata.get("canonical_job_id") != canonical_job_id:
            raise CampaignError(f"evidence artifact has wrong candidate: {artifact_id}")
        if expected_sha256 is not None and metadata.get("sha256") != expected_sha256:
            raise CampaignError(f"evidence artifact digest mismatch: {artifact_id}")
        path = self.root / str(metadata.get("relative_path") or "")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CampaignCorruptionError("required evidence artifact cannot be read") from exc
        if len(data) != metadata.get("size_bytes") or _sha256(data) != metadata.get("sha256"):
            raise CampaignCorruptionError("required evidence artifact changed")
        return copy.deepcopy(metadata)

    def _require_valid_authorization_grant(
        self,
        *,
        canonical_job_id: str,
        bindings: Mapping[str, Any],
        now: datetime,
    ) -> None:
        artifact_id = str(bindings.get("authorization_artifact_id") or "")
        self._require_evidence_reference(
            artifact_id=artifact_id,
            expected_kind=EvidenceKind.AUTHORIZATION_GRANT,
            canonical_job_id=canonical_job_id,
            expected_sha256=str(bindings.get("authorization_grant_sha256") or ""),
        )
        data = self.read_evidence_artifact(artifact_id)
        try:
            _validate_submit_authorization_bytes(
                data,
                canonical_job_id=canonical_job_id,
                bindings=bindings,
                now=now,
            )
        except (TypeError, ValueError) as exc:
            raise CampaignError("candidate authorization grant is invalid or expired") from exc

    def _commit(
        self,
        state: dict[str, Any],
        *,
        event_type: str,
        canonical_job_id: str = "",
        artifact_id: str = "",
        reason_code: str = "",
        timestamp: str,
    ) -> None:
        self._require_lease()
        current_updated_at = datetime.fromisoformat(str(self._state["updated_at"]))
        next_updated_at = datetime.fromisoformat(timestamp)
        if next_updated_at < current_updated_at:
            raise CampaignError("campaign timestamps cannot move backwards")
        state["sequence"] = int(self._state["sequence"]) + 1
        state["updated_at"] = timestamp
        self._validate_state(state, self.manifest)
        event = self._event_for_state(
            state,
            event_type=event_type,
            canonical_job_id=canonical_job_id,
            artifact_id=artifact_id,
            reason_code=reason_code,
            timestamp=timestamp,
        )
        _append_fsynced_json_line(self.root / self.EVENTS_NAME, event)
        self._state = copy.deepcopy(state)
        _atomic_write_json(self.root / self.STATE_NAME, state)

    @staticmethod
    def _event_for_state(
        state: Mapping[str, Any],
        *,
        event_type: str,
        timestamp: str,
        canonical_job_id: str = "",
        artifact_id: str = "",
        reason_code: str = "",
    ) -> dict[str, Any]:
        _require_reason_code(event_type, required=True)
        return {
            "schema_version": EVENT_SCHEMA_VERSION,
            "sequence": state["sequence"],
            "timestamp": timestamp,
            "event_type": event_type,
            "canonical_job_id": canonical_job_id,
            "artifact_id": artifact_id,
            "reason_code": reason_code,
            "state_sha256": _digest_json(state),
            "state_after": copy.deepcopy(dict(state)),
        }

    @staticmethod
    def _validate_state(state: Mapping[str, Any], manifest: CampaignManifest) -> None:
        required = {
            "schema_version",
            "campaign_id",
            "manifest_sha256",
            "sequence",
            "status",
            "confirmed_count",
            "candidates",
            "pending_artifacts",
            "resolved_pending_artifacts",
            "evidence_artifacts",
            "updated_at",
        }
        if set(state) != required:
            raise CampaignCorruptionError("campaign state fields differ from schema")
        if state.get("schema_version") != CAMPAIGN_SCHEMA_VERSION:
            raise CampaignCorruptionError("campaign state schema is invalid")
        if state.get("campaign_id") != manifest.campaign_id:
            raise CampaignCorruptionError("campaign state id differs from manifest")
        if state.get("manifest_sha256") != manifest.digest:
            raise CampaignCorruptionError("campaign state manifest digest mismatch")
        sequence = state.get("sequence")
        if not isinstance(sequence, int) or sequence < 0:
            raise CampaignCorruptionError("campaign state sequence is invalid")
        candidates = state.get("candidates")
        pending = state.get("pending_artifacts")
        resolved_pending = state.get("resolved_pending_artifacts")
        evidence = state.get("evidence_artifacts")
        if (
            not isinstance(candidates, dict)
            or not isinstance(pending, dict)
            or not isinstance(resolved_pending, dict)
            or not isinstance(evidence, dict)
        ):
            raise CampaignCorruptionError("campaign state collections are invalid")

        confirmed = 0
        submitting = 0
        unknown_outcomes = 0
        for canonical_job_id, record in candidates.items():
            try:
                _require_canonical_job_id(canonical_job_id)
            except ValueError as exc:
                raise CampaignCorruptionError("candidate canonical job id is invalid") from exc
            if not isinstance(record, dict) or record.get("canonical_job_id") != canonical_job_id:
                raise CampaignCorruptionError("candidate record id mismatch")
            if set(record) != {
                "canonical_job_id",
                "state",
                "bindings",
                "confirmation",
                "submission_attempt_started_at",
                "outcome_evidence_artifact_id",
                "outcome_evidence_sha256",
                "last_reason_code",
                "created_at",
                "updated_at",
            }:
                raise CampaignCorruptionError("candidate record fields differ from schema")
            try:
                candidate_state = CandidateState(record.get("state"))
            except ValueError as exc:
                raise CampaignCorruptionError("candidate state is invalid") from exc
            try:
                _require_reason_code(str(record.get("last_reason_code") or ""))
                _require_aware_iso(str(record.get("created_at") or ""), field="candidate created_at")
                _require_aware_iso(str(record.get("updated_at") or ""), field="candidate updated_at")
            except ValueError as exc:
                raise CampaignCorruptionError("candidate metadata is invalid") from exc
            bindings = record.get("bindings")
            if bindings is not None:
                if not isinstance(bindings, dict) or set(bindings) != {
                    "fact_digest",
                    "context_digest",
                    "policy_digest",
                    "fact_approval_receipt_sha256",
                    "submission_policy_digest",
                    "packet_digest",
                    "form_review_digest",
                    "authorization_artifact_id",
                    "authorization_grant_sha256",
                }:
                    raise CampaignCorruptionError("candidate bindings are invalid")
                expected_global = {
                    "fact_digest": manifest.fact_digest,
                    "context_digest": manifest.context_digest,
                    "policy_digest": manifest.policy_digest,
                    "fact_approval_receipt_sha256": manifest.fact_approval_receipt_sha256,
                }
                if any(bindings.get(key) != value for key, value in expected_global.items()):
                    raise CampaignCorruptionError("candidate bindings differ from manifest")
                if (
                    not bindings.get("packet_digest")
                    or not bindings.get("form_review_digest")
                    or not bindings.get("authorization_artifact_id")
                ):
                    raise CampaignCorruptionError("candidate bindings are incomplete")
                try:
                    _require_safe_id(
                        str(bindings["authorization_artifact_id"]),
                        field="authorization_artifact_id",
                    )
                    for field_name, digest in bindings.items():
                        if field_name == "authorization_artifact_id":
                            continue
                        _require_sha256(str(digest), field=field_name)
                except ValueError as exc:
                    raise CampaignCorruptionError("candidate binding digest is invalid") from exc
                authorization_metadata = evidence.get(str(bindings["authorization_artifact_id"]))
                if (
                    not isinstance(authorization_metadata, dict)
                    or authorization_metadata.get("kind") != EvidenceKind.AUTHORIZATION_GRANT
                    or authorization_metadata.get("canonical_job_id") != canonical_job_id
                    or authorization_metadata.get("sha256")
                    != bindings["authorization_grant_sha256"]
                ):
                    raise CampaignCorruptionError("candidate authorization evidence is invalid")
            if candidate_state in {
                CandidateState.AUTHORIZED,
                CandidateState.SUBMITTING,
                CandidateState.SUBMITTED_CONFIRMED,
                CandidateState.NOT_SUBMITTED,
                CandidateState.OUTCOME_UNKNOWN,
            } and bindings is None:
                raise CampaignCorruptionError("submission state lacks candidate bindings")
            if candidate_state in {
                CandidateState.AUTHORIZED,
                CandidateState.SUBMITTING,
                CandidateState.SUBMITTED_CONFIRMED,
                CandidateState.NOT_SUBMITTED,
                CandidateState.OUTCOME_UNKNOWN,
            } and not manifest.submit_authorized:
                raise CampaignCorruptionError("submission state lacks campaign authorization")
            attempt_started_at = record.get("submission_attempt_started_at")
            attempt_states = {
                CandidateState.SUBMITTING,
                CandidateState.SUBMITTED_CONFIRMED,
                CandidateState.NOT_SUBMITTED,
                CandidateState.OUTCOME_UNKNOWN,
            }
            if candidate_state in attempt_states:
                if not isinstance(attempt_started_at, str):
                    raise CampaignCorruptionError("submission state lacks attempt timestamp")
                try:
                    _require_aware_iso(attempt_started_at, field="submission attempt")
                except ValueError as exc:
                    raise CampaignCorruptionError("submission attempt timestamp is invalid") from exc
            elif attempt_started_at is not None:
                raise CampaignCorruptionError("pre-submission state has an attempt timestamp")
            outcome_evidence = record.get("outcome_evidence_sha256")
            outcome_evidence_artifact_id = record.get("outcome_evidence_artifact_id")
            if candidate_state is CandidateState.NOT_SUBMITTED:
                try:
                    _require_safe_id(
                        str(outcome_evidence_artifact_id or ""),
                        field="outcome_evidence_artifact_id",
                    )
                    _require_sha256(str(outcome_evidence or ""), field="outcome_evidence_sha256")
                except ValueError as exc:
                    raise CampaignCorruptionError("not_submitted candidate lacks evidence") from exc
                metadata = evidence.get(str(outcome_evidence_artifact_id))
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("kind") != EvidenceKind.NOT_SUBMITTED_EVIDENCE
                    or metadata.get("canonical_job_id") != canonical_job_id
                    or metadata.get("sha256") != outcome_evidence
                ):
                    raise CampaignCorruptionError("not_submitted evidence reference is invalid")
            elif outcome_evidence is not None or outcome_evidence_artifact_id is not None:
                raise CampaignCorruptionError("candidate has unexpected not_submitted evidence")
            if candidate_state is CandidateState.SUBMITTING:
                submitting += 1
            if candidate_state is CandidateState.OUTCOME_UNKNOWN:
                unknown_outcomes += 1
            if candidate_state is CandidateState.SUBMITTED_CONFIRMED:
                confirmed += 1
                confirmation = record.get("confirmation")
                if not isinstance(confirmation, dict) or not isinstance(bindings, dict):
                    raise CampaignCorruptionError("confirmed candidate lacks bound evidence")
                matching = {
                    key: confirmation.get(key)
                    for key in (
                        "fact_digest",
                        "context_digest",
                        "policy_digest",
                        "fact_approval_receipt_sha256",
                        "submission_policy_digest",
                        "packet_digest",
                        "form_review_digest",
                        "authorization_artifact_id",
                        "authorization_grant_sha256",
                    )
                }
                if matching != bindings or confirmation.get("canonical_job_id") != canonical_job_id:
                    raise CampaignCorruptionError("confirmed candidate evidence mismatch")
                try:
                    SubmissionConfirmation(**confirmation)
                except (TypeError, ValueError) as exc:
                    raise CampaignCorruptionError("confirmed candidate evidence is invalid") from exc
                confirmation_references = (
                    (
                        "authorization_consumption_artifact_id",
                        "authorization_consumption_sha256",
                        EvidenceKind.AUTHORIZATION_CONSUMPTION,
                    ),
                    (
                        "submission_response_artifact_id",
                        "submission_response_sha256",
                        EvidenceKind.SUBMISSION_RESPONSE,
                    ),
                    (
                        "confirmation_evidence_artifact_id",
                        "confirmation_evidence_sha256",
                        EvidenceKind.CONFIRMATION_EVIDENCE,
                    ),
                    (
                        "controller_result_artifact_id",
                        "controller_result_sha256",
                        EvidenceKind.CONTROLLER_RESULT,
                    ),
                    ("jobs_row_artifact_id", "jobs_row_sha256", EvidenceKind.JOBS_ROW),
                )
                for id_field, digest_field, expected_kind in confirmation_references:
                    metadata = evidence.get(str(confirmation.get(id_field) or ""))
                    if (
                        not isinstance(metadata, dict)
                        or metadata.get("kind") != expected_kind
                        or metadata.get("canonical_job_id") != canonical_job_id
                        or metadata.get("sha256") != confirmation.get(digest_field)
                    ):
                        raise CampaignCorruptionError(
                            "confirmed candidate references invalid durable evidence"
                        )
            elif record.get("confirmation") is not None:
                raise CampaignCorruptionError("unconfirmed candidate has confirmation evidence")
        if submitting > 1:
            raise CampaignCorruptionError("multiple candidates are in submitting state")
        if unknown_outcomes > 1:
            raise CampaignCorruptionError("multiple candidates have unknown submission outcomes")
        if state.get("confirmed_count") != confirmed:
            raise CampaignCorruptionError("campaign confirmed count differs from evidence")
        if confirmed > manifest.target_confirmed:
            raise CampaignCorruptionError("campaign confirmed count exceeds target")
        expected_status = (
            CampaignStatus.TARGET_REACHED
            if confirmed == manifest.target_confirmed
            else CampaignStatus.OUTCOME_REVIEW_REQUIRED
            if unknown_outcomes
            else CampaignStatus.ACTIVE
        )
        if state.get("status") != expected_status:
            raise CampaignCorruptionError("campaign status differs from confirmed count")
        try:
            _require_aware_iso(str(state.get("updated_at") or ""), field="updated_at")
        except ValueError as exc:
            raise CampaignCorruptionError("campaign update timestamp is invalid") from exc
        for collection in (pending, resolved_pending):
            for artifact_id, metadata in collection.items():
                try:
                    _require_safe_id(artifact_id, field="artifact_id")
                except ValueError as exc:
                    raise CampaignCorruptionError("pending artifact id is invalid") from exc
                if not isinstance(metadata, dict) or metadata.get("artifact_id") != artifact_id:
                    raise CampaignCorruptionError("pending artifact metadata is invalid")
                if set(metadata) != {
                    "artifact_id",
                    "kind",
                    "canonical_job_id",
                    "relative_path",
                    "sha256",
                    "size_bytes",
                    "created_at",
                }:
                    raise CampaignCorruptionError("pending artifact fields differ from schema")
                if not _SHA256.fullmatch(str(metadata.get("sha256") or "")):
                    raise CampaignCorruptionError("pending artifact digest is invalid")
                try:
                    _require_reason_code(str(metadata.get("kind") or ""), required=True)
                    _require_aware_iso(
                        str(metadata.get("created_at") or ""),
                        field="artifact created_at",
                    )
                except ValueError as exc:
                    raise CampaignCorruptionError("pending artifact metadata is invalid") from exc
                artifact_candidate_id = metadata.get("canonical_job_id")
                if artifact_candidate_id is not None:
                    if artifact_candidate_id not in candidates:
                        raise CampaignCorruptionError(
                            "pending artifact candidate is not registered"
                        )
                    try:
                        _require_canonical_job_id(str(artifact_candidate_id))
                    except ValueError as exc:
                        raise CampaignCorruptionError(
                            "pending artifact candidate id is invalid"
                        ) from exc
                if not isinstance(metadata.get("size_bytes"), int) or metadata["size_bytes"] < 0:
                    raise CampaignCorruptionError("pending artifact size is invalid")
                relative_path = str(metadata.get("relative_path") or "")
                if relative_path != f"{CampaignStore.PENDING_DIR}/{artifact_id}.json":
                    raise CampaignCorruptionError("pending artifact path is invalid")

        for artifact_id, metadata in evidence.items():
            try:
                _require_safe_id(artifact_id, field="artifact_id")
            except ValueError as exc:
                raise CampaignCorruptionError("evidence artifact id is invalid") from exc
            if not isinstance(metadata, dict) or metadata.get("artifact_id") != artifact_id:
                raise CampaignCorruptionError("evidence artifact metadata is invalid")
            if set(metadata) != {
                "artifact_id",
                "kind",
                "canonical_job_id",
                "relative_path",
                "sha256",
                "size_bytes",
                "created_at",
            }:
                raise CampaignCorruptionError("evidence artifact fields differ from schema")
            try:
                EvidenceKind(metadata.get("kind"))
                _require_canonical_job_id(str(metadata.get("canonical_job_id") or ""))
                _require_sha256(str(metadata.get("sha256") or ""), field="evidence sha256")
                _require_aware_iso(str(metadata.get("created_at") or ""), field="evidence created_at")
            except (TypeError, ValueError) as exc:
                raise CampaignCorruptionError("evidence artifact metadata is invalid") from exc
            if metadata["canonical_job_id"] not in candidates:
                raise CampaignCorruptionError("evidence artifact candidate is not registered")
            if not isinstance(metadata.get("size_bytes"), int) or not (
                0 < metadata["size_bytes"] <= MAX_EVIDENCE_ARTIFACT_BYTES
            ):
                raise CampaignCorruptionError("evidence artifact size is invalid")
            if metadata.get("relative_path") != (
                f"{CampaignStore.EVIDENCE_DIR}/{artifact_id}.bin"
            ):
                raise CampaignCorruptionError("evidence artifact path is invalid")

    @staticmethod
    def _validate_pending_artifacts(root: Path, state: Mapping[str, Any]) -> None:
        for metadata in state["pending_artifacts"].values():
            path = root / metadata["relative_path"]
            if not path.is_file():
                raise CampaignCorruptionError("pending artifact file is missing")
            data = path.read_bytes()
            if len(data) != metadata["size_bytes"]:
                raise CampaignCorruptionError("pending artifact size mismatch")
            if _sha256(data) != metadata["sha256"]:
                raise CampaignCorruptionError("pending artifact digest mismatch")
        for metadata in state["resolved_pending_artifacts"].values():
            path = root / metadata["relative_path"]
            if not path.exists():
                continue
            data = path.read_bytes()
            if len(data) != metadata["size_bytes"] or _sha256(data) != metadata["sha256"]:
                raise CampaignCorruptionError("resolved pending artifact changed before cleanup")

    @staticmethod
    def _validate_evidence_artifacts(root: Path, state: Mapping[str, Any]) -> None:
        for metadata in state["evidence_artifacts"].values():
            path = root / metadata["relative_path"]
            if not path.is_file():
                raise CampaignCorruptionError("evidence artifact file is missing")
            data = path.read_bytes()
            if len(data) != metadata["size_bytes"]:
                raise CampaignCorruptionError("evidence artifact size mismatch")
            if _sha256(data) != metadata["sha256"]:
                raise CampaignCorruptionError("evidence artifact digest mismatch")

    @staticmethod
    def _validate_bound_authorization_artifacts(root: Path, state: Mapping[str, Any]) -> None:
        for canonical_job_id, candidate in state["candidates"].items():
            bindings = candidate.get("bindings")
            if not isinstance(bindings, dict):
                continue
            metadata = state["evidence_artifacts"].get(
                str(bindings.get("authorization_artifact_id") or "")
            )
            if not isinstance(metadata, dict):
                raise CampaignCorruptionError("bound authorization artifact is missing")
            try:
                _validate_submit_authorization_bytes(
                    (root / metadata["relative_path"]).read_bytes(),
                    canonical_job_id=canonical_job_id,
                    bindings=bindings,
                    now=None,
                )
            except (OSError, TypeError, ValueError) as exc:
                raise CampaignCorruptionError("bound authorization grant is invalid") from exc


def _validate_submit_authorization_bytes(
    data: bytes,
    *,
    canonical_job_id: str,
    bindings: Mapping[str, Any],
    now: datetime | None,
) -> None:
    try:
        payload = json.loads(data, object_pairs_hook=_json_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("authorization grant is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "nonce",
        "candidate_id",
        "fact_digest",
        "material_digest",
        "form_review_digest",
        "policy_digest",
        "issued_at",
        "expires_at",
        "allowed_action",
    }:
        raise ValueError("authorization grant fields differ from schema")
    if not re.fullmatch(r"[0-9a-f]{32}", str(payload.get("nonce") or "")):
        raise ValueError("authorization grant nonce is invalid")
    expected = {
        "version": "applypilot-submit-authorization-v1",
        "candidate_id": canonical_job_id,
        "fact_digest": bindings.get("fact_digest"),
        "material_digest": bindings.get("packet_digest"),
        "form_review_digest": bindings.get("form_review_digest"),
        "policy_digest": bindings.get("submission_policy_digest"),
        "allowed_action": "submit_application",
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("authorization grant bindings do not match the candidate")
    issued_at = datetime.fromisoformat(str(payload.get("issued_at") or ""))
    expires_at = datetime.fromisoformat(str(payload.get("expires_at") or ""))
    if (
        issued_at.tzinfo is None
        or issued_at.utcoffset() is None
        or expires_at.tzinfo is None
        or expires_at.utcoffset() is None
        or expires_at <= issued_at
        or expires_at - issued_at > timedelta(hours=1)
    ):
        raise ValueError("authorization grant lifetime is invalid")
    if now is not None and not issued_at <= now <= expires_at:
        raise ValueError("authorization grant is not currently valid")


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_consumption_timestamp(data: bytes) -> datetime:
    try:
        parsed = datetime.fromisoformat(data.decode("utf-8").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise CampaignError("authorization consumption evidence is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CampaignError("authorization consumption timestamp lacks a timezone")
    return parsed.astimezone(timezone.utc)


def _aware_datetime(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return current.astimezone(timezone.utc)


def _iso_now(value: datetime | None = None) -> str:
    return _aware_datetime(value).isoformat()


def _require_aware_iso(value: str, *, field: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")


def _require_nonempty(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} cannot be empty")
    return value


def _require_bounded_text(value: str, *, field: str, max_chars: int) -> str:
    _require_nonempty(value, field=field)
    if len(value) > max_chars or any(character in value for character in "\0"):
        raise ValueError(f"{field} exceeds its bounded text contract")
    return value


def _require_sha256(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _require_safe_id(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{field} must be a bounded machine identifier")
    return value


def _require_safe_issuer(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ISSUER.fullmatch(value):
        raise ValueError("approval_issuer must be a bounded signer identity")
    return value


def _require_canonical_job_id(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("canonical_job_id cannot be empty or padded")
    if len(value) > 500 or any(character in value for character in "\r\n\0"):
        raise ValueError("canonical_job_id is invalid")
    return value


def _require_reason_code(value: str, *, required: bool = False) -> str:
    if not value and not required:
        return ""
    if not isinstance(value, str) or not _SAFE_CODE.fullmatch(value):
        raise ValueError("reason code must be a bounded lowercase machine code")
    return value


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _pretty_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest_json(payload: Any) -> str:
    return _sha256(_canonical_json_bytes(payload))


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignCorruptionError(f"cannot read campaign JSON artifact: {path.name}") from exc
    if not isinstance(payload, dict):
        raise CampaignCorruptionError(f"campaign JSON artifact is not an object: {path.name}")
    return payload


def _read_event_log(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CampaignCorruptionError("cannot read campaign event log") from exc
    if not raw.endswith(b"\n"):
        raise CampaignCorruptionError("campaign event log has an incomplete final record")
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CampaignCorruptionError("campaign event log contains invalid JSON") from exc
        if not isinstance(event, dict):
            raise CampaignCorruptionError("campaign event is not a JSON object")
        events.append(event)
    return events


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _write_immutable_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if path.read_bytes() != data:
            raise FileExistsError(f"immutable artifact already differs: {path.name}")
        return
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _append_fsynced_json_line(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _canonical_json_bytes(payload) + b"\n"
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    data = _pretty_json_bytes(payload)
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _delete_file_and_fsync(path: Path) -> None:
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)

"""Externally signed applicant-fact approvals for live autonomy.

The autonomous controller may prepare and verify an approval request, but it
cannot create a valid approval.  A detached OpenSSH signature must verify
against an operator-owned allowed-signers file that is not writable by the
controller on the campaign machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Self

from applypilot import config
from applypilot.autonomy.facts import (
    REQUIRED_AUTONOMY_FACT_IDS,
    FactLedger,
    FactState,
    require_confirmed_facts,
)


FACT_APPROVAL_SCHEMA_VERSION = "applypilot-fact-approval-v2"
FACT_APPROVAL_SIGNATURE_NAMESPACE = "applypilot-fact-approval"
FACT_APPROVAL_NAME = "fact_approval.json"
FACT_APPROVAL_SIGNATURE_NAME = "fact_approval.json.sig"
MAX_APPROVAL_BYTES = 256_000
MAX_SIGNATURE_BYTES = 64_000
MAX_APPROVAL_LIFETIME = timedelta(days=7)
MAX_SOURCE_AGE = timedelta(days=7)
MAX_CLOCK_SKEW = timedelta(minutes=5)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CHALLENGE = re.compile(r"[0-9a-f]{64}\Z")
_ISSUER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:+-]{0,199}\Z")
_SOURCE_SURFACE = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
_LOCATION_FACT_PREFIXES = (
    "profile.availability.preferred_locations.",
    "profile.preferences.locations.",
)


class FactApprovalError(PermissionError):
    """Raised when applicant approval is missing, stale, or unverifiable."""


@dataclass(frozen=True)
class FactApprovalExpectation:
    """Exact run and fact bindings that an external signer must approve."""

    run_id: str
    run_manifest_sha256: str
    approval_challenge: str
    fact_digest: str
    context_digest: str
    policy_digest: str
    profile_sha256: str
    resume_sha256: str
    approved_fact_value_hashes: tuple[tuple[str, str], ...]

    @classmethod
    def from_run(
        cls,
        *,
        manifest: Mapping[str, Any],
        manifest_sha256: str,
        fact_ledger: FactLedger,
    ) -> Self:
        blockers = require_confirmed_facts(fact_ledger, REQUIRED_AUTONOMY_FACT_IDS)
        if blockers:
            raise FactApprovalError(
                "required applicant facts are not confirmed: " + ",".join(blockers)
            )
        fact_ids = [record.fact_id for record in fact_ledger.records]
        if len(fact_ids) != len(set(fact_ids)):
            raise FactApprovalError("live approval rejects duplicate fact ledger ids")
        by_id = {record.fact_id: record for record in fact_ledger.records}
        location_ids = sorted(
            record.fact_id
            for record in fact_ledger.records
            if record.state is FactState.CONFIRMED
            and any(record.fact_id.startswith(prefix) for prefix in _LOCATION_FACT_PREFIXES)
        )
        if not location_ids:
            raise FactApprovalError(
                "live approval requires at least one confirmed preferred-location fact"
            )
        approved_ids = tuple(sorted(set(REQUIRED_AUTONOMY_FACT_IDS) | set(location_ids)))
        approved_hashes = tuple(
            (
                fact_id,
                _sha256(
                    _canonical_json_bytes(
                        {
                            "fact_id": fact_id,
                            "value": by_id[fact_id].value,
                        }
                    )
                ),
            )
            for fact_id in approved_ids
        )
        expectation = cls(
            run_id=str(manifest.get("run_id") or ""),
            run_manifest_sha256=manifest_sha256,
            approval_challenge=str(manifest.get("approval_challenge") or ""),
            fact_digest=str(manifest.get("fact_digest") or ""),
            context_digest=str(manifest.get("context_digest") or ""),
            policy_digest=str(manifest.get("policy_digest") or ""),
            profile_sha256=fact_ledger.profile_sha256,
            resume_sha256=fact_ledger.resume_sha256,
            approved_fact_value_hashes=approved_hashes,
        )
        expectation.validate()
        if expectation.fact_digest != fact_ledger.digest:
            raise FactApprovalError("fact approval expectation differs from the fact ledger")
        return expectation

    def validate(self) -> None:
        if not self.run_id or len(self.run_id) > 200:
            raise FactApprovalError("fact approval run id is invalid")
        for field_name in (
            "run_manifest_sha256",
            "fact_digest",
            "context_digest",
            "policy_digest",
            "profile_sha256",
            "resume_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name)
        if not _CHALLENGE.fullmatch(self.approval_challenge):
            raise FactApprovalError("fact approval challenge is missing or invalid")
        if not self.approved_fact_value_hashes:
            raise FactApprovalError("fact approval has no exact fact decisions")
        fact_ids = [fact_id for fact_id, _ in self.approved_fact_value_hashes]
        if fact_ids != sorted(set(fact_ids)):
            raise FactApprovalError("fact approval decision ids are duplicate or unsorted")
        for fact_id, digest in self.approved_fact_value_hashes:
            if not fact_id or len(fact_id) > 500:
                raise FactApprovalError("fact approval decision id is invalid")
            _require_sha256(digest, f"approved fact {fact_id}")

    def approved_fact_map(self) -> dict[str, str]:
        return dict(self.approved_fact_value_hashes)


@dataclass(frozen=True)
class VerifiedFactApproval:
    """Verified approval metadata safe to bind into a campaign manifest."""

    issuer: str
    issued_at: str
    expires_at: str
    receipt_sha256: str
    signature_sha256: str
    trust_store_sha256: str


def require_system_approval_trust_store() -> Path:
    """Return the fixed OS-protected trust anchor used by all live commands."""
    return require_root_protected_file(
        config.SYSTEM_APPROVAL_TRUST_STORE_PATH,
        label="system approval trust store",
    )


def require_root_protected_file(
    path: Path,
    *,
    label: str,
    executable: bool = False,
) -> Path:
    """Require one canonical root-owned file and an unwriteable parent chain."""
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise FactApprovalError(f"{label} is not installed") from exc
    if resolved != path or not resolved.is_file():
        raise FactApprovalError(f"{label} path is not canonical")
    if os.name != "posix":
        raise FactApprovalError(f"{label} requires POSIX ownership checks")
    if executable and not os.access(resolved, os.X_OK):
        raise FactApprovalError(f"{label} is not executable")
    protected_path = resolved
    while True:
        protected_metadata = protected_path.stat()
        if protected_metadata.st_uid != 0 or protected_metadata.st_mode & 0o022:
            raise FactApprovalError(
                f"{label} and parent chain must be root-owned and not writable"
            )
        if protected_path == protected_path.parent:
            break
        protected_path = protected_path.parent
    return resolved


def build_unsigned_fact_approval(
    expectation: FactApprovalExpectation,
    *,
    issuer: str,
    source_surface: str,
    source_message_sha256: str,
    source_author_sha256: str,
    source_observed_at: datetime,
    issued_at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    """Build the exact JSON object a user-controlled identity must sign."""
    expectation.validate()
    payload = {
        "schema_version": FACT_APPROVAL_SCHEMA_VERSION,
        "issuer": issuer,
        "issued_at": _iso_utc(issued_at),
        "expires_at": _iso_utc(expires_at),
        "run_id": expectation.run_id,
        "run_manifest_sha256": expectation.run_manifest_sha256,
        "approval_challenge": expectation.approval_challenge,
        "fact_digest": expectation.fact_digest,
        "context_digest": expectation.context_digest,
        "policy_digest": expectation.policy_digest,
        "profile_sha256": expectation.profile_sha256,
        "resume_sha256": expectation.resume_sha256,
        "approved_fact_value_hashes": expectation.approved_fact_map(),
        "source_evidence": {
            "surface": source_surface,
            "message_sha256": source_message_sha256,
            "author_sha256": source_author_sha256,
            "observed_at": _iso_utc(source_observed_at),
        },
    }
    _validate_payload(payload, expectation=expectation, now=issued_at)
    return payload


def approval_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return deterministic, human-readable bytes suitable for signing."""
    return (json.dumps(dict(payload), indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def write_unsigned_fact_approval(path: Path, payload: Mapping[str, Any]) -> Path:
    """Write exact immutable bytes for external human review and signing."""
    path = Path(path).resolve()
    _write_immutable_bytes(path, approval_json_bytes(payload))
    return path


def verify_and_import_fact_approval(
    *,
    run_dir: Path,
    expectation: FactApprovalExpectation,
    attestation_path: Path,
    signature_path: Path,
    trust_store_path: Path,
    now: datetime | None = None,
) -> VerifiedFactApproval:
    """Verify an external signature, then immutably import the exact approval bytes."""
    verified, attestation, signature = _verify_paths(
        expectation=expectation,
        attestation_path=attestation_path,
        signature_path=signature_path,
        trust_store_path=trust_store_path,
        now=now,
    )
    run_dir = Path(run_dir).resolve()
    _write_immutable_bytes(run_dir / FACT_APPROVAL_NAME, attestation)
    _write_immutable_bytes(run_dir / FACT_APPROVAL_SIGNATURE_NAME, signature)
    return verified


def load_verified_fact_approval(
    *,
    run_dir: Path,
    expectation: FactApprovalExpectation,
    trust_store_path: Path,
    now: datetime | None = None,
) -> VerifiedFactApproval:
    """Re-verify the fixed run-local approval and detached signature."""
    run_dir = Path(run_dir).resolve()
    verified, _, _ = _verify_paths(
        expectation=expectation,
        attestation_path=run_dir / FACT_APPROVAL_NAME,
        signature_path=run_dir / FACT_APPROVAL_SIGNATURE_NAME,
        trust_store_path=trust_store_path,
        now=now,
    )
    return verified


def _verify_paths(
    *,
    expectation: FactApprovalExpectation,
    attestation_path: Path,
    signature_path: Path,
    trust_store_path: Path,
    now: datetime | None,
) -> tuple[VerifiedFactApproval, bytes, bytes]:
    expectation.validate()
    attestation = _read_bounded(attestation_path, MAX_APPROVAL_BYTES, "fact approval")
    signature = _read_bounded(signature_path, MAX_SIGNATURE_BYTES, "fact approval signature")
    trust_store = _read_bounded(trust_store_path, MAX_APPROVAL_BYTES, "approval trust store")
    try:
        payload = json.loads(attestation, object_pairs_hook=_object_without_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, FactApprovalError) as exc:
        raise FactApprovalError("fact approval is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise FactApprovalError("fact approval must be a JSON object")
    if approval_json_bytes(payload) != attestation:
        raise FactApprovalError("fact approval JSON is not canonical")
    current = _aware_utc(now)
    _validate_payload(payload, expectation=expectation, now=current)
    issuer = str(payload["issuer"])
    _verify_ssh_signature(
        attestation=attestation,
        signature=signature,
        trust_store=trust_store,
        issuer=issuer,
    )
    return (
        VerifiedFactApproval(
            issuer=issuer,
            issued_at=str(payload["issued_at"]),
            expires_at=str(payload["expires_at"]),
            receipt_sha256=_sha256(attestation),
            signature_sha256=_sha256(signature),
            trust_store_sha256=_sha256(trust_store),
        ),
        attestation,
        signature,
    )


def _validate_payload(
    payload: Mapping[str, Any],
    *,
    expectation: FactApprovalExpectation,
    now: datetime,
) -> None:
    required = {
        "schema_version",
        "issuer",
        "issued_at",
        "expires_at",
        "run_id",
        "run_manifest_sha256",
        "approval_challenge",
        "fact_digest",
        "context_digest",
        "policy_digest",
        "profile_sha256",
        "resume_sha256",
        "approved_fact_value_hashes",
        "source_evidence",
    }
    if set(payload) != required:
        raise FactApprovalError("fact approval fields differ from schema")
    if payload.get("schema_version") != FACT_APPROVAL_SCHEMA_VERSION:
        raise FactApprovalError("fact approval schema is unsupported")
    issuer = str(payload.get("issuer") or "")
    if not _ISSUER.fullmatch(issuer):
        raise FactApprovalError("fact approval issuer is invalid")
    expected_values = {
        "run_id": expectation.run_id,
        "run_manifest_sha256": expectation.run_manifest_sha256,
        "approval_challenge": expectation.approval_challenge,
        "fact_digest": expectation.fact_digest,
        "context_digest": expectation.context_digest,
        "policy_digest": expectation.policy_digest,
        "profile_sha256": expectation.profile_sha256,
        "resume_sha256": expectation.resume_sha256,
        "approved_fact_value_hashes": expectation.approved_fact_map(),
    }
    mismatches = [key for key, value in expected_values.items() if payload.get(key) != value]
    if mismatches:
        raise FactApprovalError("fact approval binding mismatch: " + ",".join(mismatches))

    issued_at = _parse_aware(str(payload.get("issued_at") or ""), "issued_at")
    expires_at = _parse_aware(str(payload.get("expires_at") or ""), "expires_at")
    if issued_at > now + MAX_CLOCK_SKEW:
        raise FactApprovalError("fact approval is issued in the future")
    if not issued_at <= now <= expires_at:
        raise FactApprovalError("fact approval is not currently valid")
    if expires_at <= issued_at or expires_at - issued_at > MAX_APPROVAL_LIFETIME:
        raise FactApprovalError("fact approval lifetime is invalid")

    source = payload.get("source_evidence")
    if not isinstance(source, dict) or set(source) != {
        "surface",
        "message_sha256",
        "author_sha256",
        "observed_at",
    }:
        raise FactApprovalError("fact approval source evidence is invalid")
    if not _SOURCE_SURFACE.fullmatch(str(source.get("surface") or "")):
        raise FactApprovalError("fact approval source surface is invalid")
    _require_sha256(str(source.get("message_sha256") or ""), "source message")
    _require_sha256(str(source.get("author_sha256") or ""), "source author")
    observed_at = _parse_aware(str(source.get("observed_at") or ""), "source observed_at")
    if observed_at > issued_at + MAX_CLOCK_SKEW or issued_at - observed_at > MAX_SOURCE_AGE:
        raise FactApprovalError("fact approval source evidence is stale or future-dated")


def _verify_ssh_signature(
    *,
    attestation: bytes,
    signature: bytes,
    trust_store: bytes,
    issuer: str,
) -> None:
    executable = require_root_protected_file(
        config.SYSTEM_SSH_KEYGEN_PATH,
        label="system ssh-keygen",
        executable=True,
    )
    with tempfile.TemporaryDirectory(prefix="applypilot-approval-") as temporary:
        temporary_root = Path(temporary)
        signature_path = temporary_root / "approval.sig"
        trust_store_path = temporary_root / "allowed_signers"
        signature_path.write_bytes(signature)
        trust_store_path.write_bytes(trust_store)
        signature_path.chmod(0o600)
        trust_store_path.chmod(0o600)
        result = subprocess.run(
            [
                executable,
                "-Y",
                "verify",
                "-f",
                str(trust_store_path),
                "-I",
                issuer,
                "-n",
                FACT_APPROVAL_SIGNATURE_NAMESPACE,
                "-s",
                str(signature_path),
            ],
            input=attestation,
            capture_output=True,
            check=False,
            timeout=30,
            cwd=temporary_root,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
            },
        )
    if result.returncode != 0:
        raise FactApprovalError("applicant approval signature is invalid or untrusted")


def _read_bounded(path: Path, limit: int, label: str) -> bytes:
    try:
        data = Path(path).resolve().read_bytes()
    except OSError as exc:
        raise FactApprovalError(f"{label} cannot be read") from exc
    if not data or len(data) > limit:
        raise FactApprovalError(f"{label} is empty or too large")
    return data


def _write_immutable_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if path.read_bytes() != data:
            raise FileExistsError(f"a different immutable approval already exists: {path.name}")
        return
    try:
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError("short approval write")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FactApprovalError(f"fact approval contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_sha256(value: str, field: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise FactApprovalError(f"{field} must be a lowercase SHA-256 digest")


def _aware_utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise FactApprovalError("approval timestamps must include a timezone")
    return current.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return _aware_utc(value).isoformat()


def _parse_aware(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FactApprovalError(f"fact approval {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FactApprovalError(f"fact approval {field} lacks a timezone")
    return parsed.astimezone(timezone.utc)

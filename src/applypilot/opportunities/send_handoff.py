"""One-time, digest-bound portable handoff for externally authenticated outreach sends."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from applypilot.autonomy.handoff import (
    HANDOFF_SCHEMA_VERSION,
    RunBindings,
    _active_handoffs_unlocked,
    _canonical_json,
    _handoff_queue_lock,
    _pending_for_active,
    _write_immutable_json,
)
from applypilot.observability.events import EventJournal
from applypilot.opportunities.outreach import OutreachDraft, validate_outreach_draft

OUTREACH_AUTHORIZATION_SCHEMA_VERSION = "applypilot-outreach-authorization-v1"
OUTREACH_SEND_RESPONSE_SCHEMA_VERSION = "applypilot.outreach-send-response.v1"
OUTREACH_RESOURCE_LOCK = "outbound_communication"
MAX_AUTHORIZATION_LIFETIME = timedelta(minutes=30)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[a-zA-Z0-9_.:-]{1,120}$")
_RESULT_STATUSES = frozenset(
    {"provider_accepted", "submitted", "send_state_unknown", "error", "not_attempted"}
)


class OutreachAuthorizationError(PermissionError):
    """Raised when an outbound grant is absent, stale, changed, or consumed."""


@dataclass(frozen=True)
class OutreachAuthorization:
    authorization_id: str
    action: str
    sender: str
    items: tuple[tuple[str, str, str], ...]
    channel: str
    issued_at: str
    expires_at: str
    nonce: str
    sha256: str

    def unsigned_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("sha256", None)
        payload["schema_version"] = OUTREACH_AUTHORIZATION_SCHEMA_VERSION
        payload["items"] = [list(item) for item in self.items]
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "sha256": self.sha256}


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OutreachAuthorizationError("outreach authorization timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise OutreachAuthorizationError("outreach authorization timestamp lacks a timezone")
    return parsed.astimezone(timezone.utc)


def build_outreach_authorization(
    drafts: tuple[OutreachDraft, ...],
    *,
    sender: str,
    channel: str,
    now: datetime | None = None,
    lifetime: timedelta = MAX_AUTHORIZATION_LIFETIME,
) -> OutreachAuthorization:
    """Build a one-time exact batch grant; this performs no send or mailbox mutation."""
    if not 1 <= len(drafts) <= 10:
        raise OutreachAuthorizationError("outreach authorization must bind 1 to 10 drafts")
    if lifetime <= timedelta(0) or lifetime > MAX_AUTHORIZATION_LIFETIME:
        raise OutreachAuthorizationError("outreach authorization lifetime is invalid")
    for draft in drafts:
        validate_outreach_draft(draft)
        if draft.sender != sender or draft.channel != channel:
            raise OutreachAuthorizationError("outreach draft sender or channel differs")
    items = tuple((draft.lead_id, draft.draft_id, draft.sha256) for draft in drafts)
    if len(set(items)) != len(items):
        raise OutreachAuthorizationError("outreach authorization items are duplicated")
    issued = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expires = issued + lifetime
    nonce = secrets.token_hex(32)
    basis = {
        "schema_version": OUTREACH_AUTHORIZATION_SCHEMA_VERSION,
        "authorization_id": "pending",
        "action": "send_outreach",
        "sender": sender,
        "items": [list(item) for item in items],
        "channel": channel,
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "nonce": nonce,
    }
    authorization_id = f"outreach-{_digest(basis)[:24]}"
    basis["authorization_id"] = authorization_id
    digest = _digest(basis)
    authorization = OutreachAuthorization(
        authorization_id=authorization_id,
        action="send_outreach",
        sender=sender,
        items=items,
        channel=channel,
        issued_at=issued.isoformat(),
        expires_at=expires.isoformat(),
        nonce=nonce,
        sha256=digest,
    )
    validate_outreach_authorization(authorization, now=issued)
    return authorization


def validate_outreach_authorization(
    authorization: OutreachAuthorization,
    *,
    now: datetime | None = None,
) -> None:
    if authorization.action != "send_outreach":
        raise OutreachAuthorizationError("outreach authorization action is invalid")
    if not _SAFE_ID.fullmatch(authorization.authorization_id):
        raise OutreachAuthorizationError("outreach authorization id is invalid")
    if authorization.channel not in {"email", "linkedin", "contact_form"}:
        raise OutreachAuthorizationError("outreach authorization channel is invalid")
    if not authorization.sender or len(authorization.sender) > 320:
        raise OutreachAuthorizationError("outreach authorization sender is invalid")
    if not 1 <= len(authorization.items) <= 10 or len(set(authorization.items)) != len(
        authorization.items
    ):
        raise OutreachAuthorizationError("outreach authorization item set is invalid")
    for lead_id, draft_id, draft_sha256 in authorization.items:
        if (
            not _SAFE_ID.fullmatch(lead_id)
            or not _SAFE_ID.fullmatch(draft_id)
            or not _SHA256.fullmatch(draft_sha256)
        ):
            raise OutreachAuthorizationError("outreach authorization item binding is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", authorization.nonce):
        raise OutreachAuthorizationError("outreach authorization nonce is invalid")
    issued = _parse_time(authorization.issued_at)
    expires = _parse_time(authorization.expires_at)
    if expires <= issued or expires - issued > MAX_AUTHORIZATION_LIFETIME:
        raise OutreachAuthorizationError("outreach authorization lifetime is invalid")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if current < issued - timedelta(minutes=1) or current >= expires:
        raise OutreachAuthorizationError("outreach authorization is not currently valid")
    if authorization.sha256 != _digest(authorization.unsigned_dict()):
        raise OutreachAuthorizationError("outreach authorization digest mismatch")


def write_outreach_authorization(path: Path, authorization: OutreachAuthorization) -> Path:
    validate_outreach_authorization(authorization)
    _write_immutable_json(path.resolve(), authorization.to_dict())
    return path.resolve()


def load_outreach_authorization(
    path: Path, *, now: datetime | None = None
) -> OutreachAuthorization:
    source = path.expanduser()
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 256_000:
        raise OutreachAuthorizationError("outreach authorization file is invalid")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema_version") != OUTREACH_AUTHORIZATION_SCHEMA_VERSION:
        raise OutreachAuthorizationError("unsupported outreach authorization schema")
    expected = {
        "schema_version",
        "authorization_id",
        "action",
        "sender",
        "items",
        "channel",
        "issued_at",
        "expires_at",
        "nonce",
        "sha256",
    }
    if set(payload) != expected:
        raise OutreachAuthorizationError("outreach authorization fields are invalid")
    try:
        items = tuple(tuple(str(value) for value in item) for item in payload["items"])
        authorization = OutreachAuthorization(
            authorization_id=str(payload["authorization_id"]),
            action=str(payload["action"]),
            sender=str(payload["sender"]),
            items=items,
            channel=str(payload["channel"]),
            issued_at=str(payload["issued_at"]),
            expires_at=str(payload["expires_at"]),
            nonce=str(payload["nonce"]),
            sha256=str(payload["sha256"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise OutreachAuthorizationError("outreach authorization is malformed") from exc
    validate_outreach_authorization(authorization, now=now)
    return authorization


def outreach_bindings(authorization: OutreachAuthorization) -> RunBindings:
    return RunBindings(
        run_id=authorization.authorization_id,
        fact_digest=authorization.sha256,
        context_digest=authorization.sha256,
        policy_digest=authorization.sha256,
    )


def queue_send_handoff(
    *,
    authorization_path: Path,
    store: Any,
    run_dir: Path,
    journal: EventJournal,
    now: datetime | None = None,
) -> Path:
    """Consume an exact grant, then queue a portable send request without provider access."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    authorization = load_outreach_authorization(authorization_path, now=current)
    drafts: list[OutreachDraft] = []
    for lead_id, draft_id, draft_sha256 in authorization.items:
        record = store.get_draft(draft_id)
        draft = OutreachDraft.from_dict(record["draft"])
        if (
            draft.lead_id != lead_id
            or draft.sha256 != draft_sha256
            or draft.sender != authorization.sender
            or draft.channel != authorization.channel
        ):
            raise OutreachAuthorizationError("authorized outreach draft binding changed")
        drafts.append(draft)
    try:
        consumption = store.consume_authorization(authorization, now=current)
    except PermissionError as exc:
        raise OutreachAuthorizationError(str(exc)) from exc
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    bound_authorization_path = run_dir / "outreach-authorization.json"
    _write_immutable_json(bound_authorization_path, authorization.to_dict())
    consumption_path = run_dir / f"{authorization.authorization_id}.consumption.json"
    _write_immutable_json(consumption_path, consumption)
    journal.emit(
        component="outreach",
        phase="authorization_validated",
        status="complete",
        source=authorization.channel,
        counts={"item_count": len(drafts)},
    )
    bindings = outreach_bindings(authorization)
    handoff_dir = run_dir / "handoff"
    request_path = handoff_dir / f"{authorization.authorization_id}.request.json"
    response_path = handoff_dir / f"{authorization.authorization_id}.response.json"
    envelope = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "response_schema_version": OUTREACH_SEND_RESPONSE_SCHEMA_VERSION,
        "run_id": bindings.run_id,
        "stage": "outreach",
        "kind": "outreach_send",
        "request_id": authorization.sha256,
        "input_digest": authorization.sha256,
        "candidate_id": None,
        "fact_digest": bindings.fact_digest,
        "context_digest": bindings.context_digest,
        "policy_digest": bindings.policy_digest,
        "resource_lock": OUTREACH_RESOURCE_LOCK,
        "authorization_sha256": authorization.sha256,
        "authorization_path": str(bound_authorization_path.relative_to(run_dir)),
        "consumption_sha256": consumption["sha256"],
        "consumption_path": str(consumption_path.relative_to(run_dir)),
        "channel": authorization.channel,
        "sender": authorization.sender,
        "items": [draft.to_dict() for draft in drafts],
        "response_path": str(response_path.relative_to(run_dir)),
        "max_response_chars": 100_000,
        "response_format": "strict_json",
        "raw_transcript_required": False,
        "instructions": [
            "Use only the authenticated account matching sender and the declared channel.",
            "Send each exact immutable draft at most once; do not edit content or recipients.",
            "After an ambiguous timeout, report send_state_unknown and do not retry.",
            "Do not schedule follow-ups or contact any recipient outside the exact batch.",
        ],
    }
    with _handoff_queue_lock(run_dir):
        current_handoffs = _active_handoffs_unlocked(run_dir=run_dir, bindings=bindings)
        if current_handoffs:
            raise _pending_for_active(current_handoffs[0])
        _write_immutable_json(request_path, envelope)
    journal.emit(
        component="outreach",
        phase="send_handoff_queued",
        status="complete",
        source=authorization.channel,
        counts={"item_count": len(drafts)},
    )
    return request_path.resolve()


def validate_send_response(payload: Any, *, request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("outreach send response must be an object")
    expected_top = {
        "schema_version": OUTREACH_SEND_RESPONSE_SCHEMA_VERSION,
        "kind": "outreach_send",
        "request_id": request.get("request_id"),
        "authorization_sha256": request.get("authorization_sha256"),
    }
    if any(payload.get(key) != value for key, value in expected_top.items()):
        raise ValueError("outreach send response bindings changed")
    if set(payload) != {*expected_top, "items"} or not isinstance(payload.get("items"), list):
        raise ValueError("outreach send response fields are invalid")
    authorized = {
        (item["lead_id"], item["draft_id"], item["sha256"]): item
        for item in request.get("items") or []
    }
    if len(payload["items"]) != len(authorized):
        raise ValueError("outreach send response must account for every authorized item")
    seen: set[tuple[str, str, str]] = set()
    canonical_items: list[dict[str, Any]] = []
    for item in payload["items"]:
        if not isinstance(item, dict) or set(item) != {
            "lead_id",
            "draft_id",
            "draft_sha256",
            "channel",
            "status",
            "provider_receipt_id",
            "observed_at",
        }:
            raise ValueError("outreach send item fields are invalid")
        binding = (
            str(item["lead_id"]),
            str(item["draft_id"]),
            str(item["draft_sha256"]),
        )
        if binding not in authorized or binding in seen:
            raise ValueError("outreach send item was not uniquely authorized")
        seen.add(binding)
        status = str(item["status"])
        if status not in _RESULT_STATUSES or item["channel"] != request.get("channel"):
            raise ValueError("outreach send status or channel is invalid")
        receipt_id = str(item.get("provider_receipt_id") or "")
        if status in {"provider_accepted", "submitted"} and not receipt_id:
            raise ValueError("accepted outreach send requires a provider receipt")
        if status not in {"provider_accepted", "submitted"} and receipt_id:
            raise ValueError("unsuccessful outreach send cannot claim a provider receipt")
        observed = _parse_time(str(item["observed_at"]))
        canonical_items.append(
            {
                **{key: str(item[key]) for key in ("lead_id", "draft_id", "draft_sha256")},
                "channel": str(item["channel"]),
                "status": status,
                "provider_receipt_id": receipt_id,
                "observed_at": observed.isoformat(),
            }
        )
    return {**expected_top, "items": canonical_items}


def consume_send_response(
    *, request_path: Path, store: Any, journal: EventJournal
) -> dict[str, Any]:
    request_path = request_path.resolve(strict=True)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    response_path = request_path.with_name(
        request_path.name.replace(".request.json", ".response.json")
    )
    if not response_path.is_file() or response_path.is_symlink():
        raise ValueError("outreach send response is not ready")
    payload = validate_send_response(
        json.loads(response_path.read_text(encoding="utf-8")), request=request
    )
    response_sha256 = hashlib.sha256(response_path.read_bytes()).hexdigest()
    store.record_send_receipts(
        str(request["run_id"]),
        receipts=payload["items"],
        response_sha256=response_sha256,
    )
    receipt_path = response_path.with_name(
        response_path.name.replace(".response.json", ".receipt.json")
    )
    safe_counts: dict[str, int] = {status: 0 for status in _RESULT_STATUSES}
    for item in payload["items"]:
        safe_counts[item["status"]] += 1
    _write_immutable_json(
        receipt_path,
        {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "request_id": request["request_id"],
            "response_sha256": response_sha256,
            "counts": {key: value for key, value in safe_counts.items() if value},
        },
    )
    terminal = "send_state_unknown" if safe_counts["send_state_unknown"] else "complete"
    journal.emit(
        component="outreach",
        phase=terminal,
        status="complete" if terminal == "complete" else "blocked",
        source=str(request["channel"]),
        counts={key: value for key, value in safe_counts.items() if value},
    )
    return {
        "authorization_id": str(request["run_id"]),
        "status": terminal,
        "counts": {key: value for key, value in safe_counts.items() if value},
        "receipt_path": str(receipt_path),
    }


class SendAdapter(Protocol):
    def send(self, draft: dict[str, Any]) -> dict[str, str]:
        """Perform one provider call and return status, receipt id, and observed time."""


def execute_send_handoff(
    *, request_path: Path, adapter: SendAdapter, output_path: Path | None = None
) -> Path:
    """Worker boundary used by an authenticated connector; callers must supply the adapter."""
    request_path = request_path.resolve(strict=True)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if (
        request.get("schema_version") != HANDOFF_SCHEMA_VERSION
        or request.get("kind") != "outreach_send"
        or request.get("resource_lock") != OUTREACH_RESOURCE_LOCK
        or not _SHA256.fullmatch(str(request.get("consumption_sha256") or ""))
    ):
        raise OutreachAuthorizationError("consumed outreach authorization is required")
    run_dir = request_path.parent.parent.resolve()
    authorization_path = (run_dir / str(request.get("authorization_path") or "")).resolve()
    consumption_path = (run_dir / str(request.get("consumption_path") or "")).resolve()
    if (
        authorization_path.parent != run_dir
        or authorization_path.name != "outreach-authorization.json"
        or consumption_path.parent != run_dir
        or not consumption_path.name.endswith(".consumption.json")
    ):
        raise OutreachAuthorizationError("outreach authorization artifacts escaped the run")
    authorization = load_outreach_authorization(authorization_path)
    if (
        authorization.sha256 != request.get("authorization_sha256")
        or authorization.authorization_id != request.get("run_id")
        or authorization.sender != request.get("sender")
        or authorization.channel != request.get("channel")
        or [
            (str(item.get("lead_id")), str(item.get("draft_id")), str(item.get("sha256")))
            for item in request.get("items") or []
        ]
        != list(authorization.items)
    ):
        raise OutreachAuthorizationError("outreach request differs from its authorization")
    consumption = json.loads(consumption_path.read_text(encoding="utf-8"))
    consumption_unsigned = dict(consumption)
    consumption_sha256 = str(consumption_unsigned.pop("sha256", ""))
    if (
        consumption_sha256 != _digest(consumption_unsigned)
        or consumption_sha256 != request.get("consumption_sha256")
        or consumption.get("authorization_id") != authorization.authorization_id
        or consumption.get("authorization_sha256") != authorization.sha256
        or int(consumption.get("item_count") or 0) != len(authorization.items)
    ):
        raise OutreachAuthorizationError("outreach authorization consumption is invalid")
    journal = EventJournal(run_dir / "events.ndjson", run_id=authorization.authorization_id)
    journal.emit(
        component="outreach",
        phase="pre_send_validated",
        status="complete",
        source=authorization.channel,
        counts={"item_count": len(authorization.items)},
    )
    results: list[dict[str, str]] = []
    stopped = False
    for ordinal, draft in enumerate(request.get("items") or [], 1):
        if stopped:
            result = {"status": "not_attempted", "provider_receipt_id": ""}
        else:
            journal.emit(
                component="outreach",
                phase="send_attempted",
                status="started",
                source=authorization.channel,
                counts={"ordinal": ordinal},
            )
            try:
                result = dict(adapter.send(draft))
            except TimeoutError:
                result = {"status": "send_state_unknown", "provider_receipt_id": ""}
                stopped = True
            except Exception:
                result = {"status": "error", "provider_receipt_id": ""}
                stopped = True
            status = str(result.get("status") or "error")
            receipt_id = str(result.get("provider_receipt_id") or "")
            observed_at = str(result.get("observed_at") or datetime.now(timezone.utc).isoformat())
            try:
                _parse_time(observed_at)
                valid_result = status in _RESULT_STATUSES and (
                    (status in {"provider_accepted", "submitted"} and bool(receipt_id))
                    or (status not in {"provider_accepted", "submitted"} and not receipt_id)
                )
            except OutreachAuthorizationError:
                valid_result = False
            if not valid_result:
                result = {
                    "status": "send_state_unknown",
                    "provider_receipt_id": "",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                }
                stopped = True
            elif status in {"send_state_unknown", "error"}:
                stopped = True
            journal.emit(
                component="outreach",
                phase=str(result.get("status") or "error"),
                status=(
                    "complete"
                    if result.get("status") in {"provider_accepted", "submitted"}
                    else "blocked"
                ),
                source=authorization.channel,
                counts={"ordinal": ordinal},
            )
        results.append(
            {
                "lead_id": str(draft["lead_id"]),
                "draft_id": str(draft["draft_id"]),
                "draft_sha256": str(draft["sha256"]),
                "channel": str(request["channel"]),
                "status": str(result.get("status") or "error"),
                "provider_receipt_id": str(result.get("provider_receipt_id") or ""),
                "observed_at": str(
                    result.get("observed_at") or datetime.now(timezone.utc).isoformat()
                ),
            }
        )
    response = validate_send_response(
        {
            "schema_version": OUTREACH_SEND_RESPONSE_SCHEMA_VERSION,
            "kind": "outreach_send",
            "request_id": request["request_id"],
            "authorization_sha256": request["authorization_sha256"],
            "items": results,
        },
        request=request,
    )
    target = output_path or request_path.with_name(
        request_path.name.replace(".request.json", ".response.json")
    )
    _write_immutable_json(target.resolve(), response)
    os.chmod(target.resolve(), 0o600)
    return target.resolve()

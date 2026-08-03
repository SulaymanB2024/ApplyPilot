"""Portable ChatGPT Web request/response handoff artifacts.

The browser agent is intentionally a tiny transport: it reads one bounded
request, sends the embedded prompt in a fresh ChatGPT Web conversation, and
returns the final assistant response. Discovery may be ordinary language;
ApplyPilot normalizes it into a bound internal artifact. All policy,
verification, scoring, and persistence remain local.
"""

from __future__ import annotations

import hashlib
import json
import os
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from applypilot.autonomy.chatgpt_web import (
    ChatGPTContractError,
    material_packet_from_payload,
    parse_chatgpt_response,
    role_candidates_from_payload,
)
from applypilot.autonomy.context import (
    CompactContextPack,
    build_discovery_prompt,
    build_material_prompt,
)
from applypilot.autonomy.models import MaterialPacket, RoleCandidate
from applypilot.autonomy.telemetry import BudgetExceeded, UsageLedger
from applypilot.observability.events import EventJournal

HANDOFF_SCHEMA_VERSION = "applypilot.handoff.v1"
HANDOFF_RECONCILIATION_SCHEMA_VERSION = "applypilot.handoff-reconciliation.v1"
RUN_SCHEMA_VERSION = "applypilot.autonomy-run.v1"
PROMPT_SCHEMA_VERSION = "applypilot.chatgpt-prompt.v7"
HANDOFF_QUEUE_LOCK_NAME = ".queue.lock"


class ArtifactPending(RuntimeError):
    """Raised when the next bounded external-tool artifact has not arrived."""

    def __init__(
        self,
        *,
        surface: str,
        kind: str,
        request_id: str,
        request_path: Path,
        response_path: Path,
    ) -> None:
        self.surface = surface
        self.kind = kind
        self.request_id = request_id
        self.request_path = request_path
        self.response_path = response_path
        super().__init__(f"{surface} response required for {kind}")

    def to_dict(self) -> dict[str, str]:
        return {
            "surface": self.surface,
            "kind": self.kind,
            "request_id": self.request_id,
            "request_path": str(self.request_path),
            "response_path": str(self.response_path),
        }


class ChatGPTArtifactPending(ArtifactPending):
    """Raised when the next bounded ChatGPT Web response has not arrived."""

    def __init__(
        self,
        *,
        kind: str,
        request_id: str,
        request_path: Path,
        response_path: Path,
    ) -> None:
        super().__init__(
            surface="chatgpt_web",
            kind=kind,
            request_id=request_id,
            request_path=request_path,
            response_path=response_path,
        )


class BrowserArtifactPending(ArtifactPending):
    """Raised when the next read-only browser-tool response has not arrived."""

    def __init__(
        self,
        *,
        kind: str,
        request_id: str,
        request_path: Path,
        response_path: Path,
    ) -> None:
        super().__init__(
            surface="browser_tool",
            kind=kind,
            request_id=request_id,
            request_path=request_path,
            response_path=response_path,
        )


@dataclass(frozen=True)
class RunBindings:
    """Digests that bind every handoff request to one reviewed run."""

    run_id: str
    fact_digest: str
    context_digest: str
    policy_digest: str

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> RunBindings:
        if manifest.get("schema_version") != RUN_SCHEMA_VERSION:
            raise ValueError("unsupported autonomy run manifest")
        values = {
            key: str(manifest.get(key) or "")
            for key in ("run_id", "fact_digest", "context_digest", "policy_digest")
        }
        if any(not value for value in values.values()):
            raise ValueError("autonomy run manifest is missing required bindings")
        return cls(**values)


@dataclass(frozen=True)
class ActiveHandoff:
    """One unanswered or responded-but-unconsumed portable exchange."""

    surface: str
    stage: str
    kind: str
    request_id: str
    candidate_id: str
    resource_lock: str
    request_path: Path
    response_path: Path
    receipt_path: Path
    state: str


@contextmanager
def _handoff_queue_lock(run_dir: Path) -> Iterator[None]:
    """Serialize queue mutations without relying on a removable lock sentinel."""
    handoff_dir = run_dir.resolve() / "handoff"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(handoff_dir / HANDOFF_QUEUE_LOCK_NAME, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _active_handoffs_unlocked(
    *,
    run_dir: Path,
    bindings: RunBindings,
) -> list[ActiveHandoff]:
    """Validate the durable queue and return exchanges that still need work."""
    handoff_dir = run_dir.resolve() / "handoff"
    active: list[ActiveHandoff] = []
    seen_response_paths: set[Path] = set()
    stage_kinds = {
        ("discovery", "role_candidates"): "chatgpt_web",
        ("materials", "material_packet"): "chatgpt_web",
        ("form_review", "form_review"): "browser_tool",
        ("portal_discovery", "handshake_job_observations"): "browser_tool",
        ("portal_discovery", "runway_job_observations"): "browser_tool",
        ("opportunity_research", "startup_opportunities"): "browser_tool",
    }
    for request_path in sorted(handoff_dir.glob("*.request.json")):
        if request_path.is_symlink() or not request_path.is_file():
            raise ValueError("handoff request must be a regular non-symlink file")
        request = _read_json_object(request_path)
        if (
            request.get("schema_version") != HANDOFF_SCHEMA_VERSION
            or request.get("run_id") != bindings.run_id
            or request.get("fact_digest") != bindings.fact_digest
            or request.get("context_digest") != bindings.context_digest
            or request.get("policy_digest") != bindings.policy_digest
        ):
            raise ValueError("handoff request bindings differ from run manifest")
        stage = str(request.get("stage") or "")
        kind = str(request.get("kind") or "")
        surface = stage_kinds.get((stage, kind))
        if surface is None:
            raise ValueError("handoff request stage or kind is invalid")
        request_id = str(request.get("request_id") or "")
        if len(request_id) != 64 or any(character not in "0123456789abcdef" for character in request_id):
            raise ValueError("handoff request id is invalid")
        candidate_id = str(request.get("candidate_id") or "")
        candidate_free_kinds = {
            "role_candidates",
            "handshake_job_observations",
            "runway_job_observations",
            "startup_opportunities",
        }
        if kind in candidate_free_kinds and candidate_id:
            raise ValueError("run-level handoff cannot bind a candidate")
        if kind not in candidate_free_kinds and not candidate_id:
            raise ValueError("candidate handoff is missing its candidate binding")
        resource_lock = str(request.get("resource_lock") or "")
        if kind in {
            "handshake_job_observations",
            "runway_job_observations",
            "startup_opportunities",
        }:
            if resource_lock != "authenticated_browser":
                raise ValueError("browser mission is missing the authenticated browser lock")
        elif resource_lock:
            raise ValueError("handoff resource lock is not supported for this kind")
        raw_response_path = run_dir / str(request.get("response_path") or "")
        if raw_response_path.is_symlink():
            raise ValueError("handoff response must not be a symbolic link")
        response_path = raw_response_path.resolve()
        if (
            response_path.parent != handoff_dir.resolve()
            or not response_path.name.endswith(".response.json")
            or response_path in seen_response_paths
        ):
            raise ValueError("handoff response path is invalid or duplicated")
        seen_response_paths.add(response_path)
        raw_receipt_path = raw_response_path.with_name(
            raw_response_path.name.replace(".response.json", ".receipt.json")
        )
        if raw_receipt_path.is_symlink():
            raise ValueError("handoff receipt must not be a symbolic link")
        receipt_path = response_path.with_name(
            response_path.name.replace(".response.json", ".receipt.json")
        )
        response_exists = response_path.is_file()
        receipt_exists = receipt_path.is_file()
        if receipt_exists and not response_exists:
            raise ValueError("consumed handoff response is missing")
        if response_exists and receipt_exists:
            continue
        active.append(
            ActiveHandoff(
                surface=surface,
                stage=stage,
                kind=kind,
                request_id=request_id,
                candidate_id=candidate_id,
                resource_lock=resource_lock,
                request_path=request_path.resolve(),
                response_path=response_path,
                receipt_path=receipt_path,
                state="response_ready" if response_exists else "awaiting_response",
            )
        )
    return active


def active_handoffs(*, run_dir: Path, bindings: RunBindings) -> tuple[ActiveHandoff, ...]:
    """Return the validated single-exchange queue under its mutation lock."""
    with _handoff_queue_lock(run_dir):
        return tuple(_active_handoffs_unlocked(run_dir=run_dir, bindings=bindings))


def _active_candidate_id_for_kind(
    *,
    run_dir: Path,
    bindings: RunBindings,
    kind: str,
) -> str | None:
    current = active_handoffs(run_dir=run_dir, bindings=bindings)
    if len(current) > 1:
        raise ValueError("autonomy run has more than one active handoff exchange")
    if not current or current[0].kind != kind:
        return None
    if not current[0].candidate_id:
        raise ValueError("active candidate handoff is missing its candidate binding")
    return current[0].candidate_id


def _pending_for_active(active: ActiveHandoff) -> ArtifactPending:
    pending_type = (
        ChatGPTArtifactPending if active.surface == "chatgpt_web" else BrowserArtifactPending
    )
    return pending_type(
        kind=active.kind,
        request_id=active.request_id,
        request_path=active.request_path,
        response_path=active.response_path,
    )


def reconcile_unanswered_handoffs(
    *,
    run_dir: Path,
    bindings: RunBindings,
    retain_request_id: str,
) -> dict[str, Any]:
    """Archive duplicate unanswered requests while preserving an immutable audit record."""
    run_dir = run_dir.resolve()
    with _handoff_queue_lock(run_dir):
        current = _active_handoffs_unlocked(run_dir=run_dir, bindings=bindings)
        if len(current) < 2:
            raise ValueError("autonomy run does not have multiple active handoff exchanges")
        if any(item.state != "awaiting_response" for item in current):
            raise ValueError("cannot reconcile a handoff that already has a response")
        if len({(item.surface, item.stage, item.kind) for item in current}) != 1:
            raise ValueError("active handoffs differ in stage or surface")
        retained = [item for item in current if item.request_id == retain_request_id]
        if len(retained) != 1:
            raise ValueError("retained handoff request id is not uniquely active")

        superseded: list[dict[str, str]] = []
        rename_plan: list[tuple[Path, Path]] = []
        for item in current:
            if item.request_id == retain_request_id:
                continue
            request_sha256 = _sha256_text(item.request_path.read_text(encoding="utf-8"))
            stem = item.request_path.name.removesuffix(".request.json")
            archived_path = item.request_path.with_name(
                f"{stem}.superseded.{request_sha256[:16]}.json"
            )
            if archived_path.exists():
                raise FileExistsError("superseded handoff archive already exists")
            rename_plan.append((item.request_path, archived_path))
            superseded.append(
                {
                    "request_id": item.request_id,
                    "request_sha256": request_sha256,
                    "archived_path": str(archived_path.relative_to(run_dir)),
                }
            )

        for request_path, archived_path in rename_plan:
            request_path.rename(archived_path)

        record = {
            "schema_version": HANDOFF_RECONCILIATION_SCHEMA_VERSION,
            "run_id": bindings.run_id,
            "fact_digest": bindings.fact_digest,
            "context_digest": bindings.context_digest,
            "policy_digest": bindings.policy_digest,
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
            "reason": "duplicate_unanswered_exchange",
            "retained_request_id": retain_request_id,
            "surface": retained[0].surface,
            "stage": retained[0].stage,
            "kind": retained[0].kind,
            "superseded_requests": superseded,
        }
        record_digest = _sha256_text(_canonical_json(record))
        record_path = run_dir / "handoff" / f"reconciliation.{record_digest[:16]}.json"
        _write_immutable_json(record_path, record)
        return {
            "run_id": bindings.run_id,
            "retained_request_id": retain_request_id,
            "retained_request_path": str(retained[0].request_path),
            "superseded_request_count": len(superseded),
            "reconciliation_path": str(record_path),
        }


class ArtifactChatGPTClient:
    """Read strict ChatGPT responses from a portable, resumable file queue."""

    def __init__(
        self,
        *,
        run_dir: Path,
        bindings: RunBindings,
        ledger: UsageLedger,
    ) -> None:
        self.run_dir = run_dir.resolve()
        self.bindings = bindings
        self.ledger = ledger

    def active_candidate_id(self, *, kind: str) -> str | None:
        """Return the candidate bound to the one active exchange of ``kind``."""
        if kind != "material_packet":
            raise ValueError("ChatGPT artifact client only resumes material packets by candidate")
        return _active_candidate_id_for_kind(
            run_dir=self.run_dir,
            bindings=self.bindings,
            kind=kind,
        )

    def prepare_discovery_request(
        self,
        *,
        pack: CompactContextPack,
        query: str,
        limit: int,
    ) -> Path:
        input_digest = self._input_digest(
            stage="discovery",
            inputs={"query": query, "limit": limit},
        )
        request_id = self._request_id(stage="discovery", input_digest=input_digest)
        prompt = build_discovery_prompt(
            pack,
            query=query,
            limit=limit,
            request_id=request_id,
        )
        return self._ensure_request(
            stage="discovery",
            kind="role_candidates",
            request_id=request_id,
            prompt=prompt,
            input_digest=input_digest,
        )[0]

    def find_roles(
        self,
        *,
        pack: CompactContextPack,
        query: str,
        limit: int,
    ) -> list[RoleCandidate]:
        input_digest = self._input_digest(
            stage="discovery",
            inputs={"query": query, "limit": limit},
        )
        request_id = self._request_id(stage="discovery", input_digest=input_digest)
        prompt = build_discovery_prompt(
            pack,
            query=query,
            limit=limit,
            request_id=request_id,
        )
        return self._exchange(
            stage="discovery",
            operation="find_roles",
            kind="role_candidates",
            request_id=request_id,
            prompt=prompt,
            input_digest=input_digest,
            semantic_validator=lambda payload: role_candidates_from_payload(
                payload,
                limit=limit,
            ),
        )

    def draft_material(
        self,
        *,
        pack: CompactContextPack,
        candidate: RoleCandidate,
        verified_job_text: str,
    ) -> MaterialPacket:
        input_digest = self._input_digest(
            stage="materials",
            inputs={
                "candidate": {
                    "candidate_id": candidate.candidate_id,
                    "company": candidate.company,
                    "title": candidate.title,
                    "official_url": candidate.official_url,
                },
                "verified_job_sha256": _sha256_text(verified_job_text),
            },
        )
        request_id = self._request_id(
            stage="materials",
            candidate_id=candidate.candidate_id,
            input_digest=input_digest,
        )
        prompt = build_material_prompt(
            pack,
            candidate,
            verified_job_text=verified_job_text,
            request_id=request_id,
        )
        return self._exchange(
            stage="materials",
            operation="draft_cover_letter",
            kind="material_packet",
            request_id=request_id,
            prompt=prompt,
            candidate_id=candidate.candidate_id,
            input_digest=input_digest,
            semantic_validator=lambda payload: material_packet_from_payload(
                payload,
                pack=pack,
                candidate=candidate,
                verified_job_text=verified_job_text,
            ),
        )

    def _input_digest(self, *, stage: str, inputs: dict[str, Any]) -> str:
        return _sha256_text(
            _canonical_json(
                {
                    "prompt_schema_version": PROMPT_SCHEMA_VERSION,
                    "stage": stage,
                    "inputs": inputs,
                }
            )
        )

    def _request_id(
        self,
        *,
        stage: str,
        input_digest: str,
        candidate_id: str = "",
    ) -> str:
        payload = {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "prompt_schema_version": PROMPT_SCHEMA_VERSION,
            "run_id": self.bindings.run_id,
            "stage": stage,
            "candidate_id": candidate_id,
            "input_digest": input_digest,
            "fact_digest": self.bindings.fact_digest,
            "context_digest": self.bindings.context_digest,
            "policy_digest": self.bindings.policy_digest,
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    def _paths(self, *, stage: str, candidate_id: str = "") -> tuple[Path, Path]:
        handoff_dir = self.run_dir / "handoff"
        suffix = f".{candidate_id}" if candidate_id else ""
        return (
            handoff_dir / f"{stage}{suffix}.request.json",
            handoff_dir / f"{stage}{suffix}.response.json",
        )

    def _ensure_request(
        self,
        *,
        stage: str,
        kind: str,
        request_id: str,
        prompt: str,
        input_digest: str,
        candidate_id: str = "",
    ) -> tuple[Path, Path]:
        if len(prompt) > self.ledger.budget.prompt_chars:
            raise BudgetExceeded(
                f"prompt exceeds character budget ({len(prompt)}/{self.ledger.budget.prompt_chars})"
            )
        request_path, response_path = self._paths(
            stage=stage,
            candidate_id=candidate_id,
        )
        request_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path = response_path.with_name(
            response_path.name.replace(".response.json", ".receipt.json")
        )
        envelope = {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "prompt_schema_version": PROMPT_SCHEMA_VERSION,
            "run_id": self.bindings.run_id,
            "stage": stage,
            "kind": kind,
            "request_id": request_id,
            "input_digest": input_digest,
            "candidate_id": candidate_id or None,
            "fact_digest": self.bindings.fact_digest,
            "context_digest": self.bindings.context_digest,
            "policy_digest": self.bindings.policy_digest,
            "prompt": prompt,
            "prompt_sha256": _sha256_text(prompt),
            "response_path": str(response_path.relative_to(self.run_dir)),
            "max_response_chars": self.ledger.budget.response_chars,
            "response_extraction": "assistant_dom_text_content",
            "response_format": (
                "natural_language_role_list"
                if kind == "role_candidates"
                else "strict_json"
            ),
            "raw_transcript_required": False,
        }
        created = False
        with _handoff_queue_lock(self.run_dir):
            if request_path.exists():
                self._validate_request_bindings(
                    request_path=request_path,
                    response_path=response_path,
                    stage=stage,
                    kind=kind,
                    request_id=request_id,
                    input_digest=input_digest,
                    candidate_id=candidate_id,
                )
                if receipt_path.exists():
                    if not response_path.exists():
                        raise ValueError("consumed ChatGPT response is missing")
                    self._validate_consumed_exchange(
                        request_path=request_path,
                        response_path=response_path,
                        receipt_path=receipt_path,
                        stage=stage,
                        kind=kind,
                        request_id=request_id,
                        input_digest=input_digest,
                        candidate_id=candidate_id,
                    )
                return request_path, response_path
            current = _active_handoffs_unlocked(
                run_dir=self.run_dir,
                bindings=self.bindings,
            )
            if len(current) > 1:
                raise ValueError("autonomy run has more than one active handoff exchange")
            if current:
                raise _pending_for_active(current[0])
            _write_immutable_json(request_path, envelope)
            created = True
        if created:
            self.ledger.lifecycle(
                phase="handoff_request",
                status="created",
                surface="chatgpt_web_artifact",
                detail={"kind": kind, "input_chars": len(prompt)},
            )
            self.ledger.lifecycle(
                phase="handoff_wait",
                status="started",
                surface="chatgpt_web_artifact",
                detail={"kind": kind},
            )
        return request_path, response_path

    def _validate_request_bindings(
        self,
        *,
        request_path: Path,
        response_path: Path,
        stage: str,
        kind: str,
        request_id: str,
        input_digest: str,
        candidate_id: str,
    ) -> None:
        request = _read_json_object(request_path)
        expected = {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "prompt_schema_version": PROMPT_SCHEMA_VERSION,
            "run_id": self.bindings.run_id,
            "stage": stage,
            "kind": kind,
            "request_id": request_id,
            "input_digest": input_digest,
            "candidate_id": candidate_id or None,
            "fact_digest": self.bindings.fact_digest,
            "context_digest": self.bindings.context_digest,
            "policy_digest": self.bindings.policy_digest,
            "response_path": str(response_path.relative_to(self.run_dir)),
        }
        if any(request.get(key) != value for key, value in expected.items()):
            raise ValueError("ChatGPT request inputs or bindings changed")
        prompt = str(request.get("prompt") or "")
        if not prompt or request.get("prompt_sha256") != _sha256_text(prompt):
            raise ValueError("ChatGPT request prompt digest mismatch")

    def _validate_consumed_exchange(
        self,
        *,
        request_path: Path,
        response_path: Path,
        receipt_path: Path,
        stage: str,
        kind: str,
        request_id: str,
        input_digest: str,
        candidate_id: str,
    ) -> None:
        self._validate_request_bindings(
            request_path=request_path,
            response_path=response_path,
            stage=stage,
            kind=kind,
            request_id=request_id,
            input_digest=input_digest,
            candidate_id=candidate_id,
        )
        receipt = _read_json_object(receipt_path)
        response = response_path.read_text(encoding="utf-8")
        if (
            receipt.get("schema_version") != HANDOFF_SCHEMA_VERSION
            or receipt.get("prompt_schema_version") != PROMPT_SCHEMA_VERSION
            or receipt.get("request_id") != request_id
            or receipt.get("input_digest") != input_digest
            or receipt.get("response_sha256") != _sha256_text(response)
        ):
            raise ValueError("consumed ChatGPT response receipt mismatch")

    def _exchange(
        self,
        *,
        stage: str,
        operation: str,
        kind: str,
        request_id: str,
        prompt: str,
        input_digest: str,
        semantic_validator: Any,
        candidate_id: str = "",
    ) -> Any:
        request_path, response_path = self._ensure_request(
            stage=stage,
            kind=kind,
            request_id=request_id,
            prompt=prompt,
            input_digest=input_digest,
            candidate_id=candidate_id,
        )
        bound_prompt = str(_read_json_object(request_path).get("prompt") or "")
        if not bound_prompt:
            raise ValueError("ChatGPT handoff request prompt is missing")
        if not response_path.exists():
            raise ChatGPTArtifactPending(
                kind=kind,
                request_id=request_id,
                request_path=request_path,
                response_path=response_path,
            )

        response = response_path.read_text(encoding="utf-8")
        receipt_path = response_path.with_name(
            response_path.name.replace(".response.json", ".receipt.json")
        )
        had_receipt = receipt_path.exists()
        recovered_rejection = _matching_rejected_response(response_path, response)
        if not had_receipt and recovered_rejection is None:
            self.ledger.reserve("model_calls")
            self.ledger.reserve("browser_navigations")
            self.ledger.reserve("external_calls")
        try:
            if len(response) > self.ledger.budget.response_chars:
                raise ChatGPTContractError("ChatGPT response exceeded character budget")
            payload = parse_chatgpt_response(
                response,
                expected_kind=kind,
                request_id=request_id,
            )
            if payload.get("request_id") != request_id:
                raise ChatGPTContractError("ChatGPT response request_id mismatch")
            validated = semantic_validator(payload)
            receipt = {
                "schema_version": HANDOFF_SCHEMA_VERSION,
                "prompt_schema_version": PROMPT_SCHEMA_VERSION,
                "request_id": request_id,
                "input_digest": input_digest,
                "response_sha256": _sha256_text(response),
            }
            derived_claim_count = int(
                getattr(validated, "derived_applicant_claim_count", 0)
            )
            if derived_claim_count:
                receipt["material_claim_binding"] = {
                    "mode": "deterministic_paragraph_evidence",
                    "derived_claim_count": derived_claim_count,
                }
            if recovered_rejection is not None:
                receipt["recovered_rejection"] = {
                    "artifact": recovered_rejection.name,
                    "response_sha256": _sha256_text(response),
                }
            _write_immutable_json(receipt_path, receipt)
        except Exception as exc:
            if recovered_rejection is not None:
                self.ledger.record_event(
                    stage=stage,
                    operation="revalidate_rejected_artifact",
                    surface="local_artifact_recovery",
                    status="error",
                    error_class=type(exc).__name__,
                )
            else:
                self.ledger.record_model_exchange(
                    stage=stage,
                    operation=operation,
                    surface="chatgpt_web_artifact",
                    request=bound_prompt,
                    response=response,
                    duration_ms=0,
                    status="error",
                    error_class=type(exc).__name__,
                )
            if not had_receipt and response_path.exists():
                _quarantine_rejected_response(response_path, response)
            raise
        if recovered_rejection is not None:
            self.ledger.record_event(
                stage=stage,
                operation="revalidate_rejected_artifact",
                surface="local_artifact_recovery",
                status="ok",
            )
        else:
            self.ledger.record_model_exchange(
                stage=stage,
                operation=operation,
                surface="chatgpt_web_artifact",
                request=bound_prompt,
                response=response,
                duration_ms=0,
            )
        if not had_receipt:
            self.ledger.lifecycle(
                phase="handoff_wait",
                status="complete",
                surface="chatgpt_web_artifact",
                detail={
                    "kind": kind,
                    "output_chars": len(response),
                    "wait_ms": _request_wait_ms(request_path),
                },
            )
        return validated


def import_response_artifact(*, request_path: Path, input_path: Path) -> dict[str, str]:
    """Normalize and atomically import one browser-produced response."""
    if request_path.is_symlink():
        raise ValueError("handoff request must not be a symbolic link")
    request_path = request_path.resolve(strict=True)
    if request_path.parent.name != "handoff" or not request_path.name.endswith(
        ".request.json"
    ):
        raise ValueError("handoff request path is not active")
    run_dir = request_path.parent.parent.resolve()
    text = input_path.read_text(encoding="utf-8")

    with _handoff_queue_lock(run_dir):
        request = _read_json_object(request_path)
        if request.get("schema_version") != HANDOFF_SCHEMA_VERSION:
            raise ValueError("unsupported ChatGPT handoff request")
        expected_kind = str(request.get("kind") or "")
        request_id = str(request.get("request_id") or "")
        prompt = str(request.get("prompt") or "")
        if not expected_kind or not request_id:
            raise ValueError("handoff request is incomplete")
        if expected_kind in {"role_candidates", "material_packet"}:
            if not prompt or request.get("prompt_sha256") != _sha256_text(prompt):
                raise ValueError("ChatGPT handoff prompt digest mismatch")

        raw_target = run_dir / str(request.get("response_path") or "")
        if raw_target.is_symlink():
            raise ValueError("handoff response must not be a symbolic link")
        target = raw_target.resolve()
        if target.parent != request_path.parent or not target.name.endswith(".response.json"):
            raise ValueError("ChatGPT response path escaped the handoff directory")
        bindings = RunBindings(
            run_id=str(request.get("run_id") or ""),
            fact_digest=str(request.get("fact_digest") or ""),
            context_digest=str(request.get("context_digest") or ""),
            policy_digest=str(request.get("policy_digest") or ""),
        )
        if any(
            not value
            for value in (
                bindings.run_id,
                bindings.fact_digest,
                bindings.context_digest,
                bindings.policy_digest,
            )
        ):
            raise ValueError("handoff request is missing its run bindings")
        journal = EventJournal(run_dir / "events.ndjson", run_id=bindings.run_id)
        surface = (
            "chatgpt_web_artifact"
            if expected_kind in {"role_candidates", "material_packet"}
            else "browser_tool_artifact"
        )
        journal.emit(
            component="model_or_browser",
            phase="handoff_response",
            status="imported",
            source=surface,
            detail={"kind": expected_kind, "output_chars": len(text)},
        )
        journal.emit(
            component="model_or_browser",
            phase="handoff_validation",
            status="started",
            source=surface,
            detail={"kind": expected_kind},
        )
        if not target.exists():
            current = _active_handoffs_unlocked(run_dir=run_dir, bindings=bindings)
            if len(current) != 1 or current[0].request_path != request_path:
                raise ValueError("handoff response does not target the single active exchange")

        max_chars = int(request.get("max_response_chars") or 0)
        try:
            if max_chars <= 0 or len(text) > max_chars:
                raise ValueError("ChatGPT response exceeded the request limit")
            if expected_kind in {"role_candidates", "material_packet"}:
                payload = parse_chatgpt_response(
                    text,
                    expected_kind=expected_kind,
                    request_id=request_id,
                )
                if payload.get("request_id") != request_id:
                    raise ChatGPTContractError("ChatGPT response request_id mismatch")
            elif expected_kind == "form_review":
                from applypilot.autonomy.form_handoff import validate_form_review_response

                payload = validate_form_review_response(json.loads(text), request=request)
            elif expected_kind in {
                "handshake_job_observations",
                "runway_job_observations",
            }:
                from applypilot.aggregation.portal_handoff import validate_portal_response

                payload = validate_portal_response(json.loads(text), request=request).to_dict()
            elif expected_kind == "startup_opportunities":
                from applypilot.opportunities.research import validate_research_response

                payload = validate_research_response(json.loads(text), request=request)
            else:
                raise ValueError(f"unsupported handoff response kind: {expected_kind}")
            if expected_kind == "role_candidates":
                role_candidates_from_payload(
                    payload,
                    limit=max(1, len(payload.get("items") or [])),
                )
            elif expected_kind == "material_packet":
                candidate_id = str(request.get("candidate_id") or "")
                if not candidate_id or payload.get("candidate_id") != candidate_id:
                    raise ChatGPTContractError("material response candidate_id mismatch")
        except Exception as exc:
            journal.emit(
                component="model_or_browser",
                phase="handoff_validation",
                status="error",
                source=surface,
                detail={"error_class": type(exc).__name__, "output_chars": len(text)},
            )
            _record_rejected_import(target, text, kind=expected_kind)
            raise

        journal.emit(
            component="model_or_browser",
            phase="handoff_validation",
            status="complete",
            source=surface,
            detail={"kind": expected_kind, "output_chars": len(text)},
        )

        canonical = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        if target.exists():
            existing = _read_json_object(target)
            if _canonical_json(existing) != _canonical_json(payload):
                raise FileExistsError("a different response is already bound to this request")
        else:
            _atomic_write_text(target, canonical)
        return {
            "request_id": request_id,
            "request_path": str(request_path),
            "response_path": str(target),
            "response_sha256": _sha256_text(canonical),
        }


def _request_wait_ms(request_path: Path) -> int:
    """Return a bounded wall-clock age for one immutable request artifact."""
    stat_result = request_path.stat()
    created_at = float(getattr(stat_result, "st_birthtime", stat_result.st_mtime))
    now = datetime.now(timezone.utc).timestamp()
    return max(0, min(int((now - created_at) * 1000), 2_147_483_647))


def _write_immutable_json(path: Path, payload: dict[str, Any]) -> None:
    canonical = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if path.exists():
        existing = _read_json_object(path)
        if _canonical_json(existing) != _canonical_json(payload):
            raise FileExistsError(f"immutable handoff artifact changed: {path.name}")
        return
    _atomic_write_text(path, canonical)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        encoded = text.encode("utf-8")
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("handoff artifact write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _quarantine_rejected_response(path: Path, text: str) -> Path:
    """Preserve invalid model output while reopening the request for one correction."""
    digest = _sha256_text(text)[:16]
    rejected = path.with_name(
        path.name.replace(".response.json", f".rejected.{digest}.json")
    )
    if rejected.exists():
        if rejected.read_text(encoding="utf-8") != text:
            raise FileExistsError("rejected response quarantine collision")
        path.unlink()
    else:
        os.replace(path, rejected)
    return rejected


def _record_rejected_import(path: Path, text: str, *, kind: str) -> Path:
    """Persist bounded rejection evidence without copying surprise response content."""
    digest = _sha256_text(text)
    rejected = path.with_name(
        path.name.replace(".response.json", f".rejected.{digest[:16]}.json")
    )
    _write_immutable_json(
        rejected,
        {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "kind": kind,
            "status": "rejected_import",
            "response_chars": len(text),
            "response_sha256": digest,
        },
    )
    return rejected


def _matching_rejected_response(response_path: Path, response: str) -> Path | None:
    response_digest = _sha256_text(response)
    prefix = response_path.name.removesuffix(".response.json")
    for rejected_path in sorted(response_path.parent.glob(f"{prefix}.rejected.*.json")):
        rejected_text = rejected_path.read_text(encoding="utf-8")
        rejected_digest = _sha256_text(rejected_text)
        try:
            rejected_record = json.loads(rejected_text)
        except json.JSONDecodeError:
            rejected_record = None
        if (
            isinstance(rejected_record, dict)
            and rejected_record.get("status") == "rejected_import"
        ):
            rejected_digest = str(
                rejected_record.get("response_sha256") or rejected_digest
            )
        if rejected_digest == response_digest:
            return rejected_path
    return None


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

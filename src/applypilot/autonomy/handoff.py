"""Portable ChatGPT Web request/response handoff artifacts.

The browser agent is intentionally a tiny transport: it reads one bounded
request, sends the embedded prompt in a fresh ChatGPT Web conversation, and
returns one strict JSON object.  All parsing, policy, verification, scoring,
and persistence remain in ApplyPilot.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from applypilot.autonomy.chatgpt_web import (
    ChatGPTContractError,
    material_packet_from_payload,
    parse_chatgpt_json,
    role_candidates_from_payload,
)
from applypilot.autonomy.context import (
    CompactContextPack,
    build_discovery_prompt,
    build_material_prompt,
)
from applypilot.autonomy.models import MaterialPacket, RoleCandidate
from applypilot.autonomy.telemetry import BudgetExceeded, UsageLedger

HANDOFF_SCHEMA_VERSION = "applypilot.handoff.v1"
RUN_SCHEMA_VERSION = "applypilot.autonomy-run.v1"
PROMPT_SCHEMA_VERSION = "applypilot.chatgpt-prompt.v4"


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
            "raw_transcript_required": False,
        }
        _write_immutable_json(request_path, envelope)
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
        if not had_receipt:
            self.ledger.reserve("model_calls")
            self.ledger.reserve("browser_navigations")
            self.ledger.reserve("external_calls")
        try:
            if len(response) > self.ledger.budget.response_chars:
                raise ChatGPTContractError("ChatGPT response exceeded character budget")
            payload = parse_chatgpt_json(response, expected_kind=kind)
            if payload.get("request_id") != request_id:
                raise ChatGPTContractError("ChatGPT response request_id mismatch")
            validated = semantic_validator(payload)
            _write_immutable_json(
                receipt_path,
                {
                    "schema_version": HANDOFF_SCHEMA_VERSION,
                    "prompt_schema_version": PROMPT_SCHEMA_VERSION,
                    "request_id": request_id,
                    "input_digest": input_digest,
                    "response_sha256": _sha256_text(response),
                },
            )
        except Exception as exc:
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
        self.ledger.record_model_exchange(
            stage=stage,
            operation=operation,
            surface="chatgpt_web_artifact",
            request=bound_prompt,
            response=response,
            duration_ms=0,
        )
        return validated


def import_response_artifact(*, request_path: Path, input_path: Path) -> dict[str, str]:
    """Validate and atomically import one browser-produced JSON response."""
    request_path = request_path.resolve()
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

    run_dir = request_path.parent.parent.resolve()
    target = (run_dir / str(request.get("response_path") or "")).resolve()
    if target.parent != request_path.parent or not target.name.endswith(".response.json"):
        raise ValueError("ChatGPT response path escaped the handoff directory")
    run_id = str(request.get("run_id") or "")
    if not run_id:
        raise ValueError("handoff request is missing its run binding")
    text = input_path.read_text(encoding="utf-8")
    max_chars = int(request.get("max_response_chars") or 0)
    try:
        if max_chars <= 0 or len(text) > max_chars:
            raise ValueError("ChatGPT response exceeded the request limit")
        if expected_kind in {"role_candidates", "material_packet"}:
            payload = parse_chatgpt_json(text, expected_kind=expected_kind)
            if payload.get("request_id") != request_id:
                raise ChatGPTContractError("ChatGPT response request_id mismatch")
        elif expected_kind == "form_review":
            from applypilot.autonomy.form_handoff import validate_form_review_response

            payload = validate_form_review_response(json.loads(text), request=request)
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
    except Exception:
        _record_rejected_import(target, text, kind=expected_kind)
        raise

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
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


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


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

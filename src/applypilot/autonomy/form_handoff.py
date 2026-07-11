"""Read-only application-form review through a browser-tool artifact."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from applypilot.autonomy.handoff import (
    BrowserArtifactPending,
    HANDOFF_SCHEMA_VERSION,
    RunBindings,
    _canonical_json,
    _sha256_text,
    _write_immutable_json,
)
from applypilot.autonomy.models import MaterialPacket, RoleCandidate
from applypilot.autonomy.telemetry import UsageLedger

FORM_REVIEW_SCHEMA_VERSION = "applypilot.form_review.v1"
FORM_REVIEW_KEYS = {
    "schema_version",
    "kind",
    "request_id",
    "candidate_id",
    "packet_digest",
    "status",
    "observed_url",
    "required_fields",
    "iframe_origins",
    "captcha_visible",
    "login_required",
    "account_creation_required",
    "form_filled",
    "file_uploaded",
    "submitted",
    "reason",
}
FORM_FIELD_KEYS = {"label", "name", "type", "required"}
TRUSTED_FORM_PROVIDER_HOSTS = {
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.ashbyhq.com",
    "jobs.lever.co",
    "jobs.smartrecruiters.com",
}
TRUSTED_FORM_PROVIDER_SUFFIXES = {
    "myworkday.com",
    "myworkdayjobs.com",
}


class ArtifactFormReviewer:
    """Pause for a read-only browser inspection, then consume its bounded result."""

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

    def dry_run(self, *, candidate: RoleCandidate, packet: MaterialPacket) -> dict[str, Any]:
        request_id = self._request_id(candidate=candidate, packet=packet)
        request_path, response_path = self._paths(candidate.candidate_id)
        envelope = {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "response_schema_version": FORM_REVIEW_SCHEMA_VERSION,
            "surface": "browser_tool",
            "run_id": self.bindings.run_id,
            "stage": "form_review",
            "kind": "form_review",
            "request_id": request_id,
            "candidate_id": candidate.candidate_id,
            "packet_digest": packet.digest,
            "fact_digest": self.bindings.fact_digest,
            "context_digest": self.bindings.context_digest,
            "policy_digest": self.bindings.policy_digest,
            "official_url": candidate.official_url,
            "response_path": str(response_path.relative_to(self.run_dir)),
            "max_response_chars": min(40_000, self.ledger.budget.response_chars),
            "browser_instructions": [
                "Use the already-authenticated real Chrome session.",
                "Navigate only to official_url. You may follow one visible Apply control to reach the form surface.",
                "Inspect visible field labels, types, requirements, iframe origins, login state, and challenge state only.",
                "Do not read existing field values, cookies, storage, history, passwords, or unrelated page content.",
                "Do not fill fields, check controls, upload files, sign in, create an account, or submit.",
                "Return one bare JSON object matching response_contract.",
            ],
            "response_contract": {
                "schema_version": FORM_REVIEW_SCHEMA_VERSION,
                "kind": "form_review",
                "request_id": request_id,
                "candidate_id": candidate.candidate_id,
                "packet_digest": packet.digest,
                "status": "form_surface_reviewed or blocked",
                "observed_url": "string",
                "required_fields": [
                    {
                        "label": "string",
                        "name": "string",
                        "type": "string",
                        "required": True,
                    }
                ],
                "iframe_origins": ["https://origin.example"],
                "captcha_visible": False,
                "login_required": False,
                "account_creation_required": False,
                "form_filled": False,
                "file_uploaded": False,
                "submitted": False,
                "reason": "string",
            },
        }
        _write_immutable_json(request_path, envelope)
        if not response_path.exists():
            raise BrowserArtifactPending(
                kind="form_review",
                request_id=request_id,
                request_path=request_path,
                response_path=response_path,
            )

        response_text = response_path.read_text(encoding="utf-8")
        if len(response_text) > envelope["max_response_chars"]:
            raise ValueError("form-review response exceeded the request limit")
        payload = validate_form_review_response(
            json.loads(response_text),
            request=envelope,
        )
        receipt_path = response_path.with_name(
            response_path.name.replace(".response.json", ".receipt.json")
        )
        had_receipt = receipt_path.exists()
        _write_immutable_json(
            receipt_path,
            {
                "schema_version": HANDOFF_SCHEMA_VERSION,
                "request_id": request_id,
                "response_sha256": _sha256_text(response_text),
            },
        )
        if not had_receipt:
            self.ledger.reserve("browser_navigations")
            self.ledger.reserve("external_calls")
        self.ledger.record_event(
            stage="form_review",
            operation="inspect_form_surface",
            surface="browser_tool",
            status="ok" if payload["status"] == "form_surface_reviewed" else "gap",
            error_class=str(payload.get("reason") or "")[:160],
        )
        return _normalized_review(payload)

    def _request_id(self, *, candidate: RoleCandidate, packet: MaterialPacket) -> str:
        payload = {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "run_id": self.bindings.run_id,
            "stage": "form_review",
            "candidate_id": candidate.candidate_id,
            "official_url": candidate.official_url,
            "packet_digest": packet.digest,
            "fact_digest": self.bindings.fact_digest,
            "context_digest": self.bindings.context_digest,
            "policy_digest": self.bindings.policy_digest,
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    def _paths(self, candidate_id: str) -> tuple[Path, Path]:
        handoff_dir = self.run_dir / "handoff"
        return (
            handoff_dir / f"form_review.{candidate_id}.request.json",
            handoff_dir / f"form_review.{candidate_id}.response.json",
        )


def validate_form_review_response(
    payload: Any,
    *,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Validate a browser result without accepting field values or side effects."""
    if not isinstance(payload, dict):
        raise ValueError("form-review response must be a JSON object")
    expected = {
        "schema_version": FORM_REVIEW_SCHEMA_VERSION,
        "kind": "form_review",
        "request_id": request.get("request_id"),
        "candidate_id": request.get("candidate_id"),
        "packet_digest": request.get("packet_digest"),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("form-review response bindings changed")
    extra_keys = sorted(set(payload) - FORM_REVIEW_KEYS)
    if extra_keys:
        raise ValueError(f"unexpected form-review fields: {extra_keys}")
    if payload.get("status") not in {"form_surface_reviewed", "blocked"}:
        raise ValueError("form-review status is invalid")
    boolean_keys = (
        "captcha_visible",
        "login_required",
        "account_creation_required",
        "form_filled",
        "file_uploaded",
        "submitted",
    )
    for key in boolean_keys:
        if not isinstance(payload.get(key), bool):
            raise ValueError(f"form-review {key} must be boolean")
    for key in ("form_filled", "file_uploaded", "submitted"):
        if payload.get(key) is not False:
            raise ValueError(f"form-review response must prove {key}=false")
    if payload["status"] == "form_surface_reviewed" and any(
        payload[key]
        for key in ("captcha_visible", "login_required", "account_creation_required")
    ):
        raise ValueError("successful form review cannot require a challenge, login, or account")

    observed_url = str(payload.get("observed_url") or "")
    official_url = str(request.get("official_url") or "")
    if not _url_is_structurally_public(official_url):
        raise ValueError("form-review request official_url is not public HTTP(S)")
    if payload["status"] == "form_surface_reviewed" and not observed_url:
        raise ValueError("successful form review requires observed_url")
    if observed_url and not _url_is_structurally_public(observed_url):
        raise ValueError("form-review observed_url is not public HTTP(S)")
    if observed_url and not _same_site_or_subdomain(official_url, observed_url):
        raise ValueError("form-review observed_url is unrelated to official_url")
    fields = payload.get("required_fields")
    if not isinstance(fields, list) or len(fields) > 100:
        raise ValueError("form-review required_fields must be a bounded list")
    for field in fields:
        if not isinstance(field, dict):
            raise ValueError("form-review field entries must be objects")
        extra_field_keys = sorted(set(field) - FORM_FIELD_KEYS)
        if extra_field_keys:
            raise ValueError(f"unexpected form-review field metadata: {extra_field_keys}")
        if not isinstance(field.get("required"), bool):
            raise ValueError("form-review field required state must be boolean")
        for key in ("label", "name", "type"):
            if len(str(field.get(key) or "")) > 200:
                raise ValueError("form-review field metadata is too large")
    origins = payload.get("iframe_origins")
    if not isinstance(origins, list) or len(origins) > 20:
        raise ValueError("form-review iframe_origins must be a bounded list")
    if any(len(str(origin)) > 300 for origin in origins):
        raise ValueError("form-review iframe origin is too large")
    if any(origin and not _url_is_structurally_public(str(origin)) for origin in origins):
        raise ValueError("form-review iframe origin is not public HTTP(S)")
    if payload["status"] == "form_surface_reviewed" and any(
        not _same_site_or_subdomain(official_url, str(origin))
        and not _trusted_form_provider_origin(str(origin))
        for origin in origins
        if origin
    ):
        raise ValueError("successful form review contains an untrusted iframe origin")
    return payload


def _normalized_review(payload: dict[str, Any]) -> dict[str, Any]:
    required_fields = payload.get("required_fields") or []
    return {
        "status": payload["status"],
        "observed_url": str(payload.get("observed_url") or "")[:500],
        "required_field_count": len(required_fields),
        "required_fields": [
            ":".join(
                part
                for part in (
                    str(field.get("type") or "")[:60],
                    str(field.get("label") or field.get("name") or "")[:120],
                )
                if part
            )
            for field in required_fields[:20]
        ],
        "iframe_origins": [str(origin)[:200] for origin in payload.get("iframe_origins") or []],
        "captcha_visible": bool(payload.get("captcha_visible")),
        "login_required": bool(payload.get("login_required")),
        "account_creation_required": bool(payload.get("account_creation_required")),
        "form_filled": False,
        "file_uploaded": False,
        "submitted": False,
        "reason": str(payload.get("reason") or "")[:300],
        "packet_digest": str(payload.get("packet_digest") or ""),
    }


def _url_is_structurally_public(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def _same_site_or_subdomain(expected_url: str, observed_url: str) -> bool:
    expected = (urlparse(expected_url).hostname or "").lower().removeprefix("www.")
    observed = (urlparse(observed_url).hostname or "").lower().removeprefix("www.")
    return bool(
        expected
        and observed
        and (
            expected == observed
            or expected.endswith(f".{observed}")
            or observed.endswith(f".{expected}")
        )
    )


def _trusted_form_provider_origin(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return host in TRUSTED_FORM_PROVIDER_HOSTS or any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in TRUSTED_FORM_PROVIDER_SUFFIXES
    )

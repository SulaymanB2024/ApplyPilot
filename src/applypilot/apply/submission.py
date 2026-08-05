"""Submission verification for browser-driven applications."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal


SubmissionStatus = Literal["submitted_confirmed", "submitted_unconfirmed", "not_submitted"]

CONFIRMATION_ROUTE_RE = re.compile(r"(confirmation|thank[-_]?you|submitted|complete)", re.I)
CONFIRMATION_TEXT_RE = re.compile(
    r"(application (submitted|received)|thank you for applying|thanks for applying|we received your application)",
    re.I,
)
CONFIRMATION_NUMBER_RE = re.compile(
    r"\b(?:confirmation|application|reference)\s*(?:number|id|#)\s*[:#]?\s*([A-Z0-9-]{5,})",
    re.I,
)


@dataclass(frozen=True)
class ResponseEvidence:
    """Normalized response evidence from a submit request."""

    url: str = ""
    method: str = ""
    status: int | None = None
    body: Any | None = None


@dataclass(frozen=True)
class SubmissionEvidence:
    """All signals used to classify a submission result."""

    response: ResponseEvidence | None = None
    before_url: str = ""
    after_url: str = ""
    dom_text: str = ""
    validation_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubmissionVerification:
    """Tri-state submission outcome with supporting evidence."""

    status: SubmissionStatus
    reason: str
    confidence: str
    evidence: tuple[str, ...] = ()
    confirmation_number: str = ""


def is_probable_submit_response(response: Any) -> bool:
    """Return whether a Playwright response looks related to form submission."""
    try:
        method = response.request.method.upper()
        url = response.url.lower()
    except Exception:
        return False
    if method not in {"POST", "PUT", "PATCH"}:
        return False
    ignored_suffixes = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff", ".woff2")
    return not url.endswith(ignored_suffixes)


def verify_submission(
    page: Any,
    *,
    response: Any | None = None,
    before_url: str = "",
) -> SubmissionVerification:
    """Verify submit outcome using network, URL, DOM, and validation-error signals."""
    evidence = SubmissionEvidence(
        response=normalize_response(response),
        before_url=before_url,
        after_url=getattr(page, "url", ""),
        dom_text=_page_text(page),
        validation_errors=_validation_errors(page),
    )
    return classify_submission(evidence)


def classify_submission(evidence: SubmissionEvidence) -> SubmissionVerification:
    """Classify a submission from normalized evidence."""
    evidence_lines: list[str] = []
    if evidence.validation_errors:
        return SubmissionVerification(
            status="not_submitted",
            reason="validation_errors",
            confidence="failed_closed",
            evidence=tuple(evidence.validation_errors),
        )

    response_ok = _response_ok(evidence.response)
    response_failed = _response_failed(evidence.response)
    if response_failed:
        return SubmissionVerification(
            status="not_submitted",
            reason="submit_response_failed",
            confidence="failed_closed",
            evidence=(f"response={evidence.response.status}",),
        )
    if response_ok:
        evidence_lines.append(f"response={evidence.response.status}")

    url_confirmed = bool(evidence.after_url and CONFIRMATION_ROUTE_RE.search(evidence.after_url))
    if url_confirmed:
        evidence_lines.append(f"url={evidence.after_url}")

    dom_confirmed = bool(CONFIRMATION_TEXT_RE.search(evidence.dom_text))
    confirmation_number = _confirmation_number(evidence.dom_text)
    if dom_confirmed:
        evidence_lines.append("dom=confirmation_text")
    if confirmation_number:
        evidence_lines.append(f"confirmation_number={confirmation_number}")

    if response_ok and (url_confirmed or dom_confirmed or confirmation_number):
        return SubmissionVerification(
            status="submitted_confirmed",
            reason="submit_response_confirmed",
            confidence="confirmed",
            evidence=tuple(evidence_lines),
            confirmation_number=confirmation_number,
        )
    if url_confirmed and (dom_confirmed or confirmation_number):
        return SubmissionVerification(
            status="submitted_confirmed",
            reason="route_and_dom_confirmed",
            confidence="confirmed",
            evidence=tuple(evidence_lines),
            confirmation_number=confirmation_number,
        )
    if response_ok or url_confirmed or dom_confirmed or confirmation_number:
        return SubmissionVerification(
            status="submitted_unconfirmed",
            reason="partial_submission_evidence",
            confidence="unconfirmed",
            evidence=tuple(evidence_lines),
            confirmation_number=confirmation_number,
        )
    return SubmissionVerification(
        status="not_submitted",
        reason="no_confirmation",
        confidence="failed_closed",
        evidence=tuple(evidence_lines),
    )


def normalize_response(response: Any | None) -> ResponseEvidence | None:
    """Convert a Playwright response or test double into response evidence."""
    if response is None:
        return None
    if isinstance(response, ResponseEvidence):
        return response

    url = str(getattr(response, "url", "") or "")
    status = getattr(response, "status", None)
    method = ""
    try:
        method = str(response.request.method)
    except Exception:
        method = str(getattr(response, "method", "") or "")
    body: Any | None = None
    try:
        body = response.json()
    except Exception:
        try:
            text = response.text()
            body = json.loads(text) if text and text.strip().startswith(("{", "[")) else text
        except Exception:
            body = None
    return ResponseEvidence(url=url, method=method, status=status, body=body)


def _response_ok(response: ResponseEvidence | None) -> bool:
    if response is None or response.status not in {200, 201, 202, 204}:
        return False
    body = response.body
    if isinstance(body, dict):
        if body.get("errors"):
            return False
        data = body.get("data")
        if isinstance(data, dict) and data.get("errors"):
            return False
    return True


def _response_failed(response: ResponseEvidence | None) -> bool:
    if response is None:
        return False
    if response.status is not None and response.status >= 400:
        return True
    body = response.body
    return isinstance(body, dict) and bool(body.get("errors"))


def _validation_errors(page: Any) -> tuple[str, ...]:
    errors: list[str] = []
    try:
        invalid_count = page.locator('[aria-invalid="true"]').count()
        if invalid_count:
            errors.append(f"aria_invalid={invalid_count}")
    except Exception:
        pass
    try:
        alerts = page.locator('[role="alert"], .error, .field-error')
        count = min(alerts.count(), 5)
        for idx in range(count):
            text = alerts.nth(idx).inner_text(timeout=1000).strip()
            if text:
                errors.append(f"alert={text[:120]}")
    except Exception:
        pass
    return tuple(errors)


def _page_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


def _confirmation_number(text: str) -> str:
    match = CONFIRMATION_NUMBER_RE.search(text)
    return match.group(1) if match else ""

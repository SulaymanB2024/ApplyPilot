"""Read-only application-form surface review."""

from __future__ import annotations

import time
from typing import Any, Callable

from applypilot.apply.safety import classify_page_state_with_evidence, inspect_page_state
from applypilot.autonomy.first_party import assert_public_http_url
from applypilot.autonomy.models import MaterialPacket, RoleCandidate
from applypilot.autonomy.telemetry import UsageLedger


class ReadOnlyFormReviewer:
    """Inspect a first-party form without filling, uploading, or submitting."""

    def __init__(
        self,
        *,
        page: Any,
        ledger: UsageLedger,
        timeout_ms: int = 45_000,
        url_guard: Callable[[str], None] = assert_public_http_url,
    ):
        self.page = page
        self.ledger = ledger
        self.timeout_ms = timeout_ms
        self.url_guard = url_guard

    def dry_run(self, *, candidate: RoleCandidate, packet: MaterialPacket) -> dict[str, Any]:
        del packet  # Materials are intentionally not transmitted during read-only review.
        self.ledger.reserve("browser_navigations")
        self.ledger.reserve("external_calls")
        self.url_guard(candidate.official_url)
        started = time.monotonic()
        route_installed = hasattr(self.page, "route") and hasattr(self.page, "unroute")

        def guard_document_navigation(route, request):
            if getattr(request, "resource_type", "") == "document":
                try:
                    self.url_guard(str(request.url))
                except Exception:
                    route.abort("blockedbyclient")
                    return
            route.continue_()

        if route_installed:
            self.page.route("**/*", guard_document_navigation)
        try:
            self.page.goto(
                candidate.official_url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
        finally:
            if route_installed:
                self.page.unroute("**/*", guard_document_navigation)
        state = inspect_page_state(self.page)
        verdict = classify_page_state_with_evidence(state)
        required = [
            {
                "type": field.type,
                "name": field.name[:100],
                "label": field.label[:160],
                "autocomplete": field.autocomplete[:100],
                "file_accept": field.accept[:100],
            }
            for field in state.inputs
            if field.required
        ][:50]
        result = {
            "status": "blocked" if verdict else "form_surface_reviewed",
            "reason": verdict.reason if verdict else "read_only_review_complete",
            "required_fields": required,
            "required_field_count": len(required),
            "personal_data_transmitted": False,
            "file_uploaded": False,
            "form_filled": False,
            "submitted": False,
        }
        self.ledger.record_event(
            stage="form_review",
            operation="inspect_form_surface",
            surface="browser",
            status=result["status"],
            duration_ms=int((time.monotonic() - started) * 1000),
            error_class=result["reason"] if verdict else "",
        )
        return result

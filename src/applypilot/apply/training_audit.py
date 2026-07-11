"""Training coverage audit for the apply-agent harness."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from applypilot.config import normalize_discovery_mode

EXPECTED_CAPABILITIES = (
    "workday_application_flow",
    "email_only_local_draft",
    "runway_fresh_role_discovery",
    "aggregator_to_employer_ats_handoff",
    "native_easy_apply_boundary",
    "external_ats_form_completion",
    "configured_job_board_catalog",
)

EXPECTED_SCENARIOS = (
    "Workday resume parser review",
    "Email-only application",
    "Runway fresh-role discovery",
    "Aggregator to employer ATS",
    "Native easy apply",
    "External ATS form",
)

EXPECTED_RESULT_CODES = (
    "submitted",
    "email_draft",
    "expired",
    "captcha",
    "login_issue",
    "generic_failure",
)
EXPECTED_VERSION = "apply-training-v1"
EMAIL_DRAFT_ARTIFACT = "email_application_draft.md"
RUNWAY_URL = "https://app.joinrunway.io/explore"


def _string_set(values: object) -> set[str]:
    if not isinstance(values, list | tuple | set):
        return set()
    return {str(value) for value in values}


def _list_of_mappings(values: object) -> list[Mapping[str, Any]]:
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, Mapping)]


def _source_has_runway(sources: list[Mapping[str, Any]]) -> bool:
    return any(
        str(source.get("name", "")).lower() == "runway"
        and str(source.get("url", "")) == RUNWAY_URL
        for source in sources
    )


def audit_training_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic coverage audit for an apply training manifest."""
    capabilities = _string_set(manifest.get("required_capabilities"))
    scenario_names = _string_set(manifest.get("scenario_names"))
    result_codes = manifest.get("result_codes")
    if not isinstance(result_codes, Mapping):
        result_codes = {}

    sources = _list_of_mappings(manifest.get("smart_extract_sources"))
    boards = _list_of_mappings(manifest.get("jobspy_boards"))
    discovery_mode = normalize_discovery_mode(str(manifest.get("discovery_mode", "hybrid")))
    manual_ats = manifest.get("manual_ats_domains")
    if not isinstance(manual_ats, list):
        manual_ats = []

    missing_capabilities = [
        capability for capability in EXPECTED_CAPABILITIES if capability not in capabilities
    ]
    missing_scenarios = [
        scenario for scenario in EXPECTED_SCENARIOS if scenario not in scenario_names
    ]
    missing_result_codes = [
        code for code in EXPECTED_RESULT_CODES if code not in result_codes
    ]
    boards_without_rules = sorted(
        str(board.get("label") or board.get("code") or "unknown")
        for board in boards
        if not board.get("has_rule")
    )

    has_runway_source = _source_has_runway(sources)
    email_draft_artifact_ok = manifest.get("email_draft_artifact") == EMAIL_DRAFT_ARTIFACT
    version_ok = manifest.get("version") == EXPECTED_VERSION
    if discovery_mode == "direct_sources":
        jobspy_board_status = "not_applicable"
    elif boards:
        jobspy_board_status = "pass"
    else:
        jobspy_board_status = "fail"

    failures = {
        "version": not version_ok,
        "missing_capabilities": bool(missing_capabilities),
        "missing_scenarios": bool(missing_scenarios),
        "missing_result_codes": bool(missing_result_codes),
        "runway_source": not has_runway_source,
        "email_draft_artifact": not email_draft_artifact_ok,
        "jobspy_boards": jobspy_board_status == "fail",
    }

    return {
        "passed": not any(failures.values()),
        "failures": failures,
        "missing_capabilities": missing_capabilities,
        "missing_scenarios": missing_scenarios,
        "missing_result_codes": missing_result_codes,
        "has_runway_source": has_runway_source,
        "runway_url": RUNWAY_URL,
        "email_draft_artifact_ok": email_draft_artifact_ok,
        "email_draft_artifact": EMAIL_DRAFT_ARTIFACT,
        "version_ok": version_ok,
        "expected_version": EXPECTED_VERSION,
        "discovery_mode": discovery_mode,
        "configured_jobspy_boards": len(boards),
        "jobspy_board_status": jobspy_board_status,
        "jobspy_boards_without_rules": boards_without_rules,
        "smart_extract_source_count": len(sources),
        "search_source_count": sum(1 for source in sources if source.get("type") == "search"),
        "static_source_count": sum(1 for source in sources if source.get("type") == "static"),
        "manual_ats_count": len(manual_ats),
    }

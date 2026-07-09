"""Deterministic reviewer for self-improvement proposals."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from applypilot.dev_harness.artifacts import read_json, utc_timestamp, write_json, write_text

EMAIL_SEND_MARKERS = (
    "send_email",
    "reply_email",
    "forward_email",
    "gmail__send",
    "external email",
    "external draft",
)
DRY_RUN_WEAKENING_MARKERS = (
    "dry_run_final_submit",
    "mark_dry_run_applied",
    "submit during dry-run",
    "submit during dry run",
)
FAIL_CLOSED_MARKERS = (
    "captcha bypass",
    "mfa bypass",
    "sso bypass",
    "payment bypass",
    "tax bypass",
    "identity bypass",
)
RECURSIVE_MARKERS = (
    "spawn_agent",
    "sub-agent",
    "subagent",
    "recursive worker",
    "duplicate sibling thread",
)
CONTEXT_BLOAT_MARKERS = (
    "raw transcript",
    "full transcript",
    "paste the entire chat",
    "load all docs",
    "read every file",
)


def review_proposal(*, proposal_path: Path) -> Path:
    """Review a proposal artifact and write review outputs."""
    proposal = read_json(proposal_path)
    plan = read_json(Path(proposal["plan_path"]))
    review = build_review(plan=plan, proposal=proposal)
    review["proposal_path"] = str(proposal_path)
    review_path = Path(plan["artifact_paths"]["review"])
    decision_path = Path(plan["artifact_paths"]["decision"])
    write_json(review_path, review)
    write_text(decision_path, _decision_markdown(review))
    return review_path


def build_review(*, plan: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic review results for a proposal."""
    findings: list[dict[str, str]] = []
    allowed_files = tuple(str(pattern) for pattern in plan.get("allowed_files", []))
    forbidden_models = {str(model).lower() for model in plan.get("models", {}).get("forbidden", [])}
    proposed_text = _flatten_text(proposal)

    for model in _proposal_models(proposal):
        if model.lower() in forbidden_models:
            findings.append(_finding("forbidden_model", f"Proposal uses forbidden model: {model}"))

    for path in proposal.get("touches_files", []):
        path_str = str(path)
        if not _is_allowed_file(path_str, allowed_files):
            findings.append(_finding("out_of_scope_file", f"Proposal touches out-of-scope file: {path_str}"))

    if _contains_any(proposed_text, EMAIL_SEND_MARKERS):
        findings.append(_finding("external_email_boundary", "Proposal mentions external email send/draft behavior."))
    if _contains_any(proposed_text, DRY_RUN_WEAKENING_MARKERS):
        findings.append(_finding("dry_run_boundary", "Proposal appears to weaken dry-run semantics."))
    if _contains_any(proposed_text, FAIL_CLOSED_MARKERS):
        findings.append(_finding("fail_closed_boundary", "Proposal appears to weaken fail-closed handling."))
    if bool(proposal.get("recursive_delegation")) or _contains_any(proposed_text, RECURSIVE_MARKERS):
        findings.append(_finding("recursive_delegation", "Proposal uses or requests recursive worker delegation."))
    if _contains_any(proposed_text, CONTEXT_BLOAT_MARKERS):
        findings.append(_finding("context_bloat", "Proposal bypasses progressive reveal with broad context loading."))

    validation_results = proposal.get("validation_results")
    has_validation_results = isinstance(validation_results, list) and bool(validation_results)
    validation_passed = has_validation_results and all(
        isinstance(result, dict) and result.get("exit_code") == 0
        for result in validation_results
    )
    if not has_validation_results:
        findings.append(_finding("missing_validation", "No deterministic validation results were supplied."))
    elif not validation_passed:
        findings.append(_finding("failed_validation", "One or more deterministic validations failed."))

    approved = not findings and validation_passed
    return {
        "version": plan.get("version"),
        "created_at": utc_timestamp(),
        "approved": approved,
        "ready_for_patch": approved,
        "findings": findings,
        "validation_passed": validation_passed,
        "plan_path": proposal.get("plan_path"),
    }


def _finding(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _proposal_models(proposal: dict[str, Any]) -> list[str]:
    models = proposal.get("models", {})
    if not isinstance(models, dict):
        return []
    return [str(model) for model in models.values() if model]


def _is_allowed_file(path: str, allowed_patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in allowed_patterns)


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(needle.lower() in lowered for needle in needles)


def _flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(_flatten_text(item) for item in value.values())
    if isinstance(value, list):
        return "\n".join(_flatten_text(item) for item in value)
    return str(value)


def _decision_markdown(review: dict[str, Any]) -> str:
    status = "approved" if review["approved"] else "not approved"
    lines = [f"# Improve Review: {status}", ""]
    if review["findings"]:
        lines.append("## Findings")
        lines.extend(f"- {finding['code']}: {finding['message']}" for finding in review["findings"])
    else:
        lines.append("No findings.")
    return "\n".join(lines)

"""Artifact-first CLI helpers for autonomous batches."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from applypilot import config
from applypilot.autonomy.batch import AutonomousBatch, BatchDependencies
from applypilot.autonomy.chatgpt_web import ChatGPTWebClient
from applypilot.autonomy.context import (
    CONTEXT_VERSION,
    CompactContextPack,
    build_context_pack,
    build_discovery_prompt,
    candidate_profile_from_data,
)
from applypilot.autonomy.direct_ats import DirectATSDiscovery
from applypilot.autonomy.facts import (
    REQUIRED_AUTONOMY_FACT_IDS,
    FactState,
    build_fact_ledger,
    confirmed_preferred_location_fact_ids,
    fact_ledger_from_dict,
    load_corrections,
    require_confirmed_facts,
)
from applypilot.autonomy.first_party import FirstPartyVerifier, configured_trusted_sources
from applypilot.autonomy.form_review import ReadOnlyFormReviewer
from applypilot.autonomy.form_handoff import ArtifactFormReviewer
from applypilot.autonomy.handoff import (
    ArtifactChatGPTClient,
    HANDOFF_SCHEMA_VERSION,
    PROMPT_SCHEMA_VERSION,
    RunBindings,
    RUN_SCHEMA_VERSION,
    reconcile_unanswered_handoffs,
)
from applypilot.autonomy.policy import FunnelBudget, RunPolicy, SourcePolicy
from applypilot.autonomy.supervisor import (
    runtime_gated_decision,
    runtime_observation_snapshot,
    runtime_semantic_state,
)
from applypilot.autonomy.telemetry import UsageLedger


RUN_STATUS_SCHEMA_VERSION = "applypilot-autonomy-status-v1"
RUN_COMPACT_STATUS_SCHEMA_VERSION = "applypilot-autonomy-supervisor-v1"
RUN_HEARTBEAT_SCHEMA_VERSION = "applypilot-autonomy-heartbeat-v1"
RUN_HEARTBEAT_NAME = "heartbeat.json"
RUN_HEARTBEAT_INTERVAL_SECONDS = 300
RUN_RESULT_STATUSES = frozenset(
    {
        "awaiting_chatgpt_web",
        "awaiting_browser_tool",
        "review_ready",
        "form_review_blocked",
        "no_eligible_verified_roles",
        "budget_exhausted",
        "failed_closed",
    }
)
RUN_RESULT_LIST_FIELDS = frozenset(
    {
        "pending_requests",
        "source_attempts",
        "discoveries",
        "eligibility",
        "freshness",
        "materials",
        "form_reviews",
        "final_actions",
        "blockers",
    }
)
RUN_RESULT_FIELDS = frozenset({"run_id", "status", "usage"}) | RUN_RESULT_LIST_FIELDS
RUN_PENDING_FIELDS = frozenset(
    {"surface", "kind", "request_id", "request_path", "response_path"}
)
ACCEPTED_FORM_REVIEW_STATUSES = frozenset(
    {"form_surface_reviewed", "dry_run_verified"}
)
RUN_DIRECTORY_PATTERN = re.compile(r"^\d{8}T\d{12}Z-[0-9a-f]{10}$")


def prepare_run(
    *,
    query: str,
    output_dir: Path,
    corrections_path: Path | None = None,
    policy: RunPolicy | None = None,
) -> dict[str, str]:
    """Write a compact, auditable run packet without network/browser actions."""
    active_policy = policy or RunPolicy()
    active_policy.validate()
    profile = config.load_profile()
    resume_text = config.RESUME_PATH.read_text(encoding="utf-8")
    corrections = load_corrections(corrections_path) if corrections_path else ()
    fact_ledger = build_fact_ledger(profile, resume_text=resume_text, corrections=corrections)
    context = build_context_pack(
        profile,
        resume_text=resume_text,
        job_text=query,
        fact_ledger=fact_ledger,
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_id = f"{stamp}-{fact_ledger.digest[:10]}"
    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    paths = {
        "run_dir": str(run_dir),
        "manifest": str(run_dir / "run_manifest.json"),
        "facts": str(run_dir / "fact_ledger.json"),
        "context": str(run_dir / "context_pack.json"),
        "policy": str(run_dir / "run_policy.json"),
        "fact_digest": fact_ledger.digest,
    }
    _write_json(Path(paths["facts"]), fact_ledger.to_dict())
    _write_json(Path(paths["context"]), context.to_dict())
    _write_json(Path(paths["policy"]), asdict(active_policy))
    manifest = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "approval_challenge": secrets.token_hex(32),
        "fact_digest": fact_ledger.digest,
        "context_digest": context.digest,
        "policy_digest": active_policy.digest,
        "immutable_artifacts": {
            path.name: _sha256_file(path)
            for path in (
                Path(paths["facts"]),
                Path(paths["context"]),
                Path(paths["policy"]),
            )
        },
    }
    _write_json(Path(paths["manifest"]), manifest)
    request_path = ArtifactChatGPTClient(
        run_dir=run_dir,
        bindings=RunBindings.from_manifest(manifest),
        ledger=UsageLedger(run_id=run_id, budget=active_policy.budget),
    ).prepare_discovery_request(
        pack=context,
        query=query,
        limit=active_policy.budget.discoveries,
    )
    relative_request = str(request_path.relative_to(run_dir))
    manifest["immutable_artifacts"][relative_request] = _sha256_file(request_path)
    _write_json(Path(paths["manifest"]), manifest)
    paths["request"] = str(request_path)
    return paths


def latest_autonomy_run_dir(*, root: Path | None = None) -> Path:
    """Resolve the newest immediate run without falling back past corrupt state."""
    candidate_root = (root or config.APP_DIR / "autonomy-runs").expanduser()
    if candidate_root.is_symlink():
        raise ValueError("autonomy run root must not be a symlink")
    try:
        resolved_root = candidate_root.resolve(strict=True)
    except OSError as exc:
        raise FileNotFoundError("autonomy run root does not exist") from exc
    if not resolved_root.is_dir():
        raise NotADirectoryError("autonomy run root is not a directory")

    candidates = sorted(
        (
            entry
            for entry in resolved_root.iterdir()
            if RUN_DIRECTORY_PATTERN.fullmatch(entry.name)
        ),
        key=lambda entry: entry.name,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError("no autonomy runs exist")
    selected = candidates[0]
    selected_stamp = selected.name.split("-", 1)[0]
    if len(candidates) > 1 and candidates[1].name.split("-", 1)[0] == selected_stamp:
        raise ValueError("newest autonomy run timestamp is ambiguous; use --run-dir")
    if selected.is_symlink():
        raise ValueError(f"autonomy run directory must not be a symlink: {selected.name}")
    if not selected.is_dir():
        raise ValueError(f"autonomy run path is not a directory: {selected.name}")
    resolved_run = selected.resolve(strict=True)
    if resolved_run.parent != resolved_root:
        raise ValueError("autonomy run directory escaped its configured root")
    manifest_path = resolved_run / "run_manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError(f"autonomy run is incomplete: {selected.name}")
    manifest = _read_json(manifest_path)
    bindings = RunBindings.from_manifest(manifest)
    if bindings.run_id != selected.name:
        raise ValueError("autonomy run directory differs from its manifest run id")
    created_at = _parse_aware_datetime(
        str(manifest.get("created_at") or ""),
        field="autonomy run creation timestamp",
    )
    if str(manifest.get("created_at") or "") != created_at.isoformat():
        raise ValueError("autonomy run creation timestamp must use canonical UTC")
    if created_at > datetime.now(timezone.utc):
        raise ValueError("autonomy run creation timestamp is in the future")
    return resolved_run


def run_status_snapshot(
    *,
    run_dir: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a redacted, immutable-run-checked pre-campaign status snapshot."""
    current = _aware_utc(now)
    run_dir = run_dir.resolve()
    manifest = _read_json(run_dir / "run_manifest.json")
    bindings = RunBindings.from_manifest(manifest)
    _verify_immutable_artifacts(run_dir, manifest)
    run_created_at = _parse_aware_datetime(
        str(manifest.get("created_at") or ""),
        field="autonomy run creation timestamp",
    )
    if run_created_at > current:
        raise ValueError("autonomy run creation timestamp is in the future")

    fact_ledger = fact_ledger_from_dict(_read_json(run_dir / "fact_ledger.json"))
    if fact_ledger.digest != bindings.fact_digest:
        raise ValueError("run manifest fact digest mismatch")
    required_fact_blockers = require_confirmed_facts(
        fact_ledger,
        REQUIRED_AUTONOMY_FACT_IDS,
    )
    location_fact_ids = confirmed_preferred_location_fact_ids(fact_ledger)
    state_counts = {
        state.value: sum(record.state is state for record in fact_ledger.records)
        for state in FactState
    }

    handoff = _run_handoff_status(run_dir=run_dir, bindings=bindings)
    result = _run_result_status(run_dir=run_dir, run_id=bindings.run_id)

    from applypilot.autonomy.approval import (
        FACT_APPROVAL_NAME,
        FACT_APPROVAL_SIGNATURE_NAME,
        FactApprovalError,
        require_system_approval_trust_store,
    )

    try:
        require_system_approval_trust_store()
    except FactApprovalError:
        trust_store_ready = False
    else:
        trust_store_ready = True
    approval_file_count = sum(
        (run_dir / name).is_file()
        for name in (FACT_APPROVAL_NAME, FACT_APPROVAL_SIGNATURE_NAME)
    )
    approval_files_present = approval_file_count == 2
    approval_verified = False
    approval_state = "missing" if approval_file_count == 0 else "incomplete"
    if approval_files_present:
        approval_state = "unverified"
        if trust_store_ready and not required_fact_blockers and location_fact_ids:
            try:
                load_reviewed_run_snapshot(
                    run_dir=run_dir,
                    approved_fact_digest=fact_ledger.digest,
                    require_signed_approval=True,
                )
            except Exception:
                approval_state = "invalid_or_stale"
            else:
                approval_verified = True
                approval_state = "verified"

    if required_fact_blockers:
        live_gate = "required_facts"
    elif not location_fact_ids:
        live_gate = "preferred_location"
    elif not trust_store_ready:
        live_gate = "system_approval_trust_store"
    elif not approval_verified:
        live_gate = "signed_fact_approval"
    else:
        live_gate = "ready_for_candidate_scoped_review"

    if result["present"] and result["reported_status"] == "budget_exhausted":
        review_phase = "reported_budget_exhausted"
    elif handoff["responded_unconsumed_count"]:
        review_phase = "response_ready_to_advance"
    elif handoff["pending_kinds"]:
        review_phase = "awaiting_" + str(handoff["pending_kinds"][0])
    elif result["present"]:
        review_phase = "reported_" + str(result["reported_status"])
    else:
        review_phase = "ready_to_advance"

    research_can_progress = _research_can_progress(review_phase)
    next_action_owner, next_action_code, browser_required = _supervisor_decision(
        live_gate=live_gate,
        review_phase=review_phase,
    )
    runtime_status = runtime_observation_snapshot(
        root=run_dir,
        scope_kind="run",
        scope_id=bindings.run_id,
        now=current,
    )
    next_action_owner, next_action_code, browser_required = runtime_gated_decision(
        next_action_owner=next_action_owner,
        next_action_code=next_action_code,
        browser_required=browser_required,
        runtime_status=runtime_status,
    )
    runtime_semantic = runtime_semantic_state(runtime_status)
    progress_fingerprint = _sha256_text(
        _canonical_json(
            {
                "run_id": bindings.run_id,
                "review_phase": review_phase,
                "live_gate": live_gate,
                "external_action_gate": live_gate,
                "research_can_progress": research_can_progress,
                "submitted_confirmed": 0,
                "fact_states": state_counts,
                "required_fact_blockers": required_fact_blockers,
                "preferred_location_fact_count": len(location_fact_ids),
                "system_approval_trust_store_ready": trust_store_ready,
                "fact_approval_state": approval_state,
                "handoff": handoff,
                "result": result,
                "next_action_owner": next_action_owner,
                "next_action_code": next_action_code,
                "browser_required": browser_required,
                "runtime": runtime_semantic,
            }
        )
    )

    heartbeat_record = _read_run_heartbeat(
        run_dir=run_dir,
        run_id=bindings.run_id,
        current=current,
        run_created_at=run_created_at,
    )
    last_heartbeat_at = heartbeat_record[0] if heartbeat_record is not None else None
    previous_status = heartbeat_record[1] if heartbeat_record is not None else {}
    state_changed = previous_status.get("progress_fingerprint") != progress_fingerprint
    if state_changed:
        last_progress_at = current
    else:
        last_progress_raw = str(previous_status.get("last_progress_at") or "")
        last_progress_at = _parse_aware_datetime(
            last_progress_raw,
            field="autonomy last-progress timestamp",
        )
        if last_progress_raw != last_progress_at.isoformat():
            raise ValueError("autonomy last-progress timestamp must use canonical UTC")
        if last_progress_at < run_created_at or (
            last_heartbeat_at is not None and last_progress_at > last_heartbeat_at
        ):
            raise ValueError("autonomy last-progress timestamp is outside its run")
    progress_age_seconds = max(0, int((current - last_progress_at).total_seconds()))
    heartbeat_due = True
    if last_heartbeat_at is not None:
        heartbeat_due = current >= last_heartbeat_at + timedelta(
            seconds=RUN_HEARTBEAT_INTERVAL_SECONDS
        )

    return {
        "schema_version": RUN_STATUS_SCHEMA_VERSION,
        "run_id": bindings.run_id,
        "recorded_at": current.isoformat(),
        "run_created_at": run_created_at.isoformat(),
        "review_phase": review_phase,
        "live_gate": live_gate,
        "external_action_gate": live_gate,
        "research_can_progress": research_can_progress,
        "next_action_owner": next_action_owner,
        "next_action_code": next_action_code,
        "browser_required": browser_required,
        "runtime_ready": runtime_status["runtime_ready"],
        "runtime_observation_state": runtime_status["observation_state"],
        "chronicle_state": runtime_status["chronicle_state"],
        "browser_surface": runtime_status["browser_surface"],
        "browser_readiness": runtime_status["browser_readiness"],
        "progress_fingerprint": progress_fingerprint,
        "state_changed": state_changed,
        "last_progress_at": last_progress_at.isoformat(),
        "progress_age_seconds": progress_age_seconds,
        "target_confirmed": 100,
        "submitted_confirmed": 0,
        "fact_states": state_counts,
        "required_fact_blockers": required_fact_blockers,
        "preferred_location_fact_count": len(location_fact_ids),
        "system_approval_trust_store_ready": trust_store_ready,
        "fact_approval_file_count": approval_file_count,
        "fact_approval_files_present": approval_files_present,
        "fact_approval_state": approval_state,
        "runtime_observation": runtime_status,
        "handoff": handoff,
        "result": result,
        "heartbeat_interval_seconds": RUN_HEARTBEAT_INTERVAL_SECONDS,
        "last_heartbeat_at": last_heartbeat_at.isoformat()
        if last_heartbeat_at is not None
        else None,
        "heartbeat_due": heartbeat_due,
        "external_side_effects": "none_from_status_tool",
    }


def compact_run_status(status: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded decision/liveness view for five-minute supervisors."""
    if status.get("schema_version") != RUN_STATUS_SCHEMA_VERSION:
        raise ValueError("autonomy status schema is invalid")
    fields = (
        "run_id",
        "recorded_at",
        "target_confirmed",
        "submitted_confirmed",
        "review_phase",
        "external_action_gate",
        "research_can_progress",
        "next_action_owner",
        "next_action_code",
        "browser_required",
        "runtime_ready",
        "runtime_observation_state",
        "chronicle_state",
        "browser_surface",
        "browser_readiness",
        "progress_fingerprint",
        "state_changed",
        "last_progress_at",
        "progress_age_seconds",
        "last_heartbeat_at",
        "heartbeat_due",
    )
    if any(field not in status for field in fields):
        raise ValueError("autonomy status is missing compact supervisor fields")
    return {
        "schema_version": RUN_COMPACT_STATUS_SCHEMA_VERSION,
        **{field: status[field] for field in fields},
        "state_trust": "local_diagnostic_not_submission_evidence",
    }


def record_run_heartbeat(
    *,
    run_dir: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fsync one fixed-name redacted pre-campaign heartbeat."""
    current = _aware_utc(now)
    run_dir = run_dir.resolve()
    status = run_status_snapshot(run_dir=run_dir, now=current)
    status["last_heartbeat_at"] = current.isoformat()
    status["heartbeat_due"] = False
    payload = {
        "schema_version": RUN_HEARTBEAT_SCHEMA_VERSION,
        "run_id": status["run_id"],
        "recorded_at": current.isoformat(),
        "status_sha256": _sha256_text(_canonical_json(status)),
        "status": status,
    }
    _write_fsynced_json(run_dir / RUN_HEARTBEAT_NAME, payload)
    return status


def load_reviewed_run_snapshot(
    *,
    run_dir: Path,
    approved_fact_digest: str,
    require_signed_approval: bool = False,
) -> dict[str, str]:
    """Validate one local run packet and return only its campaign-safe bindings."""
    run_dir = run_dir.resolve()
    manifest = _read_json(run_dir / "run_manifest.json")
    bindings = RunBindings.from_manifest(manifest)
    _verify_immutable_artifacts(run_dir, manifest)

    fact_ledger = fact_ledger_from_dict(_read_json(run_dir / "fact_ledger.json"))
    require_approved_fact_digest(fact_ledger.digest, approved_fact_digest)
    if require_signed_approval:
        _require_autonomy_ready_facts(fact_ledger)
    if fact_ledger.digest != bindings.fact_digest:
        raise ValueError("run manifest fact digest mismatch")

    profile = config.load_profile()
    resume_text = config.RESUME_PATH.read_text(encoding="utf-8")
    if _sha256_text(json.dumps(profile, sort_keys=True, ensure_ascii=False)) != fact_ledger.profile_sha256:
        raise ValueError("profile changed after the reviewed autonomy plan")
    if _sha256_text(resume_text) != fact_ledger.resume_sha256:
        raise ValueError("resume changed after the reviewed autonomy plan")

    context_pack = _context_from_dict(_read_json(run_dir / "context_pack.json"))
    if context_pack.digest != bindings.context_digest:
        raise ValueError("run manifest context digest mismatch")
    policy = _policy_from_dict(_read_json(run_dir / "run_policy.json"))
    if policy.digest != bindings.policy_digest:
        raise ValueError("run manifest policy digest mismatch")
    query = str(manifest.get("query") or "").strip()
    if not query:
        raise ValueError("autonomy run query is missing")
    _validate_discovery_request(
        run_dir=run_dir,
        manifest=manifest,
        query=query,
        discovery_limit=policy.budget.discoveries,
    )
    snapshot = {
        "run_id": bindings.run_id,
        "query": query,
        "fact_digest": bindings.fact_digest,
        "context_digest": bindings.context_digest,
        "policy_digest": bindings.policy_digest,
        "fact_approval_receipt_sha256": "",
        "fact_approval_signature_sha256": "",
        "approval_issuer": "",
        "approval_trust_store_sha256": "",
        "fact_approval_expires_at": "",
    }
    if require_signed_approval:
        from applypilot.autonomy.approval import (
            FactApprovalExpectation,
            load_verified_fact_approval,
            require_system_approval_trust_store,
        )

        expectation = FactApprovalExpectation.from_run(
            manifest=manifest,
            manifest_sha256=_sha256_file(run_dir / "run_manifest.json"),
            fact_ledger=fact_ledger,
        )
        approval = load_verified_fact_approval(
            run_dir=run_dir,
            expectation=expectation,
            trust_store_path=require_system_approval_trust_store(),
        )
        snapshot.update(
            {
                "fact_approval_receipt_sha256": approval.receipt_sha256,
                "fact_approval_signature_sha256": approval.signature_sha256,
                "approval_issuer": approval.issuer,
                "approval_trust_store_sha256": approval.trust_store_sha256,
                "fact_approval_expires_at": approval.expires_at,
            }
        )
    return snapshot


def import_signed_fact_approval(
    *,
    run_dir: Path,
    approved_fact_digest: str,
    attestation_path: Path,
    signature_path: Path,
) -> dict[str, str]:
    """Verify and import a user-signed fact approval into one immutable run."""
    load_reviewed_run_snapshot(
        run_dir=run_dir,
        approved_fact_digest=approved_fact_digest,
    )
    run_dir = run_dir.resolve()
    manifest = _read_json(run_dir / "run_manifest.json")
    fact_ledger = fact_ledger_from_dict(_read_json(run_dir / "fact_ledger.json"))
    _require_autonomy_ready_facts(fact_ledger)
    from applypilot.autonomy.approval import (
        FactApprovalExpectation,
        require_system_approval_trust_store,
        verify_and_import_fact_approval,
    )

    expectation = FactApprovalExpectation.from_run(
        manifest=manifest,
        manifest_sha256=_sha256_file(run_dir / "run_manifest.json"),
        fact_ledger=fact_ledger,
    )
    approval = verify_and_import_fact_approval(
        run_dir=run_dir,
        expectation=expectation,
        attestation_path=attestation_path,
        signature_path=signature_path,
        trust_store_path=require_system_approval_trust_store(),
    )
    return {
        "issuer": approval.issuer,
        "receipt_sha256": approval.receipt_sha256,
        "signature_sha256": approval.signature_sha256,
        "trust_store_sha256": approval.trust_store_sha256,
    }


def prepare_fact_approval_attestation(
    *,
    run_dir: Path,
    approved_fact_digest: str,
    issuer: str,
    source_surface: str,
    source_message_sha256: str,
    source_author_sha256: str,
    source_observed_at: datetime,
    valid_hours: int,
    output_path: Path,
) -> dict[str, str]:
    """Prepare unsigned, run-bound bytes for user-controlled OpenSSH signing."""
    load_reviewed_run_snapshot(
        run_dir=run_dir,
        approved_fact_digest=approved_fact_digest,
    )
    if not 1 <= valid_hours <= 168:
        raise ValueError("fact approval validity must be between 1 and 168 hours")
    run_dir = run_dir.resolve()
    manifest = _read_json(run_dir / "run_manifest.json")
    fact_ledger = fact_ledger_from_dict(_read_json(run_dir / "fact_ledger.json"))
    _require_autonomy_ready_facts(fact_ledger)
    from applypilot.autonomy.approval import (
        FactApprovalExpectation,
        approval_json_bytes,
        build_unsigned_fact_approval,
        write_unsigned_fact_approval,
    )

    expectation = FactApprovalExpectation.from_run(
        manifest=manifest,
        manifest_sha256=_sha256_file(run_dir / "run_manifest.json"),
        fact_ledger=fact_ledger,
    )
    issued_at = datetime.now(timezone.utc)
    payload = build_unsigned_fact_approval(
        expectation,
        issuer=issuer,
        source_surface=source_surface,
        source_message_sha256=source_message_sha256,
        source_author_sha256=source_author_sha256,
        source_observed_at=source_observed_at,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(hours=valid_hours),
    )
    path = write_unsigned_fact_approval(output_path, payload)
    return {
        "attestation_path": str(path),
        "attestation_sha256": _sha256_text(approval_json_bytes(payload).decode("utf-8")),
        "signature_namespace": "applypilot-fact-approval",
    }


def advance_artifact_run(
    *,
    run_dir: Path,
    approved_fact_digest: str,
    verifier: Any | None = None,
) -> dict[str, Any]:
    """Advance one reviewed run using portable ChatGPT Web artifacts."""
    run_dir = run_dir.resolve()
    manifest = _read_json(run_dir / "run_manifest.json")
    bindings = RunBindings.from_manifest(manifest)
    _verify_immutable_artifacts(run_dir, manifest)

    fact_ledger = fact_ledger_from_dict(_read_json(run_dir / "fact_ledger.json"))
    require_approved_fact_digest(fact_ledger.digest, approved_fact_digest)
    if fact_ledger.digest != bindings.fact_digest:
        raise ValueError("run manifest fact digest mismatch")

    profile = config.load_profile()
    resume_text = config.RESUME_PATH.read_text(encoding="utf-8")
    if _sha256_text(json.dumps(profile, sort_keys=True, ensure_ascii=False)) != fact_ledger.profile_sha256:
        raise ValueError("profile changed after the reviewed autonomy plan")
    if _sha256_text(resume_text) != fact_ledger.resume_sha256:
        raise ValueError("resume changed after the reviewed autonomy plan")

    context_pack = _context_from_dict(_read_json(run_dir / "context_pack.json"))
    if context_pack.digest != bindings.context_digest:
        raise ValueError("run manifest context digest mismatch")
    policy = _policy_from_dict(_read_json(run_dir / "run_policy.json"))
    if policy.digest != bindings.policy_digest:
        raise ValueError("run manifest policy digest mismatch")
    if not policy.review_only:
        raise ValueError("artifact handoff runner is review-only")

    ledger = UsageLedger(run_id=bindings.run_id, budget=policy.budget)
    _restore_artifact_usage(ledger, run_dir)
    web = ArtifactChatGPTClient(
        run_dir=run_dir,
        bindings=bindings,
        ledger=ledger,
    )
    active_verifier = verifier or FirstPartyVerifier(
        ledger=ledger,
        trusted_sources=configured_trusted_sources(),
    )
    result = AutonomousBatch(
        run_id=bindings.run_id,
        profile=candidate_profile_from_data(profile),
        context_pack=context_pack,
        dependencies=BatchDependencies(
            discovery=web,
            verifier=active_verifier,
            materials=web,
            form_review=ArtifactFormReviewer(
                run_dir=run_dir,
                bindings=bindings,
                ledger=ledger,
            ),
        ),
        policy=policy,
        output_dir=run_dir.parent,
        ledger=ledger,
        fact_ledger=fact_ledger,
    ).run(query=str(manifest.get("query") or ""))
    return result.to_dict()


def reconcile_artifact_handoffs(
    *,
    run_dir: Path,
    approved_fact_digest: str,
    retain_request_id: str,
) -> dict[str, Any]:
    """Auditably retain one of several unanswered requests for a reviewed run."""
    run_dir = run_dir.resolve()
    manifest = _read_json(run_dir / "run_manifest.json")
    bindings = RunBindings.from_manifest(manifest)
    _verify_immutable_artifacts(run_dir, manifest)
    fact_ledger = fact_ledger_from_dict(_read_json(run_dir / "fact_ledger.json"))
    require_approved_fact_digest(fact_ledger.digest, approved_fact_digest)
    if fact_ledger.digest != bindings.fact_digest:
        raise ValueError("run manifest fact digest mismatch")
    return reconcile_unanswered_handoffs(
        run_dir=run_dir,
        bindings=bindings,
        retain_request_id=retain_request_id,
    )


def run_with_cdp(
    *,
    query: str,
    cdp_port: int,
    output_dir: Path,
    corrections_path: Path | None = None,
    approved_fact_digest: str,
    allow_legacy_cdp: bool = False,
    policy: RunPolicy | None = None,
) -> dict[str, Any]:
    """Run the review-only funnel against an explicitly approved legacy CDP session."""
    _require_legacy_cdp_opt_in(allow_legacy_cdp)
    from playwright.sync_api import sync_playwright

    active_policy = policy or RunPolicy(review_only=True)
    if not active_policy.review_only:
        raise ValueError("CDP runner is review-only; final submission requires a separate scoped grant")
    active_policy.validate()
    profile = config.load_profile()
    resume_text = config.RESUME_PATH.read_text(encoding="utf-8")
    corrections = load_corrections(corrections_path) if corrections_path else ()
    fact_ledger = build_fact_ledger(profile, resume_text=resume_text, corrections=corrections)
    require_approved_fact_digest(fact_ledger.digest, approved_fact_digest)
    context_pack = build_context_pack(
        profile,
        resume_text=resume_text,
        job_text=query,
        fact_ledger=fact_ledger,
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_id = f"{stamp}-{fact_ledger.digest[:10]}"
    ledger = UsageLedger(run_id=run_id, budget=active_policy.budget)
    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    for _ in range(4):
        ledger.reserve("artifacts")
    _write_json(run_dir / "fact_ledger.json", fact_ledger.to_dict())
    _write_json(run_dir / "context_pack.json", context_pack.to_dict())
    _write_json(run_dir / "run_policy.json", asdict(active_policy))
    _write_json(
        run_dir / "chatgpt_discovery_request.json",
        {
            "run_id": run_id,
            "surface": "chatgpt_web",
            "prompt": build_discovery_prompt(
                context_pack,
                query=query,
                limit=active_policy.budget.discoveries,
            ),
            "raw_transcript_required": False,
        },
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        try:
            web = ChatGPTWebClient(page=page, ledger=ledger)
            dependencies = BatchDependencies(
                discovery=web,
                verifier=FirstPartyVerifier(
                    ledger=ledger,
                    trusted_sources=configured_trusted_sources(),
                ),
                materials=web,
                form_review=ReadOnlyFormReviewer(page=page, ledger=ledger),
                fallback_discovery=DirectATSDiscovery(ledger=ledger),
            )
            result = AutonomousBatch(
                run_id=run_id,
                profile=candidate_profile_from_data(profile),
                context_pack=context_pack,
                dependencies=dependencies,
                policy=active_policy,
                output_dir=output_dir,
                ledger=ledger,
                fact_ledger=fact_ledger,
            ).run(query=query)
        finally:
            page.close()
    return result.to_dict()


def probe_chatgpt_cdp(*, cdp_port: int, allow_legacy_cdp: bool = False) -> dict[str, Any]:
    """Perform an explicitly approved legacy CDP composer/auth probe."""
    _require_legacy_cdp_opt_in(allow_legacy_cdp)
    from playwright.sync_api import sync_playwright

    policy = RunPolicy()
    ledger = UsageLedger(run_id="doctor-probe", budget=policy.budget)
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        try:
            ledger.reserve("browser_navigations")
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=30_000)
            result = ChatGPTWebClient(page=page, ledger=ledger).probe()
        finally:
            page.close()
    return result


def _require_legacy_cdp_opt_in(allowed: bool) -> None:
    if not allowed:
        raise PermissionError(
            "legacy CDP transport is disabled by default; use the portable ChatGPT Web "
            "artifact handoff with the Codex Chrome connector, or pass --allow-legacy-cdp "
            "only for a deliberate caller-provided compatibility session"
        )


def require_approved_fact_digest(actual: str, approved: str) -> None:
    """Require an exact digest copied from a reviewed fact ledger."""
    if not approved or approved != actual:
        raise PermissionError(
            "approved fact digest does not match; run autonomy plan and review fact_ledger.json"
        )


def _require_autonomy_ready_facts(fact_ledger: Any) -> None:
    blockers = require_confirmed_facts(fact_ledger, REQUIRED_AUTONOMY_FACT_IDS)
    if blockers:
        raise PermissionError(
            "live application facts require applicant review before approval, form filling, "
            "or submission: "
            + ",".join(blockers)
        )


def _restore_artifact_usage(ledger: UsageLedger, run_dir: Path) -> None:
    """Restore unique accepted/rejected tool-call counts across resumptions."""
    handoff_dir = run_dir / "handoff"
    for path in sorted(handoff_dir.glob("*.receipt.json")):
        ledger.reserve("browser_navigations")
        ledger.reserve("external_calls")
        surface = "browser_tool"
        if not path.name.startswith("form_review."):
            ledger.reserve("model_calls")
            surface = "chatgpt_web_artifact"
        ledger.record_event(
            stage="handoff",
            operation="restore_accepted_artifact",
            surface=surface,
            status="ok",
        )
    for path in sorted(handoff_dir.glob("*.rejected.*.json")):
        ledger.reserve("retries")
        ledger.reserve("browser_navigations")
        ledger.reserve("external_calls")
        surface = "browser_tool"
        if not path.name.startswith("form_review."):
            ledger.reserve("model_calls")
            surface = "chatgpt_web_artifact"
        ledger.record_event(
            stage="handoff",
            operation="restore_rejected_artifact",
            surface=surface,
            status="error",
            error_class="rejected_response",
        )


def _run_handoff_status(*, run_dir: Path, bindings: RunBindings) -> dict[str, Any]:
    request_count = 0
    response_count = 0
    receipt_count = 0
    responded_unconsumed_count = 0
    pending_kinds: list[str] = []
    handoff_dir = run_dir / "handoff"
    for request_path in sorted(handoff_dir.glob("*.request.json")):
        request = _read_json(request_path)
        if (
            request.get("run_id") != bindings.run_id
            or request.get("fact_digest") != bindings.fact_digest
            or request.get("context_digest") != bindings.context_digest
            or request.get("policy_digest") != bindings.policy_digest
        ):
            raise ValueError("handoff request bindings differ from run manifest")
        kind = str(request.get("kind") or "")
        if kind not in {"role_candidates", "material_packet", "form_review"}:
            raise ValueError("handoff request kind is invalid")
        response_path = (run_dir / str(request.get("response_path") or "")).resolve()
        if response_path.parent != handoff_dir.resolve() or not response_path.name.endswith(
            ".response.json"
        ):
            raise ValueError("handoff response path escaped its run directory")
        receipt_path = response_path.with_name(
            response_path.name.replace(".response.json", ".receipt.json")
        )
        request_count += 1
        if response_path.is_file():
            response_count += 1
            if receipt_path.is_file():
                receipt_count += 1
            else:
                responded_unconsumed_count += 1
        else:
            pending_kinds.append(kind)
    if len(pending_kinds) + responded_unconsumed_count > 1:
        raise ValueError("autonomy run has more than one active handoff exchange")
    return {
        "request_count": request_count,
        "response_count": response_count,
        "receipt_count": receipt_count,
        "responded_unconsumed_count": responded_unconsumed_count,
        "pending_count": len(pending_kinds),
        "pending_kinds": sorted(set(pending_kinds)),
        "rejected_response_count": len(list(handoff_dir.glob("*.rejected.*.json"))),
        "superseded_request_count": len(
            list(handoff_dir.glob("*.superseded.*.json"))
        ),
    }


def _run_result_status(*, run_dir: Path, run_id: str) -> dict[str, Any]:
    path = run_dir / "result_ledger.json"
    if not path.is_file():
        return {
            "present": False,
            "trust": "absent",
            "reported_status": "not_started",
            "reported_discovery_count": 0,
            "reported_material_count": 0,
            "reported_form_review_count": 0,
            "reported_final_action_count": 0,
        }
    result = _read_json(path)
    if result.get("run_id") != run_id:
        raise ValueError("result ledger run id mismatch")
    status = str(result.get("status") or "")
    if status not in RUN_RESULT_STATUSES:
        raise ValueError("result ledger status is invalid")
    if set(result) != RUN_RESULT_FIELDS:
        raise ValueError("result ledger fields differ from schema")
    if any(not isinstance(result.get(field), list) for field in RUN_RESULT_LIST_FIELDS):
        raise ValueError("result ledger collections are invalid")
    if any(
        not isinstance(item, dict)
        for field in RUN_RESULT_LIST_FIELDS
        for item in result[field]
    ):
        raise ValueError("result ledger collections must contain objects")
    usage = result.get("usage")
    if not isinstance(usage, dict) or usage.get("run_id") != run_id:
        raise ValueError("result ledger usage binding is invalid")
    pending_requests = result["pending_requests"]
    if len(pending_requests) > 1:
        raise ValueError("result ledger has more than one pending request")
    if pending_requests:
        _validate_result_pending_request(pending_requests[0])
    if status == "awaiting_chatgpt_web" and (
        len(pending_requests) != 1
        or pending_requests[0].get("surface") != "chatgpt_web"
    ):
        raise ValueError("result ledger ChatGPT wait has no matching request")
    if status == "awaiting_browser_tool" and (
        len(pending_requests) != 1
        or pending_requests[0].get("surface") != "browser_tool"
    ):
        raise ValueError("result ledger browser wait has no matching request")
    if status not in {
        "awaiting_chatgpt_web",
        "awaiting_browser_tool",
        "budget_exhausted",
    } and pending_requests:
        raise ValueError("result ledger terminal state has a pending request")
    final_actions = result["final_actions"]
    if final_actions:
        raise ValueError("pre-campaign result ledger cannot contain final actions")
    if status == "review_ready" and (
        not result["materials"]
        or any(
            item.get("status") not in ACCEPTED_FORM_REVIEW_STATUSES
            for item in result["form_reviews"]
        )
    ):
        raise ValueError("result ledger review-ready state is inconsistent")
    if status == "form_review_blocked" and (
        not result["materials"]
        or not result["form_reviews"]
        or all(
            item.get("status") in ACCEPTED_FORM_REVIEW_STATUSES
            for item in result["form_reviews"]
        )
    ):
        raise ValueError("result ledger form-review state is inconsistent")
    if status == "no_eligible_verified_roles" and (
        result["materials"] or result["form_reviews"]
    ):
        raise ValueError("result ledger empty-role state is inconsistent")
    return {
        "present": True,
        "trust": "validated_but_mutable_untrusted",
        "reported_status": status,
        "reported_discovery_count": len(result["discoveries"]),
        "reported_material_count": len(result["materials"]),
        "reported_form_review_count": len(result["form_reviews"]),
        "reported_final_action_count": len(final_actions),
    }


def _validate_result_pending_request(pending: dict[str, Any]) -> None:
    if set(pending) != RUN_PENDING_FIELDS or any(
        not isinstance(pending.get(field), str) or not pending[field]
        for field in RUN_PENDING_FIELDS
    ):
        raise ValueError("result ledger pending request fields are invalid")
    surface = pending["surface"]
    kind = pending["kind"]
    allowed = {
        "chatgpt_web": {"role_candidates", "material_packet"},
        "browser_tool": {"form_review"},
    }
    if surface not in allowed or kind not in allowed[surface]:
        raise ValueError("result ledger pending request surface or kind is invalid")


def _supervisor_decision(*, live_gate: str, review_phase: str) -> tuple[str, str, bool]:
    gate_decisions = {
        "required_facts": ("applicant", "review_required_facts", False),
        "preferred_location": ("applicant", "confirm_preferred_location", False),
        "system_approval_trust_store": (
            "system_admin",
            "install_approval_trust_store",
            False,
        ),
        "signed_fact_approval": ("applicant", "sign_fact_approval", False),
    }
    phase_decisions = {
        "reported_budget_exhausted": ("none", "budget_exhausted", False),
        "response_ready_to_advance": ("controller", "advance_imported_response", False),
        "awaiting_role_candidates": (
            "browser_connector",
            "provide_chatgpt_web_role_candidates",
            True,
        ),
        "awaiting_material_packet": (
            "browser_connector",
            "provide_chatgpt_web_material_packet",
            True,
        ),
        "awaiting_form_review": (
            "browser_connector",
            "perform_read_only_form_review",
            True,
        ),
        "reported_review_ready": ("applicant", "review_candidate_packet", False),
        "reported_form_review_blocked": (
            "controller",
            "inspect_form_review_blocker",
            False,
        ),
        "reported_no_eligible_verified_roles": (
            "controller",
            "plan_next_bounded_run",
            False,
        ),
        "reported_failed_closed": ("controller", "inspect_failed_closed", False),
        "ready_to_advance": ("controller", "advance_run", False),
    }
    if review_phase in phase_decisions:
        return phase_decisions[review_phase]
    if live_gate in gate_decisions:
        return gate_decisions[live_gate]
    return "controller", "inspect_supervisor_status", False


def _research_can_progress(review_phase: str) -> bool:
    """Report whether reversible research/review work remains actionable.

    Applicant facts and live approvals are external-action gates. They must not
    suppress discovery, first-party verification, local material drafting, or
    read-only form inspection.
    """
    return review_phase in {
        "response_ready_to_advance",
        "awaiting_role_candidates",
        "awaiting_material_packet",
        "awaiting_form_review",
        "reported_review_ready",
        "reported_form_review_blocked",
        "reported_no_eligible_verified_roles",
        "reported_failed_closed",
        "ready_to_advance",
    }


def _read_run_heartbeat(
    *,
    run_dir: Path,
    run_id: str,
    current: datetime,
    run_created_at: datetime,
) -> tuple[datetime, dict[str, Any]] | None:
    path = run_dir / RUN_HEARTBEAT_NAME
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError("autonomy heartbeat must not be a symbolic link")
    payload = _read_json(path)
    if set(payload) != {
        "schema_version",
        "run_id",
        "recorded_at",
        "status_sha256",
        "status",
    }:
        raise ValueError("autonomy heartbeat fields differ from schema")
    status = payload.get("status")
    recorded_at = str(payload.get("recorded_at") or "")
    if (
        payload.get("schema_version") != RUN_HEARTBEAT_SCHEMA_VERSION
        or payload.get("run_id") != run_id
        or not isinstance(status, dict)
        or status.get("schema_version") != RUN_STATUS_SCHEMA_VERSION
        or status.get("run_id") != run_id
        or status.get("recorded_at") != recorded_at
        or status.get("last_heartbeat_at") != recorded_at
        or status.get("heartbeat_due") is not False
        or status.get("heartbeat_interval_seconds") != RUN_HEARTBEAT_INTERVAL_SECONDS
        or payload.get("status_sha256") != _sha256_text(_canonical_json(status))
    ):
        raise ValueError("autonomy heartbeat bindings are invalid")
    parsed = _parse_aware_datetime(
        recorded_at,
        field="autonomy heartbeat timestamp",
    )
    if recorded_at != parsed.isoformat():
        raise ValueError("autonomy heartbeat timestamp must use canonical UTC")
    if parsed < run_created_at:
        raise ValueError("autonomy heartbeat timestamp predates its run")
    if parsed > current:
        raise ValueError("autonomy heartbeat timestamp is in the future")
    progress_fingerprint = status.get("progress_fingerprint")
    last_progress_raw = status.get("last_progress_at")
    if (progress_fingerprint is None) != (last_progress_raw is None):
        raise ValueError("autonomy heartbeat progress fields are incomplete")
    if progress_fingerprint is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", str(progress_fingerprint)):
            raise ValueError("autonomy heartbeat progress fingerprint is invalid")
        last_progress_at = _parse_aware_datetime(
            str(last_progress_raw),
            field="autonomy last-progress timestamp",
        )
        if str(last_progress_raw) != last_progress_at.isoformat():
            raise ValueError("autonomy last-progress timestamp must use canonical UTC")
        if last_progress_at < run_created_at or last_progress_at > parsed:
            raise ValueError("autonomy last-progress timestamp is outside its heartbeat")
    return parsed, status


def _validate_discovery_request(
    *,
    run_dir: Path,
    manifest: dict[str, Any],
    query: str,
    discovery_limit: int,
) -> None:
    relative_path = "handoff/discovery.request.json"
    artifacts = manifest.get("immutable_artifacts")
    if not isinstance(artifacts, dict) or relative_path not in artifacts:
        raise ValueError("autonomy run does not immutably bind its discovery request")
    request = _read_json(run_dir / relative_path)
    input_payload = {
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "stage": "discovery",
        "inputs": {"query": query, "limit": discovery_limit},
    }
    input_digest = _sha256_text(
        json.dumps(
            input_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    request_id_payload = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "run_id": str(manifest.get("run_id") or ""),
        "stage": "discovery",
        "candidate_id": "",
        "input_digest": input_digest,
        "fact_digest": str(manifest.get("fact_digest") or ""),
        "context_digest": str(manifest.get("context_digest") or ""),
        "policy_digest": str(manifest.get("policy_digest") or ""),
    }
    request_id = _sha256_text(
        json.dumps(
            request_id_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    expected = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "run_id": request_id_payload["run_id"],
        "stage": "discovery",
        "kind": "role_candidates",
        "request_id": request_id,
        "input_digest": input_digest,
        "fact_digest": request_id_payload["fact_digest"],
        "context_digest": request_id_payload["context_digest"],
        "policy_digest": request_id_payload["policy_digest"],
        "response_path": "handoff/discovery.response.json",
    }
    mismatches = [key for key, value in expected.items() if request.get(key) != value]
    prompt = str(request.get("prompt") or "")
    if mismatches or not prompt or request.get("prompt_sha256") != _sha256_text(prompt):
        raise ValueError("immutable discovery request differs from the reviewed run")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, default=str, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _write_fsynced_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, default=str, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        try:
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, data[offset:])
                if written <= 0:
                    raise OSError("short heartbeat write")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _aware_utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("autonomy status time must include a timezone")
    return current.astimezone(timezone.utc)


def _parse_aware_datetime(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    return _aware_utc(parsed)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def _verify_immutable_artifacts(run_dir: Path, manifest: dict[str, Any]) -> None:
    artifacts = manifest.get("immutable_artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("autonomy run manifest has no immutable artifacts")
    for name, expected in artifacts.items():
        path = (run_dir / str(name)).resolve()
        if path != run_dir and run_dir not in path.parents:
            raise ValueError(f"immutable autonomy artifact escaped run directory: {name}")
        if not path.is_file():
            raise ValueError(f"immutable autonomy artifact missing: {name}")
        if _sha256_file(path) != expected:
            raise ValueError(f"immutable autonomy artifact changed: {name}")


def _context_from_dict(payload: dict[str, Any]) -> CompactContextPack:
    evidence = payload.get("evidence")
    profile = payload.get("profile")
    if payload.get("version") != CONTEXT_VERSION:
        raise ValueError("unsupported context pack version")
    if not isinstance(profile, dict) or not isinstance(evidence, list):
        raise ValueError("context pack has invalid profile or evidence")
    if any(not isinstance(item, dict) for item in evidence):
        raise ValueError("context pack evidence must contain objects")
    core = {
        "version": payload["version"],
        "profile": profile,
        "evidence": evidence,
    }
    serialized = json.dumps(
        core,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = _sha256_text(serialized)
    if payload.get("digest") != digest:
        raise ValueError("context pack digest mismatch")
    return CompactContextPack(
        version=str(payload["version"]),
        profile=profile,
        evidence=tuple(evidence),
        digest=digest,
        serialized_chars=len(serialized),
    )


def _policy_from_dict(payload: dict[str, Any]) -> RunPolicy:
    source_raw = payload.get("source")
    budget_raw = payload.get("budget")
    if not isinstance(source_raw, dict) or not isinstance(budget_raw, dict):
        raise ValueError("run policy source or budget is invalid")
    source = SourcePolicy(
        primary=str(source_raw.get("primary") or ""),
        disabled=tuple(str(item) for item in source_raw.get("disabled") or ()),
        fallbacks=tuple(str(item) for item in source_raw.get("fallbacks") or ()),
        require_recorded_primary_failure=bool(
            source_raw.get("require_recorded_primary_failure", True)
        ),
    )
    policy = RunPolicy(
        version=str(payload.get("version") or ""),
        review_only=bool(payload.get("review_only")),
        source=source,
        budget=FunnelBudget(**budget_raw),
        allow_nested_model_processes=bool(payload.get("allow_nested_model_processes")),
        require_explicit_submit_authorization=bool(
            payload.get("require_explicit_submit_authorization")
        ),
        max_post_age_days=int(payload.get("max_post_age_days") or 0),
    )
    policy.validate()
    return policy


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

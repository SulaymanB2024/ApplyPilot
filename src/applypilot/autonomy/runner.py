"""Artifact-first CLI helpers for autonomous batches."""

from __future__ import annotations

import hashlib
import json
import os
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
    build_fact_ledger,
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
)
from applypilot.autonomy.policy import FunnelBudget, RunPolicy, SourcePolicy
from applypilot.autonomy.telemetry import UsageLedger


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


def run_with_cdp(
    *,
    query: str,
    cdp_port: int,
    output_dir: Path,
    corrections_path: Path | None = None,
    approved_fact_digest: str,
    policy: RunPolicy | None = None,
) -> dict[str, Any]:
    """Run the review-only funnel against a caller-provided Chrome CDP session."""
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
    _require_autonomy_ready_facts(fact_ledger)
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


def probe_chatgpt_cdp(*, cdp_port: int) -> dict[str, Any]:
    """Perform a no-send ChatGPT composer/auth probe."""
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
            "autonomy facts require applicant review before any model or browser call: "
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

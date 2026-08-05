"""Artifact-first CLI helpers for autonomous batches."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot import config
from applypilot.autonomy.batch import AutonomousBatch, BatchDependencies
from applypilot.autonomy.chatgpt_web import ChatGPTWebClient
from applypilot.autonomy.context import (
    build_context_pack,
    build_discovery_prompt,
    candidate_profile_from_data,
)
from applypilot.autonomy.direct_ats import DirectATSDiscovery
from applypilot.autonomy.facts import build_fact_ledger, load_corrections
from applypilot.autonomy.first_party import FirstPartyVerifier, configured_trusted_hosts
from applypilot.autonomy.form_review import ReadOnlyFormReviewer
from applypilot.autonomy.policy import RunPolicy
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
        "facts": str(run_dir / "fact_ledger.json"),
        "context": str(run_dir / "context_pack.json"),
        "policy": str(run_dir / "run_policy.json"),
        "request": str(run_dir / "chatgpt_discovery_request.json"),
        "fact_digest": fact_ledger.digest,
    }
    _write_json(Path(paths["facts"]), fact_ledger.to_dict())
    _write_json(Path(paths["context"]), context.to_dict())
    _write_json(Path(paths["policy"]), asdict(active_policy))
    _write_json(
        Path(paths["request"]),
        {
            "run_id": run_id,
            "surface": "chatgpt_web",
            "prompt": build_discovery_prompt(
                context,
                query=query,
                limit=active_policy.budget.discoveries,
            ),
            "response_path": str(run_dir / "chatgpt_discovery_response.json"),
            "raw_transcript_required": False,
        },
    )
    return paths


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
                    trusted_hosts=configured_trusted_hosts(),
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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str, sort_keys=True), encoding="utf-8")

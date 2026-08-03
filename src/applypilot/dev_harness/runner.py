"""Run planning and worker-artifact steps for the self-improvement harness."""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from applypilot import config
from applypilot.dev_harness.artifacts import read_json, utc_timestamp, write_json, write_text
from applypilot.dev_harness.contracts import (
    DEFAULT_ALLOWED_FILES,
    DEFAULT_FORBIDDEN_BOUNDARIES,
    DEFAULT_VALIDATION_COMMANDS,
    HARNESS_VERSION,
    DevHarnessSettings,
    load_settings,
)
from applypilot.dev_harness.knowledge import (
    build_knowledge_index,
    format_knowledge_index_for_prompt,
    write_knowledge_base,
)


def default_run_dir() -> Path:
    """Return the default local run directory for a new improve run."""
    return config.APP_DIR / "dev-harness" / utc_timestamp()


def create_plan(
    *,
    scope: str = "apply",
    out_dir: Path | None = None,
    goal: str | None = None,
    allowed_files: tuple[str, ...] | None = None,
    settings: DevHarnessSettings | None = None,
) -> Path:
    """Create a scoped self-improvement plan and prompt packet."""
    active_settings = settings or load_settings()
    run_dir = out_dir or default_run_dir()
    files = tuple(allowed_files or DEFAULT_ALLOWED_FILES)
    plan_path = run_dir / "plan.json"
    worker_prompt_path = run_dir / "worker_prompt.md"
    reviewer_prompt_path = run_dir / "reviewer_prompt.md"
    proposal_path = run_dir / "proposal.json"
    review_path = run_dir / "review.json"
    decision_path = run_dir / "decision.md"
    knowledge_paths = write_knowledge_base(run_dir, scope=scope)
    knowledge_index = build_knowledge_index()

    plan: dict[str, Any] = {
        "version": HARNESS_VERSION,
        "created_at": utc_timestamp(),
        "scope": scope,
        "goal": goal or "Improve ApplyPilot using bounded worker and reviewer artifacts.",
        "mode": active_settings.mode,
        "models": {
            "worker": active_settings.worker_model,
            "worker_effort": active_settings.worker_effort,
            "reviewer": active_settings.reviewer_model,
            "reviewer_effort": active_settings.reviewer_effort,
            "service_tier": active_settings.service_tier,
            "forbidden": list(active_settings.forbidden_models),
        },
        "allowed_files": list(files),
        "forbidden_boundaries": list(DEFAULT_FORBIDDEN_BOUNDARIES),
        "validation_commands": list(DEFAULT_VALIDATION_COMMANDS),
        "artifact_paths": {
            "plan": str(plan_path),
            "worker_prompt": str(worker_prompt_path),
            "reviewer_prompt": str(reviewer_prompt_path),
            "proposal": str(proposal_path),
            "results": str(run_dir / "results.json"),
            "review": str(review_path),
            "decision": str(decision_path),
            **knowledge_paths,
        },
        "knowledge": {
            "index_cards": knowledge_index["cards"],
            "retrieval_policy": knowledge_index["retrieval_policy"],
        },
        "worker_contract": [
            "Inspect only declared allowed_files unless the plan is updated by a human.",
            "Read knowledge_index.json before requesting additional context.",
            "Open full knowledge cards only when their when_to_open field matches the task.",
            "Use research_queue.json for ChatGPT Web questions instead of loading raw transcripts.",
            "Do not scan parent directories or neighboring repositories.",
            "Do not spawn recursive workers or hand off broad prompts.",
            "Return proposal artifacts only; do not edit repository files in v1.",
        ],
        "review_contract": [
            "Reject forbidden models, out-of-scope files, recursive delegation, and boundary weakening.",
            "Do not approve without deterministic validation results.",
            "Keep missing provider/model/browser coverage as a measurement gap.",
        ],
    }
    write_json(plan_path, plan)
    write_json(run_dir / "commands.json", {"validation_commands": plan["validation_commands"]})
    write_text(worker_prompt_path, build_worker_prompt(plan))
    write_text(reviewer_prompt_path, build_reviewer_prompt(plan))
    return plan_path


def build_worker_prompt(plan: dict[str, Any]) -> str:
    """Build the constrained worker prompt for an improve run."""
    allowed = "\n".join(f"- {path}" for path in plan["allowed_files"])
    forbidden = "\n".join(f"- {boundary}" for boundary in plan["forbidden_boundaries"])
    knowledge_index = {"cards": plan["knowledge"]["index_cards"]}
    knowledge = format_knowledge_index_for_prompt(knowledge_index)
    return f"""# ApplyPilot Improve Worker

Goal: {plan["goal"]}

You are a bounded worker. Produce a proposal artifact only. Do not edit files.

Allowed files:
{allowed}

Forbidden boundaries:
{forbidden}

{knowledge}

Rules:
- Do not inspect parent directories or neighboring repositories.
- Do not spawn subagents, recursive workers, or duplicate sibling threads.
- Do not use forbidden models: {", ".join(plan["models"]["forbidden"])}.
- Open knowledge cards only when the task matches the card trigger; otherwise use the index.
- Put ChatGPT Web research needs into research_queue.json and curate summaries into cards later.
- Preserve dry-run, email-only local draft, and fail-closed safety behavior.
- Include proposed changed files, rationale, risk flags, and validation commands.
"""


def build_reviewer_prompt(plan: dict[str, Any]) -> str:
    """Build the deterministic reviewer prompt for an improve run."""
    return f"""# ApplyPilot Improve Reviewer

Review the worker proposal for scope `{plan["scope"]}`.

Reject if it:
- Uses forbidden models: {", ".join(plan["models"]["forbidden"])}.
- Touches files outside the allowed list.
- Weakens dry-run, email-only local draft, or fail-closed safety behavior.
- Uses recursive worker delegation.
- Lacks deterministic validation results.

Return `approved: false` unless both guardrails and validation pass.
"""


def create_worker_proposal(*, plan_path: Path, dry_run: bool = True) -> Path:
    """Create a worker proposal artifact without editing source files."""
    if not dry_run:
        raise ValueError("The v1 improve worker only supports dry-run artifact generation.")
    plan = read_json(plan_path)
    proposal_path = Path(plan["artifact_paths"]["proposal"])
    proposal: dict[str, Any] = {
        "version": plan["version"],
        "created_at": utc_timestamp(),
        "plan_path": str(plan_path),
        "mode": "dry-run",
        "models": {
            "worker": plan["models"]["worker"],
            "worker_effort": plan["models"]["worker_effort"],
            "service_tier": plan["models"]["service_tier"],
        },
        "summary": "Dry-run worker artifact created. No repository files were edited.",
        "touches_files": [],
        "proposed_changes": [],
        "risk_flags": [],
        "recursive_delegation": False,
        "validation_commands": plan["validation_commands"],
        "validation_results": [],
    }
    write_json(proposal_path, proposal)
    return proposal_path


def run_validation(*, plan_path: Path, timeout_seconds: int = 300) -> Path:
    """Run deterministic validation commands from a plan and attach results to the proposal."""
    plan = read_json(plan_path)
    results_path = Path(plan["artifact_paths"].get("results", plan_path.parent / "results.json"))
    results: list[dict[str, Any]] = []
    for command in plan["validation_commands"]:
        start = time.time()
        argv = _command_argv(str(command))
        completed = subprocess.run(
            argv,
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        results.append(
            {
                "command": str(command),
                "exit_code": completed.returncode,
                "duration_ms": int((time.time() - start) * 1000),
                "stdout_tail": completed.stdout[-2000:],
                "stderr_tail": completed.stderr[-2000:],
            }
        )
    payload = {
        "version": plan["version"],
        "created_at": utc_timestamp(),
        "plan_path": str(plan_path),
        "passed": all(result["exit_code"] == 0 for result in results),
        "results": results,
    }
    write_json(results_path, payload)

    proposal_path = Path(plan["artifact_paths"]["proposal"])
    if proposal_path.exists():
        proposal = read_json(proposal_path)
        proposal["validation_results"] = results
        write_json(proposal_path, proposal)
    return results_path


def _command_argv(command: str) -> list[str]:
    argv = shlex.split(command)
    if argv and argv[0] == "python":
        argv[0] = sys.executable
    return argv

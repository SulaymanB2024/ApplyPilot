"""Progressive-reveal knowledge artifacts for improve runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from applypilot.dev_harness.artifacts import write_json

KNOWLEDGE_VERSION = "applypilot-knowledge-v1"

DEFAULT_KNOWLEDGE_CARDS: tuple[dict[str, Any], ...] = (
    {
        "id": "codex_context_budget",
        "title": "Keep worker context narrow",
        "summary": "Workers should start from an index and open only task-matched cards or files.",
        "tags": ["codex", "context", "tokens", "progressive-reveal"],
        "when_to_open": "Open when a worker wants broad repository scans, parent-directory searches, or full docs.",
        "case_type": "failure_mode",
        "misalignment_risks": [
            "neighboring-repo AGENTS files pollute the task",
            "large prompts spend tokens before useful work starts",
            "worker repeats context already represented by artifacts",
        ],
        "preferred_actions": [
            "read knowledge_index.json first",
            "open only cards whose when_to_open matches the task",
            "request one additional file or card at a time",
            "record missing context as a measurement gap instead of widening automatically",
        ],
        "source_refs": ["local_codex_experiment_20260709"],
        "retrieval_budget": "index entry first; full card only if context scope is unclear",
    },
    {
        "id": "recursive_worker_boundary",
        "title": "Do not spawn recursive workers",
        "summary": "Worker/reviewer lanes must stay bounded and cannot create nested workers by default.",
        "tags": ["codex", "subagents", "orchestration", "cost"],
        "when_to_open": "Open when a proposal mentions subagents, nested reviewers, fanout, or parallel lanes.",
        "case_type": "guardrail",
        "misalignment_risks": [
            "recursive handoffs duplicate prompts",
            "runaway lanes spend tokens without improving evidence",
            "reviewer and worker roles become indistinguishable",
        ],
        "preferred_actions": [
            "reject recursive delegation",
            "use one bounded reviewer gate after worker output exists",
            "require owner, inputs, allowed files, validation command, and merge gate for any future lane",
        ],
        "source_refs": ["interrupted_self_improve_reviewer_lane"],
        "retrieval_budget": "full card only when delegation is proposed",
    },
    {
        "id": "forbidden_spark_model",
        "title": "Avoid fast-draining Spark model path",
        "summary": "The improve harness forbids gpt-5.3-codex-spark by default.",
        "tags": ["model-policy", "codex", "cost"],
        "when_to_open": "Open when a plan or proposal changes model defaults or worker/reviewer model policy.",
        "case_type": "model_policy",
        "misalignment_risks": [
            "unavailable or expensive models are assumed without probing",
            "model choice is hard-coded into prompts instead of policy artifacts",
        ],
        "preferred_actions": [
            "read APPLYPILOT_DEV_FORBIDDEN_MODELS",
            "fail closed when selected models match forbidden entries",
            "keep model names configurable and separate from apply-run executor models",
        ],
        "source_refs": ["user_model_cost_constraint"],
        "retrieval_budget": "full card only for model config changes",
    },
    {
        "id": "dry_run_email_fail_closed",
        "title": "Preserve apply safety boundaries",
        "summary": "Dry-run, local email drafts, and fail-closed browser gates cannot be weakened by improve proposals.",
        "tags": ["apply", "safety", "dry-run", "email", "captcha"],
        "when_to_open": "Open when a proposal touches apply flow, email behavior, submit gates, CAPTCHA, MFA, SSO, payment, tax, or identity checks.",
        "case_type": "safety_contract",
        "misalignment_risks": [
            "dry-run marks jobs as applied",
            "email-only flow creates external drafts or sends messages",
            "CAPTCHA or identity surfaces are treated as solvable automation tasks",
        ],
        "preferred_actions": [
            "reject external send behavior",
            "keep email output local as email_application_draft.md",
            "treat blocked browser states as measurement gaps or fail-closed results",
        ],
        "source_refs": ["apply_harness_contract", "training_audit_contract"],
        "retrieval_budget": "full card before any apply safety change",
    },
    {
        "id": "dirty_worktree_protocol",
        "title": "Respect pre-existing dirty worktrees",
        "summary": "Improve runs must preserve unrelated dirty files and report mixed diffs honestly.",
        "tags": ["git", "worktree", "review"],
        "when_to_open": "Open when a proposal plans to stage, commit, reset, rebase, or summarize changed files.",
        "case_type": "operator_protocol",
        "misalignment_risks": [
            "agent claims pre-existing edits as its own",
            "unrelated user work is reverted or swept into a commit",
            "review status hides dirty files outside the task scope",
        ],
        "preferred_actions": [
            "record active branch, remote, and dirty status before edits",
            "separate owned changes from pre-existing dirty files",
            "do not stage or commit unless explicitly requested",
        ],
        "source_refs": ["codex_operating_model"],
        "retrieval_budget": "full card when git operations are proposed",
    },
    {
        "id": "validation_gate",
        "title": "Reviewer approval requires deterministic evidence",
        "summary": "Review verdicts are not enough; validation results must pass before a proposal is patch-ready.",
        "tags": ["validation", "review", "tests"],
        "when_to_open": "Open when a proposal claims it is ready, approved, complete, or safe to patch.",
        "case_type": "review_gate",
        "misalignment_risks": [
            "model review substitutes for tests",
            "missing provider or browser coverage is treated as success",
            "approval is granted before commands run",
        ],
        "preferred_actions": [
            "require results.json",
            "keep missing coverage as a gap",
            "run focused tests before broad tests",
        ],
        "source_refs": ["improve_review_contract"],
        "retrieval_budget": "full card before approving a proposal",
    },
    {
        "id": "chatgpt_web_research_handoff",
        "title": "Use ChatGPT Web as curated research input",
        "summary": "ChatGPT Web research should become concise source-backed case artifacts, not raw transcripts.",
        "tags": ["research", "chatgpt-web", "knowledge", "sources"],
        "when_to_open": "Open when a worker needs current research, broad web synthesis, or examples beyond local repo evidence.",
        "case_type": "research_protocol",
        "misalignment_risks": [
            "raw web transcripts bloat prompts",
            "research claims lose source boundaries",
            "login/private browser state is mistaken for repo evidence",
        ],
        "preferred_actions": [
            "write research questions into research_queue.json",
            "ask ChatGPT Web for concise findings with citations or source names",
            "curate results into new knowledge cards before worker use",
            "separate public research from private/login-only observations",
        ],
        "source_refs": ["chatgpt_research_workflow_memory"],
        "retrieval_budget": "full card before web research or research import",
    },
)


def build_knowledge_index(cards: tuple[dict[str, Any], ...] = DEFAULT_KNOWLEDGE_CARDS) -> dict[str, Any]:
    """Return a compact routing index without full case details."""
    return {
        "version": KNOWLEDGE_VERSION,
        "retrieval_policy": [
            "Read this index first.",
            "Open a full card only when the task matches its when_to_open field.",
            "Do not paste raw research transcripts into worker prompts.",
            "If no card matches, write a targeted research_queue item instead of widening context.",
        ],
        "cards": [
            {
                "id": card["id"],
                "title": card["title"],
                "summary": card["summary"],
                "tags": card["tags"],
                "when_to_open": card["when_to_open"],
                "retrieval_budget": card["retrieval_budget"],
            }
            for card in cards
        ],
    }


def build_research_queue(scope: str) -> dict[str, Any]:
    """Return a bounded queue for ChatGPT Web or manual research."""
    return {
        "version": KNOWLEDGE_VERSION,
        "scope": scope,
        "research_surface": "ChatGPT Web primary; local repo evidence remains authoritative for implementation.",
        "output_contract": {
            "question": "The exact research question.",
            "why_needed": "Which knowledge gap or card triggered the research.",
            "sources": "Named sources, URLs, or clear source labels when available.",
            "summary": "Concise findings suitable for a knowledge card.",
            "confidence": "high | medium | low",
            "followups": "Remaining gaps or checks.",
        },
        "items": [
            {
                "id": "chatgpt-web-self-improve-cases",
                "question": "Find concise examples of progressive context or retrieval-gated agent workflows that could inform ApplyPilot improve runs.",
                "why_needed": "Seed future knowledge cards without loading long transcripts into the worker prompt.",
                "status": "queued",
            }
        ],
    }


def write_knowledge_base(run_dir: Path, *, scope: str) -> dict[str, str]:
    """Write the knowledge index, full cards, and research queue artifacts."""
    cards_dir = run_dir / "knowledge_cards"
    index_path = run_dir / "knowledge_index.json"
    queue_path = run_dir / "research_queue.json"
    cards_dir.mkdir(parents=True, exist_ok=True)
    for card in DEFAULT_KNOWLEDGE_CARDS:
        write_json(cards_dir / f"{card['id']}.json", card)
    write_json(index_path, build_knowledge_index())
    write_json(queue_path, build_research_queue(scope))
    return {
        "knowledge_index": str(index_path),
        "knowledge_cards_dir": str(cards_dir),
        "research_queue": str(queue_path),
    }


def format_knowledge_index_for_prompt(index: dict[str, Any]) -> str:
    """Format compact knowledge routing text for worker prompts."""
    lines = ["Knowledge index (open full cards only when needed):"]
    for entry in index["cards"]:
        tags = ", ".join(entry["tags"])
        lines.append(
            f"- {entry['id']} [{tags}]: {entry['summary']} "
            f"When to open: {entry['when_to_open']}"
        )
    return "\n".join(lines)

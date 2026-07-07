"""Deterministic apply harness configuration and run contracts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from applypilot.apply.onepassword import DEFAULT_EXTENSION_ID


AgentBackend = Literal["claude", "codex"]
DEFAULT_CLAUDE_MODEL = "haiku"
DEFAULT_CODEX_MODEL = "gpt-5.5"


class HarnessSettings(BaseSettings):
    """Environment-driven harness settings.

    The default backend stays on the existing Claude executor. The Codex
    backend defaults to the user's configured Codex 5.5 account model and uses
    the deterministic Python/Playwright controller by default.
    """

    agent_backend: AgentBackend = "claude"
    executor_model: str = DEFAULT_CODEX_MODEL
    supervisor_model: str = DEFAULT_CODEX_MODEL
    supervisor_poll_seconds: int = Field(default=60, ge=5)
    deterministic_mode: bool = True
    deterministic_controller: bool = True
    allow_account_creation: bool = True
    onepassword_enabled: bool = True
    onepassword_vault: str | None = None
    onepassword_extension_id: str = DEFAULT_EXTENSION_ID

    model_config = SettingsConfigDict(env_prefix="APPLYPILOT_", extra="ignore")


def load_settings(
    *,
    agent_backend: str | None = None,
    executor_model: str | None = None,
    supervisor_model: str | None = None,
) -> HarnessSettings:
    """Load harness settings with optional CLI overrides."""
    overrides: dict[str, str] = {}
    if agent_backend:
        overrides["agent_backend"] = agent_backend
    if executor_model:
        overrides["executor_model"] = executor_model
    if supervisor_model:
        overrides["supervisor_model"] = supervisor_model
    settings = HarnessSettings(**overrides)
    if not executor_model and settings.agent_backend == "claude":
        settings = settings.model_copy(update={"executor_model": DEFAULT_CLAUDE_MODEL})
    return settings


def prompt_header(settings: HarnessSettings) -> str:
    """Build deterministic instructions prepended to every apply prompt."""
    return f"""== APPLY HARNESS CONTRACT ==
Executor backend: {settings.agent_backend}
Executor model: {settings.executor_model}
Supervisor model: {settings.supervisor_model}
Supervisor wait policy: sleep/poll every {settings.supervisor_poll_seconds}s until executor finishes or times out.
Deterministic mode: {str(settings.deterministic_mode).lower()}
Deterministic controller: {str(settings.deterministic_controller).lower()}
Account creation allowed: {str(settings.allow_account_creation).lower()}
1Password enabled for job-site logins: {str(settings.onepassword_enabled).lower()}

Use deterministic checks before judgment. Prefer direct DOM inspection, fixed selectors, page URLs, explicit form values, saved files, and tool outputs over speculation. When a deterministic check can answer a question, run that check instead of asking the model to infer it.

External communication boundary: never send outbound email or create external email drafts from this harness. If a job requires email submission, write a local email_application_draft.md artifact for user review and finish with RESULT:EMAIL_DRAFT.

Finish with exactly one RESULT line."""


def write_contract(
    *,
    worker_dir: Path,
    worker_id: int,
    port: int,
    job: dict,
    settings: HarnessSettings,
    prompt_path: Path,
    mcp_config_path: Path,
    training_manifest_path: Path | None = None,
) -> Path:
    """Write a per-job harness contract for reproducible execution."""
    artifacts = {
        "prompt": str(prompt_path),
        "mcp_config": str(mcp_config_path),
        "email_draft": str(worker_dir / "email_application_draft.md"),
    }
    if training_manifest_path:
        artifacts["training_manifest"] = str(training_manifest_path)

    contract = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "worker_id": worker_id,
        "cdp_port": port,
        "agent_backend": settings.agent_backend,
        "executor_model": settings.executor_model,
        "supervisor_model": settings.supervisor_model,
        "supervisor_poll_seconds": settings.supervisor_poll_seconds,
        "deterministic_mode": settings.deterministic_mode,
        "deterministic_controller": settings.deterministic_controller,
        "allow_account_creation": settings.allow_account_creation,
        "onepassword": {
            "enabled": settings.onepassword_enabled,
            "vault_configured": bool(settings.onepassword_vault),
            "extension_id": settings.onepassword_extension_id,
        },
        "job": {
            "url": job.get("url"),
            "application_url": job.get("application_url"),
            "title": job.get("title"),
            "site": job.get("site"),
            "fit_score": job.get("fit_score"),
        },
        "artifacts": artifacts,
        "done_criteria": [
            "executor process exits",
            "stdout/logs are captured",
            "final text contains exactly one RESULT line",
            "training manifest records Workday, email draft, Runway, and board handoff coverage",
            "email-only applications write email_application_draft.md instead of sending",
            "job-site account credentials are stored in 1Password when account creation is needed",
            "SSO, passkey, MFA, email verification, unsafe permission, biometric, payment, and tax flows fail closed",
            "database status is updated by deterministic parser",
        ],
    }
    path = worker_dir / "apply_harness_contract.json"
    path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
    return path

"""Deterministic apply harness configuration and run contracts."""

from __future__ import annotations

import json
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from applypilot.apply.google_passwords import PROVIDER_NAME as GOOGLE_PASSWORD_MANAGER
from applypilot.apply.onepassword import DEFAULT_EXTENSION_ID
from applypilot.model_routing import APPLY_FIELD_ROUTE, APPLY_SUPERVISOR_ROUTE


AgentBackend = Literal["claude", "codex"]
CredentialProvider = Literal["google_password_manager", "onepassword", "none"]
ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max", "ultra"]
ServiceTier = Literal["default", "priority"]
DEFAULT_CLAUDE_MODEL = "haiku"
DEFAULT_CODEX_MODEL = APPLY_FIELD_ROUTE.requested_model


class HarnessSettings(BaseSettings):
    """Environment-driven harness settings.

    The default backend is Codex, but production application work remains in
    the deterministic Python/Playwright controller. Codex is available only as
    a narrow, schema-constrained fallback for unresolved safe fields.
    """

    agent_backend: AgentBackend = "codex"
    executor_model: str = DEFAULT_CODEX_MODEL
    executor_effort: ReasoningEffort = APPLY_FIELD_ROUTE.requested_effort
    supervisor_model: str = APPLY_SUPERVISOR_ROUTE.requested_model
    supervisor_effort: ReasoningEffort = APPLY_SUPERVISOR_ROUTE.requested_effort
    model_service_tier: ServiceTier = APPLY_FIELD_ROUTE.service_tier
    supervisor_poll_seconds: int = Field(default=60, ge=5)
    deterministic_mode: bool = True
    deterministic_controller: bool = True
    field_model_call_budget: int = Field(default=0, ge=0, le=2)
    allow_account_creation: bool = False
    credential_provider: CredentialProvider = GOOGLE_PASSWORD_MANAGER
    onepassword_enabled: bool = False
    onepassword_vault: str | None = None
    onepassword_extension_id: str = DEFAULT_EXTENSION_ID

    model_config = SettingsConfigDict(env_prefix="APPLYPILOT_", extra="ignore")

    @property
    def uses_onepassword(self) -> bool:
        """Return whether deprecated legacy 1Password compatibility is active."""
        return self.credential_provider == "onepassword" or self.onepassword_enabled

    @property
    def uses_google_password_manager(self) -> bool:
        """Return whether Chrome should use Google Password Manager/autofill."""
        return self.credential_provider == GOOGLE_PASSWORD_MANAGER and not self.uses_onepassword

    @property
    def requires_model_cli(self) -> bool:
        """Return whether the deterministic controller may invoke a model subprocess."""
        return self.field_model_call_budget > 0


def load_settings(
    *,
    agent_backend: str | None = None,
    executor_model: str | None = None,
    supervisor_model: str | None = None,
    allow_account_creation: bool | None = None,
) -> HarnessSettings:
    """Load harness settings with optional CLI overrides."""
    overrides: dict[str, str] = {}
    if agent_backend:
        overrides["agent_backend"] = agent_backend
    if executor_model:
        overrides["executor_model"] = executor_model
    if supervisor_model:
        overrides["supervisor_model"] = supervisor_model
    if allow_account_creation is not None:
        overrides["allow_account_creation"] = allow_account_creation
    settings = HarnessSettings(**overrides)
    if settings.uses_onepassword:
        warnings.warn(
            "1Password support is deprecated; use Google Password Manager",
            DeprecationWarning,
            stacklevel=2,
        )
    if not executor_model and settings.agent_backend == "claude":
        settings = settings.model_copy(update={"executor_model": DEFAULT_CLAUDE_MODEL})
    return settings


def prompt_header(settings: HarnessSettings) -> str:
    """Build deterministic instructions prepended to every apply prompt."""
    return f"""== APPLY HARNESS CONTRACT ==
Executor backend: {settings.agent_backend}
Executor model: {settings.executor_model}
Executor effort: {settings.executor_effort}
Supervisor model: {settings.supervisor_model}
Supervisor effort: {settings.supervisor_effort}
Model service tier: {settings.model_service_tier}
Supervisor wait policy: sleep/poll every {settings.supervisor_poll_seconds}s until executor finishes or times out.
Deterministic mode: {str(settings.deterministic_mode).lower()}
Deterministic controller: {str(settings.deterministic_controller).lower()}
Field model-call budget per job: {settings.field_model_call_budget}
Account creation allowed: {str(settings.allow_account_creation).lower()}
Credential provider for job-site auth: {settings.credential_provider}
Google Password Manager/autofill enabled: {str(settings.uses_google_password_manager).lower()}
Deprecated 1Password compatibility enabled: {str(settings.uses_onepassword).lower()}

Use deterministic checks before judgment. Prefer direct DOM inspection, fixed selectors, page URLs, explicit form values, saved files, and tool outputs over speculation. When a deterministic check can answer a question, run that check instead of asking the model to infer it.

External communication boundary: never send outbound email or create external email drafts from this harness. If a job requires email submission, write a local email_application_draft.md artifact for user review and finish with RESULT:EMAIL_DRAFT.

Credential boundary: Google Password Manager credentials stay inside Chrome. Do not export, print, or persist browser-saved passwords. For approved account creation, use Chrome's inline generated-password UI and verify only populated state; fail closed if the account gate does not clear.

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
        "executor_effort": settings.executor_effort,
        "supervisor_model": settings.supervisor_model,
        "supervisor_effort": settings.supervisor_effort,
        "model_service_tier": settings.model_service_tier,
        "supervisor_poll_seconds": settings.supervisor_poll_seconds,
        "deterministic_mode": settings.deterministic_mode,
        "deterministic_controller": settings.deterministic_controller,
        "field_model_call_budget": settings.field_model_call_budget,
        "requires_model_cli": settings.requires_model_cli,
        "allow_account_creation": settings.allow_account_creation,
        "credential_provider": settings.credential_provider,
        "google_password_manager": {
            "enabled": settings.uses_google_password_manager,
            "storage": "browser_profile_only",
        },
        "onepassword": {
            "enabled": settings.uses_onepassword,
            "deprecated": True,
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
            "Google Password Manager credentials stay in Chrome and are never exported into artifacts",
            "new account creation uses Google Password Manager inline generation and fails closed unless password fields populate and the account gate clears",
            "SSO, passkey, MFA, email verification, unsafe permission, biometric, payment, and tax flows fail closed",
            "database status is updated by deterministic parser",
        ],
    }
    path = worker_dir / "apply_harness_contract.json"
    path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
    return path

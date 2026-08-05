"""Contracts and policy for ApplyPilot's self-improvement harness."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

HARNESS_VERSION = "applypilot-dev-harness-v1"
DEFAULT_DEV_MODEL = "gpt-5.5"
DEFAULT_FORBIDDEN_MODELS = ("gpt-5.3-codex-spark",)

DEFAULT_FORBIDDEN_BOUNDARIES = (
    "external_email_send_or_external_draft_creation",
    "dry_run_final_submit_or_applied_status",
    "captcha_mfa_sso_payment_tax_identity_bypass",
    "unsafe_permission_or_biometric_bypass",
    "recursive_worker_delegation",
    "parent_directory_or_unbounded_repo_scan",
)

DEFAULT_ALLOWED_FILES = (
    ".env.example",
    ".gitignore",
    "README.md",
    "CHANGELOG.md",
    "docs/superpowers/specs/*",
    "src/applypilot/cli.py",
    "src/applypilot/apply/harness.py",
    "src/applypilot/apply/field_resolver.py",
    "src/applypilot/dev_harness/*",
    "tests/test_dev_harness.py",
    "tests/test_field_resolver.py",
    "tests/test_harness.py",
)

DEFAULT_VALIDATION_COMMANDS = (
    "python -m pytest tests/test_dev_harness.py tests/test_field_resolver.py tests/test_harness.py -q",
    "python -m ruff check src tests",
)


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class DevHarnessSettings:
    """Environment-driven settings for development self-improvement runs."""

    worker_model: str = field(
        default_factory=lambda: os.environ.get(
            "APPLYPILOT_DEV_WORKER_MODEL",
            os.environ.get("APPLYPILOT_EXECUTOR_MODEL", DEFAULT_DEV_MODEL),
        )
    )
    reviewer_model: str = field(
        default_factory=lambda: os.environ.get(
            "APPLYPILOT_DEV_REVIEWER_MODEL",
            os.environ.get("APPLYPILOT_SUPERVISOR_MODEL", DEFAULT_DEV_MODEL),
        )
    )
    mode: str = field(default_factory=lambda: os.environ.get("APPLYPILOT_DEV_MODE", "dry-run"))
    forbidden_models: tuple[str, ...] = field(
        default_factory=lambda: _csv_env("APPLYPILOT_DEV_FORBIDDEN_MODELS", DEFAULT_FORBIDDEN_MODELS)
    )

    def validate(self) -> None:
        """Reject unsafe or explicitly forbidden settings."""
        if self.mode != "dry-run":
            raise ValueError("APPLYPILOT_DEV_MODE must be dry-run for the v1 improve harness.")
        forbidden = {model.lower() for model in self.forbidden_models}
        selected = {
            "worker": self.worker_model,
            "reviewer": self.reviewer_model,
        }
        for role, model in selected.items():
            if model.lower() in forbidden:
                raise ValueError(f"{role} model is forbidden by APPLYPILOT_DEV_FORBIDDEN_MODELS: {model}")


def load_settings() -> DevHarnessSettings:
    """Load and validate development harness settings."""
    settings = DevHarnessSettings()
    settings.validate()
    return settings

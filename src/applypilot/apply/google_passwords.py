"""Google Password Manager integration through the user's Chrome profile.

ApplyPilot does not read, export, or create Google-stored passwords directly.
This module selects a Chrome profile where browser-managed credentials can
autofill and describes the inline Chrome UI operations a visible-browser worker
may use. Password generation and saving happen inside Chrome; no password value
is returned to ApplyPilot or the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from applypilot import config

PROVIDER_NAME = "google_password_manager"

PasswordFormOutcome = Literal["not_needed", "autofilled", "generated", "unavailable"]


@dataclass(frozen=True)
class PasswordFormResult:
    """Value-free result from operating Chrome's inline password UI."""

    outcome: PasswordFormOutcome
    visible_password_fields: int


def _password_fields_satisfied(password_fields: Any) -> bool:
    """Check password field presence without returning any password bytes."""
    count = password_fields.count()
    if count < 1:
        return True
    states = [
        bool(
            password_fields.nth(index).evaluate(
                "element => Boolean(element.value && element.value.length)"
            )
        )
        for index in range(count)
    ]
    return all(states)


def satisfy_password_form_with_google_password_manager(
    page: Any,
    *,
    allow_generation: bool,
) -> PasswordFormResult:
    """Autofill or generate a password through Chrome's inline manager UI.

    The function focuses the first visible password field and accepts Chrome's
    inline suggestion with keyboard navigation. It observes only whether fields
    are populated, never their values. A generated password is therefore kept
    inside Chrome and can be saved by Google Password Manager when the account
    form succeeds.
    """
    try:
        password_fields = page.locator('input[type="password"]:visible')
        count = password_fields.count()
        if count < 1:
            return PasswordFormResult("not_needed", 0)
        page.wait_for_timeout(350)
        if _password_fields_satisfied(password_fields):
            return PasswordFormResult("autofilled", count)

        first = password_fields.first
        first.click(timeout=5000)
        page.wait_for_timeout(250)
        page.keyboard.press("ArrowDown")
        first.evaluate(
            """
            element => {
              element.addEventListener('keydown', event => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  event.stopImmediatePropagation();
                }
              }, {capture: true, once: true});
            }
            """
        )
        page.keyboard.press("Enter")
        page.wait_for_timeout(750)
        if not _password_fields_satisfied(password_fields):
            return PasswordFormResult("unavailable", count)
        return PasswordFormResult(
            "generated" if allow_generation else "autofilled",
            count,
        )
    except Exception:
        return PasswordFormResult("unavailable", 0)


def browser_credential_tool_contract(
    *,
    allow_account_creation: bool,
) -> dict[str, object]:
    """Describe the value-free Google Password Manager browser tool.

    This is deliberately a UI capability contract, not a password database API.
    The worker can activate Chrome's inline autofill/generation controls but may
    not open the credential store, reveal a value, or copy one into an artifact.
    """
    operations = ["autofill_existing_login"]
    if allow_account_creation:
        operations.append("generate_and_save_new_password")
    return {
        "provider": PROVIDER_NAME,
        "interface": "chrome_inline_password_manager_ui",
        "allowed_operations": operations,
        "secret_access": "browser_only_never_model_or_response",
        "prompt_policy": "never_ask_applicant_for_authentication",
        "unavailable_behavior": "return_structured_blocker_without_prompting",
        "completion_evidence": (
            "password_fields_populated_account_continuation_activated_and_gate_cleared"
        ),
    }


def chrome_profile_dirs(user_data_dir: Path | None = None) -> list[Path]:
    """Return local Chrome profile directories in stable preference order."""
    root = user_data_dir or config.get_chrome_user_data()
    if not root.exists():
        return []
    candidates = [root / "Default"] + sorted(root.glob("Profile *"))
    return [path for path in candidates if path.exists() and path.is_dir()]


def chrome_profiles_with_password_store(user_data_dir: Path | None = None) -> list[str]:
    """Return profiles that have a Chrome password-store database.

    This checks only file presence/size metadata. It never opens Chrome's
    encrypted Login Data database or reads credential contents.
    """
    profiles: list[str] = []
    for profile in chrome_profile_dirs(user_data_dir):
        login_data = profile / "Login Data"
        if login_data.exists() and login_data.stat().st_size > 0:
            profiles.append(profile.name)
    return profiles


def choose_chrome_profile_for_google_passwords(user_data_dir: Path | None = None) -> str | None:
    """Choose a Chrome profile for Google Password Manager/autofill."""
    root = user_data_dir or config.get_chrome_user_data()
    explicit = config.get_chrome_profile_directory()
    if explicit and (root / explicit).exists():
        return explicit

    profiles = chrome_profiles_with_password_store(root)
    if "Default" in profiles:
        return "Default"
    if profiles:
        return profiles[0]

    dirs = chrome_profile_dirs(root)
    if not dirs:
        return None
    for profile in dirs:
        if profile.name == "Default":
            return "Default"
    return dirs[0].name


def configure_google_password_preferences(profile_dir: Path) -> None:
    """Enable Chrome password/autofill preferences for a worker profile."""
    prefs_file = profile_dir / "Preferences"
    if not prefs_file.exists():
        return
    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        prefs["credentials_enable_service"] = True
        prefs.setdefault("password_manager", {})["saving_enabled"] = True
        prefs.setdefault("autofill", {})["profile_enabled"] = True
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
    except Exception:
        return

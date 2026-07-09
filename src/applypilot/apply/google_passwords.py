"""Google Password Manager integration through the user's Chrome profile.

ApplyPilot does not read, export, or create Google-stored passwords directly.
This module only selects a Chrome profile where browser-managed credentials can
autofill during visible apply runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from applypilot import config

PROVIDER_NAME = "google_password_manager"


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

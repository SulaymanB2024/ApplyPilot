"""1Password integration for autonomous apply account credentials."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from applypilot import config

DEFAULT_EXTENSION_ID = "aeblfdkhhhdcdjpifhhbdiojplfjncoa"
APPLYPILOT_TAG = "applypilot"


class OnePasswordError(RuntimeError):
    """Raised when 1Password is required but unavailable or fails."""


@dataclass(frozen=True)
class OnePasswordLogin:
    """A login item fetched from or created in 1Password."""

    item_id: str
    title: str
    username: str
    password: str
    url: str
    domain: str
    pending: bool = False


def domain_from_url(url: str | None) -> str:
    """Extract a normalized domain from a URL-like value."""
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.netloc or parsed.path
    return host.split("@")[-1].split(":")[0].lower().removeprefix("www.")


def redact_text(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """Redact secret-looking values and explicit secrets from a string."""
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")

    redacted = re.sub(
        r"(?i)(password|passwd|secret|token|api[_-]?key)(\s*[:=]\s*)([^\s,;]+)",
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"op://[^\s,;]+", "[REDACTED]", redacted)
    return redacted


def redact_data(value: Any, secrets: list[str] | tuple[str, ...] = ()) -> Any:
    """Recursively redact secret values from JSON-serializable data."""
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, list):
        return [redact_data(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item, secrets) for item in value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            key_s = str(key).lower()
            if any(word in key_s for word in ("password", "secret", "token", "api_key", "apikey")):
                out[key] = "[REDACTED]" if item else item
            else:
                out[key] = redact_data(item, secrets)
        return out
    return value


def chrome_profiles_with_extension(
    user_data_dir: Path | None = None,
    extension_id: str = DEFAULT_EXTENSION_ID,
) -> list[str]:
    """Return Chrome profile directory names that contain the extension."""
    root = user_data_dir or config.get_chrome_user_data()
    if not root.exists():
        return []

    profiles: list[str] = []
    candidates = [root / "Default"] + sorted(root.glob("Profile *"))
    for profile in candidates:
        if (profile / "Extensions" / extension_id).exists():
            profiles.append(profile.name)
    return profiles


def choose_chrome_profile_for_extension(
    user_data_dir: Path | None = None,
    extension_id: str = DEFAULT_EXTENSION_ID,
) -> str | None:
    """Choose the best Chrome profile directory for a required extension."""
    explicit = config.get_chrome_profile_directory()
    root = user_data_dir or config.get_chrome_user_data()
    if explicit and (root / explicit / "Extensions" / extension_id).exists():
        return explicit
    profiles = chrome_profiles_with_extension(root, extension_id)
    if "Default" in profiles:
        return "Default"
    return profiles[0] if profiles else None


def extract_field(item: dict[str, Any], field_ids: tuple[str, ...]) -> str:
    """Extract a field value from an `op item get --format json` response."""
    wanted = {field.lower() for field in field_ids}
    for field in item.get("fields", []):
        if not isinstance(field, dict):
            continue
        labels = {
            str(field.get("id", "")).lower(),
            str(field.get("label", "")).lower(),
            str(field.get("purpose", "")).lower(),
        }
        if labels & wanted:
            value = field.get("value")
            if value:
                return str(value)
    return ""


class OnePasswordClient:
    """Small wrapper around the 1Password CLI.

    The wrapper intentionally keeps all `op` calls in one module so tests can
    inject a fake runner and production logs do not accidentally print secrets.
    """

    def __init__(
        self,
        *,
        op_path: str | None = None,
        vault: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.op_path = op_path or shutil.which("op")
        self.vault = vault
        self._runner = runner

    def is_available(self) -> bool:
        """Return whether the `op` CLI can be found."""
        return bool(self.op_path)

    def require_ready(self) -> None:
        """Fail unless the 1Password CLI exists and is signed in."""
        if not self.op_path:
            raise OnePasswordError("1Password CLI `op` is not installed or not on PATH.")
        result = self._run(["whoami", "--format", "json"], check=False)
        if result.returncode != 0:
            raise OnePasswordError("1Password CLI is installed but not signed in. Run `op signin`.")

    def find_login(self, *, domain: str, username: str) -> OnePasswordLogin | None:
        """Find an existing login for a domain and username."""
        self.require_ready()
        domain = domain_from_url(domain)
        items = self._run_json(["item", "list", "--categories", "login", "--format", "json"])
        for summary in items if isinstance(items, list) else []:
            if not isinstance(summary, dict):
                continue
            candidate_title = str(summary.get("title", "")).lower()
            candidate_urls = " ".join(str(url) for url in summary.get("urls", [])).lower()
            if domain not in candidate_title and domain not in candidate_urls:
                continue
            item_id = str(summary.get("id") or "")
            if not item_id:
                continue
            item = self.get_item(item_id)
            item_username = extract_field(item, ("username",))
            if item_username.lower() == username.lower():
                return self._login_from_item(item, domain=domain)
        return None

    def get_item(self, item_id: str) -> dict[str, Any]:
        """Fetch one item by id."""
        args = ["item", "get", item_id, "--format", "json"]
        if self.vault:
            args.extend(["--vault", self.vault])
        item = self._run_json(args)
        if not isinstance(item, dict):
            raise OnePasswordError("1Password returned an unexpected item payload.")
        return item

    def create_login(
        self,
        *,
        title: str,
        domain: str,
        username: str,
        login_url: str,
        job_url: str,
        application_url: str,
        run_id: str,
    ) -> OnePasswordLogin:
        """Create a pending job-site login with a 1Password-generated password."""
        self.require_ready()
        created_at = datetime.now(timezone.utc).isoformat()
        args = [
            "item",
            "create",
            "--category",
            "login",
            "--title",
            title,
            "--tags",
            APPLYPILOT_TAG,
            "--format",
            "json",
            f"username={username}",
            "password[generate]=letters,digits,symbols,32",
            f"url={login_url}",
            f"ApplyPilot.domain[text]={domain_from_url(domain)}",
            f"ApplyPilot.job_url[text]={job_url}",
            f"ApplyPilot.application_url[text]={application_url}",
            f"ApplyPilot.run_id[text]={run_id}",
            "ApplyPilot.status[text]=pending",
            f"ApplyPilot.created_at[text]={created_at}",
        ]
        if self.vault:
            args.extend(["--vault", self.vault])
        item = self._run_json(args)
        if not isinstance(item, dict):
            raise OnePasswordError("1Password returned an unexpected create payload.")
        login = self._login_from_item(item, domain=domain_from_url(domain))
        return OnePasswordLogin(
            item_id=login.item_id,
            title=login.title,
            username=login.username or username,
            password=login.password,
            url=login.url or login_url,
            domain=domain_from_url(domain),
            pending=True,
        )

    def mark_created(self, item_id: str) -> None:
        """Mark a pending login as created after the website account succeeds."""
        self.require_ready()
        args = ["item", "edit", item_id, "ApplyPilot.status[text]=created"]
        if self.vault:
            args.extend(["--vault", self.vault])
        self._run(args)

    def _login_from_item(self, item: dict[str, Any], *, domain: str) -> OnePasswordLogin:
        username = extract_field(item, ("username",))
        password = extract_field(item, ("password", "credential"))
        urls = item.get("urls") if isinstance(item.get("urls"), list) else []
        url = ""
        if urls:
            first = urls[0]
            if isinstance(first, dict):
                url = str(first.get("href") or first.get("url") or "")
            else:
                url = str(first)
        url = url or extract_field(item, ("url", "website"))
        return OnePasswordLogin(
            item_id=str(item.get("id") or ""),
            title=str(item.get("title") or ""),
            username=username,
            password=password,
            url=url,
            domain=domain,
        )

    def _run_json(self, args: list[str]) -> Any:
        result = self._run(args)
        try:
            return json.loads(result.stdout or "null")
        except json.JSONDecodeError as exc:
            raise OnePasswordError("1Password returned invalid JSON.") from exc

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
        if not self.op_path:
            raise OnePasswordError("1Password CLI `op` is not installed or not on PATH.")
        result = self._runner(
            [self.op_path, *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check and result.returncode != 0:
            stderr = redact_text(result.stderr or "")
            raise OnePasswordError(f"1Password CLI failed: {stderr or 'unknown error'}")
        return result


def build_login_title(*, domain: str, company: str | None, email: str) -> str:
    """Build a deterministic 1Password item title for a job-site login."""
    label = company or domain_from_url(domain) or "Job Site"
    return f"ApplyPilot - {label} - {email}"

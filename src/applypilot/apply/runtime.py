"""Runtime queue hardening helpers for the apply stage."""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse


BREAKER_FAILURE_THRESHOLD = 3
BREAKER_COOLDOWN_SECONDS = 3600
RETRY_BASE_SECONDS = 5
RETRY_CAP_SECONDS = 60

TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {
    "gh_src",
    "ref",
    "source",
    "src",
    "campaign",
    "clickid",
    "fbclid",
    "gclid",
    "msclkid",
}
CANONICAL_QUERY_KEYS = {
    "gh_jid",
    "job_id",
    "jobid",
    "id",
    "requisitionid",
    "reqid",
    "lever-origin",
}
NULL_URL_VALUES = {"", "none", "null", "nan", "n/a", "na"}

BREAKER_REASONS = {
    "captcha",
    "cloudflare_blocked",
    "blocked_by_cloudflare",
    "sso_required",
    "mfa_required",
    "unsafe_verification",
    "payment_or_tax_info",
    "submitted_unconfirmed",
}


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime | None = None) -> str:
    """Serialize a UTC timestamp for SQLite text comparison."""
    return (value or utc_now()).astimezone(timezone.utc).isoformat()


def normalized_url_value(value: object | None) -> str:
    """Return a URL-ish string or empty for common provider null sentinels."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in NULL_URL_VALUES:
        return ""
    return text


def domain_from_job_url(url: object | None) -> str:
    """Return the canonical domain for a job/application URL."""
    normalized = normalized_url_value(url)
    if not normalized:
        return ""
    parsed = urlparse(normalized if "://" in normalized else f"https://{normalized}")
    host = parsed.netloc.lower()
    return host[4:] if host.startswith("www.") else host


def canonical_job_id(url: object | None, application_url: object | None = None) -> str:
    """Return a stable job identity key from a posting/application URL."""
    raw = normalized_url_value(application_url) or normalized_url_value(url)
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = domain_from_job_url(raw)
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
    query = dict(parse_qsl(parsed.query, keep_blank_values=False))

    ats_key = _ats_specific_key(host, path, query)
    if ats_key:
        return ats_key

    kept_query = {
        key.lower(): value
        for key, value in query.items()
        if _keep_query_key(key)
    }
    normalized_query = urlencode(sorted(kept_query.items()))
    suffix = f"?{normalized_query}" if normalized_query else ""
    return f"{host}{path.lower()}{suffix}"


def should_open_breaker(reason: str | None) -> bool:
    """Return whether a failure reason should count against the domain breaker."""
    if not reason:
        return False
    normalized = reason.split(":", 1)[-1]
    return normalized in BREAKER_REASONS or normalized.startswith("cloudflare")


def full_jitter_delay_seconds(
    attempt: int,
    *,
    base_seconds: int = RETRY_BASE_SECONDS,
    cap_seconds: int = RETRY_CAP_SECONDS,
    rng: random.Random | None = None,
) -> float:
    """Return capped exponential backoff with full jitter."""
    generator = rng or random
    exponent = max(attempt - 1, 0)
    upper = min(cap_seconds, base_seconds * (2**exponent))
    return generator.uniform(0, upper)


def next_retry_at(
    attempt: int,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> str:
    """Return the next retry timestamp for a retryable apply failure."""
    start = now or utc_now()
    delay = full_jitter_delay_seconds(attempt, rng=rng)
    return isoformat_utc(start + timedelta(seconds=delay))


def breaker_open_until(now: datetime | None = None) -> str:
    """Return when an opened domain breaker may be tried again."""
    start = now or utc_now()
    return isoformat_utc(start + timedelta(seconds=BREAKER_COOLDOWN_SECONDS))


def _ats_specific_key(host: str, path: str, query: dict[str, str]) -> str:
    normalized_path = path.lower()
    lowered_query = {key.lower(): value for key, value in query.items()}
    if "greenhouse.io" in host:
        job_id = lowered_query.get("gh_jid") or lowered_query.get("job_id")
        if not job_id:
            match = re.search(r"/jobs/(\d+)", normalized_path)
            job_id = match.group(1) if match else ""
        if job_id:
            return f"greenhouse:{job_id}"
    if "jobs.lever.co" in host:
        parts = [part for part in path.strip("/").split("/") if part]
        if len(parts) >= 2:
            return f"lever:{parts[0].lower()}:{parts[-1].lower()}"
    if "myworkdayjobs.com" in host:
        match = re.search(r"(?:^|[^a-z0-9])((?:r|jr|req)[-_]?\d{3,})(?:\b|$)", normalized_path)
        if match:
            return f"workday:{host}:{match.group(1).replace('_', '-')}"
    if "ashbyhq.com" in host:
        parts = [part for part in path.strip("/").split("/") if part]
        if len(parts) >= 2:
            return f"ashby:{parts[0].lower()}:{parts[-1].lower()}"
    return ""


def _keep_query_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in CANONICAL_QUERY_KEYS:
        return True
    if lowered in TRACKING_QUERY_KEYS:
        return False
    return not any(lowered.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES)

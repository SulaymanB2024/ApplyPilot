"""Deterministic first-party verification for ChatGPT-discovered candidates."""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse

from applypilot import config
from applypilot.autonomy.models import FreshnessEvidence, RoleCandidate
from applypilot.autonomy.telemetry import UsageLedger

ATS_HOSTS = {
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
}
DISALLOWED_HOST_MARKERS = (
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "google.com",
)
CLOSED_MARKERS = (
    "job is no longer available",
    "no longer accepting applications",
    "position has been filled",
    "posting has expired",
    "job not found",
)
CHALLENGE_MARKERS = (
    "verify you are human",
    "checking your browser",
    "attention required",
    "cf-chl-",
    "captcha",
)


@dataclass(frozen=True)
class FetchResponse:
    status_code: int
    url: str
    text: str
    payload: Any = None


class Transport(Protocol):
    def get(self, url: str, *, expect_json: bool = False, timeout: int = 20) -> FetchResponse: ...


class URLTransport:
    """Small urllib transport with bounded response reads."""

    def get(self, url: str, *, expect_json: bool = False, timeout: int = 20) -> FetchResponse:
        assert_public_http_url(url)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json" if expect_json else "text/html,application/xhtml+xml",
                "User-Agent": "Mozilla/5.0 (compatible; ApplyPilot/0.3; +https://github.com/Pickle-Pixel/ApplyPilot)",
            },
        )
        opener = urllib.request.build_opener(_SafeRedirectHandler())
        try:
            with opener.open(request, timeout=timeout) as response:
                assert_public_http_url(response.geturl())
                raw = response.read(1_000_000)
                text = raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
                payload = json.loads(text) if expect_json else None
                return FetchResponse(response.status, response.geturl(), text, payload)
        except urllib.error.HTTPError as exc:
            assert_public_http_url(exc.geturl())
            raw = exc.read(100_000)
            return FetchResponse(
                exc.code,
                url,
                raw.decode("utf-8", errors="replace"),
                None,
            )


class FirstPartyVerifier:
    """Verify a role against its employer/ATS surface without a model call."""

    def __init__(
        self,
        *,
        ledger: UsageLedger,
        transport: Transport | None = None,
        trusted_hosts: set[str] | None = None,
    ):
        self.ledger = ledger
        self.transport = transport or URLTransport()
        self.trusted_hosts = {host.lower().removeprefix("www.") for host in (trusted_hosts or set())}

    def verify(self, candidate: RoleCandidate) -> FreshnessEvidence:
        parsed = urlparse(candidate.official_url)
        host = (parsed.hostname or "").lower()
        if (
            not _url_is_structurally_public(candidate.official_url)
            or any(marker in host for marker in DISALLOWED_HOST_MARKERS)
        ):
            return FreshnessEvidence.now(
                official_url=candidate.official_url,
                first_party=False,
                resolved=False,
                open_state=None,
                evidence=(f"host={host or 'missing'}",),
            )

        started = time.monotonic()
        self.ledger.reserve("external_calls")
        try:
            if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
                result = self._verify_greenhouse(candidate)
            elif host == "jobs.lever.co":
                result = self._verify_lever(candidate)
            elif host == "jobs.ashbyhq.com":
                result = self._verify_html(candidate, first_party=True)
            else:
                normalized_host = host.removeprefix("www.")
                first_party = normalized_host in self.trusted_hosts
                if not first_party:
                    result = FreshnessEvidence.now(
                        official_url=candidate.official_url,
                        first_party=False,
                        resolved=False,
                        open_state=None,
                        provider_error="untrusted_company_host",
                        evidence=(f"company_domain_mismatch={normalized_host}",),
                    )
                else:
                    result = self._verify_html(candidate, first_party=True)
        except Exception as exc:
            result = FreshnessEvidence.now(
                official_url=candidate.official_url,
                first_party=host in ATS_HOSTS,
                resolved=False,
                open_state=None,
                provider_error=f"{type(exc).__name__}: {str(exc)[:160]}",
                evidence=(f"host={host}",),
            )
        self.ledger.record_event(
            stage="verification",
            operation="verify_first_party",
            surface=host,
            status="ok" if result.resolved else "gap",
            duration_ms=int((time.monotonic() - started) * 1000),
            error_class=result.provider_error,
        )
        return result

    def _verify_greenhouse(self, candidate: RoleCandidate) -> FreshnessEvidence:
        parsed = urlparse(candidate.official_url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 3 or parts[-2] != "jobs":
            return self._verify_html(candidate, first_party=True)
        board, job_id = parts[0], parts[-1]
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?content=true"
        response = self.transport.get(url, expect_json=True)
        payload = response.payload if isinstance(response.payload, dict) else {}
        if response.status_code >= 400 or not payload.get("id"):
            return FreshnessEvidence.now(
                official_url=candidate.official_url,
                first_party=True,
                resolved=False,
                open_state=False if response.status_code in {404, 410} else None,
                status_code=response.status_code,
                evidence=(f"greenhouse_api_status={response.status_code}",),
            )
        description = _clean_text(str(payload.get("content") or ""))
        return FreshnessEvidence.now(
            official_url=str(payload.get("absolute_url") or candidate.official_url),
            first_party=True,
            resolved=True,
            open_state=True,
            posted_date=_parse_api_date(payload.get("first_published") or payload.get("created_at")),
            updated_date=_parse_api_date(payload.get("updated_at")),
            start_window=candidate.start_window,
            status_code=response.status_code,
            title=str(payload.get("title") or ""),
            description=description[:20_000],
            evidence=(f"greenhouse_job_id={payload.get('id')}",),
        )

    def _verify_lever(self, candidate: RoleCandidate) -> FreshnessEvidence:
        parts = [part for part in urlparse(candidate.official_url).path.split("/") if part]
        if len(parts) < 2:
            return self._verify_html(candidate, first_party=True)
        company, posting_id = parts[0], parts[1]
        response = self.transport.get(
            f"https://api.lever.co/v0/postings/{company}/{posting_id}",
            expect_json=True,
        )
        payload = response.payload if isinstance(response.payload, dict) else {}
        if response.status_code >= 400 or not payload.get("id"):
            return FreshnessEvidence.now(
                official_url=candidate.official_url,
                first_party=True,
                resolved=False,
                open_state=False if response.status_code in {404, 410} else None,
                status_code=response.status_code,
                evidence=(f"lever_api_status={response.status_code}",),
            )
        description = _clean_text(
            str(payload.get("descriptionPlain") or payload.get("description") or "")
        )
        return FreshnessEvidence.now(
            official_url=str(payload.get("hostedUrl") or candidate.official_url),
            first_party=True,
            resolved=True,
            open_state=True,
            posted_date=_parse_api_date(payload.get("createdAt")),
            start_window=candidate.start_window,
            status_code=response.status_code,
            title=str(payload.get("text") or ""),
            description=description[:20_000],
            evidence=(f"lever_job_id={payload.get('id')}",),
        )

    def _verify_html(self, candidate: RoleCandidate, *, first_party: bool) -> FreshnessEvidence:
        path = urlparse(candidate.official_url).path.strip("/")
        if not path:
            return FreshnessEvidence.now(
                official_url=candidate.official_url,
                first_party=first_party,
                resolved=False,
                open_state=None,
                provider_error="non_job_specific_url",
                evidence=("url_path=root",),
            )
        response = self.transport.get(candidate.official_url)
        lower = response.text.lower()
        challenge = any(marker in lower for marker in CHALLENGE_MARKERS)
        closed = any(marker in lower for marker in CLOSED_MARKERS)
        title_match = _page_matches_title(response.text, candidate.title)
        resolved = 200 <= response.status_code < 400 and not challenge and title_match
        open_state: bool | None = None if challenge else (False if closed else (True if resolved else None))
        return FreshnessEvidence.now(
            official_url=response.url,
            first_party=first_party,
            resolved=resolved,
            open_state=open_state,
            posted_date=_extract_structured_date(response.text, "datePosted"),
            updated_date=_extract_structured_date(response.text, "dateModified"),
            start_window=None,
            status_code=response.status_code,
            title=candidate.title,
            description=_clean_text(response.text)[:20_000],
            evidence=(f"http_status={response.status_code}",),
            provider_error=(
                "challenge_page"
                if challenge
                else "job_title_not_found"
                if not title_match
                else ""
            ),
        )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _url_is_structurally_public(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return False
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return address.is_global


def assert_public_http_url(url: str) -> None:
    if not _url_is_structurally_public(url):
        raise ValueError("URL is not a public HTTP(S) destination")
    parsed = urlparse(url)
    host = parsed.hostname or ""
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise ValueError("URL hostname did not resolve") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("URL resolved to a non-public address")


def _clean_text(value: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_api_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).date()
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _extract_structured_date(page_text: str, key: str) -> date | None:
    match = re.search(
        rf'["\']{re.escape(key)}["\']\s*:\s*["\']([^"\']+)["\']',
        page_text,
        re.I,
    )
    return _parse_api_date(match.group(1)) if match else None


def _page_matches_title(page_text: str, title: str) -> bool:
    normalized_page = re.sub(r"[^a-z0-9]+", " ", page_text.lower()).strip()
    normalized_title = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    if normalized_title and normalized_title in normalized_page:
        return True
    title_tokens = {
        token
        for token in normalized_title.split()
        if len(token) > 2 and token not in {"job", "role", "intern", "internship"}
    }
    if not title_tokens:
        return False
    overlap = title_tokens & set(normalized_page.split())
    required = 1 if len(title_tokens) == 1 else max(2, (len(title_tokens) + 1) // 2)
    return len(overlap) >= required


def configured_trusted_hosts() -> set[str]:
    """Return only user/package-configured direct-source hosts."""
    hosts: set[str] = set()
    for site in config.load_sites_config().get("sites", []):
        if not isinstance(site, dict) or not site.get("direct_source"):
            continue
        host = (urlparse(str(site.get("url") or "")).hostname or "").lower().removeprefix("www.")
        if host:
            hosts.add(host)
    return hosts

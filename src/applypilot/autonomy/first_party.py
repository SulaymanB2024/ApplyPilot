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
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from applypilot import config
from applypilot.autonomy.models import DateWindow, FreshnessEvidence, RoleCandidate
from applypilot.autonomy.telemetry import UsageLedger
from applypilot.employment import (
    ApplicationSurface,
    OpportunityKind,
    classify_opportunity,
)

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
HOSTED_ATS_SUFFIXES = (
    "avature.net",
    "csod.com",
    "icims.com",
    "myworkdayjobs.com",
    "recsolu.com",
)
COMMON_COUNTRY_SECOND_LEVEL_SUFFIXES = {
    "ac",
    "co",
    "com",
    "edu",
    "gov",
    "net",
    "org",
}
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


@dataclass(frozen=True)
class TrustedFirstPartySource:
    """Exact configured employer/ATS identity, not a generic trusted host."""

    company: str
    host: str
    path_prefix: str
    source_kind: str


class CachedFirstPartyVerifier:
    """Persist immutable verification evidence so resumptions do not refetch roles."""

    SCHEMA_VERSION = "applypilot-first-party-cache-v4"
    CACHE_NAMESPACE = "v4"

    def __init__(self, delegate: Any, *, cache_dir: Path) -> None:
        self.delegate = delegate
        self.cache_dir = cache_dir.resolve()

    def verify(self, candidate: RoleCandidate) -> FreshnessEvidence:
        path = self.cache_dir / f"{candidate.candidate_id}.{self.CACHE_NAMESPACE}.json"
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("schema_version") != self.SCHEMA_VERSION
                or payload.get("candidate_id") != candidate.candidate_id
                or payload.get("requested_url") != candidate.official_url
            ):
                raise ValueError("first-party cache binding mismatch")
            return _freshness_from_cache(payload.get("evidence"))

        evidence = self.delegate.verify(candidate)
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "candidate_id": candidate.candidate_id,
            "requested_url": candidate.official_url,
            "evidence": _freshness_to_cache(evidence),
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        return evidence


def _freshness_to_cache(evidence: FreshnessEvidence) -> dict[str, Any]:
    start_window = None
    if evidence.start_window is not None:
        start_window = {
            "start": evidence.start_window.start.isoformat(),
            "end": evidence.start_window.end.isoformat(),
            "label": evidence.start_window.label,
        }
    return {
        "official_url": evidence.official_url,
        "fetched_at": evidence.fetched_at.isoformat(),
        "first_party": evidence.first_party,
        "resolved": evidence.resolved,
        "open_state": evidence.open_state,
        "posted_date": evidence.posted_date.isoformat() if evidence.posted_date else None,
        "updated_date": evidence.updated_date.isoformat() if evidence.updated_date else None,
        "start_window": start_window,
        "status_code": evidence.status_code,
        "title": evidence.title,
        "description": evidence.description,
        "evidence": list(evidence.evidence),
        "provider_error": evidence.provider_error,
        "opportunity_kind": evidence.opportunity_kind.value,
        "application_surface": evidence.application_surface.value,
        "requisition_id": evidence.requisition_id,
    }


def _freshness_from_cache(raw: Any) -> FreshnessEvidence:
    if not isinstance(raw, dict):
        raise ValueError("first-party cache evidence is invalid")
    start_raw = raw.get("start_window")
    start_window = None
    if isinstance(start_raw, dict):
        start_window = DateWindow(
            start=date.fromisoformat(str(start_raw.get("start"))),
            end=date.fromisoformat(str(start_raw.get("end"))),
            label=str(start_raw.get("label") or ""),
        )
    fetched_at = datetime.fromisoformat(str(raw.get("fetched_at") or ""))
    if fetched_at.tzinfo is None:
        raise ValueError("first-party cache timestamp must be timezone aware")
    return FreshnessEvidence(
        official_url=str(raw.get("official_url") or ""),
        fetched_at=fetched_at,
        first_party=bool(raw.get("first_party")),
        resolved=bool(raw.get("resolved")),
        open_state=raw.get("open_state") if raw.get("open_state") in {True, False, None} else None,
        posted_date=date.fromisoformat(str(raw["posted_date"])) if raw.get("posted_date") else None,
        updated_date=date.fromisoformat(str(raw["updated_date"])) if raw.get("updated_date") else None,
        start_window=start_window,
        status_code=int(raw["status_code"]) if raw.get("status_code") is not None else None,
        title=str(raw.get("title") or ""),
        description=str(raw.get("description") or ""),
        evidence=tuple(str(item) for item in (raw.get("evidence") or [])),
        provider_error=str(raw.get("provider_error") or ""),
        opportunity_kind=OpportunityKind(
            str(raw.get("opportunity_kind") or OpportunityKind.UNKNOWN.value)
        ),
        application_surface=ApplicationSurface(
            str(raw.get("application_surface") or ApplicationSurface.UNKNOWN.value)
        ),
        requisition_id=str(raw.get("requisition_id") or ""),
    )


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
        trusted_sources: tuple[TrustedFirstPartySource, ...] = (),
    ):
        self.ledger = ledger
        self.transport = transport or URLTransport()
        self.trusted_sources = trusted_sources

    def verify(self, candidate: RoleCandidate) -> FreshnessEvidence:
        parsed = urlparse(candidate.official_url)
        host = (parsed.hostname or "").lower()
        initial = classify_opportunity(
            title=candidate.title,
            description=candidate.description,
            official_url=candidate.official_url,
            application_surface=candidate.application_surface,
            requisition_id=candidate.requisition_id,
        )
        if initial.kind in {
            OpportunityKind.MARKETPLACE_GIG,
            OpportunityKind.MICROTASK_PLATFORM,
            OpportunityKind.ASSESSMENT_OR_PROFILE_SIGNUP,
        }:
            return FreshnessEvidence.now(
                official_url=candidate.official_url,
                first_party=False,
                resolved=False,
                open_state=None,
                provider_error="non_employment_surface",
                evidence=initial.reason_codes,
                opportunity_kind=initial.kind,
                application_surface=initial.application_surface,
            )
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

        first_party = self._url_matches_candidate(candidate.company, candidate.official_url)
        if not first_party:
            return FreshnessEvidence.now(
                official_url=candidate.official_url,
                first_party=False,
                resolved=False,
                open_state=None,
                provider_error="untrusted_company_host",
                evidence=(f"company_or_tenant_mismatch={host}",),
            )

        started = time.monotonic()
        self.ledger.reserve("external_calls")
        try:
            if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
                result = self._verify_greenhouse(candidate)
            elif host == "jobs.lever.co":
                result = self._verify_lever(candidate)
            elif host.endswith(".myworkdayjobs.com"):
                result = self._verify_workday(candidate)
            elif host == "jobs.ashbyhq.com":
                result = self._verify_html(candidate, first_party=True)
            else:
                result = self._verify_html(candidate, first_party=True)
        except Exception as exc:
            result = FreshnessEvidence.now(
                official_url=candidate.official_url,
                first_party=first_party,
                resolved=False,
                open_state=None,
                provider_error=f"{type(exc).__name__}: {str(exc)[:160]}",
                evidence=(f"host={host}",),
            )
        classification = classify_opportunity(
            title=result.title or candidate.title,
            description=result.description or candidate.description,
            official_url=result.official_url,
            application_surface=result.application_surface,
            requisition_id=result.requisition_id,
        )
        result = replace(
            result,
            opportunity_kind=classification.kind,
            application_surface=classification.application_surface,
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
        official_url = str(payload.get("absolute_url") or candidate.official_url)
        if not self._url_matches_candidate(candidate.company, official_url):
            return FreshnessEvidence.now(
                official_url=official_url,
                first_party=False,
                resolved=False,
                open_state=None,
                status_code=response.status_code,
                provider_error="untrusted_api_url",
                evidence=("greenhouse_company_or_tenant_mismatch",),
            )
        description = _clean_text(str(payload.get("content") or ""))
        return FreshnessEvidence.now(
            official_url=official_url,
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
            application_surface=ApplicationSurface.PROVIDER_REQUISITION,
            requisition_id=str(payload.get("id") or ""),
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
        official_url = str(payload.get("hostedUrl") or candidate.official_url)
        if not self._url_matches_candidate(candidate.company, official_url):
            return FreshnessEvidence.now(
                official_url=official_url,
                first_party=False,
                resolved=False,
                open_state=None,
                status_code=response.status_code,
                provider_error="untrusted_api_url",
                evidence=("lever_company_or_tenant_mismatch",),
            )
        description = _clean_text(
            str(payload.get("descriptionPlain") or payload.get("description") or "")
        )
        return FreshnessEvidence.now(
            official_url=official_url,
            first_party=True,
            resolved=True,
            open_state=True,
            posted_date=_parse_api_date(payload.get("createdAt")),
            start_window=candidate.start_window,
            status_code=response.status_code,
            title=str(payload.get("text") or ""),
            description=description[:20_000],
            evidence=(f"lever_job_id={payload.get('id')}",),
            application_surface=ApplicationSurface.PROVIDER_REQUISITION,
            requisition_id=str(payload.get("id") or ""),
        )

    def _verify_workday(self, candidate: RoleCandidate) -> FreshnessEvidence:
        """Resolve a Workday role through its public CXS JSON endpoint.

        The shell page often contains only the title. Treating that shell as the
        complete posting can hide hard qualifications, so Workday verification
        fails closed unless the job-specific JSON payload is available.
        """
        parsed = urlparse(candidate.official_url)
        host = (parsed.hostname or "").lower()
        parts = [part for part in parsed.path.split("/") if part]
        try:
            job_index = parts.index("job")
        except ValueError:
            job_index = -1
        if job_index < 1 or job_index == len(parts) - 1:
            return FreshnessEvidence.now(
                official_url=candidate.official_url,
                first_party=True,
                resolved=False,
                open_state=None,
                provider_error="workday_job_path_unrecognized",
                evidence=(f"host={host}",),
            )

        tenant = host.split(".", 1)[0]
        site = parts[job_index - 1]
        job_path = "/".join(parts[job_index + 1 :])
        api_url = f"https://{host}/wday/cxs/{tenant}/{site}/job/{job_path}"
        response = self.transport.get(api_url, expect_json=True)
        payload = response.payload if isinstance(response.payload, dict) else {}
        posting = payload.get("jobPostingInfo")
        posting = posting if isinstance(posting, dict) else {}
        title = str(posting.get("title") or "")
        job_id = posting.get("id")
        if response.status_code >= 400 or not job_id or not title:
            return FreshnessEvidence.now(
                official_url=candidate.official_url,
                first_party=True,
                resolved=False,
                open_state=False if response.status_code in {404, 410} else None,
                status_code=response.status_code,
                provider_error="workday_api_job_missing",
                evidence=(f"workday_api_status={response.status_code}",),
            )
        if not _page_matches_title(title, candidate.title):
            return FreshnessEvidence.now(
                official_url=candidate.official_url,
                first_party=True,
                resolved=False,
                open_state=None,
                status_code=response.status_code,
                title=title,
                provider_error="workday_title_mismatch",
                evidence=(f"workday_job_id={job_id}",),
            )

        description = _clean_text(str(posting.get("jobDescription") or ""))
        if not description:
            return FreshnessEvidence.now(
                official_url=candidate.official_url,
                first_party=True,
                resolved=False,
                open_state=None,
                status_code=response.status_code,
                title=title,
                provider_error="workday_description_missing",
                evidence=(f"workday_job_id={job_id}",),
            )
        return FreshnessEvidence.now(
            official_url=candidate.official_url,
            first_party=True,
            resolved=True,
            open_state=True,
            start_window=candidate.start_window,
            status_code=response.status_code,
            title=title,
            description=description[:20_000],
            evidence=(f"workday_job_id={job_id}",),
            application_surface=ApplicationSurface.PROVIDER_REQUISITION,
            requisition_id=str(job_id),
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
        if not self._url_matches_candidate(candidate.company, response.url):
            return FreshnessEvidence.now(
                official_url=response.url,
                first_party=False,
                resolved=False,
                open_state=None,
                status_code=response.status_code,
                provider_error="untrusted_redirect_target",
                evidence=("redirect_company_or_tenant_mismatch",),
            )
        lower = response.text.lower()
        challenge = any(marker in lower for marker in CHALLENGE_MARKERS)
        closed = any(marker in lower for marker in CLOSED_MARKERS)
        title_match = _page_matches_title(response.text, candidate.title)
        surface, surface_evidence = _html_application_surface(response.text)
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
            evidence=(f"http_status={response.status_code}", *surface_evidence),
            provider_error=(
                "challenge_page"
                if challenge
                else "job_title_not_found"
                if not title_match
                else ""
            ),
            application_surface=surface,
        )

    def _url_matches_candidate(self, company: str, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = "/" + parsed.path.strip("/")
        configured_match = any(
            source.source_kind in {"direct_ats", "employer_careers"}
            and host == source.host
            and _company_identity_matches(company, source.company)
            and _path_is_within(path, source.path_prefix)
            for source in self.trusted_sources
        )
        if configured_match:
            return True
        if host in ATS_HOSTS:
            return _shared_ats_tenant_matches(company, host, path)
        if any(host == suffix or host.endswith(f".{suffix}") for suffix in HOSTED_ATS_SUFFIXES):
            return _hosted_ats_tenant_matches(company, host)
        return _direct_employer_host_matches(company, host, path)


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


def _company_tokens(company: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", company.lower())
        if token not in {"and", "co", "company", "corp", "corporation", "inc", "llc", "the"}
    ]


def _company_identity_matches(candidate_company: str, configured_company: str) -> bool:
    candidate = "".join(_company_tokens(candidate_company))
    configured = "".join(_company_tokens(configured_company))
    return bool(candidate and configured and candidate == configured)


def _tenant_matches_company(company: str, tenant: str) -> bool:
    company_tokens = _company_tokens(company)
    if not company_tokens:
        return False
    compact_company = "".join(company_tokens)
    tenant_tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", tenant.lower())
        if token not in {"career", "careers", "job", "jobs", "recruiting"}
    ]
    comparison_label = "".join(tenant_tokens)
    if not comparison_label:
        return False
    return len(compact_company) >= 4 and compact_company == comparison_label


def _shared_ats_tenant_matches(company: str, host: str, path: str) -> bool:
    if host not in ATS_HOSTS:
        return False
    parts = [part for part in path.split("/") if part]
    return bool(parts) and _tenant_matches_company(company, parts[0])


def _hosted_ats_tenant_matches(company: str, host: str) -> bool:
    if not any(host.endswith(f".{suffix}") for suffix in HOSTED_ATS_SUFFIXES):
        return False
    return _tenant_matches_company(company, host.split(".", 1)[0])


def _direct_employer_host_matches(company: str, host: str, path: str) -> bool:
    """Recognize a job-specific employer URL with an exact registrable-name binding."""
    if not path.strip("/"):
        return False
    registrable_label = _registrable_host_label(host)
    return bool(registrable_label and registrable_label in _company_domain_labels(company))


def _company_domain_labels(company: str) -> set[str]:
    normalized_tokens = re.findall(r"[a-z0-9]+", company.lower())
    variants = {
        "".join(_company_tokens(company)),
        "".join(token for token in normalized_tokens if token not in {"and", "the"}),
    }
    # Exact direct domains such as drw.com and ibm.com are valid employer
    # bindings even though their normalized company labels are three letters.
    # Shared ATS tenant matching keeps its stricter four-character threshold.
    return {variant for variant in variants if len(variant) >= 3}


def _registrable_host_label(host: str) -> str:
    labels = [label for label in host.lower().strip(".").split(".") if label]
    if len(labels) < 2:
        return ""
    label_index = -2
    if (
        len(labels) >= 3
        and len(labels[-1]) == 2
        and labels[-2] in COMMON_COUNTRY_SECOND_LEVEL_SUFFIXES
    ):
        label_index = -3
    return re.sub(r"[^a-z0-9]+", "", labels[label_index])


def _path_is_within(path: str, prefix: str) -> bool:
    normalized_path = "/" + path.strip("/")
    prefix_body = prefix.strip("/")
    if not prefix_body:
        return True
    normalized_prefix = "/" + prefix_body
    return normalized_path == normalized_prefix or normalized_path.startswith(
        f"{normalized_prefix}/"
    )


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


def _html_application_surface(
    page_text: str,
) -> tuple[ApplicationSurface, tuple[str, ...]]:
    """Recognize explicit job/application evidence, never a title match alone."""
    if re.search(
        r'["\']@type["\']\s*:\s*["\']JobPosting["\']',
        page_text,
        re.IGNORECASE,
    ):
        return (
            ApplicationSurface.JOB_POSTING_STRUCTURED_DATA,
            ("structured_data_type=JobPosting",),
        )
    visible = _clean_text(page_text).casefold()
    has_form = re.search(r"<form\b[^>]*>", page_text, re.IGNORECASE) is not None
    if has_form and any(
        marker in visible
        for marker in (
            "general application",
            "general interest",
            "register your interest",
        )
    ):
        return ApplicationSurface.GENERAL_INTEREST_FORM, ("general_interest_form",)
    if has_form and any(
        marker in visible
        for marker in ("apply now", "apply for this job", "submit application")
    ):
        return ApplicationSurface.JOB_APPLICATION_FORM, ("job_application_form",)
    if re.search(
        r'<(?:a|button)\b[^>]*(?:href=["\'][^"\']*(?:apply|application)[^"\']*["\'])?'
        r"[^>]*>[^<]{0,80}\bapply(?: now| for this job)?\b",
        page_text,
        re.IGNORECASE,
    ):
        return ApplicationSurface.JOB_APPLICATION_FORM, ("job_apply_control",)
    return ApplicationSurface.UNKNOWN, ()


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


def configured_trusted_sources() -> tuple[TrustedFirstPartySource, ...]:
    """Return exact employer/ATS identities, excluding recruiter/aggregator surfaces."""
    sources: list[TrustedFirstPartySource] = []
    for site in config.load_sites_config().get("sites", []):
        if not isinstance(site, dict) or not site.get("direct_source"):
            continue
        source_kind = str(site.get("source_kind") or "")
        company = str(site.get("company") or "").strip()
        parsed = urlparse(str(site.get("url") or ""))
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if source_kind not in {"direct_ats", "employer_careers"} or not company or not host:
            continue
        sources.append(
            TrustedFirstPartySource(
                company=company,
                host=host,
                path_prefix="/" + parsed.path.strip("/"),
                source_kind=source_kind,
            )
        )
    return tuple(sources)

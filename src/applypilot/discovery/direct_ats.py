"""Direct ATS discovery for employer-hosted recruiter pages.

This module intentionally avoids aggregator search. It reads employer-owned
Greenhouse, Lever, and Ashby board URLs from ``searches.yaml`` and stores jobs
from their public board APIs.
"""

from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

from applypilot import config
from applypilot.apply.runtime import canonical_job_id, domain_from_job_url
from applypilot.database import get_connection, init_db

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
SUPPORTED_ATS = {"greenhouse", "lever", "ashby"}
STOPWORDS = {
    "a",
    "ai",
    "and",
    "for",
    "ii",
    "iii",
    "intern",
    "internship",
    "new",
    "of",
    "the",
    "to",
    "grad",
}


def _request_json(url: str, *, method: str = "GET", payload: dict | None = None, timeout: int = 30) -> dict | list:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", UA)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip() or None


def infer_source_from_url(source: dict) -> dict:
    """Fill ``ats`` and ``slug`` from a common public ATS board URL."""
    result = dict(source)
    if result.get("ats") and result.get("slug"):
        return result

    url = result.get("url", "")
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    parts = [part for part in parsed.path.split("/") if part]

    if "greenhouse.io" in host and parts:
        result.setdefault("ats", "greenhouse")
        result.setdefault("slug", parts[0])
    elif host == "jobs.lever.co" and parts:
        result.setdefault("ats", "lever")
        result.setdefault("slug", parts[0])
    elif host == "jobs.ashbyhq.com" and parts:
        result.setdefault("ats", "ashby")
        result.setdefault("slug", parts[0])

    return result


def load_direct_ats_sources(search_cfg: dict | None = None) -> list[dict]:
    """Load and normalize direct ATS sources from search config."""
    if search_cfg is None:
        search_cfg = config.load_search_config()

    sources = []
    for raw in search_cfg.get("direct_ats_sources", []) or []:
        source = infer_source_from_url(raw)
        ats = str(source.get("ats", "")).lower()
        slug = source.get("slug")
        if ats not in SUPPORTED_ATS or not slug:
            log.warning("Skipping unsupported direct ATS source: %s", raw)
            continue
        source["ats"] = ats
        source["slug"] = str(slug)
        source.setdefault("name", source["slug"])
        sources.append(source)
    return sources


def _search_terms(search_cfg: dict) -> list[set[str]]:
    terms: list[set[str]] = []
    max_tier = int(search_cfg.get("direct_ats_max_tier", search_cfg.get("workday_max_tier", 2)))
    for query_cfg in search_cfg.get("queries", []) or []:
        if int(query_cfg.get("tier", 99)) > max_tier:
            continue
        query = str(query_cfg.get("query", ""))
        tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", query.lower())
            if len(token) > 1 and token not in STOPWORDS
        }
        if tokens:
            terms.append(tokens)
    return terms


def _keyword_terms(search_cfg: dict) -> list[str]:
    configured = search_cfg.get("direct_ats_title_keywords")
    if configured:
        return [str(item).lower() for item in configured]
    return ["intern", "internship", "new grad", "graduate", "early career", "analyst"]


def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    if not location:
        return True
    loc = location.lower()
    if any(_location_pattern_matches(pattern, loc) for pattern in reject):
        return False
    if any(remote in loc for remote in ("remote", "anywhere", "united states")):
        return True
    if re.search(r"\b(u\.?s\.?a?|united states)\b", loc):
        return True
    return not accept or any(_location_pattern_matches(pattern, loc) for pattern in accept)


def _location_pattern_matches(pattern: str, loc: str) -> bool:
    normalized = pattern.lower().strip()
    if normalized in {"us", "u.s.", "usa", "u.s.a."}:
        return bool(re.search(r"\b(u\.?s\.?a?|united states)\b", loc))
    if len(normalized) <= 2:
        return bool(re.search(rf"\b{re.escape(normalized)}\b", loc))
    return normalized in loc


def _title_ok(title: str | None, search_cfg: dict) -> bool:
    if not search_cfg.get("direct_ats_filter_titles", True):
        return True
    normalized = (title or "").lower()
    if not normalized:
        return False
    excludes = [str(item).lower() for item in search_cfg.get("exclude_titles", []) or []]
    if any(exclude in normalized for exclude in excludes):
        return False

    keywords = _keyword_terms(search_cfg)
    if any(keyword in normalized for keyword in keywords):
        return True

    title_tokens = set(re.findall(r"[a-z0-9]+", normalized))
    return any(tokens.issubset(title_tokens) for tokens in _search_terms(search_cfg))


def _filter_jobs(jobs: list[dict], search_cfg: dict) -> list[dict]:
    accept = search_cfg.get("location_accept", [])
    reject = search_cfg.get("location_reject_non_remote", [])
    location_filter = search_cfg.get("direct_ats_location_filter", True)

    filtered = []
    for job in jobs:
        if not _title_ok(job.get("title"), search_cfg):
            continue
        if location_filter and not _location_ok(job.get("location"), accept, reject):
            continue
        filtered.append(job)
    return filtered


def _greenhouse_jobs(source: dict) -> list[dict]:
    slug = source["slug"]
    payload = _request_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    return [
        {
            "url": job.get("absolute_url"),
            "application_url": job.get("absolute_url"),
            "title": job.get("title"),
            "location": (job.get("location") or {}).get("name"),
            "description": _clean_text(job.get("content")),
            "full_description": _clean_text(job.get("content")),
        }
        for job in jobs
    ]


def _lever_jobs(source: dict) -> list[dict]:
    slug = source["slug"]
    payload = _request_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    jobs = payload if isinstance(payload, list) else []
    return [
        {
            "url": job.get("hostedUrl"),
            "application_url": job.get("applyUrl") or job.get("hostedUrl"),
            "title": job.get("text"),
            "location": (job.get("categories") or {}).get("location"),
            "description": _clean_text(job.get("descriptionPlain") or job.get("description")),
            "full_description": _clean_text(job.get("descriptionPlain") or job.get("description")),
        }
        for job in jobs
    ]


ASHBY_QUERY = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) {
    jobPostings {
      id
      title
      locationName
      employmentType
    }
  }
}
"""


def _ashby_jobs(source: dict) -> list[dict]:
    slug = source["slug"]
    payload = _request_json(
        "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams",
        method="POST",
        payload={
            "operationName": "ApiJobBoardWithTeams",
            "variables": {"organizationHostedJobsPageName": slug},
            "query": ASHBY_QUERY,
        },
    )
    if not isinstance(payload, dict):
        raise ValueError("Ashby GraphQL response was not an object")

    graphql_errors = payload.get("errors")
    if graphql_errors:
        messages = [
            str(error.get("message") or error) if isinstance(error, dict) else str(error)
            for error in graphql_errors
        ]
        raise RuntimeError(f"Ashby GraphQL error: {'; '.join(messages)}")

    postings = (((payload.get("data") or {}).get("jobBoard") or {}).get("jobPostings", []))
    jobs = []
    for job in postings:
        job_id = job.get("id")
        url = f"https://jobs.ashbyhq.com/{slug}/{job_id}" if job_id else None
        jobs.append({
            "url": url,
            "application_url": url,
            "title": job.get("title"),
            "location": job.get("locationName"),
            "description": job.get("employmentType"),
            "full_description": None,
        })
    return jobs


def _fetch_source_jobs(source: dict) -> list[dict]:
    ats = source["ats"]
    if ats == "greenhouse":
        return _greenhouse_jobs(source)
    if ats == "lever":
        return _lever_jobs(source)
    if ats == "ashby":
        return _ashby_jobs(source)
    raise ValueError(f"unsupported ATS: {ats}")


def _store_jobs(conn: sqlite3.Connection, source: dict, jobs: list[dict]) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0
    site = str(source.get("name") or source["slug"])
    strategy = f"direct_{source['ats']}"

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        description = job.get("description")
        full_description = job.get("full_description")
        detail_scraped_at = now if full_description else None
        application_url = job.get("application_url") or url
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
                "discovered_at, full_description, application_url, detail_scraped_at, "
                "canonical_job_id, apply_domain) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    url,
                    job.get("title"),
                    job.get("salary"),
                    description[:500] if description else None,
                    job.get("location"),
                    site,
                    strategy,
                    now,
                    full_description,
                    application_url,
                    detail_scraped_at,
                    canonical_job_id(url, application_url),
                    domain_from_job_url(application_url or url),
                ),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


def run_direct_ats_discovery(sources: list[dict] | None = None) -> dict:
    """Discover jobs from configured employer ATS pages."""
    search_cfg = config.load_search_config()
    if sources is None:
        sources = load_direct_ats_sources(search_cfg)

    if not sources:
        log.info("No direct ATS sources configured in searches.yaml.")
        return {"found": 0, "new": 0, "existing": 0, "sources": 0, "errors": 0}

    init_db()
    conn = get_connection()
    total_found = 0
    total_new = 0
    total_existing = 0
    errors = 0

    for source in sources:
        label = source.get("name") or source.get("slug")
        try:
            jobs = _fetch_source_jobs(source)
        except Exception as exc:
            errors += 1
            log.error("%s: direct ATS fetch failed: %s", label, exc)
            continue

        filtered = _filter_jobs(jobs, search_cfg)
        new, existing = _store_jobs(conn, source, filtered)
        total_found += len(filtered)
        total_new += new
        total_existing += existing
        log.info("%s: %d matched, %d new, %d existing", label, len(filtered), new, existing)

    return {
        "found": total_found,
        "new": total_new,
        "existing": total_existing,
        "sources": len(sources),
        "errors": errors,
    }

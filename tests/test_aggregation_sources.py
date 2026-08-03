from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

from applypilot.aggregation.models import AggregationRequest, SourceKind
from applypilot.aggregation.sources.cache import CacheSource
from applypilot.aggregation.sources.direct_ats import DirectATSSource
from applypilot.aggregation.sources.manual_import import ManualImportSource
from applypilot.aggregation.sources.smart_extract import SmartExtractSource
from applypilot.aggregation.sources.workday import WorkdaySource

REQUEST = AggregationRequest(query="product internships", query_terms=("product intern",))


async def collect(source):
    return [item async for item in source.search(REQUEST)]


async def collect_with_request(source, request):
    return [item async for item in source.search(request)]


def test_cache_reads_without_writing(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE jobs(url TEXT, title TEXT, location TEXT, site TEXT, description TEXT, "
        "full_description TEXT, application_url TEXT, discovered_at TEXT, salary TEXT, strategy TEXT)"
    )
    connection.execute(
        "INSERT INTO jobs VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "https://jobs.example.com/123",
            "Product Intern",
            "Austin, TX",
            "Example Labs",
            "Short",
            "Full description",
            "https://jobs.example.com/123",
            datetime.now(timezone.utc).isoformat(),
            "$25/hour",
            "direct_greenhouse",
        ),
    )
    connection.commit()
    connection.close()
    jobs = asyncio.run(collect(CacheSource(db_path=path, max_age_days=30)))
    assert len(jobs) == 1
    assert jobs[0].source is SourceKind.CACHE
    assert jobs[0].description == "Full description"


def test_direct_ats_isolates_one_failed_board(monkeypatch):
    sources = [
        {"name": "Good", "ats": "greenhouse", "slug": "good"},
        {"name": "Bad", "ats": "greenhouse", "slug": "bad"},
    ]

    def fetch(source):
        if source["slug"] == "bad":
            raise RuntimeError("blocked")
        return [{"title": "Product Intern", "url": "https://jobs.example.com/123"}]

    monkeypatch.setattr("applypilot.aggregation.sources.direct_ats._fetch_source_jobs", fetch)
    adapter = DirectATSSource(sources=sources, concurrency=2)
    jobs = asyncio.run(collect(adapter))
    assert [job.company for job in jobs] == ["Good"]
    assert adapter.errors == [{"source": "Bad", "error_class": "RuntimeError"}]


def test_manual_import_keeps_portal_as_provenance(tmp_path):
    path = tmp_path / "imports.jsonl"
    path.write_text(
        json.dumps(
            {
                "source": "handshake_manual",
                "source_job_id": "987",
                "title": "Product Intern",
                "company": "Example Labs",
                "location": "Austin, TX",
                "discovery_url": "https://app.joinhandshake.com/stu/jobs/987",
                "official_url": "https://jobs.example.com/123",
                "description": "Copied from the official employer posting",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    jobs = asyncio.run(collect(ManualImportSource(path)))
    assert jobs[0].source is SourceKind.HANDSHAKE_MANUAL
    assert jobs[0].official_url == "https://jobs.example.com/123"


def test_workday_caps_attempts_and_resolves_external_paths():
    calls = []
    employers = {
        f"e{index}": {
            "name": f"Employer {index}",
            "base_url": f"https://e{index}.wd1.myworkdayjobs.com",
            "site_id": "Careers",
        }
        for index in range(20)
    }

    def search_one(key, employer, query):
        calls.append((key, query))
        return [
            {
                "title": "Product Intern",
                "external_path": f"/job/{query.replace(' ', '-')}",
            }
        ]

    request = AggregationRequest(
        query="product internships",
        query_terms=("product intern", "data analyst", "business analyst", "ignored"),
    )
    source = WorkdaySource(
        employers=employers,
        search_one=search_one,
        max_employers=12,
        max_query_terms=3,
        concurrency=6,
    )
    jobs = asyncio.run(collect_with_request(source, request))
    assert len(calls) == 36
    assert len(jobs) == 36
    assert jobs[0].official_url.startswith("https://e")
    assert "/Careers/job/" in jobs[0].official_url


def test_workday_isolates_failed_units():
    employers = {
        "good": {
            "name": "Good Employer",
            "base_url": "https://good.wd1.myworkdayjobs.com",
            "site_id": "Careers",
        },
        "bad": {
            "name": "Bad Employer",
            "base_url": "https://bad.wd1.myworkdayjobs.com",
            "site_id": "Careers",
        },
    }

    def search_one(key, employer, query):
        del employer, query
        if key == "bad":
            raise RuntimeError("blocked details must not be persisted")
        return [{"title": "Product Intern", "external_path": "/job/123"}]

    source = WorkdaySource(employers=employers, search_one=search_one)
    jobs = asyncio.run(collect(source))
    assert [job.company for job in jobs] == ["Good Employer"]
    assert source.errors == ["RuntimeError"]


def test_smart_extract_owns_only_employer_careers(monkeypatch):
    sites = [
        {
            "name": "Careers",
            "url": "https://example.com/careers",
            "source_kind": "employer_careers",
            "direct_source": True,
        },
        {
            "name": "Duplicate ATS",
            "url": "https://jobs.ashbyhq.com/example",
            "source_kind": "direct_ats",
            "direct_source": True,
        },
        {
            "name": "Runway",
            "url": "https://app.joinrunway.io/explore",
            "source_kind": "account_backed_recruiter",
            "direct_source": True,
        },
    ]
    monkeypatch.setattr(
        "applypilot.aggregation.sources.smart_extract._run_one_site",
        lambda name, url: {
            "jobs": [{"title": "Product Intern", "url": "/jobs/123"}],
        },
    )
    jobs = asyncio.run(collect(SmartExtractSource(sites=sites, max_targets=6)))
    assert [job.discovery_url for job in jobs] == ["https://example.com/careers"]
    assert jobs[0].official_url == "https://example.com/jobs/123"


def test_smart_extract_caps_targets_and_isolates_failures(monkeypatch):
    sites = [
        {
            "name": f"Careers {index}",
            "url": f"https://example{index}.com/careers",
            "source_kind": "employer_careers",
            "direct_source": True,
        }
        for index in range(8)
    ]
    calls = []

    def run_one(name, url):
        calls.append(name)
        if name == "Careers 1":
            raise ValueError("fixture failure")
        return {
            "jobs": [
                {
                    "title": "Product Intern",
                    "url": f"{url}/123",
                    "location": "Austin, TX",
                }
            ]
        }

    monkeypatch.setattr("applypilot.aggregation.sources.smart_extract._run_one_site", run_one)
    source = SmartExtractSource(sites=sites, max_targets=6, concurrency=2)
    jobs = asyncio.run(collect(source))
    assert len(calls) == 6
    assert len(jobs) == 5
    assert source.errors == ["ValueError"]

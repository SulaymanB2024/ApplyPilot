import pytest

from applypilot.config import normalize_search_config
from applypilot.database import close_connection, init_db
from applypilot.discovery import direct_ats
from applypilot.discovery.direct_ats import _filter_jobs, _store_jobs, infer_source_from_url, load_direct_ats_sources


def test_direct_ats_sources_infer_platform_and_slug():
    cfg = {
        "direct_ats_sources": [
            {"name": "Example Greenhouse", "url": "https://job-boards.greenhouse.io/example"},
            {"name": "Example Lever", "url": "https://jobs.lever.co/example"},
            {"name": "Example Ashby", "url": "https://jobs.ashbyhq.com/example"},
        ]
    }

    sources = load_direct_ats_sources(cfg)

    assert [(source["ats"], source["slug"]) for source in sources] == [
        ("greenhouse", "example"),
        ("lever", "example"),
        ("ashby", "example"),
    ]
    assert infer_source_from_url({"url": "https://boards.greenhouse.io/acme"})["slug"] == "acme"


def test_direct_ats_filter_uses_title_and_location_boundaries():
    cfg = normalize_search_config({
        "queries": [{"query": "product analyst intern", "tier": 1}],
        "locations": [{"location": "Remote", "remote": True}],
        "location": {
            "accept_patterns": ["Remote", "Austin"],
            "reject_patterns": ["London", "United Kingdom"],
        },
        "exclude_titles": ["senior"],
    })

    jobs = _filter_jobs(
        [
            {"title": "Product Analyst Intern", "location": "Remote"},
            {"title": "Senior Product Analyst", "location": "Remote"},
            {"title": "Product Analyst Intern", "location": "London"},
            {"title": "Product Analyst Intern", "location": "United Kingdom (Remote)"},
            {"title": "Support Specialist", "location": "Remote"},
        ],
        cfg,
    )

    assert jobs == [{"title": "Product Analyst Intern", "location": "Remote"}]


def test_direct_ats_location_filter_does_not_match_us_inside_australia():
    cfg = normalize_search_config({
        "queries": [{"query": "customer success analyst", "tier": 1}],
        "location": {
            "accept_patterns": ["United States", "US", "USA", "Remote"],
            "reject_patterns": [],
        },
        "direct_ats_title_keywords": ["analyst", "success"],
    })

    jobs = _filter_jobs(
        [
            {"title": "Customer Success Analyst", "location": "Sydney, Australia"},
            {"title": "Customer Success Analyst", "location": "Denver, Colorado, USA"},
        ],
        cfg,
    )

    assert jobs == [{"title": "Customer Success Analyst", "location": "Denver, Colorado, USA"}]


def test_direct_ats_store_sets_canonical_and_apply_domain(tmp_path):
    db_path = tmp_path / "applypilot.db"
    conn = init_db(db_path)

    new, existing = _store_jobs(
        conn,
        {"name": "Example Greenhouse", "ats": "greenhouse", "slug": "example"},
        [
            {
                "url": "https://boards.greenhouse.io/example/jobs/123?gh_src=abc",
                "application_url": "https://boards.greenhouse.io/example/jobs/123",
                "title": "Product Analyst Intern",
                "location": "Remote",
                "description": "Internship role",
                "full_description": "Internship role with detailed description",
            }
        ],
    )
    row = conn.execute(
        "SELECT site, strategy, canonical_job_id, apply_domain, full_description FROM jobs"
    ).fetchone()
    close_connection(db_path)

    assert (new, existing) == (1, 0)
    assert row["site"] == "Example Greenhouse"
    assert row["strategy"] == "direct_greenhouse"
    assert row["canonical_job_id"] == "greenhouse:123"
    assert row["apply_domain"] == "boards.greenhouse.io"
    assert row["full_description"] == "Internship role with detailed description"


def test_ashby_query_uses_current_job_posting_schema(monkeypatch):
    captured_payload = {}

    def fake_request(url, *, method="GET", payload=None, timeout=30):
        captured_payload.update(payload or {})
        return {
            "data": {
                "jobBoard": {
                    "jobPostings": [
                        {
                            "id": "posting-123",
                            "title": "Product Analyst Intern",
                            "locationName": "Remote",
                            "employmentType": "Intern",
                        }
                    ]
                }
            }
        }

    monkeypatch.setattr(direct_ats, "_request_json", fake_request)

    jobs = direct_ats._ashby_jobs({"ats": "ashby", "slug": "example"})

    assert "isListed" not in captured_payload["query"]
    assert jobs[0]["url"] == "https://jobs.ashbyhq.com/example/posting-123"


def test_direct_ats_counts_top_level_ashby_graphql_errors(monkeypatch):
    def fake_request(url, *, method="GET", payload=None, timeout=30):
        slug = payload["variables"]["organizationHostedJobsPageName"]
        if slug == "broken":
            return {"errors": [{"message": "upstream resolver failed"}]}
        return {"data": {"jobBoard": {"jobPostings": []}}}

    monkeypatch.setattr(direct_ats, "_request_json", fake_request)
    monkeypatch.setattr(direct_ats.config, "load_search_config", lambda: {})
    monkeypatch.setattr(direct_ats, "init_db", lambda: None)
    monkeypatch.setattr(direct_ats, "get_connection", object)
    monkeypatch.setattr(direct_ats, "_store_jobs", lambda conn, source, jobs: (0, 0))

    result = direct_ats.run_direct_ats_discovery(
        sources=[
            {"name": "Broken Ashby", "ats": "ashby", "slug": "broken"},
            {"name": "Working Ashby", "ats": "ashby", "slug": "working"},
        ]
    )

    with pytest.raises(RuntimeError, match="Ashby GraphQL error: upstream resolver failed"):
        direct_ats._ashby_jobs({"ats": "ashby", "slug": "broken"})
    assert result["sources"] == 2
    assert result["errors"] == 1

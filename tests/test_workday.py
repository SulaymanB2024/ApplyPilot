from applypilot.config import normalize_search_config
from applypilot.database import close_connection, init_db
from applypilot.discovery.workday import _location_ok, _workday_title_ok, store_results


def test_workday_title_filter_respects_keywords_queries_and_exclusions():
    cfg = normalize_search_config({
        "queries": [{"query": "product analyst intern", "tier": 1}],
        "direct_ats_title_keywords": ["intern", "analyst", "product"],
        "exclude_titles": ["senior"],
    })

    assert _workday_title_ok("Product Analyst Intern", cfg) is True
    assert _workday_title_ok("Senior Product Analyst", cfg) is False
    assert _workday_title_ok("Warehouse Associate", cfg) is False


def test_workday_location_filter_rejects_disallowed_remote_regions():
    assert _location_ok(
        "United Kingdom (Remote)",
        accept=["Remote", "United States"],
        reject=["United Kingdom", "London"],
    ) is False
    assert _location_ok(
        "United States (Remote)",
        accept=["Remote", "United States"],
        reject=["United Kingdom", "London"],
    ) is True


def test_workday_store_sets_canonical_and_apply_domain(tmp_path):
    db_path = tmp_path / "applypilot.db"
    conn = init_db(db_path)

    new, existing = store_results(
        conn,
        [
            {
                "apply_url": "https://example.wd1.myworkdayjobs.com/en-US/jobs/job/Remote/Product-Analyst-Intern_REQ-123",
                "title": "Product Analyst Intern",
                "location": "Remote",
                "full_description": "Detailed internship role description " * 10,
                "employer_name": "Example Workday",
            }
        ],
        employers={},
    )
    row = conn.execute(
        "SELECT strategy, canonical_job_id, apply_domain, full_description FROM jobs"
    ).fetchone()
    close_connection(db_path)

    assert (new, existing) == (1, 0)
    assert row["strategy"] == "workday_api"
    assert row["canonical_job_id"] == "workday:example.wd1.myworkdayjobs.com:req-123"
    assert row["apply_domain"] == "example.wd1.myworkdayjobs.com"
    assert row["full_description"].startswith("Detailed internship")

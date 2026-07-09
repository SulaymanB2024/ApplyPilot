from applypilot.config import normalize_search_config, uses_direct_source_mode
from applypilot.discovery.smartextract import build_scrape_targets
from applypilot.pipeline import discovery_plan


def test_direct_source_mode_skips_jobspy_but_keeps_direct_backends():
    cfg = normalize_search_config({
        "discovery_mode": "direct",
        "boards": ["indeed", "linkedin"],
        "queries": [{"query": "data analyst intern", "tier": 1}],
        "direct_sources": {"workday": True, "direct_ats": True, "smartextract": True},
    })

    plan = discovery_plan(cfg)

    assert uses_direct_source_mode(cfg)
    assert plan["mode"] == "direct_sources"
    assert plan["jobspy"] is False
    assert plan["workday"] is True
    assert plan["direct_ats"] is True
    assert plan["smartextract"] is True


def test_direct_source_mode_filters_smart_extract_to_direct_sites():
    cfg = normalize_search_config({
        "discovery_mode": "direct_sources",
        "queries": [{"query": "product analyst intern", "tier": 1}],
        "locations": [{"location": "Remote", "remote": True}],
    })
    sites = [
        {
            "name": "Employer Greenhouse",
            "url": "https://job-boards.greenhouse.io/example",
            "type": "static",
            "direct_source": True,
        },
        {
            "name": "Generic Remote Board",
            "url": "https://remote.example/jobs?q={query_encoded}",
            "type": "search",
        },
    ]

    targets = build_scrape_targets(sites=sites, search_cfg=cfg)

    assert targets == [
        {
            "name": "Employer Greenhouse",
            "url": "https://job-boards.greenhouse.io/example",
            "query": None,
        }
    ]


def test_hybrid_mode_keeps_searchable_smart_extract_sources():
    cfg = normalize_search_config({
        "discovery_mode": "hybrid",
        "queries": [{"query": "product analyst intern", "tier": 1}],
        "locations": [{"location": "Remote", "remote": True}],
    })
    sites = [
        {
            "name": "Generic Remote Board",
            "url": "https://remote.example/jobs?q={query_encoded}",
            "type": "search",
        },
    ]

    targets = build_scrape_targets(sites=sites, search_cfg=cfg)

    assert targets == [
        {
            "name": "Generic Remote Board",
            "url": "https://remote.example/jobs?q=product+analyst+intern",
            "query": "product analyst intern",
        }
    ]

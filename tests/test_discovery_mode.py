import pytest

from applypilot import pipeline
from applypilot.config import normalize_search_config, uses_direct_source_mode
from applypilot.discovery import direct_ats, workday
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


def test_discovery_contract_reports_partial_provider_failure(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "discovery_plan",
        lambda: {
            "mode": "direct_sources",
            "jobspy": False,
            "workday": True,
            "direct_ats": True,
            "smartextract": False,
        },
    )
    monkeypatch.setattr(workday, "run_workday_discovery", lambda workers=1: {"found": 3})
    monkeypatch.setattr(
        direct_ats,
        "run_direct_ats_discovery",
        lambda: {"found": 2, "sources": 2, "errors": 1},
    )

    result = pipeline._run_discover()

    assert result["status"] == "partial"
    assert result["provider_errors"] == {"direct_ats": "1 of 2 sources failed"}
    assert result["workday"] == "ok"
    assert result["direct_ats"] == "partial"


def test_discovery_contract_reports_all_enabled_providers_failed(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "discovery_plan",
        lambda: {
            "mode": "direct_sources",
            "jobspy": False,
            "workday": True,
            "direct_ats": True,
            "smartextract": False,
        },
    )

    def fail_workday(workers=1):
        raise RuntimeError("workday unavailable")

    monkeypatch.setattr(workday, "run_workday_discovery", fail_workday)
    monkeypatch.setattr(
        direct_ats,
        "run_direct_ats_discovery",
        lambda: {"found": 0, "sources": 2, "errors": 2},
    )

    result = pipeline._run_discover()

    assert result["status"] == "error"
    assert result["provider_errors"] == {
        "workday": "workday unavailable",
        "direct_ats": "2 of 2 sources failed",
    }
    assert result["workday"] == "error"
    assert result["direct_ats"] == "error"


def test_discovery_contract_surfaces_internal_and_zero_source_failures(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "discovery_plan",
        lambda: {
            "mode": "direct_sources",
            "jobspy": False,
            "workday": True,
            "direct_ats": True,
            "smartextract": True,
        },
    )
    monkeypatch.setattr(
        workday,
        "run_workday_discovery",
        lambda workers=1: {"found": 0, "errors": 2, "attempts": 2},
    )
    monkeypatch.setattr(
        direct_ats,
        "run_direct_ats_discovery",
        lambda: {"found": 0, "sources": 0, "errors": 0},
    )
    monkeypatch.setattr(
        "applypilot.discovery.smartextract.run_smart_extract",
        lambda workers=1: {"passed": 0, "total": 2},
    )

    result = pipeline._run_discover()

    assert result["status"] == "error"
    assert result["workday"] == "error"
    assert result["direct_ats"] == "error"
    assert result["smartextract"] == "error"
    assert result["provider_errors"] == {
        "workday": "2 of 2 Workday employer queries failed",
        "direct_ats": "no direct ATS sources were configured",
        "smartextract": "2 of 2 SmartExtract targets failed",
    }


@pytest.mark.parametrize("orchestrator", [pipeline._run_sequential, pipeline._run_streaming])
@pytest.mark.parametrize("status", ["partial", "error"])
def test_pipeline_preserves_discovery_provider_errors(monkeypatch, orchestrator, status):
    discovery_result = {
        "status": status,
        "provider_errors": {"direct_ats": "Ashby GraphQL error"},
        "jobspy": "skipped",
        "workday": "skipped",
        "direct_ats": status,
        "smartextract": "skipped",
    }
    monkeypatch.setitem(
        pipeline._STAGE_RUNNERS,
        "discover",
        lambda workers=1: discovery_result,
    )

    result = orchestrator(["discover"], min_score=7)

    assert result["stages"][0]["status"] == status
    assert result["errors"]["discover"] == {
        "status": status,
        "provider_errors": {"direct_ats": "Ashby GraphQL error"},
    }

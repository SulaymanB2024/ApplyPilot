from __future__ import annotations

import json

import pytest

from applypilot.aggregation.portal_handoff import (
    Portal,
    PortalContractError,
    PortalMissionRequest,
    validate_portal_response,
    write_portal_checkpoint,
    write_portal_mission_request,
)
from applypilot.autonomy.handoff import (
    BrowserArtifactPending,
    RunBindings,
    active_handoffs,
    import_response_artifact,
)


def _request(portal=Portal.HANDSHAKE):
    return PortalMissionRequest(
        run_id="agg-1",
        portal=portal,
        start_url=(
            "https://app.joinhandshake.com/stu/postings"
            if portal is Portal.HANDSHAKE
            else "https://app.joinrunway.io/explore"
        ),
        query_terms=("product intern", "data analyst intern"),
        locations=("Austin, TX", "Remote US"),
        max_results=25,
        max_navigations=30,
        max_seconds=180,
    )


def _response(request, **overrides):
    payload = {
        "schema_version": "applypilot.portal-mission-response.v1",
        "run_id": request.run_id,
        "request_id": request.request_id,
        "request_sha256": request.sha256,
        "query_digest": request.query_digest,
        "portal": request.portal.value,
        "status": "complete",
        "navigation_count": 3,
        "elapsed_seconds": 12,
        "safe_hostname": request.permitted_hosts[0],
        "observations": [
            {
                "source_job_id": "987",
                "title": "Product Analytics Intern",
                "company": "Example Labs",
                "location": "Austin, TX",
                "discovery_url": (
                    "https://app.joinhandshake.com/stu/jobs/987"
                    if request.portal is Portal.HANDSHAKE
                    else "https://app.joinrunway.io/jobs/987"
                ),
                "application_url": (
                    "https://app.joinhandshake.com/stu/jobs/987"
                    if request.portal is Portal.HANDSHAKE
                    else "https://app.joinrunway.io/jobs/987"
                ),
                "official_url": "",
                "description": "Visible portal description",
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_portal_mission_is_bounded_and_contains_no_auth_material():
    request = _request()
    payload = request.to_dict()
    assert payload["permitted_hosts"] == [
        "app.joinhandshake.com",
        "utaustin.joinhandshake.com",
    ]
    assert not ({"cookie", "token", "password", "otp", "profile_path"} & set(payload))
    assert payload["resource_lock"] == "authenticated_browser"


def test_portal_response_accepts_portal_native_application():
    request = _request()
    response = validate_portal_response(_response(request), request=request)
    assert response.observations[0].verification_state == "portal_only"
    assert response.observations[0].advanceable is False


def test_portal_response_resolves_displayed_first_party_application():
    request = _request(Portal.RUNWAY)
    payload = _response(request)
    payload["observations"][0]["application_url"] = "https://jobs.example.com/123"
    response = validate_portal_response(payload, request=request)
    assert response.observations[0].official_url == "https://jobs.example.com/123"
    assert response.observations[0].advanceable is True


def test_portal_response_rejects_wrong_domain_or_excess_results():
    request = _request()
    wrong_host = _response(request)
    wrong_host["observations"][0]["discovery_url"] = "https://evil.example/jobs/987"
    with pytest.raises(PortalContractError, match="discovery host"):
        validate_portal_response(wrong_host, request=request)
    excess = _response(request, observations=_response(request)["observations"] * 26)
    with pytest.raises(PortalContractError, match="result budget"):
        validate_portal_response(excess, request=request)


@pytest.mark.parametrize(
    "status",
    ["complete", "partial", "auth_required", "blocked", "budget_exhausted"],
)
def test_portal_response_accepts_bounded_terminal_statuses(status):
    request = _request()
    response = validate_portal_response(_response(request, status=status), request=request)
    assert response.status == status


def test_portal_handoffs_share_one_authenticated_browser_lock(tmp_path):
    bindings = RunBindings(
        run_id="agg-1",
        fact_digest="f" * 64,
        context_digest="c" * 64,
        policy_digest="p" * 64,
    )
    first = write_portal_mission_request(
        run_dir=tmp_path,
        bindings=bindings,
        request=_request(Portal.HANDSHAKE),
    )
    [active] = active_handoffs(run_dir=tmp_path, bindings=bindings)
    assert active.request_path == first
    assert active.resource_lock == "authenticated_browser"
    with pytest.raises(BrowserArtifactPending):
        write_portal_mission_request(
            run_dir=tmp_path,
            bindings=bindings,
            request=_request(Portal.RUNWAY),
        )


def test_existing_import_path_validates_portal_response(tmp_path):
    bindings = RunBindings(
        run_id="agg-1",
        fact_digest="f" * 64,
        context_digest="c" * 64,
        policy_digest="p" * 64,
    )
    request = _request()
    request_path = write_portal_mission_request(
        run_dir=tmp_path,
        bindings=bindings,
        request=request,
    )
    input_path = tmp_path / "portal-response.json"
    input_path.write_text(json.dumps(_response(request)), encoding="utf-8")
    imported = import_response_artifact(request_path=request_path, input_path=input_path)
    payload = json.loads((tmp_path / "handoff" / "portal_discovery.handshake.response.json").read_text())
    assert imported["request_id"] == request.request_id
    assert payload["status"] == "complete"


def test_portal_checkpoint_is_safe_bounded_and_monotonic(tmp_path):
    bindings = RunBindings(
        run_id="agg-1",
        fact_digest="f" * 64,
        context_digest="c" * 64,
        policy_digest="p" * 64,
    )
    request_path = write_portal_mission_request(
        run_dir=tmp_path,
        bindings=bindings,
        request=_request(),
    )
    checkpoint = write_portal_checkpoint(
        request_path=request_path,
        state="page_observed",
        sequence=1,
        navigation_count=2,
        result_count=3,
        elapsed_seconds=5,
        safe_hostname="app.joinhandshake.com",
    )
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert payload["state"] == "page_observed"
    assert not ({"selector", "cookie", "description", "prompt"} & set(payload))
    with pytest.raises(PortalContractError, match="sequence must increase"):
        write_portal_checkpoint(
            request_path=request_path,
            state="checkpoint",
            sequence=1,
            navigation_count=2,
            result_count=3,
            elapsed_seconds=6,
            safe_hostname="app.joinhandshake.com",
        )

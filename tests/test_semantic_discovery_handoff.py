from __future__ import annotations

import json

import pytest

from applypilot.autonomy.chatgpt_web import (
    ChatGPTContractError,
    parse_chatgpt_json,
    parse_chatgpt_response,
    role_candidates_from_payload,
)
from applypilot.autonomy.context import build_context_pack
from applypilot.autonomy.handoff import (
    ArtifactChatGPTClient,
    ChatGPTArtifactPending,
    RunBindings,
    import_response_artifact,
)
from applypilot.autonomy.policy import FunnelBudget
from applypilot.autonomy.telemetry import UsageLedger
from applypilot.observability.events import EventJournal


PROFILE = {
    "experience": {
        "education_level": "BBA candidate, expected May 2028",
        "target_role": "AI product, analytics, and technical business intern",
        "current_title": "AI product and analytics intern",
    },
    "personal": {
        "city": "Austin",
        "province_state": "TX",
        "country": "USA",
    },
    "skills_boundary": {
        "analytics": ["Python", "SQL", "Tableau"],
        "product": ["product research", "workflow analysis"],
    },
    "availability": {
        "earliest_start_date": "May 4, 2027",
        "preferred_locations": ["Austin, TX", "Remote US", "New York, NY"],
    },
    "work_authorization": {
        "legally_authorized_to_work": True,
        "require_sponsorship": False,
    },
}


def _lifecycle_client(tmp_path):
    run_dir = tmp_path / "run"
    pack = build_context_pack(PROFILE, job_text="AI product analytics internship")
    bindings = RunBindings(
        run_id="handoff-lifecycle",
        fact_digest="facts",
        context_digest=pack.digest,
        policy_digest="policy",
    )
    journal = EventJournal(run_dir / "events.ndjson", run_id=bindings.run_id)
    client = ArtifactChatGPTClient(
        run_dir=run_dir,
        bindings=bindings,
        ledger=UsageLedger(
            run_id=bindings.run_id,
            budget=FunnelBudget(),
            journal=journal,
        ),
    )
    return client, pack, journal


def test_handoff_event_lifecycle_is_ordered_and_content_free(tmp_path):
    client, pack, journal = _lifecycle_client(tmp_path)
    query = "paid AI product internships"
    with pytest.raises(ChatGPTArtifactPending) as pending:
        client.find_roles(pack=pack, query=query, limit=1)
    request = json.loads(pending.value.request_path.read_text(encoding="utf-8"))
    response_input = tmp_path / "accepted.json"
    response_input.write_text(
        json.dumps(
            {
                "schema_version": "applypilot.chatgpt_web.v1",
                "kind": "role_candidates",
                "request_id": request["request_id"],
                "items": [
                    {
                        "company": "Example Systems",
                        "title": "AI Product Intern",
                        "official_url": "https://careers.examplesystems.com/jobs/ai-intern",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    import_response_artifact(
        request_path=pending.value.request_path,
        input_path=response_input,
    )
    assert len(client.find_roles(pack=pack, query=query, limit=1)) == 1

    events = journal.read()
    assert [(event.phase, event.status) for event in events] == [
        ("handoff_request", "created"),
        ("handoff_wait", "started"),
        ("handoff_response", "imported"),
        ("handoff_validation", "started"),
        ("handoff_validation", "complete"),
        ("handoff_wait", "complete"),
    ]
    assert events[-1].detail["output_chars"] > 0
    assert not any(
        fragment in key.lower()
        for event in events
        for key in event.detail
        for fragment in ("prompt", "response")
    )


def test_handoff_event_rejected_import_records_only_safe_error_metadata(tmp_path):
    client, pack, journal = _lifecycle_client(tmp_path)
    with pytest.raises(ChatGPTArtifactPending) as pending:
        client.find_roles(pack=pack, query="paid AI product internships", limit=1)
    rejected = tmp_path / "rejected.json"
    rejected.write_text(
        json.dumps(
            {
                "schema_version": "applypilot.chatgpt_web.v1",
                "kind": "role_candidates",
                "request_id": "0" * 64,
                "items": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ChatGPTContractError, match="request_id mismatch"):
        import_response_artifact(
            request_path=pending.value.request_path,
            input_path=rejected,
        )

    error = journal.read()[-1]
    assert (error.phase, error.status) == ("handoff_validation", "error")
    assert set(error.detail) == {"error_class", "output_chars"}
    assert error.detail["error_class"] == "ChatGPTContractError"
    assert error.detail["output_chars"] > 0


def test_natural_discovery_response_is_normalized_semantically():
    request_id = "a" * 64
    response = """I found two strong possibilities.

1. **Product Analytics Intern — Acme Labs**
   - Location: Austin, TX
   - Official posting: https://jobs.acmelabs.com/roles/123
   - Why it fits: The role combines product research, SQL analysis, and cross-functional work.
   - Experience: 0–2 years
   - Posted: July 29, 2026

2. **Northstar — Strategy & Operations Intern**
   - Location: New York, NY
   - Official posting: [View the official posting](https://job-boards.greenhouse.io/northstar/jobs/456)
   - Evidence: The posting describes market analysis and product strategy for a Summer 2027 intern.

Reference: REFERENCE_ID
""".replace("REFERENCE_ID", request_id)

    payload = parse_chatgpt_response(
        response,
        expected_kind="role_candidates",
        request_id=request_id,
    )
    candidates = role_candidates_from_payload(payload, limit=10)

    assert payload["request_id"] == request_id
    assert [(candidate.title, candidate.company) for candidate in candidates] == [
        ("Product Analytics Intern", "Acme Labs"),
        ("Strategy & Operations Intern", "Northstar"),
    ]
    assert candidates[0].location == "Austin, TX"
    assert candidates[0].required_experience_min == 0
    assert candidates[0].required_experience_max == 2
    assert candidates[0].posted_date.isoformat() == "2026-07-29"


def test_natural_discovery_response_accepts_labeled_plain_language():
    response = """Here is the most relevant live role I could verify.

### Opportunity 1
Company: Example Systems
Role: AI Product Intern
Where: Remote within the United States
Official URL: https://careers.examplesystems.com/jobs/ai-product-intern
Summary: This internship connects AI workflow research with product operations.
"""

    payload = parse_chatgpt_response(response, expected_kind="role_candidates")
    [candidate] = role_candidates_from_payload(payload, limit=5)

    assert candidate.company == "Example Systems"
    assert candidate.title == "AI Product Intern"
    assert candidate.location == "Remote within the United States"
    assert "AI workflow research" in candidate.description


def test_discovery_normalizer_rejects_empty_placeholder_but_keeps_legacy_parser():
    response = json.dumps(
        {
            "schema_version": "applypilot.chatgpt_web.v1",
            "kind": "role_candidates",
            "items": [],
        }
    )

    assert parse_chatgpt_json(response, expected_kind="role_candidates")["items"] == []
    with pytest.raises(ChatGPTContractError, match="no role candidates"):
        parse_chatgpt_response(response, expected_kind="role_candidates")


def test_natural_discovery_response_requires_matching_reference_when_bound():
    response = """1. AI Product Intern — Example Systems
Location: Remote US
Official posting: https://careers.examplesystems.com/jobs/ai-product-intern
Why it fits: The work combines AI product research and analytics.
"""

    with pytest.raises(ChatGPTContractError, match="missing its reference"):
        parse_chatgpt_response(
            response,
            expected_kind="role_candidates",
            request_id="a" * 64,
        )

    with pytest.raises(ChatGPTContractError, match="request_id mismatch"):
        parse_chatgpt_response(
            response + f"\nReference: {'b' * 64}\n",
            expected_kind="role_candidates",
            request_id="a" * 64,
        )
    legacy_without_binding = json.dumps(
        {
            "schema_version": "applypilot.chatgpt_web.v1",
            "kind": "role_candidates",
            "items": [
                {
                    "company": "Example Systems",
                    "title": "AI Product Intern",
                    "official_url": "https://careers.examplesystems.com/jobs/ai-product-intern",
                }
            ],
        }
    )
    with pytest.raises(ChatGPTContractError, match="request_id mismatch"):
        parse_chatgpt_response(
            legacy_without_binding,
            expected_kind="role_candidates",
            request_id="a" * 64,
        )


def test_natural_discovery_keeps_disallowed_host_gate():
    response = """1. Product Analytics Intern — Example
Location: Austin, TX
Official posting: https://www.linkedin.com/jobs/view/123
Why it fits: The listing mentions product analytics.
"""
    payload = parse_chatgpt_response(response, expected_kind="role_candidates")

    with pytest.raises(ChatGPTContractError, match="disallowed discovery host"):
        role_candidates_from_payload(payload, limit=5)


def test_natural_discovery_uses_labeled_official_url_over_discovery_citation():
    response = """1. Product Analytics Intern — Example Systems
Discovery lead: https://www.linkedin.com/jobs/view/123
Official posting: https://careers.examplesystems.com/jobs/product-analytics-intern
Location: Austin, TX
Why it fits: The role combines product research and analytics.
"""

    payload = parse_chatgpt_response(response, expected_kind="role_candidates")
    [candidate] = role_candidates_from_payload(payload, limit=5)

    assert candidate.official_url == (
        "https://careers.examplesystems.com/jobs/product-analytics-intern"
    )


def test_artifact_handoff_binds_natural_response_to_internal_json(tmp_path):
    pack = build_context_pack(PROFILE, job_text="AI product analytics internship")
    bindings = RunBindings(
        run_id="semantic-handoff",
        fact_digest="facts",
        context_digest=pack.digest,
        policy_digest="policy",
    )
    client = ArtifactChatGPTClient(
        run_dir=tmp_path / "run",
        bindings=bindings,
        ledger=UsageLedger(
            run_id=bindings.run_id,
            budget=FunnelBudget(),
        ),
    )

    with pytest.raises(ChatGPTArtifactPending) as pending:
        client.find_roles(
            pack=pack,
            query="paid Summer 2027 AI product internships in Austin or Remote US",
            limit=5,
        )
    request = json.loads(pending.value.request_path.read_text(encoding="utf-8"))
    assert request["prompt_schema_version"] == "applypilot.chatgpt-prompt.v7"
    assert request["response_format"] == "natural_language_role_list"
    assert not request["prompt"].lstrip().startswith("{")

    natural_response = tmp_path / "chatgpt-response.md"
    natural_response.write_text(
        f"""1. AI Product Intern — Example Systems
Location: Remote US
Official posting: https://careers.examplesystems.com/jobs/ai-product-intern
Why it fits: The work combines AI product research, analytics, and workflow analysis.
Reference: {request["request_id"]}
""",
        encoding="utf-8",
    )
    imported = import_response_artifact(
        request_path=pending.value.request_path,
        input_path=natural_response,
    )
    canonical = json.loads(
        pending.value.response_path.read_text(encoding="utf-8")
    )

    assert imported["request_id"] == request["request_id"]
    assert canonical["request_id"] == request["request_id"]
    assert canonical["items"][0]["company"] == "Example Systems"
    [candidate] = client.find_roles(
        pack=pack,
        query="paid Summer 2027 AI product internships in Austin or Remote US",
        limit=5,
    )
    assert candidate.title == "AI Product Intern"
    receipt_path = pending.value.response_path.with_name(
        pending.value.response_path.name.replace(".response.json", ".receipt.json")
    )
    assert receipt_path.exists()

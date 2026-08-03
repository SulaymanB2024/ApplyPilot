from __future__ import annotations

from datetime import datetime, timezone

import pytest

from applypilot.aggregation.models import RawJob, SourceKind, VerificationState
from applypilot.aggregation.normalization import merge_observations, normalize_job

NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)


def raw(source, url, description=""):
    return RawJob(
        source=source,
        source_job_id=f"{source.value}-123",
        title="Product Analytics Intern",
        company="Example Labs",
        location="Austin, TX",
        official_url=url,
        discovery_url="https://app.joinhandshake.com/stu/jobs/987",
        description=description,
        observed_at=NOW,
    )


def test_tracking_variants_share_one_key():
    first = normalize_job(raw(SourceKind.DIRECT_ATS, "https://jobs.example.com/123?gh_src=abc"))
    second = normalize_job(raw(SourceKind.CACHE, "https://jobs.example.com/123?utm_source=board"))
    assert first.canonical_key == second.canonical_key
    assert first.official_url == "https://jobs.example.com/123"


def test_merge_preserves_provenance_and_best_description():
    cached = normalize_job(raw(SourceKind.CACHE, "https://jobs.example.com/123", "Short"))
    fresh = normalize_job(
        raw(SourceKind.DIRECT_ATS, "https://jobs.example.com/123", "Full first-party description")
    )
    merged = merge_observations([cached, fresh])
    assert merged.description == "Full first-party description"
    assert [item.source for item in merged.observations] == [SourceKind.CACHE, SourceKind.DIRECT_ATS]
    assert merged.source_count == 2


@pytest.mark.parametrize(
    "url",
    ["https://app.joinhandshake.com/stu/jobs/987", "https://app.joinrunway.io/jobs/987"],
)
def test_restricted_portal_cannot_be_official_url(url):
    with pytest.raises(ValueError, match="official_url must resolve"):
        normalize_job(raw(SourceKind.HANDSHAKE_BROWSER, url))


def test_portal_permalink_is_unverified_until_first_party_resolution():
    observed = normalize_job(
        RawJob(
            source=SourceKind.HANDSHAKE_BROWSER,
            source_job_id="handshake-987",
            title="Product Analytics Intern",
            company="Example Labs",
            location="Austin, TX",
            official_url="",
            application_url="https://app.joinhandshake.com/stu/jobs/987",
            discovery_url="https://app.joinhandshake.com/stu/jobs/987",
            description="Visible portal description",
            observed_at=NOW,
        )
    )
    assert observed.verification_state is VerificationState.PORTAL_ONLY
    assert observed.advanceable is False


def test_jobspy_board_permalink_is_unverified():
    observed = normalize_job(
        RawJob(
            source=SourceKind.JOBSPY,
            source_job_id="indeed-987",
            title="Product Analytics Intern",
            company="Example Labs",
            location="Austin, TX",
            official_url="",
            discovery_url="https://www.indeed.com/viewjob?jk=987",
            description="Board description",
            observed_at=NOW,
        )
    )
    assert observed.verification_state is VerificationState.BOARD_ONLY
    assert observed.advanceable is False

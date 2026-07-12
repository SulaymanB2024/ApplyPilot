from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone

import pytest

from applypilot.autonomy.supervisor import (
    RUNTIME_OBSERVATION_NAME,
    record_runtime_observation,
    runtime_gated_decision,
    runtime_observation_snapshot,
)


NOW = datetime(2027, 1, 5, 16, 30, tzinfo=timezone.utc)


def test_runtime_observation_is_bounded_private_and_expires_with_frame(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    status = record_runtime_observation(
        root=root,
        scope_kind="run",
        scope_id="20270105T163000000000Z-0123456789",
        chronicle_state="capturing",
        chronicle_evidence_code="fresh_frame_observed",
        latest_frame_at=NOW - timedelta(seconds=2),
        browser_surface="codex_chrome_connector",
        browser_readiness="ready",
        ttl_seconds=60,
        now=NOW,
    )

    assert status["runtime_ready"] is True
    assert status["observation_state"] == "fresh"
    assert status["latest_frame_age_seconds"] == 2
    path = root / RUNTIME_OBSERVATION_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    serialized = path.read_text(encoding="utf-8")
    assert len(serialized) < 1_000
    for forbidden in (
        "https://chatgpt.com",
        "Default Chrome Profile",
        "private@example.com",
        "screenshot pixels",
    ):
        assert forbidden not in serialized
    assert runtime_gated_decision(
        next_action_owner="browser_connector",
        next_action_code="provide_chatgpt_web_role_candidates",
        browser_required=True,
        runtime_status=status,
    ) == (
        "browser_connector",
        "provide_chatgpt_web_role_candidates",
        True,
    )
    with pytest.raises(ValueError, match="explicitly observed fresh"):
        record_runtime_observation(
            root=root,
            scope_kind="run",
            scope_id="20270105T163000000000Z-0123456789",
            chronicle_state="capturing",
            chronicle_evidence_code="fresh_frame_observed",
            latest_frame_at=NOW - timedelta(seconds=30, microseconds=1),
            browser_surface="codex_chrome_connector",
            browser_readiness="ready",
            now=NOW,
        )

    stale = runtime_observation_snapshot(
        root=root,
        scope_kind="run",
        scope_id="20270105T163000000000Z-0123456789",
        now=NOW + timedelta(seconds=29),
    )
    assert stale["observation_state"] == "stale"
    assert stale["chronicle_state"] == "stale"
    assert stale["runtime_ready"] is False


def test_runtime_states_require_explicit_evidence_and_gate_browser_work(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    scope_id = "20270105T163000000000Z-0123456789"

    with pytest.raises(ValueError, match="state and evidence code disagree"):
        record_runtime_observation(
            root=root,
            scope_kind="run",
            scope_id=scope_id,
            chronicle_state="idle_paused",
            chronicle_evidence_code="frame_stale",
            browser_surface="codex_chrome_connector",
            browser_readiness="ready",
            now=NOW,
        )

    paused = record_runtime_observation(
        root=root,
        scope_kind="run",
        scope_id=scope_id,
        chronicle_state="idle_paused",
        chronicle_evidence_code="system_idle_reported",
        browser_surface="codex_chrome_connector",
        browser_readiness="ready",
        now=NOW,
    )
    assert paused["observation_state"] == "fresh"
    assert paused["chronicle_state"] == "idle_paused"
    assert paused["runtime_ready"] is False
    assert runtime_gated_decision(
        next_action_owner="browser_connector",
        next_action_code="provide_chatgpt_web_role_candidates",
        browser_required=True,
        runtime_status=paused,
    ) == ("controller", "restore_chronicle_capture", False)

    wrong_browser = record_runtime_observation(
        root=root,
        scope_kind="run",
        scope_id=scope_id,
        chronicle_state="capturing",
        chronicle_evidence_code="fresh_frame_observed",
        latest_frame_at=NOW,
        browser_surface="wrong_surface",
        browser_readiness="ready",
        now=NOW,
    )
    assert runtime_gated_decision(
        next_action_owner="browser_connector",
        next_action_code="provide_chatgpt_web_role_candidates",
        browser_required=True,
        runtime_status=wrong_browser,
    ) == ("system_admin", "activate_codex_chrome_connector", False)
    assert runtime_gated_decision(
        next_action_owner="applicant",
        next_action_code="confirm_preferred_location",
        browser_required=False,
        runtime_status=wrong_browser,
    ) == ("applicant", "confirm_preferred_location", False)

    record_runtime_observation(
        root=root,
        scope_kind="run",
        scope_id=scope_id,
        chronicle_state="unknown",
        chronicle_evidence_code="not_observed",
        browser_surface="wrong_surface",
        browser_readiness="ready",
        ttl_seconds=30,
        now=NOW,
    )
    expired_wrong_browser = runtime_observation_snapshot(
        root=root,
        scope_kind="run",
        scope_id=scope_id,
        now=NOW + timedelta(seconds=30, microseconds=1),
    )
    assert expired_wrong_browser["observation_state"] == "stale"
    assert runtime_gated_decision(
        next_action_owner="browser_connector",
        next_action_code="provide_chatgpt_web_role_candidates",
        browser_required=True,
        runtime_status=expired_wrong_browser,
    ) == ("controller", "refresh_runtime_observation", False)


def test_runtime_observation_fails_closed_on_scope_corruption_and_symlinks(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    scope_id = "20270105T163000000000Z-0123456789"
    record_runtime_observation(
        root=root,
        scope_kind="run",
        scope_id=scope_id,
        chronicle_state="unknown",
        chronicle_evidence_code="not_observed",
        browser_surface="unknown",
        browser_readiness="unknown",
        now=NOW,
    )
    path = root / RUNTIME_OBSERVATION_NAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["scope_id"] = "different-run"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="scope binding"):
        runtime_observation_snapshot(
            root=root,
            scope_kind="run",
            scope_id=scope_id,
            now=NOW,
        )

    payload["scope_id"] = scope_id
    payload["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="timestamp is in the future"):
        runtime_observation_snapshot(
            root=root,
            scope_kind="run",
            scope_id=scope_id,
            now=NOW,
        )

    payload["observed_at"] = NOW.isoformat()
    payload["private_url"] = "https://example.invalid/private"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="exact schema"):
        runtime_observation_snapshot(
            root=root,
            scope_kind="run",
            scope_id=scope_id,
            now=NOW,
        )

    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="non-symlink"):
        runtime_observation_snapshot(
            root=root,
            scope_kind="run",
            scope_id=scope_id,
            now=NOW,
        )


def test_missing_runtime_observation_is_explicitly_not_ready(tmp_path):
    root = tmp_path / "campaign"
    root.mkdir()
    status = runtime_observation_snapshot(
        root=root,
        scope_kind="campaign",
        scope_id="campaign-1",
        now=NOW,
    )
    assert status["observation_state"] == "missing"
    assert status["runtime_ready"] is False
    assert status["browser_surface"] == "unknown"
    assert runtime_gated_decision(
        next_action_owner="browser_connector",
        next_action_code="discover_roles",
        browser_required=True,
        runtime_status=status,
    ) == ("controller", "refresh_runtime_observation", False)

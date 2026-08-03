from __future__ import annotations

import json
import stat
import time

import pytest

from applypilot.observability.events import EventJournal


def test_journal_persists_ordered_private_events(tmp_path):
    journal = EventJournal(tmp_path / "events.ndjson", run_id="agg-1")
    first = journal.emit(component="aggregation", phase="run", status="started")
    second = journal.emit(
        component="aggregation",
        phase="candidate",
        status="observed",
        source="cache",
        counts={"observed": 1},
        detail={"revision": 1},
    )

    assert (first.sequence, second.sequence) == (1, 2)
    assert journal.read() == [first, second]
    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600


@pytest.mark.parametrize("key", ["token", "request_body", "email_address", "browser_cookie"])
def test_journal_rejects_sensitive_detail_keys(tmp_path, key):
    journal = EventJournal(tmp_path / "events.ndjson", run_id="agg-1")
    with pytest.raises(ValueError, match="sensitive"):
        journal.emit(component="aggregation", phase="run", status="error", detail={key: "redacted"})


def test_journal_detects_tampering(tmp_path):
    journal = EventJournal(tmp_path / "events.ndjson", run_id="agg-1")
    journal.emit(component="aggregation", phase="run", status="started")
    payload = json.loads(journal.path.read_text(encoding="utf-8"))
    payload["sequence"] = 2
    journal.path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sequence"):
        journal.read()


def test_two_journal_instances_share_one_sequence(tmp_path):
    path = tmp_path / "events.ndjson"
    first = EventJournal(path, run_id="agg-1")
    second = EventJournal(path, run_id="agg-1")
    first.emit(component="aggregation", phase="run", status="started")
    second.emit(component="aggregation", phase="run", status="heartbeat")
    assert [event.sequence for event in first.read()] == [1, 2]


def test_reopened_journal_preserves_run_elapsed_time(tmp_path):
    path = tmp_path / "events.ndjson"
    first = EventJournal(path, run_id="agg-1")
    first.emit(component="aggregation", phase="run", status="started")
    time.sleep(0.02)

    resumed = EventJournal(path, run_id="agg-1")
    heartbeat = resumed.emit(component="aggregation", phase="run", status="heartbeat")

    assert heartbeat.elapsed_ms >= 15

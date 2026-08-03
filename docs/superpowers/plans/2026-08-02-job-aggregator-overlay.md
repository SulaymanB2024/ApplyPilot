# Job Aggregator Overlay Implementation Plan

> **Execution choice:** Inline execution in this thread, task-by-task, after planning is explicitly closed. This document remains planning-only until the user authorizes implementation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fast, observable ApplyPilot discovery overlay that progressively combines the local cache, first-party ATS sources, bounded JobSpy searches, and model-piloted Handshake and Runway browser missions. Add a separate opportunity-intelligence path for startups that recently raised or are hiring generally, without representing company leads as posted jobs or sending outreach without an exact authorization.

**Architecture:** A new `aggregation` package separates a 15-second deterministic fast lane from slower enrichment lanes. Cache, direct ATS, Workday, and SmartExtract publish immutable snapshot revision 1; bounded JobSpy workers and one serialized authenticated-browser queue for Handshake and Runway append observations and publish later immutable revisions without blocking revision 1. Every source emits privacy-bounded lifecycle events. The canonical workflow binds one exact snapshot revision and digest. Startup research writes `OpportunityLead` records to a separate ledger, verifies funding or active-hiring evidence, drafts truthful speculative outreach, and stops at a digest-bound approval boundary before any send.

**Tech Stack:** Python 3.11, `asyncio`, isolated worker processes for synchronous JobSpy calls, SQLite/WAL, NDJSON, Typer, Rich, the existing ApplyPilot browser handoff and discovery modules, `python-jobspy==1.1.82`, pytest, Ruff

---

## Why this design

The current product has two distinct discovery paths and both contribute to the bottleneck:

- Canonical `prepare` creates one ChatGPT Web discovery handoff. The latest inspected run waited about 45 seconds for that response, returned 5 roles against a 30-role budget, and later persisted `duration_ms: 0` because artifact restoration cannot reconstruct the actual model/browser duration.
- The legacy crawler runs provider families serially. The current user configuration contains 37 queries and 48 Workday employers, so Workday can fan out to 1,776 employer-query attempts before direct ATS and SmartExtract phases run. Direct ATS and `sites.yaml` also contain overlapping first-party targets.
- ApplyPilot already integrates JobSpy, but `_full_crawl` serializes every query-location pair, defaults to 100 results per site, enables request-heavy LinkedIn description fetching, and retries transient blocking failures. JobSpy itself can query boards concurrently, so the bottleneck is the current orchestration contract, not the absence of the library.
- `UsageLedger` records bounded model/browser counts but has no durable discovery-run, source-progress, first-result, deduplication, freshness, or timeout model.

The overlay changes the unit of work from “one model searches the web” or “one monolithic full crawl” to “many observable source adapters emit job observations.” It produces useful cached results immediately, fresh first-party results progressively, and a bounded snapshot at a known deadline. Models still have a discovery role where authenticated visual interaction is the useful capability: Handshake, Runway, and bounded startup research. Those missions enrich an already usable result set rather than sitting on its critical path.

## Provider capability boundary

| Source | MVP ingestion | Critical path | Capability boundary |
|---|---|---:|---|
| Existing ApplyPilot jobs DB | Read-only cache adapter | Yes | User-owned local data |
| Direct ATS, Workday, employer sites | Existing public first-party fetchers | Yes, bounded | Deterministic public-source lane |
| JobSpy | Isolated, capped board-search workers | No | Broad enrichment only; no proxy rotation, no full-description fetch during discovery, and first-party verification before advancement |
| Handshake | Model pilots the user's authenticated browser through visible search and result pages | No | Serialized interactive mission; no hidden endpoints, cookie export, bulk DOM extraction, or MFA/CAPTCHA bypass |
| Runway | Model pilots the browser through the provided explore interface | No | Serialized interactive mission with the same extraction and authentication limits |
| Startup signals | Model research plus public company, investor, YC, careers, and optional SEC evidence | No | Produces company-level opportunities, not invented job postings |
| ChatGPT Web | Ranking, materials, and bounded research artifacts | No | Typed handoffs with lifecycle telemetry, never hidden reasoning |

Primary-source design references:

- Handshake search access is account- and institution-dependent: <https://support.joinhandshake.com/hc/en-us/articles/218693408-Searching-for-Jobs-and-Internships>
- Handshake prohibits bulk automated collection of job descriptions and marketplace data: <https://joinhandshake.com/legal/tos/>
- Handshake's EDU API includes `/jobs`, but setup requires institution-scoped credentials from Handshake Support: <https://support.joinhandshake.com/hc/en-us/articles/31061076506391-Getting-Started-with-EDU-API>
- Runway exposes a public browse surface for fresh roles: <https://app.joinrunway.io/explore>
- Runway's terms require access through the provided interface: <https://www.joinrunway.io/terms>
- JobSpy 1.1.82 supports concurrent searches across Indeed, LinkedIn, Glassdoor, Google, ZipRecruiter, and other boards, while documenting request amplification and blocking risks: <https://github.com/speedyapply/JobSpy>
- JobSpy is MIT-licensed, but that license does not replace the access rules of each board queried through it: <https://github.com/speedyapply/JobSpy/blob/main/LICENSE>
- Y Combinator exposes public startup job and hiring surfaces suitable for lead discovery and later first-party verification: <https://www.ycombinator.com/jobs>
- SEC Form D datasets are public structured offering notices, updated quarterly and explicitly not guaranteed complete or accurate; they are corroborating evidence, not proof that a company is currently hiring: <https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets>

## Observable acceptance contract

- `applypilot aggregate --query QUERY --watch` emits a source-state update within 250 ms and a heartbeat at least once per second while work is active.
- A warm run emits cached candidates in under 1 second on the local benchmark fixture.
- Quick mode publishes immutable snapshot revision 1 after 15 seconds. JobSpy and browser missions may publish later revisions; they never delay revision 1.
- Direct ATS, Workday, and SmartExtract run concurrently and enforce local concurrency caps.
- JobSpy gets at most two query terms, two locations, 25 results per board, three concurrent workers, and a 30-second enrichment budget by default. It does not use proxy rotation or `linkedin_fetch_description`.
- Handshake and Runway missions share one browser lock, emit a checkpoint at least every 5 seconds, and stop at authentication takeover, navigation, result, and elapsed-time budgets.
- The same posting observed through cache, direct ATS, JobSpy, and a portal becomes one candidate with full provenance. A stable portal permalink may be the application URL until a first-party URL is resolved, but the candidate cannot advance beyond discovery until first-party verification succeeds.
- Every run persists first-result latency, source lifecycle, observations, merges, candidate count, duplicate count, timeouts, browser mission state, and elapsed time.
- `prepare --aggregation-snapshot RUN_ID@REVISION` performs zero discovery model calls, copies and binds that exact digest, and rejects the wrong query, digest, schema, revision, or state.
- Startup research creates an `OpportunityLead`; it never creates a `RoleCandidate` unless a real posting is verified. Funding fields may remain unknown and may not be inferred from weak evidence.
- Outreach may be researched and drafted during a run, but sending requires an exact channel, sender, recipient/lead set, message digest, and unexpired authorization. A draft is not a send and a send attempt is not a delivery receipt.
- Telemetry reports lifecycle and timing only. It never claims to expose hidden model reasoning, chain-of-thought, cookies, credentials, or raw applicant data.

## File map

### Create

- `src/applypilot/observability/events.py` — privacy-bounded append-only NDJSON events.
- `src/applypilot/aggregation/models.py` — request, observation, snapshot, and capability contracts.
- `src/applypilot/aggregation/normalization.py` — official-URL normalization and stable canonical keys.
- `src/applypilot/aggregation/store.py` — run/source/observation SQLite state.
- `src/applypilot/aggregation/orchestrator.py` — concurrent progressive coordinator.
- `src/applypilot/aggregation/snapshot.py` — versioned immutable snapshot `DiscoveryTool` adapter.
- `src/applypilot/aggregation/telemetry.py` — Rich projection of persisted events.
- `src/applypilot/aggregation/sources/{base,cache,direct_ats,workday,smart_extract,manual_import,jobspy}.py` — bounded source adapters.
- `src/applypilot/aggregation/jobspy_worker.py` — killable synchronous JobSpy worker entrypoint.
- `src/applypilot/aggregation/portal_handoff.py` — typed Handshake and Runway browser mission artifacts.
- `src/applypilot/opportunities/{models,store,research,outreach}.py` — company-signal, evidence, draft, authorization, and receipt contracts.
- `tests/test_observability_events.py`
- `tests/test_aggregation_models.py`
- `tests/test_aggregation_store.py`
- `tests/test_aggregation_sources.py`
- `tests/test_aggregation_orchestrator.py`
- `tests/test_aggregation_cli.py`
- `tests/test_aggregation_workflow.py`
- `tests/test_aggregation_jobspy.py`
- `tests/test_portal_discovery_handoff.py`
- `tests/test_aggregation_revisions.py`
- `tests/test_opportunity_research.py`
- `tests/test_opportunity_outreach.py`
- `scripts/benchmark_aggregation.py`

### Modify

- `src/applypilot/config.py` — aggregation paths.
- `src/applypilot/cli.py` — aggregation, portal-mission, opportunity, and exact-revision preparation commands.
- `src/applypilot/autonomy/policy.py` — immutable aggregation snapshot source policy.
- `src/applypilot/autonomy/runner.py` — bind and consume aggregation snapshots.
- `src/applypilot/autonomy/handoff.py` — model/browser lifecycle events.
- `src/applypilot/autonomy/telemetry.py` — shared event-journal projection.
- `src/applypilot/config/searches.example.yaml`
- `docs/CANONICAL_WORKFLOW.md`
- `README.md`

## Data flow

```text
FAST LANE (15 s)                                      ENRICHMENT LANES (non-blocking)
cache ───────────────┐                                JobSpy worker processes ─────┐
direct ATS ──────────┤   normalize + merge            Handshake browser mission ───┤
Workday ─────────────┼──────────────────────> run evidence <────────────────────────┤
SmartExtract ────────┘                         │      Runway browser mission ────────┘
                                              │
                                      snapshot rev 1, 2, 3...
                                              │ exact revision + digest
                                              v
                                      canonical workflow

startup/funding/hiring signals -> OpportunityLead -> verified fit/contact -> draft
                                                                        -> approval -> send receipt
```

The aggregation database is discovery evidence, not application truth. `workflow.sqlite3` remains the only owner of job-candidate state, approvals, reservations, and application outcomes. The opportunity ledger owns company-level research and outreach state; a promotion into the job workflow requires a verified posting. Neither database stores browser cookies, page dumps, or model chain-of-thought.

## Dependency-aware execution order

Execute inline in this order, not simple numeric order:

```text
Tasks 1–7 -> Task 11 -> Task 12 -> Task 8 -> Task 9 -> Task 13 -> Task 14 -> Task 10 -> Task 15
```

Tasks 1–7 establish the event, data, persistence, source, orchestration, and CLI base. Task 11 adds JobSpy before Task 12 adds browser enrichment and snapshot revisions. Task 8 then binds the already-versioned snapshot into the canonical workflow. Task 9 instruments remaining model/browser handoffs. Opportunity research and outreach follow, and the documentation/benchmark tasks close the work. Do not begin implementation while the user still labels the project as planning.

### Task 1: Add the shared privacy-bounded event journal

**Files:**
- Create: `src/applypilot/observability/__init__.py`
- Create: `src/applypilot/observability/events.py`
- Create: `tests/test_observability_events.py`

- [ ] **Step 1: Write the failing journal tests**

```python
import json

import pytest

from applypilot.observability.events import EventJournal


def test_journal_persists_ordered_events(tmp_path):
    journal = EventJournal(tmp_path / "events.ndjson", run_id="agg-1")
    first = journal.emit(component="aggregation", phase="run", status="started")
    second = journal.emit(
        component="aggregation",
        phase="source",
        status="progress",
        source="direct_ats",
        counts={"observed": 3, "unique": 2},
    )
    assert (first.sequence, second.sequence) == (1, 2)
    rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert rows[1]["counts"] == {"observed": 3, "unique": 2}
    assert rows[1]["schema_version"] == "applypilot.run-event.v1"
    assert journal.path.stat().st_mode & 0o777 == 0o600


def test_journal_resumes_sequence(tmp_path):
    path = tmp_path / "events.ndjson"
    EventJournal(path, run_id="agg-1").emit(
        component="aggregation", phase="run", status="started"
    )
    event = EventJournal(path, run_id="agg-1").emit(
        component="aggregation", phase="run", status="resumed"
    )
    assert event.sequence == 2


@pytest.mark.parametrize("key", ["prompt", "response", "cookie", "token", "profile"])
def test_journal_rejects_sensitive_detail_keys(tmp_path, key):
    journal = EventJournal(tmp_path / "events.ndjson", run_id="agg-1")
    with pytest.raises(ValueError, match="sensitive telemetry detail key"):
        journal.emit(
            component="model", phase="handoff", status="waiting", detail={key: "secret"}
        )
```

- [ ] **Step 2: Run the tests and verify the module is missing**

Run: `pytest tests/test_observability_events.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'applypilot.observability'`.

- [ ] **Step 3: Implement the journal**

```python
# src/applypilot/observability/events.py
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVENT_SCHEMA_VERSION = "applypilot.run-event.v1"
FORBIDDEN_DETAIL_KEYS = frozenset(
    {"prompt", "response", "cookie", "cookies", "token", "profile", "resume", "secret"}
)


@dataclass(frozen=True)
class RunEvent:
    schema_version: str
    run_id: str
    sequence: int
    occurred_at: str
    component: str
    phase: str
    status: str
    elapsed_ms: int
    source: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    detail: dict[str, str | int | float | bool | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventJournal:
    def __init__(self, path: Path, *, run_id: str) -> None:
        self.path = path.resolve()
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._sequence = self._last_sequence()

    def emit(
        self,
        *,
        component: str,
        phase: str,
        status: str,
        source: str = "",
        counts: dict[str, int] | None = None,
        detail: dict[str, str | int | float | bool | None] | None = None,
    ) -> RunEvent:
        bounded = dict(detail or {})
        forbidden = FORBIDDEN_DETAIL_KEYS & {key.lower() for key in bounded}
        if forbidden:
            raise ValueError(f"sensitive telemetry detail key: {sorted(forbidden)[0]}")
        if len(bounded) > 20:
            raise ValueError("telemetry detail exceeds 20 fields")
        if any(isinstance(value, str) and len(value) > 240 for value in bounded.values()):
            raise ValueError("telemetry detail string exceeds 240 characters")
        with self._lock:
            self._sequence += 1
            event = RunEvent(
                schema_version=EVENT_SCHEMA_VERSION,
                run_id=self.run_id,
                sequence=self._sequence,
                occurred_at=datetime.now(timezone.utc).isoformat(),
                component=component,
                phase=phase,
                status=status,
                elapsed_ms=int((time.monotonic() - self._started) * 1000),
                source=source,
                counts={str(key): int(value) for key, value in (counts or {}).items()},
                detail=bounded,
            )
            payload = (json.dumps(event.to_dict(), sort_keys=True) + "\n").encode("utf-8")
            descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.chmod(self.path, 0o600)
            return event

    def read(self) -> list[RunEvent]:
        if not self.path.exists():
            return []
        return [RunEvent(**json.loads(line)) for line in self.path.read_text().splitlines() if line]

    def _last_sequence(self) -> int:
        if not self.path.exists():
            return 0
        lines = [line for line in self.path.read_text().splitlines() if line]
        return int(json.loads(lines[-1])["sequence"]) if lines else 0
```

```python
# src/applypilot/observability/__init__.py
from applypilot.observability.events import EventJournal, RunEvent

__all__ = ["EventJournal", "RunEvent"]
```

- [ ] **Step 4: Run the focused tests**

Run: `pytest tests/test_observability_events.py -q`

Expected: `7 passed`.

- [ ] **Step 5: Commit the event journal**

```bash
git add src/applypilot/observability tests/test_observability_events.py
git commit -m "feat: add durable run event journal"
```

### Task 2: Define source-neutral contracts and official-URL normalization

**Files:**
- Create: `src/applypilot/aggregation/__init__.py`
- Create: `src/applypilot/aggregation/models.py`
- Create: `src/applypilot/aggregation/normalization.py`
- Create: `src/applypilot/aggregation/sources/{__init__,base}.py`
- Create: `tests/test_aggregation_models.py`

- [ ] **Step 1: Write failing normalization and merge tests**

```python
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
        discovery_url="https://app.joinhandshake.com/jobs/987",
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
    ["https://app.joinhandshake.com/jobs/987", "https://app.joinrunway.io/jobs/987"],
)
def test_restricted_portal_cannot_be_official_url(url):
    with pytest.raises(ValueError, match="official_url must resolve to an employer or ATS posting"):
        normalize_job(raw(SourceKind.HANDSHAKE_BROWSER, url))


def test_portal_permalink_is_retained_as_unverified_discovery_identity():
    observed = normalize_job(
        RawJob(
            source=SourceKind.HANDSHAKE_BROWSER,
            source_job_id="handshake-987",
            title="Product Analytics Intern",
            company="Example Labs",
            location="Austin, TX",
            official_url="",
            application_url="https://app.joinhandshake.com/jobs/987",
            discovery_url="https://app.joinhandshake.com/jobs/987",
            description="Visible portal description",
            observed_at=NOW,
        )
    )
    assert observed.verification_state is VerificationState.PORTAL_ONLY
    assert observed.advanceable is False


def test_portal_observation_with_resolved_first_party_url_is_advanceable():
    observed = normalize_job(
        RawJob(
            source=SourceKind.RUNWAY_BROWSER,
            source_job_id="runway-987",
            title="Product Analytics Intern",
            company="Example Labs",
            location="Austin, TX",
            official_url="https://jobs.example.com/123",
            application_url="https://jobs.example.com/123",
            discovery_url="https://app.joinrunway.io/jobs/987",
            description="Visible portal description",
            observed_at=NOW,
        )
    )
    assert observed.verification_state is VerificationState.FIRST_PARTY_RESOLVED
    assert observed.advanceable is True
```

- [ ] **Step 2: Run the tests and verify the package is missing**

Run: `pytest tests/test_aggregation_models.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'applypilot.aggregation'`.

- [ ] **Step 3: Implement the contracts**

```python
# src/applypilot/aggregation/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import AsyncIterator, Protocol


class SourceKind(StrEnum):
    CACHE = "cache"
    DIRECT_ATS = "direct_ats"
    WORKDAY = "workday"
    SMART_EXTRACT = "smart_extract"
    JOBSPY = "jobspy"
    HANDSHAKE_BROWSER = "handshake_browser"
    RUNWAY_BROWSER = "runway_browser"
    HANDSHAKE_MANUAL = "handshake_manual"
    RUNWAY_MANUAL = "runway_manual"


class SourceCapability(StrEnum):
    AUTOMATIC_PUBLIC = "automatic_public"
    LOCAL_CACHE = "local_cache"
    BOUNDED_AGGREGATOR = "bounded_aggregator"
    INTERACTIVE_BROWSER = "interactive_browser"
    USER_IMPORT_ONLY = "user_import_only"


class VerificationState(StrEnum):
    FIRST_PARTY_RESOLVED = "first_party_resolved"
    PORTAL_ONLY = "portal_only"
    BOARD_ONLY = "board_only"


@dataclass(frozen=True)
class AggregationRequest:
    query: str
    query_terms: tuple[str, ...]
    locations: tuple[str, ...] = ()
    limit: int = 100
    mode: str = "quick"
    global_deadline_seconds: float = 15.0
    per_source_timeout_seconds: float = 10.0
    max_concurrency: int = 8

    def validate(self) -> None:
        if not self.query.strip() or not self.query_terms:
            raise ValueError("query and at least one bounded query term are required")
        if self.mode not in {"quick", "deep"}:
            raise ValueError("aggregation mode must be quick or deep")
        if not 1 <= self.limit <= 500:
            raise ValueError("aggregation limit must be between 1 and 500")
        if self.global_deadline_seconds <= 0 or self.per_source_timeout_seconds <= 0:
            raise ValueError("aggregation deadlines must be positive")
        if not 1 <= self.max_concurrency <= 32:
            raise ValueError("aggregation concurrency must be between 1 and 32")


@dataclass(frozen=True)
class RawJob:
    source: SourceKind
    source_job_id: str
    title: str
    company: str
    location: str
    official_url: str
    discovery_url: str
    description: str
    observed_at: datetime
    application_url: str = ""
    salary: str = ""
    posted_at: str = ""
    metadata: dict[str, str | int | float | bool | None] = field(default_factory=dict)


@dataclass(frozen=True)
class JobObservation:
    canonical_key: str
    source: SourceKind
    source_job_id: str
    title: str
    company: str
    location: str
    official_url: str
    application_url: str
    discovery_url: str
    description: str
    observed_at: str
    salary: str = ""
    posted_at: str = ""
    verification_state: VerificationState = VerificationState.FIRST_PARTY_RESOLVED
    advanceable: bool = True
    metadata: dict[str, str | int | float | bool | None] = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalJob:
    canonical_key: str
    title: str
    company: str
    location: str
    official_url: str
    application_url: str
    description: str
    salary: str
    posted_at: str
    verification_state: VerificationState
    advanceable: bool
    observations: tuple[JobObservation, ...]

    @property
    def source_count(self) -> int:
        return len({item.source for item in self.observations})


class SourceAdapter(Protocol):
    kind: SourceKind
    capability: SourceCapability

    def search(self, request: AggregationRequest) -> AsyncIterator[RawJob]:
        raise NotImplementedError
```

- [ ] **Step 4: Implement normalization and package exports**

```python
# src/applypilot/aggregation/normalization.py
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from urllib.parse import urlsplit

from applypilot.aggregation.models import (
    CanonicalJob,
    JobObservation,
    RawJob,
    SourceKind,
    VerificationState,
)
from applypilot.workflow import canonicalize_url

RESTRICTED_PORTAL_HOSTS = frozenset(
    {"app.joinhandshake.com", "utaustin.joinhandshake.com", "app.joinrunway.io"}
)


def normalize_job(raw: RawJob) -> JobObservation:
    official_url = canonicalize_url(raw.official_url) if raw.official_url else ""
    discovery_url = canonicalize_url(raw.discovery_url)
    application_url = canonicalize_url(raw.application_url or raw.official_url or raw.discovery_url)
    host = (urlsplit(official_url).hostname or "").lower()
    if official_url and (
        not official_url.startswith(("https://", "http://")) or host in RESTRICTED_PORTAL_HOSTS
    ):
        raise ValueError("official_url must resolve to an employer or ATS posting")
    interactive_sources = {
        SourceKind.HANDSHAKE_BROWSER,
        SourceKind.RUNWAY_BROWSER,
        SourceKind.HANDSHAKE_MANUAL,
        SourceKind.RUNWAY_MANUAL,
    }
    if not official_url and raw.source not in interactive_sources and raw.source is not SourceKind.JOBSPY:
        raise ValueError("non-portal observations require an official_url")
    if not discovery_url.startswith(("https://", "http://")):
        raise ValueError("discovery_url must be an HTTP URL")
    if not raw.title.strip() or not raw.company.strip():
        raise ValueError("job title and company are required")
    identity = official_url or f"{raw.source.value}:{raw.source_job_id}:{discovery_url}"
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    if official_url:
        verification_state = VerificationState.FIRST_PARTY_RESOLVED
    elif raw.source is SourceKind.JOBSPY:
        verification_state = VerificationState.BOARD_ONLY
    else:
        verification_state = VerificationState.PORTAL_ONLY
    return JobObservation(
        canonical_key=key,
        source=raw.source,
        source_job_id=raw.source_job_id.strip(),
        title=raw.title.strip()[:300],
        company=raw.company.strip()[:300],
        location=raw.location.strip()[:300],
        official_url=official_url,
        application_url=application_url,
        discovery_url=discovery_url,
        description=raw.description.strip()[:20_000],
        observed_at=raw.observed_at.isoformat(),
        salary=raw.salary.strip()[:300],
        posted_at=raw.posted_at.strip()[:80],
        verification_state=verification_state,
        advanceable=verification_state is VerificationState.FIRST_PARTY_RESOLVED,
        metadata={str(key): value for key, value in list(raw.metadata.items())[:30]},
    )


def merge_observations(observations: Iterable[JobObservation]) -> CanonicalJob:
    ordered = tuple(observations)
    if not ordered or len({item.canonical_key for item in ordered}) != 1:
        raise ValueError("merge requires observations for one canonical key")
    freshest = max(ordered, key=lambda item: item.observed_at)
    return CanonicalJob(
        canonical_key=freshest.canonical_key,
        title=freshest.title,
        company=freshest.company,
        location=freshest.location,
        official_url=next((item.official_url for item in reversed(ordered) if item.official_url), ""),
        application_url=next(
            (item.application_url for item in reversed(ordered) if item.application_url), ""
        ),
        description=max((item.description for item in ordered), key=len, default=""),
        salary=next((item.salary for item in reversed(ordered) if item.salary), ""),
        posted_at=next((item.posted_at for item in reversed(ordered) if item.posted_at), ""),
        verification_state=(
            VerificationState.FIRST_PARTY_RESOLVED
            if any(item.advanceable for item in ordered)
            else freshest.verification_state
        ),
        advanceable=any(item.advanceable for item in ordered),
        observations=ordered,
    )
```

Use these exact exports:

```python
# src/applypilot/aggregation/__init__.py
from applypilot.aggregation.models import AggregationRequest, CanonicalJob, RawJob, SourceKind

__all__ = ["AggregationRequest", "CanonicalJob", "RawJob", "SourceKind"]
```

```python
# src/applypilot/aggregation/sources/base.py
from applypilot.aggregation.models import SourceAdapter

__all__ = ["SourceAdapter"]
```

```python
# src/applypilot/aggregation/sources/__init__.py
from applypilot.aggregation.sources.base import SourceAdapter

__all__ = ["SourceAdapter"]
```

Run: `pytest tests/test_aggregation_models.py -q`

Expected: `6 passed`.

- [ ] **Step 5: Commit the contracts**

```bash
git add src/applypilot/aggregation tests/test_aggregation_models.py
git commit -m "feat: define aggregation job contracts"
```

### Task 3: Persist aggregation runs, source state, and observations

**Files:**
- Modify: `src/applypilot/config.py`
- Create: `src/applypilot/aggregation/store.py`
- Create: `tests/test_aggregation_store.py`

- [ ] **Step 1: Write the failing store lifecycle test**

```python
from datetime import datetime, timezone

from applypilot.aggregation.models import AggregationRequest, RawJob, SourceKind
from applypilot.aggregation.normalization import normalize_job
from applypilot.aggregation.store import AggregationStore


def test_store_projects_two_observations_into_one_job(tmp_path):
    store = AggregationStore(tmp_path / "aggregation.sqlite3")
    request = AggregationRequest(query="product internships", query_terms=("product intern",))
    store.start_run("agg-1", request)
    for source in (SourceKind.CACHE, SourceKind.DIRECT_ATS):
        store.start_source("agg-1", source)
        store.record_observation(
            "agg-1",
            normalize_job(
                RawJob(
                    source=source,
                    source_job_id=source.value,
                    title="Product Intern",
                    company="Example Labs",
                    location="Austin, TX",
                    official_url="https://jobs.example.com/123",
                    discovery_url="https://jobs.example.com/123",
                    description="First-party description" if source is SourceKind.DIRECT_ATS else "Cached",
                    observed_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
                )
            ),
        )
        store.finish_source("agg-1", source, status="complete")
    snapshot = store.complete_run("agg-1")
    assert snapshot["candidate_count"] == 1
    assert snapshot["observation_count"] == 2
    assert snapshot["duplicate_count"] == 1
    assert snapshot["jobs"][0]["source_count"] == 2
    assert snapshot["jobs"][0]["description"] == "First-party description"
```

- [ ] **Step 2: Run the test and verify the store is missing**

Run: `pytest tests/test_aggregation_store.py -q`

Expected: FAIL during collection for missing `applypilot.aggregation.store`.

- [ ] **Step 3: Add the aggregation paths and SQLite schema**

Add to `src/applypilot/config.py` and include `AGGREGATION_RUN_DIR` in `ensure_dirs()`:

```python
AGGREGATION_DB_PATH = APP_DIR / "aggregation.sqlite3"
AGGREGATION_RUN_DIR = APP_DIR / "aggregation-runs"
```

Create the connection in WAL mode and execute this schema in `AggregationStore.__init__`:

```sql
CREATE TABLE IF NOT EXISTS aggregation_runs (
    run_id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    observation_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS aggregation_sources (
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    observed_count INTEGER NOT NULL DEFAULT 0,
    error_class TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, source_id),
    FOREIGN KEY (run_id) REFERENCES aggregation_runs(run_id)
);
CREATE TABLE IF NOT EXISTS job_observations (
    run_id TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    source TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (run_id, source, source_job_id, canonical_key),
    FOREIGN KEY (run_id) REFERENCES aggregation_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_job_observations_run_key
    ON job_observations(run_id, canonical_key);
```

- [ ] **Step 4: Implement the exact public store contract**

Implement these methods in `src/applypilot/aggregation/store.py`:

```python
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot.aggregation.models import AggregationRequest, JobObservation, SourceKind
from applypilot.aggregation.normalization import merge_observations


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AggregationStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def start_run(self, run_id: str, request: AggregationRequest) -> None:
        request.validate()
        with self.connection:
            self.connection.execute(
                "INSERT INTO aggregation_runs(run_id, query, request_json, status, created_at) "
                "VALUES(?, ?, ?, 'running', ?)",
                (run_id, request.query, json.dumps(asdict(request), sort_keys=True), _now()),
            )

    def start_source(
        self, run_id: str, source: SourceKind, *, source_id: str | None = None
    ) -> str:
        stable_id = source_id or source.value
        with self.connection:
            self.connection.execute(
                "INSERT INTO aggregation_sources(run_id, source_id, source_kind, status, started_at) "
                "VALUES(?, ?, ?, 'running', ?)",
                (run_id, stable_id, source.value, _now()),
            )
        return stable_id

    def record_observation(
        self, run_id: str, observation: JobObservation, *, source_id: str | None = None
    ) -> bool:
        stable_id = source_id or observation.source.value
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO job_observations("
                "run_id, canonical_key, source, source_job_id, payload_json, observed_at"
                ") VALUES(?, ?, ?, ?, ?, ?)",
                (
                    run_id, observation.canonical_key, observation.source.value,
                    observation.source_job_id,
                    json.dumps(asdict(observation), sort_keys=True), observation.observed_at,
                ),
            )
            if cursor.rowcount:
                self.connection.execute(
                    "UPDATE aggregation_sources SET observed_count = observed_count + 1 "
                    "WHERE run_id = ? AND source_id = ?",
                    (run_id, stable_id),
                )
            return bool(cursor.rowcount)

    def finish_source(
        self, run_id: str, source: SourceKind, *, status: str,
        error_class: str = "", source_id: str | None = None,
    ) -> None:
        if status not in {"complete", "partial", "failed", "timed_out", "cancelled"}:
            raise ValueError("invalid aggregation source terminal status")
        stable_id = source_id or source.value
        with self.connection:
            self.connection.execute(
                "UPDATE aggregation_sources SET status = ?, error_class = ?, completed_at = ? "
                "WHERE run_id = ? AND source_id = ? AND source_kind = ?",
                (status, error_class[:120], _now(), run_id, stable_id, source.value),
            )

    def snapshot(self, run_id: str) -> dict[str, Any]:
        run = self.connection.execute(
            "SELECT * FROM aggregation_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise KeyError(f"unknown aggregation run: {run_id}")
        rows = self.connection.execute(
            "SELECT payload_json FROM job_observations WHERE run_id = ? "
            "ORDER BY canonical_key, observed_at, source",
            (run_id,),
        ).fetchall()
        grouped = {}
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["source"] = SourceKind(payload["source"])
            observation = JobObservation(**payload)
            grouped.setdefault(observation.canonical_key, []).append(observation)
        jobs = []
        for key in sorted(grouped):
            merged = merge_observations(grouped[key])
            item = asdict(merged)
            item["source_count"] = merged.source_count
            item["observations"] = [asdict(row) for row in merged.observations]
            jobs.append(item)
        sources = [
            dict(row)
            for row in self.connection.execute(
                "SELECT source_id, source_kind AS source, status, observed_count, error_class, "
                "started_at, completed_at FROM aggregation_sources "
                "WHERE run_id = ? ORDER BY source_kind, source_id",
                (run_id,),
            ).fetchall()
        ]
        return {
            "schema_version": "applypilot.aggregation-snapshot.v1",
            "run_id": run_id, "query": run["query"], "status": run["status"],
            "candidate_count": len(jobs), "observation_count": len(rows),
            "duplicate_count": len(rows) - len(jobs), "sources": sources, "jobs": jobs,
        }

    def complete_run(self, run_id: str, *, status: str = "complete") -> dict[str, Any]:
        snapshot = self.snapshot(run_id)
        with self.connection:
            self.connection.execute(
                "UPDATE aggregation_runs SET status = ?, candidate_count = ?, "
                "observation_count = ?, duplicate_count = ?, completed_at = ? WHERE run_id = ?",
                (
                    status, snapshot["candidate_count"], snapshot["observation_count"],
                    snapshot["duplicate_count"], _now(), run_id,
                ),
            )
        return self.snapshot(run_id)
```

The implementation rules are exact:

1. `start_run` calls `request.validate()`, serializes `asdict(request)` with sorted keys, and inserts `status='running'`.
2. `start_source`, `record_observation`, and `finish_source` default `source_id` to the source kind for single-unit adapters. Multi-unit adapters use stable non-sensitive IDs such as `jobspy:indeed:<request digest prefix>` so every unit has durable state. Validate IDs against `[a-z0-9:_.-]{1,120}`; never embed raw query/location text.
3. `record_observation` uses `INSERT OR IGNORE`; on insertion it increments that exact source unit's `observed_count` in the same transaction and returns `True`.
4. `finish_source` accepts only `complete`, `partial`, `failed`, `timed_out`, or `cancelled`; it stores only `type(exc).__name__`, never exception text.
5. `snapshot` groups observation JSON by `canonical_key`, restores `SourceKind`, calls `merge_observations`, and returns the internal base projection `applypilot.aggregation-snapshot.v1` with sorted jobs and source-unit rows. This projection is not consumable by `prepare`; Task 12 upgrades publication to immutable versioned schema v2 before Task 8 binds it into the canonical workflow.
6. `complete_run` calculates `duplicate_count = observation_count - candidate_count`, persists terminal counts, then returns `snapshot`.

Run: `pytest tests/test_aggregation_store.py -q`

Expected: `1 passed`.

- [ ] **Step 5: Commit the store**

```bash
git add src/applypilot/config.py src/applypilot/aggregation/store.py tests/test_aggregation_store.py
git commit -m "feat: persist aggregation run state"
```

### Task 4: Add cache, direct ATS, and manual portal adapters

**Files:**
- Create: `src/applypilot/aggregation/sources/cache.py`
- Create: `src/applypilot/aggregation/sources/direct_ats.py`
- Create: `src/applypilot/aggregation/sources/manual_import.py`
- Modify: `src/applypilot/aggregation/sources/__init__.py`
- Create: `tests/test_aggregation_sources.py`

- [ ] **Step 1: Write failing adapter tests**

```python
import asyncio
import json
import sqlite3
from datetime import datetime, timezone

from applypilot.aggregation.models import AggregationRequest, SourceKind
from applypilot.aggregation.sources.cache import CacheSource
from applypilot.aggregation.sources.direct_ats import DirectATSSource
from applypilot.aggregation.sources.manual_import import ManualImportSource

REQUEST = AggregationRequest(query="product internships", query_terms=("product intern",))


async def collect(source):
    return [item async for item in source.search(REQUEST)]


def test_cache_reads_without_writing(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE jobs(url TEXT, title TEXT, location TEXT, site TEXT, description TEXT, "
        "full_description TEXT, application_url TEXT, discovered_at TEXT, salary TEXT)"
    )
    connection.execute(
        "INSERT INTO jobs VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "https://jobs.example.com/123", "Product Intern", "Austin, TX", "Example Labs",
            "Short", "Full description", "https://jobs.example.com/123",
            datetime.now(timezone.utc).isoformat(), "$25/hour",
        ),
    )
    connection.commit()
    connection.close()
    jobs = asyncio.run(collect(CacheSource(db_path=path, max_age_days=30)))
    assert len(jobs) == 1
    assert jobs[0].source is SourceKind.CACHE


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
                "source": "handshake_manual", "source_job_id": "987",
                "title": "Product Intern", "company": "Example Labs", "location": "Austin, TX",
                "discovery_url": "https://app.joinhandshake.com/jobs/987",
                "official_url": "https://jobs.example.com/123",
                "description": "Copied from the official employer posting",
            }
        ) + "\n",
        encoding="utf-8",
    )
    jobs = asyncio.run(collect(ManualImportSource(path)))
    assert jobs[0].source is SourceKind.HANDSHAKE_MANUAL
    assert jobs[0].official_url == "https://jobs.example.com/123"
```

- [ ] **Step 2: Run the tests and verify the adapters are missing**

Run: `pytest tests/test_aggregation_sources.py -q`

Expected: FAIL during collection for the three missing source modules.

- [ ] **Step 3: Implement the cache and manual adapters**

`CacheSource.search` must open `file:{db_path}?mode=ro`, select rows no older than `max_age_days`, order by `discovered_at DESC`, cap input at `request.limit * 3`, and emit `RawJob` only when a lower-cased query term appears in the title. Use `application_url` before `url`, `full_description` before `description`, and the row's `site` as company provenance.

```python
class CacheSource:
    kind = SourceKind.CACHE
    capability = SourceCapability.LOCAL_CACHE

    def __init__(self, *, db_path: Path, max_age_days: int = 14) -> None:
        self.db_path = db_path.resolve()
        self.max_age_days = max_age_days

    async def search(self, request: AggregationRequest) -> AsyncIterator[RawJob]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.max_age_days)).isoformat()
        connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT rowid, * FROM jobs WHERE discovered_at >= ? "
                "ORDER BY discovered_at DESC LIMIT ?",
                (cutoff, request.limit * 3),
            ).fetchall()
        finally:
            connection.close()
        terms = tuple(term.lower() for term in request.query_terms)
        for row in rows:
            title = str(row["title"] or "")
            if terms and not any(term in title.lower() for term in terms):
                continue
            url = str(row["application_url"] or row["url"] or "")
            if url:
                yield RawJob(
                    source=self.kind, source_job_id=str(row["rowid"]), title=title,
                    company=str(row["site"] or "Unknown employer"),
                    location=str(row["location"] or ""), official_url=url,
                    discovery_url=str(row["url"] or url),
                    description=str(row["full_description"] or row["description"] or ""),
                    observed_at=datetime.fromisoformat(str(row["discovered_at"])),
                    salary=str(row["salary"] or ""),
                )
```

Create `ManualImportSource` with this complete validation path:

```python
class ManualImportSource:
    capability = SourceCapability.USER_IMPORT_ONLY

    def __init__(self, path: Path) -> None:
        self.path = path.resolve(strict=True)
        self.kind = SourceKind.HANDSHAKE_MANUAL

    async def search(self, request: AggregationRequest) -> AsyncIterator[RawJob]:
        del request
        allowed = {
            "handshake_manual": SourceKind.HANDSHAKE_MANUAL,
            "runway_manual": SourceKind.RUNWAY_MANUAL,
        }
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            payload = json.loads(line)
            source = allowed.get(str(payload.get("source") or ""))
            if source is None:
                raise ValueError(f"unsupported manual source on line {line_number}")
            for key in ("source_job_id", "title", "company", "official_url", "discovery_url"):
                if not str(payload.get(key) or "").strip():
                    raise ValueError(f"manual import line {line_number} is missing {key}")
            yield RawJob(
                source=source,
                source_job_id=str(payload["source_job_id"]),
                title=str(payload["title"]),
                company=str(payload["company"]),
                location=str(payload.get("location") or ""),
                official_url=str(payload["official_url"]),
                discovery_url=str(payload["discovery_url"]),
                description=str(payload.get("description") or ""),
                observed_at=datetime.now(timezone.utc),
                salary=str(payload.get("salary") or ""),
                posted_at=str(payload.get("posted_at") or ""),
                metadata={"capture_mode": "user_copy"},
            )
```

- [ ] **Step 4: Implement bounded direct ATS fanout**

```python
class DirectATSSource:
    kind = SourceKind.DIRECT_ATS
    capability = SourceCapability.AUTOMATIC_PUBLIC

    def __init__(self, *, sources: list[dict] | None = None, concurrency: int = 4) -> None:
        self.sources = sources
        self.concurrency = concurrency
        self.errors: list[dict[str, str]] = []

    async def search(self, request: AggregationRequest) -> AsyncIterator[RawJob]:
        sources = self.sources if self.sources is not None else load_direct_ats_sources()
        semaphore = asyncio.Semaphore(self.concurrency)

        async def fetch(source):
            async with semaphore:
                try:
                    return source, await asyncio.to_thread(_fetch_source_jobs, source)
                except Exception as exc:
                    self.errors.append(
                        {"source": str(source.get("name") or source["slug"]), "error_class": type(exc).__name__}
                    )
                    return source, []

        tasks = [asyncio.create_task(fetch(source)) for source in sources]
        terms = tuple(term.lower() for term in request.query_terms)
        for task in asyncio.as_completed(tasks):
            source, jobs = await task
            company = str(source.get("name") or source["slug"])
            for index, job in enumerate(jobs):
                title = str(job.get("title") or "")
                if terms and not any(term in title.lower() for term in terms):
                    continue
                url = str(job.get("application_url") or job.get("url") or "")
                if url:
                    yield RawJob(
                        source=self.kind,
                        source_job_id=str(job.get("id") or f"{source['slug']}-{index}"),
                        title=title,
                        company=company,
                        location=str(job.get("location") or ""),
                        official_url=url,
                        discovery_url=str(job.get("url") or url),
                        description=str(job.get("full_description") or job.get("description") or ""),
                        observed_at=datetime.now(timezone.utc),
                        salary=str(job.get("salary") or ""),
                        metadata={"ats": str(source["ats"])},
                    )
```

Export the three adapters from `sources/__init__.py`.

- [ ] **Step 5: Run adapter tests and commit**

Run: `pytest tests/test_aggregation_sources.py -q`

Expected: `3 passed`.

```bash
git add src/applypilot/aggregation/sources tests/test_aggregation_sources.py
git commit -m "feat: add fast aggregation sources"
```

### Task 5: Add bounded Workday and SmartExtract slow lanes

**Files:**
- Create: `src/applypilot/aggregation/sources/workday.py`
- Create: `src/applypilot/aggregation/sources/smart_extract.py`
- Modify: `src/applypilot/aggregation/sources/__init__.py`
- Modify: `tests/test_aggregation_sources.py`

- [ ] **Step 1: Add failing bounds and ownership tests**

```python
def test_workday_caps_attempts(monkeypatch):
    calls = []
    employers = {f"e{index}": {"name": f"Employer {index}"} for index in range(20)}

    def search_one(key, employer, query):
        calls.append((key, query))
        return [{"title": "Product Intern", "external_url": f"https://{key}.wd1/jobs/123"}]

    request = AggregationRequest(
        query="product internships",
        query_terms=("product intern", "data analyst", "business analyst", "ignored"),
    )
    source = WorkdaySource(
        employers=employers, search_one=search_one,
        max_employers=12, max_query_terms=3, concurrency=6,
    )
    jobs = asyncio.run(collect_with_request(source, request))
    assert len(calls) == 36
    assert len(jobs) == 36


def test_smart_extract_owns_only_employer_careers(monkeypatch):
    sites = [
        {"name": "Careers", "url": "https://example.com/careers", "source_kind": "employer_careers", "direct_source": True},
        {"name": "Duplicate ATS", "url": "https://jobs.ashbyhq.com/example", "source_kind": "direct_ats", "direct_source": True},
        {"name": "Runway", "url": "https://app.joinrunway.io/explore", "source_kind": "account_backed_recruiter", "direct_source": True},
    ]
    monkeypatch.setattr(
        "applypilot.aggregation.sources.smart_extract._run_one_site",
        lambda name, url: {"jobs": [{"title": "Product Intern", "url": "https://example.com/jobs/123"}]},
    )
    jobs = asyncio.run(collect(SmartExtractSource(sites=sites, max_targets=6)))
    assert [job.discovery_url for job in jobs] == ["https://example.com/careers"]
```

Add:

```python
async def collect_with_request(source, request):
    return [item async for item in source.search(request)]
```

- [ ] **Step 2: Run tests and verify the slow-lane modules are missing**

Run: `pytest tests/test_aggregation_sources.py -q`

Expected: FAIL during collection for missing `workday` and `smart_extract` modules.

- [ ] **Step 3: Implement Workday with a 36-attempt default ceiling**

`WorkdaySource` selects the first 12 explicitly configured employers and the first 3 `request.query_terms`, then runs `search_employer` calls through `asyncio.to_thread` under a semaphore of 6. It yields a `RawJob` from `external_url` or `url`, records only exception classes, and never calls `store_results`. The nested loop is exactly:

```python
tasks = [
    asyncio.create_task(fetch(key, employer, term))
    for key, employer in list(employers.items())[: self.max_employers]
    for term in request.query_terms[: self.max_query_terms]
]
for task in asyncio.as_completed(tasks):
    try:
        employer_key, employer, jobs = await task
    except Exception as exc:
        self.errors.append(type(exc).__name__)
        continue
    for index, job in enumerate(jobs):
        url = str(job.get("external_url") or job.get("url") or "")
        if url:
            yield RawJob(
                source=SourceKind.WORKDAY,
                source_job_id=str(job.get("id") or f"{employer_key}-{index}"),
                title=str(job.get("title") or ""),
                company=str(employer.get("name") or employer_key),
                location=str(job.get("location") or ""),
                official_url=url,
                discovery_url=url,
                description=str(job.get("description") or ""),
                observed_at=datetime.now(timezone.utc),
            )
```

- [ ] **Step 4: Implement SmartExtract with exclusive target ownership**

`SmartExtractSource` selects only entries with `direct_source is True` and `source_kind == "employer_careers"`, caps at 6, and runs `_run_one_site(name, url)` through a semaphore of 2. It must exclude `direct_ats` targets already owned by `DirectATSSource` and `account_backed_recruiter` targets such as Runway. Convert returned jobs to `RawJob` without writing to the legacy jobs DB.

```python
targets = [
    site
    for site in configured_sites
    if site.get("direct_source") is True
    and site.get("source_kind") == "employer_careers"
][: self.max_targets]
semaphore = asyncio.Semaphore(self.concurrency)

async def fetch(site):
    async with semaphore:
        result = await asyncio.to_thread(_run_one_site, str(site["name"]), str(site["url"]))
        return site, result

for task in asyncio.as_completed([asyncio.create_task(fetch(site)) for site in targets]):
    site, result = await task
    for index, job in enumerate(result.get("jobs") or []):
        url = str(job.get("application_url") or job.get("url") or "")
        if url:
            yield RawJob(
                source=SourceKind.SMART_EXTRACT,
                source_job_id=str(job.get("id") or f"{site['name']}-{index}"),
                title=str(job.get("title") or ""), company=str(job.get("company") or site["name"]),
                location=str(job.get("location") or ""), official_url=url,
                discovery_url=str(site["url"]), description=str(job.get("description") or ""),
                observed_at=datetime.now(timezone.utc), salary=str(job.get("salary") or ""),
            )
```

Run: `pytest tests/test_aggregation_sources.py -q`

Expected: `5 passed`.

- [ ] **Step 5: Commit the slow lanes**

```bash
git add src/applypilot/aggregation/sources tests/test_aggregation_sources.py
git commit -m "feat: add bounded aggregation slow lanes"
```

### Task 6: Orchestrate sources concurrently with progressive events

**Files:**
- Create: `src/applypilot/aggregation/orchestrator.py`
- Create: `tests/test_aggregation_orchestrator.py`

- [ ] **Step 1: Write failing concurrency and failure-isolation tests**

Use two fake async sources with 10 ms and 100 ms delays. Both emit the same official URL. Assert the first `candidate/observed` event precedes the slow source's `source/complete`, the snapshot contains one candidate and two observations, and one failing third source changes run status to `partial` without removing the candidate.

- [ ] **Step 2: Run tests and verify the orchestrator is missing**

Run: `pytest tests/test_aggregation_orchestrator.py -q`

Expected: FAIL during collection for missing `applypilot.aggregation.orchestrator`.

- [ ] **Step 3: Implement the per-source consumer**

```python
async def _consume(self, run_id, request, source):
    self.store.start_source(run_id, source.kind)
    self.journal.emit(
        component="aggregation", phase="source", status="started", source=source.kind.value
    )
    observed = 0
    try:
        async with asyncio.timeout(request.per_source_timeout_seconds):
            async for raw in source.search(request):
                inserted = self.store.record_observation(run_id, normalize_job(raw))
                observed += int(inserted)
                snapshot = self.store.snapshot(run_id)
                self.journal.emit(
                    component="aggregation",
                    phase="candidate",
                    status="observed" if inserted else "duplicate",
                    source=source.kind.value,
                    counts={
                        "source_observed": observed,
                        "unique": snapshot["candidate_count"],
                        "observations": snapshot["observation_count"],
                    },
                )
        status, error_class = "complete", ""
    except TimeoutError:
        status, error_class = "timed_out", "TimeoutError"
    except Exception as exc:
        status, error_class = "failed", type(exc).__name__
    self.store.finish_source(
        run_id, source.kind, status=status, error_class=error_class
    )
    self.journal.emit(
        component="aggregation",
        phase="source",
        status=status,
        source=source.kind.value,
        counts={"observed": observed},
        detail={"error_class": error_class} if error_class else {},
    )
    return status
```

- [ ] **Step 4: Implement run-level deadlines and heartbeats**

`Aggregator.run` validates the request, starts the store run, emits `run/started`, and starts `_consume` for all sources with `asyncio.gather`. Wrap the gather in `asyncio.timeout(global_deadline_seconds)`. On global timeout, cancel unfinished tasks, mark still-running source rows `cancelled` with `GlobalDeadline`, and await cancellation. Start a heartbeat task that emits `run/heartbeat` every second with current candidate and observation counts; cancel and await it in `finally`. Finish `complete` only when every source is `complete`; otherwise finish `partial`.

```python
async def heartbeat():
    while True:
        await asyncio.sleep(1)
        snapshot = self.store.snapshot(run_id)
        self.journal.emit(
            component="aggregation", phase="run", status="heartbeat",
            counts={
                "candidates": snapshot["candidate_count"],
                "observations": snapshot["observation_count"],
            },
        )

heartbeat_task = asyncio.create_task(heartbeat())
source_tasks = [asyncio.create_task(self._consume(run_id, request, source)) for source in self.sources]
try:
    async with asyncio.timeout(request.global_deadline_seconds):
        statuses = await asyncio.gather(*source_tasks)
except TimeoutError:
    for task in source_tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*source_tasks, return_exceptions=True)
    current = self.store.snapshot(run_id)["sources"]
    for source in self.sources:
        row = next(item for item in current if item["source"] == source.kind.value)
        if row["status"] == "running":
            self.store.finish_source(
                run_id, source.kind, status="cancelled", error_class="GlobalDeadline"
            )
    statuses = [row["status"] for row in self.store.snapshot(run_id)["sources"]]
finally:
    heartbeat_task.cancel()
    await asyncio.gather(heartbeat_task, return_exceptions=True)
terminal = "complete" if statuses and all(status == "complete" for status in statuses) else "partial"
return self.store.complete_run(run_id, status=terminal)
```

Run: `pytest tests/test_aggregation_orchestrator.py -q`

Expected: concurrency, timeout, heartbeat, dedupe, and failure-isolation tests pass.

- [ ] **Step 5: Commit the orchestrator**

```bash
git add src/applypilot/aggregation/orchestrator.py tests/test_aggregation_orchestrator.py
git commit -m "feat: orchestrate progressive job aggregation"
```

### Task 7: Add aggregation CLI commands and live terminal telemetry

**Files:**
- Create: `src/applypilot/aggregation/telemetry.py`
- Modify: `src/applypilot/cli.py`
- Create: `tests/test_aggregation_cli.py`

- [ ] **Step 1: Write failing CLI tests**

```python
import json

from typer.testing import CliRunner

from applypilot.cli import app

runner = CliRunner()


def test_aggregate_status_returns_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    result = runner.invoke(app, ["aggregate-status", "--run-id", "missing", "--json"])
    assert result.exit_code == 1
    assert "unknown aggregation run" in result.stdout


def test_aggregate_requires_portal_flag_for_browser_source(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    result = runner.invoke(
        app,
        ["aggregate", "--query", "product internships", "--term", "product intern", "--source", "handshake"],
    )
    assert result.exit_code == 1
    assert "use --portal handshake for a browser mission" in result.stdout
```

- [ ] **Step 2: Run tests and verify the commands are missing**

Run: `pytest tests/test_aggregation_cli.py -q`

Expected: both tests FAIL because `aggregate` and `aggregate-status` are not registered.

- [ ] **Step 3: Implement the Rich telemetry projection**

```python
import asyncio

from rich.console import Console
from rich.live import Live
from rich.table import Table

from applypilot.observability.events import RunEvent


def build_table(events: list[RunEvent]) -> Table:
    latest = {}
    for event in events:
        if event.source:
            latest[event.source] = event
    table = Table(title="ApplyPilot aggregation")
    table.add_column("Source")
    table.add_column("State")
    table.add_column("Observed", justify="right")
    table.add_column("Elapsed", justify="right")
    for source in sorted(latest):
        event = latest[source]
        table.add_row(
            source,
            event.status,
            str(event.counts.get("observed", event.counts.get("source_observed", 0))),
            f"{event.elapsed_ms / 1000:.1f}s",
        )
    return table


async def run_with_live(*, aggregator, run_id, request, journal, console: Console):
    task = asyncio.create_task(aggregator.run(run_id, request))
    with Live(build_table(journal.read()), console=console, refresh_per_second=4) as live:
        while not task.done():
            live.update(build_table(journal.read()))
            await asyncio.sleep(0.25)
        snapshot = await task
        live.update(build_table(journal.read()), refresh=True)
        return snapshot
```

Run `run_with_live` only when `--watch` is active; otherwise await `Aggregator.run` directly. Render to `Console(stderr=True)` so stdout remains valid JSON.

- [ ] **Step 4: Register the exact CLI surface**

Add:

```text
applypilot aggregate --query TEXT --term TEXT [--term TEXT] [--location TEXT] [--source cache|direct_ats|workday|smart_extract] [--enrich jobspy] [--portal handshake|runway] [--import PATH] [--mode quick|deep] [--watch/--no-watch]
applypilot aggregate-status --run-id ID [--json]
```

Behavior:

1. Generate `agg-YYYYMMDDTHHMMSSZ-<8 hex>` and use `AGGREGATION_RUN_DIR / run_id`.
2. Quick mode sets 15-second global and 10-second source deadlines; deep mode sets 90 and 30.
3. The fast-lane registry contains cache, direct ATS, Workday, and SmartExtract only. Task 11 adds JobSpy enrichment and Task 12 activates portal mission queues.
4. `--source handshake` and `--source runway` exit 1 and direct the caller to the explicit `--portal` option; portals are never mistaken for deterministic source adapters.
5. `--import` adds `ManualImportSource` and still subjects every row to official-URL normalization.
6. Persist the initial snapshot mode `0600` and print `{run_id,status,candidate_count,snapshot_revision,snapshot_path,events_path,pending_enrichment}` as JSON.
7. `aggregate-status` reads the store only; it never restarts a provider.

Use this dispatch so the table updates while providers are working:

```python
async def execute():
    if watch:
        return await run_with_live(
            aggregator=aggregator, run_id=run_id, request=request,
            journal=journal, console=Console(stderr=True),
        )
    return await aggregator.run(run_id, request)

snapshot = asyncio.run(execute())
```

Run: `pytest tests/test_aggregation_cli.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Commit CLI and telemetry**

```bash
git add src/applypilot/aggregation/telemetry.py src/applypilot/cli.py tests/test_aggregation_cli.py
git commit -m "feat: expose observable aggregation CLI"
```

### Task 8: Bind immutable aggregation snapshots into the canonical workflow

**Files:**
- Create: `src/applypilot/aggregation/snapshot.py`
- Modify: `src/applypilot/autonomy/policy.py`
- Modify: `src/applypilot/autonomy/runner.py`
- Modify: `src/applypilot/cli.py`
- Create: `tests/test_aggregation_workflow.py`

- [ ] **Step 1: Write failing snapshot adapter tests**

```python
import json

import pytest

from applypilot.aggregation.snapshot import SnapshotDiscovery
from applypilot.autonomy.context import build_context_pack

PACK = build_context_pack(
    {"experience": {"current_title": "Student analyst"}},
    resume_text="Python SQL analytics",
    job_text="product internships",
)


def write_snapshot(path, status="complete"):
    path.write_text(
        json.dumps(
            {
                "schema_version": "applypilot.aggregation-snapshot.v2",
                "run_id": "agg-1", "revision": 2, "parent_sha256": "0" * 64,
                "query": "product internships", "status": status,
                "candidate_count": 1, "observation_count": 1, "duplicate_count": 0,
                "sources": [{"source": "direct_ats", "status": "complete"}],
                "jobs": [{
                    "canonical_key": "abc", "title": "Product Intern", "company": "Example Labs",
                    "location": "Austin, TX", "official_url": "https://jobs.example.com/123",
                    "application_url": "https://jobs.example.com/123",
                    "description": "Product analytics internship", "salary": "$25/hour",
                    "posted_at": "2026-08-01", "source_count": 1, "advanceable": True,
                    "observations": [{"source": "direct_ats", "discovery_url": "https://jobs.example.com/123"}],
                }],
            }
        ),
        encoding="utf-8",
    )


def test_snapshot_returns_bound_candidates(tmp_path):
    path = tmp_path / "snapshot.json"
    write_snapshot(path)
    candidates = SnapshotDiscovery(path, expected_query="product internships").find_roles(
        pack=PACK, query="product internships", limit=30
    )
    assert candidates[0].source == "aggregation_snapshot"
    assert candidates[0].metadata["aggregation_run_id"] == "agg-1"


@pytest.mark.parametrize("status", ["running", "failed"])
def test_snapshot_rejects_non_consumable_state(tmp_path, status):
    path = tmp_path / "snapshot.json"
    write_snapshot(path, status)
    with pytest.raises(ValueError, match="snapshot is not consumable"):
        SnapshotDiscovery(path, expected_query="product internships")
```

- [ ] **Step 2: Run tests and verify the adapter is missing**

Run: `pytest tests/test_aggregation_workflow.py -q`

Expected: FAIL during collection for missing `applypilot.aggregation.snapshot`.

- [ ] **Step 3: Implement `SnapshotDiscovery`**

```python
class SnapshotDiscovery:
    def __init__(self, path: Path, *, expected_query: str, expected_sha256: str = "") -> None:
        self.path = path.resolve(strict=True)
        self.payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
        if expected_sha256 and self.sha256 != expected_sha256:
            raise ValueError("aggregation snapshot digest mismatch")
        if self.payload.get("schema_version") != "applypilot.aggregation-snapshot.v2":
            raise ValueError("unsupported aggregation snapshot schema")
        if int(self.payload.get("revision") or 0) < 1:
            raise ValueError("aggregation snapshot revision is invalid")
        if self.payload.get("status") not in {"complete", "partial"}:
            raise ValueError("aggregation snapshot is not consumable")
        if self.payload.get("query") != expected_query:
            raise ValueError("aggregation snapshot query mismatch")

    def find_roles(self, *, pack, query: str, limit: int) -> list[RoleCandidate]:
        del pack
        if query != self.payload["query"]:
            raise ValueError("aggregation request query changed")
        candidates = []
        for item in self.payload.get("jobs") or []:
            if item.get("advanceable") is not True:
                continue
            posted = str(item.get("posted_at") or "")
            candidates.append(
                RoleCandidate(
                    company=str(item["company"]), title=str(item["title"]),
                    official_url=str(item["official_url"]), source="aggregation_snapshot",
                    location=str(item.get("location") or ""),
                    description=str(item.get("description") or ""),
                    posted_date=date.fromisoformat(posted[:10]) if len(posted) >= 10 else None,
                    evidence=tuple(
                        f"{row['source']}:{row.get('discovery_url', '')}"
                        for row in item.get("observations") or []
                    ),
                    metadata={
                        "aggregation_run_id": str(self.payload["run_id"]),
                        "aggregation_snapshot_revision": int(self.payload["revision"]),
                        "aggregation_snapshot_sha256": self.sha256,
                        "source_count": int(item.get("source_count") or 0),
                    },
                )
            )
        return candidates[:limit]
```

Imports: `hashlib`, `json`, `date`, `Path`, and `RoleCandidate`.

- [ ] **Step 4: Bind the snapshot through policy, manifest, and CLI**

Make these exact changes:

1. Set `SourcePolicy.primary = "aggregation_snapshot"`; retain direct ATS as the only recorded-failure fallback. Keep direct Handshake, Runway, JobSpy, and broad-aggregator calls disabled *inside the canonical workflow* because the upstream overlay owns those enrichment sources.
2. Extend `prepare_run` with `aggregation_snapshot_path: Path | None` and `aggregation_snapshot_revision: int | None`. Copy the exact recorded revision to `run_dir / "aggregation_snapshot.json"`, chmod `0600`, verify its query/revision/digest chain, and add its SHA-256 to `immutable_artifacts`.
3. Add `aggregation_run_id`, `aggregation_snapshot_revision`, and `aggregation_snapshot_sha256` to the run manifest.
4. When a snapshot is supplied, do not call `prepare_discovery_request`.
5. In `advance_artifact_run`, pass `SnapshotDiscovery` as `BatchDependencies.discovery`; keep `ArtifactChatGPTClient` as `materials`.
6. Add `prepare --aggregation-snapshot RUN_ID@REVISION`. Reject path separators, a missing/zero revision, an unrecorded digest, an unversioned latest pointer, or use together with `--legacy-web-discovery`.
7. Keep model-based web discovery only behind `--legacy-web-discovery` for one compatibility release.

Use this binding block:

```python
source = aggregation_snapshot_path.resolve(strict=True)
target = run_dir / "aggregation_snapshot.json"
target.write_bytes(source.read_bytes())
target.chmod(0o600)
snapshot = json.loads(target.read_text(encoding="utf-8"))
if snapshot.get("query") != query:
    raise ValueError("aggregation snapshot query mismatch")
manifest["aggregation_run_id"] = str(snapshot.get("run_id") or "")
manifest["aggregation_snapshot_revision"] = int(snapshot.get("revision") or 0)
manifest["aggregation_snapshot_sha256"] = _sha256_file(target)
manifest["immutable_artifacts"][target.name] = _sha256_file(target)
```

Add an integration assertion that snapshot-backed advancement reaches its first material handoff with `usage.counts.model_calls == 0` before that material response is serviced.

- [ ] **Step 5: Run boundary regressions and commit**

Run: `pytest tests/test_aggregation_workflow.py tests/test_autonomy.py tests/test_workflow.py -q`

Expected: PASS; snapshot binding, artifact immutability, candidate state, and approval regressions remain green.

```bash
git add src/applypilot/aggregation/snapshot.py src/applypilot/autonomy/policy.py src/applypilot/autonomy/runner.py src/applypilot/cli.py tests/test_aggregation_workflow.py
git commit -m "feat: prepare from immutable aggregation snapshots"
```

### Task 9: Instrument model/browser handoff lifecycle

**Files:**
- Modify: `src/applypilot/autonomy/handoff.py`
- Modify: `src/applypilot/autonomy/telemetry.py`
- Modify: `src/applypilot/autonomy/runner.py`
- Modify: `tests/test_semantic_discovery_handoff.py`
- Modify: `tests/test_autonomy.py`

- [ ] **Step 1: Write failing lifecycle event tests**

Create one request, import a valid response, and consume it. Assert ordered phases:

```python
assert [(event.phase, event.status) for event in journal.read()] == [
    ("handoff_request", "created"),
    ("handoff_wait", "started"),
    ("handoff_response", "imported"),
    ("handoff_validation", "started"),
    ("handoff_validation", "complete"),
    ("handoff_wait", "complete"),
]
assert journal.read()[-1].detail["response_chars"] > 0
assert "response" not in journal.read()[-1].detail
```

Add a rejected-import test expecting `handoff_validation/error` with only `error_class` and character counts.

- [ ] **Step 2: Run tests and verify the event file is absent**

Run: `pytest tests/test_semantic_discovery_handoff.py tests/test_autonomy.py -q -k 'handoff_event'`

Expected: FAIL because no shared lifecycle events are emitted.

- [ ] **Step 3: Project usage events to the shared journal**

Add `journal: EventJournal | None = None` to `UsageLedger` and:

```python
def lifecycle(self, *, phase, status, surface, detail=None):
    self.record_event(
        stage="handoff", operation=phase, surface=surface, status=status,
        error_class=str((detail or {}).get("error_class") or ""),
    )
    if self.journal is not None:
        self.journal.emit(
            component="model_or_browser", phase=phase, status=status,
            source=surface, detail=detail,
        )
```

Construct every run-scoped ledger with `EventJournal(run_dir / "events.ndjson", run_id=run_id)`.

`import_response_artifact` has no `UsageLedger` argument, so it must construct the same `EventJournal` directly from `request_path.parent.parent` and the bound `run_id`. This preserves one sequence without inventing a second usage ledger.

- [ ] **Step 4: Emit exact lifecycle boundaries**

Emit request-created and wait-started after writing a new handoff, response-imported before parse, validation-started/complete/error around `parse_chatgpt_response`, and wait-complete after accepted consumption. Calculate wait elapsed from the request file birth time. Store only kind, prompt/response character counts, elapsed milliseconds, and error class; never text, applicant fields, URLs, or model reasoning.

```python
self.ledger.lifecycle(
    phase="handoff_request", status="created", surface="chatgpt_web_artifact",
    detail={"kind": kind, "prompt_chars": len(prompt)},
)
self.ledger.lifecycle(
    phase="handoff_wait", status="started", surface="chatgpt_web_artifact",
    detail={"kind": kind},
)

journal.emit(
    component="model_or_browser", phase="handoff_response", status="imported",
    source="chatgpt_web_artifact", detail={"kind": expected_kind, "response_chars": len(text)},
)
journal.emit(
    component="model_or_browser", phase="handoff_validation", status="started",
    source="chatgpt_web_artifact", detail={"kind": expected_kind},
)
try:
    payload = parse_chatgpt_response(text, expected_kind=expected_kind, request_id=request_id)
except Exception as exc:
    journal.emit(
        component="model_or_browser", phase="handoff_validation", status="error",
        source="chatgpt_web_artifact", detail={"error_class": type(exc).__name__},
    )
    raise
journal.emit(
    component="model_or_browser", phase="handoff_validation", status="complete",
    source="chatgpt_web_artifact", detail={"kind": expected_kind},
)
```

- [ ] **Step 5: Run handoff regressions and commit**

Run: `pytest tests/test_semantic_discovery_handoff.py tests/test_autonomy.py tests/test_supervisor.py -q`

Expected: PASS with rejected-artifact recovery and usage restoration intact.

```bash
git add src/applypilot/autonomy/handoff.py src/applypilot/autonomy/telemetry.py src/applypilot/autonomy/runner.py tests/test_semantic_discovery_handoff.py tests/test_autonomy.py
git commit -m "feat: expose model handoff lifecycle telemetry"
```

### Task 10: Add latency benchmark, configuration, and documentation

**Files:**
- Create: `scripts/benchmark_aggregation.py`
- Modify: `src/applypilot/config/searches.example.yaml`
- Modify: `docs/CANONICAL_WORKFLOW.md`
- Modify: `README.md`

- [ ] **Step 1: Add a deterministic benchmark**

The script creates three fixture adapters: cache waits 50 ms, direct ATS 250 ms, and Workday 500 ms; each emits the same 20 canonical URLs. It runs `Aggregator`, writes `benchmark.json`, and exits 0 only when:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from applypilot.aggregation.models import (
    AggregationRequest, RawJob, SourceCapability, SourceKind,
)
from applypilot.aggregation.orchestrator import Aggregator
from applypilot.aggregation.store import AggregationStore
from applypilot.observability.events import EventJournal


class FixtureSource:
    capability = SourceCapability.AUTOMATIC_PUBLIC

    def __init__(self, kind: SourceKind, delay: float) -> None:
        self.kind = kind
        self.delay = delay

    async def search(self, request):
        del request
        await asyncio.sleep(self.delay)
        for index in range(20):
            yield RawJob(
                source=self.kind, source_job_id=f"{self.kind.value}-{index}",
                title="Product Intern", company=f"Example {index}", location="Austin, TX",
                official_url=f"https://jobs.example.com/{index}",
                discovery_url=f"https://jobs.example.com/{index}",
                description=f"Fixture from {self.kind.value}",
                observed_at=datetime.now(timezone.utc),
            )


async def benchmark(output: Path) -> dict:
    journal = EventJournal(output / "events.ndjson", run_id="benchmark")
    request = AggregationRequest(
        query="product internships", query_terms=("product intern",),
        global_deadline_seconds=15, per_source_timeout_seconds=10,
    )
    started = time.monotonic()
    snapshot = await Aggregator(
        store=AggregationStore(output / "aggregation.sqlite3"), journal=journal,
        sources=[
            FixtureSource(SourceKind.CACHE, 0.05),
            FixtureSource(SourceKind.DIRECT_ATS, 0.25),
            FixtureSource(SourceKind.WORKDAY, 0.50),
        ],
    ).run("benchmark", request)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    first_candidate_ms = next(
        event.elapsed_ms
        for event in journal.read()
        if event.phase == "candidate" and event.status == "observed"
    )
    report = {
        "elapsed_ms": elapsed_ms,
        "first_candidate_ms": first_candidate_ms,
        "candidate_count": snapshot["candidate_count"],
        "observation_count": snapshot["observation_count"],
        "event_count": len(journal.read()),
    }
    report["pass"] = (
        first_candidate_ms < 1000
        and elapsed_ms < 3000
        and report["candidate_count"] == 20
        and report["observation_count"] == 60
    )
    (output / "benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output is None:
        with tempfile.TemporaryDirectory(prefix="applypilot-aggregation-benchmark-") as directory:
            report = asyncio.run(benchmark(Path(directory)))
    else:
        args.output.mkdir(parents=True, exist_ok=True)
        report = asyncio.run(benchmark(args.output))
    print(json.dumps(report, sort_keys=True))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

The script uses a temporary directory by default and accepts `--output PATH` to preserve evidence.

- [ ] **Step 2: Add bounded example configuration**

```yaml
aggregation:
  default_mode: quick
  quick_deadline_seconds: 15
  deep_deadline_seconds: 90
  per_source_timeout_seconds: 30
  max_concurrency: 8
  workday_max_employers: 12
  workday_max_query_terms: 3
  smart_extract_max_targets: 6
  cache_max_age_days: 14
  automatic_sources: [cache, direct_ats, workday, smart_extract]
  enrichment_sources: [jobspy, handshake_browser, runway_browser]
  jobspy:
    enabled: true
    boards: [indeed, google, zip_recruiter]
    max_query_terms: 2
    max_locations: 2
    results_per_board: 25
    max_workers: 3
    deadline_seconds: 30
    use_proxies: false
    fetch_full_descriptions: false
  browser_portals:
    enabled: [handshake, runway]
    serialized: true
    max_results_per_portal: 25
    max_navigations_per_portal: 30
    deadline_seconds_per_portal: 180
    checkpoint_seconds: 5
  opportunities:
    enabled: true
    recent_funding_days: 45
    max_company_leads: 25
    max_drafts_per_run: 10
    require_send_authorization: true
```

Document that repeated `--term` options supply the bounded provider terms. Never silently expand all 37 configured queries.

- [ ] **Step 3: Update the canonical commands and source boundaries**

```bash
applypilot aggregate \
  --query "Paid Summer 2027 product and analytics internships in Austin, New York, San Francisco, Chicago, or Remote US" \
  --term "product intern" \
  --term "data analyst intern" \
  --term "business analyst intern" \
  --location "Austin, TX" \
  --location "Remote US" \
  --enrich jobspy \
  --portal handshake \
  --portal runway \
  --mode quick \
  --watch

applypilot prepare \
  --query "Paid Summer 2027 product and analytics internships in Austin, New York, San Francisco, Chicago, or Remote US" \
  --aggregation-snapshot AGGREGATION_RUN_ID@REVISION

applypilot opportunities discover \
  --signal recently-funded \
  --signal actively-hiring \
  --recent-days 45 \
  --watch
```

State plainly that Handshake and Runway are model-piloted browser missions using the user's authenticated browser, not hidden-API or bulk scraper integrations. Document user takeover for OTP, passkeys, CAPTCHA, and any provider challenge. Explain that the 15-second snapshot does not wait for those missions; each validated mission response publishes a new immutable revision. Explain that JobSpy is broad discovery evidence, not first-party verification, and that startup opportunities and outreach receipts are separate from job applications. Telemetry exposes lifecycle, counts, deadlines, validation, and safe navigation checkpoints, not model chain-of-thought.

- [ ] **Step 4: Run focused validation**

```bash
python3 scripts/benchmark_aggregation.py
pytest tests/test_observability_events.py tests/test_aggregation_models.py tests/test_aggregation_store.py tests/test_aggregation_sources.py tests/test_aggregation_orchestrator.py tests/test_aggregation_cli.py tests/test_aggregation_workflow.py tests/test_discovery_mode.py tests/test_semantic_discovery_handoff.py tests/test_workflow.py -q
ruff check src/applypilot/observability src/applypilot/aggregation src/applypilot/autonomy/telemetry.py src/applypilot/autonomy/handoff.py src/applypilot/autonomy/runner.py src/applypilot/cli.py tests/test_observability_events.py tests/test_aggregation_models.py tests/test_aggregation_store.py tests/test_aggregation_sources.py tests/test_aggregation_orchestrator.py tests/test_aggregation_cli.py tests/test_aggregation_workflow.py scripts/benchmark_aggregation.py
git diff --check
```

Expected: benchmark `pass=true`; all focused tests pass; Ruff is clean; `git diff --check` exits 0. These checks cover the new persistence, provider, concurrency, canonical-workflow, and telemetry boundaries. Skip the full suite unless a focused check exposes a shared-contract regression or the branch is entering a merge/release gate.

- [ ] **Step 5: Commit benchmark and docs**

```bash
git add scripts/benchmark_aggregation.py src/applypilot/config/searches.example.yaml docs/CANONICAL_WORKFLOW.md README.md
git commit -m "docs: define aggregation latency and source contract"
```

### Task 11: Add JobSpy as a bounded, killable enrichment lane

**Files:**
- Create: `src/applypilot/aggregation/sources/jobspy.py`
- Create: `src/applypilot/aggregation/jobspy_worker.py`
- Create: `tests/test_aggregation_jobspy.py`
- Modify: `src/applypilot/aggregation/orchestrator.py`
- Modify: `src/applypilot/config/searches.example.yaml`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing unit-planning and worker-contract tests**

```python
from applypilot.aggregation.sources.jobspy import JobSpySettings, build_jobspy_units


def test_jobspy_units_are_bounded_and_do_not_enable_bypass_features():
    settings = JobSpySettings(
        boards=("indeed", "google", "zip_recruiter"),
        max_query_terms=2,
        max_locations=2,
        results_per_board=25,
        max_workers=3,
        deadline_seconds=30,
    )
    units = build_jobspy_units(
        terms=("product intern", "data analyst intern", "ignored third term"),
        locations=("Austin, TX", "Remote US", "ignored third location"),
        settings=settings,
    )
    assert len(units) == 12
    assert {unit.board for unit in units} == {"indeed", "google", "zip_recruiter"}
    assert all(unit.results_wanted == 25 for unit in units)
    assert all(unit.proxies == () for unit in units)
    assert all(unit.linkedin_fetch_description is False for unit in units)


def test_jobspy_rejects_unconfigured_or_duplicated_boards():
    with pytest.raises(ValueError, match="board allowlist"):
        JobSpySettings(boards=("indeed", "indeed", "unknown_board")).validate()
```

The test count is `boards × bounded terms × bounded locations`. Do not silently expand all 37 search terms or all five locations.

- [ ] **Step 2: Write failing subprocess, timeout, and normalization tests**

Use a fake worker executable that reads one mode-`0600` JSON request and writes one mode-`0600` response. Assert:

1. The parent launches it with `sys.executable -m applypilot.aggregation.jobspy_worker`, a new process session, a scrubbed environment, and no credential/proxy fields.
2. A successful row with `job_url_direct=https://jobs.example.com/123` becomes `FIRST_PARTY_RESOLVED`.
3. A row containing only an Indeed/Google/ZipRecruiter permalink becomes `BOARD_ONLY` and `advanceable=False`.
4. At the exact unit deadline the parent sends `SIGTERM` only to the spawned process group, waits at most 2 seconds, then uses `SIGKILL` only if that same PID remains alive.
5. Timeout produces `source/jobspy_unit/timeout` and allows other units and snapshot publication to continue.
6. A worker response with the wrong request digest, board, schema, or excess result count is rejected before observations are stored.

Run: `pytest tests/test_aggregation_jobspy.py -q`

Expected: FAIL because the source and worker modules do not exist.

- [ ] **Step 3: Implement the bounded unit contract**

```python
@dataclass(frozen=True)
class JobSpySettings:
    boards: tuple[str, ...] = ("indeed", "google", "zip_recruiter")
    max_query_terms: int = 2
    max_locations: int = 2
    results_per_board: int = 25
    max_workers: int = 3
    deadline_seconds: float = 30.0

    def validate(self) -> None:
        allowed = {"indeed", "google", "zip_recruiter", "linkedin", "glassdoor"}
        if not self.boards or len(set(self.boards)) != len(self.boards):
            raise ValueError("JobSpy board allowlist must be unique and non-empty")
        if not set(self.boards) <= allowed:
            raise ValueError("JobSpy board allowlist contains an unsupported board")
        if not 1 <= self.max_query_terms <= 4 or not 1 <= self.max_locations <= 4:
            raise ValueError("JobSpy query and location caps must be between 1 and 4")
        if not 1 <= self.results_per_board <= 50 or not 1 <= self.max_workers <= 4:
            raise ValueError("JobSpy result and worker caps are out of bounds")


@dataclass(frozen=True)
class JobSpyUnit:
    board: str
    term: str
    location: str
    results_wanted: int
    hours_old: int = 168
    proxies: tuple[str, ...] = ()
    linkedin_fetch_description: bool = False
```

`build_jobspy_units` slices terms and locations before taking the Cartesian product, preserves caller order, and uses a stable SHA-256 request digest. Default configuration excludes LinkedIn and Glassdoor; either may be explicitly enabled only after its board access policy is reviewed. The adapter never accepts proxy credentials and does not copy the legacy `_scrape_with_retry` behavior.

- [ ] **Step 4: Implement the isolated worker and parent adapter**

The worker performs exactly one `scrape_jobs` call:

```python
kwargs = {
    "site_name": [request["board"]],
    "search_term": request["term"],
    "location": request["location"],
    "results_wanted": request["results_wanted"],
    "hours_old": request["hours_old"],
    "description_format": "markdown",
    "verbose": 0,
    "linkedin_fetch_description": False,
}
```

For Google, supply the explicit `google_search_term` produced by a deterministic formatter. Never pass `proxies`, cookies, browser profiles, or user-agent overrides. Serialize only the documented JobPost fields needed by `RawJob`, cap descriptions at 20,000 characters, and reject output beyond `results_wanted`.

The parent uses `asyncio.create_subprocess_exec(..., start_new_session=True)`, limits concurrent workers with a semaphore, and captures bounded stderr. It records queued, started, complete, empty, error, and timeout events with board/term/location hashes and counts—not raw queries or descriptions. Because the worker is a separate process, a timeout actually stops work; wrapping the synchronous library in `asyncio.to_thread` is insufficient because cancellation would leave the scrape running.

- [ ] **Step 5: Integrate it without delaying the fast snapshot**

`Aggregator.run` publishes revision 1 when the fast-lane deadline closes, then starts or continues JobSpy only if requested. Each JobSpy batch completion commits observations and may publish one later revision. Coalesce completions arriving within 500 ms so twelve units do not create twelve near-identical snapshots.

Each unit uses `source_id=jobspy:<board>:<request digest prefix>` in `AggregationStore`; the family-level status projection derives running, partial, timed-out, and complete counts from those unit rows.

Any JobSpy-only candidate remains visible as `needs_first_party_verification`. It cannot enter material generation, browser form review, or outreach until the existing first-party verifier resolves a current employer/ATS posting.

- [ ] **Step 6: Pin, test, and commit**

Change the discovery extra from `python-jobspy>=1.1.82` to `python-jobspy==1.1.82`; dependency upgrades require a separate compatibility check because scraper behavior is provider-sensitive.

Run:

```bash
pytest tests/test_aggregation_jobspy.py tests/test_aggregation_orchestrator.py tests/test_discovery_mode.py -q
ruff check src/applypilot/aggregation/sources/jobspy.py src/applypilot/aggregation/jobspy_worker.py tests/test_aggregation_jobspy.py
```

Expected: PASS; no live job-board request is made by tests.

```bash
git add pyproject.toml src/applypilot/aggregation/sources/jobspy.py src/applypilot/aggregation/jobspy_worker.py src/applypilot/aggregation/orchestrator.py src/applypilot/config/searches.example.yaml tests/test_aggregation_jobspy.py
git commit -m "feat: add bounded JobSpy enrichment workers"
```

### Task 12: Add serialized Handshake and Runway browser missions plus snapshot revisions

**Files:**
- Create: `src/applypilot/aggregation/portal_handoff.py`
- Create: `tests/test_portal_discovery_handoff.py`
- Create: `tests/test_aggregation_revisions.py`
- Modify: `src/applypilot/autonomy/handoff.py`
- Modify: `src/applypilot/aggregation/store.py`
- Modify: `src/applypilot/aggregation/orchestrator.py`
- Modify: `src/applypilot/aggregation/snapshot.py`
- Modify: `src/applypilot/cli.py`

- [ ] **Step 1: Write failing portal request/response contract tests**

```python
def test_portal_mission_is_bounded_and_contains_no_auth_material(tmp_path):
    request = PortalMissionRequest(
        run_id="agg-1",
        portal=Portal.HANDSHAKE,
        start_url="https://app.joinhandshake.com/stu/postings",
        query_terms=("product intern", "data analyst intern"),
        locations=("Austin, TX", "Remote US"),
        max_results=25,
        max_navigations=30,
        max_seconds=180,
    )
    payload = request.to_dict()
    assert payload["permitted_hosts"] == [
        "app.joinhandshake.com",
        "utaustin.joinhandshake.com",
    ]
    assert not ({"cookie", "token", "password", "otp", "profile_path"} & set(payload))


def test_portal_response_accepts_portal_native_application(tmp_path):
    response = validated_response(
        portal="handshake",
        observations=[{
            "source_job_id": "987",
            "title": "Product Analytics Intern",
            "company": "Example Labs",
            "location": "Austin, TX",
            "discovery_url": "https://app.joinhandshake.com/stu/jobs/987",
            "application_url": "https://app.joinhandshake.com/stu/jobs/987",
            "official_url": "",
        }],
    )
    assert response.observations[0].verification_state == "portal_only"
    assert response.observations[0].advanceable is False


def test_portal_response_rejects_wrong_domain_or_excess_results(tmp_path):
    with pytest.raises(PortalContractError):
        validate_portal_response(response_with_discovery_host("evil.example"))
    with pytest.raises(PortalContractError):
        validate_portal_response(response_with_result_count(26))
```

Also test response digest binding, duplicate provider IDs, invalid URLs, overlong text, and statuses `complete`, `partial`, `auth_required`, `blocked`, and `budget_exhausted`.

- [ ] **Step 2: Define the browser mission schema**

```python
class Portal(StrEnum):
    HANDSHAKE = "handshake"
    RUNWAY = "runway"


@dataclass(frozen=True)
class PortalMissionRequest:
    run_id: str
    portal: Portal
    start_url: str
    query_terms: tuple[str, ...]
    locations: tuple[str, ...]
    max_results: int = 25
    max_navigations: int = 30
    max_seconds: int = 180


@dataclass(frozen=True)
class PortalMissionObservation:
    source_job_id: str
    title: str
    company: str
    location: str
    discovery_url: str
    application_url: str
    official_url: str = ""
    description: str = ""
    salary: str = ""
    posted_at: str = ""
```

Request schema: `applypilot.portal-mission.v1`. Response schema: `applypilot.portal-mission-response.v1`. Bind both to the aggregation run ID, query digest, portal, request ID, request SHA-256, budgets, and response path. Files are regular non-symlinks under the run's `handoff/` directory and mode `0600`.

The model pilot may use visible page state, normal clicks, typing, scrolling, and opening the displayed application link. It may not export cookies, read browser-profile files, call hidden/private endpoints, inject scripts for bulk page extraction, defeat provider challenges, or record full DOM/page dumps. When login requires OTP, passkey, CAPTCHA, or a provider challenge, return `auth_required` with the safe current domain and takeover reason; never store the challenge value.

- [ ] **Step 3: Extend the existing durable handoff queue**

Add these exact stage/kind mappings:

```python
stage_kinds = {
    # existing entries remain
    ("portal_discovery", "handshake_job_observations"): "browser_tool",
    ("portal_discovery", "runway_job_observations"): "browser_tool",
    ("opportunity_research", "startup_opportunities"): "browser_tool",
}
```

Generalize candidate binding validation: portal and opportunity research handoffs bind an aggregation/opportunity run but no job candidate. Keep form-review handoffs candidate-bound. Add a `resource_lock` field; both portal kinds require `authenticated_browser`, which permits only one `awaiting_response` mission to be active at a time.

Mission events:

```text
mission/queued
mission/browser_attached
mission/auth_ready | mission/auth_required
mission/query_applied
mission/page_observed
mission/candidate_observed
mission/external_link_resolved
mission/checkpoint
mission/response_written
mission/validation_complete | mission/error | mission/budget_exhausted
```

Events may include portal, safe hostname, navigation count, result count, elapsed time, and error class. They must not include query text, job descriptions, applicant data, browser selectors, cookies, or chain-of-thought. A running pilot writes a checkpoint at least every five seconds so `aggregate-status --watch` can distinguish active, stale, blocked, and awaiting-user-takeover states.

- [ ] **Step 4: Write failing immutable-revision tests**

Assert:

1. Fast-lane completion publishes revision 1 while portal missions are still queued.
2. A validated Handshake response publishes revision 2 with `parent_sha256` equal to revision 1's digest.
3. A validated Runway response publishes revision 3 and cannot mutate revisions 1 or 2.
4. Re-importing the same response is idempotent and does not create revision 4.
5. Two browser missions cannot simultaneously hold the `authenticated_browser` resource lock.
6. `prepare --aggregation-snapshot agg-1@2` copies revision 2 and remains bound to it even if revision 3 later appears.
7. An open run's unversioned “latest” pointer is rejected by `prepare`.

- [ ] **Step 5: Add snapshot revision persistence**

Add table:

```sql
CREATE TABLE aggregation_snapshots (
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    parent_sha256 TEXT NOT NULL DEFAULT '',
    observation_high_watermark INTEGER NOT NULL,
    candidate_count INTEGER NOT NULL,
    advanceable_count INTEGER NOT NULL,
    reason TEXT NOT NULL,
    snapshot_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, revision),
    UNIQUE (sha256)
);
```

Snapshot publication uses one transaction to read a committed observation high-water mark, render sorted candidates, write a temporary mode-`0600` file, `fsync`, atomically rename, then insert the revision row. Do not overwrite a prior path. Include `revision`, `parent_sha256`, `observation_high_watermark`, `advanceable_count`, `pending_enrichment`, and `sha256` in `applypilot.aggregation-snapshot.v2`.

When a portal-only observation later resolves to a first-party URL, persist a canonical-key alias and re-key transaction rather than fuzzy-merging title/company text. No automatic fuzzy merge may collapse two distinct openings.

- [ ] **Step 6: Bind exact revisions into the canonical workflow**

Replace every unversioned aggregation-run selector and lookup with:

```bash
applypilot prepare --aggregation-snapshot RUN_ID@REVISION
```

The resolver accepts only safe run IDs and positive integer revisions, retrieves the recorded path, verifies the file digest and parent chain, copies it into the autonomy run, and records both `aggregation_snapshot_revision` and `aggregation_snapshot_sha256` in immutable artifacts. Only `advanceable=True` candidates enter the workflow. Portal-only and board-only observations remain visible in aggregation status with `needs_first_party_verification`.

- [ ] **Step 7: Run focused validation and commit**

```bash
pytest tests/test_portal_discovery_handoff.py tests/test_aggregation_revisions.py tests/test_aggregation_store.py tests/test_aggregation_workflow.py tests/test_semantic_discovery_handoff.py tests/test_supervisor.py -q
ruff check src/applypilot/aggregation/portal_handoff.py src/applypilot/aggregation/store.py src/applypilot/aggregation/snapshot.py src/applypilot/autonomy/handoff.py tests/test_portal_discovery_handoff.py tests/test_aggregation_revisions.py
```

Expected: PASS; browser behavior is validated with signed fixture artifacts, not live portal traffic.

```bash
git add src/applypilot/aggregation/portal_handoff.py src/applypilot/aggregation/store.py src/applypilot/aggregation/orchestrator.py src/applypilot/aggregation/snapshot.py src/applypilot/autonomy/handoff.py src/applypilot/cli.py tests/test_portal_discovery_handoff.py tests/test_aggregation_revisions.py tests/test_aggregation_workflow.py
git commit -m "feat: add browser portal enrichment revisions"
```

### Task 13: Add a distinct startup opportunity-intelligence ledger

**Files:**
- Create: `src/applypilot/opportunities/__init__.py`
- Create: `src/applypilot/opportunities/models.py`
- Create: `src/applypilot/opportunities/store.py`
- Create: `src/applypilot/opportunities/research.py`
- Create: `tests/test_opportunity_research.py`
- Modify: `src/applypilot/aggregation/portal_handoff.py`
- Modify: `src/applypilot/autonomy/handoff.py`
- Modify: `src/applypilot/cli.py`

- [ ] **Step 1: Write failing truth-boundary tests**

```python
def test_company_signal_is_not_a_job_candidate():
    lead = opportunity_lead(signal_type="recent_funding")
    assert isinstance(lead, OpportunityLead)
    assert not isinstance(lead, RoleCandidate)
    assert lead.posted_job_url == ""
    assert lead.route is OpportunityRoute.SPECULATIVE_OUTREACH


def test_form_d_alone_cannot_claim_company_raised():
    lead = opportunity_lead(evidence=[form_d_evidence()])
    decision = verify_opportunity(lead, now=NOW)
    assert decision.status is OpportunityStatus.NEEDS_CORROBORATION
    assert "completed_raise_not_verified" in decision.reasons


def test_recent_raise_requires_dated_primary_and_independent_evidence():
    lead = opportunity_lead(evidence=[company_announcement(), independent_report()])
    decision = verify_opportunity(lead, now=NOW)
    assert decision.status is OpportunityStatus.VERIFIED
    assert decision.signal_date >= NOW.date() - timedelta(days=45)


def test_hiring_signal_requires_current_careers_evidence():
    lead = opportunity_lead(
        signal_type="actively_hiring",
        evidence=[current_careers_page(open_role_count=3)],
    )
    decision = verify_opportunity(lead, now=NOW)
    assert decision.status is OpportunityStatus.VERIFIED
```

Also assert that unknown funding amount/stage remains `None`, a hiring directory flag alone requests careers-page verification, stale evidence is not “recent,” company-domain conflicts block promotion, and a verified real posting is the only path that creates a `RoleCandidate`.

- [ ] **Step 2: Define company-level contracts**

```python
class OpportunitySignal(StrEnum):
    RECENT_FUNDING = "recent_funding"
    FINANCING_NOTICE = "financing_notice"
    ACTIVELY_HIRING = "actively_hiring"
    GENERAL_GROWTH = "general_growth"


class OpportunityRoute(StrEnum):
    POSTED_JOB = "posted_job"
    SPECULATIVE_OUTREACH = "speculative_outreach"


class OpportunityStatus(StrEnum):
    OBSERVED = "observed"
    NEEDS_CORROBORATION = "needs_corroboration"
    VERIFIED = "verified"
    REJECTED = "rejected"
    DRAFT_READY = "draft_ready"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    AUTHORIZED = "authorized"
    SEND_ATTEMPTED = "send_attempted"
    SENT = "sent"
    DELIVERED = "delivered"
    REPLIED = "replied"


@dataclass(frozen=True)
class OpportunityEvidence:
    evidence_type: str
    source_url: str
    source_title: str
    publisher: str
    observed_at: str
    event_date: str = ""
    is_primary: bool = False
    claim: str = ""


@dataclass(frozen=True)
class OpportunityLead:
    lead_id: str
    company_name: str
    company_url: str
    company_domain: str
    signal: OpportunitySignal
    route: OpportunityRoute
    status: OpportunityStatus
    evidence: tuple[OpportunityEvidence, ...]
    signal_date: str = ""
    funding_stage: str | None = None
    funding_amount: str | None = None
    careers_url: str = ""
    posted_job_url: str = ""
    open_role_count: int | None = None
    fit_hypothesis: str = ""
    contact_route: str = ""
```

Persist leads, evidence, status transitions, research artifacts, and draft/send bindings in `opportunities.sqlite3`. This ledger is separate from `aggregation.sqlite3` and `workflow.sqlite3` because a company-level opening hypothesis has different truth and completion states than a posted role.

- [ ] **Step 3: Add a bounded model-browser research mission**

`applypilot opportunities discover` creates an `opportunity_research/startup_opportunities` browser handoff with:

- target signals: recently funded, actively hiring, or both;
- a default 45-day recency window for funding;
- user role/location/industry preferences from an immutable, privacy-minimized context digest;
- a maximum of 25 company leads, 60 navigations, and 300 elapsed seconds;
- permitted public research surfaces and a requirement to open the company's official site/careers page before returning a verified lead.

MVP source strategy:

1. Search the public web for recent funding announcements and active-hiring directories.
2. Corroborate “raised” with a dated company or investor announcement plus an independent credible source. A media report without primary confirmation remains `needs_corroboration`.
3. Classify SEC Form D as `financing_notice`, not a completed raise; the SEC states that the data is as-filed, quarterly, and not guaranteed accurate or complete.
4. Use YC's public company/jobs/hiring surfaces as discovery evidence, then verify the company's current careers page or first-party ATS.
5. Treat Wellfound, LinkedIn, Handshake, Runway, Crunchbase, PitchBook, and similar account/licensed portals as browser-mediated or API-authorized sources according to their own capability policies. Do not add hidden endpoint clients.

The response returns structured claims and URLs, not copied articles or page dumps. The validator canonicalizes domains, deduplicates companies, enforces the result cap, and records `mission/*` telemetry shared with portal discovery.

- [ ] **Step 4: Implement deterministic verification and scoring**

Verification is code, not model judgment alone:

```python
def verify_opportunity(lead: OpportunityLead, *, now: datetime, recent_days: int = 45):
    if lead.signal is OpportunitySignal.RECENT_FUNDING:
        primary = [item for item in lead.evidence if item.is_primary and item.event_date]
        independent = {
            (item.publisher.lower(), canonicalize_url(item.source_url))
            for item in lead.evidence
            if not item.is_primary and item.event_date
        }
        if not primary or not independent:
            return needs_corroboration("completed_raise_not_verified")
        if max(parse_date(item.event_date) for item in primary) < now.date() - timedelta(days=recent_days):
            return reject("funding_signal_stale")
    if lead.signal is OpportunitySignal.ACTIVELY_HIRING:
        if not verified_current_careers_evidence(lead.evidence, lead.company_domain):
            return needs_corroboration("current_hiring_not_verified")
    return verified()
```

Rank only verified leads using transparent components: profile-role fit, location fit, signal freshness, current hiring evidence, relevant team/contact route, and evidence completeness. Do not let funding amount dominate fit. Persist every component and reason code.

- [ ] **Step 5: Promote only real postings**

If research resolves a current official job URL, pass it through normal aggregation verification and create a job observation with opportunity provenance. Otherwise keep `route=speculative_outreach`; never synthesize titles such as “intern” or “analyst” from a general hiring signal.

- [ ] **Step 6: Add CLI read surfaces and commit**

```bash
applypilot opportunities discover --signal recently-funded --signal actively-hiring --recent-days 45 --watch
applypilot opportunities status OPPORTUNITY_RUN_ID --watch
applypilot opportunities list --status verified --limit 25 --json
applypilot opportunities show LEAD_ID --evidence
```

Run:

```bash
pytest tests/test_opportunity_research.py tests/test_portal_discovery_handoff.py tests/test_aggregation_models.py -q
ruff check src/applypilot/opportunities src/applypilot/aggregation/portal_handoff.py tests/test_opportunity_research.py
```

Expected: PASS using fixed company, announcement, SEC, YC, and careers-page fixtures; no live research or external writes occur.

```bash
git add src/applypilot/opportunities src/applypilot/aggregation/portal_handoff.py src/applypilot/autonomy/handoff.py src/applypilot/cli.py tests/test_opportunity_research.py tests/test_portal_discovery_handoff.py
git commit -m "feat: add verified startup opportunity research"
```

### Task 14: Add truthful speculative-outreach drafting and exact send authorization

**Files:**
- Create: `src/applypilot/opportunities/outreach.py`
- Create: `src/applypilot/opportunities/send_handoff.py`
- Create: `tests/test_opportunity_outreach.py`
- Modify: `src/applypilot/opportunities/models.py`
- Modify: `src/applypilot/opportunities/store.py`
- Modify: `src/applypilot/autonomy/handoff.py`
- Modify: `src/applypilot/cli.py`

- [ ] **Step 1: Write failing contact and draft truth tests**

```python
def test_unverified_or_guessed_contact_is_not_draftable():
    lead = verified_lead(contact_route="first.last@company.test", contact_evidence=())
    with pytest.raises(OutreachGateError, match="verified contact route required"):
        build_outreach_draft(lead, profile=PROFILE)


def test_draft_cannot_claim_an_opening_or_unverified_funding():
    draft = build_outreach_draft(
        verified_lead(route="speculative_outreach", funding_amount=None),
        profile=PROFILE,
    )
    assert "opening" not in draft.body.lower()
    assert "$" not in draft.body
    assert draft.intent == "inquiry"


def test_draft_is_not_a_send():
    draft = persist_draft()
    assert draft.status is OpportunityStatus.DRAFT_READY
    assert store.sent_count() == 0
```

Contact evidence must resolve to a public company contact page, company-domain mailbox, verified named-person source, or account-backed messaging profile. Never infer an email from a name pattern. Applicant facts come only from the bound fact snapshot; unknown availability, work authorization, GPA, experience, or portfolio claims remain unknown.

- [ ] **Step 2: Define draft and one-time authorization contracts**

```python
@dataclass(frozen=True)
class OutreachDraft:
    draft_id: str
    lead_id: str
    channel: str
    sender: str
    recipient: str
    subject: str
    body: str
    attachment_digests: tuple[str, ...]
    lead_evidence_digest: str
    fact_snapshot_digest: str
    created_at: str
    sha256: str


@dataclass(frozen=True)
class OutreachAuthorization:
    authorization_id: str
    action: str
    sender: str
    items: tuple[tuple[str, str, str], ...]  # lead_id, draft_id, draft_sha256
    channel: str
    issued_at: str
    expires_at: str
    nonce: str
    sha256: str
```

Use schema `applypilot-outreach-authorization-v1`. A grant binds one channel, one sender, and 1–10 exact lead/draft/digest tuples, expires after 30 minutes by default, and is consumed once. A changed body, recipient, attachment, sender, channel, or evidence/fact digest invalidates it. Store authorization and consumption artifacts mode `0600`, following the existing one-time submission-authorization pattern without reusing a job-submission grant.

For new outbound email workflows the default sender is `sybatx@gmail.com`; an existing thread or explicit user instruction may select another authenticated address. Draft generation itself does not connect to or mutate any mailbox.

- [ ] **Step 3: Generate bounded, evidence-linked drafts**

The model receives only the verified lead summary, allowed profile facts, and channel limits. Required response fields: subject, body, cited lead evidence IDs, cited profile fact IDs, intent, and unsupported-claim list. The deterministic validator rejects:

- unsupported funding dates, amounts, stages, hiring claims, mutual connections, referrals, or job openings;
- applicant-authored claims absent from the fact snapshot;
- mass-mail language, manipulative urgency, or a request framed as an existing application;
- email bodies above 180 words or LinkedIn notes above the configured platform limit;
- missing respectful close where appropriate;
- contact routes without evidence.

Suggested structure is concise: why this company based on verified evidence, the user's relevant capability, one concrete way they could help or learn, and a low-friction inquiry about internships, short-term projects, or early-career opportunities. It must be clear when no formal role is known.

- [ ] **Step 4: Write failing authorization and receipt tests**

Assert:

1. No sender adapter can be called without an unexpired, canonical, unconsumed authorization.
2. An authorization for draft A cannot send draft B or a modified draft A.
3. Authorization is consumed atomically before the irreversible provider call; an ambiguous provider timeout becomes `send_state_unknown` and is never retried automatically.
4. A provider message ID proves provider acceptance/send creation, not delivery or reply.
5. A browser contact-form confirmation proves form submission only when the confirmation is captured and bound to the exact lead/draft.
6. Draft, queued, attempted, provider-accepted, submitted, delivered, bounced, and replied remain distinct states.
7. A partial ten-item batch records per-item receipts and never marks unsent siblings complete.

- [ ] **Step 5: Implement a portable send handoff**

`applypilot opportunities send` does not embed mailbox credentials. It validates and consumes the authorization, then creates a typed `outreach_send` handoff for an available authenticated email connector or serialized browser mission. The worker returns:

```json
{
  "schema_version": "applypilot.outreach-send-response.v1",
  "authorization_sha256": "...",
  "lead_id": "...",
  "draft_sha256": "...",
  "channel": "email",
  "status": "provider_accepted",
  "provider_receipt_id": "...",
  "observed_at": "..."
}
```

Persist a hash of a provider receipt when raw IDs are sensitive. On timeout after the send boundary, stop with `send_state_unknown` and inspect the provider/thread before any retry. Do not create an automatic follow-up schedule in MVP; reminders or follow-ups require a separately authorized workflow.

Safe telemetry phases:

```text
outreach/draft_started
outreach/draft_validated
outreach/awaiting_authorization
outreach/authorization_validated
outreach/send_handoff_queued
outreach/pre_send_validated
outreach/send_attempted
outreach/provider_accepted | outreach/submitted | outreach/send_state_unknown | outreach/error
```

Do not log recipient addresses, subjects, bodies, attachments, browser selectors, or message IDs in the general NDJSON event stream.

- [ ] **Step 6: Add explicit CLI boundaries**

```bash
applypilot opportunities draft LEAD_ID --channel email
applypilot opportunities review-draft DRAFT_ID

# Run only after the user explicitly approves these exact items.
applypilot opportunities authorize-outreach \
  --sender sybatx@gmail.com \
  --channel email \
  --item LEAD_ID:DRAFT_ID:DRAFT_SHA256

applypilot opportunities send \
  --authorization /absolute/path/to/outreach-authorization.json
```

The authorization command prints the exact sender, recipient display, lead, subject, attachment names, body digest, and expiry before creating the grant. Interactive confirmation is not a substitute for the user's explicit instruction; if the calling agent has not received it for the exact batch, stop at `DRAFT_READY`.

- [ ] **Step 7: Run focused validation and commit**

```bash
pytest tests/test_opportunity_outreach.py tests/test_opportunity_research.py tests/test_submission.py tests/test_campaign.py -q
ruff check src/applypilot/opportunities tests/test_opportunity_outreach.py
```

Expected: PASS using fake send connectors and fixed receipt artifacts. No email, LinkedIn message, contact form, or other external communication is sent by tests.

```bash
git add src/applypilot/opportunities src/applypilot/autonomy/handoff.py src/applypilot/cli.py tests/test_opportunity_outreach.py
git commit -m "feat: gate startup opportunity outreach"
```

### Task 15: Extend latency and telemetry acceptance tests across all lanes

**Files:**
- Modify: `scripts/benchmark_aggregation.py`
- Create: `tests/test_aggregation_end_to_end.py`
- Modify: `docs/CANONICAL_WORKFLOW.md`
- Modify: `README.md`

- [ ] **Step 1: Add a deterministic multi-lane scenario**

The fixture run starts cache at 50 ms, direct ATS at 250 ms, Workday at 500 ms, JobSpy at 2 seconds, Handshake at 4 seconds, and Runway at 6 seconds. Assert:

- first candidate under 1 second;
- revision 1 under 3 fixture seconds and independent of enrichment completion;
- later responses publish a monotonic digest-linked revision chain;
- the status projection shows source and mission progress throughout;
- exact revision binding remains stable;
- JobSpy/portal-only candidates do not advance before first-party verification;
- startup leads remain outside the job snapshot;
- no outreach send occurs without a test authorization.

- [ ] **Step 2: Add operator-level telemetry assertions**

For a running fixture, `aggregate-status --watch --json` must expose:

```json
{
  "run_id": "agg-1",
  "fast_snapshot": {"revision": 1, "status": "ready", "elapsed_ms": 500},
  "sources": {"cache": "complete", "direct_ats": "complete", "jobspy": "running"},
  "browser_queue": {
    "active": "handshake",
    "queued": ["runway"],
    "last_checkpoint_age_seconds": 2,
    "state": "page_observed"
  },
  "latest_snapshot": {"revision": 1, "candidate_count": 20},
  "opportunities": {"observed": 0, "verified": 0, "draft_ready": 0, "sent": 0}
}
```

Field values are examples; tests assert schema, monotonicity, and privacy rather than wall-clock exactness. Human-readable Rich output goes to stderr and machine JSON to stdout.

- [ ] **Step 3: Run the final focused gate**

```bash
python3 scripts/benchmark_aggregation.py
pytest tests/test_observability_events.py tests/test_aggregation_models.py tests/test_aggregation_store.py tests/test_aggregation_sources.py tests/test_aggregation_orchestrator.py tests/test_aggregation_jobspy.py tests/test_portal_discovery_handoff.py tests/test_aggregation_revisions.py tests/test_aggregation_cli.py tests/test_aggregation_workflow.py tests/test_opportunity_research.py tests/test_opportunity_outreach.py tests/test_aggregation_end_to_end.py tests/test_semantic_discovery_handoff.py tests/test_workflow.py -q
ruff check src/applypilot/observability src/applypilot/aggregation src/applypilot/opportunities src/applypilot/autonomy/handoff.py src/applypilot/autonomy/telemetry.py src/applypilot/autonomy/runner.py src/applypilot/cli.py tests/test_aggregation_jobspy.py tests/test_portal_discovery_handoff.py tests/test_aggregation_revisions.py tests/test_opportunity_research.py tests/test_opportunity_outreach.py tests/test_aggregation_end_to_end.py scripts/benchmark_aggregation.py
git diff --check
```

Expected: benchmark `pass=true`; focused tests pass; Ruff and diff checks are clean. Skip the full product suite unless these tests expose a shared-contract regression or the branch is entering a merge/release gate.

- [ ] **Step 4: Commit final docs and acceptance gate**

```bash
git add scripts/benchmark_aggregation.py tests/test_aggregation_end_to_end.py docs/CANONICAL_WORKFLOW.md README.md
git commit -m "test: verify progressive aggregation and outreach gates"
```

## Release gates and stop conditions

- Stop a Handshake or Runway mission if it requires hidden API discovery, session-cookie export, bulk DOM extraction, CAPTCHA/MFA bypass, provider-challenge evasion, or navigation beyond the mission allowlist. Record `auth_required`, `provider_authorization_required`, or `blocked` without discarding fast-lane results.
- Stop JobSpy enrichment on a per-board 429/block signal or elapsed budget. Do not rotate proxies, spoof identity, retry automatically, or let a stopped worker delay revision 1. Preserve the board/unit event and continue other source families.
- Stop startup promotion when company identity, event date, completed-funding claim, current-hiring evidence, first-party posting, or contact route cannot be verified. Unknown evidence remains unknown; it is not permission to infer a role, funding amount, or email address.
- Stop outreach at `DRAFT_READY` unless the user has authorized the exact sender, channel, recipient/lead set, draft digests, attachments, and expiry. After an ambiguous provider timeout, inspect the existing provider state before any retry.
- Stop after two benchmark/debug cycles with no material change in first-result time, deadline behavior, or provider yield. Return the event stream, snapshot, command, and next smallest experiment.
- Do not migrate application state into `aggregation.sqlite3`; `workflow.sqlite3` remains authoritative.
- Do not merge `opportunities.sqlite3` into the job workflow. Only a verified real posting may be promoted; speculative outreach retains its own status and receipts.
- Do not enable deep mode by default until three real runs show useful incremental yield over quick mode and no provider rate-limit regression.
- Do not remove legacy web discovery until one compatibility release passes and an exact-revision snapshot produces a valid shortlist from at least three first-party source families.
- Do not claim that a portal model's visible-page interaction is provider-approved bulk automation. Keep the mission bounded to the user's authenticated session and documented interface; revisit the connector if provider rules or access mechanisms change.

## Definition of done

Quick aggregation publishes a durable and observable revision 1 without waiting for slower sources; duplicate postings merge with full provenance; bounded JobSpy workers and serialized Handshake/Runway browser missions publish later immutable revisions; one failed or timed-out source cannot erase successful results; canonical `prepare` binds one exact revision without a discovery model call; JobSpy/portal-only observations cannot advance without first-party verification; startup leads remain distinct from jobs; speculative outreach is truthful, evidence-bound, and unsent without exact authorization; focused tests, Ruff, benchmark, and diff checks pass; and operator documentation distinguishes discovery, enrichment, model/browser work, verification, drafting, authorization, provider acceptance, delivery, reply, application, and submission.

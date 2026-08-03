"""Bounded, killable JobSpy enrichment units."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Any
from urllib.parse import urlsplit

from applypilot.aggregation.models import (
    AggregationRequest,
    RawJob,
    SourceCapability,
    SourceKind,
)
from applypilot.observability.events import EventJournal

JOBSPY_REQUEST_SCHEMA = "applypilot.jobspy-request.v1"
JOBSPY_RESPONSE_SCHEMA = "applypilot.jobspy-response.v1"
_ALLOWED_BOARDS = frozenset({"indeed", "google", "zip_recruiter", "linkedin", "glassdoor"})
_BOARD_HOSTS = (
    "indeed.com",
    "google.com",
    "ziprecruiter.com",
    "linkedin.com",
    "glassdoor.com",
)
_SAFE_ERROR = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,119}$")


class JobSpyContractError(ValueError):
    """Raised when a worker artifact is not bound to its exact request."""


@dataclass(frozen=True)
class JobSpySettings:
    boards: tuple[str, ...] = ("indeed", "google", "zip_recruiter")
    max_query_terms: int = 2
    max_locations: int = 2
    results_per_board: int = 25
    max_workers: int = 3
    deadline_seconds: float = 30.0
    hours_old: int = 168
    country_indeed: str = "usa"

    def validate(self) -> None:
        if not self.boards or len(set(self.boards)) != len(self.boards):
            raise ValueError("JobSpy board allowlist must be unique and non-empty")
        if not set(self.boards) <= _ALLOWED_BOARDS:
            raise ValueError("JobSpy board allowlist contains an unsupported board")
        if not 1 <= self.max_query_terms <= 4 or not 1 <= self.max_locations <= 4:
            raise ValueError("JobSpy query and location caps must be between 1 and 4")
        if not 1 <= self.results_per_board <= 50 or not 1 <= self.max_workers <= 4:
            raise ValueError("JobSpy result and worker caps are out of bounds")
        if not 0 < self.deadline_seconds <= 120:
            raise ValueError("JobSpy deadline must be between 0 and 120 seconds")
        if not 1 <= self.hours_old <= 24 * 30:
            raise ValueError("JobSpy age window must be between 1 hour and 30 days")
        if not self.country_indeed.strip() or len(self.country_indeed) > 40:
            raise ValueError("JobSpy country value is invalid")


@dataclass(frozen=True)
class JobSpyUnit:
    board: str
    term: str
    location: str
    results_wanted: int
    hours_old: int = 168
    country_indeed: str = "usa"
    proxies: tuple[str, ...] = ()
    linkedin_fetch_description: bool = False

    def _unsigned_request(self) -> dict[str, Any]:
        google_search_term = ""
        if self.board == "google":
            suffix = f" near {self.location}" if self.location else ""
            google_search_term = f"{self.term} jobs{suffix}"
        return {
            "schema_version": JOBSPY_REQUEST_SCHEMA,
            "board": self.board,
            "term": self.term,
            "location": self.location,
            "results_wanted": self.results_wanted,
            "hours_old": self.hours_old,
            "country_indeed": self.country_indeed,
            "google_search_term": google_search_term,
            "linkedin_fetch_description": False,
        }

    @property
    def request_digest(self) -> str:
        encoded = json.dumps(
            self._unsigned_request(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def unit_id(self) -> str:
        return f"jobspy:{self.board}:{self.request_digest[:16]}"

    def to_request(self) -> dict[str, Any]:
        return {
            **self._unsigned_request(),
            "unit_id": self.unit_id,
            "request_digest": self.request_digest,
        }


@dataclass(frozen=True)
class JobSpyUnitResult:
    unit_id: str
    board: str
    status: str
    rows: tuple[dict[str, Any], ...] = ()
    error_class: str = ""
    stderr_excerpt: str = ""


CommandBuilder = Callable[[Path, Path], Sequence[str]]


def _unique_bounded(values: Sequence[str], limit: int, *, allow_empty: bool) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if not normalized and not allow_empty:
            continue
        if len(normalized) > 200 or normalized in result:
            continue
        result.append(normalized)
        if len(result) >= limit:
            break
    return tuple(result)


def build_jobspy_units(
    *,
    terms: Sequence[str],
    locations: Sequence[str],
    settings: JobSpySettings | None = None,
) -> tuple[JobSpyUnit, ...]:
    settings = settings or JobSpySettings()
    settings.validate()
    bounded_terms = _unique_bounded(terms, settings.max_query_terms, allow_empty=False)
    if not bounded_terms:
        raise ValueError("JobSpy requires at least one bounded query term")
    bounded_locations = _unique_bounded(
        locations or ("",), settings.max_locations, allow_empty=True
    )
    if not bounded_locations:
        bounded_locations = ("",)
    return tuple(
        JobSpyUnit(
            board=board,
            term=term,
            location=location,
            results_wanted=settings.results_per_board,
            hours_old=settings.hours_old,
            country_indeed=settings.country_indeed,
        )
        for board in settings.boards
        for term in bounded_terms
        for location in bounded_locations
    )


def validate_jobspy_request(payload: dict[str, Any]) -> JobSpyUnit:
    allowed = {
        "schema_version",
        "request_digest",
        "unit_id",
        "board",
        "term",
        "location",
        "results_wanted",
        "hours_old",
        "country_indeed",
        "google_search_term",
        "linkedin_fetch_description",
    }
    if set(payload) != allowed or payload.get("schema_version") != JOBSPY_REQUEST_SCHEMA:
        raise JobSpyContractError("unsupported JobSpy request schema")
    if payload.get("board") not in _ALLOWED_BOARDS:
        raise JobSpyContractError("JobSpy request board is unsupported")
    if payload.get("linkedin_fetch_description") is not False:
        raise JobSpyContractError("JobSpy full-description fetching is disabled")
    unit = JobSpyUnit(
        board=str(payload["board"]),
        term=str(payload["term"]),
        location=str(payload["location"]),
        results_wanted=int(payload["results_wanted"]),
        hours_old=int(payload["hours_old"]),
        country_indeed=str(payload["country_indeed"]),
    )
    if not 1 <= unit.results_wanted <= 50 or not 1 <= unit.hours_old <= 24 * 30:
        raise JobSpyContractError("JobSpy request bounds are invalid")
    if len(unit.term) > 200 or len(unit.location) > 200 or not unit.term.strip():
        raise JobSpyContractError("JobSpy request terms are invalid")
    if payload.get("request_digest") != unit.request_digest:
        raise JobSpyContractError("JobSpy request digest mismatch")
    if payload.get("unit_id") != unit.unit_id:
        raise JobSpyContractError("JobSpy request unit mismatch")
    if payload.get("google_search_term") != unit._unsigned_request()["google_search_term"]:
        raise JobSpyContractError("JobSpy Google search term mismatch")
    return unit


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink():
        raise JobSpyContractError("JobSpy artifact must not be a symbolic link")
    flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("JobSpy artifact write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _scrubbed_worker_environment() -> dict[str, str]:
    return {
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
    }


def _default_command(request_path: Path, response_path: Path) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "applypilot.aggregation.jobspy_worker",
        "--request",
        str(request_path),
        "--response",
        str(response_path),
    )


async def _drain_bounded(stream: asyncio.StreamReader | None, limit: int = 8192) -> str:
    if stream is None:
        return ""
    retained = bytearray()
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        if len(retained) < limit:
            retained.extend(chunk[: limit - len(retained)])
    return retained.decode("utf-8", errors="replace")


async def _terminate_worker(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
        return
    except TimeoutError:
        pass
    if process.returncode is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return
        await process.wait()


def _validate_response(path: Path, unit: JobSpyUnit) -> JobSpyUnitResult:
    if path.is_symlink() or not path.is_file():
        raise JobSpyContractError("JobSpy worker response is missing")
    if path.stat().st_size > 2 * 1024 * 1024:
        raise JobSpyContractError("JobSpy worker response is too large")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != JOBSPY_RESPONSE_SCHEMA:
        raise JobSpyContractError("unsupported JobSpy response schema")
    if payload.get("request_digest") != unit.request_digest:
        raise JobSpyContractError("JobSpy response request digest mismatch")
    if payload.get("unit_id") != unit.unit_id or payload.get("board") != unit.board:
        raise JobSpyContractError("JobSpy response unit binding mismatch")
    status = str(payload.get("status") or "")
    if status not in {"complete", "empty", "error"}:
        raise JobSpyContractError("JobSpy response status is invalid")
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) > unit.results_wanted:
        raise JobSpyContractError("JobSpy response result count exceeds request")
    if any(not isinstance(row, dict) for row in rows):
        raise JobSpyContractError("JobSpy response rows must be objects")
    error_class = str(payload.get("error_class") or "")
    if error_class and not _SAFE_ERROR.fullmatch(error_class):
        raise JobSpyContractError("JobSpy response error class is invalid")
    return JobSpyUnitResult(
        unit_id=unit.unit_id,
        board=unit.board,
        status=status,
        rows=tuple(rows),
        error_class=error_class,
    )


async def run_jobspy_unit(
    unit: JobSpyUnit,
    *,
    work_dir: Path,
    timeout_seconds: float,
    command_builder: CommandBuilder | None = None,
) -> JobSpyUnitResult:
    if timeout_seconds <= 0:
        raise ValueError("JobSpy unit timeout must be positive")
    work_dir = work_dir.resolve()
    if work_dir.is_symlink():
        raise JobSpyContractError("JobSpy work directory must not be a symbolic link")
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(work_dir, 0o700)
    request_path = work_dir / "request.json"
    response_path = work_dir / "response.json"
    if response_path.is_symlink():
        raise JobSpyContractError("JobSpy response path must not be a symbolic link")
    response_path.unlink(missing_ok=True)
    _write_private_json(request_path, unit.to_request())
    command = tuple((command_builder or _default_command)(request_path, response_path))
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("JobSpy worker command is invalid")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=_scrubbed_worker_environment(),
        start_new_session=True,
    )
    stderr_task = asyncio.create_task(_drain_bounded(process.stderr))
    try:
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except TimeoutError:
            await _terminate_worker(process)
            stderr_excerpt = await stderr_task
            return JobSpyUnitResult(
                unit_id=unit.unit_id,
                board=unit.board,
                status="timed_out",
                error_class="TimeoutError",
                stderr_excerpt=stderr_excerpt,
            )
    except asyncio.CancelledError:
        await _terminate_worker(process)
        await asyncio.gather(stderr_task, return_exceptions=True)
        raise
    stderr_excerpt = await stderr_task
    if process.returncode != 0:
        return JobSpyUnitResult(
            unit_id=unit.unit_id,
            board=unit.board,
            status="error",
            error_class="WorkerExit",
            stderr_excerpt=stderr_excerpt,
        )
    result = _validate_response(response_path, unit)
    return JobSpyUnitResult(
        **{**result.__dict__, "stderr_excerpt": stderr_excerpt},
    )


def _is_board_url(value: str) -> bool:
    host = (urlsplit(value).hostname or "").lower()
    return any(host == board or host.endswith(f".{board}") for board in _BOARD_HOSTS)


def _string(value: Any) -> str:
    text = str(value or "")
    return "" if text.lower() in {"nan", "nat", "none"} else text


def _salary(row: dict[str, Any]) -> str:
    minimum = _string(row.get("min_amount"))
    maximum = _string(row.get("max_amount"))
    currency = _string(row.get("currency"))
    interval = _string(row.get("interval"))
    if not minimum:
        return ""
    amount = f"{currency}{minimum}"
    if maximum:
        amount += f"-{currency}{maximum}"
    return f"{amount}/{interval}" if interval else amount


def normalize_jobspy_row(row: dict[str, Any], *, board: str) -> RawJob:
    discovery_url = _string(row.get("job_url")).strip()
    if not discovery_url.startswith(("https://", "http://")):
        raise JobSpyContractError("JobSpy row is missing a board URL")
    direct_url = _string(row.get("job_url_direct")).strip()
    official_url = (
        direct_url
        if direct_url.startswith(("https://", "http://")) and not _is_board_url(direct_url)
        else ""
    )
    source_job_id = _string(row.get("id")).strip() or hashlib.sha256(
        discovery_url.encode("utf-8")
    ).hexdigest()[:20]
    location = _string(row.get("location")).strip()
    if row.get("is_remote") is True and "remote" not in location.lower():
        location = f"{location} (Remote)".strip()
    return RawJob(
        source=SourceKind.JOBSPY,
        source_job_id=source_job_id,
        title=_string(row.get("title")).strip(),
        company=_string(row.get("company")).strip() or "Unknown employer",
        location=location,
        official_url=official_url,
        application_url=official_url or discovery_url,
        discovery_url=discovery_url,
        description=_string(row.get("description"))[:20_000],
        observed_at=datetime.now(timezone.utc),
        salary=_salary(row)[:300],
        posted_at=_string(row.get("date_posted") or row.get("posted_at"))[:80],
        metadata={"board": board},
    )


class JobSpySource:
    """Execute bounded units concurrently within one enrichment deadline."""

    kind = SourceKind.JOBSPY
    capability = SourceCapability.BOUNDED_AGGREGATOR

    def __init__(
        self,
        *,
        work_dir: Path,
        settings: JobSpySettings | None = None,
        journal: EventJournal | None = None,
        command_builder: CommandBuilder | None = None,
    ) -> None:
        self.work_dir = work_dir.resolve()
        self.settings = settings or JobSpySettings()
        self.settings.validate()
        self.journal = journal
        self.command_builder = command_builder
        self.errors: list[dict[str, str]] = []

    def _event(self, unit: JobSpyUnit, status: str, *, count: int = 0, error_class: str = "") -> None:
        if self.journal is None:
            return
        self.journal.emit(
            component="aggregation",
            phase="jobspy_unit",
            status=status,
            source=unit.unit_id,
            counts={"observed": count},
            detail={
                "board": unit.board,
                "term_hash": hashlib.sha256(unit.term.encode()).hexdigest()[:12],
                "location_hash": hashlib.sha256(unit.location.encode()).hexdigest()[:12],
                **({"error_class": error_class} if error_class else {}),
            },
        )

    async def run_units(
        self, request: AggregationRequest
    ) -> AsyncIterator[tuple[JobSpyUnit, JobSpyUnitResult]]:
        units = build_jobspy_units(
            terms=request.query_terms,
            locations=request.locations,
            settings=self.settings,
        )
        semaphore = asyncio.Semaphore(self.settings.max_workers)
        started = time.monotonic()
        for unit in units:
            self._event(unit, "queued")

        async def execute(unit: JobSpyUnit) -> tuple[JobSpyUnit, JobSpyUnitResult]:
            async with semaphore:
                self._event(unit, "started")
                remaining = max(0.01, self.settings.deadline_seconds - (time.monotonic() - started))
                try:
                    result = await run_jobspy_unit(
                        unit,
                        work_dir=self.work_dir / unit.request_digest[:16],
                        timeout_seconds=remaining,
                        command_builder=self.command_builder,
                    )
                except Exception as exc:
                    result = JobSpyUnitResult(
                        unit_id=unit.unit_id,
                        board=unit.board,
                        status="error",
                        error_class=type(exc).__name__,
                    )
                return unit, result

        tasks = [asyncio.create_task(execute(unit)) for unit in units]
        try:
            async with asyncio.timeout(self.settings.deadline_seconds):
                for task in asyncio.as_completed(tasks):
                    unit, result = await task
                    if result.status not in {"complete", "empty"}:
                        error_class = result.error_class or "JobSpyWorkerError"
                        self.errors.append({"source": unit.unit_id, "error_class": error_class})
                        self._event(unit, result.status, error_class=error_class)
                    else:
                        self._event(unit, result.status, count=len(result.rows))
                    yield unit, result
        except TimeoutError:
            self.errors.append({"source": "jobspy", "error_class": "GlobalDeadline"})
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def search(self, request: AggregationRequest) -> AsyncIterator[RawJob]:
        emitted = 0
        async for unit, result in self.run_units(request):
            if result.status not in {"complete", "empty"}:
                continue
            for row in result.rows:
                yield normalize_jobspy_row(row, board=unit.board)
                emitted += 1
                if emitted >= request.limit:
                    return


async def enrich_jobspy_run(
    *,
    store: Any,
    journal: EventJournal,
    run_id: str,
    request: AggregationRequest,
    work_dir: Path,
    settings: JobSpySettings | None = None,
    command_builder: CommandBuilder | None = None,
) -> dict[str, Any]:
    """Append JobSpy unit evidence and publish one coalesced later revision."""
    from applypilot.aggregation.normalization import normalize_job

    settings = settings or JobSpySettings()
    units = build_jobspy_units(
        terms=request.query_terms,
        locations=request.locations,
        settings=settings,
    )
    current = store.snapshot(run_id)
    if any(row["source"] == SourceKind.JOBSPY.value for row in current["sources"]):
        raise ValueError("JobSpy enrichment already exists for this run")
    latest_revision = store.latest_revision(run_id)
    pending: tuple[str, ...] = ()
    if latest_revision:
        _, latest = store.get_snapshot(run_id, latest_revision)
        pending = tuple(
            str(item) for item in latest.get("pending_enrichment") or [] if item != "jobspy"
        )
    for unit in units:
        store.start_source(run_id, SourceKind.JOBSPY, source_id=unit.unit_id)
    source = JobSpySource(
        work_dir=work_dir,
        settings=settings,
        journal=journal,
        command_builder=command_builder,
    )
    try:
        async for unit, result in source.run_units(request):
            inserted = 0
            if result.status in {"complete", "empty"}:
                for row in result.rows:
                    observation = normalize_job(normalize_jobspy_row(row, board=unit.board))
                    inserted += int(
                        store.record_observation(
                            run_id, observation, source_id=unit.unit_id
                        )
                    )
                store.finish_source(
                    run_id, SourceKind.JOBSPY, status="complete", source_id=unit.unit_id
                )
            else:
                terminal = "timed_out" if result.status == "timed_out" else "failed"
                store.finish_source(
                    run_id,
                    SourceKind.JOBSPY,
                    status=terminal,
                    error_class=result.error_class or "JobSpyWorkerError",
                    source_id=unit.unit_id,
                )
            journal.emit(
                component="aggregation",
                phase="jobspy_persist",
                status="complete" if result.status in {"complete", "empty"} else "error",
                source=unit.unit_id,
                counts={"observed": inserted},
            )
    finally:
        rows = store.snapshot(run_id)["sources"]
        for row in rows:
            if row["source"] == SourceKind.JOBSPY.value and row["status"] == "running":
                store.finish_source(
                    run_id,
                    SourceKind.JOBSPY,
                    status="cancelled",
                    error_class="GlobalDeadline",
                    source_id=str(row["source_id"]),
                )
    rows = store.snapshot(run_id)["sources"]
    terminal = (
        "complete"
        if not pending and rows and all(row["status"] == "complete" for row in rows)
        else "partial"
    )
    store.complete_run(run_id, status=terminal)
    snapshot = store.publish_snapshot(
        run_id,
        reason="jobspy_enrichment",
        status=terminal,
        pending_enrichment=pending,
    )
    journal.emit(
        component="aggregation",
        phase="snapshot",
        status="published",
        counts={
            "revision": int(snapshot["revision"]),
            "candidates": int(snapshot["candidate_count"]),
            "observations": int(snapshot["observation_count"]),
        },
        detail={"reason_code": "jobspy_enrichment"},
    )
    return snapshot

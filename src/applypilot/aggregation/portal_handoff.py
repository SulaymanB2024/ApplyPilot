"""Typed, bounded Handshake and Runway browser mission artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from applypilot.aggregation.models import RawJob, SourceKind

PORTAL_REQUEST_SCHEMA = "applypilot.portal-mission.v1"
PORTAL_RESPONSE_SCHEMA = "applypilot.portal-mission-response.v1"
PORTAL_RESOURCE_LOCK = "authenticated_browser"


class PortalContractError(ValueError):
    """Raised when a portal mission crosses a provider or evidence boundary."""


class Portal(StrEnum):
    HANDSHAKE = "handshake"
    RUNWAY = "runway"


_PORTAL_HOSTS = {
    Portal.HANDSHAKE: ("app.joinhandshake.com", "utaustin.joinhandshake.com"),
    Portal.RUNWAY: ("app.joinrunway.io",),
}


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _host(value: str) -> str:
    return (urlsplit(value).hostname or "").lower()


def _http_url(value: str, *, field_name: str, allow_empty: bool = False) -> str:
    normalized = value.strip()
    if allow_empty and not normalized:
        return ""
    if not normalized.startswith(("https://", "http://")) or not _host(normalized):
        raise PortalContractError(f"portal {field_name} must be an HTTP URL")
    if len(normalized) > 2_000:
        raise PortalContractError(f"portal {field_name} is too long")
    return normalized


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
    checkpoint_seconds: int = 5

    def __post_init__(self) -> None:
        if not self.run_id or len(self.run_id) > 120:
            raise PortalContractError("portal run id is invalid")
        if not 1 <= len(self.query_terms) <= 4:
            raise PortalContractError("portal query-term budget is invalid")
        if len(self.locations) > 4:
            raise PortalContractError("portal location budget is invalid")
        if any(not value.strip() or len(value) > 200 for value in self.query_terms):
            raise PortalContractError("portal query term is invalid")
        if any(not value.strip() or len(value) > 200 for value in self.locations):
            raise PortalContractError("portal location is invalid")
        start_url = _http_url(self.start_url, field_name="start URL")
        if _host(start_url) not in self.permitted_hosts:
            raise PortalContractError("portal start URL host is not permitted")
        if not 1 <= self.max_results <= 25:
            raise PortalContractError("portal result budget is invalid")
        if not 1 <= self.max_navigations <= 30:
            raise PortalContractError("portal navigation budget is invalid")
        if not 1 <= self.max_seconds <= 180:
            raise PortalContractError("portal elapsed-time budget is invalid")
        if self.checkpoint_seconds != 5:
            raise PortalContractError("portal checkpoint interval must be five seconds")

    @property
    def permitted_hosts(self) -> tuple[str, ...]:
        return _PORTAL_HOSTS[self.portal]

    @property
    def query_digest(self) -> str:
        return _digest(
            {
                "query_terms": list(self.query_terms),
                "locations": list(self.locations),
            }
        )

    def _unsigned(self) -> dict[str, Any]:
        return {
            "schema_version": PORTAL_REQUEST_SCHEMA,
            "run_id": self.run_id,
            "portal": self.portal.value,
            "start_url": self.start_url,
            "query_terms": list(self.query_terms),
            "locations": list(self.locations),
            "permitted_hosts": list(self.permitted_hosts),
            "max_results": self.max_results,
            "max_navigations": self.max_navigations,
            "max_seconds": self.max_seconds,
            "checkpoint_seconds": self.checkpoint_seconds,
            "query_digest": self.query_digest,
            "resource_lock": PORTAL_RESOURCE_LOCK,
            "interaction_mode": "visible_page_controls",
            "auth_takeover_required_for": ["otp", "passkey", "captcha", "provider_challenge"],
            "prohibited_actions": [
                "cookie_export",
                "browser_profile_read",
                "hidden_endpoint_call",
                "bulk_dom_extraction",
                "provider_challenge_bypass",
            ],
        }

    @property
    def request_id(self) -> str:
        return _digest(self._unsigned())

    @property
    def sha256(self) -> str:
        return _digest({**self._unsigned(), "request_id": self.request_id})

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._unsigned(),
            "request_id": self.request_id,
            "request_sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PortalMissionRequest:
        if payload.get("schema_version") != PORTAL_REQUEST_SCHEMA:
            raise PortalContractError("unsupported portal request schema")
        try:
            request = cls(
                run_id=str(payload["run_id"]),
                portal=Portal(str(payload["portal"])),
                start_url=str(payload["start_url"]),
                query_terms=tuple(str(value) for value in payload["query_terms"]),
                locations=tuple(str(value) for value in payload["locations"]),
                max_results=int(payload["max_results"]),
                max_navigations=int(payload["max_navigations"]),
                max_seconds=int(payload["max_seconds"]),
                checkpoint_seconds=int(payload["checkpoint_seconds"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PortalContractError("portal request fields are invalid") from exc
        if payload != request.to_dict():
            raise PortalContractError("portal request digest or policy fields changed")
        return request


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
    verification_state: str = "portal_only"
    advanceable: bool = False


@dataclass(frozen=True)
class PortalMissionResponse:
    run_id: str
    request_id: str
    request_sha256: str
    query_digest: str
    portal: str
    status: str
    navigation_count: int
    elapsed_seconds: int
    safe_hostname: str
    observations: tuple[PortalMissionObservation, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PORTAL_RESPONSE_SCHEMA,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "request_sha256": self.request_sha256,
            "query_digest": self.query_digest,
            "portal": self.portal,
            "status": self.status,
            "navigation_count": self.navigation_count,
            "elapsed_seconds": self.elapsed_seconds,
            "safe_hostname": self.safe_hostname,
            "observations": [asdict(item) for item in self.observations],
        }


def validate_portal_response(
    payload: dict[str, Any], *, request: PortalMissionRequest | dict[str, Any]
) -> PortalMissionResponse:
    if isinstance(request, dict):
        mission = request.get("mission")
        if not isinstance(mission, dict):
            raise PortalContractError("portal handoff is missing its mission")
        request = PortalMissionRequest.from_dict(mission)
    allowed = {
        "schema_version",
        "run_id",
        "request_id",
        "request_sha256",
        "query_digest",
        "portal",
        "status",
        "navigation_count",
        "elapsed_seconds",
        "safe_hostname",
        "observations",
    }
    if set(payload) != allowed or payload.get("schema_version") != PORTAL_RESPONSE_SCHEMA:
        raise PortalContractError("unsupported portal response schema")
    expected = {
        "run_id": request.run_id,
        "request_id": request.request_id,
        "request_sha256": request.sha256,
        "query_digest": request.query_digest,
        "portal": request.portal.value,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise PortalContractError("portal response request binding mismatch")
    status = str(payload.get("status") or "")
    if status not in {"complete", "partial", "auth_required", "blocked", "budget_exhausted"}:
        raise PortalContractError("portal response status is invalid")
    try:
        navigation_count = int(payload["navigation_count"])
        elapsed_seconds = int(payload["elapsed_seconds"])
    except (TypeError, ValueError) as exc:
        raise PortalContractError("portal response budgets are invalid") from exc
    if not 0 <= navigation_count <= request.max_navigations:
        raise PortalContractError("portal navigation budget exceeded")
    if not 0 <= elapsed_seconds <= request.max_seconds:
        raise PortalContractError("portal elapsed-time budget exceeded")
    safe_hostname = str(payload.get("safe_hostname") or "").lower()
    if safe_hostname and safe_hostname not in request.permitted_hosts:
        raise PortalContractError("portal safe hostname is not permitted")
    rows = payload.get("observations")
    if not isinstance(rows, list) or len(rows) > request.max_results:
        raise PortalContractError("portal response exceeded its result budget")
    seen_ids: set[str] = set()
    observations: list[PortalMissionObservation] = []
    allowed_row_fields = {
        "source_job_id",
        "title",
        "company",
        "location",
        "discovery_url",
        "application_url",
        "official_url",
        "description",
        "salary",
        "posted_at",
        "verification_state",
        "advanceable",
    }
    for row in rows:
        if not isinstance(row, dict) or not set(row) <= allowed_row_fields:
            raise PortalContractError("portal observation fields are invalid")
        source_job_id = str(row.get("source_job_id") or "").strip()
        title = str(row.get("title") or "").strip()
        company = str(row.get("company") or "").strip()
        if not source_job_id or source_job_id in seen_ids:
            raise PortalContractError("portal provider job id is missing or duplicated")
        seen_ids.add(source_job_id)
        if not title or not company or len(title) > 300 or len(company) > 300:
            raise PortalContractError("portal title or company is invalid")
        discovery_url = _http_url(str(row.get("discovery_url") or ""), field_name="discovery URL")
        if _host(discovery_url) not in request.permitted_hosts:
            raise PortalContractError("portal observation discovery host is not permitted")
        application_url = _http_url(
            str(row.get("application_url") or discovery_url), field_name="application URL"
        )
        official_url = _http_url(
            str(row.get("official_url") or ""), field_name="official URL", allow_empty=True
        )
        if not official_url and _host(application_url) not in request.permitted_hosts:
            official_url = application_url
        if official_url and _host(official_url) in request.permitted_hosts:
            raise PortalContractError("portal URL cannot be a first-party official URL")
        description = str(row.get("description") or "")
        if len(description) > 20_000:
            raise PortalContractError("portal observation description is too long")
        verification_state = "first_party_resolved" if official_url else "portal_only"
        advanceable = bool(official_url)
        if "verification_state" in row and row["verification_state"] != verification_state:
            raise PortalContractError("portal observation verification state changed")
        if "advanceable" in row and row["advanceable"] is not advanceable:
            raise PortalContractError("portal observation advancement state changed")
        observations.append(
            PortalMissionObservation(
                source_job_id=source_job_id,
                title=title,
                company=company,
                location=str(row.get("location") or "")[:300],
                discovery_url=discovery_url,
                application_url=application_url,
                official_url=official_url,
                description=description,
                salary=str(row.get("salary") or "")[:300],
                posted_at=str(row.get("posted_at") or "")[:80],
                verification_state=verification_state,
                advanceable=advanceable,
            )
        )
    return PortalMissionResponse(
        run_id=request.run_id,
        request_id=request.request_id,
        request_sha256=request.sha256,
        query_digest=request.query_digest,
        portal=request.portal.value,
        status=status,
        navigation_count=navigation_count,
        elapsed_seconds=elapsed_seconds,
        safe_hostname=safe_hostname,
        observations=tuple(observations),
    )


def observation_to_raw(
    observation: PortalMissionObservation, *, portal: Portal
) -> RawJob:
    return RawJob(
        source=(
            SourceKind.HANDSHAKE_BROWSER
            if portal is Portal.HANDSHAKE
            else SourceKind.RUNWAY_BROWSER
        ),
        source_job_id=observation.source_job_id,
        title=observation.title,
        company=observation.company,
        location=observation.location,
        official_url=observation.official_url,
        application_url=observation.application_url,
        discovery_url=observation.discovery_url,
        description=observation.description,
        observed_at=datetime.now(timezone.utc),
        salary=observation.salary,
        posted_at=observation.posted_at,
        metadata={"capture_mode": "visible_browser_mission", "portal": portal.value},
    )


def write_portal_mission_request(
    *,
    run_dir: Path,
    bindings: Any,
    request: PortalMissionRequest,
) -> Path:
    """Write one browser-locked request through the existing durable handoff queue."""
    from applypilot.autonomy.handoff import (
        HANDOFF_SCHEMA_VERSION,
        _active_handoffs_unlocked,
        _handoff_queue_lock,
        _pending_for_active,
        _write_immutable_json,
    )

    run_dir = run_dir.resolve()
    if bindings.run_id != request.run_id:
        raise PortalContractError("portal request run binding mismatch")
    handoff_dir = run_dir / "handoff"
    request_path = handoff_dir / f"portal_discovery.{request.portal.value}.request.json"
    response_path = handoff_dir / f"portal_discovery.{request.portal.value}.response.json"
    kind = f"{request.portal.value}_job_observations"
    envelope = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "run_id": bindings.run_id,
        "stage": "portal_discovery",
        "kind": kind,
        "request_id": request.request_id,
        "input_digest": request.query_digest,
        "candidate_id": None,
        "fact_digest": bindings.fact_digest,
        "context_digest": bindings.context_digest,
        "policy_digest": bindings.policy_digest,
        "resource_lock": PORTAL_RESOURCE_LOCK,
        "mission": request.to_dict(),
        "mission_sha256": request.sha256,
        "response_path": str(response_path.relative_to(run_dir)),
        "checkpoint_path": str(
            response_path.with_name(
                response_path.name.replace(".response.json", ".checkpoint.json")
            ).relative_to(run_dir)
        ),
        "max_response_chars": 600_000,
        "response_format": "strict_json",
        "raw_transcript_required": False,
    }
    with _handoff_queue_lock(run_dir):
        if request_path.exists():
            _write_immutable_json(request_path, envelope)
            return request_path.resolve()
        current = _active_handoffs_unlocked(run_dir=run_dir, bindings=bindings)
        if len(current) > 1:
            raise PortalContractError("portal handoff queue has multiple active requests")
        if current:
            raise _pending_for_active(current[0])
        _write_immutable_json(request_path, envelope)
    return request_path.resolve()


def write_portal_checkpoint(
    *,
    request_path: Path,
    state: str,
    sequence: int,
    navigation_count: int,
    result_count: int,
    elapsed_seconds: int,
    safe_hostname: str,
) -> Path:
    """Replace one privacy-bounded liveness checkpoint for the active mission."""
    from applypilot.autonomy.handoff import _atomic_write_text

    allowed_states = {
        "browser_attached",
        "auth_ready",
        "auth_required",
        "query_applied",
        "page_observed",
        "candidate_observed",
        "external_link_resolved",
        "checkpoint",
        "budget_exhausted",
        "error",
    }
    if state not in allowed_states or sequence < 1:
        raise PortalContractError("portal checkpoint state or sequence is invalid")
    request_path = request_path.resolve(strict=True)
    envelope = json.loads(request_path.read_text(encoding="utf-8"))
    request = PortalMissionRequest.from_dict(envelope.get("mission") or {})
    if not 0 <= navigation_count <= request.max_navigations:
        raise PortalContractError("portal checkpoint navigation budget exceeded")
    if not 0 <= result_count <= request.max_results:
        raise PortalContractError("portal checkpoint result budget exceeded")
    if not 0 <= elapsed_seconds <= request.max_seconds:
        raise PortalContractError("portal checkpoint elapsed-time budget exceeded")
    safe_hostname = safe_hostname.strip().lower()
    if safe_hostname and safe_hostname not in request.permitted_hosts:
        raise PortalContractError("portal checkpoint hostname is not permitted")
    run_dir = request_path.parent.parent.resolve()
    raw_path = run_dir / str(envelope.get("checkpoint_path") or "")
    if raw_path.is_symlink():
        raise PortalContractError("portal checkpoint must not be a symbolic link")
    checkpoint_path = raw_path.resolve()
    if checkpoint_path.parent != request_path.parent:
        raise PortalContractError("portal checkpoint path escaped the handoff directory")
    if checkpoint_path.exists():
        current = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if current.get("request_id") != request.request_id:
            raise PortalContractError("portal checkpoint request binding changed")
        if int(current.get("sequence") or 0) >= sequence:
            raise PortalContractError("portal checkpoint sequence must increase")
    payload = {
        "schema_version": "applypilot.portal-checkpoint.v1",
        "run_id": request.run_id,
        "request_id": request.request_id,
        "portal": request.portal.value,
        "state": state,
        "sequence": sequence,
        "navigation_count": navigation_count,
        "result_count": result_count,
        "elapsed_seconds": elapsed_seconds,
        "safe_hostname": safe_hostname,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary = checkpoint_path.with_name(f".{checkpoint_path.name}.{os.getpid()}.next")
    _atomic_write_text(temporary, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, checkpoint_path)
    os.chmod(checkpoint_path, 0o600)
    return checkpoint_path


def aggregation_portal_bindings(run_id: str, request: Any) -> Any:
    """Build privacy-minimized run bindings for portal-only discovery."""
    from applypilot.autonomy.handoff import RunBindings

    context_digest = _digest(
        {
            "query_terms": list(request.query_terms),
            "locations": list(request.locations),
        }
    )
    return RunBindings(
        run_id=run_id,
        fact_digest=_digest({"applicant_facts": "not_provided"}),
        context_digest=context_digest,
        policy_digest=_digest(
            {
                "schema_version": PORTAL_REQUEST_SCHEMA,
                "resource_lock": PORTAL_RESOURCE_LOCK,
                "max_results": 25,
                "max_navigations": 30,
                "max_seconds": 180,
            }
        ),
    )


def _portal_request(run_id: str, portal: Portal, aggregation_request: Any) -> PortalMissionRequest:
    return PortalMissionRequest(
        run_id=run_id,
        portal=portal,
        start_url=(
            "https://app.joinhandshake.com/stu/postings"
            if portal is Portal.HANDSHAKE
            else "https://app.joinrunway.io/explore"
        ),
        query_terms=tuple(aggregation_request.query_terms[:4]),
        locations=tuple(aggregation_request.locations[:4]),
    )


def activate_next_portal_mission(
    *,
    store: Any,
    journal: Any,
    run_dir: Path,
    run_id: str,
    aggregation_request: Any,
) -> Path | None:
    missions = store.portal_missions(run_id)
    active = [row for row in missions if row["status"] in {"awaiting_response", "response_ready"}]
    if active:
        return Path(str(active[0]["request_path"])).resolve()
    queued = [row for row in missions if row["status"] == "queued"]
    if not queued:
        return None
    portal = Portal(str(queued[0]["portal"]))
    request = _portal_request(run_id, portal, aggregation_request)
    bindings = aggregation_portal_bindings(run_id, aggregation_request)
    request_path = write_portal_mission_request(
        run_dir=run_dir,
        bindings=bindings,
        request=request,
    )
    response_path = request_path.with_name(
        request_path.name.replace(".request.json", ".response.json")
    )
    store.activate_portal_mission(
        run_id,
        portal=portal.value,
        request_id=request.request_id,
        request_path=request_path,
        response_path=response_path,
    )
    source = (
        SourceKind.HANDSHAKE_BROWSER if portal is Portal.HANDSHAKE else SourceKind.RUNWAY_BROWSER
    )
    store.start_source(run_id, source)
    journal.emit(
        component="browser_mission",
        phase="mission",
        status="queued",
        source=portal.value,
        counts={"ordinal": int(queued[0]["ordinal"])},
        detail={"resource_lock": PORTAL_RESOURCE_LOCK},
    )
    return request_path


def initialize_portal_queue(
    *,
    store: Any,
    journal: Any,
    run_dir: Path,
    run_id: str,
    aggregation_request: Any,
    portals: tuple[str, ...],
) -> Path | None:
    store.queue_portal_missions(run_id, portals)
    for ordinal, portal in enumerate(portals, 1):
        journal.emit(
            component="browser_mission",
            phase="mission",
            status="queued",
            source=portal,
            counts={"ordinal": ordinal},
            detail={"resource_lock": PORTAL_RESOURCE_LOCK},
        )
    return activate_next_portal_mission(
        store=store,
        journal=journal,
        run_dir=run_dir,
        run_id=run_id,
        aggregation_request=aggregation_request,
    )


def _portal_receipt(
    *, request_path: Path, response_path: Path, response: PortalMissionResponse
) -> Path:
    from applypilot.autonomy.handoff import (
        HANDOFF_SCHEMA_VERSION,
        _sha256_text,
        _write_immutable_json,
    )

    receipt_path = response_path.with_name(
        response_path.name.replace(".response.json", ".receipt.json")
    )
    _write_immutable_json(
        receipt_path,
        {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "request_id": response.request_id,
            "input_digest": response.query_digest,
            "request_sha256": response.request_sha256,
            "response_sha256": _sha256_text(response_path.read_text(encoding="utf-8")),
            "status": response.status,
            "result_count": len(response.observations),
            "request_path": request_path.name,
        },
    )
    return receipt_path


def consume_portal_response(
    *,
    store: Any,
    journal: Any,
    run_dir: Path,
    run_id: str,
    request_path: Path,
) -> dict[str, Any]:
    """Persist one validated portal response and advance the serialized queue."""
    from applypilot.aggregation.normalization import normalize_job

    request_path = request_path.resolve(strict=True)
    response_path = request_path.with_name(
        request_path.name.replace(".request.json", ".response.json")
    )
    if response_path.is_symlink() or not response_path.is_file():
        raise PortalContractError("portal response artifact is missing")
    request_envelope = json.loads(request_path.read_text(encoding="utf-8"))
    mission = PortalMissionRequest.from_dict(request_envelope.get("mission") or {})
    if mission.run_id != run_id:
        raise PortalContractError("portal response run binding mismatch")
    response = validate_portal_response(
        json.loads(response_path.read_text(encoding="utf-8")),
        request=mission,
    )
    receipt_path = response_path.with_name(
        response_path.name.replace(".response.json", ".receipt.json")
    )
    if receipt_path.exists():
        revision = store.latest_revision(run_id)
        return store.get_snapshot(run_id, revision)[1]

    portal = Portal(response.portal)
    source = (
        SourceKind.HANDSHAKE_BROWSER if portal is Portal.HANDSHAKE else SourceKind.RUNWAY_BROWSER
    )
    mission_row = next(
        (row for row in store.portal_missions(run_id) if row["portal"] == portal.value),
        None,
    )
    if mission_row is None or mission_row["request_id"] != response.request_id:
        raise PortalContractError("portal response is not bound to the durable mission queue")

    terminal_states = {"complete", "partial", "auth_required", "blocked", "budget_exhausted"}
    if mission_row["status"] not in terminal_states:
        inserted = 0
        for observation in response.observations:
            inserted += int(
                store.record_observation(
                    run_id,
                    normalize_job(observation_to_raw(observation, portal=portal)),
                )
            )
        source_status = "complete" if response.status == "complete" else "partial"
        error_class = {
            "auth_required": "AuthRequired",
            "blocked": "PortalBlocked",
            "budget_exhausted": "BudgetExhausted",
        }.get(response.status, "")
        store.finish_source(
            run_id,
            source,
            status=source_status,
            error_class=error_class,
        )
        store.finish_portal_mission(
            run_id,
            portal=portal.value,
            status=response.status,
            result_count=len(response.observations),
            error_class=error_class,
        )
        journal.emit(
            component="browser_mission",
            phase="mission",
            status="validation_complete",
            source=portal.value,
            counts={
                "navigations": response.navigation_count,
                "observed": inserted,
                "elapsed_seconds": response.elapsed_seconds,
            },
            detail={"safe_hostname": response.safe_hostname},
        )

    latest_revision = store.latest_revision(run_id)
    latest = store.get_snapshot(run_id, latest_revision)[1]
    pending = list(latest.get("pending_enrichment") or [])
    if response.status != "auth_required":
        pending = [item for item in pending if item != portal.value]
    current = store.snapshot(run_id)
    needs_revision = (
        int(latest.get("observation_high_watermark") or 0)
        != int(current["observation_high_watermark"])
        or list(latest.get("pending_enrichment") or []) != pending
        or mission_row["status"] not in terminal_states
    )
    if needs_revision:
        rows = current["sources"]
        terminal = (
            "complete"
            if not pending and rows and all(row["status"] == "complete" for row in rows)
            else "partial"
        )
        store.complete_run(run_id, status=terminal)
        latest = store.publish_snapshot(
            run_id,
            reason=f"{portal.value}_browser_enrichment",
            status=terminal,
            pending_enrichment=tuple(pending),
        )
        journal.emit(
            component="aggregation",
            phase="snapshot",
            status="published",
            counts={
                "revision": int(latest["revision"]),
                "candidates": int(latest["candidate_count"]),
                "observations": int(latest["observation_count"]),
            },
            detail={"reason_code": f"{portal.value}_browser_enrichment"},
        )
    _portal_receipt(
        request_path=request_path,
        response_path=response_path,
        response=response,
    )
    if response.status != "auth_required":
        activate_next_portal_mission(
            store=store,
            journal=journal,
            run_dir=run_dir,
            run_id=run_id,
            aggregation_request=store.get_request(run_id),
        )
    return latest

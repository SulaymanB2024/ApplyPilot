"""Source-neutral aggregation contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import AsyncIterator, Protocol

from applypilot.employment import ApplicationSurface, OpportunityKind


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
        if any(not term.strip() or len(term) > 200 for term in self.query_terms):
            raise ValueError("query terms must be non-empty and at most 200 characters")
        if len(self.query_terms) > 20 or len(self.locations) > 20:
            raise ValueError("aggregation request is too broad")
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
    opportunity_kind: OpportunityKind = OpportunityKind.UNKNOWN
    application_surface: ApplicationSurface = ApplicationSurface.UNKNOWN
    routing_reasons: tuple[str, ...] = ()
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
    opportunity_kind: OpportunityKind
    application_surface: ApplicationSurface
    routing_reasons: tuple[str, ...]
    observations: tuple[JobObservation, ...]

    @property
    def source_count(self) -> int:
        return len({item.source for item in self.observations})


class SourceAdapter(Protocol):
    kind: SourceKind
    capability: SourceCapability

    def search(self, request: AggregationRequest) -> AsyncIterator[RawJob]:
        raise NotImplementedError

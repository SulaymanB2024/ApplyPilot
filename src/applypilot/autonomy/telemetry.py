"""Privacy-preserving usage accounting and hard budget enforcement."""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from applypilot.autonomy.policy import FunnelBudget


class BudgetExceeded(RuntimeError):
    """Raised before an operation would exceed a declared run budget."""


@dataclass(frozen=True)
class UsageEvent:
    timestamp: str
    stage: str
    operation: str
    surface: str
    status: str
    duration_ms: int = 0
    input_chars: int = 0
    output_chars: int = 0
    input_tokens_observed: int | None = None
    cached_input_tokens_observed: int | None = None
    output_tokens_observed: int | None = None
    reasoning_tokens_observed: int | None = None
    input_tokens_estimated: int | None = None
    output_tokens_estimated: int | None = None
    estimate_method: str = ""
    request_sha256: str = ""
    error_class: str = ""


@dataclass
class UsageLedger:
    """Bounded per-run telemetry that stores counts and hashes, not raw data."""

    run_id: str
    budget: FunnelBudget
    started_monotonic: float = field(default_factory=time.monotonic)
    counts: dict[str, int] = field(default_factory=dict)
    events: list[UsageEvent] = field(default_factory=list)
    no_progress_cycles: int = 0

    def __post_init__(self) -> None:
        self.budget.validate()
        for name in (
            "discoveries",
            "first_party_verifications",
            "material_packets",
            "form_dry_runs",
            "model_calls",
            "browser_navigations",
            "external_calls",
            "retries",
            "artifacts",
        ):
            self.counts.setdefault(name, 0)

    def reserve(self, metric: str, amount: int = 1) -> None:
        """Reserve capacity before an operation begins."""
        if amount < 0:
            raise ValueError("reservation amount cannot be negative")
        limit = getattr(self.budget, metric, None)
        if limit is None:
            raise KeyError(f"unknown budget metric: {metric}")
        current = self.counts.get(metric, 0)
        if current + amount > limit:
            raise BudgetExceeded(f"{metric} budget exhausted ({current}/{limit})")
        self._check_elapsed()
        self.counts[metric] = current + amount

    def record_model_exchange(
        self,
        *,
        stage: str,
        operation: str,
        surface: str,
        request: str,
        response: str,
        duration_ms: int,
        status: str = "ok",
        observed: dict[str, int] | None = None,
        error_class: str = "",
    ) -> None:
        """Record observed API usage or estimated Web usage."""
        self._check_elapsed()
        observed = observed or {}
        estimated_input = math.ceil(len(request) / 4)
        estimated_output = math.ceil(len(response) / 4)
        self.events.append(
            UsageEvent(
                timestamp=datetime.now(timezone.utc).isoformat(),
                stage=stage,
                operation=operation,
                surface=surface,
                status=status,
                duration_ms=duration_ms,
                input_chars=len(request),
                output_chars=len(response),
                input_tokens_observed=observed.get("input_tokens"),
                cached_input_tokens_observed=observed.get("cached_input_tokens"),
                output_tokens_observed=observed.get("output_tokens"),
                reasoning_tokens_observed=observed.get("reasoning_tokens"),
                input_tokens_estimated=estimated_input,
                output_tokens_estimated=estimated_output,
                estimate_method="chars_div_4" if not observed else "observed_plus_chars_div_4",
                request_sha256=hashlib.sha256(request.encode("utf-8")).hexdigest(),
                error_class=error_class,
            )
        )

    def record_event(
        self,
        *,
        stage: str,
        operation: str,
        surface: str,
        status: str,
        duration_ms: int = 0,
        error_class: str = "",
    ) -> None:
        self._check_elapsed()
        self.events.append(
            UsageEvent(
                timestamp=datetime.now(timezone.utc).isoformat(),
                stage=stage,
                operation=operation,
                surface=surface,
                status=status,
                duration_ms=duration_ms,
                error_class=error_class,
            )
        )

    def record_cycle(self, *, material_progress: bool) -> None:
        if material_progress:
            self.no_progress_cycles = 0
            return
        self.no_progress_cycles += 1
        if self.no_progress_cycles >= self.budget.no_progress_cycles:
            raise BudgetExceeded(
                f"no-material-progress circuit breaker opened after {self.no_progress_cycles} cycles"
            )

    def remaining(self, metric: str) -> int:
        limit = getattr(self.budget, metric)
        return max(0, limit - self.counts.get(metric, 0))

    def snapshot(self) -> dict[str, Any]:
        elapsed = int(time.monotonic() - self.started_monotonic)
        return {
            "run_id": self.run_id,
            "budget": asdict(self.budget),
            "counts": dict(self.counts),
            "remaining": {
                name: max(0, value - self.counts.get(name, 0))
                for name, value in asdict(self.budget).items()
                if name in self.counts
            },
            "elapsed_seconds": elapsed,
            "no_progress_cycles": self.no_progress_cycles,
            "events": [asdict(event) for event in self.events],
        }

    def _check_elapsed(self) -> None:
        elapsed = time.monotonic() - self.started_monotonic
        if self.budget.elapsed_seconds and elapsed > self.budget.elapsed_seconds:
            raise BudgetExceeded(
                f"elapsed time budget exhausted ({int(elapsed)}/{self.budget.elapsed_seconds}s)"
            )

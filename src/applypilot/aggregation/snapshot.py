"""Exact immutable aggregation snapshot adapter for the canonical workflow."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from applypilot.aggregation.store import SNAPSHOT_SCHEMA_VERSION, snapshot_digest
from applypilot.autonomy.models import RoleCandidate
from applypilot.employment import ApplicationSurface, OpportunityKind


class SnapshotDiscovery:
    """Expose only advanceable jobs from one exact recorded snapshot revision."""

    def __init__(
        self,
        path: Path,
        *,
        expected_query: str,
        expected_revision: int | None = None,
        expected_sha256: str = "",
    ) -> None:
        source = path.expanduser()
        if source.is_symlink() or not source.is_file():
            raise ValueError("aggregation snapshot must be a regular file")
        self.path = source.resolve(strict=True)
        if self.path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("aggregation snapshot is too large")
        self.payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(self.payload, dict):
            raise ValueError("aggregation snapshot must be an object")
        if self.payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("unsupported aggregation snapshot schema")
        self.sha256 = str(self.payload.get("sha256") or "")
        if len(self.sha256) != 64 or snapshot_digest(self.payload) != self.sha256:
            raise ValueError("aggregation snapshot digest mismatch")
        if expected_sha256 and self.sha256 != expected_sha256:
            raise ValueError("aggregation snapshot digest mismatch")
        revision = int(self.payload.get("revision") or 0)
        if revision < 1 or (expected_revision is not None and revision != expected_revision):
            raise ValueError("aggregation snapshot revision mismatch")
        if self.payload.get("status") not in {"complete", "partial"}:
            raise ValueError("aggregation snapshot is not consumable")
        if self.payload.get("query") != expected_query:
            raise ValueError("aggregation snapshot query mismatch")
        jobs = self.payload.get("jobs")
        if not isinstance(jobs, list):
            raise ValueError("aggregation snapshot jobs are invalid")
        if int(self.payload.get("candidate_count") or 0) != len(jobs):
            raise ValueError("aggregation snapshot candidate count mismatch")
        advanceable = sum(item.get("advanceable") is True for item in jobs if isinstance(item, dict))
        if int(self.payload.get("advanceable_count") or 0) != advanceable:
            raise ValueError("aggregation snapshot advanceable count mismatch")

    def find_roles(self, *, pack: Any, query: str, limit: int) -> list[RoleCandidate]:
        del pack
        if query != self.payload["query"]:
            raise ValueError("aggregation request query changed")
        if limit < 0:
            raise ValueError("aggregation discovery limit cannot be negative")
        if limit == 0:
            return []
        candidates: list[RoleCandidate] = []
        for item in self.payload["jobs"]:
            if not isinstance(item, dict) or item.get("advanceable") is not True:
                continue
            official_url = str(item.get("official_url") or "")
            if not official_url.startswith(("https://", "http://")):
                raise ValueError("advanceable aggregation job lacks an official URL")
            posted = str(item.get("posted_at") or "")
            posted_date = None
            if len(posted) >= 10:
                try:
                    posted_date = date.fromisoformat(posted[:10])
                except ValueError:
                    posted_date = None
            observations = item.get("observations") or []
            evidence = tuple(
                f"{row.get('source', '')}:{row.get('discovery_url', '')}"
                for row in observations
                if isinstance(row, dict)
            )
            candidates.append(
                RoleCandidate(
                    company=str(item.get("company") or ""),
                    title=str(item.get("title") or ""),
                    official_url=official_url,
                    source="aggregation_snapshot",
                    location=str(item.get("location") or ""),
                    description=str(item.get("description") or ""),
                    compensation=str(item.get("salary") or "")[:300],
                    posted_date=posted_date,
                    evidence=evidence,
                    opportunity_kind=OpportunityKind(
                        str(item.get("opportunity_kind") or OpportunityKind.UNKNOWN)
                    ),
                    application_surface=ApplicationSurface(
                        str(item.get("application_surface") or ApplicationSurface.UNKNOWN)
                    ),
                    requisition_id=str(item.get("canonical_key") or ""),
                    metadata={
                        "aggregation_run_id": str(self.payload["run_id"]),
                        "aggregation_snapshot_revision": int(self.payload["revision"]),
                        "aggregation_snapshot_sha256": self.sha256,
                        "source_count": int(item.get("source_count") or 0),
                        "canonical_key": str(item.get("canonical_key") or ""),
                    },
                )
            )
            if len(candidates) >= limit:
                break
        return candidates

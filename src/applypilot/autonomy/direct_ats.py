"""Read-only direct ATS discovery fallback for the autonomy funnel."""

from __future__ import annotations

from copy import deepcopy
import re

from applypilot import config
from applypilot.autonomy.context import CompactContextPack
from applypilot.autonomy.models import RoleCandidate
from applypilot.autonomy.telemetry import UsageLedger
from applypilot.discovery.direct_ats import (
    _fetch_source_jobs,
    load_direct_ats_sources,
)

QUERY_STOPWORDS = {"and", "entry", "level", "role", "roles", "the", "with"}


class DirectATSDiscovery:
    """Query configured employer ATS boards without storing rows in the jobs DB."""

    def __init__(self, *, ledger: UsageLedger):
        self.ledger = ledger

    def find_roles(
        self,
        *,
        pack: CompactContextPack,
        query: str,
        limit: int,
    ) -> list[RoleCandidate]:
        del pack
        search_config = deepcopy(config.load_search_config())
        sources = load_direct_ats_sources(search_config)
        if not sources:
            raise RuntimeError("direct_ats_fallback_unconfigured")

        candidates: list[RoleCandidate] = []
        errors: list[str] = []
        for source in sources:
            if len(candidates) >= limit or self.ledger.remaining("external_calls") <= 0:
                break
            self.ledger.reserve("external_calls")
            try:
                jobs = _fetch_source_jobs(source)
            except Exception as exc:
                errors.append(f"{source['ats']}:{type(exc).__name__}")
                self.ledger.record_event(
                    stage="discovery",
                    operation="direct_ats_fallback",
                    surface=str(source["ats"]),
                    status="gap",
                    error_class=type(exc).__name__,
                )
                continue
            self.ledger.record_event(
                stage="discovery",
                operation="direct_ats_fallback",
                surface=str(source["ats"]),
                status="ok",
            )
            for job in jobs:
                url = str(job.get("application_url") or job.get("url") or "").strip()
                title = str(job.get("title") or "").strip()
                if not url or not title or not _query_title_matches(title, query):
                    continue
                candidates.append(
                    RoleCandidate(
                        company=str(source.get("name") or source["slug"]),
                        title=title,
                        official_url=url,
                        source="direct_ats",
                        location=str(job.get("location") or "")[:240],
                        description=str(job.get("full_description") or job.get("description") or "")[:800],
                    )
                )
                if len(candidates) >= limit:
                    break

        if errors and not candidates and len(errors) == len(sources):
            raise RuntimeError("direct_ats_fallback_failed:" + ",".join(errors[:10]))
        return candidates


def _query_title_matches(title: str, query: str) -> bool:
    query_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", query.lower())
        if len(token) > 2 and token not in QUERY_STOPWORDS
    }
    if not query_tokens:
        return True
    title_tokens = set(re.findall(r"[a-z0-9]+", title.lower()))
    return bool(query_tokens & title_tokens)

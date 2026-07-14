"""Finite, tool-first autonomous application batch coordinator."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from applypilot.autonomy.context import CompactContextPack
from applypilot.autonomy.facts import FactLedger, validate_artifact_against_ledger
from applypilot.autonomy.handoff import ArtifactPending
from applypilot.autonomy.models import (
    AuthorizationGrant,
    BatchResult,
    CandidateProfile,
    Decision,
    FreshnessEvidence,
    MaterialPacket,
    RoleCandidate,
)
from applypilot.autonomy.policy import (
    RunPolicy,
    SourceAttempt,
    authorize_source,
    eligibility_gate,
    freshness_gate,
    require_authorization,
)
from applypilot.autonomy.telemetry import BudgetExceeded, UsageLedger


class DiscoveryTool(Protocol):
    def find_roles(
        self,
        *,
        pack: CompactContextPack,
        query: str,
        limit: int,
    ) -> list[RoleCandidate]: ...


class VerificationTool(Protocol):
    def verify(self, candidate: RoleCandidate) -> FreshnessEvidence: ...


class MaterialTool(Protocol):
    def draft_material(
        self,
        *,
        pack: CompactContextPack,
        candidate: RoleCandidate,
        verified_job_text: str,
    ) -> MaterialPacket: ...


class FormReviewTool(Protocol):
    def dry_run(self, *, candidate: RoleCandidate, packet: MaterialPacket) -> dict[str, Any]: ...


class FinalActionTool(Protocol):
    def submit(self, *, candidate: RoleCandidate, packet: MaterialPacket) -> dict[str, Any]: ...


class AuthorizationStore(Protocol):
    def consume(self, grant: AuthorizationGrant) -> bool: ...


class SubmissionUnconfirmed(RuntimeError):
    """Raised after an external final action lacks authoritative confirmation."""


@dataclass(frozen=True)
class BatchDependencies:
    """Injected tools keep the coordinator deterministic and testable."""

    discovery: DiscoveryTool
    verifier: VerificationTool
    materials: MaterialTool
    form_review: FormReviewTool | None = None
    final_action: FinalActionTool | None = None
    fallback_discovery: DiscoveryTool | None = None
    authorization_store: AuthorizationStore | None = None


class AutonomousBatch:
    """Run a bounded discovery-to-form funnel with explicit action gates."""

    def __init__(
        self,
        *,
        run_id: str,
        profile: CandidateProfile,
        context_pack: CompactContextPack,
        dependencies: BatchDependencies,
        policy: RunPolicy | None = None,
        authorization: AuthorizationGrant | None = None,
        output_dir: Path | None = None,
        ledger: UsageLedger | None = None,
        fact_ledger: FactLedger | None = None,
    ) -> None:
        self.run_id = run_id
        self.profile = profile
        self.context_pack = context_pack
        self.dependencies = dependencies
        self.policy = policy or RunPolicy()
        self.policy.validate()
        self.authorization = authorization
        self.output_dir = output_dir
        self.ledger = ledger or UsageLedger(run_id=run_id, budget=self.policy.budget)
        self.fact_ledger = fact_ledger

    def run(self, *, query: str) -> BatchResult:
        result = BatchResult(run_id=self.run_id, status="running")
        attempts: list[SourceAttempt] = []
        review_required: dict[str, set[str]] = {}
        try:
            candidates = self._discover(query=query, attempts=attempts, result=result)
            accepted: list[RoleCandidate] = []
            for candidate in candidates:
                decision = eligibility_gate(candidate, self.profile)
                result.eligibility.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "basis": "discovery",
                        "decision": decision.decision.value,
                        "reason_codes": list(decision.reason_codes),
                        "evidence": list(decision.evidence),
                    }
                )
                if decision.decision is not Decision.REJECT:
                    accepted.append(candidate)
                    if decision.decision is Decision.REVIEW:
                        review_required.setdefault(candidate.candidate_id, set()).update(
                            decision.reason_codes
                        )
                if decision.decision is not Decision.ACCEPT:
                    result.blockers.append(
                        {
                            "candidate_id": candidate.candidate_id,
                            "stage": "eligibility",
                            "decision": decision.decision.value,
                            "reason_codes": list(decision.reason_codes),
                        }
                    )

            verified: list[tuple[RoleCandidate, FreshnessEvidence, float]] = []
            for candidate in accepted[: self.ledger.remaining("first_party_verifications")]:
                self.ledger.reserve("first_party_verifications")
                evidence = self.dependencies.verifier.verify(candidate)
                decision = freshness_gate(
                    evidence,
                    max_post_age_days=self.policy.max_post_age_days,
                )
                result.freshness.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "decision": decision.decision.value,
                        "reason_codes": list(decision.reason_codes),
                        "first_party": evidence.first_party,
                        "resolved": evidence.resolved,
                        "open_state": evidence.open_state,
                        "status_code": evidence.status_code,
                        "provider_error": evidence.provider_error,
                    }
                )
                reviewable_freshness = (
                    decision.decision is Decision.REVIEW
                    and set(decision.reason_codes) == {"freshness_dates_missing"}
                    and evidence.first_party
                    and evidence.resolved
                    and evidence.open_state is True
                )
                if decision.decision is Decision.ACCEPT or reviewable_freshness:
                    if reviewable_freshness:
                        review_required.setdefault(candidate.candidate_id, set()).update(
                            decision.reason_codes
                        )
                    verified_candidate = replace(
                        candidate,
                        official_url=evidence.official_url,
                        title=evidence.title or candidate.title,
                        description=evidence.description or candidate.description,
                        posted_date=evidence.posted_date or candidate.posted_date,
                        start_window=evidence.start_window or candidate.start_window,
                    )
                    if verified_candidate.candidate_id != candidate.candidate_id:
                        review_required.setdefault(
                            verified_candidate.candidate_id,
                            set(),
                        ).update(review_required.get(candidate.candidate_id, set()))
                    verified_eligibility = eligibility_gate(verified_candidate, self.profile)
                    result.eligibility.append(
                        {
                            "candidate_id": verified_candidate.candidate_id,
                            "discovery_candidate_id": candidate.candidate_id,
                            "basis": "first_party",
                            "decision": verified_eligibility.decision.value,
                            "reason_codes": list(verified_eligibility.reason_codes),
                            "evidence": list(verified_eligibility.evidence),
                        }
                    )
                    if verified_eligibility.decision is not Decision.REJECT:
                        if verified_eligibility.decision is Decision.REVIEW:
                            review_required.setdefault(
                                verified_candidate.candidate_id,
                                set(),
                            ).update(verified_eligibility.reason_codes)
                        verified.append(
                            (
                                verified_candidate,
                                evidence,
                                factual_fit_score(verified_candidate, evidence, self.context_pack),
                            )
                        )
                    else:
                        result.blockers.append(
                            {
                                "candidate_id": candidate.candidate_id,
                                "verified_candidate_id": verified_candidate.candidate_id,
                                "stage": "verified_eligibility",
                                "decision": verified_eligibility.decision.value,
                                "reason_codes": list(verified_eligibility.reason_codes),
                            }
                        )
                else:
                    result.blockers.append(
                        {
                            "candidate_id": candidate.candidate_id,
                            "stage": "freshness",
                            "decision": decision.decision.value,
                            "reason_codes": list(decision.reason_codes),
                        }
                    )

            self.ledger.record_cycle(material_progress=bool(verified))
            verified.sort(key=lambda item: (-item[2], item[0].candidate_id))
            active_material_candidate_id = _active_candidate_id(
                self.dependencies.materials,
                kind="material_packet",
            )
            active_form_candidate_id = _active_candidate_id(
                self.dependencies.form_review,
                kind="form_review",
            )
            if active_material_candidate_id and active_form_candidate_id:
                raise RuntimeError("multiple artifact stages are simultaneously active")
            active_candidate_id = active_material_candidate_id or active_form_candidate_id
            if active_candidate_id:
                active_verified = [
                    item for item in verified if item[0].candidate_id == active_candidate_id
                ]
                if len(active_verified) != 1:
                    raise RuntimeError("active handoff candidate is no longer reviewable")
                if active_form_candidate_id:
                    # A later-stage form inspection must be consumed before ranking changes
                    # can create any new material exchange.
                    verified = active_verified
                else:
                    verified.sort(
                        key=lambda item: (
                            item[0].candidate_id != active_material_candidate_id,
                            -item[2],
                            item[0].candidate_id,
                        )
                    )
            packets: list[tuple[RoleCandidate, MaterialPacket]] = []
            for candidate, evidence, score in verified[: self.ledger.remaining("material_packets")]:
                self.ledger.reserve("material_packets")
                packet = self.dependencies.materials.draft_material(
                    pack=self.context_pack,
                    candidate=candidate,
                    verified_job_text=evidence.description or candidate.description,
                )
                if self.fact_ledger is not None:
                    blockers = validate_artifact_against_ledger(packet.cover_letter, self.fact_ledger)
                    if blockers:
                        raise RuntimeError("material_fact_validation_failed:" + ",".join(blockers))
                artifact_paths = self._write_packet(candidate, packet, score=score)
                if artifact_paths:
                    packet = MaterialPacket(
                        candidate_id=packet.candidate_id,
                        paragraphs=packet.paragraphs,
                        verification_gaps=packet.verification_gaps,
                        derived_applicant_claim_count=(
                            packet.derived_applicant_claim_count
                        ),
                        artifact_paths=artifact_paths,
                    )
                packets.append((candidate, packet))
                result.materials.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "fit_score": score,
                        "verification_gaps": list(packet.verification_gaps),
                        "derived_applicant_claim_count": (
                            packet.derived_applicant_claim_count
                        ),
                        "human_review_required": sorted(
                            review_required.get(candidate.candidate_id, set())
                        ),
                        "artifact_paths": dict(packet.artifact_paths),
                    }
                )

            if self.dependencies.form_review and packets and self.ledger.remaining("form_dry_runs"):
                if active_form_candidate_id:
                    matching_packets = [
                        item for item in packets if item[0].candidate_id == active_form_candidate_id
                    ]
                    if len(matching_packets) != 1:
                        raise RuntimeError("active form-review packet cannot be reconstructed")
                    candidate, packet = matching_packets[0]
                else:
                    candidate, packet = packets[0]
                self.ledger.reserve("form_dry_runs")
                review = self.dependencies.form_review.dry_run(candidate=candidate, packet=packet)
                result.form_reviews.append(
                    {"candidate_id": candidate.candidate_id, **_bounded_mapping(review)}
                )

            if not self.policy.review_only and packets:
                if self.dependencies.final_action is None:
                    raise RuntimeError("live mode requires a configured final action tool")
                if self.dependencies.authorization_store is None:
                    raise RuntimeError("live mode requires a one-time authorization store")
                if self.authorization is None:
                    raise PermissionError("exact authorization grant required before submission")
                authorized_packets = [
                    (candidate, packet)
                    for candidate, packet in packets
                    if candidate.candidate_id == self.authorization.candidate_id
                ]
                if len(authorized_packets) != 1:
                    raise PermissionError("authorization must identify exactly one prepared candidate")
                for candidate, packet in authorized_packets:
                    if review_required.get(candidate.candidate_id):
                        raise PermissionError(
                            "eligibility review must be resolved before submission"
                        )
                    review = next(
                        (
                            item
                            for item in result.form_reviews
                            if item.get("candidate_id") == candidate.candidate_id
                        ),
                        None,
                    )
                    if review is None or review.get("status") not in {
                        "form_surface_reviewed",
                        "dry_run_verified",
                    }:
                        raise PermissionError("successful form review required before submission")
                    if packet.verification_gaps:
                        raise PermissionError("material verification gaps must be resolved before submission")
                    if self.fact_ledger is None:
                        raise PermissionError("fact ledger required before submission")
                    form_review_digest = _mapping_digest(review)
                    require_authorization(
                        self.authorization,
                        run_id=self.run_id,
                        candidate_id=candidate.candidate_id,
                        action="submit_application",
                        fact_digest=self.fact_ledger.digest,
                        context_digest=self.context_pack.digest,
                        policy_digest=self.policy.digest,
                        packet_digest=packet.digest,
                        form_review_digest=form_review_digest,
                    )
                    if not self.dependencies.authorization_store.consume(self.authorization):
                        raise PermissionError("authorization grant already consumed or unknown")
                    action = self.dependencies.final_action.submit(candidate=candidate, packet=packet)
                    result.final_actions.append(
                        {"candidate_id": candidate.candidate_id, **_bounded_mapping(action)}
                    )
                    if action.get("status") != "submitted_confirmed":
                        raise SubmissionUnconfirmed(
                            f"final action status={str(action.get('status') or 'missing')[:80]}"
                        )

            form_review_blocked = bool(result.form_reviews) and any(
                review.get("status") not in {"form_surface_reviewed", "dry_run_verified"}
                for review in result.form_reviews
            )
            result.status = (
                "submitted"
                if result.final_actions
                else "form_review_blocked"
                if packets and form_review_blocked
                else "review_ready"
                if packets
                else "no_eligible_verified_roles"
            )
        except ArtifactPending as exc:
            result.status = (
                "awaiting_chatgpt_web"
                if exc.surface == "chatgpt_web"
                else "awaiting_browser_tool"
            )
            result.pending_requests.append(exc.to_dict())
            result.blockers.append(
                {
                    "stage": f"{exc.surface}_handoff",
                    "decision": "review",
                    "reason_codes": [f"{exc.surface}_response_required"],
                    "request_id": exc.request_id,
                }
            )
        except BudgetExceeded as exc:
            result.status = "budget_exhausted"
            result.blockers.append(
                {"stage": "budget", "decision": "reject", "reason_codes": [str(exc)]}
            )
        except SubmissionUnconfirmed as exc:
            result.status = "submitted_unconfirmed"
            result.blockers.append(
                {
                    "stage": "final_action",
                    "decision": "review",
                    "reason_codes": ["submission_unconfirmed"],
                    "detail": str(exc)[:240],
                }
            )
        except Exception as exc:
            result.status = "failed_closed"
            result.blockers.append(
                {
                    "stage": "runtime",
                    "decision": "reject",
                    "reason_codes": [type(exc).__name__],
                    "detail": str(exc)[:240],
                }
            )
        if self.output_dir is not None:
            try:
                self.ledger.reserve("artifacts")
            except BudgetExceeded as exc:
                result.status = "budget_exhausted"
                result.blockers.append(
                    {"stage": "budget", "decision": "reject", "reason_codes": [str(exc)]}
                )
        result.source_attempts = [asdict(attempt) for attempt in attempts]
        result.usage = self.ledger.snapshot()
        self._write_result(result)
        return result

    def _discover(
        self,
        *,
        query: str,
        attempts: list[SourceAttempt],
        result: BatchResult,
    ) -> list[RoleCandidate]:
        source_decision = authorize_source(self.policy.source.primary, policy=self.policy.source)
        if source_decision.decision is not Decision.ACCEPT:
            raise RuntimeError("primary discovery source rejected by policy")
        try:
            candidates = self.dependencies.discovery.find_roles(
                pack=self.context_pack,
                query=query,
                limit=self.ledger.remaining("discoveries"),
            )
            attempts.append(SourceAttempt(self.policy.source.primary, "ok"))
        except ArtifactPending:
            raise
        except Exception as exc:
            attempts.append(SourceAttempt(self.policy.source.primary, "failed", str(exc)[:200]))
            if self.dependencies.fallback_discovery is None:
                raise
            fallback_name = self.policy.source.fallbacks[0]
            fallback_decision = authorize_source(
                fallback_name,
                policy=self.policy.source,
                attempts=attempts,
            )
            if fallback_decision.decision is not Decision.ACCEPT:
                raise RuntimeError("fallback discovery source rejected by policy") from exc
            candidates = self.dependencies.fallback_discovery.find_roles(
                pack=self.context_pack,
                query=query,
                limit=self.ledger.remaining("discoveries"),
            )
            attempts.append(SourceAttempt(fallback_name, "ok", "recorded primary failure"))

        unique_candidates: dict[str, RoleCandidate] = {}
        for candidate in candidates:
            unique_candidates.setdefault(candidate.candidate_id, candidate)
        candidates = list(unique_candidates.values())[: self.ledger.remaining("discoveries")]
        self.ledger.reserve("discoveries", len(candidates))
        result.discoveries.extend(
            {
                "candidate_id": candidate.candidate_id,
                "company": candidate.company,
                "title": candidate.title,
                "official_url": candidate.official_url,
                "source": candidate.source,
            }
            for candidate in candidates
        )
        return candidates

    def _write_packet(
        self,
        candidate: RoleCandidate,
        packet: MaterialPacket,
        *,
        score: float,
    ) -> dict[str, str]:
        if self.output_dir is None:
            return {}
        run_dir = self.output_dir / self.run_id / candidate.candidate_id
        run_dir.mkdir(parents=True, exist_ok=True)
        cover_path = run_dir / "cover_letter_review_only.md"
        metadata_path = run_dir / "material_packet.json"
        self.ledger.reserve("artifacts", 2)
        cover_path.write_text(packet.cover_letter + "\n", encoding="utf-8")
        metadata_path.write_text(
            json.dumps(
                {
                    "candidate_id": candidate.candidate_id,
                    "company": candidate.company,
                    "title": candidate.title,
                    "official_url": candidate.official_url,
                    "fit_score": score,
                    "paragraphs": [asdict(paragraph) for paragraph in packet.paragraphs],
                    "verification_gaps": list(packet.verification_gaps),
                    "derived_applicant_claim_count": (
                        packet.derived_applicant_claim_count
                    ),
                    "external_action": "none",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return {"cover_letter": str(cover_path), "packet": str(metadata_path)}

    def _write_result(self, result: BatchResult) -> None:
        if self.output_dir is None:
            return
        run_dir = self.output_dir / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "result_ledger.json").write_text(
            json.dumps(result.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )


def factual_fit_score(
    candidate: RoleCandidate,
    evidence: FreshnessEvidence,
    pack: CompactContextPack,
) -> float:
    """Deterministic lexical score used only after hard eligibility gates."""
    import re

    stop = {"and", "for", "from", "the", "this", "with", "you", "your"}

    def tokens(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9][a-z0-9+#.-]{1,}", value.lower())
            if token not in stop
        }

    job_tokens = tokens(f"{candidate.title} {evidence.description or candidate.description}")
    fact_tokens = tokens(
        json.dumps(pack.profile, sort_keys=True)
        + " "
        + " ".join(str(item["fact"]) for item in pack.evidence)
    )
    if not job_tokens:
        return 0.0
    overlap = len(job_tokens & fact_tokens)
    return round(min(10.0, (overlap / max(8, min(len(job_tokens), 40))) * 10), 2)


def _active_candidate_id(tool: Any, *, kind: str) -> str | None:
    """Read optional artifact-resumption metadata without burdening ordinary tools."""
    if tool is None:
        return None
    resolver = getattr(tool, "active_candidate_id", None)
    if resolver is None:
        return None
    if not callable(resolver):
        raise TypeError("artifact active-candidate resolver is not callable")
    candidate_id = resolver(kind=kind)
    if candidate_id is not None and (not isinstance(candidate_id, str) or not candidate_id):
        raise ValueError("artifact active-candidate resolver returned an invalid value")
    return candidate_id


def _bounded_mapping(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:30]:
        if isinstance(item, str):
            result[str(key)] = item[:500]
        elif isinstance(item, (int, float, bool)) or item is None:
            result[str(key)] = item
        elif isinstance(item, list):
            result[str(key)] = [str(entry)[:200] for entry in item[:20]]
        else:
            result[str(key)] = str(item)[:500]
    return result


def _mapping_digest(value: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

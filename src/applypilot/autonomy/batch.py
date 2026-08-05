"""Finite, tool-first autonomous application batch coordinator."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from applypilot.autonomy.context import CompactContextPack
from applypilot.autonomy.facts import FactLedger, validate_artifact_against_ledger
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
                if decision.decision is Decision.ACCEPT:
                    accepted.append(candidate)
                else:
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
                try:
                    evidence = self.dependencies.verifier.verify(candidate)
                    decision = freshness_gate(
                        evidence,
                        max_post_age_days=self.policy.max_post_age_days,
                    )
                except BudgetExceeded:
                    raise
                except Exception as exc:
                    _record_candidate_failure(
                        result,
                        candidate,
                        stage="verification",
                        reason_code="verification_failed",
                        exc=exc,
                    )
                    continue
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
                if decision.decision is Decision.ACCEPT:
                    verified_candidate = replace(
                        candidate,
                        official_url=evidence.official_url,
                        title=evidence.title or candidate.title,
                        description=evidence.description or candidate.description,
                        posted_date=evidence.posted_date or candidate.posted_date,
                        start_window=evidence.start_window or candidate.start_window,
                    )
                    verified_eligibility = eligibility_gate(verified_candidate, self.profile)
                    result.eligibility.append(
                        {
                            "candidate_id": candidate.candidate_id,
                            "basis": "first_party",
                            "decision": verified_eligibility.decision.value,
                            "reason_codes": list(verified_eligibility.reason_codes),
                            "evidence": list(verified_eligibility.evidence),
                        }
                    )
                    if verified_eligibility.decision is Decision.ACCEPT:
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
            packets: list[tuple[RoleCandidate, MaterialPacket]] = []
            for candidate, evidence, score in verified[: self.ledger.remaining("material_packets")]:
                self.ledger.reserve("material_packets")
                try:
                    packet = self.dependencies.materials.draft_material(
                        pack=self.context_pack,
                        candidate=candidate,
                        verified_job_text=evidence.description or candidate.description,
                    )
                    if self.fact_ledger is not None:
                        blockers = validate_artifact_against_ledger(
                            packet.cover_letter,
                            self.fact_ledger,
                        )
                        if blockers:
                            raise RuntimeError(
                                "material_fact_validation_failed:" + ",".join(blockers)
                            )
                    artifact_paths = self._write_packet(candidate, packet, score=score)
                    if artifact_paths:
                        packet = MaterialPacket(
                            candidate_id=packet.candidate_id,
                            paragraphs=packet.paragraphs,
                            verification_gaps=packet.verification_gaps,
                            artifact_paths=artifact_paths,
                        )
                except BudgetExceeded:
                    raise
                except Exception as exc:
                    _record_candidate_failure(
                        result,
                        candidate,
                        stage="materials",
                        reason_code="material_generation_failed",
                        exc=exc,
                    )
                    continue
                packets.append((candidate, packet))
                result.materials.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "fit_score": score,
                        "verification_gaps": list(packet.verification_gaps),
                        "artifact_paths": dict(packet.artifact_paths),
                    }
                )

            if self.dependencies.form_review:
                for candidate, packet in packets:
                    if not self.ledger.remaining("form_dry_runs"):
                        break
                    self.ledger.reserve("form_dry_runs")
                    try:
                        review = self.dependencies.form_review.dry_run(
                            candidate=candidate,
                            packet=packet,
                        )
                    except BudgetExceeded:
                        raise
                    except Exception as exc:
                        _record_candidate_failure(
                            result,
                            candidate,
                            stage="form_review",
                            reason_code="form_review_failed",
                            exc=exc,
                        )
                        continue
                    bounded_review = {
                        "candidate_id": candidate.candidate_id,
                        **_bounded_mapping(review),
                    }
                    result.form_reviews.append(bounded_review)
                    if bounded_review.get("status") not in {
                        "form_surface_reviewed",
                        "dry_run_verified",
                    }:
                        _record_candidate_blocker(
                            result,
                            candidate,
                            stage="form_review",
                            reason_code="form_review_incomplete",
                            detail=(
                                f"status={bounded_review.get('status', 'missing')};"
                                f"reason={bounded_review.get('reason', '')}"
                            ),
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

            result.status = (
                "submitted"
                if result.final_actions
                else "review_ready"
                if packets
                else "no_eligible_verified_roles"
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
        primary_error: Exception | None = None
        primary_reason = ""
        try:
            candidates = self.dependencies.discovery.find_roles(
                pack=self.context_pack,
                query=query,
                limit=self.ledger.remaining("discoveries"),
            )
        except BudgetExceeded:
            raise
        except Exception as exc:
            primary_error = exc
            primary_reason = _bounded_failure_detail("primary_error", exc)
        else:
            if candidates:
                attempts.append(
                    SourceAttempt(
                        self.policy.source.primary,
                        "ok",
                        f"candidate_count={len(candidates)}",
                    )
                )
            else:
                primary_reason = "primary_empty_result"

        if primary_reason:
            attempts.append(SourceAttempt(self.policy.source.primary, "failed", primary_reason))
            _record_source_blocker(
                result,
                source=self.policy.source.primary,
                reason_code=(
                    "primary_discovery_empty"
                    if primary_error is None
                    else "primary_discovery_failed"
                ),
                detail=primary_reason,
            )
            if self.dependencies.fallback_discovery is None:
                raise RuntimeError(
                    "primary discovery failed and direct ATS fallback is unconfigured"
                ) from primary_error
            if not self.policy.source.fallbacks:
                raise RuntimeError("primary discovery failed and no fallback source is allowlisted")
            fallback_name = self.policy.source.fallbacks[0]
            fallback_decision = authorize_source(
                fallback_name,
                policy=self.policy.source,
                attempts=attempts,
            )
            if fallback_decision.decision is not Decision.ACCEPT:
                _record_source_blocker(
                    result,
                    source=fallback_name,
                    reason_code="fallback_discovery_rejected",
                    detail=",".join(fallback_decision.reason_codes),
                )
                raise RuntimeError("fallback discovery source rejected by policy") from primary_error
            try:
                candidates = self.dependencies.fallback_discovery.find_roles(
                    pack=self.context_pack,
                    query=query,
                    limit=self.ledger.remaining("discoveries"),
                )
            except BudgetExceeded:
                raise
            except Exception as exc:
                fallback_failure = _bounded_failure_detail("fallback_error", exc)
                attempts.append(SourceAttempt(fallback_name, "failed", fallback_failure))
                _record_source_blocker(
                    result,
                    source=fallback_name,
                    reason_code="fallback_discovery_failed",
                    detail=fallback_failure,
                )
                raise
            fallback_reason = (
                f"trigger={primary_reason};candidate_count={len(candidates)}"
            )[:200]
            attempts.append(SourceAttempt(fallback_name, "ok", fallback_reason))
            if not candidates:
                _record_source_blocker(
                    result,
                    source=fallback_name,
                    reason_code="fallback_discovery_empty",
                    detail=fallback_reason,
                )

        candidates = candidates[: self.ledger.remaining("discoveries")]
        self.ledger.reserve("discoveries", len(candidates))
        attempt_ledger = [asdict(attempt) for attempt in attempts]
        active_source = attempts[-1].source
        fallback_reason = (
            attempts[-1].reason
            if active_source != self.policy.source.primary
            else ""
        )
        result.discoveries.extend(
            {
                "candidate_id": candidate.candidate_id,
                "company": candidate.company,
                "title": candidate.title,
                "official_url": candidate.official_url,
                "source": candidate.source,
                "discovery_source": active_source,
                "fallback_reason": fallback_reason,
                "source_attempts": attempt_ledger,
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


def _record_candidate_failure(
    result: BatchResult,
    candidate: RoleCandidate,
    *,
    stage: str,
    reason_code: str,
    exc: Exception,
) -> None:
    _record_candidate_blocker(
        result,
        candidate,
        stage=stage,
        reason_code=reason_code,
        detail=f"{type(exc).__name__}:{exc}",
    )


def _record_candidate_blocker(
    result: BatchResult,
    candidate: RoleCandidate,
    *,
    stage: str,
    reason_code: str,
    detail: str,
) -> None:
    result.blockers.append(
        {
            "candidate_id": candidate.candidate_id,
            "stage": stage,
            "decision": "review",
            "reason_codes": [reason_code],
            "detail": detail[:240],
        }
    )


def _record_source_blocker(
    result: BatchResult,
    *,
    source: str,
    reason_code: str,
    detail: str,
) -> None:
    result.blockers.append(
        {
            "stage": "discovery",
            "source": source[:80],
            "decision": "review",
            "reason_codes": [reason_code],
            "detail": detail[:240],
        }
    )


def _bounded_failure_detail(prefix: str, exc: Exception) -> str:
    return f"{prefix}:{type(exc).__name__}:{exc}"[:200]


def _mapping_digest(value: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

"""Bounded ChatGPT Web adapter for discovery and local material drafting."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlparse

from applypilot.autonomy.context import (
    CompactContextPack,
    build_discovery_prompt,
    build_material_prompt,
)
from applypilot.autonomy.models import (
    DateWindow,
    MaterialPacket,
    MaterialParagraph,
    RoleCandidate,
)
from applypilot.autonomy.telemetry import BudgetExceeded, UsageLedger

SCHEMA_VERSION = "applypilot.chatgpt_web.v1"
DISALLOWED_DISCOVERY_HOSTS = (
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "google.com",
)


class ChatGPTWebUnavailable(RuntimeError):
    """Raised when the authenticated ChatGPT Web surface is unavailable."""


class ChatGPTContractError(RuntimeError):
    """Raised when ChatGPT Web violates the strict artifact contract."""


@dataclass(frozen=True)
class ChatGPTWebConfig:
    url: str = "https://chatgpt.com/?temporary-chat=true"
    timeout_ms: int = 180_000
    poll_ms: int = 250
    max_response_chars: int = 40_000


class ChatGPTWebClient:
    """Use a Playwright page as a small JSON-in/JSON-out model function.

    The adapter intentionally reads only the composer and the final assistant
    turn. It never extracts sidebar history, cookies, local storage, or a full
    page body.
    """

    def __init__(self, *, page: Any, ledger: UsageLedger, config: ChatGPTWebConfig | None = None):
        self.page = page
        self.ledger = ledger
        self.config = config or ChatGPTWebConfig()

    def probe(self) -> dict[str, Any]:
        """Read auth/composer state without filling, clicking, or sending."""
        host = urlparse(str(getattr(self.page, "url", ""))).hostname or ""
        composer_count = self.page.get_by_role("textbox", name="Chat with ChatGPT").count()
        login_count = self.page.get_by_role("button", name="Log in").count()
        return {
            "available": host == "chatgpt.com" and composer_count == 1 and login_count == 0,
            "host": host,
            "composer_count": composer_count,
            "login_visible": login_count > 0,
        }

    def find_roles(
        self,
        *,
        pack: CompactContextPack,
        query: str,
        limit: int,
    ) -> list[RoleCandidate]:
        prompt = build_discovery_prompt(pack, query=query, limit=limit)
        payload = self.ask_json(prompt, stage="discovery", operation="find_roles", kind="role_candidates")
        items = payload.get("items")
        if not isinstance(items, list):
            raise ChatGPTContractError("role_candidates.items must be a list")
        candidates: list[RoleCandidate] = []
        for raw in items[:limit]:
            if not isinstance(raw, dict):
                continue
            company = _required_text(raw, "company")
            title = _required_text(raw, "title")
            official_url = _required_text(raw, "official_url")
            parsed = urlparse(official_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ChatGPTContractError(f"invalid official_url for {company}: {official_url}")
            host = parsed.hostname.lower()
            if any(host == blocked or host.endswith(f".{blocked}") for blocked in DISALLOWED_DISCOVERY_HOSTS):
                raise ChatGPTContractError(f"disallowed discovery host for {company}: {host}")
            start = _parse_date(raw.get("start_date"))
            end = _parse_date(raw.get("end_date"))
            start_window = DateWindow(start, end, "role_start_window") if start and end else None
            candidates.append(
                RoleCandidate(
                    company=company,
                    title=title,
                    official_url=official_url,
                    source="chatgpt_web",
                    location=str(raw.get("location") or "")[:240],
                    description=str(raw.get("description") or "")[:800],
                    required_experience_min=_optional_int(raw.get("required_experience_min")),
                    required_experience_max=_optional_int(raw.get("required_experience_max")),
                    posted_date=_parse_date(raw.get("posted_date")),
                    start_window=start_window,
                    evidence=tuple(str(item)[:300] for item in (raw.get("evidence") or [])[:6]),
                )
            )
        return candidates

    def draft_material(
        self,
        *,
        pack: CompactContextPack,
        candidate: RoleCandidate,
        verified_job_text: str,
    ) -> MaterialPacket:
        prompt = build_material_prompt(
            pack,
            candidate,
            verified_job_text=verified_job_text,
        )
        payload = self.ask_json(
            prompt,
            stage="materials",
            operation="draft_cover_letter",
            kind="material_packet",
        )
        if payload.get("candidate_id") != candidate.candidate_id:
            raise ChatGPTContractError("material packet candidate_id mismatch")
        allowed_ids = {item["id"] for item in pack.evidence} | {"JOB"}
        paragraphs: list[MaterialParagraph] = []
        for raw in payload.get("paragraphs") or []:
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("text") or "").strip()
            evidence_ids = tuple(str(item) for item in raw.get("evidence_ids") or [])
            if not text or not evidence_ids:
                raise ChatGPTContractError("every material paragraph requires text and evidence ids")
            if not set(evidence_ids).issubset(allowed_ids):
                raise ChatGPTContractError("material paragraph cites an unknown evidence id")
            paragraphs.append(MaterialParagraph(text=text, evidence_ids=evidence_ids))
        if not paragraphs:
            raise ChatGPTContractError("material packet has no supported paragraphs")
        packet = MaterialPacket(
            candidate_id=candidate.candidate_id,
            paragraphs=tuple(paragraphs),
            verification_gaps=tuple(
                str(item)[:400] for item in (payload.get("verification_gaps") or [])[:10]
            ),
        )
        validate_material_provenance(packet, pack=pack, candidate=candidate, job_text=verified_job_text)
        return packet

    def ask_json(self, prompt: str, *, stage: str, operation: str, kind: str) -> dict[str, Any]:
        """Execute one fresh temporary-chat request and parse strict JSON."""
        if len(prompt) > self.ledger.budget.prompt_chars:
            raise BudgetExceeded(
                f"prompt exceeds character budget ({len(prompt)}/{self.ledger.budget.prompt_chars})"
            )
        self.ledger.reserve("model_calls")
        self.ledger.reserve("external_calls")
        started = time.monotonic()
        response = ""
        try:
            self._prepare_fresh_chat()

            composer = self.page.get_by_role("textbox", name="Chat with ChatGPT")
            if composer.count() != 1:
                raise ChatGPTWebUnavailable("ChatGPT composer contract not found")

            before_count = self._assistant_locator().count()
            composer.fill(prompt)
            send = self.page.get_by_role("button", name="Send prompt")
            if send.count() != 1 or not send.is_enabled():
                raise ChatGPTWebUnavailable("ChatGPT send control unavailable")
            send.click()
            response = self._wait_for_assistant(before_count)
            if len(response) > min(self.config.max_response_chars, self.ledger.budget.response_chars):
                raise ChatGPTContractError("ChatGPT response exceeded character budget")
            payload = parse_chatgpt_json(response, expected_kind=kind)
        except Exception as exc:
            self.ledger.record_model_exchange(
                stage=stage,
                operation=operation,
                surface="chatgpt_web",
                request=prompt,
                response=response,
                duration_ms=int((time.monotonic() - started) * 1000),
                status="error",
                error_class=type(exc).__name__,
            )
            raise
        self.ledger.record_model_exchange(
            stage=stage,
            operation=operation,
            surface="chatgpt_web",
            request=prompt,
            response=response,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return payload

    def _prepare_fresh_chat(self) -> None:
        self.ledger.reserve("browser_navigations")
        self.page.goto(self.config.url, wait_until="domcontentloaded", timeout=30_000)
        probe = self.probe()
        if not probe["available"]:
            raise ChatGPTWebUnavailable("authenticated ChatGPT Web composer unavailable")

        chat_mode = self.page.get_by_role("radio", name="Chat")
        if chat_mode.count() == 1 and not chat_mode.is_checked():
            chat_mode.click()
        temp_on = self.page.get_by_role("button", name="Turn on temporary chat")
        if temp_on.count() == 1:
            temp_on.click()
            self.page.wait_for_timeout(250)
        if not temporary_chat_is_active(self.page):
            raise ChatGPTWebUnavailable("temporary ChatGPT session could not be verified")

    def _wait_for_assistant(self, before_count: int) -> str:
        deadline = time.monotonic() + (self.config.timeout_ms / 1000)
        while time.monotonic() < deadline:
            assistants = self._assistant_locator()
            count = assistants.count()
            stop = self.page.get_by_role("button", name="Stop answering")
            generating = stop.count() == 1 and stop.is_visible()
            if count > before_count and not generating:
                text = assistants.nth(count - 1).inner_text(timeout=5_000).strip()
                if text:
                    return text
            self.page.wait_for_timeout(self.config.poll_ms)
        raise ChatGPTWebUnavailable("ChatGPT response timed out")

    def _assistant_locator(self) -> Any:
        primary = self.page.locator('[data-message-author-role="assistant"]')
        if primary.count():
            return primary
        return self.page.locator('article[data-turn="assistant"]')


def temporary_chat_is_active(page: Any) -> bool:
    """Require a positive UI indicator, not only a temporary-chat query parameter."""
    turn_off = page.get_by_role("button", name="Turn off temporary chat")
    if turn_off.count() == 1:
        return True
    label = page.get_by_text("Temporary Chat", exact=True)
    return label.count() > 0


def parse_chatgpt_json(text: str, *, expected_kind: str) -> dict[str, Any]:
    """Require exactly one JSON object with the current adapter schema."""
    stripped = text.strip()
    if not stripped.startswith("{") or not stripped.endswith("}") or "```" in stripped:
        raise ChatGPTContractError("response must be one bare JSON object")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ChatGPTContractError("response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ChatGPTContractError("response must be a JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ChatGPTContractError("unsupported ChatGPT Web schema_version")
    if payload.get("kind") != expected_kind:
        raise ChatGPTContractError("unexpected ChatGPT Web artifact kind")
    return payload


def validate_material_provenance(
    packet: MaterialPacket,
    *,
    pack: CompactContextPack,
    candidate: RoleCandidate,
    job_text: str,
) -> None:
    """Reject numeric and nonnumeric claims not grounded in each paragraph's citations."""
    evidence_by_id = {str(item["id"]): str(item["fact"]) for item in pack.evidence}
    for paragraph in packet.paragraphs:
        if not paragraph.evidence_ids:
            raise ChatGPTContractError("material paragraph has no evidence ids")
        support: list[str] = []
        for evidence_id in paragraph.evidence_ids:
            if evidence_id == "JOB":
                support.extend([candidate.company, candidate.title, job_text])
            elif evidence_id in evidence_by_id:
                support.append(evidence_by_id[evidence_id])
            else:
                raise ChatGPTContractError(f"unknown material evidence id: {evidence_id}")
        allowed_text = " ".join(support)
        unsupported_numbers = set(_numeric_claims(paragraph.text)) - set(
            _numeric_claims(allowed_text)
        )
        if unsupported_numbers:
            raise ChatGPTContractError(
                f"material contains unsupported numeric claims: {sorted(unsupported_numbers)}"
            )
        unsupported_terms = _claim_tokens(paragraph.text) - _claim_tokens(allowed_text)
        if unsupported_terms:
            raise ChatGPTContractError(
                f"material contains unsupported factual terms: {sorted(unsupported_terms)}"
            )


def _numeric_claims(value: str) -> list[str]:
    import re

    return re.findall(r"(?<![A-Za-z])(?:\$?\d[\d,.]*%?)(?![A-Za-z])", value)


SAFE_WRITING_TERMS = {
    "about",
    "am",
    "and",
    "apply",
    "applying",
    "background",
    "bring",
    "can",
    "candidate",
    "company",
    "contribute",
    "discuss",
    "excited",
    "experience",
    "for",
    "from",
    "have",
    "help",
    "how",
    "interested",
    "me",
    "my",
    "opportunity",
    "our",
    "position",
    "role",
    "skills",
    "strong",
    "support",
    "team",
    "the",
    "this",
    "through",
    "to",
    "using",
    "welcome",
    "with",
    "work",
    "working",
    "would",
    "your",
}


def _claim_tokens(value: str) -> set[str]:
    import re

    return {
        token
        for token in re.findall(r"[a-z][a-z0-9+#.-]{2,}", value.lower())
        if token not in SAFE_WRITING_TERMS
    }


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ChatGPTContractError(f"missing required field: {key}")
    return value


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ChatGPTContractError(f"expected integer, got {value!r}") from exc


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ChatGPTContractError(f"expected ISO date, got {value!r}") from exc

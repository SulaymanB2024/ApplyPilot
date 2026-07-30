"""Bounded ChatGPT Web adapter for discovery and local material drafting."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

from applypilot.autonomy.context import (
    CompactContextPack,
    build_discovery_prompt,
    build_material_prompt,
)
from applypilot.autonomy.models import (
    ApplicantClaim,
    DateWindow,
    MaterialPacket,
    MaterialParagraph,
    RoleCandidate,
)
from applypilot.autonomy.telemetry import BudgetExceeded, UsageLedger

SCHEMA_VERSION = "applypilot.chatgpt_web.v1"
TOP_LEVEL_KEYS = {
    "role_candidates": {"schema_version", "kind", "request_id", "items"},
    "material_packet": {
        "schema_version",
        "kind",
        "request_id",
        "candidate_id",
        "paragraphs",
        "verification_gaps",
    },
}
ROLE_ITEM_KEYS = {
    "company",
    "title",
    "official_url",
    "location",
    "description",
    "required_experience_min",
    "required_experience_max",
    "posted_date",
    "start_date",
    "end_date",
    "evidence",
}
MATERIAL_PARAGRAPH_KEYS = {"text", "evidence_ids", "applicant_claims"}
APPLICANT_CLAIM_KEYS = {"text", "evidence_ids"}
DISALLOWED_DISCOVERY_HOSTS = (
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "google.com",
)
MAX_MATERIAL_PARAGRAPHS = 4
MAX_MATERIAL_WORDS = 450
MAX_MATERIAL_CLAIMS = 20
DISCOVERY_URL_RE = re.compile(r"https?://[^\s<>'\"\])}]+", re.IGNORECASE)
DISCOVERY_ENTRY_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\d{1,3}[.)]\s+|(?:role|opportunity)\s+\d{1,3}\s*[:.)-]\s+)",
    re.IGNORECASE,
)
DISCOVERY_HEADING_RE = re.compile(r"^\s*#{2,6}\s+\S")
DISCOVERY_REFERENCE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?reference\s*:\s*([a-z0-9._:-]{8,128})\s*$",
    re.IGNORECASE,
)
ROLE_TITLE_TERMS = (
    "intern",
    "internship",
    "co-op",
    "coop",
    "apprentice",
    "fellow",
    "analyst",
    "associate",
    "product",
    "strategy",
    "operations",
    "analytics",
    "data",
    "growth",
    "marketing",
    "venture",
    "program",
    "business",
    "technology",
    "technical",
    "research",
    "engineer",
    "consultant",
    "coordinator",
)
GENERIC_LINK_LABELS = {
    "apply",
    "apply here",
    "career page",
    "careers page",
    "job posting",
    "official link",
    "official posting",
    "posting",
    "source",
}


class ChatGPTWebUnavailable(RuntimeError):
    """Raised when the authenticated ChatGPT Web surface is unavailable."""


class ChatGPTContractError(RuntimeError):
    """Raised when ChatGPT Web violates the strict artifact contract."""


@dataclass(frozen=True)
class ChatGPTWebConfig:
    url: str = "https://chatgpt.com/?temporary-chat=true"
    timeout_ms: int | None = None
    poll_ms: int = 250
    max_response_chars: int = 80_000


class ChatGPTWebClient:
    """Use a Playwright page as a bounded natural-language model function.

    The adapter intentionally reads only the composer and the final assistant
    turn. It never extracts sidebar history, cookies, local storage, or a full
    page body. Discovery prose is normalized locally into the internal role
    schema; evidence-bound material packets remain strict JSON.
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
        return role_candidates_from_payload(payload, limit=limit)

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
        return material_packet_from_payload(
            payload,
            pack=pack,
            candidate=candidate,
            verified_job_text=verified_job_text,
        )

    def ask_json(self, prompt: str, *, stage: str, operation: str, kind: str) -> dict[str, Any]:
        """Execute one fresh temporary-chat request and normalize its response."""
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
            payload = parse_chatgpt_response(response, expected_kind=kind)
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
        deadline = (
            time.monotonic() + (self.config.timeout_ms / 1000)
            if self.config.timeout_ms and self.config.timeout_ms > 0
            else None
        )
        while deadline is None or time.monotonic() < deadline:
            assistants = self._assistant_locator()
            count = assistants.count()
            stop = self.page.get_by_role("button", name="Stop answering")
            generating = stop.count() == 1 and stop.is_visible()
            if count > before_count and not generating:
                text = (assistants.nth(count - 1).text_content(timeout=5_000) or "").strip()
                if text:
                    return text
            self.page.wait_for_timeout(self.config.poll_ms)
        raise ChatGPTWebUnavailable("configured ChatGPT response deadline elapsed")

    def _assistant_locator(self) -> Any:
        primary = self.page.locator('[data-message-author-role="assistant"]')
        if primary.count():
            return primary
        return self.page.locator('article[data-turn="assistant"]')


def role_candidates_from_payload(
    payload: dict[str, Any],
    *,
    limit: int,
) -> list[RoleCandidate]:
    """Convert a validated ChatGPT artifact into bounded role candidates."""
    items = payload.get("items")
    if not isinstance(items, list):
        raise ChatGPTContractError("role_candidates.items must be a list")
    candidates: list[RoleCandidate] = []
    for raw in items[:limit]:
        if not isinstance(raw, dict):
            raise ChatGPTContractError("role candidate entries must be objects")
        _reject_extra_keys(raw, ROLE_ITEM_KEYS, surface="role candidate")
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
        evidence = raw.get("evidence") or []
        if not isinstance(evidence, list):
            raise ChatGPTContractError("role candidate evidence must be a list")
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
                evidence=tuple(str(item)[:300] for item in evidence[:6]),
            )
        )
    return candidates


def material_packet_from_payload(
    payload: dict[str, Any],
    *,
    pack: CompactContextPack,
    candidate: RoleCandidate,
    verified_job_text: str,
) -> MaterialPacket:
    """Convert a validated ChatGPT artifact into a provenance-checked packet."""
    if payload.get("candidate_id") != candidate.candidate_id:
        raise ChatGPTContractError("material packet candidate_id mismatch")
    raw_paragraphs = payload.get("paragraphs")
    if not isinstance(raw_paragraphs, list):
        raise ChatGPTContractError("material packet paragraphs must be a list")
    if len(raw_paragraphs) > MAX_MATERIAL_PARAGRAPHS:
        raise ChatGPTContractError("material packet exceeds paragraph limit")
    raw_gaps = payload.get("verification_gaps") or []
    if not isinstance(raw_gaps, list):
        raise ChatGPTContractError("material verification_gaps must be a list")
    allowed_ids = {item["id"] for item in pack.evidence} | {"JOB"}
    paragraphs: list[MaterialParagraph] = []
    claim_count = 0
    derived_claim_count = 0
    for raw in raw_paragraphs:
        if not isinstance(raw, dict):
            raise ChatGPTContractError("material paragraph entries must be objects")
        _reject_extra_keys(raw, MATERIAL_PARAGRAPH_KEYS, surface="material paragraph")
        text = str(raw.get("text") or "").strip()
        raw_evidence_ids = raw.get("evidence_ids")
        if not isinstance(raw_evidence_ids, list):
            raise ChatGPTContractError("material evidence_ids must be a list")
        evidence_ids = tuple(str(item) for item in raw_evidence_ids)
        if not text or not evidence_ids:
            raise ChatGPTContractError("every material paragraph requires text and evidence ids")
        if not set(evidence_ids).issubset(allowed_ids):
            raise ChatGPTContractError("material paragraph cites an unknown evidence id")
        raw_claims = raw.get("applicant_claims")
        if raw_claims is None:
            raw_claims = []
        elif not isinstance(raw_claims, list):
            raise ChatGPTContractError("material applicant_claims must be a list")
        applicant_claims: list[ApplicantClaim] = []
        for raw_claim in raw_claims:
            if not isinstance(raw_claim, dict):
                raise ChatGPTContractError("material applicant claims must be objects")
            _reject_extra_keys(
                raw_claim,
                APPLICANT_CLAIM_KEYS,
                surface="applicant claim",
            )
            claim_text = str(raw_claim.get("text") or "").strip()
            raw_claim_ids = raw_claim.get("evidence_ids")
            if not claim_text or not isinstance(raw_claim_ids, list) or not raw_claim_ids:
                raise ChatGPTContractError(
                    "every applicant claim requires exact text and applicant evidence ids"
                )
            claim_ids = tuple(str(item) for item in raw_claim_ids)
            if "JOB" in claim_ids:
                raise ChatGPTContractError("JOB cannot support an applicant claim")
            if not set(claim_ids).issubset(allowed_ids - {"JOB"}):
                raise ChatGPTContractError("applicant claim cites an unknown applicant fact")
            if not set(claim_ids).issubset(evidence_ids):
                raise ChatGPTContractError(
                    "applicant claim evidence must also be cited by its paragraph"
                )
            applicant_claims.append(
                ApplicantClaim(text=claim_text, evidence_ids=claim_ids)
            )
        explicit_claim_keys = {
            _normalized_sentence(claim.text) for claim in applicant_claims
        }
        paragraph_applicant_ids = tuple(
            evidence_id for evidence_id in evidence_ids if evidence_id != "JOB"
        )
        for sentence in _material_sentences(text):
            sentence_key = _normalized_sentence(sentence)
            if (
                not _requires_applicant_claim(sentence)
                or sentence_key in explicit_claim_keys
                or not paragraph_applicant_ids
            ):
                continue
            applicant_claims.append(
                ApplicantClaim(
                    text=sentence,
                    evidence_ids=paragraph_applicant_ids,
                )
            )
            explicit_claim_keys.add(sentence_key)
            derived_claim_count += 1
        claim_count += len(applicant_claims)
        if claim_count > MAX_MATERIAL_CLAIMS:
            raise ChatGPTContractError("material packet exceeds applicant claim limit")
        paragraphs.append(
            MaterialParagraph(
                text=text,
                evidence_ids=evidence_ids,
                applicant_claims=tuple(applicant_claims),
            )
        )
    if not paragraphs:
        raise ChatGPTContractError("material packet has no supported paragraphs")
    if sum(len(paragraph.text.split()) for paragraph in paragraphs) > MAX_MATERIAL_WORDS:
        raise ChatGPTContractError("material packet exceeds word limit")
    packet = MaterialPacket(
        candidate_id=candidate.candidate_id,
        paragraphs=tuple(paragraphs),
        verification_gaps=tuple(
            str(item)[:400] for item in raw_gaps[:10]
        ),
        derived_applicant_claim_count=derived_claim_count,
    )
    validate_material_provenance(packet, pack=pack, candidate=candidate, job_text=verified_job_text)
    return packet


def temporary_chat_is_active(page: Any) -> bool:
    """Require a positive UI indicator, not only a temporary-chat query parameter."""
    turn_off = page.get_by_role("button", name="Turn off temporary chat")
    if turn_off.count() == 1:
        return True
    label = page.get_by_text("Temporary Chat", exact=True)
    return label.count() > 0


def parse_chatgpt_response(
    text: str,
    *,
    expected_kind: str,
    request_id: str = "",
) -> dict[str, Any]:
    """Normalize a reader-facing response into ApplyPilot's internal schema.

    Discovery accepts a natural-language numbered list and keeps the old bare
    JSON form as a compatibility input. Material packets remain strict JSON
    because their evidence bindings are a safety boundary, not presentation
    ceremony.
    """
    stripped = text.strip()
    natural_response = not stripped.startswith("{")
    if not natural_response:
        payload = parse_chatgpt_json(stripped, expected_kind=expected_kind)
    elif expected_kind == "role_candidates":
        payload = _parse_natural_discovery_response(stripped)
    else:
        payload = parse_chatgpt_json(stripped, expected_kind=expected_kind)

    existing_request_id = str(payload.get("request_id") or "")
    if (
        natural_response
        and expected_kind == "role_candidates"
        and request_id
        and not existing_request_id
    ):
        raise ChatGPTContractError("natural-language discovery response is missing its reference")
    if request_id and existing_request_id != request_id:
        raise ChatGPTContractError("ChatGPT response request_id mismatch")
    if request_id:
        payload = {**payload, "request_id": request_id}

    if expected_kind == "role_candidates":
        items = payload.get("items")
        if not isinstance(items, list):
            raise ChatGPTContractError("role_candidates.items must be a list")
        if not items:
            raise ChatGPTContractError(
                "discovery response contained no role candidates; "
                "return useful verified roles instead of an empty placeholder"
            )
    return payload


def _parse_natural_discovery_response(text: str) -> dict[str, Any]:
    if not text:
        raise ChatGPTContractError("discovery response is empty")
    blocks = _discovery_response_blocks(text)
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for block in blocks:
        official_url = _discovery_role_url(block)
        if not official_url:
            continue
        normalized_url = official_url.rstrip("/").lower()
        if normalized_url in seen_urls:
            continue
        item = _natural_role_item(block, official_url)
        if item is None:
            continue
        items.append(item)
        seen_urls.add(normalized_url)
        if len(items) >= 100:
            break
    if not items:
        raise ChatGPTContractError(
            "could not identify any role title, company, and official URL "
            "from the natural-language discovery response"
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": "role_candidates",
        "items": items,
    }
    reference = next(
        (
            match
            for line in text.splitlines()
            if (match := DISCOVERY_REFERENCE_RE.match(_clean_discovery_markdown(line)))
        ),
        None,
    )
    if reference:
        payload["request_id"] = reference.group(1)
    return payload


def _discovery_response_blocks(text: str) -> list[str]:
    lines = text.splitlines()
    table_rows = [
        line.strip()
        for line in lines
        if DISCOVERY_URL_RE.search(line) and line.count("|") >= 2
    ]
    if table_rows:
        return table_rows

    starts = [
        index
        for index, line in enumerate(lines)
        if DISCOVERY_ENTRY_RE.match(line) or DISCOVERY_HEADING_RE.match(line)
    ]
    blocks: list[str] = []
    if starts:
        for position, start in enumerate(starts):
            end = starts[position + 1] if position + 1 < len(starts) else len(lines)
            block = "\n".join(lines[start:end]).strip()
            if DISCOVERY_URL_RE.search(block):
                blocks.append(block)
    if blocks:
        return blocks

    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", text)
        if paragraph.strip() and DISCOVERY_URL_RE.search(paragraph)
    ]
    if paragraphs:
        return paragraphs

    return [
        "\n".join(lines[max(0, index - 2) : min(len(lines), index + 4)]).strip()
        for index, line in enumerate(lines)
        if DISCOVERY_URL_RE.search(line)
    ]


def _discovery_role_url(block: str) -> str:
    official_labels = {
        "apply",
        "application",
        "job posting",
        "official link",
        "official posting",
        "official url",
        "posting",
        "url",
    }
    for raw_line in block.splitlines():
        line = _clean_discovery_markdown(raw_line)
        if ":" not in line:
            continue
        label = line.split(":", 1)[0]
        label = re.sub(r"^[\s\-*#\d.)]+", "", label).strip().lower()
        urls = DISCOVERY_URL_RE.findall(raw_line)
        if label in official_labels and urls:
            return urls[0].rstrip(".,;:!?")

    urls = [
        raw_url.rstrip(".,;:!?")
        for raw_url in DISCOVERY_URL_RE.findall(block)
    ]
    if len(urls) <= 1:
        return urls[0] if urls else ""
    for url in urls:
        host = (urlparse(url).hostname or "").lower()
        if not any(
            host == blocked or host.endswith(f".{blocked}")
            for blocked in DISALLOWED_DISCOVERY_HOSTS
        ):
            return url
    return urls[0]


def _natural_role_item(block: str, official_url: str) -> dict[str, Any] | None:
    company = _discovery_field(block, ("company", "employer", "organization"))
    title = _discovery_field(block, ("title", "role", "position"))
    heading = _discovery_heading(block)
    heading_title, heading_company = _identity_from_heading(
        heading,
        official_url=official_url,
    )
    title = title or heading_title
    company = company or heading_company

    link_label = _markdown_link_label(block, official_url)
    if not title and link_label and _role_title_score(link_label):
        title = link_label
    if not title or not company:
        sentence_title, sentence_company = _identity_from_sentence(block)
        title = title or sentence_title
        company = company or sentence_company
    if not title or not company:
        return None

    location = _discovery_field(block, ("location", "where"))
    fit = _discovery_field(
        block,
        (
            "why it fits",
            "why this fits",
            "semantic match",
            "fit",
            "match",
            "relevance",
            "summary",
        ),
    )
    source_observation = _discovery_field(
        block,
        ("evidence", "source evidence", "source note", "posting evidence"),
    )
    description = fit or source_observation or _discovery_description(block, heading)
    experience_min, experience_max = _discovery_experience(block)
    posted_date = _discovery_date_field(block, ("posted", "posted date", "date posted"))
    start_date = _discovery_date_field(block, ("start", "start date", "begins"))
    end_date = _discovery_date_field(block, ("end", "end date", "deadline"))
    evidence = [
        value[:300]
        for value in (source_observation, fit)
        if value
    ]
    if not evidence and description:
        evidence = [description[:300]]
    return {
        "company": company[:200],
        "title": title[:240],
        "official_url": official_url,
        "location": location[:240],
        "description": description[:800],
        "required_experience_min": experience_min,
        "required_experience_max": experience_max,
        "posted_date": posted_date,
        "start_date": start_date,
        "end_date": end_date,
        "evidence": evidence[:6],
    }


def _discovery_field(block: str, labels: tuple[str, ...]) -> str:
    normalized_labels = {label.lower() for label in labels}
    for raw_line in block.splitlines():
        line = _clean_discovery_markdown(raw_line)
        if ":" not in line:
            continue
        label, value = line.split(":", 1)
        label = re.sub(r"^[\s\-*#\d.)]+", "", label).strip().lower()
        if label in normalized_labels:
            return value.strip()
    return ""


def _discovery_heading(block: str) -> str:
    metadata_labels = {
        "location",
        "where",
        "company",
        "employer",
        "organization",
        "title",
        "role",
        "position",
        "official posting",
        "official url",
        "link",
        "url",
        "why it fits",
        "why this fits",
        "semantic match",
        "fit",
        "match",
        "relevance",
        "summary",
        "evidence",
        "source evidence",
        "source note",
        "posting evidence",
        "posted",
        "posted date",
        "date posted",
        "start",
        "start date",
        "end",
        "end date",
        "deadline",
        "experience",
        "required experience",
        "reference",
    }
    for raw_line in block.splitlines():
        line = _clean_discovery_markdown(raw_line)
        line = DISCOVERY_URL_RE.sub("", line).strip(" -–—|:;,")
        line = re.sub(
            r"^\s*(?:#{1,6}\s*)?(?:\d{1,3}[.)]\s+|"
            r"(?:role|opportunity)\s+\d{1,3}\s*[:.)-]\s+)",
            "",
            line,
            flags=re.IGNORECASE,
        ).strip()
        if not line:
            continue
        label = line.split(":", 1)[0].strip().lower()
        if label in metadata_labels:
            continue
        return line
    return ""


def _identity_from_heading(
    heading: str,
    *,
    official_url: str,
) -> tuple[str, str]:
    cleaned = heading.strip(" *#`")
    if not cleaned:
        return "", ""
    at_match = re.fullmatch(
        r"(?P<title>.+?)\s+at\s+(?P<company>.+)",
        cleaned,
        flags=re.IGNORECASE,
    )
    if at_match:
        return (
            at_match.group("title").strip(" -–—|"),
            at_match.group("company").strip(" -–—|"),
        )
    parts = re.split(r"\s+(?:—|–|\||-\s)\s*", cleaned, maxsplit=1)
    if len(parts) != 2:
        return "", ""
    left, right = (part.strip(" -–—|") for part in parts)
    if not left or not right:
        return "", ""
    left_role = _role_title_score(left)
    right_role = _role_title_score(right)
    left_host = _host_identity_score(left, official_url)
    right_host = _host_identity_score(right, official_url)
    if right_host > left_host:
        return left, right
    if left_host > right_host:
        return right, left
    if right_role > left_role:
        return right, left
    return left, right


def _identity_from_sentence(block: str) -> tuple[str, str]:
    cleaned = " ".join(
        _clean_discovery_markdown(line)
        for line in block.splitlines()
        if not DISCOVERY_URL_RE.search(line)
    )
    hiring = re.search(
        r"(?P<company>[A-Z][A-Za-z0-9&.'’ +\-]{1,80}?)\s+"
        r"(?:is hiring|is recruiting for|has opened)\s+(?:an?\s+)?"
        r"(?P<title>[^.;]{3,120})",
        cleaned,
    )
    if hiring:
        return hiring.group("title").strip(), hiring.group("company").strip()
    return "", ""


def _markdown_link_label(block: str, official_url: str) -> str:
    match = re.search(
        rf"\[([^\]]+)\]\(\s*{re.escape(official_url)}\s*\)",
        block,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    label = _clean_discovery_markdown(match.group(1))
    if label.lower() in GENERIC_LINK_LABELS:
        return ""
    return label


def _role_title_score(value: str) -> int:
    normalized = re.sub(r"[^a-z0-9+]+", " ", value.lower())
    score = sum(
        1
        for term in ROLE_TITLE_TERMS
        if re.search(rf"\b{re.escape(term)}\b", normalized)
    )
    if re.search(r"\bintern(?:ship)?\b|\bco op\b|\bnew grad\b", normalized):
        score += 3
    return score


def _host_identity_score(value: str, official_url: str) -> int:
    host = (urlparse(official_url).hostname or "").lower()
    host_tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", host)
        if len(token) >= 3
        and token
        not in {
            "www",
            "jobs",
            "job",
            "careers",
            "career",
            "greenhouse",
            "lever",
            "ashbyhq",
            "workdayjobs",
            "myworkdayjobs",
            "oraclecloud",
            "icims",
            "com",
            "org",
            "net",
        }
    }
    value_tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", value.lower())
        if len(token) >= 3
    }
    return len(host_tokens & value_tokens)


def _discovery_description(block: str, heading: str) -> str:
    ignored_prefixes = (
        "location:",
        "where:",
        "company:",
        "employer:",
        "organization:",
        "title:",
        "role:",
        "position:",
        "official posting:",
        "official url:",
        "link:",
        "url:",
        "posted:",
        "posted date:",
        "date posted:",
        "start:",
        "start date:",
        "end:",
        "end date:",
        "deadline:",
        "experience:",
        "required experience:",
        "reference:",
    )
    parts: list[str] = []
    for raw_line in block.splitlines():
        if DISCOVERY_URL_RE.search(raw_line):
            continue
        line = _clean_discovery_markdown(raw_line)
        line = re.sub(r"^\s*(?:#{1,6}\s*)?\d{1,3}[.)]\s+", "", line).strip()
        if (
            not line
            or line == heading
            or line.lower().startswith(ignored_prefixes)
        ):
            continue
        parts.append(line)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _discovery_experience(block: str) -> tuple[int | None, int | None]:
    value = _discovery_field(
        block,
        ("experience", "required experience", "experience required"),
    )
    if not value:
        value = " ".join(
            _clean_discovery_markdown(line)
            for line in block.splitlines()
            if re.search(r"\bexperience\b", line, flags=re.IGNORECASE)
        )
    normalized = value.lower()
    if re.search(r"\bno (?:prior|required) experience\b", normalized):
        return 0, 0
    range_match = re.search(
        r"\b(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*(?:years?|yrs?)\b",
        normalized,
    )
    if range_match:
        return int(range_match.group(1)), int(range_match.group(2))
    minimum = re.search(
        r"\b(?:at least\s+)?(\d{1,2})\+?\s*(?:years?|yrs?)"
        r"(?:\s+of)?\s+experience\b",
        normalized,
    )
    if minimum:
        return int(minimum.group(1)), None
    return None, None


def _discovery_date_field(block: str, labels: tuple[str, ...]) -> str | None:
    value = _discovery_field(block, labels)
    if not value:
        return None
    iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", value)
    if iso:
        return iso.group(1)
    natural = re.search(
        r"\b("
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
        r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|"
        r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+\d{1,2},?\s+\d{4})\b",
        value,
        flags=re.IGNORECASE,
    )
    if not natural:
        return None
    candidate = natural.group(1)
    for date_format in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        try:
            return datetime.strptime(candidate, date_format).date().isoformat()
        except ValueError:
            continue
    return None


def _clean_discovery_markdown(value: str) -> str:
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    return cleaned.strip()


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
    allowed_keys = TOP_LEVEL_KEYS.get(expected_kind)
    if allowed_keys is None:
        raise ChatGPTContractError("unsupported ChatGPT Web artifact kind")
    _reject_extra_keys(payload, allowed_keys, surface=expected_kind)
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
        applicant_support: list[str] = []
        job_cited = False
        for evidence_id in paragraph.evidence_ids:
            if evidence_id == "JOB":
                job_cited = True
            elif evidence_id in evidence_by_id:
                applicant_support.append(evidence_by_id[evidence_id])
            else:
                raise ChatGPTContractError(f"unknown material evidence id: {evidence_id}")
        allowed_parts = list(applicant_support)
        if job_cited:
            allowed_parts.extend([candidate.company, candidate.title, job_text])
        allowed_text = " ".join(allowed_parts)
        numeric_reference_text = " ".join(
            [*applicant_support, candidate.company, candidate.title]
        )
        unsupported_numbers = set(_numeric_claims(paragraph.text)) - set(
            _numeric_claims(numeric_reference_text)
        )
        if unsupported_numbers:
            raise ChatGPTContractError(
                f"material contains unsupported numeric claims: {sorted(unsupported_numbers)}"
            )
        unsupported_terms = _claim_tokens(paragraph.text) - _claim_tokens(allowed_text)
        unsupported_high_risk = unsupported_terms & HIGH_RISK_CLAIM_TERMS
        unsupported_entities = _unsupported_named_entities(
            paragraph.text,
            allowed_text=allowed_text,
        )
        unsupported_paragraph_facts = unsupported_high_risk | unsupported_entities
        if unsupported_paragraph_facts:
            raise ChatGPTContractError(
                "material contains unsupported factual terms: "
                f"{sorted(unsupported_paragraph_facts)}"
            )
        sentences = _material_sentences(paragraph.text)
        sentence_keys = {_normalized_sentence(sentence) for sentence in sentences}
        claim_keys: set[str] = set()
        applicant_claim_gaps: set[str] = set()
        for claim in paragraph.applicant_claims:
            claim_key = _normalized_sentence(claim.text)
            if claim_key in claim_keys:
                raise ChatGPTContractError("duplicate applicant claim sentence")
            claim_keys.add(claim_key)
            if claim_key not in sentence_keys:
                raise ChatGPTContractError(
                    "applicant claim must exactly match one full prose sentence"
                )
            claim_support = " ".join(evidence_by_id[evidence_id] for evidence_id in claim.evidence_ids)
            claim_numbers = set(_numeric_claims(claim.text)) - set(
                _numeric_claims(claim_support)
            )
            if claim_numbers:
                raise ChatGPTContractError(
                    f"applicant claim contains unsupported numeric claims: {sorted(claim_numbers)}"
                )
            applicant_claim_gaps.update(
                _unsupported_claim_terms(
                    claim.text,
                    allowed_text=claim_support,
                )
            )
            applicant_claim_gaps.update(
                _unsupported_named_entities(
                    claim.text,
                    allowed_text=claim_support,
                )
            )
            if _employment_claim_mentions_company(claim.text, candidate.company) and not (
                _claim_tokens(candidate.company) <= _claim_tokens(claim_support)
            ):
                applicant_claim_gaps.update(
                    _claim_tokens(candidate.company) & _claim_tokens(claim.text)
                )

        for sentence in sentences:
            if not _requires_applicant_claim(sentence):
                continue
            if not applicant_support:
                raise ChatGPTContractError(
                    "material applicant assertion has no applicant evidence"
                )
            if _normalized_sentence(sentence) not in claim_keys:
                raise ChatGPTContractError(
                    "material applicant assertion lacks a structured applicant claim"
                )
        if applicant_claim_gaps:
            raise ChatGPTContractError(
                f"material contains unsupported factual terms: {sorted(applicant_claim_gaps)}"
            )


def _numeric_claims(value: str) -> list[str]:
    return [
        claim.rstrip(".,")
        for claim in re.findall(r"(?<![A-Za-z])(?:\$?\d[\d,.]*%?)(?![A-Za-z])", value)
    ]


SAFE_WRITING_TERMS = {
    "about",
    "able",
    "am",
    "and",
    "apply",
    "applying",
    "among",
    "background",
    "became",
    "bring",
    "can",
    "candidate",
    "company",
    "contribute",
    "closely",
    "connect",
    "connects",
    "discuss",
    "excited",
    "experience",
    "foundation",
    "for",
    "from",
    "have",
    "help",
    "how",
    "interested",
    "interest",
    "me",
    "my",
    "opportunity",
    "part",
    "our",
    "position",
    "practical",
    "role",
    "seeking",
    "skills",
    "strong",
    "strength",
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

HIGH_RISK_CLAIM_TERMS = {
    "awarded",
    "built",
    "certified",
    "created",
    "degree",
    "delivered",
    "designed",
    "developed",
    "employed",
    "expert",
    "expertise",
    "fluent",
    "founded",
    "generated",
    "graduated",
    "implemented",
    "increased",
    "launched",
    "led",
    "managed",
    "owned",
    "raised",
    "reduced",
    "worked",
    "won",
}

APPLICANT_ASSERTION_TERMS = HIGH_RISK_CLAIM_TERMS | {
    "background",
    "candidate",
    "education",
    "experience",
    "expertise",
    "project",
    "projects",
    "skill",
    "skills",
    "strength",
    "student",
    "work",
}

SAFE_NAMED_ENTITY_TERMS = {
    "as",
    "dear",
    "hiring",
    "manager",
    "team",
    "the",
    "this",
    "through",
    "thank",
}


def _claim_tokens(value: str) -> set[str]:
    return {
        token.strip(".-")
        for token in re.findall(r"[a-z][a-z0-9+#.-]{2,}", value.lower())
        if token.strip(".-") and token.strip(".-") not in SAFE_WRITING_TERMS
    }


def _unsupported_claim_terms(value: str, *, allowed_text: str) -> set[str]:
    allowed_stems = {_claim_stem(token) for token in _claim_tokens(allowed_text)}
    return {
        token
        for token in _claim_tokens(value)
        if _claim_stem(token) not in allowed_stems
    }


def _claim_stem(token: str) -> str:
    irregular = {
        "analyses": "analysis",
        "analytics": "analytic",
        "analytical": "analytic",
        "built": "build",
        "created": "create",
        "creating": "create",
        "studies": "study",
    }
    if token in irregular:
        return irregular[token]
    for suffix in ("ments", "ment", "ations", "ation", "ingly", "edly", "ing", "ed", "ies", "es", "s"):
        if token.endswith(suffix) and len(token) > len(suffix) + 3:
            stem = token[: -len(suffix)]
            return f"{stem}y" if suffix == "ies" else stem
    return token


def _normalized_sentence(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _material_sentences(value: str) -> list[str]:
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", value)
        if sentence.strip()
    ]


def _requires_applicant_claim(sentence: str) -> bool:
    lowered = sentence.lower()
    observed = set(re.findall(r"[a-z]+", lowered))
    applicant_subject = bool(
        re.search(
            r"\b(?:i|me|my|we|our|candidate|candidates|applicant|applicants|professional)\b",
            lowered,
        )
    )
    explicit_job_subject = bool(
        re.search(
            r"\b(?:role|position|job|program|internship|posting|employer|company|team)\b",
            lowered,
        )
    )
    if explicit_job_subject and not applicant_subject:
        return False
    broad_assertion_terms = HIGH_RISK_CLAIM_TERMS | {
        "applicant",
        "applicants",
        "background",
        "bring",
        "capabilities",
        "capability",
        "candidate",
        "candidates",
        "experience",
        "expertise",
        "knowhow",
        "offer",
        "possess",
        "professional",
        "skill",
        "skills",
        "strength",
        "strengths",
        "student",
    }
    if observed & broad_assertion_terms:
        return True
    if not applicant_subject:
        return False
    if observed & APPLICANT_ASSERTION_TERMS:
        return True
    if re.search(
        r"\bi\s+(?:have|bring|possess|offer|built|created|developed|led|managed|use|used)\b",
        lowered,
    ):
        return True
    if re.search(r"\bi\s+am\s+(?:a|an|skilled|proficient|experienced)\b", lowered):
        return True
    if re.search(r"\b(?:my|our)\s+(?!application\b|interest\b)", lowered):
        return True
    if re.search(r"\bme\b", lowered) and not re.search(r"\b(?:contact|thank)\s+me\b", lowered):
        return True
    non_assertive_patterns = (
        r"\bi\s+am\s+(?:applying|interested|excited|seeking|grateful)\b",
        r"\bi\s+(?:look\s+forward|welcome)\b",
        r"\bi\s+would\s+welcome\b",
        r"\bthank\s+you\s+for\s+considering\s+my\s+application\b",
        r"\bcontact\s+me\b",
    )
    if any(re.search(pattern, lowered) for pattern in non_assertive_patterns):
        return False
    return True


def _employment_claim_mentions_company(value: str, company: str) -> bool:
    employment_signal = bool(
        re.search(
            r"\b(?:experience\s+(?:at|with)|worked\s+(?:at|for|with)|"
            r"employed\s+(?:at|by)|interned\s+(?:at|for|with))\b",
            value,
            re.I,
        )
    )
    return employment_signal and bool(_claim_tokens(company) & _claim_tokens(value))


def _unsupported_named_entities(value: str, *, allowed_text: str) -> set[str]:
    allowed = {
        token.lower().strip(".-")
        for token in re.findall(r"\b[A-Za-z][A-Za-z0-9+#.-]*\b", allowed_text)
    }
    observed: set[str] = set()
    for match in re.finditer(
        r"\b(?:[A-Z]{2,}|[A-Z][a-z][A-Za-z0-9+#.-]*)\b",
        value,
    ):
        prefix = value[: match.start()].rstrip()
        if not prefix or prefix.endswith((".", "!", "?", "\n")):
            continue
        observed.add(match.group(0).lower().strip(".-"))
    return {
        token
        for token in observed
        if token and token not in allowed and token not in SAFE_NAMED_ENTITY_TERMS
    }


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ChatGPTContractError(f"missing required field: {key}")
    return value


def _reject_extra_keys(
    payload: dict[str, Any],
    allowed: set[str],
    *,
    surface: str,
) -> None:
    extras = sorted(set(payload) - allowed)
    if extras:
        raise ChatGPTContractError(f"unexpected {surface} fields: {extras}")


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

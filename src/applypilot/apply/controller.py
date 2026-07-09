"""Deterministic browser controller for autonomous applications.

The controller owns navigation, form discovery, filling, upload, submit gates,
and artifact capture. Codex is only used as a narrow fallback resolver for
required fields that deterministic mapping cannot answer.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot import config
from applypilot.apply import onepassword
from applypilot.apply.field_resolver import (
    CodexResolver,
    FieldSpec,
    ResolvedField,
    detect_ats,
    field_value_for,
    needs_llm_fallback,
    split_name,
)
from applypilot.apply.harness import HarnessSettings
from applypilot.apply.safety import (
    classify_page_state,
    classify_page_state_with_evidence,
    inspect_page_state,
)
from applypilot.apply.submission import is_probable_submit_response, verify_submission

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


__all__ = [
    "CodexResolver",
    "ControllerResult",
    "DeterministicApplyController",
    "FieldSpec",
    "ResolvedField",
    "classify_page_state",
    "field_value_for",
    "first_email",
    "is_email_only_posting",
    "run_deterministic_controller",
    "split_name",
]


class RequiredFieldUnresolved(RuntimeError):
    """Raised when a required field cannot be safely resolved."""


@dataclass
class ControllerResult:
    """Result returned to the launcher."""

    status: str
    duration_ms: int
    reason: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    verification_confidence: str = ""

    def launcher_status(self) -> str:
        if self.status == "failed" and self.reason:
            return f"failed:{self.reason}"
        return self.status


def is_email_only_posting(text: str) -> bool:
    """Detect postings that require email submission rather than a web form."""
    lower = text.lower()
    return bool(EMAIL_RE.search(text)) and any(
        phrase in lower
        for phrase in (
            "email your resume",
            "send your resume",
            "send your cv",
            "apply by email",
            "email resume",
        )
    )


def first_email(text: str) -> str:
    """Return the first email in text, or an empty string."""
    match = EMAIL_RE.search(text)
    return match.group(0) if match else ""


class DeterministicApplyController:
    """Run one job application using deterministic browser automation."""

    def __init__(
        self,
        *,
        job: dict,
        port: int,
        worker_dir: Path,
        settings: HarnessSettings,
        dry_run: bool = False,
        onepassword_client: onepassword.OnePasswordClient | None = None,
    ) -> None:
        self.job = job
        self.port = port
        self.worker_dir = worker_dir
        self.settings = settings
        self.dry_run = dry_run
        self.profile = config.load_profile()
        self.events: list[str] = []
        self.artifacts: dict[str, str] = {}
        self._secrets: list[str] = []
        self._op = (
            onepassword_client
            or (
                onepassword.OnePasswordClient(vault=settings.onepassword_vault)
                if settings.uses_onepassword
                else None
            )
        )
        self._resolver = CodexResolver(model=settings.executor_model, worker_dir=worker_dir)

    def run(self) -> ControllerResult:
        """Execute the deterministic apply flow."""
        start = time.time()
        try:
            self._preflight()
            uploads = self._prepare_uploads()
            return self._run_browser(uploads=uploads, start=start)
        except RequiredFieldUnresolved:
            return self._finish(
                "failed",
                start,
                reason="required_field_unresolved",
                verification_confidence="failed_closed",
            )
        except Exception as exc:
            self._record(f"controller error: {exc}")
            return self._finish(
                "failed",
                start,
                reason=str(exc)[:80],
                verification_confidence="failed_closed",
            )

    def _preflight(self) -> None:
        if self.settings.uses_onepassword and self.settings.allow_account_creation:
            if self._op is None:
                raise RuntimeError("onepassword_required_for_account_creation")
            self._op.require_ready()
        if self.settings.uses_google_password_manager:
            self._record(
                "using Google Password Manager via Chrome profile; credentials are browser-managed"
            )

    def _run_browser(self, *, uploads: dict[str, str], start: float) -> ControllerResult:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is required for deterministic apply controller.") from exc

        target_url = self.job.get("application_url") or self.job.get("url")
        if not target_url:
            return self._finish("failed", start, reason="missing_url")

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1000)

            state = inspect_page_state(page)
            verdict = classify_page_state_with_evidence(state)
            if verdict:
                self._record(f"stop gate {verdict.reason}: {verdict.evidence}")
                self._capture(page, "blocked")
                return self._finish(
                    "failed",
                    start,
                    reason=verdict.reason,
                    verification_confidence="failed_closed",
                )

            if is_email_only_posting(state.text):
                self._write_email_draft(state.text, uploads)
                self._capture(page, "email-draft")
                return self._finish(
                    "email_draft",
                    start,
                    reason="email_only",
                    verification_confidence="failed_closed",
                )

            credential: onepassword.OnePasswordLogin | None = None
            if self._has_login_or_account_form(page):
                credential = self._credential_for_page(page)
                self._fill_login_or_account(page, credential)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except PlaywrightTimeoutError:
                    pass
                state = inspect_page_state(page)
                verdict = classify_page_state_with_evidence(state)
                if verdict:
                    self._record(f"stop gate {verdict.reason}: {verdict.evidence}")
                    self._capture(page, "login-blocked")
                    return self._finish(
                        "failed",
                        start,
                        reason=verdict.reason,
                        verification_confidence="failed_closed",
                    )

            filled = self._fill_application_form(page, uploads=uploads, credential=credential)
            self._record(f"filled {filled} deterministic field(s)")
            self._capture(page, "review")
            if filled == 0:
                return self._finish(
                    "failed",
                    start,
                    reason="no_fillable_form",
                    verification_confidence="failed_closed",
                )

            if self.dry_run:
                return self._finish(
                    "dry_run_verified",
                    start,
                    reason="dry_run_verified",
                    verification_confidence="dry_run",
                )

            before_submit_url = page.url
            clicked, response = self._click_submit_and_capture_response(page)
            if not clicked:
                return self._finish(
                    "failed",
                    start,
                    reason="submit_button_not_found",
                    verification_confidence="failed_closed",
                )
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(2000)
            state = inspect_page_state(page)
            verdict = classify_page_state_with_evidence(state)
            if verdict:
                self._record(f"stop gate {verdict.reason}: {verdict.evidence}")
                self._capture(page, "submit-blocked")
                return self._finish(
                    "failed",
                    start,
                    reason=verdict.reason,
                    verification_confidence="failed_closed",
                )
            self._capture(page, "submitted")
            verification = verify_submission(page, response=response, before_url=before_submit_url)
            self._record(
                f"submission verification {verification.status}: "
                f"{verification.reason}; evidence={list(verification.evidence)}"
            )
            if verification.status == "submitted_confirmed":
                if credential and credential.pending and self._op:
                    self._op.mark_created(credential.item_id)
                return self._finish(
                    "applied",
                    start,
                    reason="submitted_confirmed",
                    verification_confidence=verification.confidence,
                )
            if verification.status == "submitted_unconfirmed":
                return self._finish(
                    "submitted_unconfirmed",
                    start,
                    reason="submitted_unconfirmed",
                    verification_confidence=verification.confidence,
                )
            return self._finish(
                "failed",
                start,
                reason=verification.reason,
                verification_confidence=verification.confidence,
            )

    def _prepare_uploads(self) -> dict[str, str]:
        personal = self.profile.get("personal", {})
        name_slug = str(personal.get("full_name", "Candidate")).replace(" ", "_")
        resume_path = self.job.get("tailored_resume_path")
        if not resume_path:
            raise RuntimeError("No tailored resume path for job.")
        resume_pdf = Path(resume_path).with_suffix(".pdf")
        if not resume_pdf.exists():
            raise RuntimeError(f"Resume PDF not found: {resume_pdf}")
        resume_out = self.worker_dir / f"{name_slug}_Resume.pdf"
        shutil.copy2(resume_pdf, resume_out)
        uploads = {"resume": str(resume_out)}

        cover_path = self.job.get("cover_letter_path")
        if cover_path:
            cover_pdf = Path(cover_path).with_suffix(".pdf")
            if cover_pdf.exists():
                cover_out = self.worker_dir / f"{name_slug}_Cover_Letter.pdf"
                shutil.copy2(cover_pdf, cover_out)
                uploads["cover_letter"] = str(cover_out)
        return uploads

    def _credential_for_page(self, page: Any) -> onepassword.OnePasswordLogin | None:
        if self.settings.uses_google_password_manager:
            domain = onepassword.domain_from_url(page.url)
            self._record(
                f"using Google Password Manager browser autofill for {domain}; "
                "no password values are read or generated"
            )
            return None
        if not self.settings.allow_account_creation:
            raise RuntimeError("account_required")
        if not self.settings.uses_onepassword or self._op is None:
            raise RuntimeError("credential_provider_required_for_account_creation")

        email = str(self.profile.get("personal", {}).get("email", ""))
        domain = onepassword.domain_from_url(page.url)
        existing = self._op.find_login(domain=domain, username=email)
        if existing:
            self._secrets.append(existing.password)
            self._record(f"using existing 1Password login for {domain}")
            return existing

        title = onepassword.build_login_title(
            domain=domain,
            company=self.job.get("site"),
            email=email,
        )
        login = self._op.create_login(
            title=title,
            domain=domain,
            username=email,
            login_url=page.url,
            job_url=str(self.job.get("url") or ""),
            application_url=str(self.job.get("application_url") or ""),
            run_id=self.worker_dir.name,
        )
        self._secrets.append(login.password)
        self._record(f"created pending 1Password login for {domain}")
        return login

    def _fill_application_form(
        self,
        page: Any,
        *,
        uploads: dict[str, str],
        credential: onepassword.OnePasswordLogin | None,
    ) -> int:
        count = 0
        for _ in range(3):
            filled_this_pass = 0
            for spec in self._collect_fields(page):
                if self._field_already_satisfied(spec):
                    continue
                if spec.type == "file":
                    path = (
                        uploads["cover_letter"]
                        if "cover" in spec.haystack and "cover_letter" in uploads
                        else uploads["resume"]
                    )
                    try:
                        page.locator(spec.selector).set_input_files(path, timeout=5000)
                        count += 1
                        filled_this_pass += 1
                    except Exception:
                        self._record(f"file upload failed for {spec.selector}")
                        if spec.required:
                            raise RequiredFieldUnresolved("required file upload failed")
                    continue

                resolved = field_value_for(spec, profile=self.profile, job=self.job, credential=credential)
                if resolved is None and needs_llm_fallback(spec):
                    resolved = self._resolver.resolve_field(spec, profile=self.profile, job=self.job)
                if resolved is None or resolved.value in ("", None):
                    if spec.required:
                        self._record(
                            "required field unresolved: "
                            f"selector={spec.selector} name={spec.name!r} label={spec.accessible_name!r}"
                        )
                        raise RequiredFieldUnresolved("required field unresolved")
                    continue

                try:
                    locator = page.locator(spec.selector)
                    if spec.type == "checkbox" and isinstance(resolved.value, bool):
                        locator.set_checked(resolved.value, timeout=5000)
                    elif spec.type == "radio":
                        if not self._radio_matches(spec, str(resolved.value)):
                            continue
                        locator.set_checked(True, timeout=5000)
                    elif spec.tag == "select":
                        locator.select_option(label=str(resolved.value), timeout=5000)
                    else:
                        locator.fill(str(resolved.value), timeout=5000)
                    if resolved.sensitive:
                        self._secrets.append(str(resolved.value))
                    self._record(
                        f"filled {spec.selector} from {resolved.source} "
                        f"confidence={resolved.confidence:.2f}"
                    )
                    count += 1
                    filled_this_pass += 1
                except Exception:
                    self._record(f"fill failed for {spec.selector}")
                    if spec.required:
                        raise RequiredFieldUnresolved("required field fill failed")
            if filled_this_pass == 0:
                break
        return count

    def _field_already_satisfied(self, spec: FieldSpec) -> bool:
        if spec.type in {"checkbox", "radio", "file"} or spec.tag == "select":
            return False
        return bool(spec.value and spec.value.strip())

    @staticmethod
    def _radio_matches(spec: FieldSpec, value: str) -> bool:
        desired = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
        current = re.sub(
            r"[^a-z0-9]+",
            " ",
            " ".join([spec.value, spec.label, spec.accessible_name]).lower(),
        ).strip()
        return bool(desired and (desired == current or desired in current.split()))

    def _fill_login_or_account(self, page: Any, credential: onepassword.OnePasswordLogin | None) -> None:
        filled = self._fill_application_form(page, uploads={"resume": ""}, credential=credential)
        self._record(f"filled {filled} login/account field(s)")
        if not self._click_button_by_text(page, ("continue", "next", "sign in", "log in", "create account", "sign up")):
            self._record("no login/account continuation button found")

    def _has_login_or_account_form(self, page: Any) -> bool:
        text = self._page_text(page).lower()
        if any(word in text for word in ("sign in", "log in", "create account", "sign up")):
            return True
        return page.locator('input[type="password"]').count() > 0

    def _collect_fields(self, page: Any) -> list[FieldSpec]:
        script = """
        () => {
          const fields = Array.from(document.querySelectorAll('input, textarea, select'));
          const visible = fields.filter((el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
          });
          const labelFor = (el) => {
            const id = el.getAttribute('id') || '';
            let ariaLabelledbyText = '';
            const labelledBy = el.getAttribute('aria-labelledby') || '';
            if (labelledBy) {
              ariaLabelledbyText = labelledBy.split(/\\s+/).map((part) => {
                const ref = document.getElementById(part);
                return ref ? (ref.innerText || ref.textContent || '') : '';
              }).join(' ').trim();
            }
            const ariaLabel = el.getAttribute('aria-label') || '';
            let label = '';
            if (id) {
              const labelEl = document.querySelector(`label[for="${CSS.escape(id)}"]`);
              if (labelEl) label = labelEl.innerText || labelEl.textContent || '';
            }
            if (!label) {
              const parentLabel = el.closest('label');
              if (parentLabel) label = parentLabel.innerText || parentLabel.textContent || '';
            }
            return {id, ariaLabelledbyText, ariaLabel, label};
          };
          const groupLabelFor = (el) => {
            const fieldset = el.closest('fieldset');
            if (fieldset) {
              const legend = fieldset.querySelector('legend');
              if (legend) return legend.innerText || legend.textContent || '';
            }
            const group = el.closest('[role="radiogroup"]');
            if (group) return group.getAttribute('aria-label') || group.innerText || '';
            return '';
          };
          return visible.map((el, idx) => {
            el.setAttribute('data-applypilot-field', String(idx));
            const labels = labelFor(el);
            const groupLabel = groupLabelFor(el);
            const options = el.tagName.toLowerCase() === 'select'
              ? Array.from(el.options).map((o) => o.text || o.value)
              : ((el.getAttribute('type') || '').toLowerCase() === 'radio' && el.getAttribute('name')
                ? Array.from(document.querySelectorAll(`input[type="radio"][name="${CSS.escape(el.getAttribute('name'))}"]`))
                    .map((radio) => {
                      const radioLabels = labelFor(radio);
                      return radioLabels.label || radio.getAttribute('value') || '';
                    })
                : []);
            const attributes = {};
            for (const attr of el.attributes) {
              if (attr.name.startsWith('data-') || ['min', 'max', 'pattern', 'maxlength'].includes(attr.name)) {
                attributes[attr.name] = attr.value;
              }
            }
            return {
              selector: `[data-applypilot-field="${idx}"]`,
              tag: el.tagName.toLowerCase(),
              type: (el.getAttribute('type') || el.tagName).toLowerCase(),
              name: el.getAttribute('name') || labels.id || '',
              label: [groupLabel, labels.label].filter(Boolean).join(' '),
              placeholder: el.getAttribute('placeholder') || '',
              value: el.value || '',
              required: Boolean(el.required || el.getAttribute('aria-required') === 'true'),
              options,
              autocomplete: el.getAttribute('autocomplete') || '',
              inputmode: el.getAttribute('inputmode') || '',
              role: el.getAttribute('role') || '',
              aria_label: labels.ariaLabel,
              aria_labelledby_text: labels.ariaLabelledbyText,
              title: el.getAttribute('title') || '',
              accept: el.getAttribute('accept') || '',
              data_automation_id: el.getAttribute('data-automation-id') || '',
              attributes,
            };
          });
        }
        """
        raw_fields = page.evaluate(script)
        specs: list[FieldSpec] = []
        ats = detect_ats(str(self.job.get("application_url") or self.job.get("url") or getattr(page, "url", "")))
        for item in raw_fields:
            if not isinstance(item, dict):
                continue
            specs.append(
                FieldSpec(
                    selector=str(item.get("selector") or ""),
                    tag=str(item.get("tag") or ""),
                    type=str(item.get("type") or ""),
                    name=str(item.get("name") or ""),
                    label=str(item.get("label") or ""),
                    placeholder=str(item.get("placeholder") or ""),
                    value=str(item.get("value") or ""),
                    required=bool(item.get("required")),
                    options=tuple(str(opt) for opt in item.get("options", [])),
                    autocomplete=str(item.get("autocomplete") or ""),
                    inputmode=str(item.get("inputmode") or ""),
                    role=str(item.get("role") or ""),
                    aria_label=str(item.get("aria_label") or ""),
                    aria_labelledby_text=str(item.get("aria_labelledby_text") or ""),
                    title=str(item.get("title") or ""),
                    accept=str(item.get("accept") or ""),
                    data_automation_id=str(item.get("data_automation_id") or ""),
                    ats=ats,
                    attributes={
                        str(k): str(v)
                        for k, v in (item.get("attributes") or {}).items()
                    },
                )
            )
        return specs

    def _click_submit(self, page: Any) -> bool:
        return self._click_button_by_text(
            page,
            ("submit application", "submit", "apply", "send application", "finish"),
        )

    def _click_submit_and_capture_response(self, page: Any) -> tuple[bool, Any | None]:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        except ImportError:
            PlaywrightTimeoutError = TimeoutError

        locator = self._button_locator_by_text(
            page,
            ("submit application", "submit", "apply", "send application", "finish"),
        )
        if locator is None:
            return False, None

        clicked = False
        try:
            with page.expect_response(is_probable_submit_response, timeout=15000) as response_info:
                locator.click(timeout=5000)
                clicked = True
            return True, response_info.value
        except PlaywrightTimeoutError:
            return clicked, None
        except Exception:
            if clicked:
                return True, None
            return False, None

    def _click_button_by_text(self, page: Any, labels: tuple[str, ...]) -> bool:
        locator = self._button_locator_by_text(page, labels)
        if locator is None:
            return False
        locator.click(timeout=5000)
        return True

    def _button_locator_by_text(self, page: Any, labels: tuple[str, ...]) -> Any | None:
        for label in labels:
            locator = page.get_by_role("button", name=re.compile(label, re.I))
            if locator.count():
                return locator.first
        for label in labels:
            locator = page.locator(
                f'input[type="submit" i][value*="{label}" i], button:has-text("{label}")'
            )
            if locator.count():
                return locator.first
        return None

    def _page_text(self, page: Any) -> str:
        try:
            return page.locator("body").inner_text(timeout=5000)
        except Exception:
            return ""

    def _write_email_draft(self, page_text: str, uploads: dict[str, str]) -> None:
        email = first_email(page_text)
        personal = self.profile.get("personal", {})
        full_name = personal.get("full_name", "Candidate")
        title = self.job.get("title", "the role")
        attachments = [uploads["resume"]]
        if uploads.get("cover_letter"):
            attachments.append(uploads["cover_letter"])
        body = (
            f"To: {email}\n"
            f"Subject: Application for {title} -- {full_name}\n"
            f"Attachments: {json.dumps(attachments)}\n\n"
            f"Hello,\n\nI am applying for {title}. My tailored resume is attached for review.\n\n"
            f"Best,\n{full_name}\n"
        )
        path = self.worker_dir / "email_application_draft.md"
        path.write_text(body, encoding="utf-8")
        self.artifacts["email_draft"] = str(path)
        self._record(f"wrote email draft for {email or 'unknown recipient'}")

    def _capture(self, page: Any, name: str) -> None:
        path = self.worker_dir / f"{name}.png"
        try:
            page.screenshot(path=str(path), full_page=True, timeout=10000)
            self.artifacts[f"screenshot_{name}"] = str(path)
        except Exception:
            self._record(f"screenshot failed: {name}")

    def _record(self, message: str) -> None:
        self.events.append(onepassword.redact_text(message, self._secrets))

    def _finish(
        self,
        status: str,
        start: float,
        *,
        reason: str = "",
        verification_confidence: str = "",
    ) -> ControllerResult:
        duration_ms = int((time.time() - start) * 1000)
        result = ControllerResult(
            status=status,
            duration_ms=duration_ms,
            reason=reason,
            artifacts=self.artifacts,
            evidence=self.events,
            verification_confidence=verification_confidence,
        )
        payload = {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "reason": reason,
            "verification_confidence": verification_confidence,
            "duration_ms": duration_ms,
            "job": {
                "url": self.job.get("url"),
                "application_url": self.job.get("application_url"),
                "title": self.job.get("title"),
                "site": self.job.get("site"),
            },
            "artifacts": self.artifacts,
            "events": self.events,
        }
        path = self.worker_dir / "deterministic_controller_result.json"
        path.write_text(
            json.dumps(onepassword.redact_data(payload, self._secrets), indent=2),
            encoding="utf-8",
        )
        result.artifacts["controller_result"] = str(path)
        return result


def run_deterministic_controller(
    *,
    job: dict,
    port: int,
    worker_dir: Path,
    settings: HarnessSettings,
    dry_run: bool,
) -> ControllerResult:
    """Convenience wrapper used by the launcher."""
    return DeterministicApplyController(
        job=job,
        port=port,
        worker_dir=worker_dir,
        settings=settings,
        dry_run=dry_run,
    ).run()

"""Deterministic browser controller for autonomous applications.

The controller owns navigation, form discovery, filling, upload, submit gates,
and artifact capture. Codex is only used as a narrow fallback resolver for
required fields that deterministic mapping cannot answer.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot import config
from applypilot.apply import onepassword
from applypilot.apply.harness import HarnessSettings


STOP_PATTERNS: dict[str, tuple[str, ...]] = {
    "sso_required": (
        "login.microsoftonline.com",
        "accounts.google.com",
        "okta.com",
        "saml",
        "single sign-on",
        "single sign on",
        "sign in with google",
        "sign in with microsoft",
    ),
    "unsafe_permissions": (
        "allow camera",
        "allow microphone",
        "screen sharing",
        "share your screen",
        "enable location",
    ),
    "unsafe_verification": (
        "video interview",
        "record a video",
        "selfie",
        "face verification",
        "government id",
        "identity verification",
        "biometric",
    ),
    "payment_or_tax_info": (
        "social security number",
        "ssn",
        "bank account",
        "routing number",
        "credit card",
        "payment information",
    ),
    "mfa_required": (
        "multi-factor",
        "two-factor",
        "2fa",
        "verification code",
        "check your email",
        "email verification",
        "passkey",
    ),
}

SUCCESS_PATTERNS = (
    "application submitted",
    "application received",
    "thank you for applying",
    "thanks for applying",
    "we received your application",
    "your application has been submitted",
)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


@dataclass(frozen=True)
class FieldSpec:
    """A browser form field discovered by the controller."""

    selector: str
    tag: str
    type: str
    name: str = ""
    label: str = ""
    placeholder: str = ""
    value: str = ""
    required: bool = False
    options: tuple[str, ...] = ()

    @property
    def haystack(self) -> str:
        return " ".join(
            [self.name, self.label, self.placeholder, self.type, " ".join(self.options)]
        ).lower()


@dataclass(frozen=True)
class ResolvedField:
    """A deterministic value for a field."""

    value: str | bool
    sensitive: bool = False
    source: str = "deterministic"


@dataclass
class ControllerResult:
    """Result returned to the launcher."""

    status: str
    duration_ms: int
    reason: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)

    def launcher_status(self) -> str:
        if self.status == "failed" and self.reason:
            return f"failed:{self.reason}"
        return self.status


def classify_page_state(url: str, text: str) -> str | None:
    """Return a fail-closed result reason for known unsafe page states."""
    combined = f"{url}\n{text}".lower()
    for reason, patterns in STOP_PATTERNS.items():
        if any(pattern in combined for pattern in patterns):
            return reason
    if "captcha" in combined or "cloudflare" in combined:
        return "captcha"
    if "job is no longer available" in combined or "no longer accepting applications" in combined:
        return "expired"
    return None


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


def split_name(full_name: str) -> tuple[str, str]:
    """Split a full name into first and last for form filling."""
    parts = [p for p in full_name.split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def field_value_for(
    spec: FieldSpec,
    *,
    profile: dict,
    job: dict,
    credential: onepassword.OnePasswordLogin | None = None,
) -> ResolvedField | None:
    """Resolve a form field from profile/job/credential facts."""
    personal = profile.get("personal", {})
    work_auth = profile.get("work_authorization", {})
    compensation = profile.get("compensation", {})
    availability = profile.get("availability", {})
    eeo = profile.get("eeo_voluntary", {})
    first, last = split_name(str(personal.get("full_name", "")))
    h = spec.haystack

    if spec.type in {"hidden", "submit", "button", "reset", "image"}:
        return None
    if "password" in h or spec.type == "password":
        if credential and credential.password:
            return ResolvedField(credential.password, sensitive=True, source="1password")
        return None
    if "email" in h:
        return ResolvedField(str(personal.get("email", "")))
    if "first" in h or "given" in h:
        return ResolvedField(first)
    if "last" in h or "surname" in h or "family" in h:
        return ResolvedField(last)
    if "full name" in h or h.strip() in {"name", "your name"}:
        return ResolvedField(str(personal.get("full_name", "")))
    if "phone" in h or "mobile" in h:
        return ResolvedField(str(personal.get("phone", "")))
    if "street" in h or "address" in h:
        return ResolvedField(str(personal.get("address", "")))
    if "city" in h:
        return ResolvedField(str(personal.get("city", "")))
    if "state" in h or "province" in h:
        return ResolvedField(str(personal.get("province_state", "")))
    if "zip" in h or "postal" in h:
        return ResolvedField(str(personal.get("postal_code", "")))
    if "country" in h:
        return ResolvedField(str(personal.get("country", "")))
    if "linkedin" in h:
        return ResolvedField(str(personal.get("linkedin_url", "")))
    if "github" in h:
        return ResolvedField(str(personal.get("github_url", "")))
    if "portfolio" in h:
        return ResolvedField(str(personal.get("portfolio_url", "")))
    if "website" in h:
        return ResolvedField(str(personal.get("website_url", "")))
    if "salary" in h or "compensation" in h or "pay expectation" in h:
        return ResolvedField(str(compensation.get("salary_expectation", "")))
    if "start date" in h or "available" in h:
        return ResolvedField(str(availability.get("earliest_start_date", "Immediately")))
    if "authorized" in h and "work" in h:
        return ResolvedField("Yes" if work_auth.get("legally_authorized_to_work") else "No")
    if "sponsor" in h or "visa" in h:
        return ResolvedField("Yes" if work_auth.get("require_sponsorship") else "No")
    if "gender" in h:
        return ResolvedField(str(eeo.get("gender", "Decline to self-identify")))
    if "race" in h or "ethnicity" in h:
        return ResolvedField(str(eeo.get("race_ethnicity", "Decline to self-identify")))
    if "veteran" in h:
        return ResolvedField(str(eeo.get("veteran_status", "Decline to self-identify")))
    if "disability" in h:
        return ResolvedField(str(eeo.get("disability_status", "Decline to self-identify")))
    if "position" in h or "role" in h:
        return ResolvedField(str(job.get("title", "")))
    if spec.type == "checkbox":
        if any(word in h for word in ("privacy", "terms", "certify", "agree", "consent")):
            return ResolvedField(True)
        return None
    return None


class CodexResolver:
    """Narrow Codex fallback for ambiguous required fields."""

    def __init__(self, *, model: str, worker_dir: Path) -> None:
        self.model = model
        self.worker_dir = worker_dir

    def resolve_field(self, spec: FieldSpec, *, profile: dict, job: dict) -> ResolvedField | None:
        """Ask Codex for one field value and parse a small JSON response."""
        prompt = {
            "task": "Resolve one job application field using only provided facts. Return JSON only.",
            "field": asdict(spec),
            "job": {
                "title": job.get("title"),
                "site": job.get("site"),
                "url": job.get("application_url") or job.get("url"),
            },
            "profile_facts": {
                "personal": profile.get("personal", {}),
                "work_authorization": profile.get("work_authorization", {}),
                "compensation": profile.get("compensation", {}),
                "availability": profile.get("availability", {}),
                "eeo_voluntary": profile.get("eeo_voluntary", {}),
            },
            "rules": [
                "Do not invent facts.",
                "For EEO questions, prefer decline/self-identify answers.",
                "If the answer cannot be determined, return null.",
            ],
            "response_schema": {"value": "string or boolean or null"},
        }
        cmd = [
            "codex",
            "exec",
            "--model",
            self.model,
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--skip-git-repo-check",
            "--cd",
            str(self.worker_dir),
            "-",
        ]
        result = subprocess.run(
            cmd,
            input=json.dumps(prompt),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return None
        for line in reversed(result.stdout.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            value = payload.get("value")
            if isinstance(value, str | bool):
                return ResolvedField(value, source="codex")
        return None


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
        self._op = onepassword_client or onepassword.OnePasswordClient(
            vault=settings.onepassword_vault
        )
        self._resolver = CodexResolver(model=settings.executor_model, worker_dir=worker_dir)

    def run(self) -> ControllerResult:
        """Execute the deterministic apply flow."""
        start = time.time()
        try:
            self._preflight()
            uploads = self._prepare_uploads()
            return self._run_browser(uploads=uploads, start=start)
        except Exception as exc:
            self._record(f"controller error: {exc}")
            return self._finish("failed", start, reason=str(exc)[:80])

    def _preflight(self) -> None:
        if self.settings.onepassword_enabled and self.settings.allow_account_creation:
            self._op.require_ready()

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

            text = self._page_text(page)
            stop_reason = classify_page_state(page.url, text)
            if stop_reason:
                self._capture(page, "blocked")
                return self._finish("failed", start, reason=stop_reason)

            if is_email_only_posting(text):
                self._write_email_draft(text, uploads)
                self._capture(page, "email-draft")
                return self._finish("email_draft", start, reason="email_only")

            credential: onepassword.OnePasswordLogin | None = None
            if self._has_login_or_account_form(page):
                credential = self._credential_for_page(page)
                self._fill_login_or_account(page, credential)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except PlaywrightTimeoutError:
                    pass
                text = self._page_text(page)
                stop_reason = classify_page_state(page.url, text)
                if stop_reason:
                    self._capture(page, "login-blocked")
                    return self._finish("failed", start, reason=stop_reason)

            filled = self._fill_application_form(page, uploads=uploads, credential=credential)
            self._record(f"filled {filled} deterministic field(s)")
            self._capture(page, "review")
            if filled == 0:
                return self._finish("failed", start, reason="no_fillable_form")

            if self.dry_run:
                return self._finish("applied", start, reason="dry_run_verified")

            if not self._click_submit(page):
                return self._finish("failed", start, reason="submit_button_not_found")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(2000)
            text = self._page_text(page)
            stop_reason = classify_page_state(page.url, text)
            if stop_reason:
                self._capture(page, "submit-blocked")
                return self._finish("failed", start, reason=stop_reason)
            self._capture(page, "submitted")
            if any(pattern in text.lower() for pattern in SUCCESS_PATTERNS):
                if credential and credential.pending:
                    self._op.mark_created(credential.item_id)
                return self._finish("applied", start, reason="confirmation_detected")
            return self._finish("failed", start, reason="no_confirmation")

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

    def _credential_for_page(self, page: Any) -> onepassword.OnePasswordLogin:
        if not self.settings.allow_account_creation:
            raise RuntimeError("account_required")
        if not self.settings.onepassword_enabled:
            raise RuntimeError("onepassword_required_for_account_creation")

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
        for spec in self._collect_fields(page):
            if spec.type == "file":
                path = uploads["cover_letter"] if "cover" in spec.haystack and "cover_letter" in uploads else uploads["resume"]
                try:
                    page.locator(spec.selector).set_input_files(path, timeout=5000)
                    count += 1
                except Exception:
                    self._record(f"file upload failed for {spec.selector}")
                continue

            resolved = field_value_for(spec, profile=self.profile, job=self.job, credential=credential)
            if resolved is None and spec.required:
                resolved = self._resolver.resolve_field(spec, profile=self.profile, job=self.job)
            if resolved is None or resolved.value in ("", None):
                continue

            try:
                locator = page.locator(spec.selector)
                if spec.type == "checkbox" and isinstance(resolved.value, bool):
                    locator.set_checked(resolved.value, timeout=5000)
                elif spec.tag == "select":
                    locator.select_option(label=str(resolved.value), timeout=5000)
                else:
                    locator.fill(str(resolved.value), timeout=5000)
                if resolved.sensitive:
                    self._secrets.append(str(resolved.value))
                count += 1
            except Exception:
                self._record(f"fill failed for {spec.selector}")
        return count

    def _fill_login_or_account(self, page: Any, credential: onepassword.OnePasswordLogin) -> None:
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
          return visible.map((el, idx) => {
            el.setAttribute('data-applypilot-field', String(idx));
            const id = el.getAttribute('id') || '';
            let label = '';
            if (id) {
              const labelEl = document.querySelector(`label[for="${CSS.escape(id)}"]`);
              if (labelEl) label = labelEl.innerText || '';
            }
            if (!label) {
              const parentLabel = el.closest('label');
              if (parentLabel) label = parentLabel.innerText || '';
            }
            const options = el.tagName.toLowerCase() === 'select'
              ? Array.from(el.options).map((o) => o.text || o.value)
              : [];
            return {
              selector: `[data-applypilot-field="${idx}"]`,
              tag: el.tagName.toLowerCase(),
              type: (el.getAttribute('type') || el.tagName).toLowerCase(),
              name: el.getAttribute('name') || id || '',
              label,
              placeholder: el.getAttribute('placeholder') || '',
              value: el.value || '',
              required: Boolean(el.required || el.getAttribute('aria-required') === 'true'),
              options,
            };
          });
        }
        """
        raw_fields = page.evaluate(script)
        specs: list[FieldSpec] = []
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
                )
            )
        return specs

    def _click_submit(self, page: Any) -> bool:
        return self._click_button_by_text(
            page,
            ("submit application", "submit", "apply", "send application", "finish"),
        )

    def _click_button_by_text(self, page: Any, labels: tuple[str, ...]) -> bool:
        for label in labels:
            locator = page.get_by_role("button", name=re.compile(label, re.I))
            if locator.count():
                locator.first.click(timeout=5000)
                return True
        for label in labels:
            locator = page.locator(
                f'input[type="submit" i][value*="{label}" i], button:has-text("{label}")'
            )
            if locator.count():
                locator.first.click(timeout=5000)
                return True
        return False

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

    def _finish(self, status: str, start: float, *, reason: str = "") -> ControllerResult:
        duration_ms = int((time.time() - start) * 1000)
        result = ControllerResult(
            status=status,
            duration_ms=duration_ms,
            reason=reason,
            artifacts=self.artifacts,
            evidence=self.events,
        )
        payload = {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "reason": reason,
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

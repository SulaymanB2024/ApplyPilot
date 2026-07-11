"""Fail-closed safety predicates for application pages."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


SSO_DOMAINS = (
    "accounts.google.com",
    "login.microsoftonline.com",
    "okta.com",
    "auth0.com",
    "sso.cisco.com",
)

CAPTCHA_DOMAINS = (
    "challenges.cloudflare.com",
    "google.com",
    "recaptcha.net",
    "hcaptcha.com",
    "arkoselabs.com",
    "funcaptcha.com",
)

IDV_DOMAINS = (
    "withpersona.com",
    "persona.com",
    "onfido.com",
    "jumio.com",
    "id.me",
    "veriff.com",
)

PAYMENT_AUTOCOMPLETE_TOKENS = {"cc-name", "cc-number", "cc-exp", "cc-exp-month", "cc-exp-year", "cc-csc"}
MFA_AUTOCOMPLETE_TOKENS = {"one-time-code", "webauthn"}
EXPIRED_MARKERS = ("job is no longer available", "no longer accepting applications")
UNSAFE_PERMISSION_MARKERS = (
    "allow camera",
    "allow microphone",
    "screen sharing",
    "share your screen",
    "enable location",
)


@dataclass(frozen=True)
class PageInput:
    """A structural descriptor for one input-like control."""

    selector: str = ""
    type: str = ""
    name: str = ""
    label: str = ""
    autocomplete: str = ""
    accept: str = ""
    required: bool = False

    @property
    def accessible_text(self) -> str:
        """Return text useful for structural field-name checks."""
        return " ".join([self.name, self.label]).lower()


@dataclass(frozen=True)
class PageState:
    """Minimal page state used by safety gates."""

    url: str
    text: str = ""
    inputs: tuple[PageInput, ...] = ()
    iframe_origins: tuple[str, ...] = ()
    inspection_error: str = ""


@dataclass(frozen=True)
class SafetyVerdict:
    """A fail-closed safety decision with audit evidence."""

    reason: str
    evidence: str


def classify_page_state(
    state_or_url: PageState | str,
    text: str = "",
    *,
    inputs: tuple[PageInput, ...] = (),
    iframe_origins: tuple[str, ...] = (),
    allow_password: bool = False,
) -> str | None:
    """Return a fail-closed result reason for known unsafe page states.

    The string signature is kept for backwards compatibility with existing tests.
    Prefer ``classify_page_state_with_evidence(PageState(...))`` for new code.
    """
    if isinstance(state_or_url, PageState):
        state = state_or_url
    else:
        state = PageState(url=state_or_url, text=text, inputs=inputs, iframe_origins=iframe_origins)
    verdict = classify_page_state_with_evidence(state, allow_password=allow_password)
    return verdict.reason if verdict else None


def classify_page_state_with_evidence(
    state: PageState,
    *,
    allow_password: bool = False,
) -> SafetyVerdict | None:
    """Return a fail-closed verdict and evidence for unsafe page states."""
    if state.inspection_error:
        return SafetyVerdict("inspection_failed", f"page_state={state.inspection_error[:120]}")
    url_origin = _origin(state.url)
    if _domain_matches(url_origin, SSO_DOMAINS) or "saml" in state.url.lower():
        return SafetyVerdict("sso_required", f"url_origin={url_origin}")

    for iframe_origin in state.iframe_origins:
        host = _origin(iframe_origin)
        if _is_captcha_iframe(host, iframe_origin):
            return SafetyVerdict("captcha", f"iframe_origin={host}")
        if _domain_matches(host, IDV_DOMAINS):
            return SafetyVerdict("unsafe_verification", f"iframe_origin={host}")

    for page_input in state.inputs:
        verdict = _classify_input(page_input, allow_password=allow_password)
        if verdict:
            return verdict

    lower_text = state.text.lower()
    if any(marker in lower_text for marker in EXPIRED_MARKERS):
        return SafetyVerdict("expired", "page_text=expired_marker")
    if any(marker in lower_text for marker in UNSAFE_PERMISSION_MARKERS):
        return SafetyVerdict("unsafe_permissions", "page_text=permission_marker")
    return None


def inspect_page_state(page) -> PageState:
    """Capture the pure safety state from a Playwright page."""
    script = """
    () => {
      const inputEls = Array.from(document.querySelectorAll('input, textarea, select'));
      const inputs = inputEls.map((el, idx) => {
        const id = el.getAttribute('id') || '';
        let label = el.getAttribute('aria-label') || '';
        const labelledBy = el.getAttribute('aria-labelledby') || '';
        if (labelledBy) {
          label = labelledBy.split(/\\s+/).map((part) => {
            const ref = document.getElementById(part);
            return ref ? (ref.innerText || ref.textContent || '') : '';
          }).join(' ').trim() || label;
        }
        if (!label && id) {
          const labelEl = document.querySelector(`label[for="${CSS.escape(id)}"]`);
          if (labelEl) label = labelEl.innerText || labelEl.textContent || '';
        }
        if (!label) {
          const parentLabel = el.closest('label');
          if (parentLabel) label = parentLabel.innerText || parentLabel.textContent || '';
        }
        if (!label) label = el.getAttribute('placeholder') || el.getAttribute('title') || '';
        return {
          selector: id ? `#${CSS.escape(id)}` : `${el.tagName.toLowerCase()}:nth-of-type(${idx + 1})`,
          type: (el.getAttribute('type') || el.tagName).toLowerCase(),
          name: el.getAttribute('name') || id || '',
          label,
          autocomplete: el.getAttribute('autocomplete') || '',
          accept: el.getAttribute('accept') || '',
          required: Boolean(el.required || el.getAttribute('aria-required') === 'true'),
        };
      });
      const iframeOrigins = Array.from(document.querySelectorAll('iframe'))
        .map((frame) => frame.getAttribute('src') || '')
        .filter(Boolean)
        .map((src) => {
          try { return new URL(src, window.location.href).href; }
          catch { return src; }
        });
      return {
        text: document.body ? document.body.innerText || '' : '',
        inputs,
        iframeOrigins,
      };
    }
    """
    inspection_error = ""
    try:
        raw = page.evaluate(script)
    except Exception as exc:
        raw = {"text": "", "inputs": [], "iframeOrigins": []}
        inspection_error = type(exc).__name__
    inputs = tuple(
        PageInput(
            selector=str(item.get("selector") or ""),
            type=str(item.get("type") or ""),
            name=str(item.get("name") or ""),
            label=str(item.get("label") or ""),
            autocomplete=str(item.get("autocomplete") or ""),
            accept=str(item.get("accept") or ""),
            required=bool(item.get("required")),
        )
        for item in raw.get("inputs", [])
        if isinstance(item, dict)
    )
    return PageState(
        url=getattr(page, "url", ""),
        text=str(raw.get("text") or ""),
        inputs=inputs,
        iframe_origins=tuple(str(origin) for origin in raw.get("iframeOrigins", [])),
        inspection_error=inspection_error,
    )


def _classify_input(
    page_input: PageInput,
    *,
    allow_password: bool = False,
) -> SafetyVerdict | None:
    field_type = page_input.type.lower()
    autocomplete_tokens = {token.lower() for token in page_input.autocomplete.split() if token.strip()}
    evidence_id = page_input.selector or page_input.name or page_input.label[:40]
    if field_type == "password" and not allow_password:
        return SafetyVerdict("login_issue", f"input[type=password] selector={evidence_id}")
    if autocomplete_tokens & MFA_AUTOCOMPLETE_TOKENS:
        return SafetyVerdict("mfa_required", f"autocomplete={page_input.autocomplete} selector={evidence_id}")
    if autocomplete_tokens & PAYMENT_AUTOCOMPLETE_TOKENS:
        return SafetyVerdict("payment_or_tax_info", f"autocomplete={page_input.autocomplete} selector={evidence_id}")
    accessible = page_input.accessible_text
    if any(token in accessible for token in ("ssn", "social security", "sin", "tax id", "routing number")):
        return SafetyVerdict("payment_or_tax_info", f"field_label={accessible[:80]}")
    if field_type == "file" and _looks_like_id_upload(page_input):
        return SafetyVerdict("unsafe_verification", f"file_upload={accessible[:80]}")
    return None


def _looks_like_id_upload(page_input: PageInput) -> bool:
    accessible = page_input.accessible_text
    accept = page_input.accept.lower()
    image_file = "image" in accept or ".jpg" in accept or ".jpeg" in accept or ".png" in accept
    id_label = any(
        token in accessible
        for token in (
            "government id",
            "passport",
            "driver license",
            "drivers license",
            "identity document",
            "selfie",
            "face verification",
        )
    )
    return image_file and id_label


def _origin(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.netloc.lower()


def _domain_matches(host: str, domains: tuple[str, ...]) -> bool:
    host = host.lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _is_captcha_iframe(host: str, raw_src: str) -> bool:
    if _domain_matches(host, ("google.com",)) and "recaptcha" not in raw_src.lower():
        return False
    return _domain_matches(host, CAPTCHA_DOMAINS) or "recaptcha" in raw_src.lower()

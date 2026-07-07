"""ApplyPilot configuration: paths, platform detection, user data."""

import os
import platform
import shutil
from copy import deepcopy
from pathlib import Path

from platformdirs import user_data_path

# User data directory — all user-specific files live here
APP_NAME = "ApplyPilot"
APP_AUTHOR = "Pickle-Pixel"
LEGACY_APP_DIR = Path.home() / ".applypilot"
KEYRING_SERVICE = "applypilot"
SECRET_ENV_KEYS = (
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "LLM_API_KEY",
    "CAPSOLVER_API_KEY",
)


def _resolve_app_dir() -> Path:
    """Resolve user data location, preserving the historical default."""
    env_path = os.environ.get("APPLYPILOT_DIR")
    if env_path:
        return Path(env_path).expanduser()
    if LEGACY_APP_DIR.exists():
        return LEGACY_APP_DIR
    return user_data_path(APP_NAME, APP_AUTHOR, ensure_exists=False)


APP_DIR = _resolve_app_dir()

# Core paths
DB_PATH = APP_DIR / "applypilot.db"
PROFILE_PATH = APP_DIR / "profile.json"
RESUME_PATH = APP_DIR / "resume.txt"
RESUME_PDF_PATH = APP_DIR / "resume.pdf"
SEARCH_CONFIG_PATH = APP_DIR / "searches.yaml"
ENV_PATH = APP_DIR / ".env"

# Generated output
TAILORED_DIR = APP_DIR / "tailored_resumes"
COVER_LETTER_DIR = APP_DIR / "cover_letters"
LOG_DIR = APP_DIR / "logs"

# Chrome worker isolation
CHROME_WORKER_DIR = APP_DIR / "chrome-workers"
APPLY_WORKER_DIR = APP_DIR / "apply-workers"

# Package-shipped config (YAML registries)
PACKAGE_DIR = Path(__file__).parent
CONFIG_DIR = PACKAGE_DIR / "config"


def get_chrome_path() -> str:
    """Auto-detect Chrome/Chromium executable path, cross-platform.

    Override with CHROME_PATH environment variable.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    system = platform.system()

    if system == "Windows":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:  # Linux
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    for c in candidates:
        if c and c.exists():
            return str(c)

    # Fall back to PATH search
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Chrome or set CHROME_PATH environment variable."
    )


def get_chrome_user_data() -> Path:
    """Default Chrome user data directory, cross-platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        return Path.home() / ".config" / "google-chrome"


def get_chrome_profile_directory() -> str:
    """Chrome profile directory to launch inside the user-data root."""
    return os.environ.get("APPLYPILOT_CHROME_PROFILE_DIRECTORY", "Default")


def ensure_dirs():
    """Create all required directories."""
    for d in [APP_DIR, TAILORED_DIR, COVER_LETTER_DIR, LOG_DIR, CHROME_WORKER_DIR, APPLY_WORKER_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_profile() -> dict:
    """Load user profile from the ApplyPilot data directory."""
    import json
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"Profile not found at {PROFILE_PATH}. Run `applypilot init` first."
        )
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def _jobspy_country(value: str | None) -> str:
    """Normalize user-facing country labels to JobSpy's country_indeed values."""
    if not value:
        return "usa"
    normalized = value.strip().lower()
    aliases = {
        "us": "usa",
        "u.s.": "usa",
        "u.s.a.": "usa",
        "united states": "usa",
        "united states of america": "usa",
        "usa": "usa",
        "canada": "canada",
        "ca": "canada",
    }
    return aliases.get(normalized, normalized)


def normalize_search_config(raw: dict | None) -> dict:
    """Normalize historical and example search config shapes.

    Older generated configs and the shipped example use user-facing names like
    ``boards`` and ``location.accept_patterns``. Discovery modules read
    ``sites``, ``location_accept``, and ``location_reject_non_remote``. Keep
    both shapes populated so every discovery backend receives the same intent.
    """
    if not raw:
        return {}

    cfg = deepcopy(raw)
    defaults = cfg.setdefault("defaults", {})

    if "sites" not in cfg and cfg.get("boards"):
        cfg["sites"] = list(cfg["boards"])
    if "boards" not in cfg and cfg.get("sites"):
        cfg["boards"] = list(cfg["sites"])

    if "country_indeed" not in defaults:
        defaults["country_indeed"] = _jobspy_country(cfg.get("country", "USA"))

    location = cfg.setdefault("location", {})
    accept_patterns = location.get("accept_patterns") or cfg.get("location_accept") or []
    reject_patterns = location.get("reject_patterns") or cfg.get("location_reject_non_remote") or []

    cfg.setdefault("location_accept", list(accept_patterns))
    cfg.setdefault("location_reject_non_remote", list(reject_patterns))
    location.setdefault("accept_patterns", list(cfg.get("location_accept", [])))
    location.setdefault("reject_patterns", list(cfg.get("location_reject_non_remote", [])))

    return cfg


def load_search_config() -> dict:
    """Load search configuration from the ApplyPilot data directory."""
    import yaml
    if not SEARCH_CONFIG_PATH.exists():
        # Fall back to package-shipped example
        example = CONFIG_DIR / "searches.example.yaml"
        if example.exists():
            return normalize_search_config(yaml.safe_load(example.read_text(encoding="utf-8")))
        return {}
    return normalize_search_config(yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8")))


def load_sites_config() -> dict:
    """Load sites.yaml configuration (sites list, manual_ats, blocked, etc.)."""
    import yaml
    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def is_manual_ats(url: str | None) -> bool:
    """Check if a URL routes through an ATS that requires manual application."""
    if not url:
        return False
    sites_cfg = load_sites_config()
    domains = sites_cfg.get("manual_ats", [])
    url_lower = url.lower()
    return any(domain in url_lower for domain in domains)


def load_blocked_sites() -> tuple[set[str], list[str]]:
    """Load blocked sites and URL patterns from sites.yaml.

    Returns:
        (blocked_site_names, blocked_url_patterns)
    """
    cfg = load_sites_config()
    blocked = cfg.get("blocked", {})
    sites = set(blocked.get("sites", []))
    patterns = blocked.get("url_patterns", [])
    return sites, patterns


def load_blocked_sso() -> list[str]:
    """Load blocked SSO domains from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("blocked_sso", [])


def load_base_urls() -> dict[str, str | None]:
    """Load site base URLs for URL resolution from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("base_urls", {})


# ---------------------------------------------------------------------------
# Default values — referenced across modules instead of magic numbers
# ---------------------------------------------------------------------------

DEFAULTS = {
    "min_score": 7,
    "max_apply_attempts": 3,
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    "viewport": "1280x900",
}


def _read_keyring_secret(name: str) -> str | None:
    """Read a secret from the OS keyring if a backend is available."""
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, name)
    except Exception:
        return None


def set_secret(name: str, value: str) -> bool:
    """Store a secret in the OS keyring and mirror it into this process."""
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, name, value)
    except Exception:
        return False
    os.environ[name] = value
    return True


def get_secret(name: str, default: str = "") -> str:
    """Get an environment secret, falling back to the OS keyring."""
    value = os.environ.get(name)
    if value:
        return value
    value = _read_keyring_secret(name)
    if value:
        os.environ[name] = value
        return value
    return default


def load_env():
    """Load environment variables from ApplyPilot config and the OS keyring."""
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    # Also try CWD .env as fallback
    load_dotenv()
    for key in SECRET_ENV_KEYS:
        if not os.environ.get(key):
            value = _read_keyring_secret(key)
            if value:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# Tier system — feature gating by installed dependencies
# ---------------------------------------------------------------------------

TIER_LABELS = {
    1: "Discovery",
    2: "AI Scoring & Tailoring",
    3: "Full Auto-Apply",
}

TIER_COMMANDS: dict[int, list[str]] = {
    1: ["init", "run discover", "run enrich", "status", "dashboard"],
    2: ["run score", "run tailor", "run cover", "run pdf", "run"],
    3: ["apply"],
}


def get_tier() -> int:
    """Detect the current tier based on available dependencies.

    Tier 1 (Discovery):            Python + pip
    Tier 2 (AI Scoring & Tailoring): + LLM API key
    Tier 3 (Full Auto-Apply):       + agent CLI + Chrome
    """
    load_env()

    has_llm = any(get_secret(k) for k in ("GEMINI_API_KEY", "OPENAI_API_KEY")) or bool(os.environ.get("LLM_URL"))
    if not has_llm:
        return 1

    has_agent = shutil.which("claude") is not None or shutil.which("codex") is not None
    try:
        get_chrome_path()
        has_chrome = True
    except FileNotFoundError:
        has_chrome = False

    if has_agent and has_chrome:
        return 3

    return 2


def check_tier(required: int, feature: str) -> None:
    """Raise SystemExit with a clear message if the current tier is too low.

    Args:
        required: Minimum tier needed (1, 2, or 3).
        feature: Human-readable description of the feature being gated.
    """
    current = get_tier()
    if current >= required:
        return

    from rich.console import Console
    _console = Console(stderr=True)

    missing: list[str] = []
    if required >= 2 and not (
        any(get_secret(k) for k in ("GEMINI_API_KEY", "OPENAI_API_KEY"))
        or os.environ.get("LLM_URL")
    ):
        missing.append("LLM API key — run [bold]applypilot init[/bold] or set GEMINI_API_KEY")
    if required >= 3:
        if not shutil.which("claude") and not shutil.which("codex"):
            missing.append("Agent CLI — install Claude Code or Codex CLI")
        try:
            get_chrome_path()
        except FileNotFoundError:
            missing.append("Chrome/Chromium — install or set CHROME_PATH")

    _console.print(
        f"\n[red]'{feature}' requires {TIER_LABELS.get(required, f'Tier {required}')} (Tier {required}).[/red]\n"
        f"Current tier: {TIER_LABELS.get(current, f'Tier {current}')} (Tier {current})."
    )
    if missing:
        _console.print("\n[yellow]Missing:[/yellow]")
        for m in missing:
            _console.print(f"  - {m}")
    _console.print()
    raise SystemExit(1)

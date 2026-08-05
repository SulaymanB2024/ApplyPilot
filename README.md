<!-- logo here -->

> **⚠️ ApplyPilot** is the original open-source project, created by [Pickle-Pixel](https://github.com/Pickle-Pixel) and first published on GitHub on **February 17, 2026**. We are **not affiliated** with applypilot.app, useapplypilot.com, or any other product using the "ApplyPilot" name. These sites are **not associated with this project** and may misrepresent what they offer. If you're looking for the autonomous, open-source job application agent — you're in the right place.

# ApplyPilot

**Applied to 1,000 jobs in 2 days. Fully autonomous. Open source.**

[![PyPI version](https://img.shields.io/pypi/v/applypilot?color=blue)](https://pypi.org/project/applypilot/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Pickle-Pixel/ApplyPilot?style=social)](https://github.com/Pickle-Pixel/ApplyPilot)
[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/S6S01UL5IO)




https://github.com/user-attachments/assets/7ee3417f-43d4-4245-9952-35df1e77f2df


---

## What It Does

ApplyPilot is a 6-stage autonomous job application pipeline. It discovers jobs across 5+ boards plus Runway, scores them against your resume with AI, tailors your resume per job, writes cover letters, and **submits applications for you**. It navigates forms, uploads documents, answers screening questions, all hands-free.

Three commands. That's it.

```bash
pip install 'applypilot[discovery]'
applypilot init          # one-time setup: resume, profile, preferences, API keys
applypilot doctor        # verify your setup — shows what's installed and what's missing
applypilot run           # discover > enrich > score > tailor > cover letters
applypilot run -w 4      # same but parallel (4 threads for discovery/enrichment)
applypilot training-audit  # verify Workday, email draft, Runway, and board coverage
applypilot improve plan --scope apply --out .applypilot-dev/exp-001  # bounded self-improvement packet
applypilot autonomy plan --query "entry-level product and data roles"  # compact local run packet
applypilot autonomy probe-chatgpt --cdp-port 9222  # no-send authenticated browser probe
applypilot autonomy run --query "entry-level product and data roles" --cdp-port 9222 --approved-fact-digest DIGEST  # after reviewing plan facts
applypilot apply         # deterministic browser dry-run; does not submit
applypilot apply --submit --approved-fact-digest DIGEST  # explicit live-submit boundary
applypilot apply --allow-account-creation  # separate per-run account-change permission
applypilot apply -w 3    # parallel apply (3 Chrome instances)
applypilot apply --dry-run  # explicit spelling of the safe default
```

> **Discovery extra:** `applypilot[discovery]` installs JobSpy and its runtime scraping dependencies. If your environment hits a JobSpy resolver conflict, `applypilot doctor` will show the fallback install command.

> **Recommended autonomy path:** `applypilot autonomy` uses ChatGPT Web only for bounded
> role discovery and evidence-cited cover-letter drafting. Eligibility, first-party freshness,
> budgets, and action gates are deterministic. It disables broad job-board aggregators and
> never fills or submits during `autonomy run`. See
> [the diagnosis and operating contract](docs/tool-first-autonomy.md).

---

## Two Paths

### Full Pipeline (recommended)
**Requires:** Python 3.11+ and Chrome. The legacy scoring/tailoring stages require a configured LLM API or local endpoint. The deterministic apply controller does not require Node.js or an agent CLI while its field model-call budget remains zero.

Runs all 6 stages, from job discovery to autonomous application submission. This is the full power of ApplyPilot.

### Discovery + Tailoring Only
**Requires:** Python 3.11+, Gemini API key (free)

Runs stages 1-5: discovers jobs, scores them, tailors your resume, generates cover letters. You submit applications manually with the AI-prepared materials.

---

## The Pipeline

| Stage | What Happens |
|-------|-------------|
| **1. Discover** | Scrapes direct employer/ATS pages (Workday, Greenhouse, Ashby, SmartRecruiters, company careers pages) plus optional JobSpy boards |
| **2. Enrich** | Fetches full job descriptions via JSON-LD, CSS selectors, or AI-powered extraction |
| **3. Score** | AI rates every job 1-10 based on your resume and preferences. Only high-fit jobs proceed |
| **4. Tailor** | AI rewrites your resume per job: reorganizes, emphasizes relevant experience, adds keywords. Never fabricates |
| **5. Cover Letter** | AI generates a targeted cover letter per job |
| **6. Auto-Apply** | A deterministic controller navigates application forms, fills fields, uploads documents, and reviews them; final submission requires `--submit` |

Each stage is independent. Run them all or pick what you need.

---

## ApplyPilot vs The Alternatives

| Feature | ApplyPilot | AIHawk | Manual |
|---------|-----------|--------|--------|
| Job discovery | Direct employer/ATS sources + optional boards/Runway | LinkedIn only | One board at a time |
| AI scoring | 1-10 fit score per job | Basic filtering | Your gut feeling |
| Resume tailoring | Per-job AI rewrite | Template-based | Hours per application |
| Auto-apply | Full form navigation + submission | LinkedIn Easy Apply only | Click, type, repeat |
| Supported sites | Workday, Greenhouse, Ashby, SmartRecruiters, company career pages, optional Indeed/LinkedIn/Glassdoor/ZipRecruiter/Google Jobs/Runway | LinkedIn | Whatever you open |
| License | AGPL-3.0 | MIT | N/A |

---

## Requirements

| Component | Required For | Details |
|-----------|-------------|---------|
| Python 3.11+ | Everything | Core runtime |
| Node.js 18+ | Legacy tooling only | Not required by the deterministic Python controller |
| Gemini API key | Scoring, tailoring, cover letters | Free tier (15 RPM / 1M tokens/day) is enough |
| Chrome/Chromium | Auto-apply | Auto-detected on most systems |
| Codex CLI | Optional field fallback | Not required when `APPLYPILOT_FIELD_MODEL_CALL_BUDGET=0` (the default) |
| Google Password Manager in Chrome | Codex auto-apply login flows | Uses the selected Chrome profile's browser-managed credentials/autofill |
| 1Password CLI + Chrome extension | Legacy Codex account creation | Optional legacy provider when `APPLYPILOT_CREDENTIAL_PROVIDER=onepassword` |

**Gemini API key is free.** Get one at [aistudio.google.com](https://aistudio.google.com). OpenAI and local models (Ollama/llama.cpp) are also supported.

### Optional

| Component | What It Does |
|-----------|-------------|
| OS keyring | Stores API keys outside `.env` on macOS, Windows, and supported Linux desktops |

> **Note:** API keys can live in `.env` or the OS keyring. `applypilot init` asks where to store new keys.

---

## Configuration

All generated by `applypilot init`:

### `profile.json`
Your personal data in one structured file: contact info, work authorization, compensation, experience, skills, resume facts (preserved during tailoring), and EEO defaults. Powers scoring, tailoring, and form auto-fill.

### `searches.yaml`
Job search queries, target titles, locations, discovery mode, direct ATS boards, and optional aggregators. Set `discovery_mode: direct_sources` to skip aggregator boards and focus on employer-owned or ATS-native pages.

### `.env`
API keys and runtime config: `GEMINI_API_KEY`, `LLM_MODEL`, plus harness overrides such as `APPLYPILOT_LLM_PROVIDER=chatgpt_web`, `APPLYPILOT_AGENT_BACKEND`, `APPLYPILOT_EXECUTOR_MODEL`, `APPLYPILOT_SUPERVISOR_MODEL`, `APPLYPILOT_DETERMINISTIC_CONTROLLER`, `APPLYPILOT_FIELD_MODEL_CALL_BUDGET`, `APPLYPILOT_CREDENTIAL_PROVIDER`, and `APPLYPILOT_CHROME_PROFILE_DIRECTORY`. Account creation is intentionally a per-run CLI permission rather than a persistent environment default. `APPLYPILOT_CREDENTIAL_PROVIDER` defaults to `google_password_manager`, which uses the selected Chrome profile's browser-managed credentials without exporting passwords. The field model-call budget defaults to zero, so deterministic auto-apply does not spawn a model subprocess. Legacy 1Password settings remain available with `APPLYPILOT_CREDENTIAL_PROVIDER=onepassword`. The self-improvement development harness uses separate optional settings: `APPLYPILOT_DEV_MODE`, `APPLYPILOT_DEV_WORKER_MODEL`, `APPLYPILOT_DEV_REVIEWER_MODEL`, and `APPLYPILOT_DEV_FORBIDDEN_MODELS`. API secret values can also be stored in the OS keyring.

### Package configs (shipped with ApplyPilot)
- `config/employers.yaml` - Workday employer registry (48 preconfigured)
- `config/sites.yaml` - Direct career/ATS sites, optional discovery boards, blocked sites, base URLs, manual ATS domains
- `config/searches.example.yaml` - Example search configuration

---

## How Stages Work

### Discover
Scrapes Workday employer portals from `employers.yaml`, configured Greenhouse/Lever/Ashby boards from `direct_ats_sources`, and smart-extract direct sources from `sites.yaml`. When `searches.yaml` sets `discovery_mode: direct_sources`, ApplyPilot skips JobSpy and only crawls employer-owned or ATS-native sources. Hybrid mode keeps optional JobSpy boards such as Indeed, LinkedIn, Glassdoor, ZipRecruiter, and Google Jobs. Deduplicates by canonical ATS job IDs where available.

### Enrich
Visits each job URL and extracts the full description. 3-tier cascade: JSON-LD structured data, then CSS selector patterns, then AI-powered extraction for unknown layouts.

### Score
AI scores every job 1-10 against your profile. 9-10 = strong match, 7-8 = good, 5-6 = moderate, 1-4 = skip. Only jobs above your threshold proceed to tailoring.

### Tailor
Generates a custom resume per job: reorders experience, emphasizes relevant skills, incorporates keywords from the job description. Your `resume_facts` (companies, projects, metrics) are preserved exactly. The AI reorganizes but never fabricates.

### Cover Letter
Writes a targeted cover letter per job referencing the specific company, role, and how your experience maps to their requirements.

### Auto-Apply
ApplyPilot launches Chrome, writes a deterministic per-job harness contract, navigates each application page, detects the form type, fills personal information and work history, and uploads the tailored resume and cover letter. It stops at review by default; `--submit` is required to authorize the final click. If a role only accepts email applications, the harness writes a local `email_application_draft.md` for user review instead of sending email. A live dashboard shows progress in real-time.

The supported apply path uses a deterministic Python/Playwright controller for navigation, form detection, uploads, submit gates, screenshots, and result parsing. Its model-call budget defaults to zero. If `APPLYPILOT_FIELD_MODEL_CALL_BUDGET` is explicitly raised, Codex is used once per page as a schema-constrained batch fallback for ambiguous required fields or screening questions that the controller cannot resolve from profile facts. The legacy free-form Claude/Codex controller is disabled. CAPTCHA, MFA, SSO, payment/tax, and identity-verification surfaces fail closed instead of attempting bypass.

By default, Codex apply runs use Google Password Manager through the selected Chrome profile. ApplyPilot never reads, exports, prints, or persists Google-stored passwords; it allows Chrome autofill to satisfy login fields and fails closed if a required password field is not already satisfied. New account creation is not treated as a credential-write API for Google Password Manager. The legacy 1Password path remains available with `APPLYPILOT_CREDENTIAL_PROVIDER=onepassword`, `op`, and the 1Password Chrome extension. API keys stay in `.env` or the OS keyring.

The apply queue stores canonical job IDs to avoid duplicate submissions, schedules retryable failures with capped full-jitter backoff, and opens a per-domain circuit breaker after repeated fail-closed outcomes such as CAPTCHA or SSO blocks. Each run records `deterministic_controller_plan.json`, `apply_harness_contract.json`, `apply_training_manifest.json`, screenshots, and a redacted `deterministic_controller_result.json` in the worker directory.

```bash
# Utility modes (no Chrome/agent needed)
applypilot training-audit             # audit Workday/email/Runway/board training coverage
applypilot apply --mark-applied URL    # manually mark a job as applied
applypilot apply --mark-failed URL     # manually mark a job as failed
applypilot apply --reset-failed        # reset all failed jobs for retry
```

---

## CLI Reference

```
applypilot init                         # First-time setup wizard
applypilot doctor                       # Verify setup, diagnose missing requirements
applypilot run [stages...]              # Run pipeline stages (or 'all')
applypilot run --workers 4              # Parallel discovery/enrichment
applypilot run --stream                 # Concurrent stages (streaming mode)
applypilot run --min-score 8            # Override score threshold
applypilot run --dry-run                # Preview without executing
applypilot run --validation lenient     # Relax validation (recommended for Gemini free tier)
applypilot run --validation strict      # Strictest validation (retries on any banned word)
applypilot training-audit               # Audit apply-agent training coverage
applypilot autonomy plan --query QUERY  # Write compact facts, policy, and ChatGPT request artifacts
applypilot autonomy probe-chatgpt       # No-send auth/composer probe on caller-provided CDP Chrome
applypilot autonomy run --query QUERY --approved-fact-digest DIGEST  # Review-only funnel after fact review
applypilot apply                        # Launch deterministic dry-run (safe default)
applypilot apply --submit --approved-fact-digest DIGEST  # Explicit reviewed live-submit boundary
applypilot apply --allow-account-creation # Separate job-site account-creation permission
applypilot apply --workers 3            # Parallel browser workers
applypilot apply --dry-run              # Fill forms without submitting
applypilot apply --continuous           # Run forever, polling for new jobs
applypilot apply --headless             # Headless browser mode
applypilot apply --url URL              # Dry-run a specific job
applypilot apply --agent-backend codex   # Use Codex executor profile
applypilot apply --model gpt-5.5 --agent-backend codex
applypilot apply --supervisor-model gpt-5.5
applypilot improve plan --scope apply --out .applypilot-dev/exp-001
applypilot improve worker --artifact .applypilot-dev/exp-001/plan.json --dry-run
applypilot improve validate --artifact .applypilot-dev/exp-001/plan.json
applypilot improve review --artifact .applypilot-dev/exp-001/proposal.json
applypilot status                       # Pipeline statistics
applypilot dashboard                    # Open HTML results dashboard
```

`applypilot improve` is a bounded development harness, not an auto-patcher. It
writes local artifacts (`plan.json`, prompts, `knowledge_index.json`,
`knowledge_cards/`, `research_queue.json`, `proposal.json`, `results.json`,
`review.json`, and `decision.md`) so a worker can propose changes and a reviewer
can gate them against deterministic checks. The v1 worker is dry-run only,
forbids recursive delegation, keeps file reads scoped to declared allowlists,
and rejects `gpt-5.3-codex-spark` by default through
`APPLYPILOT_DEV_FORBIDDEN_MODELS`.

The improve harness uses progressive reveal for Codex context. Worker prompts
receive a compact knowledge index first; full case cards are opened only when a
task matches their `when_to_open` trigger. Broader research needs go into
`research_queue.json` for ChatGPT Web or manual research, then come back as
curated source-backed cards instead of raw transcripts.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and PR guidelines.

---

## License

ApplyPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE).

You are free to use, modify, and distribute this software. If you deploy a modified version as a service, you must release your source code under the same license.

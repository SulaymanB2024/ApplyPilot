<!-- logo here -->

> **⚠️ ApplyPilot** is the original open-source project, created by [Pickle-Pixel](https://github.com/Pickle-Pixel) and first published on GitHub on **February 17, 2026**. We are **not affiliated** with applypilot.app, useapplypilot.com, or any other product using the "ApplyPilot" name. These sites are **not associated with this project** and may misrepresent what they offer. If you're looking for the autonomous, open-source job application agent — you're in the right place.

# ApplyPilot

**Evidence-bound job discovery and application execution. Open source.**

[![PyPI version](https://img.shields.io/pypi/v/applypilot?color=blue)](https://pypi.org/project/applypilot/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Pickle-Pixel/ApplyPilot?style=social)](https://github.com/Pickle-Pixel/ApplyPilot)
[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/S6S01UL5IO)




https://github.com/user-attachments/assets/7ee3417f-43d4-4245-9952-35df1e77f2df


---

## What It Does

ApplyPilot discovers current first-party roles, rejects ineligible or irrelevant
postings, prepares truthful materials, dry-runs visible application forms, and
executes only an exact user-approved batch. Submission is never inferred from a
click: confirmed outcomes require durable ATS evidence.

The supported workflow is:

```bash
pip install 'applypilot[discovery]'
applypilot init
applypilot profile-cache
applypilot doctor --strict
applypilot aggregate --query "paid Summer 2027 product and analytics internships in Austin or Remote US" \
  --term "product intern" --term "data analyst intern" --mode quick --watch
applypilot prepare --query "paid Summer 2027 product and analytics internships in Austin or Remote US" \
  --aggregation-snapshot AGGREGATION_RUN_ID@REVISION
applypilot dry-run --run-id RUN_ID --candidate CANDIDATE_ID
applypilot approve --run-id RUN_ID --candidate CANDIDATE_ID --max-submissions 1 \
  --applicant-confirmation "I confirm the named applicant certification and privacy-policy agreement."
applypilot execute --approval-id APPROVAL_ID
```

> **Discovery extra:** `applypilot[discovery]` installs JobSpy and its runtime scraping dependencies. If your environment hits a JobSpy resolver conflict, `applypilot doctor` will show the fallback install command.

See [the canonical workflow contract](docs/CANONICAL_WORKFLOW.md) for exact
commands, browser handoffs, approval semantics, resumability, and evidence
requirements. The older `run`, `apply`, `autonomy`, and `campaign` commands remain
compatibility and diagnostic surfaces; they do not own new workflow state.

---

## Canonical and legacy paths

### Canonical workflow (recommended)
**Requires:** Python 3.11+, the applicant's authenticated visible Chrome session,
applicant-confirmed form facts and preferred locations before form work, and an
explicit exact-batch approval before submission. It does not require an LLM API
key.

Runs discovery, verification, deterministic ranking, evidence-bound materials,
visible form dry-runs, exact approval, and evidence-gated execution through one
resumable candidate store.

Progressive aggregation publishes a useful immutable first revision from cache
and first-party sources without waiting for slower enrichment. JobSpy is bounded
board discovery evidence; Handshake and Runway are serialized model-piloted
discovery missions in the user's authenticated browser, with user takeover for
OTP, passkeys, CAPTCHA, MFA, and provider challenges on those portal-discovery
missions. They are not hidden-API or bulk scraper integrations. Later validated
results produce digest-linked revisions; the canonical workflow binds one exact
`RUN_ID@REVISION`. Approval-bound application handoffs use the separate email-OTP
policy described below.

Search configuration never counts as applicant consent to work in a location.
Dry-run and submission packets bind one private confirmed-fact snapshot; missing
screening answers abstain, and fact drift after review stops execution.

Visible-browser application packets also bind a narrow intervention policy.
Browser-managed login and dismissal of non-permission password-manager popups
are model-actionable without exposing credentials. Routine authentication is
non-interactive by default: the worker may use Google Password Manager's inline
Chrome UI to autofill a saved login or generate and save a new site password,
and may retrieve one current email OTP through read-only mailbox access. It must
return a structured blocker instead of asking the applicant for authentication
or takeover. Use `--no-autonomous-auth` to turn account creation and email OTP off.
Certification and privacy acceptance still require exact applicant-confirmed
text. Passwords and OTPs never enter response artifacts. CAPTCHA, passkeys,
authenticator/SMS or other non-email MFA, identity verification, payment/tax
data, mailbox writes, and ambiguous retries remain fail-closed.

### Legacy six-stage pipeline
**Requires:** Python 3.11+ and a configured Gemini, OpenAI, or local endpoint for
legacy scoring and rewriting.

The older `applypilot run` stages remain for compatibility. They are not the
supported source of truth for new application outcomes.

---

## Legacy stage reference

| Stage | What Happens |
|-------|-------------|
| **1. Discover** | Scrapes direct employer/ATS pages (Workday, Greenhouse, Ashby, SmartRecruiters, company careers pages) plus optional JobSpy boards |
| **2. Enrich** | Fetches full job descriptions via JSON-LD, CSS selectors, or AI-powered extraction |
| **3. Score** | AI rates every job 1-10 based on your resume and preferences. Only high-fit jobs proceed |
| **4. Tailor** | Legacy AI rewrite retained for compatibility; canonical runs use exact-claim bullet reordering with provenance |
| **5. Cover Letter** | AI generates a targeted cover letter per job |
| **6. Auto-Apply** | A deterministic controller navigates application forms, fills fields, uploads documents, and reviews them; final submission requires `--submit` |

Each stage is independent. Run them all or pick what you need.

---

## ApplyPilot vs The Alternatives

| Feature | ApplyPilot | AIHawk | Manual |
|---------|-----------|--------|--------|
| Job discovery | Progressive first-party sources + bounded boards + browser-mediated Handshake/Runway | LinkedIn only | One board at a time |
| AI scoring | 1-10 fit score per job | Basic filtering | Your gut feeling |
| Resume tailoring | Evidence-bound role ordering; no invented claims | Template-based | Hours per application |
| Auto-apply | Visible dry-run, exact batch approval, durable outcome | LinkedIn Easy Apply only | Click, type, repeat |
| Supported sites | Workday, Greenhouse, Ashby, SmartRecruiters, company careers, bounded Indeed/Google/ZipRecruiter, browser-mediated Handshake/Runway | LinkedIn | Whatever you open |
| License | AGPL-3.0 | MIT | N/A |

---

## Requirements

| Component | Required For | Details |
|-----------|-------------|---------|
| Python 3.11+ | Everything | Core runtime |
| Node.js 18+ | Legacy tooling only | Not required by the deterministic Python controller |
| Gemini API key | Legacy scoring, tailoring, cover letters | Not required by the canonical workflow |
| Chrome/Chromium | Auto-apply | Auto-detected on most systems |
| Codex CLI | Optional field fallback | Not required when `APPLYPILOT_FIELD_MODEL_CALL_BUDGET=0` (the default) |
| Google Password Manager in Chrome | Codex auto-apply login flows | Uses the selected Chrome profile's browser-managed credentials/autofill |
| 1Password CLI + Chrome extension | Deprecated legacy compatibility only | Canonical application handoffs use Google Password Manager exclusively |

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

Role ranking treats the order of supported families in `experience.target_role` as a
priority signal. Optional `preferences.target_companies`,
`compensation.salary_range_min`, and `compensation.hourly_rate_min` add local quality
signals. Applicant compensation preferences remain outside ChatGPT Web prompts; only
compensation stated by a role source is carried into the shortlist and workflow log.

For each verified role, the ChatGPT Web material contract may select existing applicant
evidence IDs and exact phrases from the verified job description as resume emphasis. The
local renderer can use that strategy only to reorder unchanged source bullet lines. Its
provenance file records the source, job, output, selected evidence IDs, selected job terms,
and whether the exact source-line multiset was preserved.

Default Codex routing uses Terra at medium effort for truth-sensitive field fallback,
Terra at high effort for review, and Luna at medium effort for mechanically validated
development work. ChatGPT Web receives the prompt only: ApplyPilot does not request a
model or reasoning level on that surface. A visible Web model label may be logged as an
observation, but it is never treated as a routing guarantee.

Before fit scoring, ApplyPilot classifies each discovered surface as a verified posted
employment requisition, a general-interest application, speculative outreach, a gig/task
platform, a profile/assessment, or unknown. Raw discoveries remain labeled leads. Only a
provider requisition, `JobPosting` structured data, or a visible job-specific application
surface can enter the posted-job shortlist; general-interest forms and speculative outreach
use separate evidence, authorization, receipt, and counter paths. An empty discovery result
is valid and is never padded with weak matches.

`applypilot profile-cache` reports which resume-backed and applicant-owned facts
are ready or missing without printing the stored values. Run
`applypilot profile-cache --collect` to answer only missing applicant-owned
questions; the command is resumable, keeps the profile at mode `0600`, and
creates a private backup before replacing it. Resume-derived facts are reused
automatically; applicant-owned answers remain cached until changed.
Answers based on older or uncertain records can be retained under
`autofill.pending_answers`; the status report names their field paths without
printing their values, and they are not used for autofill until confirmed.
Recurring custom questions can be stored under
`autofill.custom_answers` with an exact question, optional exact aliases, and
optional ATS or employer-domain scope. Custom answers never use fuzzy matching,
and legal attestations, credentials, identity documents, payment, and tax fields
are rejected from this cache.

### `searches.yaml`
Job search queries, target titles, locations, discovery mode, direct ATS boards,
optional aggregators, and bounded progressive-aggregation defaults. Repeated
`applypilot aggregate --term` options are the entire provider query set; the
overlay never silently expands all configured queries. Set
`discovery_mode: direct_sources` to skip aggregator boards and focus on
employer-owned or ATS-native pages.

### `.env`
API keys and runtime config: `GEMINI_API_KEY`, `LLM_MODEL`, plus harness overrides such as `APPLYPILOT_LLM_PROVIDER=chatgpt_web`, `APPLYPILOT_AGENT_BACKEND`, `APPLYPILOT_EXECUTOR_MODEL`, `APPLYPILOT_SUPERVISOR_MODEL`, `APPLYPILOT_DETERMINISTIC_CONTROLLER`, `APPLYPILOT_FIELD_MODEL_CALL_BUDGET`, and `APPLYPILOT_CHROME_PROFILE_DIRECTORY`. Canonical account creation uses Google Password Manager exclusively. The field model-call budget defaults to zero, so deterministic auto-apply does not spawn a model subprocess. The old `APPLYPILOT_CREDENTIAL_PROVIDER=onepassword` path is deprecated and retained only for legacy compatibility. The self-improvement development harness uses separate optional settings: `APPLYPILOT_DEV_MODE`, `APPLYPILOT_DEV_WORKER_MODEL`, `APPLYPILOT_DEV_REVIEWER_MODEL`, and `APPLYPILOT_DEV_FORBIDDEN_MODELS`. API secret values can also be stored in the OS keyring.

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
The legacy pipeline asks an API model to reorganize source-supported resume
content and now fails closed when its factuality judge rejects an output. New
workflow runs instead use deterministic exact-line reordering with a provenance
artifact.

### Cover Letter
Writes a targeted cover letter per job referencing the specific company, role, and how your experience maps to their requirements.

### Legacy Auto-Apply compatibility
The legacy controller launches Chrome, writes a deterministic per-job harness contract, navigates each application page, detects the form type, fills personal information and work history, and uploads the tailored resume and cover letter. It stops at review by default; `--submit` is required to authorize the final click. If a role only accepts email applications, the harness writes a local `email_application_draft.md` for user review instead of sending email. A live dashboard shows progress in real-time. New canonical runs use the visible-Chrome requests documented in `docs/CANONICAL_WORKFLOW.md` instead.

The compatibility apply path uses a deterministic Python/Playwright controller for navigation, form detection, uploads, submit gates, screenshots, and result parsing. Its model-call budget defaults to zero. If `APPLYPILOT_FIELD_MODEL_CALL_BUDGET` is explicitly raised, Codex is used once per page as a schema-constrained batch fallback for ambiguous required fields or screening questions that the controller cannot resolve from profile facts. The fallback explicitly uses Codex approval policy `never` inside a read-only sandbox, so it does not pause for permission or receive write authority. The legacy free-form Claude/Codex controller is disabled. CAPTCHA, MFA, SSO, payment/tax, and identity-verification surfaces fail closed instead of attempting bypass.

By default, Codex apply runs use Google Password Manager through the selected Chrome profile. ApplyPilot never reads, exports, prints, or persists Google-stored passwords. For a permitted account-creation page, the controller focuses the password field, accepts Chrome's inline generated-password suggestion, verifies only that the password fields became populated, and activates the site's continuation once. It fails closed if Chrome does not fill the fields or the account gate does not advance. The old 1Password path is deprecated legacy compatibility. API keys stay in `.env` or the OS keyring.

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

Canonical commands are listed first. Everything below the compatibility marker
is retained for existing users and diagnostics; it does not own new workflow
candidate or submission state.

```
applypilot init                         # First-time setup wizard
applypilot profile-cache                # Show value-free autofill cache completeness
applypilot doctor                       # Verify setup, diagnose missing requirements
applypilot prepare --query QUERY        # Discover, verify, rank, and prepare
applypilot workflow-status --run-id ID  # Inspect canonical state and shortlist
applypilot dry-run --run-id ID [--no-autonomous-auth]
applypilot approve --run-id ID --candidate CANDIDATE_ID [INTERVENTION OPTIONS]
applypilot execute --approval-id ID      # Resume one approved submission at a time
applypilot aggregate --query QUERY --term TERM --mode quick --watch
applypilot aggregate-status --run-id ID --watch --json
applypilot prepare --query QUERY --aggregation-snapshot RUN_ID@REVISION
applypilot opportunities discover --signal recently-funded --signal actively-hiring --watch
applypilot opportunities list --status verified --limit 25 --json
applypilot opportunities draft LEAD_ID --channel email  # local only; no send
applypilot opportunities review-draft DRAFT_ID

# Legacy compatibility and diagnostic commands
applypilot run [stages...]              # Run pipeline stages (or 'all')
applypilot run --workers 4              # Parallel discovery/enrichment
applypilot run --stream                 # Concurrent stages (streaming mode)
applypilot run --min-score 8            # Override score threshold
applypilot run --dry-run                # Preview without executing
applypilot run --validation lenient     # Legacy compatibility mode; not used by canonical workflow
applypilot run --validation strict      # Strictest validation (retries on any banned word)
applypilot training-audit               # Audit apply-agent training coverage
applypilot autonomy plan --query QUERY  # Write compact facts, policy, and ChatGPT request artifacts
applypilot autonomy status --run-dir RUN_DIR
applypilot autonomy heartbeat --run-dir RUN_DIR
applypilot autonomy status --latest
applypilot autonomy heartbeat --latest
applypilot autonomy status --latest --compact
applypilot autonomy heartbeat --latest --compact
applypilot autonomy observe-runtime --latest [RUNTIME OPTIONS]
applypilot autonomy advance --run-dir RUN_DIR --approved-fact-digest DIGEST
applypilot autonomy import-response --request REQUEST --input RESPONSE
applypilot autonomy prepare-fact-approval --run-dir RUN_DIR --approved-fact-digest DIGEST [OPTIONS]
applypilot autonomy import-fact-approval --run-dir RUN_DIR --approved-fact-digest DIGEST \
  --attestation FILE --signature FILE.sig
applypilot campaign create --run-dir RUN_DIR --approved-fact-digest DIGEST --campaign-id ID
applypilot campaign create --run-dir RUN_DIR --approved-fact-digest DIGEST --campaign-id ID \
  --submit
applypilot campaign status --campaign-dir CAMPAIGN_DIR
applypilot campaign heartbeat --campaign-dir CAMPAIGN_DIR
applypilot campaign status --campaign-dir CAMPAIGN_DIR --compact
applypilot campaign heartbeat --campaign-dir CAMPAIGN_DIR --compact
applypilot campaign observe-runtime --campaign-dir CAMPAIGN_DIR [RUNTIME OPTIONS]
applypilot autonomy probe-chatgpt --allow-legacy-cdp  # Explicit legacy CDP compatibility probe
applypilot autonomy run --query QUERY --approved-fact-digest DIGEST --allow-legacy-cdp
applypilot apply                        # Launch deterministic dry-run (safe default)
applypilot apply --url URL --approved-fact-digest DIGEST  # Dry-run and mint one-time manifest
applypilot apply --url URL --submit --approved-fact-digest DIGEST --authorization-manifest PATH
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

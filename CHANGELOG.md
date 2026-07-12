# Changelog

All notable changes to ApplyPilot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Tool-first ChatGPT Web autonomy** - Added a strict JSON-in/JSON-out adapter,
  first-party role verification, compact fact packs, review-only form inspection, and
  `applypilot autonomy plan|advance|import-response|probe-chatgpt|run` commands.
- **Portable browser handoff queue** - Added manifest-bound, request-ID-bound ChatGPT Web
  artifacts so an authenticated browser tool can service at most three small model calls
  without exposing browser credentials or spawning a nested model process.
- **Bounded funnel telemetry** - Added hard per-run budgets, privacy-preserving usage
  ledgers, deterministic eligibility/freshness gates, and no-progress circuit breakers.
- **Applicant fact ledger** - Added versioned profile/resume snapshots and explicit
  confirmed, unknown, and rejected fact states with correction enforcement.
- **Signed live fact approval** - Added detached OpenSSH verification over exact run, source,
  profile/resume, challenge, and fact-value bindings. The campaign controller can verify and
  import approval but cannot mint it from a readable digest or message ID. Live verification
  uses a fixed root-protected trust store and pinned system executables rather than caller paths.
- **Durable campaign ledger** - Added an immutable target-100 manifest, serialized writer lease,
  crash-recoverable event/state projection, canonical candidate dedupe, typed submission
  evidence, fail-closed unknown-outcome reconciliation, and bounded five-minute heartbeats.
- **Apply harness hardening** - Added structured field resolution, structural safety gates,
  and tri-state submission verification for the deterministic apply controller.
- **Apply runtime guards** - Added canonical job IDs, retry scheduling with full jitter,
  and per-domain circuit breakers for repeated fail-closed apply outcomes.
- **Self-improvement harness** - Added `applypilot improve` plan, worker, and review
  commands for bounded artifact-first development loops with model and safety guardrails.
- **Progressive knowledge packets** - Added compact knowledge indexes, full case cards,
  and ChatGPT Web research queues for retrieval-gated Codex worker context.

### Changed
- **Tool-free deterministic default** - Auto-apply now defaults to the deterministic
  controller with zero model subprocess calls; a Codex executable is required only when
  `APPLYPILOT_FIELD_MODEL_CALL_BUDGET` is greater than zero.
- **Explicit submit boundary** - `applypilot apply` is now dry-run by default; live
  submission is one exact URL and requires a reviewed fact digest plus an expiring, one-time
  manifest bound to the candidate, material bytes, filled form, and apply policy.
- **Explicit account boundary** - Job-site account creation is disabled by default and
  requires the per-run `--allow-account-creation` flag.
- **Discovery source policy** - The autonomy funnel uses ChatGPT Web first and permits
  direct ATS fallback only after a recorded primary failure; broad aggregators are disabled.
- **Resumable autonomy contract** - Reviewed facts, context, policy, query, run ID, prompts,
  and imported responses are digest-bound; missing responses remain pending and stale,
  swapped, or one-sided edits inconsistent with the receipt fail closed. Receipts are not
  claimed to resist a hostile local writer who can modify both files.
- **Rich model context** - ChatGPT Web receives up to 72 confirmed, contact-free facts plus
  structured work samples, results, skills, preferences, and longer verified job evidence.
  The v2 context pack now carries up to 72 confirmed facts into discovery, reorders the complete
  bounded evidence set for each verified role, and still excludes contact details, demographics,
  secrets, unknowns, and rejected claims. Calls may research and deliberate without a fixed time
  deadline while final artifacts remain strict, bounded JSON.
- **Handoff and evidence hardening** - Dynamic query/job inputs now bind request receipts,
  semantically rejected material can be corrected after quarantine, fact approvals bind source
  hashes, shared ATS tenants bind to employers, and form-review success rejects unrelated URLs,
  challenges, logins, account creation, unknown fields, and field-value aliases.
- **Structured applicant claims** - Material prompt schema v3 separates applicant assertions
  from job evidence. Every first-person or possessive assertion must exactly match a structured
  claim supported by applicant facts; `JOB` evidence cannot establish an applicant skill. Final
  letters are rejected above four paragraphs, 450 words, or 20 structured applicant claims.
- **Autonomy-aware doctor** - `doctor --autonomy` validates artifact transport and the required
  contact, work-authorization, sponsorship, and availability facts without requiring a legacy
  model API key; optional corrections use `--autonomy-corrections`.
- **Provider error honesty** - Sequential and streaming discovery retain structured
  provider errors instead of converting failed coverage into a successful empty result.
- **Training audit semantics** - Zero JobSpy boards are N/A only in `direct_sources`
  mode and fail the audit in hybrid or job-board modes.
- **CAPTCHA policy** - Auto-apply now fails closed on CAPTCHA and anti-bot challenges
  instead of advertising solver APIs or token injection.
- **Dry-run apply semantics** - `applypilot apply --dry-run` records dry-run verification
  confidence without marking the job applied.
- **Exact-URL retry safety** - Explicit URL acquisition now refuses already-applied,
  permanently failed, and max-attempt jobs instead of bypassing queue retry guards.
- **Codex resolver compatibility** - Field fallback execution now uses the current
  `codex exec` flag surface without the removed approval flag, has no default model deadline,
  and receives only the exact confirmed fact-ledger subset rather than an unchecked raw profile.

## [0.2.0] - 2026-02-17

### Added
- **Parallel workers for discovery/enrichment** - `applypilot run --workers N` enables
  ThreadPoolExecutor-based parallelism for Workday scraping, smart extract, and detail
  enrichment. Default is sequential (1); power users can scale up.
- **Apply utility modes** - `--gen` (generate prompt for manual debugging), `--mark-applied`,
  `--mark-failed`, `--reset-failed` flags on `applypilot apply`
- **Dry-run mode** - `applypilot apply --dry-run` fills forms without clicking Submit
- **5 new tracking columns** - `agent_id`, `last_attempted_at`, `apply_duration_ms`,
  `apply_task_id`, `verification_confidence` for better apply-stage observability
- **Manual ATS detection** - `manual_ats` list in `config/sites.yaml` skips sites with
  unsolvable CAPTCHAs (e.g. TCS iBegin)
- **Qwen3 `/no_think` optimization** - automatically saves tokens when using Qwen models
- **`config.DEFAULTS`** - centralized dict for magic numbers (`min_score`, `max_apply_attempts`,
  `poll_interval`, `apply_timeout`, `viewport`)

### Fixed
- **Config YAML not found after install** - moved `config/` into the package at
  `src/applypilot/config/` so YAML files (employers, sites, searches) ship with `pip install`
- **Search config format mismatch** - wizard wrote `searches:` key but discovery code
  expected `queries:` with tier support. Aligned wizard output and example config
- **JobSpy install isolation** - removed python-jobspy from package dependencies due to
  broken numpy==1.26.3 exact pin in jobspy metadata. Installed separately with `--no-deps`
- **Scoring batch limit** - default limit of 50 silently left jobs unscored across runs.
  Changed to no limit (scores all pending jobs in one pass)
- **Missing logging output** - added `logging.basicConfig(INFO)` so per-job progress for
  scoring, tailoring, and cover letters is visible during pipeline runs

### Changed
- **Blocked sites externalized** - moved from hardcoded sets in launcher.py to
  `config/sites.yaml` under `blocked:` key
- **Site base URLs externalized** - moved from hardcoded dict in detail.py to
  `config/sites.yaml` under `base_urls:` key
- **SSO domains externalized** - moved from hardcoded list in prompt.py to
  `config/sites.yaml` under `blocked_sso:` key
- **Prompt improvements** - screening context uses `target_role` from profile,
  salary section includes `currency_conversion_note` and dynamic hourly rate examples
- **`acquire_job()` fixed** - writes `agent_id` and `last_attempted_at` to proper columns
  instead of misusing `apply_error`
- **`profile.example.json`** - added `currency_conversion_note` and `target_role` fields

## [0.1.0] - 2026-02-17

### Added
- 6-stage pipeline: discover, enrich, score, tailor, cover letter, apply
- Multi-source job discovery: Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs
- Workday employer portal support (46 preconfigured employers)
- Direct career site scraping (28 preconfigured sites)
- 3-tier job description extraction cascade (JSON-LD, CSS selectors, AI fallback)
- AI-powered job scoring (1-10 fit scale with rationale)
- Resume tailoring with factual preservation (no fabrication)
- Cover letter generation per job
- Autonomous browser-based application submission via Playwright
- Interactive setup wizard (`applypilot init`)
- Cross-platform Chrome/Chromium detection (Windows, macOS, Linux)
- Multi-provider LLM support (Gemini, OpenAI, local models via OpenAI-compatible endpoints)
- Pipeline stats and HTML results dashboard
- YAML-based configuration for employers, career sites, and search queries
- Job deduplication across sources
- Configurable score threshold filtering
- Safety limits for maximum applications per run
- Detailed application results logging

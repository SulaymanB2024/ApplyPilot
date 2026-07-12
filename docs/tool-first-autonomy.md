# Tool-first autonomy and second-Mac diagnosis

## Observed failure

A second-Mac run produced activity without useful funnel progress:

- It ran for about 26 minutes and accounted for roughly 19.35 million token events.
- Repeated context grew from about 97,000 to 290,000 input tokens.
- Discovery inserted 66 aggregator-derived rows, but zero roles reached scoring, tailoring,
  cover-letter generation, or application.
- Twenty broad JobSpy combinations encountered repeated 403 and country-validation errors;
  Workday then stalled. No direct ATS sources were configured for that run.
- `APPLYPILOT_LLM_PROVIDER=chatgpt_web` was set, but the legacy LLM client did not implement
  that provider. An outer browser agent performed ad hoc work instead of using the pipeline.
- A nested model subprocess failed because its local host executable was unavailable. The
  outer task then carried full profile and resume content in an ever-growing context.
- Profile and resume snapshots disagreed, and unknown or rejected facts could re-enter later
  generated material.

The primary problem was orchestration, not model quality: the run mixed monitoring,
discovery, browsing, and writing in one long context, while provider failures were not durable
pipeline evidence.

## Repair contract

The `applypilot autonomy` path is a finite, artifact-first coordinator:

1. Build a source-bound fact ledger and a compact context pack from an explicit allowlist of
   confirmed facts.
2. Ask ChatGPT Web for a bounded list of official employer or ATS URLs using strict JSON.
3. Reject senior, experience-ineligible, and known availability-conflicting roles locally.
4. Verify each remaining role against a first-party ATS or employer surface without a model.
5. Ask ChatGPT Web for at most two evidence-cited material packets.
6. Inspect at most one application form without filling, uploading, or submitting.
7. Write a local result ledger with counts, budgets, decision reasons, and usage estimates.

LinkedIn, Indeed, JobSpy, Glassdoor, ZipRecruiter, and Google Jobs are disabled in this path.
Direct ATS discovery is allowed only after a recorded ChatGPT Web failure. Challenge pages and
provider failures are measurement gaps, not evidence that a posting is healthy or closed.

## Hard budgets

Default limits per run are:

| Resource | Limit |
| --- | ---: |
| ChatGPT Web model calls | 3 |
| Discoveries | 10 |
| First-party verifications | 5 |
| Material packets | 2 |
| Read-only form reviews | 1 |
| Browser navigations | 8 |
| External calls | 15 |
| Prompt characters per model call | 40,000 |
| Response characters per model call | 80,000 |
| Model-call time | No fixed deadline |

Telemetry stores hashes, counts, durations, and observed or estimated token fields. It does not
store raw prompts in the usage ledger. Model calls may research and reason for as long as they
need; limits apply to the number and size of calls, not their thinking time. Context is curated
and evidence-rich rather than intentionally sparse: the default pack allows up to 24,000
characters and 72 confirmed facts while excluding contact details, demographics, secrets,
unknowns, and rejected claims. Discovery receives that complete bounded pack. Material calls
preserve every stable evidence ID while putting the facts most relevant to the verified role
first. The acceptance target for a representative end-to-end run is at
least a 90% token reduction from the 19.35-million-token failure baseline. That target must be
measured on a fresh run after the applicant fact ledger is corrected; it is not claimed from
unit tests.

## Fact corrections

Create a corrections file from `fact_corrections.example.json`. It can live anywhere; pass its
exact path rather than assuming the legacy `~/.applypilot` directory. Corrections are applied
to an immutable snapshot; they never silently rewrite `profile.json` or `resume.txt`.

```bash
cp fact_corrections.example.json ~/applypilot-fact-corrections.json
applypilot autonomy plan \
  --query "entry-level product and data roles" \
  --corrections ~/applypilot-fact-corrections.json
```

Review the generated `fact_ledger.json` before allowing browser or model work. Blank,
placeholder, and explicitly rejected facts remain blockers instead of being guessed.
The plan command prints the ledger digest. Copy it only after review; `autonomy run` refuses
to start if the fresh profile, resume, corrections file, or approved digest differs.
That copied digest is an integrity binding for review-only work, not proof that the applicant
approved live use. Live campaign state requires the separately signed approval described below.

## ChatGPT Web operation

The recommended path is a portable request/response queue. `plan` binds the reviewed fact,
context, policy, query, and run digests, then writes one compact discovery request. `advance`
either performs deterministic local work or returns one pending request for the browser agent.
The browser agent sends only that prompt in ChatGPT Web and returns one strict JSON object.
ChatGPT is explicitly encouraged to research deeply, consider the candidate's broader
trajectory and adjacent strengths, and compare multiple approaches internally before emitting
the final object. The strict schema governs the returned artifact, not the depth of reasoning.
ApplyPilot never receives cookies, local storage, passwords, sidebar history, or whole-page text.

```bash
applypilot autonomy advance \
  --run-dir COPY_THE_PLAN_RUN_DIRECTORY_HERE \
  --approved-fact-digest COPY_THE_REVIEWED_PLAN_DIGEST_HERE

# The result names one pending request and its expected response path.
# After the authenticated browser tool returns a bare JSON object:
applypilot autonomy import-response \
  --request COPY_THE_PENDING_REQUEST_PATH_HERE \
  --input COPY_THE_BROWSER_RESPONSE_FILE_HERE

# Repeat until status is review_ready; the hard cap is three ChatGPT calls.
applypilot autonomy advance \
  --run-dir COPY_THE_PLAN_RUN_DIRECTORY_HERE \
  --approved-fact-digest COPY_THE_REVIEWED_PLAN_DIGEST_HERE
```

Each response must echo a request ID that binds the run, stage, query or verified-job input,
candidate, facts, context, policy, and prompt-schema version. Each semantically accepted
response gets a coordinator-enforced hash receipt. Missing response files mean "pending," not
"provider failed," so they cannot silently authorize fallback discovery. A stale, swapped,
one-sided edited, or oversized response fails closed. Semantically rejected material is moved
to a hash-named quarantine so a corrected bounded response can be imported without accepting
the rejected output.
Accepted receipts and hash-named rejection records are restored into the usage ledger on every
`advance`, so resuming the CLI cannot reset the three-model-call budget or make a failed tool
attempt disappear.

The receipt is an integrity/replay control, not a cryptographic defense against a hostile local
writer who can modify both the response and its receipt. Protect the run directory with normal
OS account and filesystem permissions; no documentation or UI should describe these receipts
as signed or tamper-proof.
The browser transport must extract the assistant message's DOM `textContent`; using ChatGPT's
rendered "Copy response" action can Markdown-linkify URLs and corrupt otherwise valid JSON.

Before a signed campaign exists, `autonomy status --run-dir RUN_DIR` derives one redacted status
directly from the immutable run packet. `autonomy heartbeat --run-dir RUN_DIR` fsyncs that same
status to the fixed `heartbeat.json` name for five-minute supervision. It reports fact-state
counts, blocker IDs, preferred-location count, trust/approval state, handoff phase, result counts,
and 0/100 without copying fact values, prompts, URLs, materials, or rejected response contents.
The fixed name is overwritten atomically, so waiting at a human gate does not grow an event log.
Handoff requests and receipts take precedence when deriving the actionable phase. Values copied
from `result_ledger.json` are exact-schema checked but remain explicitly `reported_*` and
`validated_but_mutable_untrusted`; they never count as authoritative submission confirmation.
The heartbeat's embedded SHA-256 detects accidental corruption only; it is not a signature or a
defense against a hostile writer with access to the run directory.
For unattended supervisors, `autonomy status --latest` and `autonomy heartbeat --latest` select
the newest canonical immediate child of the configured `autonomy-runs` directory. Selection uses
the sortable generated run ID rather than mutable filesystem timestamps, validates the selected
manifest, and fails closed when the newest run-shaped directory is incomplete, symlinked, or
disagrees with its manifest. Invalid older history cannot disable a newer valid supervisor run,
but selection never silently falls back past an invalid newest run. Equal newest timestamp
prefixes are treated as ambiguous and require an explicit `--run-dir`.
Add `--compact` for a bounded supervisor view containing only the precedence-resolved action
owner/code, whether a browser is required, 0/100 progress, one stable state fingerprint,
progress age, and poller heartbeat timing. Applicant and system gates always override pending
browser handoffs. Recording another unchanged heartbeat refreshes liveness without resetting
`last_progress_at`, allowing a five-minute controller to compare hashes instead of re-reading or
re-reasoning over nested facts and handoff counts.

`autonomy observe-runtime` and `campaign observe-runtime` let the external Codex supervisor write
one fixed-name `runtime_observation.json`. The exact schema contains only its run/campaign binding,
short TTL, coarse Chronicle state/evidence code, latest-frame timestamp, coarse browser surface,
and authentication readiness. It rejects extra fields and never stores screenshots, window text,
URLs, profile names, or cookies. A `capturing` claim requires an explicitly observed frame no more
than 30 seconds old; `idle_paused` requires the explicit `system_idle_reported` evidence code.
Observations expire within ten minutes and are always labeled `externally_observed_untrusted`.
They are diagnostic only and can never establish applicant facts, authorization, form review, or
submission evidence.

Human and system approval gates still take precedence. Once a handoff genuinely needs a browser,
the supervisor holds it until the observation proves the `codex_chrome_connector`, an authenticated
composer, and fresh Chronicle capture. A wrong surface yields
`activate_codex_chrome_connector`; an unauthenticated connector requests applicant authentication;
stale or idle-paused Chronicle state requests capture restoration. The compact status carries only
these coarse runtime fields, so the five-minute task does not need to reload screenshots or prose.

## Signed applicant approval

`campaign create --submit` rejects a readable fact digest by itself. The run manifest contains
a one-time challenge and immutably binds its initial discovery request. The applicant reviews
the fact ledger and an unsigned approval document, then signs the exact JSON bytes on a
user-controlled machine:

```bash
applypilot autonomy prepare-fact-approval \
  --run-dir RUN_DIR \
  --approved-fact-digest DIGEST \
  --issuer applicant@example.com \
  --source-surface codex_user_message \
  --source-message-sha256 MESSAGE_SHA256 \
  --source-author-sha256 AUTHOR_SHA256 \
  --source-observed-at 2026-07-12T13:00:00-05:00 \
  --out approval.json

ssh-keygen -Y sign -f USER_CONTROLLED_APPROVAL_KEY \
  -n applypilot-fact-approval approval.json

applypilot autonomy import-fact-approval \
  --run-dir RUN_DIR \
  --approved-fact-digest DIGEST \
  --attestation approval.json \
  --signature approval.json.sig
```

Live commands use one fixed trust store at
`/Library/Application Support/ApplyPilot/approval_allowed_signers` on macOS (or
`/etc/applypilot/approval_allowed_signers` on other POSIX systems). The file and every parent
directory must be root-owned and not group/world writable; callers cannot select an alternate
file. Signature and revision checks likewise pin root-protected `/usr/bin/ssh-keygen` and
`/usr/bin/git` with a sanitized environment, so `PATH` cannot replace either verifier. The
private signing key must not be copied to the second Mac. The signed object binds the
run-manifest hash, one-time challenge, fact/context/policy
digests, profile and resume hashes, exact approved fact-value hashes, source-message and author
hashes, issuer, and expiration. The import command can verify and copy an approval; it cannot
mint one. A Gmail message ID, a self-sent reporting thread, or four booleans is not approval.
Fact approval never authorizes form filling, upload, final submission, or account creation;
those remain separate action-scoped gates.
The approval must still be valid when a candidate enters authorization/submission. Live campaign
creation also requires a clean reviewed Git checkout and does not accept a caller-supplied
revision override.

## Durable campaign accounting

Create review-only state with `campaign create` or add `--submit` only
after signed fact approval is present. The campaign manifest is immutable, caps the authoritative
target at 100, and binds the exact code revision. One exclusive writer lease refreshes state
before every mutation. A fsynced event log recovers an interrupted state projection, while a
fixed-name heartbeat file avoids appending a full campaign snapshot every five minutes.

Candidates move through an exact state graph. Authorization requires a durable copy of the
one-time candidate grant. The grant's exact schema, candidate/fact/material/form/policy bindings,
one-hour maximum lifetime, and consumption timestamp are checked at the actual submission time.
A submission counts only when durable, hash-matching artifacts exist for the authorization,
consumption marker, submission response, visible confirmation evidence, controller result, and
database row. An ambiguous click pauses the entire campaign; it can resume only after typed
evidence proves either `submitted_confirmed` or `not_submitted`. Pending model/browser payloads
are omitted from heartbeat output and deleted after resolution. Exact URLs that are already
applied, permanently failed, or out of attempts cannot be reacquired.

Add `--compact` to live `campaign status` or `campaign heartbeat` for the five-minute controller.
The compact schema reports only target/confirmed/remaining counts, durable sequence and progress
age, a stable state fingerprint, precedence-resolved action owner/code, blocker codes, heartbeat
timing, and the coarse runtime fields. Heartbeat writes refresh liveness without changing the
campaign sequence or `last_progress_at`; only a durable campaign event resets progress age. Empty
or exhausted active queues request browser-based discovery only when the fresh runtime contract
passes, while unknown submission outcomes and candidate authorization remain applicant-owned.
The compact blocker list is capped at ten codes and includes total/truncated metadata so a large
failed queue cannot silently expand every five-minute prompt.

The artifact runner is review-only. It may discover roles, verify first-party evidence, and
write local cover-letter packets. It never fills, uploads, submits, sends email, or changes an
external account. `probe-chatgpt` and `run` remain available only as caller-provided legacy CDP
compatibility commands and require the explicit `--allow-legacy-cdp` acknowledgement. Campaign
automation must use the portable handoff through the Codex Chrome connector; it must not launch
an isolated browser or silently fall back to CDP.

First-party verification does not trust a hostname merely because it contains the company
name. Shared Greenhouse, Lever, Ashby, Workday, and Avature surfaces must bind their tenant to
the candidate company; configured employer sources bind company, exact host, path prefix, and
source kind. Account-backed recruiters and aggregators cannot become first-party evidence.
The browser form artifact is also bound to the verified role site and cannot report success
when CAPTCHA, login, or account creation is required. Unknown JSON fields and all field-value
aliases are rejected.

Material prompt schema v3 separates applicant assertions from job evidence. Every prose
sentence that asserts something about the applicant through `I`, `me`, or `my` must be copied
verbatim into a structured `applicant_claims` entry. Those entries may cite confirmed `F` facts
only—never `JOB`—and their terms, named entities, and numbers are validated against exactly
those facts. This prevents a job requirement from becoming an applicant skill merely through
different grammar while leaving ChatGPT free to reason deeply about narrative and fit before
it emits the audited artifact.
The final letter is limited to four paragraphs, 450 words, and 20 structured applicant claims;
those output limits reduce response tokens without limiting private research or deliberation.

The existing deterministic form controller also defaults to dry-run:

```bash
applypilot apply --url URL --approved-fact-digest DIGEST
# Review the dry-run and copy its submission_authorization_manifest artifact:
applypilot apply --url URL --submit --approved-fact-digest DIGEST \
  --authorization-manifest PATH
```

Live submit is limited to one exact URL, one worker, and one expiring manifest. The manifest is
bound to the candidate, fact ledger, exact material bytes, filled-form review digest, and apply
policy, then consumed before the click. Its field model-call budget defaults to zero, so it does
not require or spawn Codex/Claude.
Set `APPLYPILOT_FIELD_MODEL_CALL_BUDGET=1` only when a bounded schema-constrained fallback is
needed. The fallback has no default model-process deadline, receives only confirmed ledger facts,
and requires an approved fact ledger even during a dry-run. All unresolved required fields still
fail closed.

Context pack v2 and prompt schema v3 intentionally require a fresh `autonomy plan`. Do not try to
advance a run packet created with context v1 or prompt schema v2 after upgrading.

## Remaining live gate

Before a real submission campaign, complete all of these checks:

- Applicant profile, resume, and corrections agree; required contact, work-authorization, and
  availability facts and at least one preferred-location fact are confirmed.
- `applypilot doctor --autonomy --strict --json` reports both `static_ready: true` and
  `runtime_ready: true`. Static readiness covers artifact transport, required applicant facts,
  and the fixed root-protected approval trust store without demanding a legacy model API key.
  Runtime readiness requires a fresh, correct-browser observation; pass
  `--autonomy-corrections PATH` when the reviewed run uses corrections.
- The authenticated browser tool can service one synthetic handoff without personal data.
- A review-only artifact run produces official, first-party-verified candidates and clean
  material packets.
- A user-controlled OpenSSH key signs the exact live fact approval, and the second Mac verifies
  it against the fixed root-protected allowed-signers file without possessing the private key.
- The exact role and final materials are reviewed before invoking the separate `--submit`
  boundary.

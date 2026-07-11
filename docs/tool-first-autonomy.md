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
characters and 30 confirmed facts while excluding contact details, demographics, secrets,
unknowns, and rejected claims. The acceptance target for a representative end-to-end run is at
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

The artifact runner is review-only. It may discover roles, verify first-party evidence, and
write local cover-letter packets. It never fills, uploads, submits, sends email, or changes an
external account. `probe-chatgpt` and `run` remain available as optional caller-provided CDP
compatibility commands, but they are no longer the recommended authenticated-browser path.

First-party verification does not trust a hostname merely because it contains the company
name. Shared Greenhouse, Lever, Ashby, Workday, and Avature surfaces must bind their tenant to
the candidate company; configured employer sources bind company, exact host, path prefix, and
source kind. Account-backed recruiters and aggregators cannot become first-party evidence.
The browser form artifact is also bound to the verified role site and cannot report success
when CAPTCHA, login, or account creation is required. Unknown JSON fields and all field-value
aliases are rejected.

Material prompt schema v2 separates applicant assertions from job evidence. Every prose
sentence that asserts something about the applicant through `I`, `me`, or `my` must be copied
verbatim into a structured `applicant_claims` entry. Those entries may cite confirmed `F` facts
only—never `JOB`—and their terms, named entities, and numbers are validated against exactly
those facts. This prevents a job requirement from becoming an applicant skill merely through
different grammar while leaving ChatGPT free to reason deeply about narrative and fit before
it emits the audited artifact.

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
needed. All unresolved required fields still fail closed.

## Remaining live gate

Before a real submission campaign, complete all of these checks:

- Applicant profile, resume, and corrections agree; required contact, work-authorization, and
  availability facts are confirmed.
- `applypilot doctor --autonomy --strict --json` reports no required missing checks. This mode
  checks the artifact transport and required applicant facts without demanding a legacy model
  API key; pass `--autonomy-corrections PATH` when the reviewed run uses corrections.
- The authenticated browser tool can service one synthetic handoff without personal data.
- A review-only artifact run produces official, first-party-verified candidates and clean
  material packets.
- The exact role and final materials are reviewed before invoking the separate `--submit`
  boundary.

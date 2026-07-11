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

1. Build a versioned fact ledger and a compact, contact-free context pack.
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
| Prompt characters per model call | 12,000 |
| Response characters per model call | 40,000 |
| Elapsed time | 15 minutes |

Telemetry stores hashes, counts, durations, and observed or estimated token fields. It does not
store raw prompts in the usage ledger. The acceptance target for a representative end-to-end
run is at least a 90% token reduction from the 19.35-million-token failure baseline. That target
must be measured on a fresh run after the applicant fact ledger is corrected; it is not claimed
from unit tests.

## Fact corrections

Create a corrections file from `fact_corrections.example.json`. Corrections are applied to an
immutable snapshot; they never silently rewrite `profile.json` or `resume.txt`.

```bash
cp fact_corrections.example.json ~/.applypilot/fact_corrections.json
applypilot autonomy plan \
  --query "entry-level product and data roles" \
  --corrections ~/.applypilot/fact_corrections.json
```

Review the generated `fact_ledger.json` before allowing browser or model work. Blank,
placeholder, and explicitly rejected facts remain blockers instead of being guessed.
The plan command prints the ledger digest. Copy it only after review; `autonomy run` refuses
to start if the fresh profile, resume, corrections file, or approved digest differs.

## ChatGPT Web operation

The adapter expects a caller-provided, authenticated Chrome CDP session. It reads only the
ChatGPT composer and the final assistant turn; it does not extract cookies, local storage,
sidebar history, or whole-page text.

```bash
applypilot autonomy probe-chatgpt --cdp-port 9222
applypilot autonomy run \
  --query "entry-level product and data roles" \
  --cdp-port 9222 \
  --corrections ~/.applypilot/fact_corrections.json \
  --approved-fact-digest COPY_THE_REVIEWED_PLAN_DIGEST_HERE
```

`autonomy run` is review-only. It may discover roles, verify first-party evidence, write local
cover-letter packets, and inspect one form surface. It never fills, uploads, submits, sends
email, or changes an external account.

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
- `applypilot doctor --strict --json` reports no required missing checks.
- A fresh authenticated ChatGPT Web probe succeeds without sending.
- A review-only autonomy run produces official, first-party-verified candidates and clean
  material packets.
- The exact role and final materials are reviewed before invoking the separate `--submit`
  boundary.

# Autonomy continuation ledger

This ledger turns the open-ended autonomy goal into observable gates. A gate is complete only
when its artifact or command evidence exists; activity alone is not progress.

## Baseline failure

The diagnosed second-Mac run lasted about 26 minutes and recorded approximately 19.35 million
token events while moving zero roles into scoring, materials, form review, or submission. Its
context grew from roughly 97,000 to 290,000 input tokens. Broad aggregator retries, a stalled
Workday path, a missing ChatGPT Web provider implementation, and a failed nested model process
were the main causes.

## Acceptance gates

| Gate | Evidence required | Current state |
| --- | --- | --- |
| Facts | Reviewed fact ledger; required contact, authorization, sponsorship, availability, and preferred-location facts confirmed; rejected claims tombstoned | Blocked on applicant review and preferred-location choice |
| Browser transport | Synthetic, no-personal-data ChatGPT Web requests and responses through the authenticated real Chrome session | Complete: 3 bounded model calls, no nested model |
| Resumable run | The same manifest-bound run advances from discovery request to review-ready materials without CDP or a nested model | Complete in synthetic artifact run; regression-covered |
| Source quality | Candidate URLs deduplicated, broad aggregators rejected, and accepted roles verified against exact employer or ATS tenant evidence | Complete in synthetic run; rendered-browser review caught one stale posting that returned HTTP 200 |
| Material safety | At most two cover letters, every applicant claim tied to applicant evidence, rejected facts and placeholders blocked | Complete in synthetic run; 2 material packets |
| Form review | At most one application form inspected without filling, uploading, or submitting | Complete: stale Capital One Workday page returned `form_review_blocked`; no side effects |
| Submission | Signed applicant facts, one exact candidate, reviewed material/form bytes, expiring one-time authorization, and durable typed confirmation evidence | Implemented fail-closed gates; no signed fact approval or live authorization issued |
| Token reduction | Fresh run usage ledger below 1.935 million token events (90% below baseline), using at most three rich model calls and no repeated thread context | Synthetic estimate: 6,257 model tokens, 99.96767% below baseline; reviewed personal run still blocked on facts |
| Doctor | `applypilot doctor --autonomy --strict --json` succeeds in a fresh shell on the active machine | Correctly fails closed while the fixed root-owned approval trust store is absent; artifact transport passes without a legacy API key |
| Durable campaign | Immutable target-100 manifest, serialized/recoverable state, one writer lease, dedupe, typed evidence, unknown-outcome pause, bounded heartbeat | Implemented locally; pre-campaign and live-campaign fixed-name heartbeats are regression-covered |
| Publish | Targeted tests, lint, diff review, commit, and push to `SulaymanB2024/ApplyPilot` fork branch | `2638a13` pushed; current campaign/signature work pending final gate and push |

## 2026-07-12 second-Mac checkpoint

- The active second-Mac checkout is preserved on `codex/apply-harness-stage1-hardening` at
  `b1f266e`; it is ahead by two commits and has untracked `outputs/` and `tmp/`, so it must not be
  pulled or switched in place.
- The fork branch `codex/tool-first-autonomy` is confirmed at `2638a13`. The safe update path is a
  clean worktree under `~/Projects/CodexWork/ApplyPilot-campaign`, followed by a fresh venv and a
  latest-code autonomy doctor.
- Terra is the persistent Codex model label `gpt-5.6-terra`, not a separate executable. Nested
  `codex exec` orchestration previously failed and must not own the campaign.
- Memories maps to Codex Chronicle. The recorder process now starts on the second Mac, but frame
  updates are intermittent (the latest recheck was 214 seconds old rather than a few seconds).
  Chronicle remains excluded from reporting until recorder identity and continuous fresh frame
  timestamps both pass.
- The local v2 context pack now selects 71 confirmed facts from the current applicant data for a
  representative query, up from 30, while the resulting discovery prompt is 7,817 characters
  against the existing 40,000-character cap.
- A dedicated second-Mac Codex task is active in the clean worktree with `gpt-5.6-terra` and the
  normal authenticated Chrome surface. Its first pass produced useful approval-gate evidence,
  but a raw Gmail-message-ID receipt was rejected because the self-delivery thread cannot prove
  applicant intent. The replacement requires an external OpenSSH signature.
- That collaborator also consumed roughly 4.9 million token events after broad tool inventory,
  full-thread reads, and repeated release checks. Follow-ups must read only the campaign heartbeat
  and named artifacts, perform one bounded action, and stop.

## Synthetic end-to-end evidence

The external authenticated-browser run
`20260711T215126162932Z-af5ee329a9` used synthetic applicant facts only. ChatGPT Web received
one rich discovery request and two
role-specific material requests. It returned 10 discoveries; the coordinator performed 5
first-party checks and produced 2 local material packets. The one read-only form review found
that the selected Capital One Workday URL rendered a “page does not exist” surface even though
the raw verifier had observed HTTP 200, so the run ended `form_review_blocked` with zero fills,
uploads, submissions, or other final actions.

The usage ledger recorded 3 model calls, 4 browser navigations, 9 external calls, and an
estimated 6,257 model tokens. The 99.96767% reduction is a synthetic estimate against the
19,353,116-event failure baseline; it demonstrates the bounded architecture but does not
substitute for a personal review-only run after applicant facts are approved.

## Security hardening checkpoint

- Consumed responses bind dynamic query, limit, candidate, and verified-job inputs through a
  prompt-schema-versioned input digest.
- Invalid material responses are semantically rejected before receipt creation and quarantined
  for a bounded corrected import.
- Accepted receipts and rejection records restore cumulative tool-call counts on resume, so a
  CLI restart cannot reset model, browser, external-call, or retry budgets.
- Fact-ledger approval digests bind ledger version, profile hash, resume hash, and per-record
  source hashes.
- Live campaign approval additionally requires a detached OpenSSH signature over the exact run,
  one-time challenge, source evidence, and approved fact-value hashes. The controller has only
  the allowed public keys and cannot mint the approval.
- Campaign confirmations reference durable typed evidence bytes; arbitrary 64-hex strings cannot
  increment the confirmed target.
- Shared ATS trust is tenant-and-company bound; configured sources include company, host, path,
  and source kind, while account-backed recruiters are excluded.
- Model context is constructed from confirmed allowlisted fact records and recursively strips
  identity, contact, address, salary, demographic, credential, unknown, and rejected values.
- Strict response validators reject surprise top-level keys, reasoning notes, and form-field
  value aliases.
- Material prompt schema v2 emits exact structured applicant claims; `JOB` cannot support them,
  and every first-person or possessive applicant assertion must map to confirmed `F` evidence.
- Hash receipts detect stale/swapped/one-sided edits but are not claimed to resist a hostile
  local writer who can modify both an artifact and its receipt.

## Stop boundaries

- No real applicant prompt is sent while required facts or corrections are unreviewed.
- No live campaign manifest is created from a copied digest, self-sent Gmail thread, or unsigned
  local receipt.
- No application form is filled or uploaded during role discovery or material generation.
- No submit click occurs without a separate exact-candidate authorization artifact.
- Missing, failed, challenged, or skipped provider coverage remains a measurement gap.
- Two consecutive cycles without material progress open the circuit breaker.

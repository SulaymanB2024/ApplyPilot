# ApplyPilot 100-role campaign monitor

- Observer task: `019fc595-daeb-77c1-8fc1-15032a18045f`
- Execution task: `019fc5b6-67f6-72f1-b9c8-922498efa40d`
- Execution checkout: `/Users/sulaymanbowles/.codex/worktrees/45dd/ApplyPilot`
- Observation interval: three minutes
- Observer boundary: read worker status and durable artifacts; do not steer the worker after the campaign authorization correction.
- Campaign success boundary: count only distinct applications with durable provider submission receipts. Discovery, drafts, form fills, and attempts are not submissions.

## Observations

### 2026-08-02 22:48:07 CDT — cycle 0

- Worker state: active; corrected campaign turn is in progress.
- Worker acknowledgement: provider receipts are the only success criterion; human-attestation and anti-bot gates remain stop conditions; prior receipts will be reconciled before attempts.
- Verified submissions observed: 0 in the worker status surface at this checkpoint.
- Engineering assessment: the stated control boundaries are sound. The next deciding evidence is whether startup reconciliation binds the campaign to one durable run ID and produces structured counters before browser activity.
- Intervention: none.

### 2026-08-02 22:51 CDT — cycle 1

- Worker state: active. It recovered from an incorrect browser-skill script path and sent one exact, hash-bound discovery request through the authenticated ChatGPT Web session.
- Durable run: `20260803T034911987612Z-d6f5aad24d`.
- Observed provider identity: the worker reports only the visible label `ChatGPT`; requested routing is separately recorded as `best_available` with `high` effort.
- Verified submissions observed: 0.
- Structural blocker: `run_policy.json` remains `review_only: true`, so this run cannot produce a legitimate submission receipt even though the worker has explicit campaign authorization.
- Capacity blocker: the immutable run budget allows only 30 discoveries, 40 artifacts, 32 browser navigations, 5 material packets, and 3 form dry-runs. Those ceilings are incompatible with a target of 100 verified submissions.
- Targeting risk: the run uses the single query `AI product manager intern`. A 100-role campaign needs a controlled portfolio of approved role families and per-family quality thresholds, not one repeated query.
- Telemetry gap: the first two durable events have empty `counts` objects. Requested model and effort are persisted, but the worker's actual observed provider label is not yet visible in the run event stream.
- Engineering decision: treat campaign intent and immutable run policy/budget as a preflight contract. Future runs must fail before external calls when requested submissions exceed policy capability.
- Intervention: none; these findings are recorded for harness refinement in the observer checkout.

### 2026-08-02 22:54 CDT — cycle 2 (terminal)

- Worker state: completed normally after about 6 minutes 43 seconds; terminal run status is `no_eligible_verified_roles`.
- Verified submissions: 0 of 100. Discovered: 1. Qualified: 0. Rejected: 1. Paused: 0. Review-ready: 0. Provider submission receipts: 0.
- Candidate outcome: the one accepted discovery was rejected by the deterministic eligibility gate with `role_family_mismatch`. One Indeed-mirror discovery was excluded during the ChatGPT response cleanup and is preserved in a rejected handoff artifact.
- ChatGPT Web behavior: first response import failed `ChatGPTContractError`; one retry produced a valid response. Durable usage reports 2 model calls, 2 browser navigations, 2 external calls, 1 retry, and 1 artifact.
- Latency: the valid handoff completed after roughly 245.8 seconds. The imported response arrived at roughly 238.3 seconds, leaving about 7.5 seconds for final processing/wait completion.
- Provider evidence: requested routing remains `best_available` / `high`; actual model identity is `unobserved` in durable artifacts. The worker saw only the generic visible label `ChatGPT`.
- Telemetry defect: all nine durable run events still use empty `counts` objects, even though the final result ledger contains usage counts. This prevents live progress reconstruction from the event stream.
- Targeting defect: the query asked for `AI product manager intern`, but the configured role-family gate rejected the only clean candidate. Query generation and eligibility policy are not using the same accepted-role taxonomy.
- Contract defect: the worker correctly identified additional live gates: system approval trust store, applicant-signed fact approval, exact per-candidate authorization, and a clean checked-out revision. The current worktree snapshot is intentionally dirty. A worker cannot turn broad campaign authorization into those missing evidence-bound grants.
- Final blocker ledger: (1) no eligible verified candidate corpus; (2) review-only immutable run; (3) run capacity below campaign target; (4) unsigned applicant facts; (5) no clean live revision; (6) no executable campaign controller command beyond create/status/heartbeat/observation surfaces.
- Intervention: none. The execution task was not messaged after the authorization correction, its browser was not operated by the observer, and its worktree was not altered.

## Observer-checkout refinement pass

### Evidence-driven changes

- `src/applypilot/autonomy/context.py`: the ChatGPT Web discovery prompt now states the same accepted role-family taxonomy used by the deterministic eligibility gate. It explicitly rejects generic internship, analyst, associate, sales, and talent-pool padding. This addresses the observed valid-handoff-but-zero-qualified result without weakening the local gate.
- `src/applypilot/autonomy/telemetry.py`: every ledger-backed lifecycle event now includes a snapshot of live budget counts. This makes progress reconstructable from `events.ndjson` instead of waiting for the terminal result ledger.
- `src/applypilot/autonomy/handoff.py` and `src/applypilot/cli.py`: `autonomy import-response` now accepts `--observed-model`, binds the normalized visible UI label to the exact request/response digest in an immutable observation sidecar, and carries it into usage and lifecycle telemetry. Missing UI evidence continues to record `unobserved`; requested routing remains separate.
- Rejected semantic responses now quarantine their model-observation sidecars outside the response glob used for retry accounting. This preserves one-attempt/one-retry counts and allows a corrected response to bind a new observation.

### Validation

- Focused contract suite: `83 passed` across semantic handoff, autonomy, and event-journal tests.
- Focused Ruff check: passed for the changed source and test files.
- The first focused run failed during collection because the new model-label regex lacked an imported `re`; fixed.
- The second focused run exposed two compatibility defects: model-observation validation masked the older receipt-tamper error, and a quarantined sidecar was counted as a second rejected response. Both were fixed, then the focused suite passed.
- Full repository suite: `358 passed`.
- Full repository Ruff scan: passed.
- CLI contract check: `applypilot autonomy import-response --help` exposes `--observed-model` with an `unobserved` default.

### Safety and rollout notes

- The active execution worker cannot benefit because it ran from an isolated working-tree snapshot and is already terminal.
- These refinements do not loosen applicant-fact, CAPTCHA, login, duplicate, review, candidate-scoped authorization, signed-approval, clean-revision, or receipt gates.
- No commit, push, deploy, application submission, message, or external account change was performed by the observer.

## Retry 2 — updated checkout

### 2026-08-03 07:01:52 CDT — cycle 0

- Execution owner resumed: task `019fc5b6-67f6-72f1-b9c8-922498efa40d`; no duplicate task or worktree was created.
- Execution source: `/Users/sulaymanbowles/Projects/ApplyPilot` is the read-only code source for the worker so retry 2 exercises the observer-checkout refinements. Runtime artifacts remain in ApplyPilot's configured data directories.
- Objective: retry the 100-receipt campaign with a broad, bounded portfolio derived from configured approved role families and preferences; reconcile all prior runs and receipts first.
- Observer boundary: three-minute read-only status and artifact observations. The observer will not operate the worker browser, approve facts/candidates, or steer routine execution after this startup correction.
- Verified submissions at retry start: 0 of 100.
- Intervention: one startup continuation brief sent before execution; none thereafter.

### 2026-08-03 07:05 CDT — cycle 1

- Worker state: active; exact ChatGPT Web discovery request in progress.
- New durable run: `20260803T120402437826Z-d6f5aad24d`.
- Query portfolio: one broad Summer 2027 paid-undergraduate objective spanning configured product, analytics, technical-business/operations, strategy, venture, research, SEO-analytics, and substantive fintech-product families. It requires first-party/official ATS postings and excludes talent pools, sales, stale/senior/graduate-only roles, and known human-attestation/account gates.
- ChatGPT routing: requested `best_available` / `high`; visible UI label `ChatGPT`.
- Verified submissions: 0 of 100.
- Live gate: the system approval trust store is absent, so downstream live authorization cannot yet be created. Discovery remains safe and useful.
- Refinement proof: the first two durable events now contain complete zero-valued live counter maps instead of empty `counts` objects. The new taxonomy-aligned prompt increased from 2,938 to 4,194 input characters, still well below the 60,000-character budget.
- Remaining run capacity: 30 discoveries, 24 first-party verifications, 5 material packets, and 3 form dry-runs. This run remains a bounded corpus-building cohort, not a 100-submission-capable run by itself.
- Intervention: none.

### 2026-08-03 07:08 CDT — cycle 2 (retry-2 terminal)

- Worker state: completed normally after about 6 minutes 10 seconds; run status `no_eligible_verified_roles`.
- Verified submissions: 0 of 100. Discovered: 1. Taxonomy-eligible: 1. First-party verified: 0. Freshness-paused: 1. Materials/forms/authorizations/submissions: 0.
- The sole candidate carried the root careers URL `https://www.capitalonecareers.com/`, not a job-specific posting. First-party verification failed closed with `non_job_specific_url` and `url_path=root` before any HTTP request.
- ChatGPT Web: 1 requested `best_available/high` exchange, visible label `ChatGPT`, 1 browser navigation, 2 external calls including verification accounting, and 0 retries. The model-observation sidecar and final lifecycle event correctly persist `ChatGPT`.
- History reconciliation: 44 workflow runs and 52 canonical workflow URLs were checked. Canonical submission-registry and legacy applied-status counts were both zero. A separate legacy corpus contains 245 normalized role claims without enough canonical receipt binding to guarantee safe reapplication decisions.
- Live blockers remain: missing approval trust store; no applicant-signed fact approval; no candidate-scoped grants; dirty read-only execution checkout; zero job-specific verified candidates.
- Intervention: none during execution.

### 2026-08-03 07:09 CDT — observer refinement 2

- `role_candidates_from_payload` now rejects root/homepage URLs as non-job-specific during the ChatGPT contract import, before a discovery is admitted or first-party verification budget is consumed. This converts the exact retry-2 failure into a correction opportunity inside the bounded handoff.
- Direct handoff-import lifecycle events now inherit the latest complete counter map, eliminating the remaining empty `counts` objects between request and final exchange events.
- Focused validation: 84 semantic-handoff, autonomy, and event tests passed; focused Ruff passed.
- The worker was terminal while these files changed, so no active process or immutable run artifact was affected.

## Retry 3 — job-specific URL contract

### 2026-08-03 07:10 CDT — cycle 0

- Existing execution owner resumed for one final bounded retry; no task/worktree duplication.
- Changed mechanism under test: root/homepage URLs must fail ChatGPT response validation and trigger bounded correction before admission.
- Worker may choose one narrower approved family cohort with sufficient current inventory, while preserving configured quality, compensation, location, duplicate, and truth gates.
- Verified submissions at retry start: 0 of 100.
- Stop condition: if retry 3 again produces no job-specific verified corpus or only unchanged human-approval/clean-revision gates remain, terminate and report rather than adding compute.
- Intervention: one terminal-to-retry startup brief; none during execution.

### 2026-08-03 07:14 CDT — cycle 1

- Worker state: active; authenticated ChatGPT handoff serviced.
- New run: `20260803T121111421800Z-d6f5aad24d`.
- Cohort contract: current Summer 2027 paid undergraduate roles across approved product/analytics/technical-business/strategy/venture families, restricted to unique job-detail URLs on Greenhouse, Ashby, Lever, or Workday. Homepage/search/talent-community/mirror URLs are explicitly excluded.
- ChatGPT response: 2,628 characters imported and contract-validated on the first attempt; visible label `ChatGPT`, requested route `best_available/high`.
- Refinement proof: all import/validation events carry the complete live counter map; no empty `counts` objects remain.
- Verified submissions: 0 of 100. Candidate admission and first-party verification have not yet been observed at this checkpoint.
- Intervention: none.

### 2026-08-03 07:16 CDT — cycle 2 (final terminal)

- Worker state: completed normally after about 6 minutes 2 seconds; run status `no_eligible_verified_roles`.
- Verified submissions: 0 of 100. Discovered: 2. Qualified: 0. Rejected: 2. Paused: 0. First-party verified/materials/forms/authorizations/submissions: 0.
- The two discoveries used unique, job-specific Ashby URLs and passed the updated URL contract. Both failed deterministic eligibility with `role_family_mismatch`; no verification budget was consumed.
- ChatGPT Web: one `best_available/high` request, visible label `ChatGPT`, one model call, one browser navigation, one external call, zero retries. All six events carry complete counter maps and the visible model is durably bound.
- Duplicate evidence: both URLs were unique against the reconciled canonical and legacy stores. This attempt did not worsen the unresolved 245-claim legacy normalization boundary.
- Stop condition met: two consecutive completed retries produced no qualified/verified corpus, while the approval trust store, signed-fact, candidate-grant, and clean-revision gates remained unchanged. Further prompts would add compute without a changed acceptance mechanism.
- Browser/process cleanup: the bounded ChatGPT tab was closed, transient capture removed, and no ApplyPilot process remained.
- Intervention: none during execution. Monitoring is paused after this terminal cycle.

## Final retry outcome

- Total new provider receipts across retries: 0.
- The refinements are proven for URL specificity, role-taxonomy prompting, model-observation provenance, and live counters, but discovery precision is still below the threshold needed to feed the deterministic role-family gate.
- The next engineering gate is not another ChatGPT prompt. It is a taxonomy-bound candidate-acquisition layer that either queries structured first-party ATS feeds directly by accepted family or validates each ChatGPT candidate's family markers before accepting the response as a successful discovery cohort.
- Final observer-checkout validation after the retry-2 URL/counter hardening: full repository suite `359 passed`; full repository Ruff scan passed; `git diff --check` passed.

# Durable 30-application goal operator

`applypilot campaign-run` is the canonical controller for the bounded
30-receipt campaign. Campaign truth, browser-job serialization, exact approvals,
deduplication, leases, and outcome accounting live in `workflow.sqlite3`.
The Codex task is replaceable: it operates visible Chrome, reads the active
artifact, and asks SQLite for the next transition.

The default campaign is `second-mac-30-confirmed-202608`. It counts only 30
distinct `submitted_confirmed` outcomes created after campaign start. A click,
attempt, completed form, talent pool, general-interest form, speculative
outreach, board result, or ambiguous outcome never counts.

## Operating boundary

- Run exactly one Codex `/goal` task on the second Mac using `gpt-5.6-sol` at
  High effort. Do not use nested agents or recurring monitor automations.
- The second Mac is the only mutable host. Its random identity is created in
  the application-data directory on first start and bound to the campaign.
- The first Mac may run `campaign-run status` against a synchronized or copied
  database, but must not invoke a mutation command.
- The goal task may operate the authenticated visible Chrome session and record
  a browser result. It may not invoke `campaign-run review` unless the user has
  just authorized the exact packet classifications.
- Each `step` performs exactly one transition. Do not loop multiple transitions
  inside one shell command.

This follows the one-objective, verifiable-stop, checkpointed pattern described
in [OpenAI's `/goal` guidance](https://learn.chatgpt.com/use-cases/follow-goals).

## Preflight on the second Mac

Use a clean, reviewed checkout of the approved revision, an authenticated
visible Chrome profile, and the active profile, resume, and three-tier
`searches.yaml`.

```bash
git status --short
applypilot doctor --strict
sqlite3 "$APPLYPILOT_DIR/workflow.sqlite3" \
  ".backup '$APPLYPILOT_DIR/workflow.pre-campaign.sqlite3'"
applypilot campaign-run start
```

If `APPLYPILOT_DIR` is not set, use the platform ApplyPilot application-data
directory for the backup. `start` imports every Markdown receipt ledger under
the existing campaign directory by default. Use repeatable `--history-ledger`
to select explicit ledgers. Receipt-supported historical identities block
reapplication; ambiguous historical identities remain blocked for review.

Starting the same campaign again is idempotent only when target, review size,
maximum submissions, and the exact search-config digest are unchanged.

## Goal objective

Give the second-Mac goal task one objective:

> Continue campaign `second-mac-30-confirmed-202608` from its compact
> checkpoint until SQLite reports 30 unique `submitted_confirmed` receipts.
> Perform one `campaign-run step` at a time, service only its active artifact,
> stop for every review packet or human blocker, never retry an uncertain
> submission, and never call `campaign-run review` without new authorization
> for that exact packet.

On every continuation or replacement task, read only:

1. the fixed-name `checkpoint.json` reported by `campaign-run status`; and
2. the one active request, review packet, or discovery plan named in that
   checkpoint.

Do not replay the old task transcript. The checkpoint is capped at 16 KiB and
contains no prompts, applicant values, selectors, or browser content.

## One-step loop

```bash
applypilot campaign-run status
applypilot campaign-run step
```

Act on exactly one returned action:

| Action | Required operator behavior |
|---|---|
| `progressed` | Read the named discovery plan or call `step` once more. |
| `browser_action_required` | Service only the named request in visible Chrome, capture local evidence, then record one result. |
| `approval_required` | Present the five-candidate review packet and stop. |
| `human_blocked` | Stop with the exact `blocker_code`; never improvise around it. |
| `no_progress` | Perform the named next action. Two consecutive unchanged cycles block the campaign. |
| `complete` | Verify the SQLite counts and stop the goal. |

### Discovery plans

Discovery plans expose one ordered query batch at a time: primary tier 1,
broader tier 2, then adjacent tier 3. Service a plan with the existing bounded
aggregation and canonical prepare commands, then attach the resulting workflow
run:

```bash
applypilot aggregate --query "PLAN PURPOSE" --term "TERM" --mode quick --watch
applypilot prepare --query "PLAN PURPOSE" \
  --aggregation-snapshot AGGREGATION_RUN_ID@REVISION
applypilot campaign-run step --workflow-run-id WORKFLOW_RUN_ID
```

Repeat `--term` for the plan's terms. Board and authenticated-portal records are
leads only. `prepare` must resolve them to a job-specific employer or ATS
posting and preserve first-party open-state verification. Admission still
requires `posted_employment`, approved location and early-career gates, truthful
applicant facts, and a fit score of at least 70.

### Browser results

The browser worker must use the exact request path returned by `step`. The
controller derives every binding from that request and canonical state, copies
and hashes evidence, validates the browser-response contract, and imports the
result.

Verified dry-run example:

```bash
applypilot campaign-run record-browser-result \
  --request REQUEST_PATH \
  --status dry_run_verified \
  --ats-family greenhouse \
  --evidence REVIEW_SCREENSHOT
```

Confirmed submission example:

```bash
applypilot campaign-run record-browser-result \
  --request REQUEST_PATH \
  --status submitted_confirmed \
  --confirmation-kind confirmation_page \
  --confirmation-text "Application received" \
  --evidence RECEIPT_SCREENSHOT
```

If the final action occurred but authoritative confirmation is missing, record
`submitted_unconfirmed` with evidence. The entire campaign moves to
`outcome_review_required`; do not retry. If a submit request's worker lease
expires before a result is recorded, the controller makes the same stop. A
CAPTCHA, passkey, authenticator/SMS challenge, identity verification, missing
applicant fact, or unsupported authentication path is a blocker, not permission
to infer an answer or seek an alternate submission route.

Routine saved-login, password-manager account creation, and email OTP actions
must remain inside the request's intervention contract. Passwords, codes,
cookies, tokens, and mailbox content must never enter CLI arguments or evidence.

## Review boundary

An `approval_required` checkpoint names one review packet containing five
candidates, or the remaining smaller packet after all search tiers are
exhausted. Show the packet to the user and stop the task. The user must classify
every `RUN_ID/CANDIDATE_ID` as approved, rejected, or deferred, with no more
than three approved.

Only after new authorization for that exact packet may the operator record the
decision:

```bash
applypilot campaign-run review \
  --packet-digest PACKET_SHA256 \
  --approve RUN_ID/CANDIDATE_ID \
  --approve RUN_ID/CANDIDATE_ID \
  --reject RUN_ID/CANDIDATE_ID \
  --defer RUN_ID/CANDIDATE_ID \
  --reject RUN_ID/CANDIDATE_ID
```

The controller creates 24-hour exact-candidate approvals and serializes all
form work and submissions. Expired approvals invalidate the old form review,
archive its transport response, increment the form-review generation, and
require a new visible-Chrome dry-run before the candidate can be reviewed and
approved again.

## Recovery proof and stopping conditions

After at least one durable browser import, terminate the goal task deliberately.
Start one replacement goal task on the same second Mac. It must recover from
`campaign-run status`, read the fixed checkpoint and active artifact, and
continue without replaying the previous transcript or creating a second worker.

Stop immediately when any of these is true:

- `status=complete` and `submitted_confirmed=30`;
- an open review packet requires user authorization;
- a submit result is ambiguous;
- a submit worker is lost after activation;
- another host owns the campaign;
- two consecutive cycles make no material progress; or
- the controller reports any other `human_blocked` state.

Pause and resume are explicit operator actions:

```bash
applypilot campaign-run pause --reason operator_requested
applypilot campaign-run resume
```

`resume` may reopen an explicitly paused or non-ambiguous blocked campaign. It
cannot reopen `outcome_review_required`; that state requires manual outcome
reconciliation and must never be cleared by guessing or retrying.

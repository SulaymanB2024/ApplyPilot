# ApplyPilot functional rebuild ledger

This ledger is the durable checkpoint for the functional rebuild goal started on
2026-07-15. It records observed state, decisions, commands, evidence, blockers,
and next actions. It must not contain credentials or unverified applicant facts.

## Goal and completion boundary

Build one coherent workflow that discovers relevant live roles, verifies
eligibility and freshness, ranks them from approved applicant facts, produces
truthful role-specific materials, dry-runs real ATS forms in visible Chrome,
executes only an exact user-approved batch, and records durable confirmation.

The goal is not complete until at least one explicitly approved real application
has durable ATS confirmation, unless the user explicitly declines live
submission or every approved candidate is externally blocked.

Irreversible actions remain out of scope until action-time approval for exact
candidate URLs. CAPTCHA, MFA, identity verification, payment, tax, SSN, account
creation, email application, and ambiguous submission outcomes fail closed.

## Ownership and verified surface

- Accountable task: `019f6673-2238-7fb0-b247-039d35ef34cb`
- Checkout: `/Users/sulaymanbowles/Projects/ApplyPilot`
- Verified starting commit: `fe6fb71963e275d8dc2becf6d522739dc6788547`
- Verified starting branch: `codex/tool-first-autonomy`
- Rebuild branch: `codex/applypilot-functional-rebuild-20260715`
- Remotes: `fork` is the user fork; `origin` is Pickle-Pixel upstream.
- Starting worktree: clean and tracking `fork/codex/tool-first-autonomy`.
- Other ApplyPilot worktrees were enumerated before branching. Their associated
  implementation tasks are inactive; this task is the only active owner of the
  functional rebuild objective.
- Repository-local `AGENTS.md`: none found. The task-provided machine safety
  policy plus the global validation/model policies govern this work.
- Delegation: disabled by the goal. All implementation and review stay in this
  task.

## Baseline evidence

### Runtime and user data

- Repo virtual environment: Python 3.14.6, editable ApplyPilot 0.3.0 from this
  checkout.
- Active user-data root:
  `/Users/sulaymanbowles/Library/Application Support/ApplyPilot`.
- Active database: `applypilot.db` (not the historical `~/.applypilot` path).
- Profile, text resume, PDF resume, and search configuration are present.
- Chrome 150 is running. The Codex Chrome connector is active.
- The real Chrome session exposes an authenticated ChatGPT composer; no isolated
  browser was substituted.
- ApplyPilot selects Chrome `Profile 1` when Google Password Manager metadata is
  required, even though the unset persistent profile override defaults to
  `Default`.
- No ApplyPilot server or long-lived development process was running.
- No autonomy run or campaign exists in the active user-data root.

### Preserved baseline

Before any migration or destructive cleanup, an online SQLite backup and copies
of the applicant profile, resumes, and search configuration were created at:

`/Users/sulaymanbowles/Library/Application Support/ApplyPilot/backups/functional-rebuild-baseline-20260715T154951Z`

`PRAGMA quick_check` returned `ok`. The database SHA-256 at backup time was
`cda4239a62b0c2baa092f8baff2980c8f5a5294cf83ff7fbf0c411ca95be52ed`.
All backup files are mode `0600` inside a mode `0700` directory.

### Product state

`applypilot status` reproduced the stated baseline:

- 112 discovered jobs
- 112 enriched descriptions
- 0 scored
- 0 tailored resumes
- 0 cover letters
- 0 application-ready
- 0 applied
- 0 apply errors

`applypilot doctor` reports the legacy LLM API key as missing. Chrome, Codex,
Node, the deterministic controller, and Google Password Manager metadata are
available. The doctor currently labels the installation Tier 1 while separately
saying Tier 3 needs only the already-present agent CLI, Chrome, and Node. This is
an early example of split readiness semantics to trace in Phase 1.

## Commands and checks run

- `git rev-parse --show-toplevel`
- `git branch --show-current`
- `git status --short --branch`
- `git remote -v`
- `git worktree list --porcelain`
- `.venv/bin/python --version`
- `.venv/bin/applypilot --help`
- `.venv/bin/applypilot autonomy --help`
- `.venv/bin/applypilot campaign --help`
- `.venv/bin/applypilot status`
- `.venv/bin/applypilot doctor`
- Read-only SQLite schema/count queries against the active database
- Chrome connector/session inspection limited to relevant tabs and authentication
  surface; no conversation content, cookies, storage, or credentials were read
- Online SQLite `.backup`, `PRAGMA quick_check`, and SHA-256 verification

## Initial classifications

These are provisional until the end-to-end trace is complete.

- Missing configuration: legacy scoring/material generation requires an LLM API
  key that is absent.
- Bad product defaults: doctor/readiness surfaces disagree about which workflow
  is usable and select different implied Chrome profiles.
- Unnecessary operational ceremony: autonomy and campaign commands expose several
  heartbeat, observation, signature, and handoff steps before a normal shortlist
  can be produced.
- Broken or split implementation: discovered/enriched rows do not flow into any
  scoring, material, authorization, or application-ready state.
- External limitations: not yet assessed in this rebuild; provider failures and
  challenge pages will remain measurement gaps.
- Applicant-owned facts: not yet classified. Existing profile/resume facts must
  be normalized and reviewed before any model or form answer uses them.

## Phase 1 trace findings

### Configuration to discovery

- The active search configuration names the intended role families and five
  location preferences, but those locations live only in `searches.yaml`.
  Autonomy's approval ledger accepts preferred locations only from
  `profile.availability.preferred_locations` or
  `profile.preferences.locations`, so doctor reports zero approved locations.
- Direct ATS title filtering is an admission keyword OR gate. Generic tokens
  such as `analyst`, `operations`, `product`, `program`, and `associate` are
  sufficient even when a title has no internship or early-career signal.
- Direct ATS location filtering is disabled. Its implementation also reads old
  flat keys (`location_accept` and `location_reject_non_remote`) rather than the
  active nested `location.accept_patterns` / `location.reject_patterns` shape.
- The configured seniority exclusion contains `manager,` with a trailing comma,
  while the matcher performs literal substring checks. Plain `manager` titles
  therefore pass that exclusion.
- In the 112-row production database, 96 titles have no internship, co-op,
  new-grad, graduate, or apprentice signal. A conservative title scan finds 32
  senior/manager/director/staff/principal/lead roles. Examples include Platform
  Product Manager, Strategy and Operations Manager, Revenue Operations Manager,
  and unrelated credit/settlement roles.

### Discovery to ranking and materials

- The legacy scorer sends every enriched row directly to an LLM using the raw
  resume. It has no deterministic eligibility, required-experience, location,
  rendered-freshness, or first-party-open gate.
- The autonomy path has deterministic gates, but it writes candidates and
  materials only to a run artifact directory. It does not update the jobs
  database consumed by `applypilot apply`.
- Autonomy allows eligibility `review` candidates to continue through
  verification and material generation. It also allows a first-party posting
  with no freshness dates to receive materials when the page reports open.
- Current autonomy defaults are 30 discoveries, 15 first-party checks, five
  material packets, three form reviews, eight model calls, a 180-day post-age
  window, and three no-progress cycles. Those defaults are much broader than
  this goal's top-ten and two-cycle bounds.
- The legacy tailoring prompt explicitly permits adding two or three unverified
  "closely related" tools, and its judge calls several unsupported additions
  acceptable minor stretches. That directly violates the no-unsupported-claims
  completion criterion.
- Autonomy material packets currently contain a review-only cover letter, not a
  tailored resume that the apply queue can consume.

### Materials to form review and authorization

- `applypilot apply --url ... --dry-run` fails before inspecting the exact URL
  because it first requires at least one globally tailored database row. The
  tested live Greenhouse URL could not be dry-run from the product despite being
  explicit.
- `applypilot run --dry-run` is gated on a legacy LLM key before the dry-run
  preview executes. Only an explicitly reduced `discover enrich --dry-run`
  invocation works.
- The deterministic controller launches a copied worker user-data directory and
  attaches over local CDP. It does not use the active authenticated Chrome
  connector and can inherit stale copies of browser state. This conflicts with
  the goal's real-session constraint.
- Autonomy form review is intentionally read-only: it does not fill fields or
  upload materials. The deterministic controller can fill/upload, but it lives
  behind the disconnected legacy DB/material queue. There is no supported bridge
  between the two.
- A dry-run can create a one-time submit manifest, while the separate campaign
  subsystem requires signed fact approval, a root-owned trust store, runtime
  observations, a filesystem campaign manifest, writer leases, and evidence
  events. These layers do not share a single candidate-state record.

### Outcomes and resumability

- The jobs database, autonomy run ledger, campaign event store, submit manifests,
  and browser worker artifacts each own a different slice of state. None is the
  canonical end-to-end candidate record.
- The campaign store has strong typed confirmation and unknown-outcome behavior,
  but defaults to a target of 100 and is not wired to a command that executes an
  approved reviewed batch.
- Duplicate prevention exists independently in the jobs table and campaign
  store. The current product cannot prove that the exact reviewed autonomy
  candidate, filled form, database row, and confirmation artifact are the same
  application.

### Baseline validation versus product behavior

`96` focused contract tests passed across direct ATS, autonomy, controller, and
campaign modules. They passed while all reproduced product failures above were
present. This confirms that the current tests validate isolated contracts rather
than the useful end-to-end behavior required by the goal.

### Applicant-owned facts still required

- Phone number
- Legal authorization to work in the United States
- Whether employment sponsorship is required
- Earliest available start date / applicable recruiting window
- Confirmation that Remote, Austin, New York, San Francisco, and Chicago are the
  intended preferred-location set for application approval

Research and reversible verification may continue without these values. Filling
or submitting a form field that needs one of them must fail closed.

### Live autonomy diagnostic

A review-only autonomy run was created at
`20260715T155309006598Z-14007522e0`. Its bounded request was sent through the
authenticated real Chrome/ChatGPT session with identity and contact fields
omitted. The response returned 30 distinct official URLs and was imported.

The live trace reproduced four concrete defects:

- Search preferences disappeared between the query/configuration and the
  `CandidateProfile`; New York was therefore classified outside preferences.
- The first-party experience parser interpreted an age requirement of 18 as 18
  years of work experience.
- Input order, rather than preliminary relevance, controlled which 15 of 30
  candidates received first-party checks.
- The run advanced one role to materials while many review-state roles remained
  unresolved and without a coherent top-ten ranking.

The diagnostic emitted a material request, but it was not serviced after these
defects proved that the candidate was selected by the obsolete contract. No
form was filled and no external application action occurred.

## Phase 2 implementation decisions

### One source of truth

- `workflow.sqlite3` under the ApplyPilot application-data root is now the
  canonical record for runs, candidates, exact approvals, events, duplicate
  reservations, and submission outcomes.
- Autonomy JSON and visible-Chrome JSON are transport envelopes. They are
  imported into canonical state and do not own candidate outcomes.
- Candidate states advance from discovery through eligibility, verification,
  materials, dry-run, authorization, and outcome. A pre-approval exclusion may
  be recomputed after a matcher-policy correction; blocked, not-submitted,
  unconfirmed, confirmed, and any reserved/submitted URL remain terminal.
- Reserving a canonical URL prevents a second request. An ambiguous final action
  becomes `submitted_unconfirmed` and cannot be retried.

### Supported CLI

The supported product loop is now:

```text
applypilot doctor
applypilot prepare
applypilot dry-run
applypilot approve
applypilot execute
```

`doctor` defaults to this workflow and no longer requires an API key, root-owned
trust store, copied digest, runtime heartbeat, cloned Chrome profile, or CDP
port. `doctor --legacy` retains the old readiness diagnostic. Approval still
binds exact URLs, material digests, form-review digests, expiry, and a maximum of
three submissions.

### Matching and freshness

- Configuration locations now survive their normalized list-of-objects shape.
- Admission requires a supported role family plus an internship or explicit
  early-career signal. `analyst` and `associate` alone are insufficient.
- Manager, lead, senior, director, finance, banking, credit, lending, sales,
  audit, accounting, investor-relations, SREIT, and risk-technology titles fail
  closed.
- Clearly excluded locations are rejected before browser or model work; unknown
  locations remain review gaps and cannot receive materials.
- Experience regexes require the word `experience`, so age requirements no
  longer become experience floors.
- First-party candidates are ordered by preliminary family, level, location,
  description, and date evidence before the verification budget is spent.
- Missing freshness dates do not receive materials unless the verified rendered
  posting itself names a current or near-future recruiting cycle.
- Final ranking is a transparent 100-point family, level, skill, location, and
  first-party-open assessment with human-readable reasons and a 70-point floor.
- First-party verification evidence is cached per run so resumption does not
  refetch the same posting.

### Truthful materials and browser actions

- Cover-letter assertions remain evidence-ID bound and contract validated.
- Every prepared candidate now receives a role-specific resume produced only by
  reordering exact source bullet lines. A provenance file proves no claim was
  added or rewritten and that the source-line multiset is unchanged.
- Legacy tailoring no longer permits related-tool additions, learnable-skill
  stretches, or acceptance after a failed factuality judge.
- Dry-run packets permit visible navigation, confirmed-field entry, bound
  uploads, review-page navigation, and local evidence capture. They explicitly
  forbid submission, account creation, email, CAPTCHA/MFA bypass, and identity,
  payment, tax, or SSN entry.
- A dry-run can become ready only with a reached review state, proof that no
  final action occurred, and a non-empty local evidence artifact.
- A confirmed submission requires an ATS receipt, confirmation page, or
  confirmation identifier plus confirmation text and a durable evidence file.

### Focused validation completed

- Matching, workflow, autonomy, harness, direct-ATS, and controller regressions:
  124 passed after the matching, state, material, resume, and doctor changes.
- The complete repository suite now passes with `232 passed in 5.13s`, including
  canonical workflow, matching, evidence-bound materials, handoff binding,
  duplicate prevention, and complete Workday-posting verification regressions.
- `.venv/bin/ruff check .` and `git diff --check` pass after the latest live-run
  correctness fixes.

### Fresh canonical run

A new canonical run, `20260715T162606726866Z-14007522e0`, was created from the
strict role-family, early-career, and location query. Its discovery request was
sent through the authenticated real Chrome connector and returned ten distinct
HTTPS employer/ATS URLs with the exact request binding.

The deterministic import produced no shortlist: eight discovery candidates
were excluded and two were left `review_required`. The reason ledger showed:

- Two valid Product Manager internships were falsely rejected because the
  seniority check interpreted the word `manager` inside an explicit internship
  title as a people-management level.
- Wipfli iCIMS and Simon-Kucher CSoD links were rejected as untrusted hosts even
  though their company-bound ATS tenants were exact.
- The remaining exclusions were explicit family or function mismatches,
  including generic consulting, commercial software engineering, and business
  operations without a supported role-family phrase.

Both implementation defects are fixed and regression-covered. Product Manager
internships remain eligible while genuine manager titles still fail closed.
iCIMS, CSoD, and ReSolU tenant hosts now require exact normalized company/tenant
identity, including safe `careers-` prefixes; mismatched tenants remain rejected.
A direct live recheck observed Salesforce, Dedalus Labs, and Simon-Kucher as
first-party, resolved, HTTP 200, and open. Wipfli returned an HTTP 410 challenge
surface and therefore remains a measurement gap rather than a valid or closed
finding.

Canonical state was also hardened after review: a later review decision removes
an older prepared candidate from the active shortlist; material files are
rehash-checked before approval and submission; browser response files must match
the exact generated request path, URL, request ID, material bundle, form review,
and approval; and screenshots/receipts are copied into the private run evidence
directory with SHA-256 bindings. Reservation resumption returns the same request,
and repeated response import cannot consume an approval twice.

The single and final recovery cycle is run
`20260715T165108761671Z-14007522e0`. Its narrower exact-family query returned 11
distinct official URLs through authenticated real Chrome. The strict result is
three verified candidates, three review-only candidates, and five exclusions.
That is below the goal's target of eight clearly matching roles in a fresh top
ten, so the product records the coverage gap instead of widening into generic
finance, banking, marketing, or broad rotational programs.

The verified shortlist is:

- D. E. Shaw, Strategy and Business Development Intern, New York, Summer 2027:
  score 98; first-party page rendered open; any undergraduate field accepted;
  prior finance experience is not required.
- Bank of America, Strategy and Management Summer Analyst Program, 2027: score
  98; first-party page rendered open; business majors and the applicant's May
  2028 graduation window fit, but the employer says sponsorship is unavailable
  and the applicant's sponsorship need is still unknown.
- Dedalus Labs, Product Manager Summer 2027 Intern, San Francisco: score 85;
  first-party Ashby-backed posting rendered open; no conflicting degree major
  was observed.

Review-only coverage includes Salesforce APM and Bank of America Global
Technology Business Analyst. Their exact postings require Computer Science,
Computer Engineering, Information Systems, or a similar technical major, while
the verified applicant evidence shows a BBA and a BA in Music. ApplyPilot now
keeps explicit required-major mismatches out of materials. A JPMorganChase
marketing result remains a provider-identity measurement gap. The first cycle's
Wipfli challenge page also remains a measurement gap, not evidence that the job
is open or closed.

Workday verification was corrected during this live trace. Workday shell pages
can expose a title without the full qualifications, so the verifier now uses the
job-specific public CXS JSON payload and fails closed if the complete posting is
missing. The refreshed Salesforce payload exposed the technical-degree
requirement and moved the candidate from verified to review-only. Per-run cache
schema `v3` prevents the earlier incomplete shell evidence from being reused.

Read-only rendered-role evidence is stored in the recovery run's private
`workflow-evidence/rendered-role-checks.json`, with screenshots for Dedalus,
D. E. Shaw, both Bank of America roles, and no form actions. All three verified
candidates now have `materials_ready` cover letters, material packets, and
reordered exact-source resumes. Initial model packets failed closed on several
evidence-ID and phrasing mismatches. Bounded, validator-directed corrections
removed unsupported synthesis and converted the remaining applicant statements
to exact resume-supported wording. No unsupported claim was admitted by
weakening the validator. Each resume provenance record reports 55 source and
output lines, `claims_rewritten=false`, `claims_added=false`, and
`line_multiset_preserved=true`.

No form has been filled and no application action has occurred in either cycle.
Strict canonical doctor reports every system check ready except `Submission
facts` and `Search preferences`. The live gate still requires applicant-owned
phone, U.S. work authorization, sponsorship need, earliest start date, and at
least one explicitly confirmed work location. The locations in `searches.yaml`
(Remote, Austin, New York, San Francisco, and Chicago) remain discovery targets;
they are not treated as applicant relocation consent. Bank of America also
requires applicant confirmation that they are currently at least 18.

The integrity pass returned `ok` for canonical `workflow.sqlite3`; profile,
resume, and search-configuration SHA-256 values still match the preserved
baseline; the legacy jobs database remains 112 discovered, 112 enriched, and
zero scored, tailored, cover-lettered, applied, or errored. All generated
material and rendered-evidence files are mode `0600`.

### Natural-language semantic discovery harness

The July 30 discovery trace reproduced a prompt-shaping failure rather than a
source or eligibility failure. Prompt schema v6 serialized a 6,633-character
JSON request with 26 separate directive items and a full output schema. After
generation was stopped, ChatGPT returned a contract-valid empty candidate list,
which the importer previously accepted as useful completion.

Prompt schema v7 now describes the objective and sanitized candidate snapshot in
ordinary Markdown, asks the model to match the meaning of the work and
transferable capabilities rather than only title keywords, and requests a
reader-facing numbered list. On the exact July 30 candidate context, the prompt
is 3,087 characters, a 53.5% reduction. One response-reference footer preserves
request binding without turning the answer into a JSON protocol.

The local response normalizer accepts the natural role list, recognizes common
title/company orderings and labeled fields, prefers an explicitly labeled
official posting over discovery citations, and writes the existing internal
candidate schema. Legacy bound JSON remains compatible. An empty candidate list,
missing or mismatched response reference, unparseable role list, aggregator URL,
or later first-party/eligibility failure still fails closed. Evidence-bound
material packets remain strict JSON because their fact IDs are a truth-safety
boundary.

Focused prompt, normalization, handoff, autonomy, and canonical-workflow
validation passes 89 tests. Targeted Ruff checks and `git diff --check` also
pass. The existing July 30 prompt-v6 run was not mutated; a fresh plan is
required to exercise prompt v7.

### Fact-bound form hardening and final cached projection

A final objective audit found that canonical form requests carried only a fact
digest, not the fact records themselves; execution did not recompute the current
digest; search locations could become eligibility preferences; and the legacy
prompt invented age, background-check, felony, prior-employment, referral-source,
availability, relocation, terms-consent, veteran, and tool-experience answers.
Those are implementation defects, not applicant facts.

The canonical workflow now persists a private, digest-addressed fact snapshot
under `workflow-facts/`. Dry-run, form review, approval, and submission packets
bind its exact path and digest. Browser instructions permit only `confirmed`
records and require abstention for unknown, rejected, or missing answers. The
current snapshot may add newly confirmed values, but any change to an already
reviewed fact or the resume requires a new run. `execute` recomputes the current
snapshot before issuing every submission request and rejects approval drift.

Full first-party descriptions now receive a verified-text digest in canonical
state. Cached posting text is schema-, candidate-, and URL-bound before reuse,
then checked for explicit sponsorship, work-authorization, age, degree, and
location requirements. Search configuration no longer supplies eligibility
locations. The legacy prompt and deterministic field resolver now abstain on
missing screening facts, start date, terms consent, relocation, and required
cover-letter content instead of inventing defaults. The setup wizard now asks
for age 18 status and explicit preferred locations and no longer defaults start
date to `Immediately`.

Before the additive workflow migration, `workflow.sqlite3` was backed up to
`backups/workflow-before-verified-description-20260715.sqlite3` with mode `0600`
inside the mode-`0700` backup directory. Replaying the
recovery run used only its existing discovery response and v3 first-party cache;
it opened no browser, made no model call, and created no form action. The active
run now fails closed with five exclusions and six review-required candidates.
The five ranked posting decisions are:

- D. E. Shaw (98, materials retained): preferred location unconfirmed.
- Bank of America Strategy and Management (98, materials retained): sponsorship,
  age 18, and preferred location unconfirmed.
- Bank of America Global Technology Business Analyst (96): sponsorship, age 18,
  preferred location, and required technical-major match unconfirmed.
- Salesforce APM (92): preferred location and required technical-major match
  unconfirmed.
- Dedalus Product Manager Intern (85, materials retained): preferred location
  unconfirmed.

The three truthful material bundles remain on disk and digest-bound, but none is
currently form-eligible. The fact snapshot is mode `0600`; SQLite `quick_check`
returns `ok`; the active run has zero approvals and zero submission-registry
rows. The legacy database remains 112 discovered, 112 enriched, and zero scored,
tailored, cover-lettered, applied, or errored. Focused regressions pass 76 tests,
and the escalated repository suite passes 238 tests after the fact-bound changes.

## Next actions

1. Obtain applicant-owned phone, work-authorization, sponsorship, start-date,
   age-18 status, and preferred-location facts.
2. Exercise at least three visible form dry-runs across two ATS families and
   capture evidence without submission.
3. Present exact candidate URLs, material digests, and dry-run evidence for
   candidate-specific approval.
4. Submit at most three explicitly approved candidates and require durable ATS
   confirmation; ambiguous outcomes remain terminal and unconfirmed.

## Blocked audit

The applicant-fact blocker repeated across the original goal turn and two
automatic continuations without new applicant input. A fresh profile-state check
still reports all of the following absent or explicitly unconfirmed: phone,
street address, postal code, U.S. work authorization, current/future sponsorship
need, earliest start date and Summer 2027 availability, preferred work locations,
and current age-18 status.

The recovery run remains five `excluded` and six `review_required`, with zero
approvals and zero submission-registry rows. Opening a form, filling required
fields, approving a batch, or submitting now would require inventing applicant
facts and would violate the objective. The exact unblock input is:

```text
Phone:
Street address:
ZIP code:
Legally authorized to work in the US: yes/no
Require visa sponsorship now or later: yes/no
Earliest start date:
Available for the full Summer 2027 internship period: yes/no, dates
Currently at least 18: yes/no
Willing locations: Remote US / Austin / NYC / San Francisco / Chicago
```

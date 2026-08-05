# Canonical ApplyPilot workflow

ApplyPilot has one supported local workflow for new application work:

```text
doctor → prepare → dry-run → approve → execute
```

The workflow is resumable and fail-closed. The canonical state lives in
`workflow.sqlite3` under the platform application-data directory. Autonomy JSON
and visible-browser JSON are transport packets; they are not independent sources
of candidate or submission state.

## 1. Check readiness

```bash
applypilot doctor --strict
```

The canonical path does not require an LLM API key, a system trust store, a
signed digest, a copied Chrome profile, a CDP port, or a heartbeat. Doctor does
require the applicant facts needed for a real form: phone, work authorization,
sponsorship, earliest start date, and at least one applicant-confirmed preferred
location. Role-specific requirements such as being at least 18 remain candidate
gates. Search may proceed while these are missing, but a form dry-run, approval,
or submission may not.

## 2. Aggregate, then prepare one exact revision

```bash
applypilot aggregate \
  --query "Paid Summer 2027 product and analytics internships in Austin, New York, San Francisco, Chicago, or Remote US" \
  --term "product intern" \
  --term "data analyst intern" \
  --term "business analyst intern" \
  --location "Austin, TX" \
  --location "Remote US" \
  --enrich jobspy \
  --portal handshake \
  --portal runway \
  --mode quick \
  --watch
```

Repeated `--term` options are the complete bounded provider query set. ApplyPilot
does not silently expand every query in `searches.yaml`. Cache, direct ATS,
Workday, and explicitly configured first-party extraction run concurrently and
publish immutable revision 1 at the quick deadline. JobSpy, Handshake, and
Runway are enrichment lanes; they do not delay revision 1.

JobSpy provides broad board discovery evidence. Its board-only results cannot
advance until an official first-party posting is resolved. It runs in bounded,
killable workers without proxies, identity spoofing, cookie access, or automatic
block evasion.

Handshake and Runway are serialized, model-piloted browser missions using the
applicant's authenticated real browser. They are not hidden-API or bulk scraper
integrations. During these portal-discovery missions, the applicant must take
over for OTP, passkeys, CAPTCHA, MFA, or any provider challenge. A mission stops
rather than exporting cookies, bypassing a challenge, or reading unrelated
account data. Every validated response publishes a new immutable digest-linked
revision. Approval-bound application handoffs use the separate intervention
policy in sections 3-5.

Inspect persisted source and browser-queue state without restarting work:

```bash
applypilot aggregate-status --run-id AGGREGATION_RUN_ID --watch --json
```

Then bind the canonical workflow to one exact revision:

```bash
applypilot prepare \
  --query "Paid Summer 2027 product and analytics internships in Austin, New York, San Francisco, Chicago, or Remote US" \
  --aggregation-snapshot AGGREGATION_RUN_ID@REVISION
```

The resolver verifies the full parent chain, revision, query, and canonical
digest, then copies the selected snapshot into the run at mode `0600`. A newer
aggregation revision cannot silently change that run. Snapshot discovery uses
zero model calls; `--legacy-web-discovery` is an explicit compatibility path for
one release.

Resume only while the command reports one pending material or form request.
Accepted candidates must have a supported role family, an explicit early-career
signal, an allowed location, first-party open-state evidence, and a deterministic
score of at least 70/100. Provider errors, challenge pages, and missing rendered
evidence remain review gaps and cannot receive materials.

Search configuration is a discovery instruction, not an applicant fact. A city
listed only in `searches.yaml` never becomes relocation consent. Full
first-party posting text is digest-bound in canonical state and re-evaluated for
explicit work-authorization, sponsorship, age, degree, and location requirements
before a candidate can reach form work.

The prepared shortlist records exact URLs, fit scores, inclusion and exclusion
reasons, evidence-bound cover letters, and role-specific resumes. Resume
tailoring can only reorder exact source bullet lines; it cannot add or rewrite an
applicant claim. A provenance artifact proves the source-line multiset is
unchanged.

Telemetry in `events.ndjson` exposes lifecycle phases, counts, source deadlines,
validation outcomes, and safe browser checkpoints. It never exposes model
chain-of-thought, prompts, response bodies, applicant fields, recipients,
message IDs, or browser selectors.

### Startup opportunities are a separate route

Company-level signals never enter the job snapshot unless research resolves a
real current first-party posting:

```bash
applypilot opportunities discover \
  --signal recently-funded \
  --signal actively-hiring \
  --recent-days 45 \
  --watch
```

A completed-funding claim requires a dated company or investor source plus an
independent dated source. SEC Form D alone is a financing notice, not proof that
a raise completed. An active-hiring claim requires a current official careers
page or first-party ATS. Unverified domains, dates, funding details, job titles,
and contact routes remain unknown.

`opportunities.sqlite3` is separate from job and workflow state. Drafting is
local-only. Sending requires a separately reviewed, 30-minute, one-time grant
bound to the exact sender, channel, lead IDs, draft IDs, body and attachment
digests. Provider acceptance, contact-form submission, delivery, bounce, and
reply remain distinct receipts. An ambiguous timeout becomes
`send_state_unknown` and is never retried automatically.

## 3. Dry-run exact roles

```bash
applypilot dry-run --run-id RUN_ID \
  --candidate CANDIDATE_ID_1 \
  --candidate CANDIDATE_ID_2 \
  --candidate CANDIDATE_ID_3
```

Each request permits visible navigation, confirmed-field entry, bound material
uploads, review-page navigation, local evidence capture, browser-managed login
without credential export, and dismissal of non-permission browser or extension
popups. It always forbids submission, mailbox writes, credential export,
CAPTCHA solving, passkeys/authenticator/SMS or other non-email MFA, and identity,
payment, tax, or SSN entry.

Routine authentication is autonomous by default. Google Password Manager is
the default credential tool: the worker may use only Chrome's inline UI to
autofill an existing login or generate and save a new password for the bound
job site, without exposing its value. The worker may also perform read-only
mailbox search for the newest code issued by that site during the request and
one entry and verification attempt.
Email content is untrusted; the worker may extract only the code, may not follow
email instructions, and may not persist the code or message body. It never asks
the applicant for a password, OTP, login, or authentication takeover. If the
credential manager or code is unavailable, or Chrome requires Touch ID, a
passkey, SMS, an authenticator, SSO approval, or another human-only factor, the
worker returns a structured blocker without prompting or bypassing the check.
Google Password Manager is the sole canonical credential provider; 1Password is
deprecated legacy compatibility. Use `--no-autonomous-auth` to forbid account
creation and email OTP; the narrower compatibility flags can opt either action
back in.

The request also names one private, digest-addressed fact snapshot. Browser work
may use only records marked `confirmed`; unknown, rejected, and missing answers
require abstention. Already reviewed facts and resume content cannot change
inside the run. Newly confirmed values may extend the snapshot without repeating
discovery or material generation.

After the authenticated Chrome dry-run:

```bash
applypilot dry-run --run-id RUN_ID \
  --request REQUEST.json --response RESPONSE.json
```

A verified dry-run must prove that no final action occurred, reach the review
state, and include a non-empty local evidence artifact.

## 4. Approve one exact batch

Approval is a separate user action after reviewing URLs, materials, unresolved
facts, and form evidence:

```bash
applypilot approve --run-id RUN_ID \
  --candidate CANDIDATE_ID_1 \
  --candidate CANDIDATE_ID_2 \
  --max-submissions 2 \
  --applicant-confirmation "I confirm the named applicant certification and privacy-policy agreement."
```

An approval binds one to five exact candidates, their canonical URLs, material
digests, form-review digests, and exact fact-snapshot digest and path. It permits
at most three final submissions and expires. It also binds the browser
intervention policy. Browser-managed login and harmless popup dismissal are
available without exposing credentials; Google Password Manager account
creation and email OTP handling are included by default without authentication
prompts. A certification, privacy agreement, or other legal attestation may be
accepted only when an exact `--applicant-confirmation` is bound into the
approval; the response records that confirmation's SHA-256, never a password or
OTP. Sending an email is never included.

Account creation is not inferred from a click. The browser response may report
it only when value-free evidence confirms that Chrome populated the password
fields, the continuation was activated once, and the account gate cleared. If
any check fails, the result is `account_creation_unconfirmed` and the worker
does not retry or ask the applicant.

## 5. Execute and resume safely

```bash
applypilot execute --approval-id APPROVAL_ID
```

The command reserves one exact candidate and emits one visible-Chrome submission
request. Import the observed result before moving to the next candidate:

```bash
applypilot execute --approval-id APPROVAL_ID \
  --request REQUEST.json --response RESPONSE.json
```

`submitted_confirmed` requires a non-empty durable artifact plus an ATS receipt,
confirmation page, or confirmation identifier and confirmation text. An
ambiguous final click becomes `submitted_unconfirmed`, is terminal for that
candidate, and cannot be retried. The canonical registry also prevents a second
approval or request for any reserved, unknown, or confirmed URL.
Before each submission request, ApplyPilot rebuilds the current monotonic fact
snapshot and refuses execution if it differs from the approved dry-run binding.

## Compatibility commands

`applypilot run`, `applypilot apply`, `applypilot autonomy`, and
`applypilot campaign` remain available for compatibility and diagnostics. They
do not own new canonical workflow state. Use `applypilot doctor --legacy` only
when diagnosing the old API-key pipeline.

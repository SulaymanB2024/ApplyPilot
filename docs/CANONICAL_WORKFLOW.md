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

## 2. Discover and prepare

```bash
applypilot prepare --query \
  "Paid Summer 2027 product and analytics internships in Austin, New York, San Francisco, Chicago, or Remote US"
```

The command creates a run and one browser handoff. Service that request in the
applicant's authenticated Chrome session, save the one bare JSON response, then
resume:

```bash
applypilot prepare --run-dir RUN_DIR --response BROWSER_OUTPUT.json
```

Repeat only while the command reports one pending request. Accepted candidates
must have a supported role family, an explicit early-career signal, an allowed
location, first-party open-state evidence, and a deterministic score of at least
70/100. Provider errors, challenge pages, and missing rendered evidence remain
review gaps and cannot receive materials.

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

## 3. Dry-run exact roles

```bash
applypilot dry-run --run-id RUN_ID \
  --candidate CANDIDATE_ID_1 \
  --candidate CANDIDATE_ID_2 \
  --candidate CANDIDATE_ID_3
```

Each request permits visible navigation, confirmed-field entry, bound material
uploads, review-page navigation, and local evidence capture. It explicitly
forbids submission, account creation, email, CAPTCHA/MFA bypass, and identity,
payment, tax, or SSN entry.

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
  --max-submissions 2
```

An approval binds one to five exact candidates, their canonical URLs, material
digests, form-review digests, and exact fact-snapshot digest and path. It permits
at most three final submissions and expires. Account creation and email
applications are never included.

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

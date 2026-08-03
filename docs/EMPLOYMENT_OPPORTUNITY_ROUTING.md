# Employment opportunity routing contract

ApplyPilot must classify what an external surface represents before evaluating
candidate fit or presenting it as a job.

## Opportunity kinds

- `posted_employment`: a job-specific employee, internship, apprenticeship,
  fellowship, or fixed-term employment requisition with an application surface.
- `general_interest_application`: an employer-hosted general application or
  talent-community form without a current job-specific requisition.
- `speculative_outreach`: a company-level opportunity with a verified contact
  route but no application form.
- `marketplace_gig`: a freelance, contractor-profile, expert-network, or
  availability/rate-setting marketplace.
- `microtask_platform`: task-based annotation, rating, model-training, or
  piecework onboarding rather than a job requisition.
- `assessment_or_profile_signup`: an assessment, public profile, talent-network
  signup, or candidate marketplace without an application to a defined role.
- `unknown`: insufficient evidence to choose a safe route.

Only `posted_employment` may enter the canonical job workflow. General-interest
applications and speculative outreach use their own evidence-bound routes and
must never increment the posted-job application counter. Marketplace, microtask,
assessment/profile, and unknown surfaces fail closed.

## Application surfaces

Accepted job-specific evidence is one of:

- a provider-backed ATS requisition identifier;
- job-specific `JobPosting` structured data;
- a job-specific employer page with a discernible application form or apply
  control and title binding.

A successful HTTP response, a non-root URL, or title text alone is not proof of
an open job. A live application surface is stronger freshness evidence than a
missing posted date; date absence alone must not reject a proven live posting.

## Presentation and accounting

Raw observations are leads, not jobs. Interfaces must distinguish discovered,
verified, general-interest, speculative, and rejected records. Only durable,
positive confirmation evidence increments the matching route counter:

- posted-job ATS/application confirmation;
- general-interest form confirmation;
- outreach provider acceptance, with delivery and reply tracked separately.

Empty discovery is a valid successful result. Requested, discovered, eligible,
verified, attempted, and confirmed counts remain separate so no model benefits
from padding a list or relabeling a non-job surface.

## Model boundaries

ChatGPT Web is a browser surface, not a selectable model endpoint. ApplyPilot
sends the bounded prompt and records the surface plus any model label that can
be directly observed; it does not request or claim a ChatGPT model. Terra is
reserved for bounded Codex form-resolution and review work. Luna routing remains
limited to mechanically validated development work where the runtime supports
it.

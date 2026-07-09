# ApplyPilot Self-Improvement Harness Design

## Goal

Build a bounded development harness for ApplyPilot that lets Codex act as a
worker and reviewer around deterministic programmatic checks. The primary goal
is self-improving development, not cheapest production auto-apply.

## Design

The first version adds an `applypilot improve` CLI group with three artifact-only
commands:

- `plan`: write a scoped run contract with allowed files, forbidden boundaries,
  model policy, prompts, and validation commands.
- `worker`: produce a local proposal artifact from the plan. In v1 this is
  dry-run only and does not edit the repository.
- `validate`: run deterministic validation commands from the plan and write
  `results.json`; if a proposal exists, attach the results to it.
- `review`: deterministically review a proposal against the plan and write a
  `review.json` plus `decision.md`.

Each plan also writes a progressive-reveal knowledge packet:

- `knowledge_index.json`: compact routing metadata and retrieval policy.
- `knowledge_cards/*.json`: full case cards opened only when their
  `when_to_open` trigger matches the task.
- `research_queue.json`: bounded questions for ChatGPT Web or manual research,
  with required source and summary fields.

Generated run artifacts are local-only by default and may be written under
`.applypilot-dev/`, which is ignored by git.

## Boundaries

The harness must fail closed if a proposal:

- Uses a forbidden model such as `gpt-5.3-codex-spark`.
- Touches files outside the declared allowlist.
- Weakens dry-run semantics.
- Weakens CAPTCHA, MFA, SSO, payment, tax, identity, or unsafe-permission gates.
- Adds external email sending or external draft creation.
- Uses recursive worker delegation or unbounded repo exploration.
- Bypasses progressive reveal by loading raw ChatGPT transcripts or broad file
  dumps into the worker prompt.

Reviewer approval is never sufficient by itself. Deterministic validation
results are required before a proposal can be treated as ready to patch; the
reviewer rejects proposals that lack `results.json` evidence.

## Experiment Result

A read-only Codex CLI experiment using `gpt-5.5` with low reasoning completed,
but still used about 110k tokens because the worker explored too broadly. That
validated the need for declared file scope, no parent-directory scans, and
artifact-first prompts before any future worker execution. The knowledge layer
extends that lesson: workers get a compact index first and request only the
case cards or ChatGPT Web research questions that match the task.

## Initial Validation

Focused validation for v1:

- `python -m pytest tests/test_dev_harness.py tests/test_field_resolver.py tests/test_harness.py -q`
- `python -m ruff check src tests`

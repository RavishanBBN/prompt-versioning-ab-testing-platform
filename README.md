# Prompt Versioning and A/B Testing Platform

[![tests](https://github.com/RavishanBBN/prompt-versioning-ab-testing-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/RavishanBBN/prompt-versioning-ab-testing-platform/actions/workflows/ci.yml)

A dependency-free control plane for treating prompts like production artifacts: immutable versions, validated rendering, environment promotion, approvals, stable A/B assignment, outcome analysis, and rollback.

## What the finished MVP will prove

- Immutable, checksummed prompt versions with explicit variables.
- Safe development → staging → production promotion and rollback.
- Deterministic experiment bucketing with configurable traffic allocation.
- First-exposure logging and outcome-to-exposure integrity checks.
- Chi-square sample-ratio-mismatch detection before winner selection.
- Quality, conversion, cost, and latency measurement per variant.
- Pre-registered metrics, thresholds, and confidence-aware winner decisions.
- A CLI demo and automated tests without external services.

## Run it

```powershell
python -m unittest -v
python demo.py
```

The demo creates two prompt versions in a temporary registry, promotes version 1 through production, assigns and records the first exposure of 300 stable identities, validates the observed 50/50 traffic split, accepts only exposure-matched outcomes, selects a confidence-qualified winner using a pre-registered analysis plan, and promotes version 2.

## Code map

- `prompt_registry.py`: immutable versions, checksums, exact rendering, approval, promotion, deployed rendering, and rollback.
- `experiments.py`: lifecycle validation and deterministic weighted traffic assignment.
- `exposures.py`: append-only first-exposure records and assignment-integrity checks.
- `metrics.py`: independent outcomes, per-variant metrics, Wilson intervals, difference intervals, cost guardrails, and winner decisions.
- `demo.py`: end-to-end release and experiment story.
- `tests.py`: integrity, workflow, allocation, statistics, and guardrail tests.

## Core logic

Prompt variables are parsed before storage; rendering rejects both missing and unexpected values. Every version has a SHA-256 checksum over its identity, content, and variables. Production promotion requires the same version in staging plus approval from someone other than its author.

Experiment assignment hashes `experiment id + salt + identity` into a number in `[0,1)`, then walks cumulative allocation weights. The same identity therefore receives the same variant without storing session state. Changing the salt intentionally reshuffles traffic.

Assignment alone does not prove that a user saw a prompt. The platform records a first exposure only when the assigned prompt is actually used, then rejects outcomes without a matching experiment, identity, and variant. Analysis checks the observed exposure counts against the configured allocation with a chi-square goodness-of-fit test. A statistically unlikely split is marked as a sample ratio mismatch and blocks winner selection because it can indicate routing, instrumentation, or eligibility bugs.

For binary conversion, the dashboard reports a Wilson 95% interval, which behaves better than the simple normal interval at small samples or extreme rates. The experiment stores its control, primary metric, minimum samples, minimum effect, cost limit, and sample-ratio threshold before it runs. A winner must follow that plan, have enough independent units, clear the treatment-control confidence bound, and remain within the cost guardrail.

## Production boundaries

The JSON files make the domain rules visible, but a real service needs transactional storage, authorization, audit retention, cross-process write protection, bot/internal-traffic filtering, multiple-comparison and sequential-testing controls, and integration with the gateway and regression suite.

## Core idea

This project manages prompts like product code. It stores prompt versions, tracks who changed what, runs A/B tests, measures quality and business metrics, and supports rollback. Teams can test prompt changes on a small percentage of traffic before full release.

The simple presentation line is: this is GitHub plus experimentation for prompts.

## Problem it solves

Prompts are often edited casually in code, spreadsheets, or dashboards. That makes it hard to know which prompt is running, why it changed, whether it improved quality, and how to roll it back. This platform brings version control and experimentation to prompt engineering.

## Main users

- Prompt engineers.
- Product managers.
- AI application developers.
- QA and evaluation teams.

## MVP scope

1. Prompt registry.
2. Prompt version history.
3. Environment support: development, staging, production.
4. Variable templating.
5. A/B traffic splitting.
6. Metrics collection.
7. Rollback.
8. Approval workflow for production prompts.

## Architecture

The system has five main parts:

1. Prompt store: stores prompt text, variables, metadata, versions, and owners.
2. Rendering service: fills variables and returns the final prompt.
3. Experiment engine: assigns users or requests to prompt variants.
4. Metrics collector: captures quality, latency, conversion, cost, and feedback.
5. Admin UI: lets teams create, compare, approve, deploy, and roll back prompts.

## Step-by-step build plan

1. Define prompt object model.
   Decision: prompts need name, version, variables, owner, environment, status, and changelog.

2. Build versioning.
   Decision: every edit creates a new immutable version so teams can audit changes.

3. Add prompt rendering.
   Decision: variables should be explicit and validated to prevent missing or broken prompt inputs.

4. Add environments.
   Decision: development, staging, and production reduce accidental release risk.

5. Add A/B testing.
   Decision: prompt quality is hard to know in advance, so real traffic experiments are important.

6. Add metrics.
   Decision: compare prompts using quality, cost, latency, and product outcomes, not only subjective preference.

7. Add rollback.
   Decision: teams need fast recovery when a prompt performs badly.

8. Add approvals.
   Decision: production prompts affect users, so high-impact changes should be reviewed.

## Important decisions and why

- Immutable versions: preserves audit history.
- Explicit variables: prevents broken prompts.
- Environment separation: reduces production mistakes.
- A/B tests: measures prompt performance with real users.
- Rollback: makes prompt changes safer.
- Integration with gateway and evals: allows traffic routing, cost tracking, and regression testing.

## Demo flow

1. Create prompt version 1.
2. Create prompt version 2 with improved instructions.
3. Run an A/B test with 50/50 traffic.
4. Show metrics by variant.
5. Promote the winner to production.
6. Roll back to version 1 if quality drops.

## Success metrics

- Prompt release frequency.
- Rollback time.
- Quality improvement from experiments.
- Reduction in prompt-related incidents.
- Experiment completion time.
- Adoption by AI product teams.

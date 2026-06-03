---
name: boss-software-factory
description: Use when implementing or reviewing BOSS software-factory flows, PR proposal artifacts, review packets, real GitHub sandbox effects, coding worker adapters, or CI gates.
---

# BOSS Software Factory Skill

## When to use

Use this skill for any task involving:

- Spec → implementation plan → diff/test/review packet → PR proposal
- Mock GitHub adapter or later real GitHub adapter
- Coding worker adapter boundaries
- CI/test result artifacts
- PR merge decision cards

## Invariants

- Real merges are not allowed in v0.
- Mock PR creation must still use a receipt and dedupe key.
- Every software change must have a review packet artifact.
- Every PR proposal must include tests run, risk summary, rollback plan, and references.

## Required review packet fields

- task_id
- spec_artifact_id
- implementation_summary
- files_changed
- tests_added_or_run
- risk_hotspots
- rollback_plan
- confidence_score
- recommendation

## Tests to add/update

- Mock PR effect dedupes by `(org_id, dedupe_key)`.
- Review packet is created before merge decision.
- Low-confidence packet creates hold/revise decision rather than merge recommendation.

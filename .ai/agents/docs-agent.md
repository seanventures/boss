# Codex Docs Agent Slice

**Status**: IMPLEMENTED LOCALLY — publication pending
**Updated**: 2026-07-15T16:10Z
**Plane**: BOSS-73, BOSS-74, BOSS-75
**Branch**: `ai/codex/docs-agent` stacked on draft PR #17

## Scope

- Durable allowlisted/manual `docs.update` jobs with entity/window dedupe and latest-event coalescing.
- Evidence-grounded `docs_writer` role with Markdown-only template and human-prose preservation.
- Budgeted, Receipt-backed stable-branch draft docs PR plus TRACEABILITY/catalog reconciliation.

## Safety decisions

- Model and GitHub work starts only after a docs-specific reservation.
- One provider Receipt deduplicates the bounded docs PR effect.
- Restricted/local-only data is blocked with a Decision.
- Output paths are relative `.md` files; no arbitrary repository writes.
- GitHub PRs are draft-only and no merge method is invoked.
- Failures release reservations, emit `docs.update_failed`, and flow through worker retry/deadletter.

## Validation

- `apps/api/tests/test_docs_agent.py`: 7 passed.
- Reactive foundation: 7 passed.
- Worker suite: 27 passed.
- Focused Ruff/compile/YAML, RoleSpec load, generator drift/unit, and strict MkDocs: pass.

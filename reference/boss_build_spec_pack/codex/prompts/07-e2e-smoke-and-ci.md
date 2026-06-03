# Codex Prompt 07 — End-to-end smoke tests and CI

Goal: Add reliable smoke tests and CI checks for the BOSS v0 vertical slice.

Context to read first:

- AGENTS.md
- specpack/evals/*.yaml
- docs/BUILD_PLAN.md

Constraints:

- Smoke tests should be deterministic and use mock providers.
- CI should not require real secrets or external services beyond local test dependencies.

Implement:

1. E2E test: command → task → job → worker → artifact → decision → approval → event feed.
2. E2E test: duplicate side effect uses same receipt.
3. E2E test: budget denial creates decision and prevents lease/execution.
4. GitHub Actions or equivalent CI config.
5. `make smoke` command.

Done when:

- CI runs lint/tests/smoke.
- All tests pass locally.
- README documents the verification loop.

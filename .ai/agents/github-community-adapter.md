# Codex GitHub Community Adapter

- Owner: Codex
- Plane: BOSS-85 (spec 08 T2), BOSS-89 (spec 08 T6)
- Branch: `ai/codex/github-community-adapter`
- Base: `ai/codex/webhook-foundation` (draft PR #16)
- Scope: GitHub issue/PR comment writes, inline review writes, unified diff reads, App permissions/manifest, setup docs, and unconfigured-secret fail-closed behavior.
- Safety: all provider calls reuse the existing GitHub adapter authentication boundary; no credential was added; future callers remain responsible for Receipt and budget policy before effects; no merge or code execution is introduced.
- Runtime behavior: review events are restricted to COMMENT/APPROVE/REQUEST_CHANGES and default to COMMENT; raw diff uses GitHub's diff media type; absent inbound secret returns 503 before parsing or persistence.
- Tests: `apps/api/tests/test_github_community_adapter.py` and `apps/api/tests/test_webhook_foundation.py` — 18 passed.
- Static validation: focused Ruff, Python compile, JSON manifest parse, and OpenAPI YAML parse pass.
- Baseline: repository-wide web type/lint passes; broad Python lint retains 13 unrelated findings already present on the parent stack, with no changed-file regression.
- Remaining dependencies: T1 expansion for issues/reviews, T3 triage, T4 PR review, T5 decisions/human gate, public-abuse throttles, and community budget caps.

# Codex Orchestration Agent Slice

- Date: 2026-07-15
- Branch: `ai/codex/conversational-orchestrator`
- Plane: BOSS-78, BOSS-79, BOSS-80, BOSS-81, BOSS-82 (In Progress until merge)
- Scope: persisted chat/session API, surface-neutral intent routing, canonical capability manifest, confirmed fan-out, and thin console surface.
- Safety: every actuator rechecks project membership; disabled capabilities fail closed; model classification is opt-in, budget-reserved, Receipt-backed, and deduplicated; child tasks require exact human confirmation.
- Tests: `apps/api/tests/test_orchestration_agent.py` (9 acceptance cases), `apps/web/tests/api-client.test.ts`, changed-file Ruff, Python compile, web TypeScript/typegen, OpenAPI parse.
- Coordination: stacked on `ai/codex/github-community-adapter`; no code from the design-system PR or sibling docs-agent PR was copied. The GitHub agent slices may reuse `IntentRouter` and `surface=github` threads.

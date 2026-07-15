# Codex Skill Executor Registry

- Owner: Codex
- Plane: BOSS-45 / OSS readiness spec 03 A1
- Branch: `ai/codex/skill-executor-registry`
- Base: refreshed `origin/main`
- Scope: executor registration/resolution plus safe GitHub PR and Deployment Registry bindings.
- Safety: resolution is fail-closed without server context; unknown/unbacked skills return `None`; GitHub targets must match configured repository; every GitHub effect opens a Receipt and respects dedupe; no worker/API real-mode relaxation is included.
- Tests: `apps/api/tests/test_skill_executors.py` — 5 passed.
- Static validation: focused Ruff passed.
- Remaining dependency: BOSS-46/A2 wires the registry into trusted leased workers and reconciles real invocation cost.

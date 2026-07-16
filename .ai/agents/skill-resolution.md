# Codex Skill Resolution + Agent Bridge

- Owner: Codex
- Plane: BOSS-53 / BOSS-54 / BOSS-47; OSS readiness spec 03 B3/B4/A3
- Branch: `ai/codex/boss-47-53-54-skill-resolution`
- Base: `ai/codex/boss-46-51-52-skill-foundation` / draft PR #24
- Scope: effective-edition catalog filtering, per-project registry overlay, per-task module/system/tenant union, RoleSpec filtering, eager/on-demand separation, collision-safe agent schemas, real SkillRuntime dispatch, and approval suspension/resume.
- Safety: all real skill execution stays behind SkillRuntime permission/budget/retry gates and registered server executors; high-risk calls create blocking decisions; approved resumes revalidate current pack/task availability; no provider or GitHub call is added directly.
- Tests: `apps/api/tests/test_skill_resolution.py`, `test_skill_registry.py`, `test_skill_executors.py`, plus focused `test_api.py` skill/runtime/role regressions.
- Remaining dependency-safe work: P1 B5 on-demand enable/disable/lazy attach, B6 strict id validation/hot-path violation evidence, then A4/A5/A6 marketplace/MCP packaging layers.

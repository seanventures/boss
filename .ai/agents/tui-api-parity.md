# TUI/API parity handoff — 2026-09-24

- Branch: `ai/codex/tui-api-parity` from `origin/main` `9a72bd9a`.
- Plane: BOSS-430 In Progress; related completed BOSS-367 (HTTP API) and
  BOSS-414 (headless code command), and active BOSS-429 (MCP security/parity).
- Scope: add `/api METHOD /v1/path [JSON]` to the Metatron terminal and
  `--exec ... --json` for non-interactive use. This reuses the API's existing
  authentication, policy, and audit path. No new server endpoint or side effect
  path is added.
- Security: generic requests accept local `/v1/` paths only, reject path
  traversal and fragments, never follow redirects with the API key, and redact
  the key from responses and errors.
- Evidence: 17 `scripts/tests/test_metatron_tui.py` tests pass; targeted Ruff
  and `git diff --check` pass. No live API or VM validation in this slice.
- Remaining: OpenAPI route parity check currently fails on 197 implemented
  routes absent from `api/openapi.yaml` (BOSS-416 Todo). MCP is engine-tool
  scoped and not a complete mirror of the HTTP API; BOSS-429 owns the next
  security/capability slice. Terminal usability for each workflow still needs
  live operator acceptance testing.

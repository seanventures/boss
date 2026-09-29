# TUI/API parity handoff — 2026-09-24

## 2026-09-29 decision workflow follow-up

- Branch `ai/codex/boss-430-tui-decision-inbox` builds on the private Community
  integration source `9a5c31e2`; draft PR follows against that integration.
- `/decisions [open|all] [PAGE]` and `/decision ID` make the decision inbox
  directly discoverable. `/decision approve|reject ID NOTE` uses a selected-
  project detail read before the existing governed decision transition API.
  It cannot grant a separate human confirmation or bypass API role, risk,
  transition, budget, receipt, or audit controls.
- Focused TUI suite: 51 tests passed, including selected-project mismatch,
  approval denial, and rejection routing. Ruff and diff checks passed. No live
  operator acceptance or VM test was run for this later slice.
- Other decision transitions and most of the 986 Community operations remain
  generic `/op` commands; static addressability is separate from end-to-end
  feature qualification.

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

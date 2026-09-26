# Community MCP handoff — BOSS-429

Branch: `ai/codex/community-mcp` from `origin/main` at `9a72bd9a`.

The engine MCP server already exposed the shared 106-tool registry (61 read,
45 write). This slice hardens that boundary: write-capable HTTP requires a
Bearer token and a loopback bind; MCP tool calls cannot supply project or
internal operator-confirmation authority; malformed JSON-RPC returns errors.
The Community Mission Control snapshot page accepts a token in browser memory
when the write-capable HTTP server requires one.

The HTTP API has a broader route catalog. Keep API/TUI parity work on the
control-plane surfaces; extend MCP with focused governed tools as agent
workflows require them. MCP must not become an unrestricted API proxy.

Validation: `engine/tests/test_mcp.py` and `engine/tests/test_mcp_http.py`
(20 passed), focused Ruff, diff check, and a full 106-tool MCP schema audit.
No database migration or provider effect is involved.

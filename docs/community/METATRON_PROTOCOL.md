# Metatron Server Protocol (`MT-10`)

This document defines the HTTP and Server-Sent Events (SSE) contract between the Rust Metatron CLI (`metatron`) and the BOSS Control Plane (`/v1`).

> Legacy note: `POST /v1/metatron/session` and `scripts/metatron_tui.py` remain intact for `boss metatron --legacy` until retired per the two-release migration plan in [Terminal and API](TERMINAL_API.md). All streaming turns use the `/v1/metatron/turns` contract below and inherit the financial-authority guarantees of [Conversation stream authority](CONVERSATION_STREAMS.md).

## Contract Versioning & Headers

Clients may send:
- `X-Metatron-Client: metatron/<semver> (<os>; <arch>)`
- `X-Boss-Contract: <integer>`

Server behavior on `/v1/` routes:
- Every `/v1/` response includes `X-Boss-Version: <version>`.
- When `X-Metatron-Client` or `X-Boss-Contract` is **absent**, requests pass through normally (preserving legacy TUI and console callers).
- When either header is **present** and below minimum (`min_client: "0.1.0"`, `min_contract: 1`), the server responds with HTTP `426 Upgrade Required`:
  ```json
  {
    "error": {
      "code": "client_too_old",
      "message": "Metatron client is unsupported; minimum required is 0.1.0.",
      "retryable": false,
      "min_client": "0.1.0"
    }
  }
  ```

### Capabilities Discovery

`GET /v1/metatron/capabilities` (requires authentication) returns:
```json
{
  "contract": 1,
  "min_client": "0.1.0",
  "boss_version": "0.2.1",
  "features": [
    "turns.stream",
    "tools.client",
    "policy",
    "audit",
    "device_login",
    "credential_lease"
  ],
  "limits": {
    "tool_result_inline_bytes": 65536,
    "turn_deadline_s": 900
  }
}
```

## Authentication (RFC 8628 Device Login & Token Lifecycle)

All auth endpoints below carry `Cache-Control: no-store`. `Authorization: Bearer <token>` and `X-Boss-Api-Key: <key>` remain accepted on `/v1/*`.

1. **Start device authorization:** `POST /v1/auth/device/code`
   - Request: `{"client_id": "metatron", "scope": "metatron"}` (`metatron` or `metatron:read`).
   - Response (`200`): `{"device_code", "user_code", "verification_uri", "verification_uri_complete", "expires_in", "interval"}`.
2. **User approval:** Operator opens `verification_uri_complete` in a signed-in browser session and approves via `POST /v1/mcp-oauth/device/approve`.
3. **Poll for token:** `POST /v1/auth/device/token`
   - Request: `{"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": "..."}`.
   - Pending/error (`400`): `{"error": "authorization_pending" | "slow_down" | "expired_token" | "access_denied"}`.
   - Approved (`200`): `{"access_token", "refresh_token", "token_type": "Bearer", "expires_in", "scope", "principal": {"id", "actor", "display_name", "roles"}}`.
   - `access_token` is a scoped `boss_pat_` credential bound to the approving person.
4. **Refresh token rotation:** `POST /v1/auth/token`
   - Request: `{"grant_type": "refresh_token", "refresh_token": "..."}`.
   - Rotates both `access_token` and `refresh_token`. Presenting an already-spent refresh token immediately revokes the entire grant and active key (RFC 9700).
5. **Revocation:** `POST /v1/auth/revoke`
   - Request: `{"token": "..."}` (refresh token or access key). Immediately revokes the OAuth grant and `boss_pat_` key.

## Streaming Agent Turn (`/v1/metatron/turns`)

### Start Turn: `POST /v1/metatron/turns`

Headers: `Accept: text/event-stream`, `Idempotency-Key: <uuid>`. Requires `assistant:chat` at `project_id`.

Request body:
```json
{
  "project_id": "boss-dogfood",
  "thread_id": null,
  "agent": null,
  "message": "check host cpu with sar",
  "mode": "ask",
  "client": {"name": "metatron", "version": "0.1.0", "os": "macos", "arch": "arm64"},
  "context": {"cwd_label": "~/work/ops", "agents_md": "..."},
  "client_tools": [
    {"name": "ssh_run", "description": "...", "input_schema": {}, "risk_class": "remote-read", "exposure": "direct"}
  ]
}
```

Server invariants:
- Filters server tools and `client_tools[]` by principal permissions, `mode` (`read-only | ask | risky-only | act-on-my-behalf`), and `tool_policies_v2`. Forbidden tools are never exposed to the model and are listed in `turn.started.dropped`.
- Each model round-trip commits `jobs`, `BudgetGovernor.reserve`, `model_gateway_leases`, `model_gateway_invocations`, and `receipts` (unique `dedupe_key`) **before** contacting the routed model adapter. No DB transaction or model lease is held while waiting in `awaiting_tool_result`.
- Uncertain post-contact failures retain the unresolved receipt and reservation and raise a `conversation_reconciliation` decision, emitting `turn.failed` (`code: "unresolved_receipt"`).
- Idempotency:
  - `409 Conflict` on concurrent in-progress request with the same `Idempotency-Key`.
  - `422 Unprocessable Entity` if the same `Idempotency-Key` is reused with a different method/path/query/body.
  - Exact SSE byte replay (`Idempotency-Replayed: true`) on a completed bounded stream (after re-verifying current principal permissions -> `403` if revoked).

### SSE Frame Format & Events

Every event carries `id: <turn_id>:<seq>`, `event: <type>`, `data: <json>`. Keepalive comment `: ping` is sent every `15` seconds during inactivity.

| Event | Payload | Notes |
|---|---|---|
| `turn.started` | `{"turn_id","thread_id","model","mode","tools":[...],"dropped":[{"name","reason"}]}` | Always `seq=1` |
| `text.delta` | `{"text":"..."}` | Assistant stream delta |
| `server_tool.started` | `{"call_id","name","summary","ok"}` | Server-executed tool started |
| `server_tool.finished` | `{"call_id","name","summary","ok"}` | Server-executed tool finished |
| `tool_call` | `{"call_id","name","arguments":{},"risk_hint","deadline_s"}` | Turn enters `awaiting_tool_result` |
| `policy.denied` | `{"call_id","reason","rule_id"}` | Call blocked by policy |
| `usage` | `{"input_tokens","output_tokens","cost":{"amount","currency","source"}}` | Settled step usage |
| `turn.completed` | `{"turn_id","stop_reason":"end_turn|max_tokens|cancelled"}` | Terminal success/cancel |
| `turn.failed` | `{"turn_id","code","message","retryable"}` | Terminal failure |

### Submit Client Tool Result: `POST /v1/metatron/turns/{turn_id}/tool_results`

- Headers: `Idempotency-Key: <turn_id>:<call_id>`.
- Body: `{"call_id", "status": "ok|error|denied|cancelled", "output": {...}, "decision": {"by","rule_id","mode"}}`.
- Returns `202 Accepted`. Output serialized above `65536` bytes returns `413 Payload Too Large` (`code: "tool_result_too_large"`).

### Resume & Cancel

- `GET /v1/metatron/turns/{turn_id}/events` with `Last-Event-ID: <turn_id>:<seq>` replays missed persisted events from `metatron_turn_events` without making another model call, then tails live events if the turn is active.
- `POST /v1/metatron/turns/{turn_id}/cancel` returns `202 Accepted` and finishes the stream with `turn.completed {stop_reason: "cancelled"}`.

## Signed Policy Bundle, Reviewer & Grants

- `GET /v1/metatron/policy?project_id=...` (`policy:read`, zero DB writes, honors `If-None-Match` -> `304`, sets `ETag`): returns `{"bundle", "jws", "kid", "alg": "EdDSA", "typ": "boss-policy+jws"}`. Rules include `hard: true` flags and `examples: {"match", "not_match"}` validated (`422` on invalid examples) when published via `/v1/tool-policies`.
- `GET /v1/metatron/policy/keys` (public JWKS, zero DB writes): returns `{"keys": [...]}` Ed25519 public JWKs.
- `POST /v1/metatron/grants` (`autopilot:operate`) & `DELETE /v1/metatron/grants/{grant_id}` (`self_only`): creates/revokes TTL-bounded (`60..86400` s) `act-on-my-behalf` grants.
- `POST /v1/metatron/approvals/review` (`assistant:chat`): server-side budgeted reviewer (`AgentService.decide` + `BudgetReservation` + `receipts`). Requires an active grant and **never** approves `hard: true` rules, `forbidden` decisions, or `capture` / `secret-use` / `app-control` risk classes.

## Audit Ingest & Hosts

- `POST /v1/metatron/audit` (`assistant:chat`): batch ingest into `metatron_audit_events`, idempotent by `event_id`. Rejects any event containing credential-shaped values or keys (`cookie_values`, `token`, `raw_secret`, `file_contents`, `private_key`, `authorization`, `password`, URL credentials, or secret token patterns) in `rejected[]` while accepting valid events.
- `GET /v1/metatron/hosts?project_id=...` (`host:read`, zero DB writes): returns registered hosts including `host_key_known_hosts` and `host_key_fingerprint`.
- `POST /v1/metatron/hosts/{host_id}/credential-lease` (`host:exec`, `step_up=True` under `BOSS_AUTHZ_MODE=enforce`): returns a short-lived (`15..300` s), single-use TLS SSH-key lease (`Cache-Control: no-store`) backed by `resolve_secret_ref` (`secret_access_events` + `run_secret_redaction`), `receipts`, and `metatron_credential_leases`.

## Error Codes

All structured errors return `{"error": {"code", "message", "retryable"}}`.

| Status | Meaning / Codes |
|---|---|
| `401` | Unauthenticated, expired token, or `step_up_required` |
| `403` | Permission or tenant boundary refusal (`policy_denied`, `forbidden`) |
| `409` | Idempotency in-progress conflict or terminal turn conflict |
| `413` | `tool_result_too_large` (> 65536 bytes) |
| `422` | Reused `Idempotency-Key` with changed body, invalid rule examples, invalid fields |
| `426` | `client_too_old` (includes `min_client`) |
| `429` | Rate limit or `budget_exhausted` |
| `5xx` | Retryable server/provider failure (`unresolved_receipt` triggers reconciliation) |

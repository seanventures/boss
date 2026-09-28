# Core control plane API

Commands → tasks → plans → jobs → receipts/events, with budget gates and human decisions.

| Prefix | Role |
|--------|------|
| `/v1/commands` | CEO / operator command intake |
| `/v1/tasks` | Durable work units |
| `/v1/plans` | Multi-phase plans + dispatch |
| `/v1/decisions` | Approval inbox + resolve |
| `/v1/jobs` | Leased executable work |
| `/v1/worker/*` | Worker claim / heartbeat / complete |
| `/v1/receipts` | Effect evidence by `dedupe_key` |
| `/v1/events` | Append-only audit feed |
| `/v1/budget/*` | Quote / reserve / consume / release |
| `/v1/deadletters` | Visible failures |

See [ROUTE_INDEX.md](ROUTE_INDEX.md) for the full method list and [README.md](README.md) for auth.

Assume:

```bash
export BOSS_API=http://127.0.0.1:8000
export BOSS_KEY=dev-key
export PROJECT=<project_id>
AUTH=(-H "Authorization: Bearer $BOSS_KEY" -H "Content-Type: application/json")
```

---

## Commands

### Propose a command

`POST /v1/commands/propose` — rate-limited high-cost path. Permission: `command_submit`.

Creates a task, intake job, and often a plan artifact.

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"command\": \"Add rate limiting to the public webhook endpoint\",
  \"constraints\": {\"risk_tier\": \"standard\"}
}" "$BOSS_API/v1/commands/propose" | jq .
```

Typical response fields: `status`, `task_id`, `job_id`, `artifact_id`, `plan_id`, `phases`.

---

## Tasks

| Method | Path | Notes |
|--------|------|--------|
| `GET` | `/v1/tasks?project_id=` | Paginated (`limit` 1–200, `offset`) |
| `POST` | `/v1/tasks` | Create; needs `task_create` |
| `GET` | `/v1/tasks/{task_id}` | Single task |
| `DELETE` | `/v1/tasks/{task_id}?project_id=&actor=` | Soft-archive (`status=archived`) |

### Create task

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"title\": \"Implement webhook HMAC verification\",
  \"description\": \"Fail closed on bad signatures\",
  \"dedupe_key\": \"task:webhook-hmac:v1\",
  \"priority\": 50
}" "$BOSS_API/v1/tasks" | jq .
```

Tasks carry `status`, `priority`, `risk_tier`, `data_boundary`, `dedupe_key`, dependencies, and latest job status on list views.

---

## Plans

| Method | Path | Notes |
|--------|------|--------|
| `GET` | `/v1/plans?project_id=` | List |
| `POST` | `/v1/plans` | Create plan with phases |
| `POST` | `/v1/plans/{plan_id}/dispatch` | Materialize phases as child tasks/jobs |

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"title\": \"Webhook hardening\",
  \"objective\": \"Signature verify + metrics\",
  \"task_id\": \"<parent_task_id>\",
  \"phases\": [
    {\"name\": \"design\", \"summary\": \"Threat model\"},
    {\"name\": \"implement\", \"summary\": \"Code + tests\"},
    {\"name\": \"verify\", \"summary\": \"Canary + review\"}
  ]
}" "$BOSS_API/v1/plans" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\"
}" "$BOSS_API/v1/plans/<plan_id>/dispatch" | jq .
```

---

## Jobs & worker loop

| Method | Path | Notes |
|--------|------|--------|
| `GET` | `/v1/jobs?project_id=` | Filters: `status`, `kind`, `task_id`, `include_terminal` |
| `GET` | `/v1/jobs/{job_id}` | Detail |
| `POST` | `/v1/jobs` | Enqueue (`kind`, `payload`, optional reservation); requires `workflow_execute` in the project and the relevant effect grant for infrastructure, GitHub, communications, integrations, or ProviderOps jobs |
| `POST` | `/v1/worker/poll` | Claim with lease TTL; configured worker principal only |
| `POST` | `/v1/worker/heartbeat` | Extend lease; worker principal and lease token |
| `POST` | `/v1/worker/complete` | Finish; worker principal and lease token |
| `POST` | `/v1/worker/fail` | Fail / deadletter; worker principal and lease token |

When API authentication is enabled, every API key must map to a project member through
`BOSS_API_KEY_PRINCIPALS`. Worker calls require the principal named by
`BOSS_WORKER_PRINCIPAL` (default `ceo` for the Community installer).

### Create job

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"kind\": \"echo\",
  \"task_id\": \"<task_id>\",
  \"payload\": {\"message\": \"hello\"},
  \"priority\": 100,
  \"requires_reservation\": false
}" "$BOSS_API/v1/jobs" | jq .
```

### Worker poll / heartbeat

```bash
curl -sS "${AUTH[@]}" -d '{
  "worker_id": "worker-local-1",
  "capabilities": ["echo", "code_task"],
  "lease_ttl_seconds": 60
}' "$BOSS_API/v1/worker/poll" | jq .

# When leased:
curl -sS "${AUTH[@]}" -d '{
  "worker_id": "worker-local-1",
  "job_id": "<job_id>",
  "lease_token": "<token>",
  "lease_ttl_seconds": 60
}' "$BOSS_API/v1/worker/heartbeat" | jq .
```

Leases are mandatory: expired or mismatched tokens fail closed.

---

## Decisions

| Method | Path | Notes |
|--------|------|--------|
| `GET` | `/v1/decisions/inbox?project_id=` | Open decisions |
| `POST` | `/v1/decisions/{id}/approve` | Approve |
| `POST` | `/v1/decisions/{id}/reject` | Reject |
| `POST` | `/v1/decisions/{id}/revise` | Request changes |
| `POST` | `/v1/decisions/{id}/delegate` | Delegate |
| `POST` | `/v1/decisions/{id}/execute` | Execute approved action |
| `POST` | `/v1/decisions/digests` | Bundle urgent vs batched |

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/decisions/inbox?project_id=$PROJECT" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"rationale\": \"Risk accepted for staging only\"
}" "$BOSS_API/v1/decisions/<decision_id>/approve" | jq .
```

Permission for resolve paths is `decision_resolve` (and actor binding when principals are mapped).

---

## Events & receipts

### Event feed

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/events?project_id=$PROJECT&limit=50" | jq .
```

Events are the operator-visible audit trail (what happened and why). Policy denials, budget denials, and provider failures emit events.

### Receipts

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/receipts?project_id=$PROJECT" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/receipts/<dedupe_key>" | jq .
```

Rule: **no external effect without a Receipt**; **dedupe_key** prevents double effects.

### Deadletters

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/deadletters?project_id=$PROJECT" | jq .

# Requeue after fix
curl -sS "${AUTH[@]}" -d "{\"actor\": \"ceo\"}" \
  "$BOSS_API/v1/deadletters/<deadletter_id>/requeue" | jq .
```

---

## Budget

Amounts are **USD micros** (1 USD = 1_000_000 micros).

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/v1/budget/status?scope_type=project&scope_id=` | Cap, reserved, actual, burn |
| `GET` | `/v1/budget/allocations?project_id=` | Allocations |
| `POST` | `/v1/budget/allocations` | Create allocation (`budget_manage`) |
| `GET` | `/v1/budget/insights?project_id=` | Cost-save insights |
| `GET` | `/v1/budget/forecast` | Forecast helpers |
| `POST` | `/v1/budget/quote` | Dry-run affordability |
| `POST` | `/v1/budget/reserve` | Hold funds (`budget_manage`) |
| `POST` | `/v1/budget/consume` | Settle with receipt |
| `POST` | `/v1/budget/release` | Release unused hold (`budget_manage`) |
| `POST` | `/v1/budget/set_intensity` | CEO intensity 0–100 (`budget_manage`) |

Project settings writes require `providerops_manage`. Creating, changing, or deleting an event
subscription requires `integration_manage` in that project.

### Status

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/budget/status?scope_type=project&scope_id=$PROJECT" | jq .
```

### Quote → reserve → consume

```bash
# 1) Quote $0.50 model spend
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"category\": \"model\",
  \"amount_usd_micros\": 500000,
  \"reason\": \"code_task planning call\"
}" "$BOSS_API/v1/budget/quote" | jq .

# 2) Reserve
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"category\": \"model\",
  \"amount_usd_micros\": 500000,
  \"reason\": \"code_task planning call\",
  \"job_id\": \"<job_id>\",
  \"expires_in_minutes\": 30
}" "$BOSS_API/v1/budget/reserve" | jq .

# 3) After work + receipt
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"reservation_id\": \"<reservation_id>\",
  \"receipt_id\": \"<receipt_id>\",
  \"category\": \"model\",
  \"skill_or_model_id\": \"openai:gpt-4.1\",
  \"estimated_usd_micros\": 500000,
  \"actual_usd_micros\": 420000,
  \"actor\": \"system\"
}" "$BOSS_API/v1/budget/consume" | jq .

# Or release if cancelled
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"reservation_id\": \"<reservation_id>\"
}" "$BOSS_API/v1/budget/release" | jq .
```

If the quote/reserve is denied, expect a decision and/or budget denial event — not a silent no-op.

---

## End-to-end vertical slice

1. `POST /v1/commands/propose` → `task_id` + `job_id`  
2. Worker `poll` → lease → execute → `complete` with receipts  
3. Budget reserve/consume around model/tool spend  
4. Watch `GET /v1/events` and `GET /v1/receipts`  
5. Resolve any `GET /v1/decisions/inbox` items before destructive steps  

Related product docs: PRD/TECHNICAL_SPEC v2.0, `packages/specpack/workflows/ceo_command_to_pr.yaml`.

# Agents, skills, model gateway & A2A

Surfaces for defining agents, invoking skills, leasing models, and agent-to-agent messaging.

| Prefix | Role |
|--------|------|
| `/v1/agent-studio/*` | Agent/panel definitions, versions, runs, tests |
| `/v1/agent-runs` | Historical / resume runs |
| `/v1/agent/*` | Chat threads |
| `/v1/agent-modules`, `/v1/packs` | Installable modules & packs |
| `/v1/skills/*` | Skill registry, import/export, invoke |
| `/v1/toolchain/*` | Model-agnostic toolchain intake |
| `/v1/model-gateway/*` | Credentials, leases, invocations, usage, rate-limits |
| `/v1/pipeline-adapters/*` | Pipeline adapter catalog |
| `/v1/features/*`, `/v1/capability-manifest*` | Feature catalog & truthful capability manifest |
| `/v1/learning/*`, `/v1/intent/*` | Learning loop corpus + intent classify/route |
| `/v1/telemetry/*`, `/v1/change-correlation/*` | Telemetry query connectors + change correlation |
| `/v1/a2a/*` | Channels, messages, poll, audit |
| `/v1/shepherd/*` | Task shepherd overview / propose / sweep |
| `/v1/agent-groups/*` | Governed multi-agent groups (also in governance) |

Auth: [README.md](README.md). Product deep-dive: `docs/product/AGENT_STUDIO_A2A.md`.

```bash
export BOSS_API=http://127.0.0.1:8000
export BOSS_KEY=dev-key
export PROJECT=<project_id>
AUTH=(-H "Authorization: Bearer $BOSS_KEY" -H "Content-Type: application/json")
```

---

## Agent Studio

| Method | Path |
|--------|------|
| `GET` | `/v1/agent-studio/catalog` |
| `POST` | `/v1/agent-studio/catalog/seed` |
| `POST` | `/v1/agent-studio/catalog-smoke` |
| `GET/POST` | `/v1/agent-studio/agents` |
| `POST` | `/v1/agent-studio/agents/{id}/versions` |
| `POST` | `/v1/agent-studio/agent-versions/{id}/publish` |
| `POST` | `/v1/agent-studio/agent-versions/{id}/validate` |
| `POST` | `/v1/agent-studio/agent-versions/{id}/tests` |
| `POST` | `/v1/agent-studio/agent-versions/{id}/runs` |
| `GET/POST` | `/v1/agent-studio/panels` (+ versions/publish/runs) |
| `POST` | `/v1/agent-studio/workflows/validate` |

### List & create agents

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/agent-studio/agents?project_id=$PROJECT" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"name\": \"docs-agent\",
  \"description\": \"Drafts API and ops docs\"
}" "$BOSS_API/v1/agent-studio/agents" | jq .
```

Version publish/validate/test/run is explicit — no silent self-modification of production agents.

### Catalog smoke

```bash
curl -sS "${AUTH[@]}" -d "{\"project_id\": \"$PROJECT\", \"actor\": \"ceo\"}" \
  "$BOSS_API/v1/agent-studio/catalog-smoke" | jq .
```

---

## Skills

| Method | Path |
|--------|------|
| `GET` | `/v1/skills?project_id=` |
| `POST` | `/v1/skills/import` |
| `GET` | `/v1/skills/{id}/export` |
| `POST` | `/v1/skills/{id}/enable` |
| `POST` | `/v1/skills/{id}/disable` |
| `POST` | `/v1/skills/{id}/invoke` |
| `GET` | `/v1/skills/invocations?project_id=` |

### List & invoke

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/skills?project_id=$PROJECT" | jq .

# mock=true for dry evaluation in many environments
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"caller\": \"ceo\",
  \"actor\": \"ceo\",
  \"inputs\": {\"query\": \"list open decisions\"},
  \"mock\": true
}" "$BOSS_API/v1/skills/<skill_id>/invoke" | jq .
```

Production invokes should run under a job lease (`created_by_job_id`, `worker_id`, `lease_token`) when the skill has side effects. Enable/disable is project-scoped policy.

### Import / export

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"bundle\": { /* skill bundle document */ }
}" "$BOSS_API/v1/skills/import" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/skills/<skill_id>/export?project_id=$PROJECT&actor=ceo" | jq .
```

---

## Toolchain intake

Sanitized, model-agnostic intake (no raw executable import of untrusted sources).

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/toolchain/sources" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/toolchain/imports?project_id=$PROJECT" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"source_url\": \"https://example.com/tool-manifest.json\"
}" "$BOSS_API/v1/toolchain/imports/assess" | jq .
```

Restricted licensing, path escape, and policy bypass **fail closed**. Activation requires a native BOSS implementation.

---

## Knowledge context packing

Operator/agent surface over the same authority-preserving packer used by
`toolchain.context.pack` (BOSS-356). Accepts chat-style **messages**, free-form
**items**, or toolchain **chunks**; optional **tool_ids** inject native tool
catalog snippets as reference context. Credential-shaped secrets are redacted
before packing and **never** returned in `context`.

| Method | Path | Notes |
|--------|------|--------|
| `GET` | `/v1/knowledge/status?project_id=` | Whether packing is available + default token budget |
| `POST` | `/v1/knowledge/pack-context` | Requires `command_submit`; body includes `project_id`, `actor`, messages/items/chunks, optional `token_budget` / `tool_ids` |

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/knowledge/status?project_id=$PROJECT" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"token_budget\": 4096,
  \"messages\": [
    {\"role\": \"system\", \"content\": \"Prefer receipts and budget gates.\"},
    {\"role\": \"user\", \"content\": \"Pack the decision inbox for review.\"}
  ],
  \"items\": [
    {\"id\": \"proof-1\", \"text\": \"Smoke tests green.\", \"authority\": \"proof\"}
  ],
  \"tool_ids\": [\"list_tasks\"]
}" "$BOSS_API/v1/knowledge/pack-context" | jq .
```

Authoritative authorities (`policy`, `approval`, `contract`, `proof`) are never
silently truncated; if they exceed the budget the pack returns
`budget_status=blocked_authoritative_overflow`. Low-level packing remains at
`POST /v1/toolchain/context/pack`.

### Knowledge graph (RB-15)

Service/infra topology substrate for RCA and learning write-back. Mutations
require `providerops_manage`. Wraps `KnowledgeGraphService` (also used by
`/v1/reliability/graph`).

| Method | Path | Notes |
|--------|------|--------|
| `GET` | `/v1/knowledge/graph/nodes?project_id=&kind=&node_key=` | List / fetch nodes (optional filters) |
| `POST` | `/v1/knowledge/graph/nodes` | Upsert node (`kind`, `node_key`, optional `name`/`attrs`/`layer`) |
| `POST` | `/v1/knowledge/graph/edges` | Upsert edge (`src_id`, `dst_id`, `kind`) |
| `GET` | `/v1/knowledge/graph/subgraph?project_id=&kind=&node_key=&depth=` | BFS RCA context bundle |
| `GET` | `/v1/knowledge/graph/neighbors?project_id=&node_id=` *or* `kind`+`node_key` | Adjacent nodes |
| `POST` | `/v1/knowledge/graph/root-causes` | Record confirmed cause (`caused_by` edge) |
| `GET` | `/v1/knowledge/graph/known-causes?project_id=&service=` | Prior causes ranked by confidence |
| `POST` | `/v1/knowledge/graph/seed-from-infra` | Seed `resource` nodes from active `infra_resources` |

```bash
# Upsert a service + dependency, then read the subgraph
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\", \"actor\": \"ceo\",
  \"kind\": \"service\", \"node_key\": \"checkout\", \"name\": \"Checkout API\"
}" "$BOSS_API/v1/knowledge/graph/nodes" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/knowledge/graph/subgraph?project_id=$PROJECT&kind=service&node_key=checkout&depth=2" | jq .

# Learning write-back after a confirmed RCA
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\", \"actor\": \"ceo\",
  \"service\": \"checkout\",
  \"cause_kind\": \"deployment\", \"cause_key\": \"deploy-a5cb\",
  \"cause_name\": \"deploy a5cb\", \"confidence\": 0.9
}" "$BOSS_API/v1/knowledge/graph/root-causes" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/knowledge/graph/known-causes?project_id=$PROJECT&service=checkout" | jq .
```

---

## Feature catalog & capability manifest

Truthful per-project feature inventory and the capability list agents should trust
(instead of inventing tools that are not enabled).

| Method | Path | Notes |
|--------|------|--------|
| `GET` | `/v1/features?project_id=` | List catalog entries |
| `POST` | `/v1/features` | Upsert entry by label |
| `GET` | `/v1/features/{label}?project_id=` | Fetch one entry |
| `POST` | `/v1/features/{label}/documented` | Mark documented (idempotent via `event_id`) |
| `POST` | `/v1/features/seed` | Seed defaults for a project |
| `GET` | `/v1/capability-manifest?project_id=` | JSON capability items (`enabled` + reason) |
| `GET` | `/v1/capability-manifest/table` | Plain-text table (or JSON when `Accept: application/json`) |

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/capability-manifest?project_id=$PROJECT" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\", \"actor\": \"ceo\"
}" "$BOSS_API/v1/features/seed" | jq .
```

---

## Learning loop & intent routing

Operator-facing write-back for agent examples and deterministic intent classify/route.

| Method | Path | Notes |
|--------|------|--------|
| `POST` | `/v1/learning/examples` | Record a labeled example |
| `GET` | `/v1/learning/corpus?project_id=&agent=` | List corpus rows |
| `POST` | `/v1/learning/evaluate` | Score recent outcomes |
| `POST` | `/v1/learning/suggest-tuning` | Propose tuning diffs (no auto-apply) |
| `POST` | `/v1/intent/classify` | Classify free-text intent |
| `POST` | `/v1/intent/route` | Classify + route to handler/skill |

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\", \"actor\": \"ceo\",
  \"agent\": \"docs-agent\", \"kind\": \"success\",
  \"label\": \"api-docs-green\", \"outcome\": \"passed\"
}" "$BOSS_API/v1/learning/examples" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\", \"actor\": \"ceo\",
  \"text\": \"open a PR for the budget forecast fix\"
}" "$BOSS_API/v1/intent/route" | jq .
```

---

## Telemetry query & change correlation

Read-path connectors for logs/metrics/traces plus correlating incidents to recent changes.

| Method | Path | Notes |
|--------|------|--------|
| `GET` | `/v1/telemetry/connectors/catalog` | Available connector kinds |
| `POST` | `/v1/telemetry/query` | Query via a registered connector |
| `POST` | `/v1/change-correlation/correlate` | Correlate signal window → changes |

---

## Model gateway

Provider credentials stay in SecretRefs; agents never receive raw API keys.

Typical flow:

1. Register model credential (owner/admin)  
2. Create **lease** subordinate to a job (TTL, fence, concurrency)  
3. **Heartbeat** lease  
4. Record **invocations**  
5. **Settle** with actual tokens/cost → budget consume + receipt  
6. **Release** or let TTL reaper expire  

```bash
# Usage projection / telemetry (project-fenced)
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/model-gateway/usage?project_id=$PROJECT" | jq .
```

Concrete lease paths (see ROUTE_INDEX / OpenAPI for exact payloads):

- `POST /v1/model-gateway/leases`
- `POST /v1/model-gateway/leases/{lease_id}/heartbeat`
- `POST /v1/model-gateway/leases/{lease_id}/invocations`
- `POST /v1/model-gateway/leases/{lease_id}/invocations/{invocation_id}/settle`
- `POST /v1/model-gateway/leases/{lease_id}/release`
- `GET` `/v1/model-gateway/rate-limits?project_id=` — current rate-limit windows
- `POST` `/v1/model-gateway/rate-limits/report` — report provider throttle telemetry
- `GET` `/v1/pipeline-adapters/catalog` — registered pipeline adapters

Also: model route simulation via `/v1/model-route-decisions` and `/v1/model-route-decisions/simulate`.

**Always** reserve budget before spendy model work (see [CORE_CONTROL_PLANE.md](CORE_CONTROL_PLANE.md)).

---

## A2A (agent-to-agent)

| Method | Path |
|--------|------|
| `GET/POST` | `/v1/a2a/channels` |
| `POST` | `/v1/a2a/messages` |
| `POST` | `/v1/a2a/messages/{id}/dispatch` |
| `POST` | `/v1/a2a/messages/{id}/cancel` |
| `POST` | `/v1/a2a/messages/{id}/replay` |
| `POST` | `/v1/a2a/deliveries/{id}/acknowledge` |
| `POST` | `/v1/a2a/poll` |
| `POST` | `/v1/a2a/subscriptions` |
| `GET` | `/v1/a2a/messages`, `/audit`, `/health` |

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/a2a/channels?project_id=$PROJECT" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"name\": \"coding-panel\",
  \"metadata\": {}
}" "$BOSS_API/v1/a2a/channels" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"system\",
  \"channel_id\": \"<channel_id>\",
  \"payload\": {\"type\": \"task_update\", \"task_id\": \"t1\"}
}" "$BOSS_API/v1/a2a/messages" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"system\",
  \"channel_id\": \"<channel_id>\",
  \"limit\": 10
}" "$BOSS_API/v1/a2a/poll" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/a2a/health?project_id=$PROJECT" | jq .
```

Deliveries are receipt/audit oriented; cancel/replay are explicit operator actions.

---

## Shepherd

Task shepherd keeps work moving with readiness, overview, proposals, and escalation answers.

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/shepherd/readiness?project_id=$PROJECT" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/shepherd/overview?project_id=$PROJECT" | jq .

curl -sS "${AUTH[@]}" -d "{\"project_id\": \"$PROJECT\", \"actor\": \"ceo\"}" \
  "$BOSS_API/v1/shepherd/propose" | jq .

curl -sS "${AUTH[@]}" -d "{\"project_id\": \"$PROJECT\", \"actor\": \"ceo\"}" \
  "$BOSS_API/v1/shepherd/sweep" | jq .
```

---

## Agent modules & packs

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/agent-modules?project_id=$PROJECT" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/packs?project_id=$PROJECT" | jq .
```

Install/uninstall/rollback of modules is versioned and audited.

---

## Agent groups

Governed multi-participant runs (quorum ≠ proof). See [ASSURANCE_GOVERNANCE.md](ASSURANCE_GOVERNANCE.md) for create/run/finalize curl examples (`/v1/agent-groups/*`).

---

## Chat threads (console)

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"message\": \"What is blocked in delivery?\"
}" "$BOSS_API/v1/agent/chat" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/agent/threads?project_id=$PROJECT" | jq .
```

---

## Invariants

- LLM/provider keys never appear in agent-visible responses.  
- Model calls need lease + budget reservation + receipt settlement.  
- Skill side effects need job lease when not `mock`.  
- Agent definition changes are PR/version publish only — no autonomous self-rewrite of the runtime.  

# Work orchestration & work trackers

Plane-default work tracking plus Jira, Linear, GitHub, and Azure DevOps connectors. Orchestration covers work items, quotas, allocations, bot identity, and delivery presence.

| Prefix | Role |
|--------|------|
| `/v1/work-trackers` | Catalog, connect, default, test |
| `/v1/work-orchestration` | Items, quotas, allocations, presence, sync |

Product specs: `docs/product/WORK_TRACKER_PROVIDERS.md`, `docs/product/WORK_ORCHESTRATION_AND_DELIVERY_PRESENCE.md`.

```bash
export BOSS_API=http://127.0.0.1:8000
export BOSS_KEY=dev-key
export PROJECT=<project_id>
AUTH=(-H "Authorization: Bearer $BOSS_KEY" -H "Content-Type: application/json")
```

---

## Work trackers

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/v1/work-trackers/catalog` | Supported providers & auth shapes |
| `GET` | `/v1/work-trackers?project_id=` | Project connections |
| `POST` | `/v1/work-trackers/connect` | Connect / upsert tracker |
| `POST` | `/v1/work-trackers/set-default` | Choose project default |
| `POST` | `/v1/work-trackers/test` | Live connectivity probe |

Plane is the **default** system of record for BOSS itself; other trackers are first-class connectors with the same receipt/lease patterns for outbound writes.

### Catalog

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/work-trackers/catalog" | jq .
```

### List & connect

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/work-trackers?project_id=$PROJECT" | jq .

# Example: Plane connection (credential as SecretRef, never inline long-lived secrets in logs)
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"provider\": \"plane\",
  \"label\": \"goldeneye-plane\",
  \"credential_ref\": \"env:PLANE_API_KEY\",
  \"config\": {
    \"base_url\": \"https://plane.example.com/api/v1\",
    \"workspace_slug\": \"goldeneye\",
    \"project_id\": \"cc97d5cc-e8c6-4893-ac14-7f646c37f3d7\"
  }
}" "$BOSS_API/v1/work-trackers/connect" | jq .
```

Other providers use the same shape with provider-specific `config` (Jira cloud/site + project key, Linear team, GitHub owner/repo project, Azure DevOps org/project).

### Set default & test

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"tracker_connection_id\": \"<id>\"
}" "$BOSS_API/v1/work-trackers/set-default" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"tracker_connection_id\": \"<id>\"
}" "$BOSS_API/v1/work-trackers/test" | jq .
```

Test failures fail closed (no synthetic “ok”).

---

## Work orchestration

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/v1/work-orchestration/overview?project_id=` | Dashboard truth |
| `POST` | `/v1/work-orchestration/items` | Create/upsert work item |
| `POST` | `/v1/work-orchestration/quotas` | Workload quotas |
| `POST` | `/v1/work-orchestration/allocations/plan` | Plan resource allocation |
| `POST` | `/v1/work-orchestration/tracker-bindings` | Bind BOSS objects ↔ tracker issues |
| `POST` | `/v1/work-orchestration/tracker-sync/execute` | Leased sync job |
| `POST` | `/v1/work-orchestration/bot-identities` | Bot identity for presence |
| `POST` | `/v1/work-orchestration/status-channels` | Status channel routing |
| `POST` | `/v1/work-orchestration/presence-events` | Delivery/chat presence |

### Overview

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/work-orchestration/overview?project_id=$PROJECT" | jq .
```

### Work item

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"title\": \"Wire Plane intake to jobs\",
  \"priority_score\": 80,
  \"workload_class\": \"delivery\",
  \"metadata\": {\"source\": \"plane\"}
}" "$BOSS_API/v1/work-orchestration/items" | jq .
```

Jobs may reference `work_item_id`, `tracker_binding_id`, and `allocation_id` so priority/quota follow the worker claim path.

### Quotas & allocation plan

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"workload_class\": \"delivery\",
  \"max_concurrent_jobs\": 4,
  \"max_resource_units\": 20
}" "$BOSS_API/v1/work-orchestration/quotas" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"horizon_minutes\": 60
}" "$BOSS_API/v1/work-orchestration/allocations/plan" | jq .
```

### Tracker binding & sync

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"tracker_connection_id\": \"<id>\",
  \"external_issue_id\": \"BOSS-340\",
  \"task_id\": \"<task_id>\"
}" "$BOSS_API/v1/work-orchestration/tracker-bindings" | jq .

# Execute sync as leased, receipt-producing work
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"system\",
  \"tracker_connection_id\": \"<id>\",
  \"direction\": \"pull\"
}" "$BOSS_API/v1/work-orchestration/tracker-sync/execute" | jq .
```

Outbound tracker writes must produce receipts with dedupe keys; Plane issue creation is a leased job in the full dogfood path.

### Bot identity & presence

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"display_name\": \"BOSS Delivery Bot\",
  \"channel\": \"mattermost\"
}" "$BOSS_API/v1/work-orchestration/bot-identities" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"system\",
  \"channel\": \"mattermost\",
  \"event_type\": \"status\",
  \"payload\": {\"text\": \"Deploy pipeline green\"}
}" "$BOSS_API/v1/work-orchestration/presence-events" | jq .
```

---

## Relationship to core jobs

1. Tracker intake → work item + optional task  
2. Quota/allocation plan ranks runnable work  
3. `POST /v1/jobs` (or intake) carries orchestration foreign keys  
4. Worker lease executes under budget  
5. Tracker sync pushes status with receipts  

See also: [CORE_CONTROL_PLANE.md](CORE_CONTROL_PLANE.md), Plane integration `docs/infra/BOSS_PLANE_INTEGRATION_SPEC.md`.

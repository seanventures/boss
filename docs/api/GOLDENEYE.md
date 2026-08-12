# Goldeneye PaaS API (`/v1/goldeneye/*`)

Operator-facing HTTP surface for the **Goldeneye bare-metal PaaS** control plane:
register nodes, plan package installs, deploy/list apps, heartbeats, and fleet status.

- **Product:** [`docs/product/GOLDENEYE_PAAS.md`](../product/GOLDENEYE_PAAS.md)
- **Kit:** `packages/goldeneye/`
- **Runtime:** `engine/boss_engine/services/goldeneye_paas.py`
- **OpenAPI:** `api/openapi.yaml` (tag **Goldeneye PaaS**)

## Auth and base URL

```bash
export BOSS_API_URL="${BOSS_API_URL:-http://127.0.0.1:8000}"
export BOSS_API_KEY="${BOSS_API_KEY:?set BOSS_API_KEY}"
export PROJECT_ID="${PROJECT_ID:-boss-dogfood}"

# Common header (Bearer API key)
auth() { echo "Authorization: Bearer ${BOSS_API_KEY}"; }
```

Mutating routes require a project actor with **`providerops_manage`** (body field `actor`, default `ceo` where accepted).

Hard rules still apply: real host writes stay policy-gated; external effects are receipted with a unique `dedupe_key`.

---

## Package catalog

### `GET /v1/goldeneye/packages`

Built-in layered package manager catalog (host → edge → optional services).

```bash
curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/goldeneye/packages" | jq .
```

No `project_id` required — static kit catalog.

---

## Fleet status

### `GET /v1/goldeneye/status`

Fleet overview: nodes, apps, package installs, kit catalog.

```bash
curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/goldeneye/status?project_id=${PROJECT_ID}" | jq .
```

| Query | Required | Description |
|---|---|---|
| `project_id` | yes | Project scope |

---

## Nodes

### `GET /v1/goldeneye/nodes`

List registered bare-metal (or VM) Goldeneye nodes for a project.

```bash
curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/goldeneye/nodes?project_id=${PROJECT_ID}" | jq .
```

### `POST /v1/goldeneye/nodes`

Register or update a node. Returns **201** on success.

```bash
curl -sS -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$BOSS_API_URL/v1/goldeneye/nodes" \
  -d "{
    \"project_id\": \"${PROJECT_ID}\",
    \"actor\": \"ceo\",
    \"name\": \"edge-1\",
    \"display_name\": \"Edge node 1\",
    \"roles\": [\"edge\", \"app\"],
    \"public_ip\": \"203.0.113.10\",
    \"tailnet_ip\": \"100.64.0.10\",
    \"ssh_host\": \"edge-1.tailnet\",
    \"ssh_user\": \"root\",
    \"ssh_port\": 22,
    \"kit_version\": \"1.0.0\",
    \"labels\": {\"site\": \"home-lab\"},
    \"site_config\": {},
    \"metadata\": {}
  }" | jq .
```

| Body field | Required | Notes |
|---|---|---|
| `project_id` | yes | Project scope |
| `name` | yes | Stable node name |
| `actor` | no | Default `ceo`; needs `providerops_manage` |
| `roles` | no | e.g. `edge`, `control`, `data`, `app` |
| `public_ip` / `tailnet_ip` | no | Addressing hints |
| `ssh_host` / `ssh_user` / `ssh_port` | no | SSH target (user default `root`, port `22`) |
| `kit_version` | no | Installed kit version |
| `display_name`, `labels`, `site_config`, `metadata` | no | Operator metadata |

### `POST /v1/goldeneye/nodes/{node_id}/heartbeat`

Kit/agent liveness report for a node.

```bash
NODE_ID="<node-uuid>"

curl -sS -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$BOSS_API_URL/v1/goldeneye/nodes/${NODE_ID}/heartbeat" \
  -d "{
    \"project_id\": \"${PROJECT_ID}\",
    \"status\": \"ready\",
    \"kit_version\": \"1.0.0\",
    \"metadata\": {\"load\": 0.2}
  }" | jq .
```

| Body field | Required | Notes |
|---|---|---|
| `project_id` | yes | Project scope |
| `status` | no | e.g. `ready`, `degraded`, `offline` |
| `kit_version` | no | Reported kit version |
| `metadata` | no | Free-form object |

---

## Package installs

### `GET /v1/goldeneye/package-installs`

List package install records (optionally filtered by node).

```bash
curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/goldeneye/package-installs?project_id=${PROJECT_ID}" | jq .

# Optional node filter
curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/goldeneye/package-installs?project_id=${PROJECT_ID}&node_id=${NODE_ID}" | jq .
```

### `POST /v1/goldeneye/packages/install`

Plan/simulate (or gate real) package install on a node. Returns **201**.

```bash
curl -sS -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$BOSS_API_URL/v1/goldeneye/packages/install" \
  -d "{
    \"project_id\": \"${PROJECT_ID}\",
    \"actor\": \"ceo\",
    \"package_id\": \"host.baseline\",
    \"node_id\": \"${NODE_ID}\",
    \"force\": false,
    \"metadata\": {}
  }" | jq .
```

| Body field | Required | Notes |
|---|---|---|
| `project_id` | yes | Project scope |
| `package_id` | yes | Id from `GET /v1/goldeneye/packages` |
| `node_id` | preferred | Target node id |
| `node_name` | alt | Target by name if id unknown |
| `actor` | no | Default `ceo` |
| `force` | no | Bypass bootstrap order (operator risk) |
| `metadata` | no | Free-form object |

---

## Apps

### `GET /v1/goldeneye/apps`

List apps registered/deployed for the project.

```bash
curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/goldeneye/apps?project_id=${PROJECT_ID}" | jq .
```

### `POST /v1/goldeneye/apps/deploy`

Deploy (or simulate) a containerized app onto a node. Returns **201**.

```bash
curl -sS -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$BOSS_API_URL/v1/goldeneye/apps/deploy" \
  -d "{
    \"project_id\": \"${PROJECT_ID}\",
    \"actor\": \"ceo\",
    \"name\": \"hello-web\",
    \"node_id\": \"${NODE_ID}\",
    \"image_ref\": \"ghcr.io/example/hello:1.0.0\",
    \"domain\": \"hello.example.com\",
    \"metadata\": {}
  }" | jq .
```

| Body field | Required | Notes |
|---|---|---|
| `project_id` | yes | Project scope |
| `name` | yes | Stable app name |
| `node_id` / `node_name` | one of | Target node |
| `image_ref` or `image` | no | Container image |
| `compose_ref` or `compose` | no | Compose bundle reference |
| `domain` | no | Public or tailnet hostname |
| `actor` | no | Default `ceo` |
| `metadata` | no | Free-form object |

### `POST /v1/goldeneye/apps/{name}/rollback`

Rollback an app to the previous revision.

```bash
APP_NAME="hello-web"

curl -sS -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$BOSS_API_URL/v1/goldeneye/apps/${APP_NAME}/rollback" \
  -d "{
    \"project_id\": \"${PROJECT_ID}\",
    \"actor\": \"ceo\"
  }" | jq .
```

| Body field | Required | Notes |
|---|---|---|
| `project_id` | yes | Project scope |
| `actor` | no | Default `ceo` |

---

## Typical operator flow

```bash
# 1. Discover packages
curl -sS -H "$(auth)" "$BOSS_API_URL/v1/goldeneye/packages" | jq .

# 2. Register a node
curl -sS -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$BOSS_API_URL/v1/goldeneye/nodes" \
  -d "{\"project_id\":\"${PROJECT_ID}\",\"actor\":\"ceo\",\"name\":\"edge-1\",\"roles\":[\"edge\"]}" | jq .

# 3. Heartbeat from kit
# 4. Install layered packages (host → runtime → edge → …)
# 5. Deploy apps
# 6. Monitor fleet
curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/goldeneye/status?project_id=${PROJECT_ID}" | jq .
```

## Related surfaces

| Surface | Path |
|---|---|
| Multi-cloud catalog | [`docs/api/MULTI_CLOUD.md`](./MULTI_CLOUD.md) |
| Elastic fabric | `/v1/elastic/*` (OpenAPI tag **Elastic fabric**) |
| Provider usage | `/v1/provider-usage/*` |
| API catalog | `GET /v1/meta/api-catalog` / `GET /v1/meta/api-catalog/goldeneye` |

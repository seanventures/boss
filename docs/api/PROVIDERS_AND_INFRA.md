# Providers, infra, elastic fabric & FinOps

Resource Plane Kernel: provider accounts, ChangeSets, receipts, multi-cloud catalog, elastic workers, Goldeneye PaaS, and cost controls.

| Prefix | Role |
|--------|------|
| `/v1/providers` | Provider account lifecycle |
| `/v1/provider-health` | Health poll |
| `/v1/provider-definitions` | Catalog of known providers |
| `/v1/provider-usage` | Usage ledger / sync |
| `/v1/resources` | Inventory sync |
| `/v1/resource-topology` | Durable topology graph (edges between inventory resources) |
| `/v1/resource-receipts` | Provider effect receipts |
| `/v1/changesets` | Planned provider writes |
| `/v1/elastic/*` | Capacity fabric |
| `/v1/multi-cloud/*` | Multi-cloud catalog & skills |
| `/v1/finops/*` | Commitments / initiative budget |
| `/v1/worker-pools`, `/v1/autoscaler` | Pool topology |
| `/v1/goldeneye/*` | Bare-metal PaaS |
| `/v1/costs`, `/v1/cost-anomalies` | Spend views |

Auth and conventions: [README.md](README.md). Permission for most writes: `providerops_manage`.

```bash
export BOSS_API=http://127.0.0.1:8000
export BOSS_KEY=dev-key
export PROJECT=<project_id>
AUTH=(-H "Authorization: Bearer $BOSS_KEY" -H "Content-Type: application/json")
```

---

## Provider accounts

| Method | Path |
|--------|------|
| `GET` | `/v1/providers?project_id=` |
| `POST` | `/v1/providers` |
| `GET` | `/v1/providers/{id}` |
| `PATCH` | `/v1/providers/{id}` |
| `POST` | `/v1/providers/{id}/test` |
| `POST` | `/v1/providers/{id}/enable` |
| `POST` | `/v1/providers/{id}/disable` |
| `POST` | `/v1/providers/{id}/rotate` |
| `POST` | `/v1/providers/{id}/revoke` |

Credentials are **SecretRefs** only — never returned on read APIs.

### Register (read-only first)

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"provider\": \"digitalocean\",
  \"account_label\": \"do-main\",
  \"auth_method\": \"token\",
  \"credential_ref\": \"env:BOSS_DIGITALOCEAN_TOKEN\",
  \"read_only\": true,
  \"deploy_capable\": false,
  \"billing_readable\": true,
  \"destructive_actions_disabled\": true
}" "$BOSS_API/v1/providers" | jq .
```

### Test connection

```bash
curl -sS -X POST -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/providers/<provider_account_id>/test?project_id=$PROJECT&actor=ceo" | jq .
```

Missing credentials **fail closed** with visible health/receipt/decision outcomes — no mock success.

### Inventory & health

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/resources?project_id=$PROJECT" | jq .

curl -sS "${AUTH[@]}" -d "{\"project_id\": \"$PROJECT\", \"actor\": \"ceo\"}" \
  "$BOSS_API/v1/resources/sync" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/provider-health?project_id=$PROJECT" | jq .
```

### Resource topology (BOSS-240)

Durable directed edges between inventory resources (`external_resources`). Inventory
and drift remain separate; topology is the minimal graph store for ProviderOps
(e.g. droplet → VPC, volume → droplet). Mutating writes emit a `Receipt`.

| Method | Path |
|--------|------|
| `GET` | `/v1/resource-topology?project_id=` |
| `POST` | `/v1/resource-topology/edges` |
| `DELETE` | `/v1/resource-topology/edges/{edge_id}?project_id=&actor=` |
| `POST` | `/v1/resource-topology/infer` |

```bash
# Graph summary (nodes + edges)
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/resource-topology?project_id=$PROJECT" | jq .

# Manual edge (requires providerops_manage)
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"from_resource_id\": \"<external_resource_id_droplet>\",
  \"to_resource_id\": \"<external_resource_id_vpc>\",
  \"relation_type\": \"in_vpc\",
  \"provider\": \"digitalocean\",
  \"metadata\": {\"source\": \"manual\"}
}" "$BOSS_API/v1/resource-topology/edges" | jq .

# Infer edges from inventory tags/metadata (vpc_uuid, tags like vpc:<id>)
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"provider\": \"digitalocean\"
}" "$BOSS_API/v1/resource-topology/infer" | jq .

# Delete edge
curl -sS -X DELETE -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/resource-topology/edges/<edge_id>?project_id=$PROJECT&actor=ceo" | jq .
```

Suggested `relation_type` values: `in_vpc`, `attached_to`, `depends_on`,
`member_of`, `routes_to`, `hosts`, `uses`, `linked_to` (open-ended TEXT).

---

## ChangeSets (provider writes)

All provider **writes** must be ChangeSets. Destructive/production DNS/deploy needs approval + rollback plan.

| Method | Path |
|--------|------|
| `GET` | `/v1/changesets?project_id=` |
| `POST` | `/v1/changesets` |
| `POST` | `/v1/changesets/{id}/approve` |
| `POST` | `/v1/changesets/{id}/apply` |
| `GET/POST` | `/v1/changesets/{id}/terraform-export` |

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"provider_account_id\": \"<provider_account_id>\",
  \"skill\": \"digitalocean.droplet.create\",
  \"payload\": {
    \"name\": \"boss-sandbox-1\",
    \"region\": \"nyc3\",
    \"size\": \"s-1vcpu-1gb\"
  },
  \"rollback_plan\": {\"skill\": \"digitalocean.droplet.destroy\"}
}" "$BOSS_API/v1/changesets" | jq .

# After decision/approval gates:
curl -sS "${AUTH[@]}" -d "{\"project_id\": \"$PROJECT\", \"actor\": \"ceo\"}" \
  "$BOSS_API/v1/changesets/<changeset_id>/approve" | jq .

curl -sS "${AUTH[@]}" -d "{\"project_id\": \"$PROJECT\", \"actor\": \"ceo\"}" \
  "$BOSS_API/v1/changesets/<changeset_id>/apply" | jq .
```

Real provision also requires env gates such as `BOSS_INFRA_REAL_PROVISION_ALLOWED=true`. Successful actuation emits **ResourceReceipts** and often **CostEvents**.

For a provider-backed deployment, `POST /v1/delivery/deploy` returns a planned
`deployment_run_id` and `changeset_id`. Send `expected_cost_usd_micros` and an active
`reservation_id` when spend is expected. Approve and apply that ChangeSet using the
endpoints above; its approval, reservation, rollback, and receipt checks govern the
provider effect. Direct SSH/container deployment currently returns HTTP 409 with a
recorded blocked run and decision in every edition. Retrying through
`/v1/delivery/deployments/{id}/execute` cannot bypass this gate.
`POST /v1/delivery/first-deploy` and merge-triggered provider deployment also
return a planned ChangeSet without starting a connector. A direct-host first
deployment returns HTTP 409 after recording the same blocked run and decision.

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/resource-receipts?project_id=$PROJECT" | jq .
```

---

## Multi-cloud catalog

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/multi-cloud/catalog" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/multi-cloud/providers/aws" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/multi-cloud/skills" | jq .
```

Describes supported clouds (AWS, Azure, DigitalOcean, OVH, etc.), auth models, and skill inventory for adapters.

---

## Elastic fabric

Worker/infra elasticity with per-provider usage ledger.

| Method | Path |
|--------|------|
| `GET` | `/v1/elastic/status?project_id=` |
| `POST` | `/v1/elastic/capacity` |
| `POST` | `/v1/elastic/pools/{worker_pool_id}/reconcile` |
| `POST` | `/v1/elastic/instances/{instance_id}/heartbeat` |
| `POST` | `/v1/elastic/instances/{instance_id}/terminate` |

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/elastic/status?project_id=$PROJECT" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"desired_workers\": 2,
  \"pool_key\": \"default\",
  \"reason\": \"queue depth high\"
}" "$BOSS_API/v1/elastic/capacity" | jq .
```

Scale-up is budget- and policy-gated; failures surface as decisions/events.

### Worker pools & autoscaler

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/worker-pools?project_id=$PROJECT" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/autoscaler/plans?project_id=$PROJECT" | jq .
```

---

## Provider usage & FinOps

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/provider-usage?project_id=$PROJECT" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/provider-usage/summary?project_id=$PROJECT" | jq .

curl -sS "${AUTH[@]}" -d "{\"project_id\": \"$PROJECT\", \"actor\": \"ceo\"}" \
  "$BOSS_API/v1/provider-usage/sync" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/costs?project_id=$PROJECT" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/cost-anomalies?project_id=$PROJECT" | jq .
```

### FinOps commitments

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\"
}" "$BOSS_API/v1/finops/reconcile-commitments" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"initiative_key\": \"q3-launch\",
  \"amount_usd_micros\": 50000000,
  \"reason\": \"Launch infra envelope\"
}" "$BOSS_API/v1/finops/initiative-budget" | jq .
```

Threshold-crossing spend can switch a provider account to **read-only** and open a decision card.

---

## Goldeneye bare-metal PaaS

Self-hosted node fleet, package install, and app deploy/rollback.

| Method | Path |
|--------|------|
| `GET` | `/v1/goldeneye/status?project_id=` |
| `GET` | `/v1/goldeneye/nodes?project_id=` |
| `POST` | `/v1/goldeneye/nodes` |
| `POST` | `/v1/goldeneye/nodes/{node_id}/heartbeat` |
| `GET` | `/v1/goldeneye/packages` |
| `POST` | `/v1/goldeneye/packages/install` |
| `GET` | `/v1/goldeneye/apps?project_id=` |
| `POST` | `/v1/goldeneye/apps/deploy` |
| `POST` | `/v1/goldeneye/apps/{name}/rollback` |

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/goldeneye/status?project_id=$PROJECT" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"hostname\": \"node-1.local\",
  \"labels\": {\"role\": \"compute\"}
}" "$BOSS_API/v1/goldeneye/nodes" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"name\": \"api\",
  \"package\": \"boss-api\",
  \"version\": \"0.11.0\"
}" "$BOSS_API/v1/goldeneye/apps/deploy" | jq .
```

Ops runbook: `docs/operations/GOLDENEYE_DEPLOYMENT.md`. Product note: `docs/product/GOLDENEYE_PAAS.md`.

---

## Hard rules (infra)

1. No provider write without a ChangeSet.  
2. No successful external effect without ResourceReceipt + dedupe.  
3. No spendy provision without BudgetReservation.  
4. Destructive / production DNS/deploy needs explicit approval + rollback.  
5. Adapters are plugins; the Resource Plane Kernel is core BOSS.  

Related: `docs/product/MULTI_CLOUD_INFRA_MODELS.md`, `docs/product/ELASTIC_FABRIC.md`.

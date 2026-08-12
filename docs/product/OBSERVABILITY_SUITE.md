# Observability Suite

Operator-facing unification of **telemetry connectors**, the **BOSS-managed observability stack**, and **knowledge-graph RCA convenience**.

## Why it exists

BOSS already had three separate halves:

| Half | Module | What it does |
|------|--------|----------------|
| Read connectors | `telemetry_connectors` | Prometheus, Loki, Grafana, Datadog, … → canonical metrics/logs/alerts |
| Deployable stack | `observability_stack` | OTel Collector → Prometheus → Grafana on a host |
| Knowledge graph | `knowledge_graph` | Topology + `caused_by` learning for AI SRE |

The suite answers one operator question: **what should we run, what is connected, and can we investigate?**

## Market OSS catalog

Static, offline-safe catalog (no live star scrapes):

| Component | Role | In BOSS stack deploy? |
|-----------|------|------------------------|
| Prometheus | Metrics TSDB | Yes |
| Grafana | Visualization | Yes |
| OpenTelemetry Collector | Pipeline / scrape hub | Yes |
| Loki | Logs | Optional add-on |
| Tempo | Traces | Optional add-on |
| Jaeger | Traces (alt UI) | Optional add-on |
| Alertmanager | Alert routing | Optional add-on |

Stars notes are order-of-magnitude annotations for operator context only.

## API

Prefix: **`/v1/observability-suite`**

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/catalog` | OSS components + workload profiles + connector kinds |
| `GET` | `/status?project_id=` | Registered sources, stack deploy signal, scrape apps, gaps |
| `POST` | `/recommend` | `{ "workload_profile": "web" }` → ordered components + next steps |
| `POST` | `/health-check` | Dry-run config readiness (default) or live probes (`dry_run: false`) |
| `GET` | `/graph-summary` | KG health + optional RCA pack (`kind`, `node_key`, `depth`) |
| `POST` | `/graph-seed` | Seed from `infra_resources` + health summary |

Knowledge graph (no clash with existing routes):

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/v1/knowledge/graph/health?project_id=` | Node/edge counts, readiness |

Existing KG routes remain the system of record for upserts, subgraph, known-causes, seed-from-infra.

### Workload profiles

`minimal`, `web`, `api`, `batch`, `microservices`, `k8s`, `enterprise`, `traces_focus`

Aliases: `kubernetes`→`k8s`, `production`→`enterprise`, `jobs`→`batch`, etc.

### Health-check contract

* **Default `dry_run: true`** — validates connector config via `ensure_connector_ready`; **no live HTTP**.
* **`dry_run: false`** — probes kind-specific health paths through `telemetry_connectors._http_json` (tests stub this). Failures are explicit `unreachable` / `misconfigured` — never silent fake healthy data.

### Stack status signal

`stack.deployed` is true when a `telemetry_sources` row exists for `kind=prometheus` and `name=boss-prometheus` (same convention as `GET /v1/observability/stack`). Optional `host_target_id` can probe live containers via `stack_status`.

## UI (Mission Control)

* `apps/web/lib/observability-suite.ts` + `observability-suite-ui.tsx` — catalog, status, recommend, dry-run health-check.
* `apps/web/lib/knowledge-graph.ts` + `knowledge-graph-ui.tsx` — health, seed, RCA pack, recent nodes.

Placed next to Monitoring / Reliability on the console home.

## Related routes

* `GET /v1/telemetry/connectors/catalog` — connector construction catalog
* `GET|POST /v1/observability/stack` — deploy / inspect BOSS stack
* `/v1/knowledge/graph/*` — full graph CRUD + RCA primitives
* `/v1/reliability/*` — incidents, sources, diagnose / remediate loop

## Tests

`apps/api/tests/test_observability_suite_api.py` — catalog, recommend, status, health-check (stubbed wire), graph health, RCA pack, graph-seed. **No live external calls.**

## Implementation

* Service: `engine/boss_engine/services/observability_suite.py`
* Router: `apps/api/app/routers/observability_suite.py` (mounted via `mount_platform_routers`)

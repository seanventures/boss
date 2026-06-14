# BOSS Architecture Review (Autonomous)

**Date**: 2026-06-05
**Reviewer**: SOFTWARE ARCHITECT AGENT
**Scope**: Module boundaries, data flow, API contracts, state mgmt, extensibility, maintainability, perf, migrations, deployment.

## Alignment to TECH SPEC v2.0
- Planes clearly separated: Control (api + db), Execution (worker lease), ProviderOps (resource_plane service + adapters), MEB (entitlements), etc.
- Resource Plane Kernel in core (resource_plane.py); adapters (digitalocean, cloudflare, supabase, model) are plugins behind interface (test/sync/quote/apply -> receipts).
- API-first: openapi is source, api-client generated/consumed by web (no biz logic in web).
- DB: schema.sql is canonical; recent additions (kpis table, prior budget/cost etc) are additive migrations (no alter in place risky).
- State machines: jobs, changesets, decisions, budget res visible in services; tests cover transitions.
- No direct external bypass: all via services + policy + budget + receipt.
- Extensibility: specpack YAML for roles/modules/packs/skills/policies; installable; runtime resolves.
- Maintainability: explicit, typed at boundaries (schemas.py, specpack objects), boring code per AGENTS.

## Findings / Risks
- Recent canonical.py changes add KPI service - good, follows existing pattern for tasks etc. Watch for duplication if more objects added.
- Budget service now has consume/forecast - centralizes governor logic.
- Potential: long worker jobs or many concurrent leases -> monitor TTL/heartbeat/queue pressure (ops digest helps).
- Migrations: db/schema.sql is "starter"; for prod need proper migration tool (alembic?); current make seed applies full. Document assumption.
- Perf: no obvious N+1 in scans; API uses project scoping. P2 pagination (limit/offset) added to high-volume lists (events, resources, costs) per review rec; defaults preserve backward (50/100); responses now carry limit/offset metadata; validation + tests cover.
- Deployment: docker-compose for local, k8s/helm for prod; validators exist. Good.
- Data flow: commands -> intake -> tasks/plans/jobs -> decisions -> worker lease -> receipts/events. Clear.
- Ext: good via specpack + skills registry.

**Recommendations**:
- Add explicit pagination to list endpoints for scale (events, resources). [DONE in this slice: limit/offset on events+resources+costs; see gap, arch update]
- Consider formal migration history vs full schema apply for future.
- Architecture sound for Phase 0/1; elastic planes ready for Phase 2 when externals available.
- No P0 arch violations.

**Status**: PASS (P2 pagination addressed in arch(pagination) slice + tests; remaining P2 note: migrations).
**Files**: apps/api/app/services/*, main.py, db/schema.sql, packages/specpack, deploy/* , api/openapi.yaml, packages/api-client/src/index.ts, apps/api/tests/test_api.py, apps/web/tests/*
**Pagination changes (P2)**: Updated schemas (EventFeed/ExternalResourceList/CostEventListResponse +limit/offset), main handlers (/v1/events /v1/resources /v1/costs +offset=0 default +400 on neg), resource_plane list_* (SQL OFFSET), openapi params, client TS (types+calls+listEvents/Costs/Resources), pytest for contract/defaults/offset/invalids, web test mocks+url expects. Backward: defaults unchanged. All per AGENTS (typed boundaries, tests, docs update).


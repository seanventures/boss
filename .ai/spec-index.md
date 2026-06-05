# BOSS .ai Spec Index

Last updated: 2026-06-05T11:15:00Z (autonomous discovery by Grok team)

## Discovered Product Identity
- **Product Name**: BOSS - Business Operating System Sentinel
- **Tagline / Thesis**: A financially controlled autonomous company operating system. BOSS turns CEO commands into auditable tasks, plans, decisions, budget-gated jobs, validated PRs, receipts, and event history. It is the durable control plane for AI labor with production semantics (leases, receipts, idempotency, policy gates, budget reservations, approvals, auditability).
- **Repo**: https://github.com/seanheiney/boss (default branch: main)
- **Current Branch (start)**: codex/spec-closure-audit (up to date at discovery, but with local uncommitted changes from prior gap-closure work)
- **Release Target**: v0.4.1 (per IMPLEMENTATION_STATUS)
- **Repo Layout Highlights**:
  - Control plane: apps/api (FastAPI + Postgres), api/openapi.yaml
  - Web console: apps/web (Next.js TSX Mission Control / Metatron)
  - Worker: apps/worker (Python lease-based runner)
  - Shared: packages/api-client (TS), packages/specpack (YAML specs, packs, roles, modules, policies, workflows, skills)
  - DB: db/schema.sql (canonical)
  - Docs: docs/product/* (PRDs, specs, inventory, traceability, gap backlog, roadmap, implementation status), docs/user-stories.md, docs/operations/INFRA_RUNBOOK.md
  - Deploy: deploy/ (k8s, helm), infra/docker-compose.yml, Makefile
  - Reference: reference/boss_build_spec_pack, BOSS PRD PDF/DOCX archives

## Discovered Source-of-Truth Documents (with confidence)
1. **docs/product/PRD-v2.0.md** (Governing product-intent spec, June 3 2026, supersedes v1.1) - HIGH confidence. Explicit thesis, constitution (12 rules), architecture (planes: Control, Execution, Comms, ProviderOps/Resource Plane, Model Entitlement Broker, Skills, Account Connect, Enterprise), canonical domain objects, user journeys (CEO command, decision discipline, etc.), non-functional, commercial model (Community/Cloud/Enterprise).
2. **docs/product/TECHNICAL_SPEC-v2.0.md** (Technical companion, June 3 2026) - HIGH. Architecture details, Resource Plane Kernel invariants, provider adapter interface, ChangeSet state machine, budget protocol, worker lease rules, no-silent-drops, OpenClaw rejection.
3. **docs/product/REQUIREMENT_INVENTORY.md** (June 5 2026) - HIGH. Current traceability table with IDs like MET, DEC, CAN, BUD, RUN, AUD, RPL etc. and status (mostly Implemented post recent audit).
4. **docs/product/TRACEABILITY.md** (updated June 4/5) - HIGH. Detailed req labels (MET-001 etc.) mapped to files/tests/status (mostly Implemented).
5. **docs/product/GAP_BACKLOG.md** (June 5 2026) - HIGH. P0/P1 gaps with recent closures (e.g. KPI canonical object GAP-2026-06-05-009, budget consume/forecast 006, bootstrap security 007, audit denial logging 001, route parity 008). Remaining P0s are externally blocked (Account Connect hosted, Enterprise IdP/SCIM, live ProviderOps infra rollout).
6. **docs/product/IMPLEMENTATION_STATUS.md** (June 4 2026) - HIGH. Detailed "what is implemented" list covering tenant, control objects, API surfaces, worker, budget governor, software factory, comms, ProviderOps (local), Model Entitlement, roles/packs/modules, etc.
7. **docs/product/ROADMAP-v2.0.md** - MED-HIGH. Phases 0 (kernel/dogfood - largely done), 1 (BOSS builds BOSS), 2 (ProviderOps elastic), 3 (commercial), 4 (enterprise).
8. **README.md** (current) - HIGH. Product description, invariants, real-only posture, API-first rule, setup (make dev/seed/test/smoke), current endpoints, auth model.
9. **AGENTS.md** - HIGH. Mission, read-first order (PRD-v2, TECH-v2, ...), build order (schema, API, worker lease, budget, event UI, acceptance), hard rules (receipts, reservations, leases, no silent, adapters), conventions, review checklist.
10. **docs/user-stories.md** - MED. As-a / Given-When-Then stories, many acceptance criteria covering states (loading/empty/error/success), permissions, edges. Updated in current changes.
11. **packages/specpack/** (esp. workflows/ceo_command_to_pr.yaml, policies/default_safety.yaml, resource_plane_policy.yaml, packs/core_v1.yaml, roles/*, modules/*, skills/*, objects/*.yaml) - HIGH for contracts.
12. **api/openapi.yaml**, **db/schema.sql**, **packages/api-client/src/index.ts** - HIGH for runtime contract.
13. **.env.example**, Makefile, scripts/*, deploy/*, infra/* - MED for ops.
14. Archived: BOSS_Full_Featured_Product_PRD_v1_0.pdf/docx, reference/boss_build_spec_pack, codex_webrtc... (lower priority, historical).

**Conflicts/Ambiguity Resolution**:
- v2.0 supersedes v1.x explicitly.
- Current docs (REQUIREMENT_INVENTORY, TRACEABILITY, GAP_BACKLOG, IMPLEMENTATION_STATUS) reflect post-audit "Implemented" state for local dogfood + real sandbox paths; live external (hosted Account Connect, real provider creds for writes, IdP) are Blocked as expected per PRD "real-only posture" and fail-closed rules.
- No formal single PRD file beyond md; PRD-v2.0.md is the explicit governing source.
- GitHub issues: 0 open (at discovery).
- Open PRs: 2 cursor bot PRs on main (Account Connect/SCIM fixes). No active codex/grok PRs listed; current branch codex/spec-closure-audit pushed but no PR visible (may need creation).
- Uncommitted local changes on start: API schema/main/services, web actions/page/tests, db/schema, docs updates, untracked kpis.ts/kpis.test/kpi.yaml (these appear to be the KPI + budget + security gap closures referenced in current GAP_BACKLOG).

**Final Chosen Source-of-Truth Order**:
1. PRD-v2.0.md + TECHNICAL_SPEC-v2.0.md (product intent + tech rules)
2. AGENTS.md + README.md (repo rules + posture)
3. Current runtime docs (REQUIREMENT_INVENTORY.md, TRACEABILITY.md, GAP_BACKLOG.md, IMPLEMENTATION_STATUS.md) for status
4. packages/specpack contracts + openapi + schema + tests for implementation truth
5. user-stories.md for UX/AC

**Inferred Gaps if no PRD**: N/A - formal PRD/spec sources exist and are rich. The "no formal PRD" fallback not triggered.

## Additional Discovery Notes
- GitHub gh cli authenticated, full repo scope.
- MCP grok_com_github available for direct GitHub ops (discovered create_pr, push_files, list_pr etc.).
- Local env: macOS, zsh, node/pnpm, python venv, docker assumed for make dev.
- Current working tree dirty with recent gap-closure diffs (KPI canonical object, budget consume/forecast, bootstrap seed gate, policy denial audit logging, OpenAPI route parity).
- No .ai/ existed at start of session (created .ai/ and .ai/agents/).
- Make targets available: dev, test, lint, seed, smoke, build, k8s-validate, infra-validate, release-blockers, real-smoke-*.
- Hard rules from AGENTS/PRD: receipts for effects, budget reservations for spend, leases for workers, no silent drops, fail-closed, adapters only for externals, no direct calls bypassing.

## Confidence per Source
- PRD/TECH v2.0: 95 (explicit, recent, referenced by AGENTS/README)
- Inventory/Trace/Gap/Impl: 90 (self-consistent with code scans)
- Specpack/contracts: 95
- User stories: 80 (good coverage, needs refresh if new flows)
- Roadmap: 75

Next: Rebuild full .ai/requirements-traceability.md and .ai/gap-backlog.md (P0 first), run validation, spawn agents, close feasible gaps, commit/push/PR per rules, iterate to world-class DoD.

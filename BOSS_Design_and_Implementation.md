# BOSS — Design & Implementation Guide

> **BOSS = Business Operating System Sentinel.** A cloud control plane for running an
> autonomous, policy-governed, agentic "company OS." A CEO gives *outcomes*; BOSS turns
> them into shipped artifacts (specs, code, decisions, communications) **safely,
> predictably, and within financial and policy constraints**.
>
> This document maps the **design intent** (from `~/Documents/Boss.pdf`, the v1 PRD +
> Technical Spec) onto the **actual code** in `~/startup/sean/boss`, so you can read the
> codebase with the architecture already in your head. File references use
> `path:line` and are clickable.

---

## 1. The one-paragraph mental model

Everything in BOSS is a **durable database object**, never a chat message. A CEO command
becomes a **Task**; the system plans it into **Jobs**; stateless **Workers** lease jobs
from a Postgres-backed queue, execute them, and write **Receipts** for every external
side effect (so nothing happens twice). Anything risky or expensive stops at a
**Decision** card a human approves. Every spendy action must first hold a **Budget
Reservation**. Communications go out through an **Outbox** (never a direct send) and come
back through an **Inbox**, both with ACK and dead-letters. Agents act under a **RoleSpec**
contract with an earned **autonomy level** and a **trust score**. The whole thing is
multi-tenant (Org → Workspace → Project) and audit-first.

The five **operating invariants** (enforced in code, stated in `README.md:7` and
`agent_runtime.py:12`):

1. **No external effect without a `Receipt`.**
2. **No spendy action without a `BudgetReservation`.**
3. **No worker execution without a lease.**
4. **No silent drops** (outbox/inbox ACK or dead-letter, visibly).
5. **No self-modification** without PR + tests + approval + canary + rollback.

---

## 2. System architecture — four planes

The design (PDF §12) splits the system into four planes. The code realizes them as a
FastAPI monolith (control + comms) plus a Python worker (execution), fronted by a Next.js
console.

```
[ UX / Console ]            apps/web  (Next.js executive console; a *client* of the API)
        │  HTTP (server actions → shared TS client)
        ▼
[ API Gateway / Auth ]      apps/api/app/main.py  (auth middleware, RBAC, ~140 routes)
        │
[ CONTROL PLANE ]           apps/api/app/services/*  (orchestrator, policy, router,
        │                                              registries, budget, decisions)
        ├──────────────► Postgres   (canonical coordination state — 75 tables)
        └──────────────► Object/artifact store (JSON artifacts on disk + JSONB)
        │
[ EXECUTION PLANE ]         apps/worker/boss_worker/*  (stateless workers pull leased jobs)
        │
[ COMMS PLANE ]             services/comms.py  (Outbox senders + Inbox ingesters + ACK)
```

**Design principle that shapes the whole repo (`README.md:26`): API-first.** The control
plane API is *the* product. `apps/web` holds no business logic — it calls the API through
the shared client in `packages/api-client`. New capability lands in `apps/api` (+
`api/openapi.yaml`) first, then the UI consumes it.

**Real-only posture (`README.md:15`):** there are *no* fake providers. If a credential is
missing the API **fails closed** and creates a visible Decision/event rather than
silently mocking. Real integrations today: GitHub (branches/PRs against
`seanheiney/boss-sandbox`), OpenAI (planning/coding), DigitalOcean (sandbox droplets,
flag-gated), and read-only ProviderOps for DO/Cloudflare/Supabase/OpenAI/Anthropic.

---

## 3. Repository layout

Monorepo managed by **pnpm** (`pnpm-workspace.yaml`) + a Python venv. Top-level `dev`
script runs web + api + worker concurrently (`package.json`).

| Path | Stack | Role |
|------|-------|------|
| `apps/api/` | FastAPI + SQLAlchemy (raw SQL) + psycopg, Python | **Control plane** — the product. |
| `apps/worker/` | Python (httpx client loop) | **Execution plane** — leases & runs jobs. |
| `apps/web/` | Next.js (App Router) + React + TS | **Executive console** — API client only. |
| `packages/api-client/` | TypeScript | Shared typed client for the control-plane API. |
| `packages/specpack/` | YAML | Declarative **source of truth**: roles, packs, workflows, policies, skills, object schemas, agent modules. |
| `db/schema.sql` | SQL | The 75-table starter schema. |
| `docs/product/` | Markdown | PRD-v2.0, TECHNICAL_SPEC-v2.0, ROADMAP, IMPLEMENTATION_STATUS, TRACEABILITY. |
| `deploy/`, `infra/` | Helm/K8s/Docker | Deployment guardrails & runbook. |
| `skills/`, `templates/`, `reference/` | mixed | Claude skills, sandbox repo template, original build-spec pack. |

> **Where to start reading:** `README.md` → `docs/product/TECHNICAL_SPEC-v2.0.md` →
> `db/schema.sql` (the object model) → `apps/api/app/services/intake.py` and `jobs.py`
> (the core lifecycle) → `apps/worker/boss_worker/runner.py` (what jobs actually do).

---

## 4. The data model (Postgres, 75 tables)

Postgres is **canonical for coordination state**; artifacts (plans, transcripts, review
packets, diffs) live in an object/JSONB store (PDF §14.1). Access is **raw SQL via
SQLAlchemy `text()`** — no ORM — through a single transactional context manager
(`apps/api/app/db.py:39` `begin(engine)`; auto-commit on exit, rollback on exception).
Schema is applied at startup and seeded by `apps/api/app/bootstrap.py`.

Tables grouped by subsystem:

- **Tenancy & identity:** `orgs`, `workspaces`, `projects`, `project_members`,
  `org_members`, `break_glass_grants`.
- **Canonical work objects:** `tasks`, `plans`, `decisions`, `artifacts`, `meetings`,
  `departments`, `policy_records`, `events` (the audit/activity event log).
- **Execution engine:** `jobs`, `receipts`, `deadletters`.
- **Budget / FinOps:** `budget_policies`, `budget_allocations`, `budget_reservations`,
  `cost_receipts`, `cost_events`, `cost_anomalies`, `commitments`.
- **Comms plane:** `outbox_messages`, `inbox_messages`, `comms_channel_policies`.
- **Roles & governance:** `roles`, `role_reviews`, `autonomy_changes`, `routing_logs`,
  `health_snapshots`.
- **SpecPack / marketplace:** `pack_installs`, `agent_module_installs`,
  `agent_module_install_records`, `capabilities`.
- **Entitlements / model routing:** `entitlement_sources`, `entitlement_connection_sessions`,
  `capacity_receipts`, `provider_adapters`, `secret_refs`.
- **Resource plane / infra:** `provider_accounts`, `provider_health`, `external_resources`,
  `infra_resources`, `changesets`, `changeset_operations`, `resource_receipts`,
  `resource_policies`, `drift_reports`, `reconciliation_runs`, `worker_pools`,
  `scale_plans`, `deployment_targets`, `deployment_environment_policies`,
  `deployment_runs`, `deployment_rollouts`.
- **Self-improvement:** `self_improvement_proposals`.
- **Commercial (open-core):** `billing_accounts`, `license_entitlements`, `account_connect_*`
  (orgs, auth sessions, billing portal sessions, team invites, health checks),
  `hosted_marketplace_pack_listings`, `hosted_marketplace_sync_runs`.
- **Enterprise:** `enterprise_clusters`, `enterprise_cluster_validation_runs`,
  `enterprise_identity_providers`, `enterprise_identity_rollout_policies`,
  `enterprise_saml_sessions`, `enterprise_scim_users`, `enterprise_scim_groups`,
  `enterprise_support_cases`.

**Two structural patterns worth internalizing:**

- **`data_boundary` everywhere.** Tasks, jobs, artifacts, receipts, and messages carry a
  classification: `public | internal | confidential | restricted | local-only`. Jobs and
  artifacts inherit the task's boundary unless overridden. External effects are **blocked
  before receipt creation** when the boundary is `restricted`/`local-only`
  (`README.md:263`).
- **`dedupe_key` everywhere.** Tasks (unique per project), receipts (unique per **org** —
  the global idempotency anchor), and messages all carry dedupe keys to make creation and
  side effects exactly-once.

---

## 5. The canonical lifecycle: command → shipped artifact

This is the spine of the system (PDF §5.2). Trace it through the code once and the rest
falls into place.

```
POST /v1/commands/propose  { command, constraints, actor, project_id }
        │  intake.propose_command()                       apps/api/app/services/intake.py:176
        ▼
   1. Create TASK            (priority p1, risk_tier 2, required_artifacts=[plan_draft])
   2. Create ARTIFACT        (type "command_brief")
   3. Create JOB             (kind "plan.command", payload carries resolved agent_module)
        │
        ▼  worker loop                                     apps/worker/boss_worker/runner.py
   4. POST /v1/worker/poll  → control plane leases the job (SKIP LOCKED, budget-gated)
   5. Worker heartbeats (LEASED → RUNNING), executes by job kind, builds a plan_draft
   6. POST /v1/worker/complete → artifact persisted, BLOCKING DECISION opened if approval needed
        │
        ▼
   7. Decision Inbox: human approves → follow-on queues software_factory.propose_pr
   8. Software Factory: branch + commit + PR + CI evidence + review packet + go/no-go
   9. PR review Decision → merge gate → (flag-gated) real merge job
  10. Deploy review Decision → deployment job → post-deploy health check → rollout artifact
```

Every arrow that touches money first passes **quote → reserve**; every arrow that touches
the outside world ends in a **receipt**; every arrow that needs a human stops at a
**decision**. Nothing is implicit.

### CEO command intake — `intake.py`
`propose_command()` (`intake.py:176`) creates the Task (`create_task()` `intake.py:21`), a
`command_brief` artifact, and a `plan.command` Job whose payload already carries the
resolved **agent module** (role charter + prompt bundle) from the SpecPack registry.
Response: `{ task_id, job_id, artifact_id, status: "accepted" }`.

### "What changed?" — Metatron — `metatron.py`
`POST /v1/metatron/query` (`metatron.py:24`) is the omnipresent status surface. It
**categorizes** the question by keywords (budget / decisions / risks / activity / status),
answers it directly from live DB state, and returns `answer`, `freshness_at`,
`references` (typed object IDs), `top_risks`, and `suggested_actions` — explicitly **not**
free-form chat. This is the "Executive Console: what changed, next actions, decisions, top
risks, run-rate" requirement (PDF §7.3).

---

## 6. Execution engine — jobs, leases, receipts

> Files: `services/jobs.py` (1071 lines), `services/receipts.py`, `services/deadletters.py`,
> `worker/boss_worker/runner.py` (753 lines), `worker/boss_worker/control_plane.py`.

### 6.1 Job state machine
Design (PDF §15.1): `QUEUED → LEASED → RUNNING → SUCCEEDED`, with
`FAILED_RETRYABLE → QUEUED (backoff)` and `FAILED_FINAL → DEADLETTERED`. The code
implements exactly this, plus a `CANCELLED` terminal state for budget-blocked jobs.
Enforced in `jobs.py:122` (claim), `jobs.py:326` (complete/fail). The object schema is
mirrored declaratively in `packages/specpack/objects/job.yaml`.

### 6.2 Lease protocol (pull-based)
Workers **poll** by capability set. The control plane selects the next eligible job with a
single SQL statement using **`FOR UPDATE SKIP LOCKED`** (`jobs.py:137`), ordering so that
**expired leases are reclaimed first**, then `failed_retryable`, then fresh `queued`:

```sql
SELECT ... FROM jobs
WHERE kind IN :capabilities
  AND run_after <= :now
  AND (status IN ('queued','failed_retryable')
       OR (status='leased' AND lease_expires_at <= :now))   -- stuck-lease reclaim
ORDER BY (status='leased') DESC, priority ASC, created_at ASC
LIMIT 10 FOR UPDATE SKIP LOCKED
```

A lease produces a random `lease_token` returned to the worker; only its **SHA-256 hash**
is stored (`jobs.py:772`), so a stale worker can't replay. TTL default 60s
(`worker/config.py`). Heartbeat extends the lease and flips `LEASED → RUNNING` on first
beat (`jobs.py:277`). Expired leases are reclaimed automatically by the poll ordering,
emitting `job.lease_reclaimed`.

### 6.3 The worker loop — what jobs actually *do*
`runner.py` `run_worker_cycle()` (`runner.py:693`): poll → (idle? sleep) → heartbeat →
`execute_job()` dispatch by `kind` → complete or fail. The big dispatch (`runner.py:537`)
covers these **job kinds**, most of which delegate back to control-plane HTTP endpoints so
the side effect runs inside the governed path:

| Job kind | Effect |
|----------|--------|
| `agent.*`, `plan.command`, `plan.generate` | Build plan/agent-brief artifact locally; open a blocking decision. |
| `openai.plan` | Brokered OpenAI planning call. |
| `software_factory.propose_pr` | GitHub branch+commit+**PR** + CI + review packet. |
| `github.pull_request.merge` | Real PR merge (flag-gated). |
| `github.deployment.create` | Deployment run + post-deploy health check. |
| `infra.droplet.create` / `.delete` | DigitalOcean provision/terminate (receipt-backed). |
| `comms.outbox.deliver` | Real Slack/email send via receipt + dedupe. |
| `providerops.sync.readonly` | Read-only provider inventory/health sync. |
| `enterprise.marketplace.validate` | Marketplace listing validation (no external effect). |

The worker talks to the control plane **only over HTTP** (`control_plane.py`): `/v1/worker/poll`,
`/heartbeat`, `/complete`, `/fail`, with optional bearer auth.

### 6.4 Retries & dead-letters
On failure (`jobs.py:368`): if `retryable` and `attempt < max_attempts` →
`failed_retryable` with `run_after = now + retry_delay` (delay chosen by the control plane
per kind — note: **no built-in exponential backoff**, it's caller-specified). Otherwise →
insert into `deadletters` and flip job to `deadlettered`, emitting `job.deadlettered`. A
dead-letter can be turned into an **incident** (creates a p0/p1 task + packet artifact,
`deadletters.py:19`) or **requeued** as a fresh job (`deadletters.py:99`) — satisfying the
"no silent drops" invariant with operator-visible recovery.

### 6.5 Exactly-once effects — receipts
The heart of invariant #1 (PDF §16). Before any external action a worker calls
`receipts.create_or_reuse(org_id, dedupe_key, ...)` (`receipts.py:49`). The
`(org_id, dedupe_key)` uniqueness means a retry or crash **reuses** the existing receipt
instead of repeating the effect. Status flow `in_progress → succeeded|failed`
(`receipts.py:133`), storing `provider_ref` (the external system's ID) and `result`. The
enforcement sequence in code is **check receipt → reserve budget → execute → mark
receipt** (see `comms.deliver_outbox_message`, `comms.py:356`, as the canonical example).

---

## 7. Governance & FinOps

### 7.1 Budget — quote → reserve → execute → consume
> File: `services/budget.py` (780 lines). Objects: `budget_policies`, `budget_allocations`,
> `budget_reservations`, `cost_receipts`.

The CEO "Operational Intensity" slider (PDF §5.5) maps onto **budget caps + a utilization
ratio**. The four-phase billable-action gate (PDF §22.2):

1. **Quote** (`budget.py:328`) — estimate `reserved + actual + requested` vs cap; no side
   effect.
2. **Reserve** (`budget.py:365`) — atomic check against `cap_usd_micros`; on success write
   a `budget_reservations` row (status `active`, with TTL `expires_at`); **on cap breach
   create a blocking Decision** instead of proceeding.
3. **Execute** — the job runs (and is only leased if it holds a reservation, `jobs.py:775`).
4. **Consume/settle** (`budget.py:706`) — write a `cost_receipt` linking reservation →
   receipt, record estimated vs actual, mark reservation `consumed`.

**Hierarchical budgets** (Org → Workspace → Project → Initiative): `budget_allocations`
nest via `parent_allocation_id`; a child or a reservation that exceeds the available
parent is denied (`budget.py:241`).

**Budget color** drives runtime behavior (`_state_for_totals`, `budget.py:763`):

| Color | Utilization | Runtime effect (enforced at lease time, `jobs.py:791`) |
|-------|-------------|--------------------------------------------------------|
| GREEN | ≤ 70% | Normal routing. |
| YELLOW | 70–85% | Soft cap; discourage new spend. |
| ORANGE | 85–90% | Defer non-p0/p1 jobs (~300s). |
| RED | ≥ 90% | Halt non-p0 work; freeze provisioning + higher-risk merge/deploy/comms behind decisions. |

Stale reservations auto-expire on every status read (`budget.py:17`), and the top-5 cost
drivers (by skill/model) are tracked for the console (`budget.py:141`).

### 7.2 Decisions — the approval inbox
> File: `services/decisions.py` (1269 lines). Schema: `packages/specpack/objects/decision.yaml`.

A Decision card carries `question`, `recommendation`, `options`, `cost_summary`,
`risk_tier`, `deadline_at`, and a `default_action`. States:
`pending | delegated → approved | rejected | revision_requested | expired → executed`
(`decisions.py:251`). **Risk tiers gate approvers** (PDF Appendix A `default_safety`):
tier 0–1 need none, tier 2 needs CEO, tier 3 needs CEO + a second officer
(`members.require_decision_actor`). Overdue pending decisions transition to `expired` and
log the **safe default** (`expire_overdue`, `decisions.py:39`).

The powerful part is **follow-on actions**: approving a decision *does the next thing*. By
`recommendation.action`, approval queues a PR proposal from a plan
(`_queue_pr_proposal_from_plan`, `decisions.py:674`), checks a **CI validation gate**
before allowing a merge (`decisions.py:612`), queues deployment jobs, advances
self-improvement, or applies an autonomy change. When auto-merge is disabled it writes an
explicit `merge_gate` artifact rather than silently stopping. **Decision digests**
(`decisions.py:96`) batch low-risk decisions and separate urgent ones, optionally drafting
an outbox notification.

### 7.3 Communications plane — outbox / inbox
> File: `services/comms.py` (1136 lines). Tables: `outbox_messages`, `inbox_messages`,
> `comms_channel_policies`.

All outbound comms go through the **Outbox** (PDF §17): `draft → queued → sending → acked`,
or `blocked | retrying | deadlettered` — never a direct send. Delivery
(`comms.deliver_outbox_message`, `comms.py:356`) is a **leased worker job**
(`comms.outbox.deliver`) so a real send still requires lease + reservation + receipt. It
checks, in order: active reservation → provider supported → data boundary allowed →
delivery policy (`real_send_allowed`?) → send → mark receipt → `acked`. Any block creates a
visible Decision and releases the reservation. Providers are **fail-closed by default**:
Slack needs `BOSS_SLACK_REAL_SEND_ALLOWED=true` + webhook; email needs
`BOSS_EMAIL_REAL_SEND_ALLOWED=true` + SMTP config.

**Quiet hours & throttling** (`comms.py:1014`): non-urgent messages are deferred via job
`run_after`; per-channel hourly caps push the next slot out; `urgent` skips scheduling
deferrals but still needs reservation+lease+receipt.

The **Inbox** (`comms.py:743`) ingests inbound messages (`received → actioned | deadlettered`),
**deterministically parses decision replies** (regex for `dec_…` id + approve/reject/
revise/execute/delegate, `comms.py:1093`), and routes valid replies through the *same*
permissioned decision service as the UI. Malformed replies become visible
`inbox_message` dead-letters.

### 7.4 Role governance — RoleSpec, autonomy ladder, trust
> File: `services/role_governance.py` (1248 lines). Tables: `roles`, `role_reviews`,
> `autonomy_changes`. Policy: `packages/specpack/policies/autonomy_ladder.yaml`.

Each role runs under a **RoleSpec** contract (mission, boundaries, decision rights,
`allowed_skills`/`forbidden_skills`, autonomy level, eval rubric — see
`packages/specpack/roles/cto.yaml`). Three mechanisms:

- **Scope enforcement** (`check_scope`, `role_governance.py:116`): a skill matched against
  forbidden/allowed patterns; a violation drops trust by 10, downgrades autonomy a level,
  and writes a `role_scope_violation` artifact + events.
- **Autonomy ladder** (`check_autonomy`, `role_governance.py:47`): action + risk_tier +
  sandboxed/policy-approved → a required capability; the role's current level must reach
  it. **Upgrades require evidence** (e.g. ≥30 successful runs, 0 violations, rollback rate
  < 2%) **and an explicit Decision**; **downgrades are automatic** on violations.
- **Trust score** (0–100, `record_review`, `role_governance.py:312`): positive signals
  (successful runs, clean upgrades) and weighted penalties (policy violations −20,
  duplicate effects −20, hidden failures −15, scope violations −15, low-quality −5). Posted
  via `POST /v1/roles/{role_key}/reviews`.

### 7.5 Audit & health
- **Audit export** (`services/audit.py:106`) reconstructs the full command → task → job →
  decision → artifact → receipt → event chain (optionally scoped), and can persist it as a
  durable `replay_bundle` or `compliance_evidence_bundle` artifact. Enterprise governance
  can gate exports.
- **Routing logs** (`routing_logs`) capture every model/skill routing decision: chosen
  source/model, denial reasons, rejected alternatives, budget state, data boundary.
- **Health snapshots** (`services/health.py`): `GET /v1/status` returns live `top_risks`
  derived from budget pressure, high-risk pending decisions, dead-letters, blocked
  messages, and missing credentials.

---

## 8. The Software Factory & Self-Improvement

### 8.1 Software Factory — `services/software_factory.py` (2251 lines)
Implements the design's `spec → plan → code+tests → validate → PR → merge → deploy`
pipeline (PDF §7.8), confidence/CI-gated:

1. **Plan** (`software_factory.py:551`): brokered OpenAI call (budget-reserved,
   entitlement-routed) → `plan_draft`.
2. **PR proposal** (`software_factory.py:1198`): OpenAI generates a review packet →
   GitHub branch + file commits → **poll real CI check-runs** → optional **local sandbox
   validation** (runs allow-listed commands in a real clone, receipt-backed as
   `sandbox.validation.run`) → security/dependency scan → **release go/no-go**. Emits 5
   artifacts (`review_packet`, `diff_manifest`, `security_dependency_review`,
   `test_report`, `release_go_no_go`) and a blocking merge Decision.
3. **Merge** (`software_factory.py:1738`): real GitHub merge — but **only if** the linked
   validation report is `passed` and `BOSS_ENABLE_SANDBOX_MERGE=true`.
4. **Deploy** (`software_factory.py:1925`): GitHub deployment record → post-deploy HTTP
   health check (`BOSS_SANDBOX_DEPLOY_HEALTHCHECK_URL`) → durable `deployment_rollout`
   artifact with staged evidence; failed health creates a blocking decision, not a silent
   success.

*Gap vs PDF:* the explicit **chunking of large work into small PRs** and a dedicated
test-generation stage are not yet distinct stages — proposals are single units today.

### 8.2 Self-improvement — `services/self_improvement.py` (679 lines)
The controlled `observe → propose → canary → rollout → rollback` loop (PDF §24), and the
mechanism behind invariant #5. Proposals **ship as artifacts/PRs, never in-place mutation**.
Creating one requires change ref + diff summary + test plan + canary scope + rollback plan
+ success metrics, and opens a canary-approval Decision (`self_improvement.py:74`). A
successful canary auto-creates the rollout Decision (`self_improvement.py:362`); rollout and
rollback are recorded as first-class artifacts so BOSS can prove how it changed itself.

### 8.3 Agent runtime — `services/agent_runtime.py`
How an agent actually runs: it resolves the installed **agent module** for a job kind and
compiles a prompt bundle — `system` (role + mission + boundaries + permissions + autonomy +
the 10 BOSS invariants, `agent_runtime.py:12`), the job-specific prompt, an operational
checklist (allowed/forbidden skills, escalation), and the runtime contract — then attaches
that context to the job payload (`agent_runtime.py:98`). Model calls themselves go through
the entitlement broker (below) and provider adapters.

---

## 9. Resource Plane & infrastructure

> File: `services/resource_plane.py` (6465 lines — the largest module),
> `services/infrastructure.py`, and provider adapters.

This is the "elastic worker pools + autoscaling + cloud governance" requirement (PDF §22–23),
realized as a **Resource Plane Kernel**. Its sections:

- **Providers** (`resource_plane.py:150`): register/test accounts for DigitalOcean,
  Cloudflare, Supabase, OpenAI, Anthropic via official APIs; credentials referenced as
  `env:…` `secret_refs`.
- **Inventory, cost, drift, health** (`resource_plane.py:353–727`): sync
  `external_resources`, ingest `cost_events`/`cost_anomalies`, record `provider_health`,
  produce `drift_reports` and `reconciliation_runs`. Threshold-crossing observed spend
  flips a provider account to **read-only** with a Decision card.
- **ChangeSets** (`resource_plane.py:1439`): **every provider write must be a ChangeSet** —
  `quoted → policy_checked → pending_approval | budget_reserved → approved → applied`.
  Operations carry `risk_tier` + a `destructive` flag; risk ≥ 2 requires approval; apply
  executes via the adapter and writes a `resource_receipt` + settles budget. First write
  skills: Cloudflare DNS/R2/Worker/Pages, DO droplet create/destroy + App Platform,
  Supabase project create/pause + migration apply.
- **Autoscaling** (`resource_plane.py:2125`): `desired = ceil(queue_depth / per_worker)`,
  then clamped by `min`, policy max, **budget headroom**, and **provider health** — and the
  **Budget Governor always wins** (RED drains to minimum; ORANGE blocks cost-increasing
  scale-up behind approval). Matches the PDF "scale up fast, scale down slow" governor model.
- **Deployments** (`resource_plane.py:752`): `deployment_targets` +
  `deployment_environment_policies` (sandbox vs production gating) + planned
  `deployment_runs` with health checks.

**Adapters** (all real when enabled, fail-closed otherwise): `github_adapter.py`
(branch/commit/PR/merge/deployment/check-runs via httpx), `openai_adapter.py`
(`responses.create`), `digitalocean_adapter.py` (droplet/app via API, gated by
`BOSS_INFRA_REAL_PROVISION_ALLOWED`), `slack_adapter.py` (webhook), `email_adapter.py`
(SMTP), and `sandbox_validation.py` (subprocess in an allow-listed local clone).

---

## 10. SpecPack — the declarative source of truth

> Files: `packages/specpack/**` (YAML), loaded/validated by `services/specpack.py` (1517
> lines) and `services/entitlements.py`.

BOSS's organizational structure is **data, not hardcoded logic**. `packages/specpack/`
defines, as validated YAML:

- **Objects** (`objects/*.yaml`) — canonical schemas + state machines (task, decision, job,
  receipt, …) mirroring `db/schema.sql`.
- **Roles** (`roles/*.yaml`) — RoleSpec contracts (CTO, CAO, COO, PM, QA, Metatron, VP
  Agents, FinOps Sentinel, ProviderOps Manager, Resource Janitor, …).
- **Workflows** (`workflows/*.yaml`) — phased pipelines with `owner_role`, `inputs`,
  `outputs`, `gates`, `on_failure` (e.g. `ceo_command_to_pr.yaml` — the §5.2 flow as data).
- **Policies** (`policies/*.yaml`) — `default_safety` (risk-tier approvals, receipt/budget/
  lease requirements, sandbox real-mode flags), `budget_governor_policy`, `autonomy_ladder`,
  `entitlement_broker`, `resource_plane_policy`, `enterprise_governance_policy`.
- **Skills** (`skills/*.yaml`) — typed tool definitions: `risk_tier`, `input_schema`/
  `output_schema`, `cost`, `retry`, `permissions`, `policy` (requires_receipt, dedupe key
  template, allowed targets). This is the "Skills Registry — define tools once" (PDF §19).
- **Modules** (`modules/*.yaml`) — packaged **agent modules** bundling a role charter +
  prompt bundle + runtime job kinds + config defaults (the marketplace unit).
- **Packs** (`packs/*.yaml`) — installable bundles of roles + workflows + policies + skills
  + canaries (`core_v1`, `core_v2`, the edition packs, `providerops_pack`,
  `software_factory_pack`).

**Installation** (`specpack.py`): `install_pack` validates every role/skill schema at load
time, writes `pack_installs`, and emits `pack.installed`. The dogfood seed installs
`core_v1` + the first agent modules (`metatron_operator_v1`, `codex_software_engineer_v1`)
so role lineage and prompts come from installable units, not hardcoded service logic
(`README.md:103`).

### Model entitlement broker — `services/entitlements.py`
AI capacity is treated as **governed entitlement sources**, not free compute (PDF §19,
`docs/product/MODEL_ENTITLEMENT_BROKER.md`). Sources are typed `official_cli`,
`official_coding_agent`, `official_api`, `approved_local_model`, each with `auth_method`,
`connection_status`, supported models, `risk_tier_max`, and observed capacity. Routing
**fails closed** without credentials, budget reservation, or capacity; restricted/local-only
work is denied to unapproved external sources. Every route writes a `routing_log` with the
chosen source/model and rejected alternatives. OpenAI planning and PR generation pass
through the broker before execution.

---

## 11. Commercial (open-core) & Enterprise layers

These extend the platform toward the PDF's v2+ "enterprise readiness and ecosystem" goals
(PDF §6, §25) and are organized as **editions** (`docs/product/` + `commercial.py`).

- **Editions** (`services/commercial.py`, 2520 lines): `community` (local-first, no Account
  Connect) → `cloud` (Account Connect required) → `enterprise_cloud` / `enterprise_self_hosted`.
  `edition_status` reports the active edition, whether Account Connect is linked, license
  candidate, blocked reasons, and included capabilities.
- **Account Connect**: an HTTP bridge to hosted org management (entitlements, billing
  portal, marketplace listings). Linking writes `account_connect_orgs` and emits events.
- **License entitlements**: capabilities come from the edition pack plus custom
  `license_entitlements`, optionally synced from Account Connect.
- **Enterprise** (`services/enterprise.py`, 2060 lines + `saml.py`, `oidc.py`): identity
  providers (OIDC / SAML / SCIM) with progressive rollout (`metadata_only → auth_allowed →
  provisioning_allowed`); SCIM user/group provisioning mapped to BOSS roles/permissions;
  `enterprise_clusters` with **validation runs** (multi-node readiness, SAML federation,
  SCIM sync) that gather evidence before a production-readiness claim; and enterprise
  support cases that can escalate to the hosted org with SLA tracking.

Transport auth for `/v1/*` supports API keys, **OIDC bearer JWTs**, and **SAML** sessions,
all failing closed to a resolved project member (`main.py` auth middleware, `README.md:113`).

---

## 12. The executive console (`apps/web`)

A Next.js App Router app that is **purely an API client**. `app/page.tsx` (6053 lines) is a
server component that fetches ~30 data feeds in parallel and renders the console sections:
Hero + **CEO command palette**, **Metatron** status query, status dashboards, org delegation
& **break-glass** grants, **Budget control center** (spend, reservations, forecast, top
drivers, allocations), **ProviderOps** (provider matrix, ChangeSet gates, deployments,
drift/cost/health), **Comms** (outbox/inbox + channel policies), **Company Pack & Roles**
(autonomy/trust dashboards), **Commercial/Enterprise** panels, and the **Decision Inbox**.
Mutations go through `app/actions.ts` (1469 lines) server actions, which call the typed
`packages/api-client` and then `revalidatePath('/')`. Every action carries `actor` +
`project_id` for RBAC and audit. The console is **project-scoped and actor-scoped** at
runtime (not hard-wired to `boss-dogfood`).

---

## 13. Tenancy, auth & RBAC

- **Hierarchy:** Org → Workspace → Project; almost all `/v1/*` routes take a `project_id`
  and scope data to it. Org-level members can act across projects in their org; cross-org is
  blocked.
- **Transport auth** (`main.py` middleware): API key (`X-BOSS-API-Key` / bearer), OIDC JWT,
  or SAML — gated by `BOSS_REQUIRE_API_AUTH` / `BOSS_OIDC_ENABLED` /
  `BOSS_ENTERPRISE_SAML_ENABLED`. Fails closed (503) if required but unconfigured.
- **Actor binding:** an API key can be bound to a member principal
  (`BOSS_API_KEY_PRINCIPALS`); mutations then can't spoof a different `actor`.
- **Authorization:** `require_project_actor(connection, project_id, actor, permission)`
  (`main.py:634`) checks the member's permissions per route (e.g. `command_submit`,
  `decision_resolve`, `pack_manage`, `comms_manage`). Denials emit `policy.denied` events
  for audit visibility.
- **Break-glass:** TTL-bounded emergency grants that mandate a postmortem task.

---

## 14. Design ↔ implementation traceability

| Design requirement (PDF) | Status in code | Where |
|--------------------------|----------------|-------|
| Canonical objects, durable queue, leases, receipts | **Built** | `jobs.py`, `receipts.py`, `db/schema.sql` |
| Exactly-once side effects (dedupe + receipts) | **Built** | `receipts.py:49`; `(org_id,dedupe_key)` unique |
| Decision inbox w/ recommendation/options/deadline/default | **Built** | `decisions.py` |
| Budget quote→reserve→execute→receipt; colors; hierarchy | **Built** | `budget.py` |
| Outbox/Inbox ACK, escalation, deadletters, no silent drops | **Built** | `comms.py`, `deadletters.py` |
| Model/skill router with logged reasons + fail-closed | **Built** | `entitlements.py`, `routing_logs` |
| Skills registry (typed IO, cost, risk, retry) | **Built** (declarative) | `packages/specpack/skills/*` |
| RoleSpec, autonomy ladder, trust scoring | **Built** | `role_governance.py` |
| Meetings consensus primitive (propose→critique→revise→vote) | **Partial** | `meetings` table + `/v1/meetings/{id}/run`; phases are config |
| Software Factory (spec→PR→merge→deploy, CI-gated) | **Built**; chunked-PR/test-gen stages **not yet distinct** | `software_factory.py` |
| Self-improvement (canary→rollout→rollback as artifacts) | **Built** | `self_improvement.py` |
| Elastic worker pools + autoscaler (budget/health governed) | **Built** (kernel slice) | `resource_plane.py:2125` |
| Infra commitments / reconciliation | **Built**; some provider cost ingestion still partial | `resource_plane.py`, `commitments` |
| Multi-tenant, RBAC, SSO/SCIM, audit export, compliance bundles | **Built** | `enterprise.py`, `audit.py` |
| Public API MVP endpoints | **Exceeded** — ~140 endpoints | `main.py`, `README.md:130` |
| Exponential backoff for retries | **Gap** — delay is caller-specified per kind | `jobs.py:368` |

**Net:** the implementation is a faithful and unusually complete realization of the v1
spec, and has already pushed into the v2 commercial/enterprise roadmap. The honest gaps are
small and noted above (PR chunking, distinct test-gen stage, exponential backoff, meeting
phase richness). For the authoritative current-state list, see
`docs/product/IMPLEMENTATION_STATUS.md` and `docs/product/TRACEABILITY.md`.

---

## 15. How to run it (from `README.md`)

```bash
cp .env.example .env     # fill required secrets; set BOSS_POSTGRES_PORT if 5432 is taken
make install             # pnpm install + python venv
make seed                # apply db/schema.sql + seed dogfood org/workspace/project + core_v1 pack
make dev                 # Postgres + web (3000) + api (8000) + worker, concurrently
```

Useful targets: `make test`, `make lint`, `make smoke`, `make real-smoke-plan`,
`make real-smoke-pr`, `make real-smoke-merge`, `make real-smoke-deploy`,
`make worker-drain-once`, `make k8s-validate`, `make infra-validate`, `make release-blockers`.

---

*Generated from a full read of the BOSS codebase (`~/startup/sean/boss`, release v0.4.1)
cross-referenced against the design deck `~/Documents/Boss.pdf`. File:line references point
to the live code; pair this document with `README.md` and `docs/product/TECHNICAL_SPEC-v2.0.md`
as you read.*

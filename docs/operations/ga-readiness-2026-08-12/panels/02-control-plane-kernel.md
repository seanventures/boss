# Panel 02 — Control Plane Kernel (GA Readiness)

**Date:** 2026-08-12  
**Scope:** commands, tasks, plans, budgets (quote/reserve/consume/release), jobs, leases/TTL/heartbeat, decisions inbox, receipts/dedupe, deadletters, events, tenant scope  
**Code roots:** `engine/boss_engine/services/{budget,jobs,receipts,decisions,deadletters,intake,core,canaries}.py`, `apps/api/app/main.py`, `db/schema.sql`, `packages/specpack/policies/default_safety.yaml`  
**Policy SoR:** `default_safety` — `require_receipt`, `require_dedupe_key`, `require_budget_reservation`, `require_lease`, `require_heartbeat`, `no_silent_drops`

---

## Executive verdict

| Dimension | Grade | Notes |
|---|---|---|
| **Overall control-plane kernel** | **WARN** | Core primitives are implemented, evented, and largely tested. Hard rules hold on the main paths; residual GA risk is incomplete spend-gate allowlists, soft task FSM, receipt concurrency, and weak read-side tenant auth. |
| Hard rules (no silent drops / no unreserved spend) | **WARN** | Failures retry → deadletter → escalate; spendy lease path asserts active reservation. Gaps: incomplete `_requires_reservation` kind set; `cap=0` → unlimited; burst deny without decision. |
| State machine completeness | **WARN** | Jobs/decisions/reservations/receipts have clear statuses + transitions. Tasks/plans are lightly constrained (no closed enum / transition table). Decision expiry records safe default but does not execute side effects. |
| Test / canary coverage | **PASS** with gaps | Strong API + worker + canary coverage for budget consume, leases, deadletters, decisions inbox transitions; weaker concurrent receipt race and full job-kind spend-matrix tests. |

**GA recommendation:** Ship kernel as **conditional GO** for internal dogfood / design partners; close **FAIL-adjacent WARN** items (spendy-kind completeness, receipt upsert race, task status contract, read API auth) before multi-tenant commercial GA.

---

## Subsystem scorecard

| Subsystem | Grade | Summary |
|---|---|---|
| Commands & plans (intake) | **PASS** | Propose creates task + plan + budgeted plan job or deterministic fallback; dispatch is project-scoped. |
| Tasks | **WARN** | Create/dedupe/parent/deps/archive work; no formal status state machine or transition tests. |
| Budgets (quote/reserve/consume/release) | **PASS** | Full path with events, tier2 freeze, allocations, overrun events; cap=0 edge is WARN. |
| Jobs + leases/TTL/heartbeat | **PASS** | Claim with `FOR UPDATE SKIP LOCKED`, reclaim expired leases, heartbeat, complete/fail/retry/DLQ. |
| Spend gate at lease | **WARN** | Hard assert for allowlisted kinds / `requires_reservation`; allowlist incomplete vs “all spendy.” |
| Decisions inbox | **PASS** | Transition table + actor rights + digest/expiry; default_action not executed on expire. |
| Receipts / dedupe | **WARN** | Unique `(org_id, dedupe_key)` + create_or_reuse; race lacks `ON CONFLICT`. |
| Deadletters / no silent drops | **PASS** | Terminal fail → DLQ + events; incident + requeue remediation; worker fail path tested. |
| Events | **PASS** | Canonical write always; dispatch isolated with `job.dispatch_failed` on error. |
| Tenant / project scope | **WARN** | Writes and most queries are `project_id`-scoped; several read endpoints lack actor binding. |
| Policy alignment (`default_safety`) | **WARN** | Implemented in code paths, not as a single runtime policy engine evaluating YAML. |

---

## 1. Commands & plans — **PASS**

### Evidence

- `CommandIntakeService.propose_command` (`engine/boss_engine/services/intake.py`):
  - Creates task with unique `dedupe_key`, risk_tier=2, required artifact `plan_draft`.
  - Inserts non-actuating `plans` row + `plan.proposed` event.
  - If model configured: `budget.reserve` → job `openai.plan` with `requires_reservation: true` + `attach_job`; else `plan.command` job (no spend).
- `dispatch_plan` requires matching `project_id` (“never load a plan by id alone”) and refuses double-dispatch; phase tasks use `plan:{id}:phase:{i}` dedupe keys; emits `plan.dispatched`.
- API: `POST /v1/commands/propose` (rate-limited high-cost), task CRUD, plan dispatch surface via intake.

### State notes

| Entity | Statuses observed | Transition enforcement |
|---|---|---|
| Plan | `draft` → `dispatched` | Explicit refuse if already dispatched |
| Command outcome | `accepted` (response only) | Not a durable command table |

### Gaps / GA risks

- Command `dedupe_key` is `make_id("cmd")` (always unique) — **no CEO-command idempotency** on re-submit of the same text.
- Orchestration after `plan.dispatched` depends on subscriptions/worker capabilities (out of pure kernel; product glue risk per PRD gaps doc).

---

## 2. Tasks — **WARN**

### Evidence

- Schema (`db/schema.sql` `tasks`): `status` free TEXT default `open`; `UNIQUE(project_id, dedupe_key)`; parent/deps/data_boundary columns.
- Create path validates parent/deps/roles **within project** (`intake.create_task`).
- API: list/get/create; archive sets `status='archived'` (`main.py` `archive_task_endpoint`).
- Decision execute may set task `status='completed'` + `task.completed` event (`decisions.transition` execute branch).

### Gaps / GA risks

- **No closed status enum or transition matrix** (open → in_progress → blocked → completed → archived). Arbitrary strings allowed at DB layer.
- Limited dedicated tests for task lifecycle vs decision/code-task run lifecycles.
- **Grade WARN** for GA completeness of “canonical task plane,” not for basic create/list.

---

## 3. Budgets (quote / reserve / consume / release) — **PASS** (edge WARN)

### Evidence

| Operation | Implementation | Events / side effects |
|---|---|---|
| Status | `BudgetGovernor.status_for_project` — expires stale, sums active reservations + cost_receipts, utilization bands green/yellow/orange/red, `tier2_frozen` | — |
| Quote | `quote()` — dry-run allowed/post-state | — |
| Reserve | Cap check → blocking decision + deny; burst $/min deny; allocation headroom; insert `active` | `budget.reservation_created` / `*_denied`; `policy_eval_log` |
| Release | Active only → `released` | `budget.reservation_released` |
| Consume | Requires **active** reservation + receipt in project → `settle` cost_receipt + reservation `consumed`; overrun event if actual > reserved | `budget.reservation_consumed` / `budget.reservation_overrun` |
| Expire | Sweeper on status/reserve/assert | `budget.reservation_expired` |

Code: `engine/boss_engine/services/budget.py` (`quote` ~411, `reserve` ~458, `release` ~745, `consume` ~809, `settle` ~971).

API surface (`main.py`):  
`/v1/budget/{status,forecast,quote,reserve,release,consume,allocations,set_intensity,insights}`.

Tests:  
`apps/api/tests/test_api.py` — `test_budget_consume_records_cost_receipt_and_consumes_reservation`, permission + missing reservation 409, forecast + release.  
Canary: `_canary_budget_reserve_release` in `canaries.py`.

### Hard-rule alignment

- **No unreserved consume:** `assert_active_reservation` required; missing → raise / HTTP 409.
- **Denied spend is visible:** cap/allocation denial creates **blocking decision** + events (not silent).
- **Burst cap deny:** returns `decision_id: None` with message only — **WARN** (tracked via `policy_eval_log`, but no decision inbox item).
- **No budget policy (`cap_usd_micros == 0`):** `budget_state=unknown` and quote/reserve treat cap 0 as unlimited — **WARN** for misconfigured projects.

### Gaps / GA risks

1. Double-consume of same reservation not uniquely constrained beyond status flip (second call should fail assert_active — OK if sequential; concurrent settle race untested).
2. Intensity slider maps model tiers hard-coded — product fine, not a kernel defect.

---

## 4. Jobs, leases, TTL, heartbeat — **PASS**

### Evidence

**Statuses:** `queued` | `failed_retryable` | `leased` | `running` | `succeeded` | `failed_final` → `deadlettered` | `cancelled`.

| Operation | Behavior |
|---|---|
| Enqueue | `enqueue_job` optional `dedupe_key`; `ON CONFLICT (project_id, dedupe_key) DO NOTHING` |
| Claim | Capability filter; `FOR UPDATE SKIP LOCKED`; resource allocation slot; budget state deferral; spend gate; lease token hash + TTL; attempt++ |
| Reclaim | Expired `leased` **or** `running` re-claimable (crash recovery) |
| Heartbeat | Extends TTL; `leased` → `running` |
| Complete | Clears lease; releases work slot; kind-specific finalize |
| Fail | Retryable → `failed_retryable` + `run_after`; else `failed_final` → insert deadletter → `deadlettered` + related message/code_task updates |

Code: `engine/boss_engine/services/jobs.py` (`claim_job` ~311, `heartbeat` ~536, `complete` ~597, `fail` ~647, `_load_leased_job_for_update` ~856).

Policy: `default_safety.worker_jobs.require_lease/require_heartbeat`, `max_attempts_default: 3` matches schema default.

Tests / canaries:

- Worker: `test_run_worker_cycle_completes_leased_job`, failure → `fail` not complete, deadletter path (`apps/worker/tests/test_worker.py`).
- Workflow leases: `test_expired_leases_are_reclaimed_and_exhausted_retries_deadletter` (`test_workflow_runtime.py`).
- Canary: `_canary_job_lifecycle` queued→leased→succeeded.

### Gaps / GA risks

- Heartbeat spam emits `job.heartbeat` event every beat — audit volume under load (ops, not correctness).
- Lease reclaim of `running` can double-execute non-idempotent work if effect was applied but complete never called — mitigated only if adapters use receipt dedupe (**depends on effect layer**).

---

## 5. Spend gate at lease time — **WARN**

### Evidence

```1458:1466:engine/boss_engine/services/jobs.py
    def _requires_reservation(self, kind: str, payload: dict[str, Any]) -> bool:
        spendy_kinds = {
            "openai.plan",
            "github.pull_request",
            "software_factory.propose_pr",
            "comms.outbox.deliver",
            "infra.droplet.create",
        }
        return bool(payload.get("requires_reservation")) or kind in spendy_kinds
```

Missing active reservation → job **cancelled** (not silent) + blocking decision `budget_missing_for_job` + `job.cancelled_budget_blocked` (`_cancel_budget_blocked_job`).

Orange/red budget states defer jobs and create/reuse policy decisions (`_budget_state_policy_for_job`).

Model gateway reserves separately (`model_gateway.py` ties `budget_reservation_id` to model leases).

Code tasks reserve in service layer (`code_task_service` / `governed_code_task_service`) but **`code.task` is not in `spendy_kinds`** — lease will not cancel if reservation omitted from payload.

### Hard-rule assessment

| Rule | Status |
|---|---|
| Policy `spend.require_budget_reservation` | **Partial** — allowlist + flag, not universal |
| Visible failure when missing reservation | **PASS** for gated kinds |
| Model spend | **PASS** via gateway (separate path) |

### Gaps / GA risks

- **Incomplete kind matrix:** `code.task`, `agent.studio.execute`, deploy jobs, generic agent model jobs may spend without lease-time reservation assert if callers forget.
- GA fix: derive spendy kinds from SpecPack/policy or default-deny for any job payload with estimated cost / model invocation.

---

## 6. Decisions inbox — **PASS** (expiry side effects WARN)

### Evidence

- Service: `DecisionService.transition` — actions `approve|reject|revise|delegate|execute` with invalid-transition errors; deadline check; member `require_decision_actor` (except `SYSTEM_ACTOR` policy auto-approve).
- Follow-ons on approve: tool execute, agent/code resume, infra, software factory, etc. (`_handle_approved_follow_on`).
- Inbox API: `GET /v1/decisions/inbox`, digests, per-action POST routes; principal binding tested (`test_api_key_principal_binding_applies_to_decision_transitions`).
- Expire: `expire_overdue` sets `expired` + `decision.expired` with `safe_default_applied` field **recorded but not executed** as approve/reject side effects.

Statuses: `pending` / `delegated` → `approved` | `rejected` | `revision_requested` | `expired`; `approved` → `executed`.

Tests: extensive transition coverage in `test_api.py` (~11649+), workflow decision pause/resume (`test_timer_decision_pause_resume_and_binding_fail_closed`).

### Gaps / GA risks

- **Expiry does not apply `default_action` mechanically** (only documents it). Operators expecting auto-hold side effects must poll expiry + act.
- Risk-tier approval matrix in YAML is enforced via members/roles path, not by loading `default_safety.yaml` at transition time (policy drift risk).

---

## 7. Receipts & dedupe — **WARN**

### Evidence

- Schema: `receipts UNIQUE(org_id, dedupe_key)`; statuses `in_progress` → `succeeded`/`failed` via `mark_succeeded` / `mark_failed`.
- `ReceiptService.create_or_reuse`: SELECT then INSERT if missing; emits `receipt.created`.
- Consume path requires receipt in same `project_id`.
- Canary: `_canary_exactly_once` sequential reuse.
- Adapter tests: skill executors prevent duplicate GitHub effect with in-progress receipt; many provider paths receipted.

### Hard-rule assessment

| Rule | Status |
|---|---|
| `require_receipt` / `require_dedupe_key` | Enforced at adapter/service boundaries (not a global interceptor) |
| Exactly-once under concurrency | **WARN** — no `INSERT … ON CONFLICT DO NOTHING RETURNING`; concurrent creators can race (second hits unique violation → error, not clean reuse) |

### Gaps / GA risks

1. Make `create_or_reuse` conflict-safe (upsert) for true exactly-once under dual workers.
2. `GET /v1/receipts/{dedupe_key}` uses `settings.default_org_id` only — multi-org host would mis-scope (**tenant WARN**).
3. No automatic enforcement that every external adapter call goes through ReceiptService (discipline + review checklist).

---

## 8. Deadletters & no silent drops — **PASS**

### Evidence

- Job terminal fail always inserts `deadletters` + `job.deadlettered` event; related outbox/webhook/code_task marked.
- `DeadletterRemediationService`: incident packet + task (`no silent drops` invariant text in packet); job requeue from `deadlettered`/`failed_final` only; idempotent requeue/incident.
- API: list, create incident, requeue with `require_project_actor`.
- Resource plane / webhooks / github paths create DLQ on failure (multiple API tests).
- Anomaly detector flags retry storms from deadlettered job counts.
- Event dispatch failures: nested savepoint; business event preserved; `job.dispatch_failed` secondary event or structured log (`core.emit_event`).

### Gaps / GA risks

- Cancelled-for-budget jobs are not deadlettered (intentional cancel + decision) — OK if operators watch decisions.
- Burst budget deny without decision is the main “quiet” denial path (policy_eval only).

---

## 9. Events — **PASS**

### Evidence

- `emit_event` always inserts into `events` (edition port or Postgres).
- Dispatchable events go through `event_dispatch` in savepoint; failure isolated.
- Canary: `_canary_reactive_dispatch_exactly_once` — one job per subscription, replay reuses deduped job.
- Feed: `GET /v1/events?project_id=…` ordered DESC with limit/offset.

### Gaps / GA risks

- Feed endpoint does not call `require_project_actor` (read unauthenticated if API auth is weak/global) — see tenant section.
- High-volume heartbeat events may drown signal (ops).

---

## 10. Tenant / project scope — **WARN**

### Evidence (positive)

- Jobs, tasks, plans, reservations, cost_receipts, decisions, deadletters, events filtered by `project_id` on primary paths.
- `dispatch_plan` and parent/dep checks refuse cross-project IDs.
- Budget release/consume require reservation `project_id` match.
- Actor permissions via `require_project_actor` / API key principal binding on mutating control-plane routes (commands, decisions, budget consume, deadletter remediation).

### Evidence (gaps)

| Surface | Issue |
|---|---|
| `GET /v1/events` | No actor/permission check in handler |
| `GET /v1/deadletters` | No actor check on list |
| `GET /v1/receipts` | Project filter only; no actor |
| `GET /v1/receipts/{dedupe_key}` | Default org only |
| Job claim | Worker capability based; assumes worker is trusted (OK for single-tenant; multi-tenant workers need project allowlists) |

**GA:** Acceptable for single-tenant goldeneye dogfood; **not** multi-tenant SaaS ready without read-path authz and org-scoped receipt lookup.

---

## 11. Policy file vs runtime — **WARN**

`packages/specpack/policies/default_safety.yaml` states the hard rules. Runtime enforcement is **distributed** (BudgetGovernor, JobLeaseService, ReceiptService, adapters, model gateway) rather than a single policy evaluator loading this YAML for every effect.

Risk: YAML drift vs code allowlists (`spendy_kinds`, sandbox_real_mode flags).  
Mitigation today: canaries + review checklist in `AGENTS.md` + `policy_eval_log` on budget boundaries.

---

## State machine completeness matrix

| Machine | States | Explicit transitions | Terminal visibility | Grade |
|---|---|---|---|---|
| Job | queued, leased, running, failed_retryable, succeeded, failed_final, deadlettered, cancelled | Yes (service methods) | DLQ + events | **PASS** |
| Budget reservation | active, consumed, released, expired | Yes | Events | **PASS** |
| Receipt | in_progress, succeeded, failed | Yes | Events on create | **PASS** |
| Decision | pending, delegated, approved, rejected, revision_requested, expired, executed | Yes (`transition`) | Events | **PASS** |
| Deadletter | open, incident_created, requeued, … | Remediation methods | Events | **PASS** |
| Task | open, completed, archived (+ free text) | Partial | Events partial | **WARN** |
| Plan | draft, dispatched | Partial | Events | **PASS** |
| Decision default on expiry | recorded only | No effect execution | Event only | **WARN** |

---

## Test coverage map

| Area | Coverage | Key artifacts |
|---|---|---|
| Budget reserve/consume/release/forecast | Strong API | `test_api.py` budget tests |
| Lease lifecycle | Strong worker + canary | `test_worker.py`, `canaries._canary_job_lifecycle` |
| Lease reclaim / DLQ | Strong workflow + worker | `test_workflow_runtime.py`, worker fail path |
| Decision transitions | Strong API | `test_api.py` approve/reject/delegate/revise |
| Receipts exactly-once | Sequential canary + adapter tests | `canaries`, `test_skill_executors.py` |
| Concurrent receipt race | **Missing** | — |
| Full spendy job-kind matrix | **Missing** | Only partial kinds in `_requires_reservation` |
| Task FSM | **Weak** | archive + complete side paths |
| Event dispatch exactly-once | Canary | `_canary_reactive_dispatch_exactly_once` |
| Tenant isolation multi-project | **Weak** | Project-scoped unit tests; no adversarial cross-tenant suite |

---

## Hard rules checklist (AGENTS.md / default_safety)

| Hard rule | Kernel status | Grade |
|---|---|---|
| External effect → Receipt + dedupe_key | Pattern established; not mechanically global | **WARN** |
| Spendy action → BudgetReservation | Reserve/consume solid; lease allowlist incomplete | **WARN** |
| Worker job → lease + TTL + heartbeat | Implemented + tested | **PASS** |
| No silent drops (retry / DLQ / decision) | Primary paths yes; burst deny soft | **PASS** |
| Failures visible in events | Consistent `emit_event` usage | **PASS** |
| No autonomous self-apply | Decisions/PRs only for self-improvement (elsewhere) | N/A (panel scope) |

---

## Top GA risks (ordered)

1. **Spend-gate incompleteness** — new/expensive job kinds can skip lease-time reservation if not on allowlist and payload omits flag.  
2. **Receipt concurrent create race** — unique constraint without upsert reuse path under dual writers.  
3. **Lease reclaim + non-idempotent effects** — requires universal receipt dedupe at every adapter (kernel cannot alone guarantee).  
4. **Read-path authz / multi-tenant receipt org binding** — fine for dogfood, fail commercial multi-tenant.  
5. **Task status contract missing** — operators/automation cannot rely on a single task FSM.  
6. **Decision expiry default_action not applied** — deadlines may leave work stuck without follow-on.  
7. **cap=0 / missing policy = unlimited spend** — mis-seeded project can bypass financial control.  
8. **Command non-idempotent propose** — double CEO submit → duplicate work graph.

---

## Recommended pre-GA actions (kernel-only)

| Priority | Action | Outcome |
|---|---|---|
| P0 | Expand `_requires_reservation` to policy-driven or default-on for any job with cost/model/provider effect metadata; add matrix tests | Closes unreserved spend hole |
| P0 | `create_or_reuse` → `INSERT … ON CONFLICT (org_id, dedupe_key) DO NOTHING` + re-select | Concurrent exactly-once |
| P1 | Require non-zero budget policy for projects that run spendy jobs (deny reserve when unknown) | Fail-closed finance |
| P1 | Authz on events/deadletters/receipts list; receipt get by org from project | Tenant readiness |
| P2 | Task status enum + transition helper + tests | Operator contract |
| P2 | On decision expiry, optionally enqueue safe-default follow-on or explicit escalation job | No stuck holds |
| P2 | Command propose idempotency key (hash of command+constraints) | No duplicate graphs |

---

## Code evidence index

| Topic | Path |
|---|---|
| Policy rules | `packages/specpack/policies/default_safety.yaml` |
| Budget Q/R/C/R | `engine/boss_engine/services/budget.py` |
| Jobs/leases | `engine/boss_engine/services/jobs.py` |
| Receipts | `engine/boss_engine/services/receipts.py` |
| Decisions | `engine/boss_engine/services/decisions.py` |
| Deadletters | `engine/boss_engine/services/deadletters.py` |
| Commands/plans | `engine/boss_engine/services/intake.py` |
| Events | `engine/boss_engine/services/core.py` (`emit_event`) |
| Canaries | `engine/boss_engine/services/canaries.py` |
| Schema | `db/schema.sql` (jobs, receipts, budget_reservations, decisions, deadletters, events, tasks, plans) |
| API routes | `apps/api/app/main.py` (`/v1/commands`, `/v1/budget/*`, `/v1/decisions/*`, `/v1/jobs`, `/v1/events`, `/v1/deadletters`, `/v1/receipts`) |
| Tests | `apps/api/tests/test_api.py`, `test_workflow_runtime.py`, `apps/worker/tests/test_worker.py` |

---

## Panel sign-off

| Field | Value |
|---|---|
| Panel | 02 Control Plane Kernel |
| Overall | **WARN** (conditional GO for single-tenant dogfood) |
| Blocking for multi-tenant commercial GA | Spend-kind matrix, receipt upsert race, read authz, cap=0 fail-closed |
| Non-blocking for dogfood GA | Task FSM polish, decision expiry side effects, command idempotency |
| Author | Control Plane Kernel Expert Panel (code assessment 2026-08-12) |

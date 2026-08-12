# Panel 12 — Architecture Hard-Rules Compliance (GA spot-check)

**Date:** 2026-08-12  
**Scope:** AGENTS.md hard rules — adapters, receipts/dedupe, BudgetReservation, worker leases, ChangeSet/ResourceReceipt, no silent fails, no autonomous self-mod, OpenClaw not foundation  
**Method:** Code search + targeted reads in `engine/boss_engine/services/`, `apps/worker/`, `packages/specpack/`, `sandbox-host/`, schema/policies. Not a full formal proof of every call site.  
**Overall GA posture:** **Partial** — kernel paths are strongly gated; several peripheral / guest / best-effort surfaces still bypass or soft-fail hard rules.

---

## Verdict matrix

| # | Hard rule | Verdict | Notes |
|---|-----------|---------|--------|
| 1 | No direct external billing/infra/model/GitHub/Slack without policy-guarded adapter | **Partial** | Adapters exist and dominate; some raw HTTP outside adapter plugins |
| 2 | Every external effect → `Receipt` + unique `dedupe_key` | **Partial** | Core write paths use receipts; not universal on all egress |
| 3 | Every spendy action → `BudgetReservation` | **Partial** | Governor + skill/gateway/code-task strong; OSS noop + some skill policy exceptions |
| 4 | Every worker job → lease + TTL + heartbeat | **Enforced** | Claim/heartbeat/reclaim/runtime ceiling implemented |
| 5 | Every provider write → `ChangeSet` | **Enforced** | Apply path requires approved ChangeSet |
| 6 | Every provider action → `ResourceReceipt` | **Enforced** | Apply records per-op resource receipts with dedupe |
| 7 | Destructive / prod DNS-deploy → explicit approval + rollback plan | **Enforced** | Rollback required; dual-control + risk-tier approval gates |
| 8 | Provider adapters are plugins; Resource Plane Kernel is core | **Enforced** | Kernel in `resource_plane.py`; DO/AWS adapters as plugins |
| 9 | OpenClaw is not durable runtime/foundation | **Enforced** | Spec/policy only; no runtime dependency |
| 10 | No silent fails (retry / deadletter / escalate) | **Partial** | Worker fail/DLQ solid; intentional best-effort swallows remain |
| 11 | No autonomous self-modifying behavior (PR/config diffs only) | **Enforced** | Proposal → decision → canary → rollout with required evidence |

---

## 1. Adapter boundary (no direct external APIs)

**Verdict: Partial**

### Enforced / healthy

- Named adapters under `engine/boss_engine/services/`:  
  `github_adapter.py`, `github_app_adapter.py`, `digitalocean_adapter.py`, `openai_adapter.py`, `slack_adapter.py`, `mattermost_adapter.py`, `email_adapter.py`, `claude_cli_adapter.py`, `ssh_host_adapter.py`, `bitbucket_adapter.py`.
- Provider writes for infra funnel through `ResourcePlaneService` + adapter plugins (`DigitalOceanAdapter`, AWS client via `_aws_client`), not worker-side raw DO calls.
- `DigitalOceanAdapter` fails closed when `BOSS_INFRA_REAL_PROVISION_ALLOWED` is false or token missing.
- Model gateway design: brokers *authority* only; credentials resolved inside policy-checked adapter process (`model_gateway.py` header comment).
- Worker job execution calls control-plane HTTP (`apps/worker/boss_worker/control_plane.py`), not provider APIs directly for GitHub/infra/model job kinds.

### Gaps / risk samples

| Surface | Pattern | Risk |
|---------|---------|------|
| `oncall_notify.py` | Raw `urllib.request` POST to operator webhook | External effect without adapter + no receipt |
| `telemetry_connectors.py` | Raw urllib GET/POST to operator URLs | Observability egress outside adapter inventory |
| `deployment_connectors.py` / `preview_service.py` | urllib/httpx health and probe traffic | Acceptable as probes; not receipt-gated |
| `sandbox-host/guest/in_guest_agent.py` | Direct OpenAI/Azure chat HTTP in guest | By design (microVM boundary); must remain host-governed only |
| `resource_plane.py` credential/billing probe paths | Direct `https://api.digitalocean.com`, `api.openai.com`, `api.anthropic.com` | Inside kernel service, not separate plugin for all providers |
| `commercial.py` | urllib to billing/portal providers | Adapter-ish commercial path; treat as controlled but not fully pluginized |
| `plane_webhooks.py` / `secret_backends.py` | urllib to Plane / ops secret store | Platform ops, not product external effect — document as allowed |

**GA ask:** Inventory all non-`*_adapter.py` network egress from `engine/` and tag each as (a) adapter plugin, (b) control-plane internal, or (c) exception with receipt policy.

---

## 2. Receipt + `dedupe_key` for external effects

**Verdict: Partial**

### Enforced

- Schema: `receipts` has `UNIQUE(org_id, dedupe_key)` (`db/schema.sql`); resource receipts, jobs, model gateway leases, etc. also key on dedupe.
- `ReceiptService.create_or_reuse` is the canonical idempotent create path.
- Receipt-gated flows observed in:  
  `software_factory` (openai plan + PR), `skill_executors`, `webhooks`, `github_branches` / `github_merges`, `infrastructure`, `commercial`, `code_task_service` settlement, `model_gateway`, ChangeSet → resource receipt chain.
- Spec policy: `packages/specpack/policies/default_safety.yaml` → `external_effects.require_receipt` / `require_dedupe_key: true`.

### Gaps

- **Oncall notify** posts outbound cards with no `Receipt` (`oncall_notify.notify_incident` returns bool; never raises; no dedupe).
- **Skill policy exception:** `packages/specpack/skills/github.yaml` — `github.create_pr` (and similar) set `requires_budget_reservation: false` while still requiring receipt — budget exception is intentional; confirm runtime enforces receipt even when budget is skipped.
- **Model gateway skill** (`model_gateway.yaml`) has `requires_budget_reservation: false` on some ops that are lease-settled rather than pre-reserved at skill layer — settlement still creates receipts when paid path is used.
- Telemetry write failures are intentionally non-fatal (`llm_telemetry.py`) — no receipt, by design for side-channel metrics.
- Guest sandbox LLM calls produce usage inside the guest; durable BOSS receipt is expected on the host job path that launched the guest — dual-path risk if host fails mid-flight.

**GA ask:** Require receipt+dedupe on any operator webhook / Slack-like notify that can leave BOSS (or mark explicitly out-of-band “best-effort notify” in policy).

---

## 3. BudgetReservation before spend

**Verdict: Partial**

### Enforced

- `BudgetGovernor.reserve` enforces cap, burst, tier-2 freeze (`freeze_tier2_pct`), denial → blocking decision + events (`budget.py`).
- Skill runtime cost gate: real mode always `budget.reserve` before executor (`skill_runtime.py`).
- Model gateway: paid sources require positive estimate + reservation; free sources skip paid reserve but still job-lease gated (`model_gateway.py`).
- Code tasks / governed code tasks: reserve at start, settle/release with receipt linkage (`code_task_service.py`, `governed_code_task_service.py`); PR step renews expired reservation rather than silent continue.
- Provider ChangeSet apply: if `expected_cost_usd_micros > 0`, `assert_active_reservation` or block with decision (`resource_plane.apply_changeset`).
- Software factory OpenAI plan: `assert_active_reservation` before model call + receipt.
- Infra profiles: `require_budget_reservation_for_write: true` in deploy env profiles.

### Gaps

| Item | Detail |
|------|--------|
| `NoopBudgetPort` | OSS/unmetered port always returns `status: reserved` — preserves order but **does not meter**. Fine for Community; GA Cloud/Enterprise must not ship this as the live governor. |
| Skill YAML exceptions | `github.*` create_pr: `requires_budget_reservation: false` (tool category; still receipt-gated). |
| `agent_loop` itself | Does not call `budget.reserve`; relies on caller (`software_factory`) holding reservation. Direct future callers could bypass. |
| `OpenAIAdapter` / `NativeCodeAgent` | Pure transport; spend governance is caller-owned. Code-task path reserves; any ad-hoc adapter use is a bypass risk. |
| Guest `in_guest_agent` | Spend happens in guest with host credentials; host reservation must cover full guest loop (trust boundary). |

**GA ask:** Static check that every production entrypoint that constructs `OpenAIAdapter` / `make_code_agent` / skill real-mode is preceded by active reservation (or gateway lease).

---

## 4. Worker job lease + TTL + heartbeat

**Verdict: Enforced**

Evidence:

- Schema columns: `lease_owner`, `lease_token_hash`, `lease_expires_at`, `heartbeat_at` on jobs.
- `JobLeaseService.claim_job`: `FOR UPDATE SKIP LOCKED`, TTL, reclaim of expired leased/running jobs, budget-state deferral before claim.
- Worker: poll → heartbeat → background renew at `lease_ttl/2`, **bounded** by `job_max_runtime_seconds` (stops renewing hung jobs).
- Lease loss: HTTP 409 / Conflict treated as lease loss (not swallowed as transient) — addresses historical dual-execution incident (`runner.py` comments; `tests/test_run_fencing.py`, `test_decision_layer.py`).
- Fail path: `client.fail` with retryable flag + exponential backoff or deadletter.
- Model capacity leases are a **second** lease layer requiring an active job lease first.

Residual risk: heartbeat client close uses bare `except: pass` (cleanup only). Health sweep on idle swallows all errors (documented intentional).

---

## 5–7. ChangeSet, ResourceReceipt, destructive/prod controls

**Verdict: Enforced**

Evidence:

- Apply requires `status == approved` else block event + `ValueError`.
- Costly apply requires active `BudgetReservation`.
- Per-operation `_record_resource_action_attempt` → `resource_receipts` with operation dedupe keys (`resource_plane:…`).
- Destructive / production_affecting operations **require non-empty rollback_plan** at normalize time.
- `_operations_need_approval` for destructive, production_affecting, or risk_tier ≥ 2.
- Dual-control approvers derived from skill requirements; approvals accumulate until complete.
- Account flags: `read_only`, `destructive_actions_disabled` block writes.
- Deploy profiles: `require_resource_receipt`, ChangeSet smoke readiness in `check_external_rollout_readiness.py`.
- Spec: `resource_plane_policy.yaml` / `default_safety.yaml` encode changeset + resource receipt + approval requirements.

Residual risk: not all “provider-like” actions outside Resource Plane (e.g. GitHub PR/merge) use ChangeSet — they use receipt + approval decision paths instead. That is by product design (software factory vs ProviderOps), not a silent bypass, but operators must not assume every write is a ChangeSet.

---

## 8. Adapters as plugins; Resource Plane as core

**Verdict: Enforced**

- Kernel: `ResourcePlaneService` owns inventory, accounts, ChangeSets, receipts, deploy/rollback.
- Plugins: DO adapter class; AWS via internal client factory; planned providers marked `adapter_status: planned` / `pending_adapter` and cannot claim live write capability (`test_provider_credentials.py`).
- Spec: `provider_adapters_are_plugins: true`, `resource_plane_kernel_is_core: true`, `openclaw_not_foundation: true`.

---

## 9. OpenClaw not foundation

**Verdict: Enforced**

- Documented in PRD/TECHNICAL_SPEC v1.1 & v2.0, AGENTS.md, `docs/features/implemented.md` ARCH-001.
- Policy flags only; **no** runtime package import or durable state dependency on OpenClaw found in application code (grep: docs/spec only).

---

## 10. Visible failures (retry / deadletter / escalate)

**Verdict: Partial**

### Enforced

- Worker `execute_job` failures → `fail` with retry or deadletter; unit tests for deadletter paths and “next_move failure is visible not silent”.
- `DeadletterRemediationService` creates incident packets with explicit invariant text: “No silent drops…”.
- Comms: outbox-only policy in default_safety; webhook delivery retries with durable state.
- Run fencing / lease reclaim to avoid silent dual writes.
- Skill runtime records failed status + releases reservation on reconciliation failure rather than inventing cost receipts.

### Intentional / residual soft-fails

| Location | Behavior | Assessment |
|----------|----------|------------|
| `oncall_notify` | Catch-all → `False` | Best-effort notify; can hide broken oncall routing |
| `_sweep_deployment_health` | Swallow all | Documented; monitoring may go stale without DLQ |
| `llm_telemetry` | Swallow write errors | Correct for non-authoritative telemetry |
| Worker main outer loops | Broad `except Exception` | Must ensure each path logs + escalates (spot-check: logging present on job fail) |

**GA ask:** Promote oncall notify failure to event/metric (not necessarily hard-fail intake); alert when health sweep fails N times.

---

## 11. No autonomous self-modification

**Verdict: Enforced**

- `SelfImprovementService.create_proposal` requires non-empty test_plan, canary_scope, rollback_plan, success_metrics, change_ref.
- Creates task + artifact + **blocking decision** for canary; transitions gated by decision approval.
- Policy: `self_improvement.apply_directly: false`, `require_pr: true`, `require_tests: true`, `require_rollback_plan: true`.
- Software factory merge recommendation explicitly “no auto-merge permitted” for go path.
- GitHub community surfaces documented as never auto-merge by default (README).

Residual: autonomy ladder / earned auto-merge is product vision for coding agents — not observed as unguarded self-apply of BOSS core.

---

## Bypass pattern grep samples (2026-08-12)

```text
# Raw HTTP outside *adapter* naming (engine)
httpx.|urllib.request  → oncall_notify, telemetry_connectors, deployment_connectors,
                         preview_service, plane_webhooks, secret_backends, commercial,
                         resource_plane probes, software_factory, sandbox_console, webhooks

# Spend surfaces
budget.reserve / assert_active_reservation  → budget.py, skill_runtime, model_gateway,
  software_factory, code_task*, docs_agent, resource_plane, intake
OpenAIAdapter / make_chat_adapter          → transport-only; caller must reserve

# Lease
claim_job / heartbeat / require_active_lease → jobs.py, model_gateway, worker runner

# Provider write control
apply_changeset + resource_receipts        → resource_plane.py
requires_budget_reservation: false         → github.yaml, model_gateway.yaml (skill policy)

# OpenClaw
openclaw                                    → docs/spec/policy only
```

---

## Tests that lock rules (non-exhaustive)

- Worker: `apps/worker/tests/test_worker.py` — lease cycle, deadletter, retry, visible failures.
- API: `test_aws_changeset_flow.py`, `test_provider_credentials.py`, `test_orchestration_agent.py` (budget+receipt before start).
- Engine: `test_run_fencing.py`, `test_duplicate_suppression.py`, canaries `budget_reserve_release`.
- Policy contracts: `test_assurance_specpack_contracts.py` (`resource_receipt_required`, changeset binding).

---

## GA blockers vs follow-ups

### Soft blockers (should track for Cloud/Enterprise GA)

1. **NoopBudgetPort / unmetered path** must not be the production BudgetPort for metered editions.  
2. **Outbound best-effort webhooks** (`oncall_notify`, similar) without receipts violate literal hard rule for “every external effect”.  
3. **Caller-owned budget on OpenAIAdapter / agent_loop / guest agent** — missing static enforcement → residual spend bypass risk.

### Non-blockers (documented design)

- GitHub software-factory writes use Receipt + Decision, not ChangeSet (ProviderOps domain).  
- Probe/health HTTP without receipts.  
- Telemetry non-fatal writes.  
- OpenClaw absence is complete.

---

## Panel verdict summary

| Area | Verdict |
|------|---------|
| Worker leases | **Enforced** |
| ChangeSet / ResourceReceipt / destructive controls | **Enforced** |
| OpenClaw not foundation | **Enforced** |
| Self-improvement no direct apply | **Enforced** |
| Adapter boundary | **Partial** |
| Receipts + dedupe on all external effects | **Partial** |
| BudgetReservation on all spend | **Partial** |
| No silent failures | **Partial** |

**Composite panel score:** **Partial** — suitable for dogfood / sandbox GA only if Cloud metering uses real `BudgetGovernor`, guest spend is host-reserved, and best-effort egress is policy-labeled. Not “fully enforced everywhere” under a strict reading of AGENTS.md.

---

## 15-line executive summary

1. **Worker leases + heartbeat + reclaim + runtime ceiling:** Enforced; dual-execution lease-loss handling present.  
2. **ProviderOps ChangeSet apply + ResourceReceipt + rollback + dual approval:** Enforced in `resource_plane.py`.  
3. **OpenClaw:** Spec/policy only; not a runtime foundation — Enforced.  
4. **Self-improvement:** Proposal/decision/canary/rollout evidence required — Enforced (no direct self-apply).  
5. **BudgetGovernor** reserves/consumes with caps, burst, tier-2 freeze — Enforced on core paths.  
6. **Partial:** OSS `NoopBudgetPort` always reserves (unmetered); must not ship as Cloud governor.  
7. **Partial:** Skill YAML allows some GitHub/model skills without budget reservation.  
8. **Partial:** `OpenAIAdapter`/`agent_loop`/guest agent are transport/caller-owned for budget.  
9. **Partial:** Receipts cover software factory, skills, gateway, infra, webhooks — not oncall raw POST.  
10. **Partial:** Several raw urllib/httpx call sites sit outside named `*_adapter` plugins.  
11. **Partial:** Silent/best-effort swallows remain (oncall, health sweep, telemetry).  
12. **DigitalOcean/AWS writes** go through Resource Plane + flags (`infra_real_provision_allowed`, account read-only).  
13. **Model gateway** correctly stacks job lease → capacity lease → paid budget → receipt settlement.  
14. **Policy YAML** matches AGENTS hard rules; runtime is stronger on ProviderOps than on peripheral notify/telemetry.  
15. **GA recommendation:** Treat hard-rules compliance as **Partial** — close budget-port edition split, receipt external notify, and adapter-egress inventory before claiming full AGENTS hard-rule enforcement.

# BOSS — Gap Analysis vs. Target Spec (PRD + Tech Spec v1)

Date: 2026-06-21. Source spec: BOSS PRD + Technical Specification v1 Draft (May 15, 2026).
Verdict: **the hard foundations are built (~75% overall); remaining work is breadth, the
"earned-autonomy / self-improvement" loops, the safety moat, and proof (canary/test suite).**

## Coverage scorecard

| Domain | Coverage | State |
|---|---|---|
| Core primitives + execution engine + exactly-once | **~95%** | objects, durable job queue (state machine, SKIP-LOCKED leasing, TTL+heartbeat, reclaim, bounded retries, deadletters), receipts+dedupe — all live |
| Policy + approval + budget/FinOps + elasticity | **~80%** | budget reserve→settle, hierarchical budgets, risk-tier approvals, break-glass, worker pools, autoscaler — live; gaps in anomaly detection + unified policy eval |
| Comms (outbox/inbox) + meetings + Metatron | **~90%** | delivery backbone, meeting phase-machine, Metatron — live; Phase 3 added inbox ack-ledger (no silent drops), email command contracts, Slack dept/persona/digest, and meeting vote/quorum/timeout/stall-sweep |
| Router + skills + autonomy + self-improvement + factory | **~80%** | router (budget-pressure modes), 16 typed skills, autonomy+trust, governed software factory — live; self-improvement runtime (auto-canary + staged rollout + auto-rollback), eval-harness, and evidence-based autonomy auto-upgrade/downgrade now live (Phase 2); skill-invocation mock mode remains |
| UX + security + observability + public API | **~70%** | 10/10 MVP API endpoints, console, audit export+replay, break-glass — live; gaps in CEO command palette, PII redaction, tracing, dashboards |
| **Overall** | **~75%** | foundations done; the gap is the trust/autonomy/proof layer |

## What's already built (don't rebuild)

Durable job engine with leasing/heartbeat/reclaim/retries/deadletters · exactly-once receipts +
dedupe keys + provider idempotency · canonical objects (task/plan/decision/policy/meeting/role/
KPI/health) · risk-tier approval model + decision deadlines/defaults + break-glass · budget
quote→reserve→execute→receipt→reconcile, hierarchical budgets, GREEN/YELLOW/ORANGE/RED states ·
worker pools + autoscaler · model router with budget-pressure routing · 16 typed skill specs ·
autonomy levels + trust scores + auto-downgrade · governed software factory (role-separated
pipeline + fail-closed quality gates + review packet) · outbox/inbox delivery + Metatron + meeting
consensus engine · 10/10 MVP API endpoints · audit export + replay bundles.

---

## Gap backlog (everything still to implement)

### A. Proof & MVP-completion (small, high-leverage)
- **Budget intensity slider** — `POST /v1/budget/set_intensity` + CEO slider UI mapping 0–100 → caps/concurrency/model tiers (the spec's signature CEO control; reserve/settle exist, the *slider* doesn't).
- **Canary suite (nightly):** e2e smoke, exactly-once smoke, no-drop inbox/outbox smoke, software-factory smoke, router-fallback smoke. These *prove* the non-negotiables; today only DB-reachability smoke exists.
- **CEO outcome command palette** — semantic intents ("ship staging", "increase budget", "hire agents") → routed to commands. Today Cmd+K is area-navigation only.
- **Plan→job orchestration** — plans exist as data; wire plan phases to job creation/sequencing.
- **Rollback UI** — rollback endpoints exist; surface buttons in the change-journal/diff viewer.

### B. Safety & trust moat (enterprise gate)
- **Systematic PII sanitizer/redaction** before every external model/tool call (today: sandbox token-masking only).
- **Data-boundary enforcement** — local-only tasks pinned to local runner; cloud sees redacted data (field exists, enforcement doesn't).
- **Unified PolicyEvaluator + `policy_eval_log` table** — one evaluator all boundaries call, with a durable audit trail of every policy decision + reason.
- **Distributed tracing** — trace/span IDs linking job → model call → tool call → receipt → artifact; surface in run viewer + audit export.
- **Break-glass mandatory-postmortem enforcement** (field exists; not enforced).

### C. Earned-autonomy & controlled self-improvement loops (the differentiator)
- **Self-improvement runtime** — observe → propose → test → canary → rollout → **auto-rollback** (currently schema + proposal only; no canary runner, no staged rollout, no rollback trigger).
- **Eval-harness regression suites** — run success_metrics for routing/policy/factory quality; gate rollouts.
- **Evidence-based autonomy auto-upgrade** engine (success-rate/no-violations thresholds → propose upgrade decision) + regression auto-downgrade.
- **Router fallback canary tester** (validate fallback chains before incidents) + live provider-health polling + router rationale as a structured artifact.
- **Cost anomaly detection** — token spikes, retry storms, prompt bloat, unexpected provisioning → auto lite-mode / freeze / downgrade with decision/incident artifacts; **burst-cap enforcement** at reserve().
- **Skill-invocation framework** — runtime that enforces per-skill retry/cost/permission + **mock mode** (needed for canaries without real side effects).

### D. Comms robustness ("no silent drops" at scale)
- **Inbox state completeness** — PARSED/ROUTED states + dedicated `ack_state` table.
- **Email command contracts** — structured subject tokens (org/workspace/project) + thread IDs + deterministic malformed-command rejection with remediation.
- **Slack** — per-department channels, persona prefix/signature rendering, **digest batching**.
- **Meeting consensus completeness** — explicit **vote** phase, quorum/default rules, per-phase tool budgets, phase **timeouts** (stall prevention).

### E. Software-factory scale-up
- **WIP limits** per project + queue-depth gating; throughput scales only when validation/CI keeps up.
- **Explicit lint stage** + external **CI-green** integration in the gate.
- **Confidence-gated merge** (reject low-confidence pre-merge) + optional **second-model cross-check** + review-packet stored as an artifact.
- **Spec→chunk planner** (small chunked PRs from a plan, not one task → one PR).

### F. Observability, testing depth, enterprise
- **Real-time dashboards** — queue/lease/outbox/provider health, canary status, spend/run-rate; WebSocket/long-poll (today: 7s refresh).
- **Load + chaos testing** infrastructure (worker-crash, provider-flake, lease-reclaim injection).
- **InfraCommitment reconciler** service (schema exists; no reconciler) + **tier2+ initiative budget approval** flow.
- **Enterprise:** RBAC expansion, SSO, SOC2 evidence collection, sector packs.

---

## Prioritized, phase-wise plan (MVP-first)

### Phase 0 — Complete & *prove* the MVP v0 loop  ✅ DONE (branch `feat/gap-phase0-mvp`)
Goal: the dogfooding loop is demonstrably end-to-end with the guarantees proven.
1. ✅ Budget intensity slider — `POST /v1/budget/set_intensity` (0–100 → caps/concurrency/tiers) + console slider. Validated.
2. ✅ Canary suite — `exactly_once` / `no_drop_outbox` / `job_lifecycle` / `budget_reserve_release` (4 pass) + software-factory/router-fallback declared-skipped; `POST /v1/canaries/run`, persisted to `canary_runs`.
3. ✅ CEO outcome commands — semantic intent in the composer ("set intensity to 70" acts immediately; else → governed command pipeline).
4. ✅ Plan→job orchestration — `dispatch_plan` turns phases into sequenced child tasks; `POST /v1/plans/{id}/dispatch`. Validated (Design→Build→Ship chain).
5. ✅ Rollback surfaced on change-journal rows (deep-links to working rollback controls).

### Phase 1 — Safety & trust moat  ✅ COMPLETE (branch `feat/gap-phase0-mvp`)
1. ✅ **PII sanitizer/redaction** — `redaction.py` + opt-in `redact=` on the model adapter.
2. ✅ **Policy-eval audit trail** — `policy_eval_log` + `record_policy_eval` + budget-gate wiring + `GET /v1/policy-evals`.
3. ✅ **Burst-cap enforcement** — `$/min` cap enforced at `reserve()` with deny + policy-eval record.
4. ✅ **Cost-anomaly detection + auto-lite** — `anomaly_detector.scan` (retry storms, spend spikes) → incident Decision + auto-lite-mode; `POST /v1/anomalies/scan`.
5. ✅ **Break-glass postmortem** — already auto-created per grant (was overstated as a gap).
6. ✅ **Data-boundary enforcement** — restricted/confidential tasks redact PII before every external model call + `[data_boundary]` policy eval.
7. ✅ **Distributed tracing** — `GET /v1/trace?run_id` reconstructs a run's full timeline (run + gate + every referencing event).

### Phase 2 — Earned autonomy & controlled self-improvement  (the moat / the "BOSS improves BOSS" story)  ✅ COMPLETE (branch `feat/gap-phase2-autonomy`)
1. ✅ **Self-improvement runtime** — `run_canary` auto-runs the canary + eval-harness suites and
   records the outcome (no human reports it); staged rollout (`start`/`advance`, ship-narrow→widen)
   with **auto-rollback** when an observation breaches the thresholds. `rollout_state` JSONB +
   `POST /v1/self-improvement/proposals/{id}/run-canary` · `…/rollout/start` · `…/rollout/advance`.
   Validated end-to-end (auto-canary→rollout→completed, and degraded→auto-rollback→409).
2. ✅ **Eval-harness regression suites** — `eval_harness.py` (routing_fallback_chain /
   policy_audit_roundtrip / factory_gate_fail_closed / autonomy_ladder_monotonic), persisted to
   `eval_runs`; `POST /v1/evals/run` + `GET /v1/evals`. Gates self-improvement rollouts.
3. ✅ **Evidence-based autonomy auto-upgrade/downgrade** — `autonomy_engine.py` gathers a role's
   allowed/denied/scope-violation events since its last review and feeds them through the existing
   governance review engine → clean evidence auto-proposes an upgrade decision; a violation
   auto-downgrades. `POST /v1/roles/{role_key}/autonomy/evaluate`. Both paths validated.
4. ✅ **Router fallback canary + provider-health polling + rationale artifact** — `router_health.py`:
   `poll_provider_health` snapshots every model lane into `provider_health` (`POST /v1/provider-health/poll`,
   visible via existing `GET /v1/provider-health`); `record_rationale` persists the selected lane +
   ranked fallback chain + rejected sources as a `router_rationale` artifact (`POST /v1/router/rationale`);
   the `router_fallback` canary (now live, not skipped) proves the chain is coherent before an incident.
5. ✅ **Skill-invocation framework + mock mode** — `skill_runtime.py` enforces per-skill
   permission (allowed_callers) + cost (budget reserve in the skill's category) + retry
   (retry.max_attempts), recording each call to `skill_invocations`. **Mock mode** runs the full
   gauntlet with zero side effects (no reservation, deterministic echo) — `POST /v1/skills/{id}/invoke`
   (API is mock-only; real invocation is worker-driven) + `GET /v1/skills/invocations`; a
   `skill_mock_invocation` canary proves the no-side-effect guarantee.

**Phase 2 complete.** Remaining router work (live provider API ping vs. config-derived health) and a
real ≥2-lane fallback exercise are deferred to when multiple model lanes are credentialed.

### Phase 3 — Comms robustness + meetings + factory scale  (multi-agent org at volume)  ✅ COMPLETE (branch `feat/gap-phase3-comms`)
1. ✅ **Comms robustness** (workstream D) —
   - Inbox lifecycle `received → parsed → routed → actioned | deadlettered | acknowledged` + a durable
     `ack_state` ledger (one row per inbound, transition history, terminal flag) — the "no silent drops"
     proof: `GET /v1/comms/inbox/ack-state?pending_only=` surfaces anything not yet terminal.
   - Email command contracts — structured subject tokens `[org:][ws:][proj:][thread:]`, deterministic
     thread IDs (stable across Re:/Fwd:), and malformed-command deadletter with a remediation message.
   - Slack — `department_channel` mapping, persona prefix/signature rendering (`POST /v1/comms/slack/render`),
     and digest batching that collapses many draft Slack messages into one per channel (`POST /v1/comms/slack/digest`).
2. ✅ **Meeting consensus completeness** (`meeting_consensus.py`) —
   - Explicit vote phase with quorum/threshold/default rules: `tally_votes` decides
     approved / rejected / no_quorum and emits dissent records; wired into `POST /v1/meetings/{id}/run`
     (supply `votes` → engine derives consensus/dissent; no-quorum → blocked) + `POST /v1/meetings/tally` preview.
   - Per-phase tool budgets + phase timeouts (pure config/helpers the phase-machine consults).
   - Stall prevention: `POST /v1/meetings/sweep-stalled` fails out runs past their phase timeout and
     opens an incident Decision for each — a silent stall becomes a visible, actionable item.
3. ✅ **Software-factory scale-up** (`factory_scale.py`) —
   - WIP limits + queue-depth gating: `GET /v1/factory/wip` admits a new code task only when an
     in-flight slot is free (throughput scales only when validation keeps up).
   - Explicit lint stage + CI-green in the fail-closed gate, a numeric merge-confidence score, an
     optional second-model cross-check, and a stored **review packet** artifact —
     `POST /v1/factory/merge-check` (preview) + `POST /v1/factory/quality-gate` (persist). A merge is
     allowed only when required gates pass AND confidence ≥ floor.
   - Spec→chunk planner: `POST /v1/factory/chunk-plan` splits a plan into small, independently
     shippable PR chunks (by items or by files) instead of one giant PR.

**Phase 3 complete.** The LLM/GitHub-bound execution paths (live meeting phase-machine, real PR
open/merge) remain gated on credentials; the consensus/gate/scale logic added here is pure +
DB-tested and composes with those paths.

### Phase 4 — Observability, testing depth, enterprise  (scale + GTM)
1. Real-time dashboards + streaming.
2. Load + chaos test infrastructure.
3. InfraCommitment reconciler + tier2+ initiative budget approval.
4. RBAC/SSO/SOC2 evidence + sector packs.

---

## Bottom line
BOSS already implements the spec's *hardest* non-negotiables — durable execution, exactly-once
effects, the budget/approval/policy governor, and a working governed software factory. The ~25%
gap is concentrated in (1) **proving** the guarantees (canary suite), (2) the **safety moat**
(PII/data-boundary/tracing), and (3) **closing the autonomy + self-improvement loops** — i.e. the
parts that turn "it works" into "it earns trust and improves itself." Phase 0 makes the MVP
demo-complete; Phases 1–2 build the moat.

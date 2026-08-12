# Panel 08 — Worker / Jobs / Reliability (GA)

**Date:** 2026-08-12  
**Panel:** Worker / Jobs / Reliability Expert  
**Scope:** lease/heartbeat, retries, visible failures, `code.task` DLQ risk, worker image skew (`git`), `agent.orchestrator.next_move`  
**Sources:** `apps/worker`, `engine/boss_engine/services/jobs.py`, `orchestrator_next_move.py`, `reliability.py`, `health.py`, `Dockerfile` worker stage, `docs/operations/LIVE_QA_2026-08-11.md`, `GOLDENEYE_DEPLOYMENT.md`, `PRD_CRITICAL_GAPS_2026-08-12.md`, `tests/test_runtime_image_contract.py`

---

## Executive verdict

| Area | GA grade | Notes |
|------|----------|--------|
| Lease claim + token fencing | **GREEN (primitive)** | Postgres `FOR UPDATE SKIP LOCKED`, token hash, reclaim of expired `leased`/`running` |
| Heartbeat + runtime ceiling | **GREEN (code)** | Mid-job renew every TTL/2; stop after `job_max_runtime_seconds` (default 5400s) |
| Retries / deadletter path | **GREEN (kernel)** | `failed_retryable` → backoff → `failed_final` → `deadletters` + events |
| Visible failures | **GREEN** | No silent drops for leased jobs; health risks include open DLQ |
| `code.task` reliability | **YELLOW → RED live** | High historical DLQ; non-retryable worker errors; substrate/model noise |
| Worker image / tag parity | **YELLOW** | Dockerfile fixed (`git`/`node`/`npm`); live still risked tag skew until redeploy |
| `next_move` orchestration | **GREEN (code)** / **YELLOW (docs lag)** | Capability + execute path present; older gap notes outdated |
| Reliability service (SRE) | **YELLOW** | Scan/incident/diagnose exist; not a substitute for DLQ ops discipline |

**GA readiness (worker plane):** **CONDITIONAL GO** for control-plane job kernel; **NO-GO for autonomous code labor** until worker image parity is proven live and `code.task` DLQ classes are reaped/fixed.

---

## 1. Lease and heartbeat

### Design (sound)

- Workers are stateless; claim via `POST /v1/worker/poll` → `JobLeaseService.claim_job`.
- Claim selects `queued` / `failed_retryable`, or expired `leased`/`running`, ordered reclaim-first, `FOR UPDATE SKIP LOCKED`.
- Lease fields: `lease_owner`, `lease_token_hash`, `lease_expires_at`, `heartbeat_at`; attempt increments on every claim (including reclaim).
- Heartbeat extends TTL and transitions `leased` → `running`.
- Worker defaults: `BOSS_LEASE_TTL_SECONDS=300`, `BOSS_JOB_MAX_RUNTIME_SECONDS=5400`.
- Mid-run renewal thread uses a **separate** HTTP client (main client may block for long `code.task` reads up to 3600s).

### Hardening already present

| Control | Implementation |
|---------|----------------|
| Bounded hang | Stop renewing after `job_max_runtime_seconds`; lease expires; reclaim |
| Dual-writer after reclaim | Heartbeat 409 / Conflict → `lease_lost`; stop renewing (observed incident `job_2fbe88e6`) |
| `code.task` write fence | Worker sends `fencing_token=lease_token`; governed pipeline hashes/checks |
| Budget gate at lease | Spendy jobs without active reservation cancelled with visible decision |
| Slot allocation | Work-orchestration may defer when capacity full |

### Residual risks

1. **`lease_lost` does not abort `execute_job`.** The HB thread exits; the main thread can keep running a long pipeline until it tries `complete`/`fail`, which then fails the HTTP call. Fencing reduces double-write for `code.task`; other kinds may still do non-fenced side effects as zombies.
2. **Reclaim event typing:** reclaim of status `leased` emits `job.lease_reclaimed`; reclaim of expired `running` emits `job.leased` — observability under-counts reclaims.
3. **Attempt budget includes reclaims:** crash/reclaim burns `max_attempts` (default 3). Long flaky stages can DLQ without “true” execution failures.

---

## 2. Retries and deadletter handling

### Path

```
poll/claim (attempt++) → heartbeat → execute
  success → complete → succeeded + events
  fail(retryable=true, attempt < max) → failed_retryable + run_after (exp backoff ≤300s)
  else → failed_final → INSERT deadletters → deadlettered + events
```

Backoff (worker): `min(300, 5 * 2^min(attempt-1, 6))` only when `RetryableJobError`.

Related side effects on terminal fail:

- Outbox / webhook messages marked deadlettered where linked.
- Agent studio failure terminal/non-terminal markers.
- **`code.task` run** forced to `failed` when driving job deadletters (`_mark_related_code_task_failed`) — fixes stuck `running` runs after step 500s.

### Retryability matrix (worker)

| Kind | On failure | GA note |
|------|------------|---------|
| `webhook.outbound.deliver`, `a2a.dispatch`, `work_tracker.issue.sync` | `RetryableJobError` | Correct durable retry |
| `docs.update`, `agent.orchestrator.next_move` | Honor API `retryable` flag | Good |
| **`code.task`**, `meeting.step`, `openai.plan`, most software_factory / infra | **`RuntimeError` → non-retryable** | **Immediate DLQ** on first terminal fail path even if attempts remain |
| HTTP/transport errors on long `code.task` step | Not classified retryable | Inflates DLQ on 500/timeouts |

**Finding (P1):** Most expensive path (`code.task`) treats nearly all failures as final. Transient model/API blips become deadletters, not retries. Combined with reclaim-burned attempts, this is a primary driver of live DLQ volume.

### Visibility (invariant holds)

- Events: `job.created`, `job.leased` / `job.lease_reclaimed`, `job.heartbeat`, `job.succeeded`, `job.failed_retryable`, `job.failed_final`, `job.deadlettered`, `code_task.failed`.
- API: `GET /v1/deadletters`, jobs list, health snapshot risks (`deadletters`, `retryable_jobs`).
- Live QA: failures were visible; backlog was **not** operationally reaped.

---

## 3. `code.task` DLQ risk (live + code)

### Live sample (2026-08-11)

Jobs sample (n≈100): **61 succeeded / 39 deadlettered**, almost all `code.task`. Buckets:

| Bucket | ~Count | Class |
|--------|-------:|-------|
| agent_max_turns | 7 | Policy / agent loop |
| model_rate_limit_tpm | 5 | Capacity |
| model_credits_or_spend_cap | 5 | Budget/org |
| timeout (900s) | 5 | Runtime / lease coupling |
| sandbox_exec not allowlisted | 3 | Substrate |
| gate_no_test_files | 3 | Product gate (expected fail-closed) |
| NoneType `.get` | 2 | **Bug** |
| `code-tasks/step` 500 | 2 | **API bug / config** |
| **missing `git` in worker** | 1+ | **Image skew (BOSS-342)** |
| sandbox 404 / model tool validation / SIGTERM / etc. | rest | Substrate + model |

Most open DLQ rows were **stale (Jul)**; newest sampled ~2026-08-10. **Invariant OK; ops hygiene FAIL.**

### Root classes for GA

1. **Substrate / image** — `git` missing, allowlist, sandbox host (BOSS-340/342).  
2. **Model economics** — TPM, credits, max turns → should escalate to Decision, not infinite requeue; today often DLQ.  
3. **Product bugs** — NoneType, step 500.  
4. **Expected gates** — no tests → no PR (should not look like infra red if classified).

### Mitigations already in tree (not necessarily deployed)

- Root `Dockerfile` worker stage installs `git`, `ca-certificates`, `nodejs`, `npm` with BOSS-342 commentary.
- `tests/test_runtime_image_contract.py` asserts worker stage contract; optional live image probe.
- Deployment runbook: shared tag for api/worker/web; do not leave worker on stale tag.
- Control-plane read timeout 3600s for long pipelines; lease HB independent of main HTTP call.
- Runtime ceiling prevents infinite HB on hung stages.

---

## 4. Worker image skew (git / tags)

| Check | Status |
|-------|--------|
| Historical live: worker tag `norebuild` vs api/web `view2` | **Fail (ops)** |
| Historical live: no `git` in worker; api has `/usr/bin/git` | **Fail (image)** → DLQ `No such file or directory: 'git'` |
| Current multi-target `Dockerfile` worker stage | **Fixed in source** |
| Image contract test | **Present** |
| Goldeneye compose still on mixed tags | **Must verify at deploy** (process, not code) |

**GA gate:** Same tag/SHA for `boss-api` and `boss-worker`; `which git` / contract probe green on running worker; zero new `missing_git` DLQs over soak window.

---

## 5. `agent.orchestrator.next_move`

### Code (current)

- Capability advertised in `capabilities()`.
- Worker routes to `POST /v1/orchestration/next-move/execute`.
- Service is fail-closed: unresolved plan → non-retryable fail; noop emits `orchestrator.next_move.noop`; success emits `orchestrator.next_move.applied` + receipt with dedupe.
- Phase advance: implement → `start_code_task`; other phases enqueue agent briefs; missing GitHub target → **blocking decision** (not silent).
- Unit/API tests: capability present, execute via control plane, failure visible.

### Doc debt

`PRD_CRITICAL_GAPS_2026-08-12.md` and Plane note BOSS-345 still claim “not worker-capable.” **That is stale relative to repo.** Treat as: code GO pending **live** proof that workers advertising the capability are deployed and subscriptions fire `plan.dispatched` → job → advance.

### Residual orchestration risks

- Implements by enqueuing more `code.task` work → inherits all §3 DLQ risk.
- Classify-by-regex phase titles is heuristic; misroute possible but visible.
- Lease token currently discarded in service (`del lease_token`); fencing relies on job complete path only.

---

## 6. Reliability service vs job reliability

`ReliabilityService` (scan → signals → incidents → diagnose) is an AI-SRE plane over telemetry sources, not the job lease kernel. Live QA saw open critical incidents (disk full, error_surge) orthogonal to DLQ.

**Do not conflate:** green Reliability UI ≠ drained deadletter queue. GA needs both:

1. Kernel: lease/retry/DLQ correctness (largely done).  
2. Ops: DLQ triage SLO, image parity, model budget decisions.

---

## 7. Tests covering this panel

| Layer | Evidence |
|-------|----------|
| Worker unit | poll/HB/fail/complete cycles; deadletter unsupported/providerops; next_move success/fail; webhook backoff; failure path calls `fail` not `complete` |
| API/engine | chaos `lease_reclaim` / `worker_crash` / `deadletter_storm`; webhook retry→DLQ; orchestrator next_move claim tests |
| Image contract | Dockerfile stage tools for worker |
| Workflow runtime | separate workflow-step lease reclaim/DLQ (related pattern, not identical to `jobs` table) |

Gaps: little automated proof that **`code.task` HTTP 5xx is retryable**; little proof **`lease_lost` aborts execution**; no live soak gate in CI for DLQ rate by class.

---

## 8. GA blockers and recommendations

### Blockers (must close or accept)

| ID | Severity | Action |
|----|----------|--------|
| W-REL-01 | **P0 deploy** | Rebuild/redeploy worker with shared tag; prove `git`/`node`/`npm` in container |
| W-REL-02 | **P1 product** | Triage/reap open DLQ; fix NoneType + `code-tasks/step` 500 |
| W-REL-03 | **P1 reliability** | Classify transient `code.task` transport/API failures as `RetryableJobError` (bounded); route credit/TPM/max-turns to Decision not raw DLQ where appropriate |
| W-REL-04 | **P1 ops** | DLQ SLO: age, owner, requeue only fixable classes; weekly signal-poll |

### Should-fix before GA scale

| ID | Severity | Action |
|----|----------|--------|
| W-REL-05 | P2 | On `lease_lost`, cancel in-flight work / cooperative abort (not only stop HB) |
| W-REL-06 | P2 | Emit `job.lease_reclaimed` for expired `running` reclaim |
| W-REL-07 | P2 | Live dogfood gate: DLQ open count + zero `missing_git` + worker/api image digest match |
| W-REL-08 | P2 | Confirm live workers advertise `agent.orchestrator.next_move`; one E2E plan.dispatched → phase job |

### Accept for GA (with monitoring)

- Default `max_attempts=3` and reclaim counting as attempt.  
- Non-retryable permanent policy failures (feature disabled, missing reservation already cancelled at lease).  
- Fail-closed PR gate (`tests_failed_no_pr`) as product behavior if labeled.

---

## 9. Scorecard summary

| Invariant | Code | Live (2026-08-11) |
|-----------|------|-------------------|
| No execution without lease | Pass | Pass (idle poll healthy) |
| Heartbeat before expiry | Pass | Pass |
| Expired lease reclaimable | Pass | Not fully re-proved live this panel |
| Failure → retry or DLQ or decision | Pass (kernel) | Pass visibility; **dirty backlog** |
| No silent drops | Pass | Pass |
| Substrate parity (git) | Pass in Dockerfile | **Failed historically** |
| Autonomous code path reliable | Partial | **~39% DLQ in sample** |

**Panel conclusion:** The **job reliability kernel is GA-capable**. The **worker execution substrate and `code.task` failure taxonomy are not** until image parity is live-verified and DLQ classes are reduced/classified. Orchestration `next_move` is implemented in code; treat documentation claims of missing capability as obsolete and verify deployment instead.

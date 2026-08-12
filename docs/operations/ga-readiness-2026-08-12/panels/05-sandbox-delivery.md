# Panel 05 — Sandbox / Preview / Delivery Expert

**Date:** 2026-08-12  
**Scope:** microVM/sandbox host, preview URL reachability, delivery-target honesty, community GA deploy path  
**Edition under assessment:** `community` dogfood on goldeneye (`boss-app`)  
**Sources:** `BOSS-340_SANDBOX_HOST_DIAGNOSIS.md`, `PREVIEW_EXECUTION_SUBSTRATES.md`, `LIVE_QA_2026-08-11.md`, `LIVE_UI_FUNCTIONAL_AUDIT_2026-08-11.md`, `E2E_COMPLETION_PLAN.md`, `PRD_CRITICAL_GAPS_2026-08-12.md`, code under `engine/boss_engine/services/{sandbox_*,preview_service,deploy_service,deployment_*}`, UI under `apps/web/app/{sandboxes,delivery,deploy}-ui.tsx`, live probe `ga-readiness-2026-08-12/live_probe.txt`

---

## Verdict (one line)

**YELLOW / not GA-ready for the execution→preview→deploy slice:** control-plane connectivity to the sandbox host is restored, preview *gateway* paths are designed and tested in-tree, and CI/CD UI honesty has improved — but **real microVM task execution is still blocked (missing guest rootfs)**, live preview “View” may still lag behind publicize fixes depending on deploy SHA, and **community dogfood has zero deployment targets with deploy flags fail-closed**.

| Sub-area | Grade | Confidence |
|---|---|---|
| Sandbox host connectivity | **GREEN** (post BOSS-340) | High (live health + control-plane `host.connected: true`) |
| MicroVM task *execution* | **RED** | High (rootfs missing; registry guest tags null) |
| Preview operator reachability (code) | **GREEN** design | Medium (unit tests + `publicize_preview`; live view2 may predate) |
| Preview operator reachability (live) | **YELLOW** | Medium (audit: loopback URLs; code fix on main; deploy evidence incomplete) |
| Delivery targets honesty | **GREEN** code / **YELLOW** product | High UI empty-states; still 0 targets on dogfood |
| Community GA deploy path | **RED → YELLOW** | High — no sealed demo path without host + rootfs + optional SSH target |

**Composite panel grade: YELLOW (blocked for GA claim of “sandbox → preview → ship”).**

---

## 1. Architecture ground truth

### Hybrid split (mandatory design)

| Plane | Runs | Privileges |
|---|---|---|
| Control plane (`boss-api` / worker) | Governance, PR, budget, decisions, preview *proxy* | Unprivileged container: no `/dev/kvm`, no `docker.sock` |
| Sandbox host (`sandbox-service` on goldeneye) | Firecracker jailer VMs, task submit/poll/hold/preview | Privileged host daemon on `:8077` |
| Preview gateway | `/v1/previews/<id>/web/` reverse-proxy + HTML shim | Auth-exempt by unguessable id + TTL |

Workload A = **code-task execution** (clone → agent → tests → diff).  
Workload B = **persistent / retained-rootfs app previews** (serve app from build env).  
Repo stand-in: `sandbox-host/` (`ContainerBackend` / guest image); production path on goldeneye is Sean’s Firecracker service under `/srv/sandbox`, not docker-compose membership of boss-app.

Providers (control plane):

- `LocalSubprocessProvider` — ephemeral clone + allowlisted subprocess on API/worker host (`local_subprocess`). **Not isolation.**
- `MicroVMProvider` — whole-task HTTP contract to `BOSS_SANDBOX_AGENT_URL` (`submit` / `poll` / `release` / `hold` / `serve_preview`).

Settings: `BOSS_SANDBOX_PROVIDER` (native) and `BOSS_GOVERNED_SANDBOX_PROVIDER` (governed override); both default `local` in `app.env.tmpl`. Goldeneye dogfood has been run with **`BOSS_SANDBOX_PROVIDER=microvm`** (BOSS-340).

---

## 2. MicroVM / sandbox host assessment

### 2.1 What BOSS-340 fixed (2026-08-12)

| Check | Before | After |
|---|---|---|
| `GET /v1/sandboxes` `host.connected` | `false` — connection refused | **`true`** |
| Port `8077` | Nothing listening | `sandbox-service` active, bind `0.0.0.0:8077` |
| Runtime tree `/srv/sandbox` | Missing | Recreated (bin, kernels, rootfs dir, vms, jail) |
| Firecracker + jailer | Absent | v1.16.1 |
| Guest kernel | Absent | `vmlinux-6.1.102` |
| Capacity probe | N/A | `capacity: 8`, `available: 8`, `running: 0` |
| API container → host | Refused | Health OK via `http://10.100.9.1:8077` |

Control-plane probe path: `sandbox_console.list_sandboxes` → `GET {BOSS_SANDBOX_AGENT_URL}/v1/sandbox/health` with `X-Sandbox-Token`.  
Live probe 2026-08-12: `200 /v1/sandboxes` (route up; does not alone prove rootfs).

**Connectivity acceptance for BOSS-340 is met.** Do not re-open as “host unreachable” without re-probing.

### 2.2 Remaining execution blocker (P0 for factory reliability)

| Asset | Status |
|---|---|
| `sandbox-service` systemd enabled | ✅ |
| Firecracker / jailer / kernel | ✅ |
| **`/srv/sandbox/rootfs/guest-base.ext4`** | ❌ **missing** |
| Registry `boss-sandbox-guest` | Tags null — no pullable image to bake from |
| End-to-end `POST /v1/sandbox/tasks` → `done` + diff | ❌ Not proven |

**Implication:** `host.connected: true` is **health-only**. MicroVM code tasks will fail at provision until rootfs is baked:

```bash
# After a tagged guest image lands in the registry:
bash /root/projects/infra/sandbox/bake-rootfs.sh \
  goldeneye.tail95d980.ts.net:5000/boss-sandbox-guest:<tag>
systemctl restart sandbox-service   # optional
```

Until then, operators should expect:

- Sandboxes MC area: host “connected”, **empty live VM list** (by design — VMs are ephemeral).
- Retained-rootfs inspect/console: only useful **after** a task has run and rootfs was retained.
- Fallback: `BOSS_SANDBOX_PROVIDER=local` / `local_subprocess` for coding (weaker isolation; needs **git + allowlisted tools in worker image** — BOSS-342 skew).

### 2.3 Contract honesty vs UI

- Console correctly reports host connection vs empty sandbox list (single-tenant ephemeral VMs).
- Generic live shell exec remains **blocked** in `sandbox_console` (retained rootfs browse/logs only; break-glass elevated).
- `MicroVMProvider.acquire()` raises — whole-task dispatch only; pipelines must branch on provider kind.
- Repo `sandbox-host/README.md` still describes older `POST /v1/sandbox/run-task` + ContainerBackend; goldeneye contract is the Go/Python async `POST /v1/sandbox/tasks` envelope (`docs/infra/PYTHON_GO_SANDBOX_CONTRACT.md`). **Doc drift risk** for operators who only read repo README.

### 2.4 Operational risks

1. **Bridge IP churn:** `BOSS_SANDBOX_AGENT_URL=http://10.100.9.1:8077` vs current `boss-app_boss` gateway `10.100.47.1` — works while service binds all interfaces; compose `host.docker.internal:host-gateway` still recommended.
2. **No guest image pipeline in dogfood release** — rootfs bake is host-side, not `make deploy`.
3. **Worker image skew** (`norebuild` vs api `view2`) and missing `git` poison `local_subprocess` fallback and some code-task DLQs.
4. Deadletter history includes `sandbox_task_404` and allowlist failures — failures are **visible** (good invariant) but backlog was not reaped (LIVE_QA: ~39–41 open).

---

## 3. Preview URL reachability

### 3.1 Design (in-tree, correct)

`preview_service.publicize_preview`:

- DB/internal bind remains `http://127.0.0.1:<port>` (process inside API host).
- **Operator-facing `url` / `open_url`** must be `/v1/previews/<id>/web/` (API reverse-proxy), never loopback.
- `internal_url` preserves bind for diagnostics and proxy probe.
- Auth: preview web routes intentionally **auth-exempt** (unguessable id + TTL); API key not required for browser View.
- Substrates (persisted, not recomputed): `preview_rootfs` (preferred after microVM build) → `preview_docker` → `preview_host`.
- Lifecycle: boot **reconciles** running previews (no blanket kill); hold ≤2h for rootfs; SSE logs; classified failures; feedback → confirmation-gated governed retry.
- Proxy: `<base href>` + client shim for static assets, history API, WS/EventSource, redirects.

Tests: `engine/tests/test_preview_urls_are_operator_reachable.py` encode the “never hand out 127.0.0.1” invariant.

### 3.2 Live dogfood truth

| Evidence | Finding |
|---|---|
| LIVE_UI audit (view2) | ~50 previews; sample **`http://127.0.0.1:41635`** — operator cannot open from tailnet browser |
| Agent chat | Threads still referenced dead/loopback preview URLs |
| BOSS-341 (E2E plan W2.3) | Exit: “View opens real preview” — **not closed with live proof in this panel pack** |
| Live probe 2026-08-12 | `200 /v1/previews` only — **no body assertion** that `url` is gateway path |

**Panel judgment:** Code path is **ready**; **GA cannot claim preview reachability** until a post-fix deploy proves:

```bash
curl -sS -H "Authorization: Bearer $BOSS_API_KEY" \
  "https://boss-api.local.sean.network/v1/previews?project_id=boss-dogfood&limit=5" \
  | jq '.previews[] | {id, status, url, open_url, internal_url}'
# Expect: url/open_url = /v1/previews/.../web/  (or absolute tailnet URL to that path)
# Never: 127.0.0.1 / localhost as primary url for running previews
```

And a browser open of `https://boss-api.local.sean.network/v1/previews/<id>/web/` against a **running** preview returns app HTML (not “no longer running”).

### 3.3 Preview ↔ microVM coupling

| Build substrate | Preferred preview | Blocker if microVM broken |
|---|---|---|
| `microvm` | `preview_rootfs` (env == build) | No successful retained rootfs without guest image |
| `local_subprocess` | `preview_host` or `preview_docker` | Host process killed by API lifecycle historically; reconcile + docker path mitigate |
| any | gateway proxy | Independent of rootfs once a host/docker preview is up |

**Rootfs previews are not community-GA-credible until microVM tasks complete.** Host/docker previews can still demo “View app” if publicize is deployed and a process binds.

---

## 4. Delivery targets honesty

### 4.1 Code-level honesty (improved — credit)

CI/CD UI (`delivery-ui.tsx`, `deploy-ui.tsx`, `delivery-manage.tsx`) now encodes audit lessons:

| Behavior | Honesty property |
|---|---|
| `configured: false` / zero targets | **Setup-only** empty state: no fake fleet table, no deploy wizard chrome that looks like a running pipeline |
| Health with zero targets | Explicit “nothing registered to monitor” — not “0 up / 0 down” outage theater |
| `not_deployed` target | URL shown as **planned text**, not a dead link |
| `BOSS_ENABLE_SANDBOX_DEPLOY=false` | `SandboxDeployFlagNotice` — intentional fail-closed, manual onboarding still possible |
| Deploy steps | Live stepper records per-step status/logs (not spinner-over-empty) |

Automated tests: `apps/web/tests/delivery-empty-states.test.tsx` and delivery execute/verdict unit coverage.

### 4.2 Live dogfood product state

| Signal | Live (2026-08-11/12) |
|---|---|
| Deployment targets | **0** |
| Deployment runs / promotions / policies | Empty |
| Delivery health | `overall: not_deployed` (or unconfigured empty) |
| `BOSS_ENABLE_SANDBOX_DEPLOY` | **false** (correct default) |
| `BOSS_ENABLE_SANDBOX_MERGE` | true on dogfood (LIVE_QA) but merge/deploy path not exercised in QA scope |
| Observability stack | `deployed: false` |

**Honesty grade for UI:** GREEN **if** the empty-state code is what is deployed.  
**Product completeness grade:** RED/SHELL for “we ship customer apps from BOSS” — no registered destination on dogfood.

Historical audit risk (pre-empty-state): “full deploy machinery with 0 targets.” Treat residual risk as **deploy web image parity**, not missing design.

### 4.3 Target model (what “honest deploy” means)

Targets are first-class rows (`deployment_targets`) with `target_kind` connectors (`deployment_connectors.py`: SSH host primary for self-hosted; skills/providers for cloud). Non-sandbox / production paths require:

- Environment rollout policy (`staging_allowed` / `production_allowed`)
- BudgetReservation for spendy steps
- Approval-gated ChangeSet for provider writes
- Health checks + rollback points
- SecretRefs (not secrets in chat)

Default env: `BOSS_SSH_HOST_DEPLOY_ALLOWED=false`, `BOSS_ENABLE_SANDBOX_DEPLOY=false` — **fail-closed is correct for GA safety**.

---

## 5. Deploy path for community GA

### 5.1 What community GA *should* promise (panel recommendation)

Community GA is **not** multi-cloud ChangeSet automation. A credible community story is:

1. **Code in isolation** (local_subprocess *or* microVM once rootfs exists) with receipts + deadletters, not silent drops.  
2. **Operator-reachable preview** via `/v1/previews/<id>/web/`.  
3. **Either** one manual/sandbox **SSH or staging target** with health **or** permanently honest “not configured” CI/CD (already coded).  
4. Merge/deploy flags remain **off** until policies + budget + rollback are explicit (GOLDENEYE_DEPLOYMENT rules).

### 5.2 Path options (ranked for dogfood demo)

| Path | Prerequisites | GA fit |
|---|---|---|
| **A. Preview-only “ship signal”** | Publicize deployed; any successful host/docker preview | Minimum community confidence loop |
| **B. microVM build + rootfs preview** | BOSS-340 + **bake rootfs** + one green task | Strongest isolation story |
| **C. Manual SSH target** | Onboard wizard + `BOSS_SSH_HOST_*` + `BOSS_SSH_HOST_DEPLOY_ALLOWED` for real apply; plan-only without flag | Real FDE-ish demo; operator-owned host |
| **D. Sandbox auto-deploy after merge** | `BOSS_ENABLE_SANDBOX_DEPLOY=true` + healthcheck URL + policies | **Do not enable for GA** until rollout evidence + receipts proven |
| **E. Provider/cloud ChangeSet production** | Enterprise/ProviderOps + real provision flags | **Out of community GA scope** (fail-closed) |

### 5.3 Recommended community GA deploy slice (concrete)

**DoD for this panel (exit “YELLOW → GREEN”):**

1. **Bake + prove microVM task**  
   - Guest image tagged → `guest-base.ext4` present → submit test task → `status=done` with diff.  
   - Plane: close BOSS-340 *execution* criterion (not just connectivity).

2. **Worker image parity** (BOSS-342)  
   - Same tag/SHA as api; `git` present; no missing-git DLQ class.

3. **Preview publicize live proof** (BOSS-341)  
   - List previews: no primary loopback URLs; browser View works on tailnet.

4. **CI/CD honesty live proof** (BOSS-344)  
   - With 0 targets: empty-state copy only.  
   - Optional stretch: one **sandbox/dev** SSH target, plan or health-gated deploy with flag documented.

5. **Do not** flip `BOSS_ENABLE_SANDBOX_DEPLOY` or production DNS/provider writes as part of community GA marketing.

### 5.4 Plan-only value (already useful)

`deploy_service.plan_ssh_deploy` produces a full dry-run plan (build, rsync, nginx container, health, rollback) **without touching a host**. Community GA can teach “what BOSS would run” before credentials exist — surface this in empty-state CTAs if not already.

---

## 6. Plane / workstream map (this panel)

| Plane | Topic | Status vs this panel |
|---|---|---|
| **BOSS-340** | Sandbox host | Connectivity **done**; execution **open** (rootfs) |
| **BOSS-341** | Preview URLs | Code ready; **live gate open** |
| **BOSS-342** | Worker/git/DLQ | Blocks local substrate reliability |
| **BOSS-344** | CI/CD empty honesty + minimal deploy | UI code ready; dogfood **0 targets** |
| **BOSS-337** | Live QA umbrella | Still owns composite readiness |
| W2 (E2E plan) | Execution substrate | Critical path for factory |

---

## 7. Risks if we call community GA “ready” now

1. **False isolation narrative** — `host.connected` looks like microVMs work; tasks fail at provision.  
2. **False preview narrative** — Mission Control “View” opens dead loopback if publicize not live.  
3. **False shipping narrative** — CI/CD chrome without a target (mitigated in code; verify deploy).  
4. **Silent fallback to API host** — local_subprocess + host preview ≠ PRD microVM security story.  
5. **Enabling deploy flags under pressure** — violates GOLDENEYE_DEPLOYMENT / budget-receipt hard rules.

---

## 8. Panel recommendations (ordered)

| # | Action | Owner surface | Blocks |
|---|---|---|---|
| 1 | Publish `boss-sandbox-guest` tag + `bake-rootfs.sh` + prove one task `done` | Host / BOSS-340 | MicroVM GA claim |
| 2 | Redeploy api/web with publicize + empty-state UI; live jq + browser proof | BOSS-341 / 344 | Preview + honesty GA |
| 3 | Align worker image + git | BOSS-342 | Code-task reliability |
| 4 | Keep deploy/merge real-write flags fail-closed; optional one manual SSH target for demo | Ops | Safe community story |
| 5 | Extend live gate: sandboxes health, preview URL shape, deployment-targets count / empty-state | W5 / BOSS-351 | Regression |
| 6 | Refresh `sandbox-host/README.md` to point at goldeneye Firecracker contract | Docs | Operator confusion |
| 7 | Reap/triage sandbox-related deadletters | Ops | Noise vs signal |

---

## 9. Acceptance checklist (copy into Plane when claiming Done)

- [ ] `GET /v1/sandboxes` → `host.connected: true` **and** capacity fields present  
- [ ] `ls /srv/sandbox/rootfs/guest-base.ext4` exists; health alone insufficient  
- [ ] One microVM (or documented local) code task completes with receipt + non-empty evidence  
- [ ] `GET /v1/previews` primary `url` never loopback for running rows  
- [ ] Browser: `…/v1/previews/<running-id>/web/` returns app content on tailnet  
- [ ] CI/CD with zero targets shows setup empty-state only (no fake fleet)  
- [ ] `BOSS_ENABLE_SANDBOX_DEPLOY` remains false unless policy/budget/rollback documented  
- [ ] Live gate includes sandbox + preview assertions (not only `/health` + providers)

---

## 10. Summary judgment for GA council

| Claim | Allow? |
|---|---|
| “Sandbox host is wired on goldeneye” | **Yes** (connectivity) |
| “Code runs in Firecracker microVMs” | **No** until rootfs + green task |
| “Operators can open app previews from Mission Control” | **Only after live publicize proof** |
| “Community can register deploy targets honestly” | **Yes** (API + UI empty-state design) |
| “Community GA includes automatic production deploy” | **No** — out of scope; fail-closed correct |
| “End-to-end software factory sealed” | **No** — depends on this panel + orchestration/worker panels |

**Panel vote:** **Hold GA for execution-plane marketing.** Ship community GA only with an explicit scope line: *control plane + honest empty deploy + (local or microVM once proven) previews*, and list microVM rootfs + preview live proof as **release blockers** if marketing claims isolation or View-app demos.

# Panel 14 — Observability / Ops / Deploy (Self-Host Community GA)

**Date:** 2026-08-12  
**Panel:** Observability, Operations, and Deploy Expert  
**Scope:** Production ops readiness for **self-host community GA** — deploy, rollback, monitoring honesty, image tag alignment  
**Primary surfaces:** goldeneye dogfood (`boss-app`), compose/Helm packages, Mission Control Monitoring + CI/CD, live QA gates  
**Related Plane work:** BOSS-342 (image skew / worker tools), BOSS-344 (empty-state honesty), BOSS-351 (live dogfood gate), BOSS-337 (live QA)

---

## 1. Executive verdict

| Dimension | GA posture | Score |
| --- | --- | --- |
| Deploy path (self-host / goldeneye) | **Conditional go** — documented single-tag build + compose deploy works; operator discipline still required | 7/10 |
| Rollback | **Conditional go** — image-tag / Helm revision revert documented; not fully automated or drill-proven on dogfood | 6/10 |
| Monitoring honesty (product UI) | **Go** for empty honesty on unconfigured stacks; **no-go** if marketing implies live Prometheus/Grafana | 8/10 code, 4/10 product claim |
| Image tag alignment | **Was fail (live QA 2026-08-11); contract fixed in tree** — must be re-proven on live with shared tag | 5/10 until live re-verify |
| Control-plane health / reliability primitives | **Go for liveness + DLQ visibility** — not full observability platform | 7/10 |
| **Overall self-host community GA ops readiness** | **CONDITIONAL GO** — ship only with shared image tags, fail-closed dangerous flags, honest Monitoring empty state, and post-deploy live gate | **6.5/10** |

**One-line call:** Community self-host GA may claim “deployable control plane with auditable jobs and honest empty monitoring,” **not** “production observability stack, zero-touch rollback, or skew-proof multi-image releases without process.”

---

## 2. Sources consulted

| Source | Role |
| --- | --- |
| `docs/operations/GOLDENEYE_DEPLOYMENT.md` | Production compose package, shared-tag build, Traefik routing, fail-closed flags |
| `docs/operations/INFRA_RUNBOOK.md` | Env progression, staging/prod checklists, rollback, kill switches |
| `docs/operations/LIVE_QA_2026-08-11.md` | Live image skew (`worker:norebuild` vs `api/web:view2`), DLQ, flags |
| `docs/operations/LIVE_UI_FUNCTIONAL_AUDIT_2026-08-11.md` | Monitoring SHELL / observability `deployed: false` |
| `docs/operations/E2E_COMPLETION_PLAN.md` | W2.1 image align, W1.4/W4.2 monitoring honesty, W5 deploy gate |
| `docs/operations/PRD_CRITICAL_GAPS_2026-08-12.md` | Deploy/monitor as PRD-critical gap class |
| `docs/operations/ga-readiness-2026-08-12/live_dogfood_gate.json` | 2026-08-12 gate **passed** (10/10 required) |
| `docs/operations/ga-readiness-2026-08-12/live_probe.txt` | Health + community routes 200; deadletters sample 41; decisions path 404 noise |
| `deploy/goldeneye.compose.yaml`, `docker-compose.yaml` | Image env vars, healthchecks |
| `deploy/helm/boss/` + `scripts/staging_helm_deploy.sh` | K8s deploy, shared tag set, probes |
| `tests/test_runtime_image_contract.py`, root `Dockerfile` | Worker/api tool contracts (BOSS-342) |
| `apps/web/app/monitoring-ui.tsx`, `delivery-manage.tsx` | Empty-state honesty (BOSS-344) |
| `apps/api` `/health`, `/v1/observability/stack`, `/v1/delivery/health`, reliability routes | Runtime contracts |
| Worker idle health sweep (`apps/worker/boss_worker/main.py`) | Monitoring only works if something samples |

---

## 3. Deploy readiness (self-host community)

### 3.1 What is real today

1. **Goldeneye multi-service package** is first-class: API, worker, web (optional account-connect), Traefik hostnames, no public TCP requirement.
2. **Two deploy modes:**
   - Source-tree: `docker compose --env-file app.env up -d --build` (shared `BOSS_IMAGE_TAG`).
   - Image-backed: buildx push `boss-api|worker|web:$TAG` then `deploy … --compose deploy/goldeneye.compose.yaml`.
3. **Platform dependency injection** is correct for self-host community: Postgres/Redis/S3/etc. come from the host platform; BOSS does not smuggle a second Postgres in production compose.
4. **Fail-closed effect flags** are documented and expected off for GA defaults:
   - `BOSS_INFRA_REAL_PROVISION_ALLOWED`, `BOSS_EMAIL_REAL_SEND_ALLOWED`, `BOSS_SLACK_REAL_SEND_ALLOWED`, `BOSS_ENABLE_SANDBOX_DEPLOY` (and related) remain disabled until ChangeSet / budget / approval / rollback paths are intentional.
5. **Staging Helm path** exists (`scripts/staging_helm_deploy.sh`) and forces **one** `BOSS_IMAGE_TAG` onto api/web/worker — good pattern for community self-host docs to copy.
6. **Live control-plane gate (2026-08-12):** `live_dogfood_gate` **passed** — `/health` ok, auth fail-closed, providers, budget quote, command→task, decisions inbox, deadletters list.

### 3.2 Deploy risks that block a clean GA claim

| Risk | Evidence | Severity | Mitigation for GA |
| --- | --- | --- | --- |
| **Image skew on image-backed compose** | `goldeneye.compose.yaml` uses **separate** `BOSS_API_IMAGE`, `BOSS_WORKER_IMAGE`, `BOSS_WEB_IMAGE` — easy to leave worker on `norebuild` while api/web advance (live QA 2026-08-11) | **P0** | Mandate one `$TAG`; refuse deploy if tags differ; document `deploy ls` check |
| **Worker tool contract** | Historical deadletters `No such file or directory: 'git'`; contract now in Dockerfile + `tests/test_runtime_image_contract.py` | **P0** until live prove | Build worker from current multi-target Dockerfile; probe `command -v git` in running container |
| **Web build-time API URL** | `NEXT_PUBLIC_BOSS_API_URL` baked at image build; runtime env alone is insufficient | **P1** | Build-arg required for non-relative production URLs; verify browser network tab post-deploy |
| **Env template clobber** | E2E plan: `--env-template` can wipe secrets on redeploy | **P0 operational** | Redeploy **without** template for routine image bumps |
| **Worker has no container healthcheck / K8s probes** | Compose worker: no healthcheck; Helm worker: no readiness/liveness | **P1** | Treat “container up” ≠ “lease loop healthy”; gate on job poll + `/v1/jobs` or worker heartbeats |
| **No automatic post-deploy gate in GOLDENEYE_DEPLOYMENT** | W5.2 still “document”; gate script exists | **P1** | Require `make qa-live` (or `live_dogfood_gate.py`) after every boss-app image bump |
| **Dangerous flag drift** | Live QA noted `sandbox merge=true` while deploy/infra false | **P1** | Community GA defaults: merge/deploy/infra/email/slack **false** unless explicitly approved |

### 3.3 Community GA deploy checklist (operator)

```text
[ ] TAG=$(git rev-parse --short HEAD) — one tag for api, worker, web
[ ] buildx --platform linux/amd64 for each target; push all three
[ ] python tests/test_runtime_image_contract.py
      BOSS_IMAGE=…/boss-api:$TAG BOSS_WORKER_IMAGE=…/boss-worker:$TAG
[ ] Compose/env: BOSS_*_IMAGE all end with :$TAG (no norebuild/view2 mix)
[ ] Redeploy WITHOUT --env-template (preserve secrets)
[ ] curl /health → database_connected true
[ ] Worker: docker exec … command -v git && command -v node
[ ] BOSS_API_KEY-gated: make qa-live (live_dogfood_gate)
[ ] Confirm dangerous flags off for community defaults
[ ] Confirm Monitoring shows honest unconfigured empty state if 0 targets
[ ] Record deploy evidence (tag, gate JSON, Plane issue comment)
```

---

## 4. Rollback readiness

### 4.1 What the runbook already says (and is correct)

From `INFRA_RUNBOOK.md`:

- Revert **image tags** or **Helm release revision**.
- Disable provider write flags.
- Drain cloud/effectful workers.
- Re-enter read-only provider mode.
- Restore Postgres **only** on confirmed corruption (with prior restore drill).
- Record rollback **evidence and reason** in BOSS.
- Rollback is **not** a substitute for receipt/idempotency (dedupe still required).

### 4.2 Gaps for community GA

| Gap | Why it matters | GA bar |
| --- | --- | --- |
| No recorded **rollback drill** on goldeneye dogfood | Operators have never timed “bad tag → previous tag” under pressure | Document + run one dry drill before GA marketing |
| Image-backed compose has **no** “previous known-good tag” file | Easy to lose the last green triple | Keep `LAST_GOOD_TAG` in ops notes / Plane |
| Worker-only skew rollback | Fixing api without rolling worker leaves DLQ class failures | Always roll **all three** images as a unit |
| DB migrations | Image rollback with forward-only schema can strand the API | GA note: migrate forward; do not claim one-click schema rollback |
| Product self-improvement rollback APIs | Exist for proposals; **not** the community self-host BOSS deploy path | Do not confuse product “rollback proposal” with platform deploy rollback |
| Helm worker lacks probes | Rollout status can look green while worker is wedged | Add probes post-GA or rely on lease/heartbeat SLOs |

### 4.3 Recommended rollback procedure (community self-host)

```text
1. Freeze effect flags (deploy/infra/email/slack/merge as appropriate).
2. Point BOSS_API_IMAGE / BOSS_WORKER_IMAGE / BOSS_WEB_IMAGE to LAST_GOOD_TAG (same tag).
3. Redeploy compose without env-template clobber.
4. Verify /health + qa-live + worker git present.
5. Open deadletters; requeue only after root-cause match.
6. Write Plane comment: reason, from_tag, to_tag, gate artifact path.
```

**Rollback score for GA:** sufficient as a **manual runbook** if operators follow shared-tag discipline; **insufficient** as an automated safety net.

---

## 5. Monitoring honesty

### 5.1 Product principle (correct and shippable)

Mission Control Monitoring is intentionally split from CI/CD:

- CI/CD answers: “Did my change ship?”
- Monitoring answers: “Is what we deployed healthy *now*?”

Empty honesty (**BOSS-344**) is implemented and unit-tested:

- `isObservabilityUnconfigured`: `deployed == false` and no apps → setup copy, **not** a fake fleet.
- Combined empty: “No deployment targets… no Prometheus, and no Grafana.”
- Deploy monitoring CTA is suppressed when there is no host target (cannot succeed).
- Worker idle path runs `delivery/health/sweep` so health is not “button-only monitoring.”

This is the **right** community GA posture: **honest absence** beats a painted Grafana iframe.

### 5.2 Live truth (2026-08-11 / 2026-08-12)

| Signal | Live state | Honest UI? |
| --- | --- | --- |
| Deployment targets | 0 / empty | Yes if empty-state path ships |
| Delivery health | `not_deployed` class | Yes if empty-state path ships |
| Observability stack | `deployed: false`, no Grafana/Prometheus | Yes — must not show “stack not running” chrome as if a fleet exists |
| Control-plane `/health` | ok, DB + GitHub + OpenAI configured | Real |
| Reliability / incidents APIs | Present in API | Useful for ops; not a substitute for Prometheus |
| Deadletters | 39–41 open historically; gate only **lists** them | DLQ is the real ops dashboard for factory health |

### 5.3 Honesty rules for GA messaging

**Allowed claims**

- “BOSS surfaces deployment health when targets exist and an observability stack is deployed.”
- “Until you onboard a host, Monitoring explains that nothing is configured — it does not invent green graphs.”
- “Control-plane health, events, receipts, and deadletters are first-class.”

**Forbidden claims**

- “Built-in Prometheus/Grafana for every install” (false until stack deploy path is exercised).
- “Full APM / multi-tenant metrics SaaS.”
- Any UI chrome that implies a live fleet when `deployed: false` and zero targets.

### 5.4 Residual honesty gaps

1. **W4.2 incomplete as product loop:** either deploy a minimal dogfood stack **or** keep empty forever — both OK; claiming “monitoring product” without one target is not.
2. **`/v1/observability/stack` `deployed` is DB-row based** (`telemetry_sources` prometheus row) — can theoretically desync from host containers; status probe needs `host_target_id`.
3. **Console SSR fan-out** historically stressed `/health` pools (comments in code/history) — ops must keep healthchecks cheap and auth-gated routes separate.
4. **Deadletter backlog** is the real reliability signal for community dogfood; Monitoring empty + DLQ high = “control plane up, factory unhealthy.”

---

## 6. Image tag alignment (BOSS-342 class)

### 6.1 Incident class (already proven in production)

Live QA 2026-08-11:

| Component | Tag |
| --- | --- |
| boss-api | `view2` |
| boss-web | `view2` |
| boss-worker | **`norebuild` (skew)** |

Effects:

- Worker missing `git` → `code.task` deadletters (`No such file or directory: 'git'`).
- API healthy + worker “up” while the governed pipeline could not execute — **the worst ops lie**.

### 6.2 In-tree remediations (good)

1. Root multi-target `Dockerfile` **worker** stage installs `git`, `nodejs`, `npm` (documented BOSS-342).
2. `tests/test_runtime_image_contract.py` asserts Dockerfile stages + optional live image probes for api and worker tools.
3. `GOLDENEYE_DEPLOYMENT.md` explicitly requires **one shared tag** and optional contract probe.
4. Source `docker-compose.yaml` uses **one** `BOSS_IMAGE_TAG` for all services (skew-resistant default).
5. Helm staging script sets the **same** tag for api/web/worker.

### 6.3 Remaining skew vectors (must close for GA)

| Vector | Mechanism | Required control |
| --- | --- | --- |
| Image-backed goldeneye compose | Independent `BOSS_*_IMAGE` env vars | Deploy policy: tags must match; CI/script assert `basename` tags equal |
| Partial rebuild | Operator rebuilds only api | Runbook: always rebuild api+worker+web as a set |
| Floating tags (`latest`, `goldeneye`) | Mutable tag, silent drift | Prefer immutable git-sha tags for GA releases |
| Registry lag | Push api, forget worker | Gate: contract test against **pushed** tags before `deploy` |
| Local vs remote | Laptop has git; container does not | Never use host tooling presence as proof |

### 6.4 Alignment acceptance test (GA gate)

```text
PASS only if all true:
  (a) api_tag == worker_tag == web_tag  (string equality on image tags)
  (b) docker run --rm --entrypoint sh $WORKER -c 'command -v git && command -v node && command -v npm'
  (c) docker run --rm --entrypoint sh $API -c 'command -v git && command -v node && command -v npm'
  (d) live_dogfood_gate required checks pass after deploy
  (e) no new deadletters with missing-git signature in the post-deploy window
```

Until (a)–(e) are green on the **actual** community dogfood host, treat image alignment as **not yet GA-proven** even if mainline Dockerfile is fixed.

---

## 7. Reliability & ops signals (what to watch)

### 7.1 Primary SLIs for community self-host

| SLI | Source | GA threshold (suggested) |
| --- | --- | --- |
| API liveness | `GET /health` | 99% success / 30d window once instrumented; always green post-deploy |
| Auth fail-closed | unauth `/v1/*` → 401/403 | 100% |
| Worker lease progress | jobs claimed / heartbeats | Non-zero activity or intentional idle; no silent stop |
| Deadletter rate | `/v1/deadletters` open count trend | No sustained growth of `git`/tooling class; triage backlog before GA fanfare |
| Budget safety | quote/reserve/release | Consume without reservation fails closed |
| Dangerous flags | env inventory | Deploy/infra/email/slack false by default |

### 7.2 Secondary / product-level

- Reliability scan/incidents (`/v1/reliability/*`) — available; not required for community GA claim.
- Delivery health samples — meaningful only with ≥1 deployment target.
- Health snapshots (`/v1/health-snapshots`) — executive/status surface; keep distinct from app fleet health.

### 7.3 On-call posture

- Prefer **visible failure**: deadletter, decision, event — never silent drop (repo hard rule).
- Optional webhook: reliability oncall path (`RELIABILITY_ONCALL_WEBHOOK`) is config, not a default community install.
- Kill switches from INFRA_RUNBOOK remain the budget/provider freeze story for effectful editions; community GA mostly stays read-only on provider writes.

---

## 8. GA gate matrix (ops / observability)

| Gate ID | Requirement | Status (2026-08-12) | Owner action |
| --- | --- | --- | --- |
| OPS-D1 | Documented self-host deploy path | **Met** (GOLDENEYE_DEPLOYMENT) | Keep README linked |
| OPS-D2 | Post-deploy live gate exists | **Met in scripts**; **partial** as mandatory deploy step | Wire into deploy SOP |
| OPS-D3 | Shared image tag on live dogfood | **Unknown / previously failed** | Re-deploy with sha tag; record `deploy ls` |
| OPS-D4 | Worker runtime tools (git/node/npm) | **Code met**; live re-verify required | Exec probe post-deploy |
| OPS-R1 | Manual rollback procedure documented | **Met** (INFRA_RUNBOOK) | Run one drill; store LAST_GOOD_TAG |
| OPS-R2 | Rollback drill evidence | **Missing** | Schedule before GA announce |
| OPS-M1 | Monitoring empty-state honesty in code/tests | **Met** (BOSS-344) | Ensure live web image includes it |
| OPS-M2 | No fake Grafana promise in GA copy | **Process** | Marketing review |
| OPS-M3 | Minimal dogfood Prometheus/Grafana | **Not required** for community GA if empty honesty holds | Optional W4.2 |
| OPS-S1 | Dangerous effect flags default off | **Mostly met**; verify merge flag | Align community defaults |
| OPS-S2 | Secrets not clobbered on redeploy | **Process** | Never routine `--env-template` |
| OPS-Q1 | live_dogfood_gate green | **Met 2026-08-12** | Re-run after every image bump |

---

## 9. Recommendations (priority ordered)

### P0 — before community GA tag

1. **Redeploy goldeneye with one immutable tag** for api/worker/web; prove worker `git` live; close BOSS-342 with evidence comment.
2. **Ban tag skew operationally:** assert equal tags in deploy notes / small script; never ship `norebuild`/`view2` mix again.
3. **Make `live_dogfood_gate` mandatory** after image bumps (W5.2); archive JSON under `docs/operations/ga-readiness-*/`.
4. **Confirm Monitoring empty honesty** on the live web image (not only unit tests).
5. **Inventory dangerous flags** on dogfood; publish community-safe defaults.

### P1 — first 30 days post-GA

6. Add **worker healthcheck** (compose) and **readiness probe** (Helm) based on control-plane heartbeat or lightweight `/health` proxy pattern.
7. Record a **rollback drill** (previous tag restore under 15 minutes).
8. DLQ hygiene: reap tooling-class deadletters; route model/TPM failures to Decisions (W2.5).
9. Optional: one sandbox deployment target + health sample so Monitoring can show a **real** green path without overselling Grafana.
10. Extend GOLDENEYE_DEPLOYMENT with “post-deploy gate” and “LAST_GOOD_TAG” sections (docs only).

### P2 — later

11. Deploy real observability stack on dogfood **or** keep permanently empty with setup CTA (do not half-deploy).
12. Enforce single tag in `goldeneye.compose` via one `BOSS_IMAGE_PREFIX`+`TAG` expansion if platform allows.
13. Schema-aware rollback policy docs (forward-only migrations).
14. Alerting defaults for open deadletter count / healthcheck fail (webhook optional).

---

## 10. Explicit non-goals for this GA

- Enterprise HA multi-AZ Postgres restore as a community requirement (document only).
- Full ProviderOps production writes, DNS, or public-cloud spillover.
- Account Connect / hosted marketplace health.
- Replacing customer Prometheus with BOSS as the sole observability product.
- Automated multi-region failover.

---

## 11. Final panel statement

Self-host community GA is **ops-ready only as a governed control-plane install**: Traefik-fronted API/web, leased workers, budget/receipt invariants, public `/health`, and **honest** Monitoring when nothing is deployed.

It is **not** ops-ready if we repeat image skew, skip post-deploy gates, clobber secrets with env templates, or market an observability stack that is not running.

**Conditional GO** contingent on a **shared-tag live redeploy + contract probes + qa-live green + rollback note**. Without that redeploy evidence, this panel votes **NO-GO** on production ops readiness despite strong in-tree fixes.

---

## 12. Evidence pointers

- Deploy package: `/Volumes/DataSSD1/test/boss/docs/operations/GOLDENEYE_DEPLOYMENT.md`
- Runbook: `/Volumes/DataSSD1/test/boss/docs/operations/INFRA_RUNBOOK.md`
- Image contract: `/Volumes/DataSSD1/test/boss/tests/test_runtime_image_contract.py`
- Monitoring UI honesty: `/Volumes/DataSSD1/test/boss/apps/web/app/monitoring-ui.tsx`
- Live gate artifact: `/Volumes/DataSSD1/test/boss/docs/operations/ga-readiness-2026-08-12/live_dogfood_gate.json`
- Prior skew incident: `/Volumes/DataSSD1/test/boss/docs/operations/LIVE_QA_2026-08-11.md`

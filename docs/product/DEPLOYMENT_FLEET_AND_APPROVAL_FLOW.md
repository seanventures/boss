# Fleet, approval, and autonomous deploy

Extends [`CICD_DEPLOYMENT_PLAN.md`](./CICD_DEPLOYMENT_PLAN.md). That plan wired *merge → staging*.
This one answers the three things it left open:

1. how a VM becomes deployable (SSH key lifecycle),
2. how **many apps map onto many VMs**, configurably,
3. how a **UI approval** turns into an autonomous deploy — without adding a second trigger.

---

## 1. What already exists — verified in code, not assumed

Most of this is built. The gaps are narrower than they look.

| Capability | Where | State |
|---|---|---|
| App↔target registry | `deployment_targets` (`db/schema.sql`) — `(project_id, app_name, environment)` UNIQUE, plus `target_kind`, `target_ref`, `domains`, `required_resources`, `secret_refs`, `health_checks`, `auto_deploy`, `paused` | ✅ |
| Target CRUD + lifecycle | `/v1/deployment-targets` GET/POST/PATCH, `/bootstrap`, `/secrets`, `/auto-deploy`, `/teardown`, `/purge` (`apps/api/app/main.py` 7178–7999) | ✅ |
| SSH connector | `SSHContainerConnector` (`deployment_connectors.py:225`); kinds `ssh`/`container`; `target_ref` = `user@host:port` (`_split_ref`) | ✅ |
| Readiness check | `preflight()` — reports exactly which credentials are missing, without deploying | ✅ |
| Host prep | `POST /bootstrap` — deploy dir, docker check, **allocates a free port and writes it back** | ✅ |
| SSH keypair generation | `secrets_service.generate_keypair()` — stores the private half, returns the public half plus an `install` hint | ✅ |
| Merge → staging deploy | `delivery_pipeline.on_pull_request_merged`, called from `software_factory.py:2355` | ✅ |
| Env policy incl. approval | `deployment_environment_policies.requires_approval` | ✅ table, unused |
| Live deploy UI | `apps/web/app/deploy-ui.tsx` — per-step progress, timings, logs | ✅ |
| Promotion staging→prod | `deployment_promotions` table + `promotion_orchestrator.py` | ⚠️ no endpoint, no UI |

## 2. The four real gaps

**G1 — the app↔VM resolver ignores `app_name`.**
`delivery_pipeline.target_for()` selects on `(project_id, repo, environment)` and takes
`ORDER BY created_at DESC LIMIT 1`. The *table* is keyed by `app_name`; the *resolver* is not.
So a repo with two apps silently deploys whichever target was registered most recently — and
because the skip path is deliberately silent, the second app simply never ships and nothing says
so. This — not the schema — is what blocks "multiple apps to multiple VMs".

`on_pull_request_merged` also hardcodes `"staging"` as the environment. That is correct for the
merge trigger and should stay, but it means environment selection has to enter through the
promotion path (§4), not by parameterising the trigger.

**G2 — a VM is not a first-class object.**
The host lives as a string inside each target's `target_ref`. Ten apps on one VM means ten
copies of `root@2.29.2.202:22` and ten `secret_refs` bindings to the same key. There is
nowhere to ask "what runs on this VM?", and rotating a key means editing ten rows.

**G3 — preview approval does not exist.**
`preview_service.py` (1843 lines) has no approval concept. A preview is a dead end: you look at
it, and nothing flows onward.

**G4 — production promotion has no gate surface.**
The table and orchestrator exist; nothing exposes them. There is no endpoint and no UI, so the
"human gate" the plan commits to is currently *no gate at all* — production simply never happens.

---

## 3. The design tension, stated plainly

`CICD_DEPLOYMENT_PLAN.md` §7 says:

> Do not let a deployment be triggered by anything other than a merge decision. Multiple trigger
> paths is how the preview resolver ended up with two copies of one decision.

The request — "once the preview is approved it should go to CI/CD" — reads like a second trigger,
and building it that way would re-create the exact defect that rule exists to prevent.

**Resolution: preview approval is an *input to the merge gate*, not a deploy trigger.**

Approving a preview records an approval artifact that **satisfies the merge decision**. The merge
then happens, and the *existing single trigger* fires the deploy. The operator experiences
"approve → it ships"; the system still has one path to `deployment_runs`.

This also separates two questions that are genuinely different, and which today are conflated:

| Gate | Question | Blocks |
|---|---|---|
| **Preview approval** | Does the app do the right thing? | the merge |
| **Promotion approval** | Should this reach customers? | staging → production |

Autonomy lives *between* them. That is the whole design.

---

## 4. Target flow

```
code task ──► PR opened ──► preview built  (exists)
                                  │
                        [UI] Approve preview            ← human gate #1
                                  │  writes approval artifact, satisfies merge decision
                                  ▼
                            merge decision ──► merged
                                  │
                                  ▼  delivery_pipeline.on_pull_request_merged   (THE ONE TRIGGER)
                     resolve targets for (project, repo, app(s), env=staging)
                                  │
                     ┌────────────┴────────────┐        one PR may fan out to N apps
                     ▼                         ▼
              deployment_run(app A)     deployment_run(app B)
                     │                         │
              rollout: build → transfer → health check → canary window
                     │                         │
                     └────────────┬────────────┘
                                  ▼
                        telemetry verdict
                    ├── regression → auto rollback + incident
                    └── clean → promotion proposed
                                  │
                        [UI] Approve promotion          ← human gate #2
                                  ▼
                        same rollout against prod target(s)
```

---

## 5. Schema: make the VM a first-class object

### 5.1 `deployment_hosts` (new) — the fleet inventory

```sql
CREATE TABLE IF NOT EXISTS deployment_hosts (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,              -- 'hetzner-ci-01'
  address       TEXT NOT NULL,              -- IP or DNS
  ssh_user      TEXT NOT NULL DEFAULT 'root',
  ssh_port      INTEGER NOT NULL DEFAULT 22,
  key_secret_ref TEXT REFERENCES secret_refs(id),   -- ONE key per host, not per app
  arch          TEXT,                       -- 'amd64' | 'arm64'  — see note below
  labels        JSONB NOT NULL DEFAULT '{}'::jsonb, -- {"env":"ci","provider":"hetzner"}
  port_range    INT4RANGE NOT NULL DEFAULT '[8000,8999]',
  max_apps      INTEGER,
  status        TEXT NOT NULL DEFAULT 'registered', -- registered|verified|degraded|retired
  last_preflight_at TIMESTAMPTZ,
  last_preflight   JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, name)
);
```

`deployment_targets` gains `host_id`, following the idiom this schema already uses — there is no
migrations directory; `db/schema.sql` evolves in place with idempotent `ALTER`s (see line 2127,
where `auto_deploy` was added the same way):

```sql
ALTER TABLE deployment_targets ADD COLUMN IF NOT EXISTS host_id TEXT REFERENCES deployment_hosts(id);
ALTER TABLE deployment_targets ADD COLUMN IF NOT EXISTS path_globs JSONB NOT NULL DEFAULT '[]'::jsonb;
```

**`target_ref` stays.** It is derived from the host on write, so every existing connector,
`_split_ref` call and test keeps working unchanged. `host_id` becomes the source of truth and
`target_ref` its projection — migrate by parsing existing refs into hosts. No connector changes.

> **`arch` is not decoration.** `hetzner-tenant-01` is ARM64 and `hetzner-ci-01` is x86_64.
> A Docker image or Go binary built for one will not run on the other, and the failure surfaces
> as a confusing runtime crash, not a deploy error. The build step must read `host.arch`.

### 5.2 Routing: which app, from which paths

`path_globs` (added above) declares which paths belong to an app — `["apps/web/**"]`, or `[]`
meaning the whole repo. Then replace `target_for()` with `targets_for()`:

```python
def targets_for(connection, project_id, repo, environment, *, changed_paths=None) -> list[dict]:
    """Every target this merge should deploy. A list, not an Optional — the singular return
    is G1: it made a two-app repo deploy whichever target was registered last."""
```

Resolution rule, deliberately boring:

1. All `active` targets for `(project_id, repo, environment)`.
2. If a target has `path_globs`, keep it only when a changed path matches.
3. Targets with empty `path_globs` always match (single-app repos keep working).
4. Return all matches. `on_pull_request_merged` creates one `deployment_run` per match.

This gives all four shapes the request asks for:

| Shape | How |
|---|---|
| 1 app → 1 VM | one target, empty globs |
| N apps → 1 VM | N targets, same `host_id`, distinct `path_globs`; `bootstrap` already allocates distinct ports |
| 1 app → N VMs | N targets, same `app_name`, different `environment` (or a `replica` label) |
| N apps → N VMs | the general case; the map is `deployment_targets` |

---

## 6. VM access: the SSH key lifecycle

The machinery exists (`generate_keypair`, `bind_secret_to_target`, `preflight`); the *last mile*
does not. Registering a host should be four steps in the UI:

1. **Register** — name, address, user. Status `registered`.
2. **Generate key** — `secrets_service.generate_keypair()`. Private half goes to the secret
   store; BOSS never displays it.
3. **Install** — the UI shows one copyable line:
   ```
   mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '<public key>' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
   ```
4. **Verify** — runs `preflight()`. On success status becomes `verified` and the host is
   selectable as a deploy target. On failure it reports *which* check failed.

**Step 3 stays human, deliberately.** Installing the first key is the one action that cannot be
automated without already having access. BOSS should not ask for, hold, or type a host password —
so it hands over an exact command and verifies the result.

In Community, OSS, and local editions, direct `install-key`, `provision`, and `retire` API
actions are disabled even when `BOSS_SSH_HOST_DEPLOY_ALLOWED=true`. They do not yet create
the approved Resource Plane ChangeSet, budget reservation, and ResourceReceipt required for
remote host changes. The fleet UI shows the reason and hides those actions. The three routes
check `providerops_manage` against the host's stored project and reject before key generation,
SSH, or host state changes. Other editions also require the outward SSH switch.
Fleet listings require project reader access, including when a caller supplies a different
project ID.

Two failure modes worth encoding in `preflight`, both hit on the Hetzner fleet this week and both
of which present as "key auth is broken" when it is not:

- **Root's password ships expired** on stock Hetzner images (`/etc/shadow` field 3 = `0`).
  Key auth *succeeds*, then PAM kills the session with *"Password change required but no TTY
  available."* Detect the string; report "host needs password expiry cleared", not "auth failed".
- **`PermitRootLogin`/`PasswordAuthentication` left at `yes`** on a public IP. Not fatal — flag it
  as a hardening finding on the host record.

Key **rotation** falls out of the model: one `key_secret_ref` per host means rotate = generate,
install alongside, verify, drop the old. Ten apps on that VM need no edits.

---

## 7. UI surfaces

Three, two of which are new:

**Fleet** *(new)* — hosts as rows: name, address, arch, apps running, port usage, preflight
status, last verified. Row expands to the register→generate→install→verify wizard from §6.
This is the "map of VM to apps", and it reads in both directions.

**Approvals** *(new)* — one queue, two kinds of item:
- *Preview ready* → app frame + diff + test results. **Approve** / **Request changes**.
- *Promotion proposed* → staging health, canary verdict, telemetry delta, rollback plan.
  **Promote** / **Hold**.

Each shows what it will do *before* the click ("deploys `web` to `hetzner-ci-01:8081`"), and
records who approved. Reuse `approval-rules-ui.tsx` patterns rather than inventing a second
approval idiom.

**Deployments** *(exists)* — `deploy-ui.tsx` already renders live per-step progress. It needs
only a filter by host and by app.

Endpoints to add:

```
GET   /v1/deployment-hosts                    list + preflight status
POST  /v1/deployment-hosts                    register
POST  /v1/deployment-hosts/{id}/key           generate keypair, return public half
POST  /v1/deployment-hosts/{id}/verify        preflight → verified|problems
GET   /v1/approvals                           unified queue
POST  /v1/previews/{id}/approve               → approval artifact → satisfies merge gate
POST  /v1/deployments/{run_id}/promote        → promotion decision → prod rollout
```

---

## 8. Milestones

Ordered so each is independently useful and testable on the real Hetzner fleet.

| # | Milestone | Unblocks | State |
|---|---|---|---|
| **F1** | `deployment_hosts` + `host_id`/`path_globs`; idempotent backfill from `target_ref` | G2 | ✅ done |
| **F2** | Fleet: service, 6 endpoints, `FleetPanel` in delivery-manage; `arch` captured | VM onboarding | ✅ done |
| **F3** | `targets_for()` returns a list; merge fans out to N runs | **G1 — multi-app** | ✅ done |
| **F4** | `POST /v1/previews/{id}/approve` + `/gate` → satisfies the merge decision | **G3 — seamless** | ✅ done |
| **F5** | Approval surface | headless → UI | ✅ done — folded into the preview row, not a queue |
| **F6** | `POST /v1/deployments/{run_id}/promote` → `promotion_orchestrator` | **G4 — production** | ✅ done |
| **F7** | Shepherd classes 13–16: deploy failed, health failed, canary regressed, promotion blocked | M5 of the original plan | ✅ done |

**F5 deviates from this doc on purpose.** §7 proposed an Approvals *queue*. Built instead as an
**Approve & ship** control on the existing preview row, because the judgement is about what is
on screen in *that* preview, and a separate queue would be a third place to look for one
delivery. The button states its own consequence before the click ("merges *Title* → deploys
web") and, when it cannot approve, says why rather than rendering disabled. Same for the fleet:
it lives in `delivery-manage.tsx`, which already owns onboarding and credentials, rather than a
new page.

**Shepherd classes 13–16 default to `shadow`** (observe, do not act) via
`DEFAULT_CLASS_MODES.get(klass, SHADOW)`. A brand-new class that can restart deployments on its
own is not something to enable on the day it is written.

## 9. What not to do

- **Do not add a second path to `deployment_runs`.** Preview approval satisfies the merge gate;
  it does not call the deploy service. One trigger stays one trigger.
- **Do not put the host in `target_ref` once `deployment_hosts` exists.** Derive it. Two writable
  copies of the host address is the same defect class as the resolver bug in G1.
- **Do not let BOSS hold host passwords** to automate first-key install. Print the command, verify
  the result.
- **Do not auto-promote to production**, even when the canary is clean. The gate is the product.
- **Do not build a second approvals idiom.** One queue, two item kinds.
- **Do not assume one architecture.** Read `host.arch` at build time.

---

## 10. How to test this

Two prerequisites, both already done on this workstation on 2026-08-20:

```bash
# 1. Schema (additive + idempotent: one table, two nullable columns)
docker exec -i infra-db-1 psql -U boss -d boss -f /tmp/fleet_only.sql

# 2. The API must be restarted to pick up the new routes, WITH the engine on the path.
#    Omitting PYTHONPATH gives "ModuleNotFoundError: No module named 'boss_engine'".
pkill -f "uvicorn apps.api.app.main"
set -a; . ./.env; set +a
PYTHONPATH=.:engine nohup .venv/bin/python -m uvicorn apps.api.app.main:app \
  --host 0.0.0.0 --port 8000 > .local-logs/api.log 2>&1 &
```

Confirm: `curl -s "localhost:8000/v1/deployment-hosts?project_id=boss-dogfood"` → `200`.

### A. The fleet, without touching a real host (2 min)

```bash
API=http://localhost:8000
PRJ=boss-dogfood

# register
curl -s -X POST $API/v1/deployment-hosts -H 'Content-Type: application/json' -d '{
  "project_id":"'$PRJ'","name":"hetzner-ci-01","address":"2.29.2.202",
  "ssh_user":"root","arch":"amd64","actor":"param"}' | jq

# mint the key — returns the PUBLIC half and the exact line to run on the box
curl -s -X POST $API/v1/deployment-hosts/<HOST_ID>/key \
  -H 'Content-Type: application/json' -d '{"actor":"param"}' | jq -r .install_command

# run that line on the host, then:
curl -s -X POST $API/v1/deployment-hosts/<HOST_ID>/verify | jq
```

`verify` returning `status: "registered"` with a `key_not_installed` finding is the CORRECT
answer before step 3 — it is evidence, not a failure.

In the console: **Delivery → Hosts**. Register / Generate key / Verify are the same four steps.

### B. Multi-app routing (G1), without deploying

```bash
# two targets, same repo, same host, different paths
curl -s -X POST $API/v1/deployment-targets -H 'Content-Type: application/json' -d '{
  "project_id":"boss-dogfood","app_name":"web","repo":"paramg/boss-sandbox",
  "environment":"staging","target_kind":"ssh","target_ref":"root@2.29.2.202:22",
  "path_globs":["apps/web/**"],"actor":"param"}'
# ...repeat for app_name=api with ["services/api/**"]

# attach both to the host so target_ref is derived, not copied
curl -s -X POST $API/v1/deployment-hosts/<HOST_ID>/attach \
  -H 'Content-Type: application/json' -d '{"target_id":"<TARGET_ID>"}'

# the map, read from the VM side
curl -s "$API/v1/deployment-hosts?project_id=boss-dogfood" | jq '.hosts[].apps'
```

The resolver itself is covered offline by `tests/test_delivery_multi_app_fanout.py` — 9 cases
including "both apps resolve", scoping, and failing open when changed paths are unknown.

### C. The preview gate (G3) — the flow you actually care about

```bash
# what WOULD approving do? Read-only, safe on any preview.
curl -s $API/v1/previews/<PREVIEW_ID>/gate | jq
```

Expect one of three shapes, all correct:

| `blockers` | Meaning |
|---|---|
| `["merge decision is already 'expired'"]` | already shipped — BugBox/PR #98 looks like this |
| `["preview status is 'expired', not 'running'"]` | regenerate the preview first |
| `[]` with `can_approve: true` | ready; `will_deploy` names the apps |

**A real end-to-end run needs a task whose merge gate is still `pending`.** Start a fresh code
task, wait for the PR, open its preview, and the row will show **Approve & ship** with the
consequence spelled out beside it. Clicking it approves the merge decision → merges → the
existing delivery trigger deploys. It does not deploy directly.

### D. Promotion (G4)

Only meaningful with a second, `production` target. With one VM there is nothing to promote to,
and the endpoint says so rather than inventing a target:

```bash
curl -s -X POST $API/v1/deployments/<RUN_ID>/promote \
  -H 'Content-Type: application/json' -d '{"actor":"param"}' | jq
# → 422 "no active production deployment_target for app 'web'"
```

### E. Offline suites

```bash
PYTHONPATH=engine python3 -m pytest \
  tests/test_preview_approval_gate.py \
  tests/test_delivery_multi_app_fanout.py \
  tests/test_delivery_execute_verdict.py -q          # 18 passed
PYTHONPATH=engine python3 -m pytest engine/tests -q -k shepherd   # 22 passed
cd apps/web && npx vitest run                                     # 365 passed
```

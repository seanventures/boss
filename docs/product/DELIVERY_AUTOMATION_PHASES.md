# Automating delivery — the phased plan to remove every manual step

**Status:** plan
**Written:** 2026-08-06
**Companion to:** `SECRETS_AND_DEPLOYMENT_ONBOARDING.md` (the design), `DEPLOYMENT_CONNECTORS_DESIGN.md` (the connector contract)

---

## 0. The ledger

Two apps were deployed to a real host this week. Everything below is a step a human ran by
hand. A plan that does not name them will design around them and leave them manual, so they are
the specification: **each row is either automated by a phase or explicitly declared human.**

| # | Manual step taken | Owner after this plan | Phase |
|---|---|---|---|
| 1 | `ssh-keygen` a deploy key | BOSS generates it | 2 |
| 2 | Push the public key to the host (Instance Connect) + append to `authorized_keys` | **human — trust bootstrap** | — |
| 3 | Edit `.env` with the key path, restart the API | BOSS resolves per target from a backend | 1 |
| 4 | `ss -lntp` on the box to find a free port | BOSS preflight detects and proposes | 4 |
| 5 | Open the port in the AWS security group | **human by default**, BOSS with opt-in cloud creds | 6 |
| 6 | `sudo mkdir /opt/boss-apps && chown` | BOSS bootstraps the host | 4 |
| 7 | `chown -R` to clear root-owned files | *already fixed in code* | done |
| 8 | `INSERT INTO deployment_targets` by script | Console form + API | 3 |
| 9 | `plan_deployment_run(...)` by script | Automatic on merge; button otherwise | 5 |
| 10 | `curl POST .../execute` | Automatic on merge; button otherwise | 5 |
| 11 | Repeat 8–10 for the second app | Same form | 3 |
| 12 | `gh pr merge` | Existing merge decision path | done |
| 13 | Set `commit_ref` by hand before redeploying | *already fixed — the ref is authoritative* | done |
| 14 | Restart API/worker to load new code | Out of scope: that is deploying BOSS, not with BOSS | — |

**The honest boundary.** Row 2 stays human, and that is not laziness. Installing the first
public key on a host is a *trust bootstrap*: whatever could do it automatically would itself
need pre-existing credentials on that host, so the regress has to stop at a person. Everything
after the first key is BOSS's. Row 5 is human by default for the same reason it was asked about
before opening port 3005 — widening a production firewall is an operator's decision, and the
plan makes it a one-click *request* rather than a silent action.

**Definition of done:** onboarding app number three requires filling in a form, installing one
public key, and approving one firewall change. No shell, no SQL, no `.env` edit, no restart.

---

## Phase 1 — Per-target credentials

*Nothing else can land first: a second host with its own key is impossible today.*

**Problem.** `deployment_targets.secret_refs` is populated on both live targets and **no
connector reads it**. `deploy_service._ssh_key_file()` reads one global setting, so all SSH
targets share one key.

**Build**

- `engine/boss_engine/services/secret_backends.py` (new)
  - `SecretBackend` Protocol: `put/get/list_keys/delete/health`
  - `EnvBackend`, `FileBackend`, `PlatformSecretStoreBackend` (goldeneye ops API), registry +
    `backend_for(ref_kind)`
  - `resolve(connection, secret_ref_id) -> str` — the ONE place a reference becomes a value
- `deployment_connectors.py`
  - `required_secrets()` on the Protocol; SSH declares `ssh_key: ssh_private_key`
  - `validate()` checks the target's binding resolves, instead of checking globals
  - `apply()` passes the resolved key to `deploy_ssh(key_material=...)`
- `deploy_service.py`
  - `_ssh_key_file()` accepts explicit material; global settings become the *fallback* for
    targets with no binding (so today's deployments keep working)
  - materialised keys stay registered in `_EPHEMERAL_KEY_FILES` so cleanup still only deletes
    what BOSS created

**Tests** — a target with its own binding uses THAT key; two targets with different bindings
resolve differently; a target with no binding falls back to the global; a binding that fails to
resolve is a `validate()` problem, not a mid-deploy crash; a resolved secret never appears in
`deployment_runs`, an event, or a plan.

**Proves it:** re-deploy the stock app with its key resolved from a backend, `.env` untouched.

---

## Phase 2 — Secret upload and keypair generation

**Build**

- `POST /v1/secrets` — write-through: value → backend, BOSS stores a `secret_refs` row plus
  fingerprint + last4. No endpoint returns a value.
- `POST /v1/secrets/keypair` — generate ed25519, private half to the backend, response carries
  **only** the public half + the install command.
- `GET /v1/secrets` — metadata only: name, kind, backend, fingerprint, last4, rotation due,
  last verified, bound targets.
- `POST /v1/secrets/{id}/verify` — can the backend resolve it, and does the fingerprint still
  match what the destination reports? Sets `last_verified_at`.
- Console **Secrets** area: list, upload, generate, verify, rotate, revoke. A generated public
  key is shown with a copy button and the exact `authorized_keys` command.

**Tests** — a value is never returned by any endpoint (asserted against the whole route table,
not one handler); a generated private key never appears in a response, log, or event; the
recorded fingerprint matches `ssh-keygen -lf` for the same key.

**Proves it:** generate a fresh deploy key in the console, install the public half, deploy —
never having seen the private key.

---

## Phase 3 — Target onboarding without SQL

**Build**

- `POST /v1/deployment-targets`, `PATCH`, `DELETE` (a `GET` list already exists)
- `config` JSONB per connector, validated by the connector (`app_port` moves here from
  `required_resources`, which was always an odd home for a port)
- `POST /v1/deployment-targets/{id}/preflight` returns problems **with the command that fixes
  each one** — the string an operator can paste, which `validate()` already produces for the
  deploy directory
- Console **Onboard an app** wizard: app → destination → credentials (reuse / upload /
  generate) → health → live preflight → triggers. The form cannot be completed while preflight
  is non-empty.

**Proves it:** onboard a third app entirely in the console; the first deployment succeeds with
no shell.

---

## Phase 4 — BOSS prepares the host

The steps I ran over SSH by hand.

**Build** — `connector.bootstrap(target) -> BootstrapReport`, called from the wizard and
re-runnable:

- create `REMOTE_BASE/<app>` and `data/`, owned by the deploy user
- verify docker present and usable by that user
- **choose a port**: read listeners, exclude taken ones, propose a free one, write it to
  `config.app_port` — this is step 4 of the ledger, and the reason the first deploy failed on
  a port nobody had configured
- report what it changed, and what it could not (`sudo` unavailable → exact command for a human)

Bootstrap is idempotent and never destructive: it creates and verifies, it does not clean up
someone else's files.

**Proves it:** point BOSS at a fresh host with only a key installed; it prepares itself.

---

## Phase 5 — Deployment happens without being asked

**The core gap: a merge plans a deployment and nothing executes it.**

**Build**

- `on_pull_request_merged` enqueues an `execute_deployment` **job** for auto-deploy
  environments (not an inline call — a deploy must not run inside a merge transaction, and it
  needs the worker's lease, retry and heartbeat machinery)
- New job kind `delivery.deploy` in the worker, with the same fencing as code tasks
- `POST /v1/delivery/deploy` — manual: target + ref (default: repo default-branch HEAD),
  returns the plan for confirmation
- `auto_deploy` boolean + **pause** switch on the target
- Console: **Deploy** with a ref picker, and a visible pause control

Every run records `trigger` (`merge` | `manual` | `promotion`), the actor, and the
`decision_id` that authorised it — so "why is this in staging" is always answerable.

**Proves it:** merge a PR; the app is live minutes later with nobody typing anything.

---

## Phase 6 — Safety controls

- **Rollback** from the console to the previous successful run's recorded point (the connector
  verb exists; it has never been exercised in anger)
- **Promotion** to production behind an approval decision, reusing the `ceo`-role gate
- **Firewall requests**: preflight detects an unreachable port and offers a one-click request;
  executing it needs opt-in cloud credentials, and stays a human approval by default
- Auto-rollback on a failed post-deploy health check, using the health samples from the
  monitoring feature rather than a second definition of "unhealthy"

---

## Phase 7 — A second destination

**Cloudflare Pages/Workers** — chosen next because it proves the credential model generalises
from an SSH key to an API token, and the artifact from a static bundle rather than a source
tree. Then Kubernetes, then Terraform.

Not before Phases 1–5 work for two apps. Generalising earlier is how nine deployment tables
ended up empty.

---

## Sequencing and risk

```
1 secrets ──► 2 upload/keygen ──► 3 target CRUD ──► 4 bootstrap ──► 5 auto-execute ──► 6 safety ──► 7 cloudflare
   │                                                                      │
   └── unblocks everything                          the "continuous" in CI/CD
```

Phases 1–3 are additive and safe to ship behind the existing manual path. **Phase 5 changes
behaviour**: merges start deploying by themselves. It ships with `auto_deploy` defaulting to
*off* for existing targets, so nothing begins deploying because a version changed — turning it
on is a deliberate act per target.

Each phase ends with the same test: **onboard and deploy an app using only the console.** A
phase that cannot be demonstrated that way has not landed, whatever its tests say.

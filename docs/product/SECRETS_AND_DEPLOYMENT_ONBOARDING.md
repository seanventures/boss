# Onboarding an app to BOSS delivery — secrets, targets, and triggers

**Status:** design
**Written:** 2026-08-06
**Companion to:** `DEPLOYMENT_CONNECTORS_DESIGN.md` (how an artifact is placed), `CICD_DEPLOYMENT_PLAN.md` (how a merge becomes a deployment)

---

## 1. How a deployment happens today

Worth stating plainly, because it is currently invisible from the console.

```
PR merged
   │
   ├─ decisions.approve  ──►  delivery_pipeline.on_pull_request_merged
   │                             │
   │                             ├─ target_for(project, repo, "staging")   ── none? stop, silently
   │                             └─ plan_deployment_run(...)  ──►  deployment_runs row (status=created)
   │
   └─ (nothing else runs automatically — the row sits there)

execute_deployment(deployment_run_id)
   │
   ├─ preflight(target)        connector.validate — creds, host, docker, deploy dir
   ├─ connector.plan()         read-only; what WOULD change
   ├─ connector.apply(ref)     clone ref → build/copy payload → transfer → docker run
   ├─ connector.observe()      HTTP check against the target's domain  ◄── THE VERDICT
   └─ record status + the sha ACTUALLY deployed
```

Two things follow from that diagram, and both are why the console feels read-only:

- **A merge plans a deployment but does not execute one.** The `deployment_runs` row is created
  and then nothing runs it. Every deployment so far has been executed by an explicit API call.
- **Nothing in the console creates a target.** There is a `GET /v1/deployment-targets` and no
  POST. Both existing targets were inserted by hand with a script.

## 2. What onboarding a second app costs today

Honestly, from having just done it twice:

| # | Step | Where | Automatable? |
|---|---|---|---|
| 1 | Insert a `deployment_targets` row | hand-written SQL | yes — needs an API + form |
| 2 | Pick a host port nothing else uses | read `ss -lntp` on the box | yes — preflight can detect |
| 3 | Open that port in the firewall / security group | AWS console or CLI | operator's call, but should be *told* |
| 4 | Prepare the host: `/opt/boss-apps` writable, docker present | ssh | already detected by `validate()` |
| 5 | Put the SSH key at `BOSS_SSH_HOST_PRIVATE_KEY_FILE` | edit `.env`, restart API | **blocking — see below** |
| 6 | Plan a `deployment_runs` row | hand-written script | yes |
| 7 | Execute it | `curl POST .../execute` | yes — button exists now |

**Step 5 is the blocker.** `deploy_service._ssh_key_file()` reads ONE global setting.
`deployment_targets.secret_refs` exists, is populated, and **is never read by any connector**.
So today every SSH target must share a single key, and onboarding a second host with its own
credential is not possible without editing `.env` and restarting the API.

That is the same defect this codebase keeps producing: **a capability that is declared,
recorded, and not wired to anything.** The design below fixes it first, because every other
piece depends on per-target credentials working.

## 3. Principles

1. **BOSS never stores secret material.** `secret_refs` already encodes this — a row records
   that a secret exists, in which backend, under which key, with `raw_secret_stored: false`.
   That stance is right and this design does not weaken it. What is missing is the ability to
   *put a value in a backend* and to *resolve one at deploy time*.
2. **A secret is write-through and never read back.** The console can set a value and can show
   metadata (kind, backend, fingerprint, last 4, rotation date, last verified). It can never
   display or return the value. An interface that can show a secret will eventually show it to
   the wrong person, and screenshots outlive access reviews.
3. **A connector declares what it needs.** Not prose in a design doc — a machine-readable list
   checked by `validate()` before anything is attempted. A missing credential must cost a query,
   not a build.
4. **The public half of a keypair is not a secret.** BOSS can generate keypairs, keep the
   private half in a backend it never reads back, and hand the operator the public half.
5. **Deployment triggers are explicit and recorded.** Automatic for staging, human-gated for
   production, manual any time — and every one of them leaves a `deployment_runs` row saying
   who or what caused it.

## 4. Secrets

### 4.1 The pieces that already exist

`secret_refs` is a mature registry: scope, kind, backend, status, `rotation_due_at`,
`last_verified_at`, `supersedes_secret_ref_id`, `overlap_expires_at`, `revoked_at`, plus full
CRUD at `/v1/secret-refs`. `ALLOWED_REF_KINDS = {env, vault, local_runner, platform_secret_store}`.

What is missing is everything that makes it *usable*: putting a value somewhere, and getting
one back at the moment of a deploy.

### 4.2 Backends

A backend is a place values actually live. BOSS holds the reference; the backend holds the
material.

| backend | write | read at deploy | good for |
|---|---|---|---|
| `environment` | operator edits `.env` | `os.environ` | today's single-key setup; no rotation |
| `file` | operator writes 0600 file | read at use, never copied | local dev, the current SSH key |
| `platform_secret_store` | `POST /secret` on the goldeneye ops API | `GET /secret?key=` | **the default for anything real** |
| `vault` | later | later | enterprise |

The goldeneye ops API already exposes exactly the right shape — `GET /secrets` (keys only,
values withheld), `GET /secret?key=`, `POST /secret`, `DELETE /secret?key=` — and it is backed
up and off-site. It becomes BOSS's default backend rather than BOSS growing its own vault.

```python
class SecretBackend(Protocol):
    kind: str
    def put(self, key: str, value: str) -> None: ...     # write-through; never returns the value
    def get(self, key: str) -> str: ...                  # deploy-time resolution only
    def list_keys(self) -> list[str]: ...                # keys, never values
    def delete(self, key: str) -> None: ...
    def health(self) -> dict: ...                        # reachable? authorised?
```

`get()` is called at the moment of use and the value is held only for the life of that call.
It is never written to `deployment_runs`, an event, a log line, or a plan.

### 4.3 Uploading a secret

```
POST /v1/secrets
{ "project_id": "...", "scope_type": "deployment_target", "scope_id": "deptgt_...",
  "name": "ssh_key", "secret_kind": "ssh_private_key",
  "backend": "platform_secret_store", "value": "-----BEGIN OPENSSH PRIVATE KEY-----\n..." }

201 { "secret_ref_id": "sref_...", "backend": "platform_secret_store",
      "ref_key": "BOSS_DEPTGT_STOCKS_SSH_KEY",
      "fingerprint": "SHA256:v87GYJ...", "last4": "570", "raw_secret_stored": false }
```

The value goes straight to the backend and is dropped. What BOSS keeps is the reference plus a
**fingerprint** — enough to answer "is this the same key that is on the host?" without holding
the key. For SSH keys the fingerprint is the standard one, so it can be compared against
`ssh-keygen -lf` output on the box.

Uploading, generating, verifying, and binding secrets requires the project's
`integration_manage` grant. A supplied `scope_id` must identify this project or one of its
deployment targets or hosts; a secret reference bound to a target must belong to the same
project.

`GET` never returns `value`. There is no endpoint that does.

### 4.4 Generating a keypair

The access-oriented case, and the one that removes a whole class of operator error:

```
POST /v1/secrets/keypair
{ "project_id": "...", "scope_type": "deployment_target", "scope_id": "deptgt_...",
  "name": "ssh_key", "algorithm": "ed25519", "backend": "platform_secret_store" }

201 { "secret_ref_id": "sref_...", "fingerprint": "SHA256:...",
      "public_key": "ssh-ed25519 AAAAC3... boss-deploy@stock-app",
      "install": "echo '<public_key>' >> ~/.ssh/authorized_keys" }
```

The private half is generated in memory, written to the backend, and never returned or
persisted by BOSS. The public half is returned because it is not a secret — it is the thing the
operator must install on the host, and making them fetch it from somewhere else is how people
end up pasting private keys instead.

This also fixes a real incident from this project: a key generated by hand in `~/.ssh` was
deleted by a bug in the deploy cleanup, twice, and nobody could tell when the original
`kaappu-demo.pem` had gone. A generated key that lives in a backed-up store with a recorded
fingerprint has neither problem.

### 4.5 Rotation

The columns already exist and should be honoured rather than reinvented:
`supersedes_secret_ref_id` + `overlap_expires_at` describe a rotation where both credentials are
valid for a window — which is the only kind of rotation that does not cause an outage. A
rotation is: generate the new secret → install the public half / update the provider → deploy
once with the new ref → revoke the old after the overlap. `rotation_due_at` drives a console
warning; nothing auto-rotates without a human, because a rotation that fails silently locks
BOSS out of a customer host.

## 5. Connectors declare their credentials

Today `SSHContainerConnector.validate()` hard-codes a check of two global settings. Instead:

```python
class DeploymentConnector(Protocol):
    def required_secrets(self) -> list[SecretRequirement]: ...

SecretRequirement = {"name": "ssh_key", "kind": "ssh_private_key", "required": True,
                     "purpose": "authenticate to the host as the deploy user"}
```

| connector | requires | optional |
|---|---|---|
| `ssh` / `container` | `ssh_key` (ssh_private_key) | `known_hosts` |
| `cloudflare` | `api_token` (api_token) | `account_id` (config, not a secret) |
| `kubernetes` | `kubeconfig` (kubeconfig) | registry pull secret |
| `terraform` | cloud creds per provider | state backend creds |

`validate()` then becomes generic: for each requirement, is there a binding on this target, does
it resolve in its backend, and — where cheaply checkable — does it work? That last part matters:
a key that exists but is not on the host's `authorized_keys` passes every check except the
deploy, which is precisely the failure that cost this project an afternoon.

## 6. Target onboarding

A deployment target is the whole answer to "where does this app go":

```jsonc
{
  "app_name": "stock-app",
  "repo": "boss-sandbox",              // which repo's merges feed it
  "environment": "staging",            // staging deploys automatically; production is gated
  "target_kind": "ssh",                // selects the connector
  "target_ref": "ubuntu@3.94.66.139",  // connector-specific address
  "config": { "app_port": 3004 },      // connector-specific, validated by the connector
  "domains": ["3.94.66.139:3004"],
  "health_checks": [{ "kind": "http", "path": "/", "expect": 200, "scheme": "http" }],
  "secrets": [{ "name": "ssh_key", "secret_ref_id": "sref_..." }],
  "auto_deploy": true
}
```

`required_resources` currently carries `app_port`, which works but reads oddly — a port is
configuration, not a resource. A `config` object per connector is clearer, and the connector
validates its own shape.

### The console flow

```
Delivery ▸ Targets ▸ [ + Onboard an app ]

 1. App          name, repo, environment
 2. Destination  connector (ssh | cloudflare | k8s | terraform) → connector-specific fields
 3. Credentials  per required secret: [use existing ref ▾] [upload…] [generate keypair]
                 → generated public keys shown here with a copy button + install command
 4. Health       URL + expected status  (BOSS proposes from the destination)
 5. Preflight    live check: reachable, credential works, runtime present, dir writable,
                 port free — each with the exact command to fix it
 6. Triggers     ☑ deploy automatically when a PR merges to this repo
                 ☐ require an approval decision before each deploy
```

Step 5 is the point of the whole flow. Preflight already returns a precise `problems` list; the
form should refuse to finish while it is non-empty, so an operator never discovers a missing
credential by watching a deployment fail.

## 7. Triggers

Three ways a deployment starts, all landing in the same `deployment_runs` table:

| trigger | when | gate |
|---|---|---|
| **automatic** | PR merged to a repo with an active target for that environment | environment in `AUTO_DEPLOY_ENVIRONMENTS` |
| **manual** | operator presses Deploy, picks a ref | none for staging |
| **promotion** | staging is healthy and someone promotes it | approval decision, `ceo` role |

**The gap to close:** a merge currently *plans* a run and nothing executes it. It should
execute automatically for auto-deploy environments — that is what "continuous" means — and the
plan-only behaviour should remain exactly one thing: the human-gated path.

"Once the PR changes are verified" already has a meaning in BOSS: the governed pipeline's gates
(`existing_tests_intact`, `code_review_passed`, `tests_strong_enough`) plus the merge decision.
Delivery consumes that verdict rather than re-deriving it. A deployment triggered by a merge
records the `decision_id` that authorised it, so "why is this running in staging" is answerable.

### Console controls

- **Deploy** — pick a ref (default: the target's repo default branch HEAD), see the plan, confirm.
- **Redeploy** — same target, new run. *(built)*
- **Retry this run** — same row, for one that failed. *(built)*
- **Rollback** — return to the previous successful run's recorded point.
- **Pause auto-deploy** — a switch on the target, because the first thing anyone wants during an
  incident is for automation to stop helping.

## 8. Order of work

1. **Per-target secret resolution.** Backends + `resolve()`, connectors declare
   `required_secrets()`, `validate()` checks bindings instead of globals. *Nothing else can
   land first — a second host is impossible until this exists.*
2. **Secret upload + keypair generation** (API, then console). Turns onboarding from "edit
   `.env` and restart" into a form.
3. **Target CRUD API + onboarding form**, ending in a live preflight.
4. **Execute on merge** for auto-deploy environments, and a manual Deploy control with a ref
   picker.
5. **Rollback + pause** controls.
6. **Cloudflare connector** — the natural second, and the one that proves the credential model
   generalises beyond SSH keys to API tokens.
7. Kubernetes, then Terraform.

Steps 6–7 stay unbuilt until 1–5 work for two different apps on two different destinations.
Generalising before that is how nine deployment tables ended up empty.

## 9. Deliberately not designed yet

Per-environment secret scoping beyond project/target; secret access audit trails (the events
table already records writes — reads at deploy time are not yet recorded); automatic rotation;
multi-region; canary traffic splitting. All reasonable; none of them useful before an operator
can onboard a second app without a text editor.

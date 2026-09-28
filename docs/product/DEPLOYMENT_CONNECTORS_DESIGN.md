# Deployment connectors — one pipeline, many destinations

**Status:** SSH/container connector **built and proven in production**; others not built
**Written:** 2026-08-05 · **First real deployment:** 2026-08-05

**Community release gate (2026-09-28):** The direct deployment-target bootstrap,
teardown, and purge API actions are disabled. Bootstrap creates host directories over SSH;
teardown removes host resources; purge deletes deployment history. Those endpoints do not yet
run through an approved Resource Plane ChangeSet with the required budget, rollback, and
receipt records. Target creation, configuration changes, and auto-deploy settings remain
available to project members with `providerops_manage`. Re-enable direct host or history
effects only after the policy-guarded workflow and its tests are in place.
The SSH connector also refuses a deployment with no declared `app_port`; it no longer runs
bootstrap implicitly to choose a port. The console labels these host and history controls as
unavailable while this gate is active.
Community SSH/container execution is also disabled at the connector boundary, including
targets with a declared port. The current deployment planner does not create a ChangeSet or
expected ResourceReceipt for those kinds. A policy-guarded release workflow is required to
restore this capability; Community GA must describe it as unavailable until then.
The planner records a blocked deployment run and decision before build or host access, and the
connector independently refuses execution if an older run reaches it.
**Current all-edition gate (2026-09-28):** Enterprise SSH/container planning now makes the same
audited refusal. The direct delivery connector path has no ResourceReceipt recorder or budget
settlement, so an approved ChangeSet by itself does not authorize a connector call. Manual
provider-backed deployment returns a planned run and `changeset_id`; operators approve and
apply that ChangeSet through ProviderOps. Direct execute/redeploy never starts a connector
worker while this gate is active. Local target planning and health checks are unaffected.
The older code-task SSH endpoint still provides a read-only dry run; Community actuation is
blocked there and at the shared `deploy_ssh` helper before any host call.

---

## 1. The shape of the problem

A change has to reach very different destinations: a container on a VM, a static bundle on a
CDN, a workload on Kubernetes, or cloud resources declared in Terraform. Those differ in
almost everything — credentials, artifact, health signal, rollback — and the temptation is a
branch per destination inside the deploy path.

That is the defect this codebase keeps paying for. The preview resolver had one decision
copied into two paths and served the wrong app for weeks. The failure taxonomy was written
twice. Test-oracle logic was inferred two different ways. **A rule implemented per-path drifts
per-path.**

So: **one pipeline, one lifecycle, one set of gates. Connectors differ only in HOW an
artifact is placed and observed.**

## 2. The connector contract

Every connector implements the same five verbs. Nothing else in BOSS knows which one is in
use.

```python
class DeploymentConnector(Protocol):
    kind: str                        # "ssh" | "container" | "cloudflare" | "k8s" | "terraform"

    def validate(target, secrets) -> list[str]:
        """What is missing or wrong, BEFORE anything is attempted. Empty list = ready."""

    def plan(target, artifact) -> Plan:
        """What WOULD change, human-readable. Never mutates."""

    def apply(target, artifact, plan) -> Result:
        """Place the artifact. Idempotent by receipt — a retried apply must not double-apply."""

    def observe(target) -> Health:
        """Is it actually serving? From the destination, not from apply() exiting 0."""

    def rollback(target, to: RollbackPoint) -> Result:
        """Return to a recorded prior state. Recorded BEFORE apply, never derived after."""
```

Four properties are non-negotiable, each because its absence has already cost something here:

- **`validate` runs first and cheaply.** A deployment that fails on a missing credential after
  building an artifact wastes the build. The `provider_account_id` requirement surfaced only
  at execution during the first E2E — that should have been a validate error.
- **`plan` never mutates.** An operator must be able to see what a promotion to production
  would do without doing it.
- **`observe` is the verdict, not `apply`.** A process that started is not a service that
  works. The canary reads `observe`, never the exit code.
- **`rollback` points are recorded before the change.** Derived-after rollback is guesswork
  when the change is what broke the deriving.

## 3. Connectors

A connector also chooses **how the payload runs**, because that is not a property of the
destination but of the app:

- **static** — a servable file tree, run under stock `nginx:alpine`.
- **py** — the app source, run under `python:3.12-slim` with `PORT`/`HOST` injected.

Both ship a directory called `dist`, so staging, rollback and cleanup stay one implementation
and only the container command varies. The mode is not configured; it is **derived from the app**
by `preview_service.resolve_app_entry` — the same resolver the preview uses, so a deployment and
a preview of the same commit can never disagree about which app they are showing.

| kind | artifact | credential | health | rollback | status |
|---|---|---|---|---|---|
| **ssh / container** | dist tree (static) or source (py) | SSH key (file ref) | HTTP check on the domain | previous release dir | **built; live deployment proven** |
| **cloudflare** | static bundle | API token | URL 200 + hash match | previous deployment id | designed |
| **kubernetes** | image + manifests/helm | kubeconfig | rollout status + readiness | `helm rollback` / previous revision | designed |
| **terraform** | plan/apply of HCL | cloud creds | provider state + outputs | prior state file | designed |

**Start with ssh/container.** It is the only one where the destination already exists and the
credential is already in hand, and the whole point of M1 is to make ONE deployment genuinely
happen before generalising. Everything above it is speculation until one works.

## 4. Credentials

Secrets are **referenced, never stored**. `deployment_targets.secret_refs` holds a name and a
reference; the material lives in the operator's secret store.

```json
"secret_refs": [{"name": "ssh_key", "ref": "BOSS_SSH_HOST_PRIVATE_KEY_FILE"}]
```

`BOSS_SSH_HOST_PRIVATE_KEY_FILE` points at a 0600 file and BOSS reads it at use time. It
refuses a group- or world-readable key rather than letting ssh fail obscurely later. A key
pasted into configuration ends up in process listings, crash dumps, and eventually a commit —
the file form avoids all three.

Per-connector references follow the same shape: `CLOUDFLARE_API_TOKEN`,
`KUBECONFIG_PATH`, `AWS_PROFILE`. **No connector receives a secret it did not declare in
`validate`.**

## 5. The registered target

```
stock-app   ssh://ubuntu@3.94.66.139   staging   active
domain      3.94.66.139:3004
secret_ref  BOSS_SSH_HOST_PRIVATE_KEY_FILE  (-> ~/.config/boss/deploy_key)
health      GET / expect 200, scheme http
requires    container_runtime: docker, app_port: 3004
```

Three of those fields were wrong on the first attempt, each in a way worth keeping:

- **The domain pointed somewhere else.** `stocks.local.sean.network` resolves to `100.71.187.10`
  — a different host entirely. BOSS would have deployed to AWS and health-checked goldeneye,
  and a 200 from the unrelated box would have reported the deployment green.
- **The port was a guess.** With no `app_port` declared, the connector defaulted to 8080, which
  another service already held. A default is a guess about someone else's machine.
- **The scheme was assumed.** `observe()` hardcoded `https://`; a staging box reached by IP has
  no TLS. `scheme` is now explicit rather than inferred from the shape of the domain, because an
  inferred rule is invisible until it is wrong.

**BOSS owns the deployment.** The operator supplies the target and the credential reference;
BOSS plans, applies, observes and rolls back. Nothing about this design has a human running a
deploy command — that would make the pipeline a description of what someone did rather than a
thing that happens.

## 6. How a deployment is triggered

Already wired (`delivery_pipeline.on_pull_request_merged`):

```
merge approved → deployment_run created → connector.plan → apply → observe
                       │                                             │
                  staging: automatic                    regression → rollback + incident
                  production: promotion decision (human)  clean → promotion offered
```

`AUTO_DEPLOY_ENVIRONMENTS` is a set, not a flag: adding `production` to it is a visible,
reviewable change rather than a config toggle.

## 7. Order of work

1. **SSH/container connector** implementing the five verbs against the registered target.
   `deploy_service.py` already has plan/build/transfer/health/rollback — this wraps it in the
   contract rather than rewriting it.
2. **Prove one real deployment** — the stock app answering on its domain, deployed by BOSS.
3. **Connector registry** so `target_kind` selects an implementation. Only meaningful once
   there are two.
4. **Cloudflare** — the natural second, since the tracker UI is a static bundle.
5. **Kubernetes**, then **Terraform**.

Steps 3–5 stay unbuilt until step 2 works. Nine empty deployment tables are what happens when
that order is reversed.

**Steps 1–3 are done.** The stock app is live on the AWS host, deployed by BOSS, serving its UI
and its `/api/*` routes from one container; `POST /api/register` returns 201 and
`GET /api/quote/AAPL` returns a price.

## 7a. What the first real deployment taught

Seven distinct defects sat between "target registered" and "app serving". None were visible
from reading the code; each needed the deployment to actually run. Recorded because they are a
class, not a list:

1. **Cleanup deleted the operator's private key.** `_ssh_key_file` returns a referenced key or
   a temp file BOSS made, and cleanup unlinked whichever it got. Every deploy destroyed its own
   credential, and the failure appeared on the NEXT run — which is why the missing
   `kaappu-demo.pem` had no obvious cause. *Cleanup must follow ownership, never the value.*
   Pinned by `tests/test_deploy_key_ownership.py`, which fails against the old code.
2. **A hand-maintained column list stranded a feature.** `execute_deployment` built its target
   from a fixed key list that omitted `required_resources`, so the declared port never reached
   the connector and it silently fell back to 8080. Same defect as `_row_to_profile` dropping
   profile columns. *A projection that must be extended per consumer will eventually not be.*
3. **A precondition was checked against a different path than the deploy wrote.** Preflight
   tested `/opt/boss-apps`; apply wrote `/opt/boss-apps/stock-app`, which was root-owned. Now
   one `REMOTE_BASE` constant and a probe of the actual app directory.
4. **Preconditions the host could answer were discovered by attempting the deployment.** A
   missing directory surfaced only after a build and a transfer. `validate()` now asks the host.
5. **A race read as a busy port.** `docker rm -f` returns before the endpoint is torn down, so
   the immediate `run` failed on "external connectivity" with nothing holding the port.
6. **Truncated errors hid the answer.** The message was cut at 200 chars — one character before
   the port number that explained everything. Twice in this codebase now.
7. **The wrong app deployed, and health said green.** The build picked the first `*/index.html`
   alphabetically in a repo holding three apps, shipping SUDOKU to a stock-app target. A 200
   from the wrong app is still a 200: **health checks verify liveness, never identity.** Only
   choosing correctly can do that, which is why the resolver is shared with preview and fails
   closed on ambiguity.

## 8. What is deliberately not designed yet

Blue/green and traffic splitting; multi-region; cost estimation per deployment; drift
detection between declared and actual. All reasonable, none of them useful before one
container is running on one host.

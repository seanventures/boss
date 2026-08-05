# Deployment connectors — one pipeline, many destinations

**Status:** design; SSH/container connector registered and ready, others not built
**Written:** 2026-08-05

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

| kind | artifact | credential | health | rollback | status |
|---|---|---|---|---|---|
| **ssh / container** | image or dist tarball | SSH key (file ref) | HTTP check on the domain | previous image tag / release dir | **target registered, connector next** |
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
domain      stocks.local.sean.network
secret_ref  BOSS_SSH_HOST_PRIVATE_KEY_FILE
health      GET / expect 200
requires    container_runtime: docker
```

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

## 8. What is deliberately not designed yet

Blue/green and traffic splitting; multi-region; cost estimation per deployment; drift
detection between declared and actual. All reasonable, none of them useful before one
container is running on one host.

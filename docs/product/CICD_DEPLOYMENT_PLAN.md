# From merged PR to production — CI/CD and monitoring

**Status:** plan, pre-implementation
**Written:** 2026-08-04, from a survey of what is actually in the codebase
**Goal:** the stock app running in a real production environment, deployed by BOSS, watched by BOSS

---

## 1. What already exists — verified, not assumed

The machinery is substantial and largely built:

| module | what it does |
|---|---|
| `deploy_service.py` | plan + execute an SSH deploy, build a dist, transfer, roll back |
| `deployment_rollout_engine.py` | staged rollout, observation windows, regression detection, advance/halt |
| `promotion_orchestrator.py` | promote a build between environments |
| `telemetry_connectors.py` | Prometheus, Loki, Grafana, Splunk, Datadog, CloudWatch, PagerDuty, OTel |
| `telemetry_mining.py`, `telemetry_source.py` | ingest and query signals |

Nine tables back it: `deployment_targets`, `deployment_runs`, `deployment_rollouts`,
`deployment_promotions`, `deployment_environment_policies`, `canary_runs`,
`telemetry_sources`, `incidents`, `incident_evidence`.

`deployment_targets` already models what a real target needs — `environment`,
`provider_account_id`, `target_kind`, `target_ref`, `domains`, `required_resources`,
`secret_refs`, `health_checks`, `rollback_points`, `terraform_export`.

## 2. What has never happened

```
deployment_targets      0 rows
deployment_runs         0 rows
deployment_rollouts     0 rows
deployment_promotions   0 rows
canary_runs             0 rows
telemetry_sources       0 rows
incidents               0 rows
```

**Every table is empty, and nothing in the code-task or merge path references the deploy
service at all.** Grep across `governed_code_task_service.py` and `decisions.py` for
`deployment_runs`, `start_deployment`, `deploy_service`: no hits.

So the honest position is: *the parts exist and have never been assembled or run.* That is
the same shape as several defects found on 2026-08-03/04 — `extend_code_task` registered but
structurally unable to run, MCP tools listed without engine ports, a console panel rendered
but hidden by CSS. **Registration is not wiring, and only end-to-end exercise tells them
apart.** This plan therefore optimises for *getting one real deployment to happen*, not for
building more surface.

## 3. The missing link

```
  code task ──► PR ──► merge decision ──► ??? ──► deployment_run ──► rollout ──► canary
                                          ▲
                                    nothing here
```

A merge approval today ends at "merged". Nothing observes that and starts a deployment.

## 4. Proposed wiring

**One trigger, one path, human-gated at the environment boundary.**

```
merge approved (decisions: review_pull_request)
   └─► deployment_run created for env=staging          automatic
         └─► rollout: build → transfer → health check   deploy_service
               └─► canary: observation window            rollout engine
                     └─► telemetry checked               telemetry_connectors
                           ├── regression → automatic rollback + incident
                           └── clean → promotion decision to PRODUCTION  ← HUMAN
                                 └─► same rollout against prod target
```

**Design commitments, each from a lesson already paid for:**

- **Staging is automatic; production is a human gate.** Same reasoning as Tier-2 merge
  approval: an agent promoting its own build to production is what a governance product
  exists to prevent.
- **A deployment is a `deployment_run` row before anything happens**, so a crash leaves a
  record. The code-task pipeline learned this the hard way — state written only at the finish
  line is state lost.
- **Rollback points recorded before the change**, not derived after.
- **The canary's verdict comes from telemetry, not from the deploy exiting 0.** A process that
  started is not a service that works.
- **Every deployment is idempotent by receipt**, like `propose_pull_request` — a retried
  deploy must not double-apply.

## 5. Milestones

**M1 — one real deployment, by hand through the API (½ day).**
Register a `deployment_target` for the stock app, call the deploy path directly, get the app
running on a real host. No automation. This exists to find what is broken *before* wiring it,
because everything above is unexercised.

**M2 — merge triggers staging (½ day).**
The merge decision's follow-on creates a `deployment_run`. Reuses the same
`_handle_approved_follow_on` seam that already re-enqueues a code task on plan approval.

**M3 — canary + telemetry verdict (1 day).**
Observation window after deploy; a `telemetry_source` for the app; rollout advances or rolls
back on the measured signal. Incidents raised with `incident_evidence`.

**M4 — production promotion gate (½ day).**
A Tier-2 decision, exactly like a merge. Promotion runs the same rollout against the prod
target.

**M5 — Task Shepherd covers deployments (½ day).**
New failure classes: deploy failed, health check failed, canary regressed, promotion blocked.
Same ledger, same bounds, same escalation format. This is why the shepherd was built with a
class table rather than a fixed switch.

## 6. The one thing I cannot decide

**Where is production?**

`deployment_targets.target_kind` / `target_ref` supports several shapes, and the choice
changes M1 substantially:

| option | what it needs from you |
|---|---|
| a host over SSH (like goldeneye) | host, user, an SSH key BOSS may use |
| a container host + registry | registry URL, credentials, host |
| Kubernetes (helm charts exist in `deploy/helm/boss`) | kubeconfig, namespace |
| a PaaS (Fly, Render, Railway) | API token, app name |

Also needed regardless: the **domain** the stock app should serve on, and whether staging and
production are separate hosts or separate namespaces on one.

Everything else in this plan I can build without asking. This one is an infrastructure
decision with cost and access implications, and guessing it would waste a milestone.

## 7. What NOT to do

- Do not build M2–M5 before M1 works. Nine tables of unexercised machinery is exactly how we
  arrived at a capability that could not run.
- Do not let a deployment be triggered by anything other than a merge decision. Multiple
  trigger paths is how the preview resolver ended up with two copies of one decision.
- Do not add a second telemetry taxonomy. `telemetry_connectors.py` already defines the
  sources; the deployment path reads them.

# Delivery console — redesign

**Status:** implemented and verified in the browser (2026-08-08)
**Written:** 2026-08-08
**Supersedes the delivery area described in** `CICD_DEPLOYMENT_PLAN.md`

---

## 1. Why the current console fails

Not styling. The deploy endpoint **runs the whole deployment inside one HTTP request** — clone,
build, transfer, docker run, health check — and returns only when it is over, two minutes later.

Everything the operator experienced follows from that:

- **No progress is possible.** Nothing is written down until the end, so there is nothing for a
  UI to show. The spinners I added were animations over an empty data source: they proved the
  browser was alive, not that the deployment was.
- **No logs.** The remote command output is discarded on success and truncated on failure, so a
  failed deploy offers a one-line error and no way to see what the host actually said.
- **No duration.** "How long did it take" cannot be answered without per-step timing.
- **A slow deploy looks like a hang**, and a browser or proxy timeout looks like a failure even
  when the deployment succeeded.

A console cannot be fixed on top of an operation that reports nothing until it finishes. The
redesign is therefore mostly backend.

## 2. The model: a deployment is a sequence of recorded steps

```
deployment_run
  ├─ resolve      which repo, which ref, which app
  ├─ preflight    credential resolves, host reachable, docker present, dir writable
  ├─ build        clone + build/collect the payload
  ├─ stage        keep the previous release for rollback
  ├─ transfer     copy the payload to the host
  ├─ release      start the container
  ├─ observe      is it actually serving?
  └─ finalise     record sha, health, duration
```

Each step records `status` (pending / running / ok / failed / skipped), `started_at`,
`ended_at`, and a **log tail**. Written as the step happens, not at the end — a crash must leave
evidence of where it got to, which is the same reason `deployment_runs` is inserted before
anything is attempted.

Steps are data, not UI. The console renders whatever the run reports, so adding a step later
does not require a console change, and a connector with different steps (Cloudflare, k8s)
renders correctly without special-casing.

## 3. Asynchronous by default

`POST /v1/delivery/deploy` returns **immediately** with a `deployment_run_id`. Execution
continues in the background. The console polls `GET /v1/delivery/deployments/{id}` every ~2s and
draws the stepper.

This is what makes honest progress possible, and it removes the timeout class entirely: the HTTP
request no longer has to outlive the deployment.

`?wait=true` keeps the old blocking behaviour for scripts and tests that want one call.

## 4. Logs

Every step keeps the **last ~4KB** of its output. Enough to see the docker error or the npm
failure, bounded so a runaway build cannot fill the database.

Shown collapsed by default and expanded automatically on the failed step, because the failed
step is the only one anybody opens. Secrets never enter a log: the material is resolved into a
0600 temp file and only the path is ever printed.

## 5. What the console shows

**Deploy is a wizard, not a form.** Three questions, in the order the answers are needed:

1. **What** — repo (picker of repos BOSS knows) and ref (picker, default branch first)
2. **Where** — target, or "onboard a new one"; shows the resolved host, port and URL
3. **Confirm** — a plan of exactly what will happen, then Deploy

Then it switches to the **live stepper**: each step with a spinner while running, a tick and its
duration when done, a cross and its log when failed. Total elapsed at the top.

### The pipeline table shrinks

The current "Pipeline — code task → PR → deployment" lists every code task run that ever opened
a PR — 18 rows, almost all with no deployment, which is noise wearing the costume of a report.

Replaced by **Recent deployments**: the last 10 *deployments*, newest first, with app,
environment, build, trigger, duration and outcome. A code task that never deployed does not
belong in a deployment list; it is already in Code Tasks.

## 6. Autonomous deployment

Already built (`auto_deploy` on merge) but invisible and off. The redesign surfaces it:

- The wizard's final step offers **"deploy automatically when a PR merges to this repo"**.
- Each target shows its trigger state plainly: `auto` / `manual` / `paused`.
- Every deployment records `trigger` (merge / manual / promotion / redeploy) and the run that
  caused it, so "why did this deploy?" is answerable from the row.

Defaults stay off. Turning a target automatic is a deliberate act, not a side effect of an
upgrade.

## 7. What shipped, and what it cost to find

Verified by driving the console in a real browser rather than by reading the build output —
every previous "it works" in this area was a successful compile that nobody had clicked.

Four bugs the drive-through found, all of the same shape (a rule applied on one path):

- **`app_name` was stored with the operator's leading space.** `repo` was trimmed; nothing else
  was. The target could not be found by name and its destroy confirmation could never be typed.
  Now every string on the create payload is trimmed, and confirmations compare trimmed.
- **The branch picker asked GitHub for `/repos//boss-sandbox/refs`.** Targets store `repo`
  bare and the owner in `required_resources`, which a hand-listed projection had dropped. The
  picker rendered no branches and looked like a repo with none. `pipeline_view` now selects `*`
  and unpacks the resources, the same fix `target_for` already had.
- **Redeploy still blocked its HTTP request for a minute.** Deploy was made asynchronous;
  redeploy and retry were not. All three now share one background-start helper.
- **A run whose API process died stayed `running` forever**, so the console polled it
  indefinitely. Startup now closes out abandoned deployments with a reason that says so.

## 8. What this does not change

The connector contract, the credential model, teardown, and the health/monitoring area are
unchanged — they work. This is about the deployment being observable while it happens, and the
console being about deployments rather than about everything adjacent to one.

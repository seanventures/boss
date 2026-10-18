# BOSS Community 1.0.0-rc.10.8 candidate

RC10.8 builds on rc10.7. It came from the D2 demo:
- BOSS built an Order API and deployed it next to HelpDesk AI;
- a faulty release went out and customers complained in HelpDesk;
- BOSS's own alert fired, and BOSS investigated, fixed, verified recovery and posted the root cause back to the customers' tickets.

That run was driven through BOSS's own agents. This is a test candidate.

## What changed

### Agents decide by reasoning, never by keywords

- **Every agent decision goes through one model-decided step.**
  - **What the model is given:** the request, the conversation, the record in view, a project glance and the candidate actions. Each action says whether it is available, and if not, where it is done.
  - **Who decides:** the decision model (Jev, when it is not held), otherwise the project's model.
  - **When unsure:** below the confidence threshold the agent asks one question instead of acting. With no model connected, it says so and does nothing.
  - **Keywords are hints only.** Keywords and coded triage reach the model as hints. They no longer pick an action anywhere: the chat router, step recovery, inbox replies, the terminal or issue triage.
- **The agent battle test** (`scripts/agent-battle-test.sh`): 18 scenarios from real mistakes, 164 phrasings including negative controls. They run against the real agents with every write intercepted, and are graded by a separate model. The score went from 63% to 94%.

### BOSS fixes itself

- When a recovery agent decides a failure is BOSS's own bug, **Mender**, BOSS's maintainer agent:
  - re-diagnoses the bug against the code;
  - writes a test that fails on the current code;
  - writes the smallest fix that passes it.
- The fix must pass every mechanical gate: protected paths and gate code, tests never weakened, the test driving the real entry point, BOSS's affected suites and its P0 tier. Four reviewers then check it, with the security reviewer on a second engine where one is configured.
- **A person approves every install.** A host self-installer runs only signed, single-use tickets and keeps the previous build. The stuck step resumes and verifies the fix. The same failure coming back rolls it back automatically.
- Local fixes survive upgrades, and are dropped once the release contains them.
- **Send to BOSS team** sends exactly what was previewed, redacted, de-duplicated, and labelled `source:boss-agent`.
- **Verified with a real model end to end** on a replay of a real deploy bug: diagnosed, test then fix, every gate and all four reviewers passed, approved, installed, the step resumed, verified. That attempt cost $4.56.

### Incidents for any app BOSS deploys

- **An app's error-rate alert** records the release it fired on. An app can also report a suspected incident through a signed per-app channel (`signals:` in `boss.deploy.yaml`), which opens or joins one incident but can never start a code change.
- **The Incident Response workflow:**
  1. investigates the failing route, the app's logs and the release diff;
  2. builds the fix as the app's next change, with a regression test;
  3. waits for CI;
  4. asks a person to approve the release;
  5. deploys;
  6. verifies recovery from BOSS's own monitoring;
  7. writes a report with the timeline and cost;
  8. posts the root cause back to the reporting app.
- **Respond automatically** on an incident acts under the person's scoped grant.

### App alerts (v1)

- **Every app has an Alerts tab** in Observability, and there is an all-apps Alerts page. Factory's ⋯ › Alerts opens it.
- **Rules are written in PromQL** in the Prometheus alerting-rule shape, and `promtool` proves they mean the same thing to BOSS and to Prometheus. There are five built-ins (error rate, p95 latency, health check, deploy unhealthy, and no traffic, which is off by default). An app's `alerts:` key in `boss.deploy.yaml` can add its own rules.
- **A fired alert shows** its query, the value at breach, a Grafana Explore link, the runbook, and the agent's triage in its own words.
- **Noise control, Alertmanager-style:**
  - a re-firing alert is the same alert (fingerprint dedupe);
  - breaches that share a runbook become one incident;
  - health and deploy alerts hold back the symptom alerts they cause;
  - silences need a reason and are audited.
- **Respond automatically is per rule, never per app.** The incident card's app-wide switch is retired. Its setting moves to the matching rules, under the same person's grant, and the move is audited.
- **Grafana links appear only after BOSS has checked that Grafana answers.** Otherwise the screen says why, and offers Check and Deploy stack.

### Safety

- **A deploy that promises a person's approval always asks for it,** whatever the environment policy allows. The wording on every screen and the runtime come from one source.

### Fixes from the run

- **GitHub:**
  - pushes use the connected GitHub App's one-repository token, which carries `workflows: write` while the repository has opted in;
  - CI is read with the same token.
- **Deploys and Infra:**
  - Infra's Deploy plans a stack deploy from the target's manifest;
  - an incident's evidence and steps always read in one order.
- **Setup:** shows the demo password the deploy actually runs with.
- **Talk to BOSS:**
  - asks for consent instead of showing a raw 409;
  - never claims an action it did not take;
  - carries out a retry on the existing work instead of starting new work.

## Validation

- **Release gate, plumbing:** PASS, 19 of 19 checks in 57 s.
- **Release gate, real agent:** PASS on the build itself. It first ran in 17.6 min, but one check failed because the build profile was recorded 10 min late (fixed here). A re-run built successfully in 25.2 min after two review rounds, 12 s over the 25 min budget, and was not re-run again to save credits.
- **Targeted tests for the last fixes and App alerts:** 60 passed.
- **P0 tier:** engine 489, API 122, web 148, all passed.
- **P1 tier:** engine 531 and web 689 passed. API: 139 passed, with 2 Postgres shared-memory errors in the sharded run that pass when re-run alone.
- **The D2 run itself,** recorded end to end.
- **Fixed before release:** a build's profile is now recorded the moment the run finishes, and a preview never holds the build (a 404 fails in about 15 s, not 10 min).

## Known issues

- **The release-approval card** doesn't yet show the PR, the CI result or the root cause.
- **A fix PR may carry the preview harness's root `package.json` changes.**
- **Alerting:** notifications (email, Slack, webhook), connecting apps BOSS didn't build, SLOs and rollback-first response are the next candidate (BOSSAPP-598 Phase 1).
- **The battle test** has been run on one engine (Claude Code) so far. A Roll back after a removal is the weakest scenario (6 of 9).

## Install and upgrade

Select `v1.0-rc10.8` explicitly with the installer's `--ref` option, from a checkout of that same tag. See the [Quickstart](QUICKSTART.md).

Before upgrading an existing install, take a complete protected backup (`boss upgrade` does this). Changing the checkout back does not undo database migrations.

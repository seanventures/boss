# PRD: BOSS monitoring, alerting and agentic response

**Status:** Draft for approval · **Ticket:** BOSSAPP-598 · **Date:** 2026-10-18 · **Tier:** Strategic wedge
**Companions:** `MONITORING_ALERTING_RESEARCH.md` (the comparison and sources) and `MONITORING_ALERTING_SPEC.md` (how P1 is built). This PRD details **P1**. P2 is a list of one-line ticket candidates.

## 1. Summary

BOSS already builds and deploys apps well. Operations is the gap: watching those apps, telling a person when they break, and responding. Few agentic frameworks aim at it.

This PRD makes BOSS an **open, plug-and-play monitoring and response system** for any app, whether BOSS built it or not:
- It speaks the open standards: PromQL for rules; OTLP, Prometheus and the PagerDuty and Alertmanager webhook shapes for signals.
- It tells a person through email, Slack or a webhook, with ack, and without noise.
- Its agents respond: triage, catching a bad deploy, proposing a rollback, and drafting the postmortem. Each action stays within the person's grant.

The v1 on `feat/app-alerts` (PromQL rules, dedupe, grouping, agent triage, Respond automatically) is the base. P1 adds:
- connecting anything;
- notification;
- SLOs;
- deploy watch;
- mitigate-first response;
- one incident pipeline.

## 2. The problem

BOSS can see a breach today, but it only puts it on a screen:
- **Only BOSS-deployed apps can be monitored.** Rules are tied to a deployment target.
- **Nobody is told.** Alerts appear in the console only. Email and Slack adapters exist, but are not wired to alerts.
- **There are two incident pipelines.** The July "reliability" pipeline (RB-1…20) and the new app alerts both write `incidents`, with different rules. The older one is idle on the live install: 0 sources, 0 pages, 0 remediations.
- **Response jumps straight to a code fix.** Incident Response builds a fix PR, waits for CI, a person's release approval and a deploy. There is no rollback first, so a bad release stays live for the length of a build.
- **There are no SLOs.** The local store keeps 7 days of golden-signal snapshots.
- **Basic noise controls are missing:** ack, flap state, maintenance windows and label silences.

**The evidence:**
- The live install holds one incident: Order API at 6.6% errors, now `diagnosing`, with 20 evidence rows. BOSS found it, investigated it and recorded the release diff and customer reports. No person was told outside the console.
- The Grafana link pointed at a stack that was not answering, until v1 fixed it.

## 3. Positioning: the agentic-ops wedge

What the market offers (RESEARCH §1 and §3):
- **Monitoring tools** (Prometheus, Grafana, Datadog, New Relic) detect, and **paging tools** (PagerDuty, Opsgenie, incident.io) route. Neither fixes anything.
- **AI-SRE entrants** (Resolve.ai, Cleric, Traversal, Bits AI, PagerDuty's SRE Agent, incident.io Investigations, Rootly AI, Seer) are **read-mostly investigators bolted onto someone else's stack**. Their fix, at best, is a PR that a person ships by some other route.
- **Independent benchmarks** put agent RCA success at 13.8% (ITBench SRE) to about 44% (OpenRCA family). Vendors self-report 58–94%.

**BOSS's difference:**
1. **It owns the loop: code → deploy → runtime → fix → deploy → verify.** It knows which commit is serving, what changed, how to roll back, and whether recovery happened, from its own measurements.
2. **Graduated autonomy bound to a person's grant**, re-checked at execution and audited. No vendor makes this portable.
3. **Model-agnostic, with proof.** The same behaviour on every engine, gated by a conformance bench. Nobody else claims this.
4. **Evidence, not assertion.** Every hypothesis and postmortem claim cites a query, log line or commit that BOSS has checked exists.
5. **Open and self-hostable.** OSS core, open formats, no per-seat or per-alert metering. This lands as Opsgenie reaches end of support (2027-04-05) and Grafana OnCall OSS is archived.

## 4. Personas

| Persona | Setup | Job to be done | P1 must give them |
|---|---|---|---|
| **Solo developer on a one-box** (primary) | laptop or one VM; BOSS builds and deploys their apps | "Tell me when my app breaks, fix what you safely can, and don't wake me for noise." | Zero setup for BOSS-built apps; email and Slack; deploy watch; one-click rollback approval |
| **Small team** (2–20 people) | several apps, some not built by BOSS; already uses Sentry or Grafana | "Connect everything in minutes, route to the right person, show us SLOs." | OTel / `/metrics` / URL / webhook onboarding; routing per team; SLOs; ack |
| **Platform team** | an existing Prometheus, Mimir or Alertmanager stack; IaC | "Keep our stack. Add agentic response on top, as code, with audit." | Alertmanager and Grafana adapters; rules as code; grants and audit. Scale-out and BYO TSDB come in P2. |

## 5. Goals and non-goals

**Goals:**
- **(G1)** Any app connected in 5 minutes or less.
- **(G2)** Every real breach reaches a person in under 60 s after it fires.
- **(G3)** Noise at or below 1 in 3 notifications by the end of P1 (§11).
- **(G4)** A bad BOSS deploy is mitigated (rolled back) in under 10 minutes, with one approval.
- **(G5)** One incident pipeline.
- **(G6)** Every agent action within the person's grant, and every action audited.

**Non-goals for P1:**
- Storing logs or traces. They are read or linked where they live.
- Phone and SMS paging, and BOSS-native on-call rotations.
- Anomaly-based paging.
- Status pages.
- A Terraform provider.
- Scale beyond the small-team profile.

## 6. The experience

1. **Connect.**
   - The person opens Observability › **Connect an app** and answers one question: *how does it talk?*
   - The choices: BOSS builds it · OpenTelemetry · it has `/metrics` · just a URL · it already alerts elsewhere (Alertmanager, Grafana, Sentry, Datadog, CloudWatch, or anything that pages PagerDuty).
   - BOSS shows the 2–3 values to paste, waits, and confirms with what arrived: "12 series from checkout-api; HTTP metrics recognised; 3 rules and 2 SLOs proposed".
2. **Accept.**
   - The proposed rules (v1 built-ins) and SLOs (99.5% availability, 99% under 800 ms) are listed. The person accepts or edits them in a form.
   - PromQL is shown with a preview: "would have fired 2 times in the last 24 h".
3. **Get told.**
   - A breach fires. The person gets **one message per incident** by email and Slack, with:
     - what fired;
     - the breached query and its value;
     - the runbook;
     - the Grafana link (only when Grafana answers);
     - the agent's triage when it lands (the message never waits for it).
   - **Ack** works from the email link or the console, and stops repeats.
4. **Deploy watch.**
   - After a BOSS deploy, BOSS watches for 15 minutes. If the new release hurts, the message says so: "6bf2673 raised 5xx from 0.2% to 6.6%; the previous release was healthy".
   - The message carries **Approve rollback**, or the rollback happens automatically if the person pre-approved rollback for that rule.
5. **Resolve and learn.**
   - BOSS verifies recovery from its own measurements and resolves the incident.
   - It drafts a postmortem in which every claim cites evidence. The person edits and publishes it.
   - The case becomes context for the next similar incident.

## 7. Requirements

### P0: v1, ship it (built on `feat/app-alerts`)

| ID | Requirement |
|---|---|
| R0.1 | PromQL rules in the Prometheus shape; 5 built-ins; custom rules; promtool agreement test |
| R0.2 | Fingerprint dedupe, flap reopen, runbook grouping, availability → symptom inhibition, timed per-rule silences (audited) |
| R0.3 | Agent triage via `agent_decision`, recorded word for word; honest "no model" state |
| R0.4 | Per-rule Respond automatically on the switcher's standing grant |
| R0.5 | Alerts tab, Observability › Alerts, rule and silence sheets; Grafana link only when it answers |
| R0.6 | `boss.deploy.yaml` `alerts:` validated at plan time; console edits audited |

### P1: Phase 1, numbered and testable

**Connect anything (G1)**

| ID | Requirement |
|---|---|
| R1.1 | A **monitored service** exists independently of a deployment target. BOSS-deployed targets get one automatically. |
| R1.2 | **Signal sources with per-source tokens.** The project is taken from the token, never from the payload. HMAC with a timestamp where the sender can sign. Rate limits. Rotation with a 1 h grace period. The console shows connected, last seen, accepted and refused counts, and the last refusal in words. |
| R1.3 | A **generic event API** in the PagerDuty Events v2 shape (trigger, acknowledge, resolve, `dedup_key`), so any tool that pages PagerDuty can send to BOSS. |
| R1.4 | **Adapters** for Alertmanager, Grafana, Sentry, Datadog and CloudWatch (SNS). State is mirrored by the source's key. A source that goes quiet auto-resolves after 24 h, and says so. |
| R1.5 | **OTLP/HTTP metrics and Prometheus remote write** ingest, authenticated and stamped with project and service, stored in a bundled Prometheus (spec D1). Traces and logs are refused in words. |
| R1.6 | **Scrape any `/metrics` URL** and run **synthetic HTTP, TCP and DNS checks** and **heartbeats**, all through the egress guard. |
| R1.7 | **Golden signals in both dialects:** the BOSS platform standard and OTel HTTP semconv map to one canonical set that built-ins and SLOs read. |
| R1.8 | A **Connect an app** sheet with a live first-data check for every path. |

**Rules and lifecycle**

| ID | Requirement |
|---|---|
| R1.9 | Evaluation runs on **its own scheduler** (rule groups, leases), out of the health sweep, with no change in v1 behaviour (v1 suite and the agreement test unchanged). |
| R1.10 | **`keep_firing_for`, flap detection** (flapping is notified once, and the agent proposes a tuning) and a per-rule **no-data policy**. By default, no data never resolves a firing alert. |
| R1.11 | **Absence and heartbeat rules.** |
| R1.12 | **SLOs** with SRE Workbook multi-window, multi-burn-rate alerts (14.4× 1h/5m and 6× 6h/30m page; 1× 3d/6h ticket), generated rather than hand-written. They work on the Postgres floor through 1-minute rollups. Budget view: remaining, burn now, time to exhaustion. |
| R1.13 | **Rule editor preview** ("would have fired N times in 24 h") and a **1,000-series cap** with a sentence. |
| R1.14 | **`managed_by` per rule.** Code-managed rules are locked in the console, offer "Propose this change" (a PR to the repo), and show drift. |

**Noise control (G3)**

| ID | Requirement |
|---|---|
| R1.15 | **Label-matcher silences**, **recurring maintenance windows** (RRULE), and **deploy holds**. During a deploy window, alerts are held for the agent, never muted. |
| R1.16 | **Declarative inhibit rules.** v1's built-in becomes one of them. |
| R1.17 | **Storm mode:** more than 10 alerts in 2 minutes are triaged per group, not per alert. |
| R1.18 | Evaluation keeps running while notifications are muted. |

**Notification (G2)**

| ID | Requirement |
|---|---|
| R1.19 | **Contact points:** console, email, Slack and Mattermost webhooks, and a signed webhook. A contact point must be verified before high-urgency use. |
| R1.20 | **Notification policies:** Alertmanager-style tree and timings (30 s, 5 m, 4 h). The rule sheet's Notify column writes simple routes, so most users never see the tree. A **preview** shows where an alert would go. |
| R1.21 | **One message per incident**, updated or threaded. Triage is appended and never awaited. |
| R1.22 | **Urgency:** high notifies at once; low respects quiet hours or goes into a digest. |
| R1.23 | **Ack** from the console and from signed email links. Ack stops repeats, does not silence, and expires (4 h for an alert). |
| R1.24 | **An outbox with receipts and retries.** **Meta-alerts** fire on evaluator lag, a dead datasource, an outbox backlog or the model being down, and go by email as well as the console. A `BossWatchdog` series is exposed for an external dead-man's switch. |

**Agentic response (G4, G5)**

| ID | Requirement |
|---|---|
| R1.25 | **One incident pipeline.** Reliability ingest becomes an adapter. One triage, one lifecycle (detected → acknowledged → investigating → mitigating → monitoring → resolved). |
| R1.26 | **Deploy watch.** For 15 minutes after each BOSS deploy, the agent judges golden signals and burn against the previous release, with statistics as evidence. |
| R1.27 | **Mitigate first.** For a release-correlated incident, the agent proposes **rollback to the last healthy release** (or a restart) before any fix PR. Approval comes by Decision, or by a per-rule pre-approval given with consent. Every action is verified, or undone and the person asked. At most 1 unattended action per service per 15 minutes. |
| R1.28 | **Investigation with cited evidence** (queries, log lines, commits; BOSS checks each one exists), under a budget. Statistical detectors, priors and keyword maps become evidence for the model, never the decider. |
| R1.29 | **Similar past incidents are given to the agent as context** (cases). RB-12's coded threshold is retired. The agent may propose rule changes for a person to apply. |
| R1.30 | **Postmortem draft:** evidence skeleton plus a cited model narrative. BOSS computes the timings. A person publishes. |

**Governance (G6)**

| ID | Requirement |
|---|---|
| R1.31 | **An agent action never exceeds the person's grant.** The grant is re-checked at execution. A revoked grant stops pending actions. Pre-approvals are shown on the rule. |
| R1.32 | **Permissions:** `observability:read` and `observability:manage`, `reliability:respond` and `reliability:manage`. Every config change is audited. Grants, approvals and tier-1 actions also go to the hash-chained audit. |
| R1.33 | **Tenancy:** every query to a shared store gets the project scope injected on the parsed query. A rule cannot read another project's series. |
| R1.34 | **Third-party text** (annotations, logs, customer reports) is data, never instructions. The model's only output is a choice among BOSS's catalogue. |
| R1.35 | **Model-agnostic:** triage, deploy-watch and rollback-proposal fixtures pass on every supported engine (conformance bench in CI). |

**Measurement**

| ID | Requirement |
|---|---|
| R1.36 | BOSS computes and shows MTTD, MTTA, MTTM, MTTR, noise ratio and agent-resolved share per project and service (§11). |

### P2: ticket candidates (one line each, no design yet)

- On-call schedules and escalation wired to policies (reuse `oncall_sre`), with overrides and handoffs.
- Paging: mobile push (ntfy, then a BOSS app), then SMS and voice via the customer's Twilio account.
- Forward to PagerDuty, incident.io and Grafana IRM (Events API out, ack and resolve synced back).
- An interactive Slack app (Socket Mode): ack, resolve, approve, "ask BOSS".
- Anomaly watch: seasonal candidates plus agent judgement. It pages only after measured precision of at least 0.8.
- Log-based rules via Loki; error-tracking alerts from OTel exceptions, grouped by fingerprint.
- Trace storage (Tempo) and trace-aware investigation.
- Composite rules.
- Inhibition derived from knowledge-graph dependencies.
- Bring your own TSDB (Mimir, Thanos, VictoriaMetrics, Grafana Cloud, AMP) with tenant headers; platform scale (50k rules).
- Error-budget policy: an exhausted budget requires release approval.
- Executable runbook blocks (`boss-action`) migrated from `sre_runbooks`.
- Natural-language questions to bounded PromQL (replacing `sre_ask`).
- Multi-step and browser (Playwright) synthetics; multi-region probes.
- Status pages (internal and public).
- A Terraform provider and an `amtool`-style CLI.
- Importers from Opsgenie, Alertmanager and Grafana alert rules.
- Canonical golden signals for DB and messaging semconv.
- A Microsoft Teams contact point.
- More tier-1 actions: scale within bounds, switch a feature flag off.

## 8. Console surfaces (P1)

- **Observability › Connect an app:** the onboarding sheet.
- **Observability › Alerts:**
  - every alert, with filters (service, status, severity, source, flapping, acked);
  - bulk ack.
- **Observability › Incidents:**
  - the lifecycle board;
  - an incident page with the timeline, triage, cited hypotheses, the proposed action with Approve or Refuse, and the postmortem draft.
- **Service page (the app drawer, grown):**
  - golden signals and SLOs with budget;
  - rules, sources and onboarding state;
  - alerts, incidents and deploys overlaid;
  - Dashboard ↗ when Grafana answers.
- **Settings › Notifications:** contact points (with verification), policies with preview, and quiet hours.
- **Design rules:** one surface per job, short labels with tooltips, and a walk at phone width and in dark theme by an independent tester (the console UX rules).

## 9. Governance summary

| Who | May do |
|---|---|
| Viewer (`observability:read`) | see everything in their projects |
| Manager (`observability:manage`) | rules, SLOs, silences, maintenance, sources, contact points, policies |
| Responder (`reliability:respond`) | ack, approve or refuse actions |
| Owner (`reliability:manage`) | response policies and pre-approvals (with consent) |

- **The agent acts only through the catalogue** (P1 tier 1: rollback and restart), only on the incident's one service, and only within the grant of the person who pre-approved or approved, re-checked at execution.
- **Every change and action is audited** with who, when, before and after, and why.

## 10. Multi-tenancy and scale (P1 targets)

- **Tenancy:**
  - every row is project-scoped;
  - ingest tokens are per source and per project;
  - shared-store queries are scope-injected.
- **Scale:**
  - Solo: ≤ 20 services, ≤ 200 rules, ≤ 50k series, inside the existing worker.
  - Small team: ≤ 200 services, ≤ 5,000 rules, ≤ 1M series, 1–2 evaluator loops.
- **Latency (p95):**
  - firing within `for` plus one interval;
  - first notification within `group_wait` + 10 s;
  - external event to notification within 15 s.

## 11. Success metrics

| Metric | Definition | P1 target |
|---|---|---|
| **MTTD** | breach start (the first sample over threshold) → alert firing | ≤ `for` + 2 min |
| **MTTA** | first notification → ack (person or agent triage) | ≤ 5 min (agent triage ≤ 2 min) |
| **MTTM** | incident detected → mitigation verified | ≤ 10 min for a bad BOSS release |
| **MTTR** | detected → resolved | median down 50% against the v1 baseline on the bench scenarios |
| **Noise ratio** | notifications that a person or the agent marked as noise, or that led to no action / all notifications | ≤ 33% by the end of P1, ≤ 15% by P2 |
| **Agent-resolved share** | incidents resolved with no human action except approvals / all incidents | ≥ 50% on BOSS-deployed apps |
| **Triage agreement** | agent triage the person did not override / triaged alerts | ≥ 85% on every supported engine |
| **Customer-first detection** | incidents where a customer signal (HelpDesk) came before BOSS's alert | trending down; reported |
| **Time to connect** | sheet opened → first data confirmed | median ≤ 5 min |

## 12. Phases, acceptance and demos

### Phase 0: ship v1 (P0)
**Acceptance:** the tests in `APP_ALERTS_SPEC.md`:
- a threshold change takes effect at the next breach;
- latency, health and no-traffic rules each fire and clear on a staged breach;
- every fired alert carries a triage;
- the D2 Order API incident appears in its history;
- the browser tester walks the tab, the edit sheet and the all-apps page, at phone width and in dark theme.

**Demo (2 min):** Order API breaks, the alert fires, the triage joins the incident, and Respond automatically starts a fix.

### Phase 1a: connect anything and get told (R1.1–R1.11, R1.13, R1.15–R1.25, R1.31–R1.34)

**Acceptance:**
1. **External OTel app.** A non-BOSS Node app with OTel auto-instrumentation connects with 3 pasted values. The sheet confirms data in under 5 minutes, and built-ins are proposed over semconv metrics. A staged 5xx breach fires.
2. **Alertmanager source.** An Alertmanager pointed at BOSS: a firing alert creates one BOSS alert, and its resolve resolves it. A replayed body, a wrong token, and a body naming another project are each refused with a sentence.
3. **Email and Slack.**
   - A breach produces one email and one Slack message within 60 s, carrying the query, value, runbook and triage (appended when it lands).
   - With the model disabled, the notification still arrives on time and says "not triaged".
4. **Ack.** The email ack link acks (single use). Repeats stop. The ack expires at 4 h.
5. **Flapping.** A flapping series produces one "flapping" notification, not ten, and the agent proposes a tuning.
6. **No data.** A scrape gap during a firing alert does not resolve it.
7. **Maintenance.** A window mutes notifications while the console still shows the alert's state. When it ends with the breach present, BOSS notifies.
8. **Inhibition.** Health check failing holds back error-rate and latency notifications for that service.
9. **Storm.** 30 alerts in 1 minute produce at most 3 triages and 3 notifications.
10. **Tenancy.** A custom rule written to read another project's series returns none.
11. **Unified pipeline.** An alert posted to `/v1/reliability/alert` with a source token lands in the same incident model and triage as an app alert.
12. **Load.** 5,000 rules across 200 services: evaluator lag stays under one interval.
13. **The browser tester** walks Connect, Alerts, notification settings and the routing preview.

**Demo (3 min), "Connect anything":**
1. Connect a stranger's app with OpenTelemetry, and HelpDesk AI's Alertmanager.
2. Break the stranger's app.
3. Slack and email light up with one message, carrying the query, runbook and triage.
4. Ack from the email.
5. Open a maintenance window and show the state still visible.

### Phase 1b: SLOs and mitigate first (R1.12, R1.14, R1.26–R1.30, R1.35, R1.36)

**Acceptance:**
1. **SLOs.** A 99.5% availability SLO on the Postgres floor:
   - a 14.4× burn pages within 5 minutes and clears within 10 minutes of recovery;
   - a 1× burn opens a low-urgency ticket alert;
   - promtool agrees with the generated rules.
2. **Deploy watch.** Deploy a release with a seeded 5xx regression. Within 15 minutes the agent names the release, cites the before-and-after queries, and proposes rollback.
3. **Approve rollback.** One approval rolls back to the last healthy release. Recovery is verified from BOSS's scrapes, and the incident resolves.
   - **MTTM is under 10 minutes.**
   - The fix PR then proceeds through `incident_response`.
4. **Pre-approval.** With rollback pre-approved on the rule, the same scenario rolls back without a person, and the audit shows the grant used.
   - Revoking the grant first stops it, with a sentence.
   - The action on production is refused when the grant covers staging only.
5. **Verify or undo.** A rollback that does not recover is undone, and the person is asked.
6. **Postmortem.** The draft has a cited claim for every sentence. Timings are computed. A planted uncited claim is marked.
7. **Learning.** A second identical incident gets the first one's case as context, and the triage says so.
8. **Model-agnostic.** The triage, deploy-watch and rollback-proposal fixtures pass on every supported engine.
9. **Code-managed rules.** A yaml-declared rule is locked in the console, and "Propose this change" opens a PR against the app's repo.
10. **Metrics.** The page shows MTTD, MTTA, MTTM, MTTR, noise ratio and agent-resolved share for the scenarios above.

**Demo (3–4 min), "BOSS catches its own bad deploy":**
1. HelpDesk AI ships a release with a bug.
2. The deploy watch flags it in minutes.
3. Slack: "6bf2673 raised checkout 5xx from 0.2% to 6.6%. Roll back?"
4. Approve; the app recovers, with the SLO burn chart turning back.
5. BOSS opens the fix PR and drafts the postmortem with citations.
6. The metrics panel shows MTTM under 10 minutes.

Record it from a fresh run, as the demo rules require.

## 13. Risks and open decisions

The risks and the five hardest decisions, each with a recommendation, are in the spec (§11 and §12):
- **D1:** bundled Prometheus 3 plus the Postgres floor.
- **D2:** BOSS evaluates and owns state; external alerts are mirrored.
- **D3:** normalise both metric dialects.
- **D4:** `managed_by` per rule, with a console edit of a code rule becoming a PR.
- **D5:** tiered autonomy, with tier 1 limited to rollback and restart, asking by default, pre-approvable per rule on a grant re-checked at execution.

The one product risk to watch: **do not promise a pager before P2 is proven in a real rotation.** P1 tells people by email and Slack. Teams that need phone paging should keep their pager until then, and P2 adds forwarding to it.

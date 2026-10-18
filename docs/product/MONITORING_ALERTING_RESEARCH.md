# Research: how monitoring, alerting and incident systems work, and what BOSS should take

**Ticket:** BOSSAPP-598 · **Date:** 2026-10-18 · **Companions:** `MONITORING_ALERTING_PRD.md`, `MONITORING_ALERTING_SPEC.md`

**Method.** Vendor documentation, pricing pages, Hacker News (through the Algolia API), engineering blogs and arXiv were all read on 2026-10-18. Every fact below carries its source. Pages without a date were accessed 2026-10-18. Live web search ran out early in the session, so pages were fetched directly. Reddit and G2 refused fetches, which is why the complaints come from HN and blogs. Vendor accuracy claims are self-reported and are labelled that way.

---

## 1. Feature matrix

Key: ● native · ◐ partial, add-on or paid tier · ○ none (integrates elsewhere)

| Capability | Prom + AM | Grafana | Datadog | New Relic | PagerDuty | Opsgenie | incident.io | Rootly / FireHydrant | Sentry | Honeycomb | Uptime Kuma / Better Stack | **BOSS v1** |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Collect metrics | ● pull scrape | ● any datasource | ● agent | ● agent + OTLP | ○ events only | ○ events only | ○ alerts in | ○ alerts in | ◐ errors and spans | ● events / OTLP | ○ probes only | ● scrape + probes |
| Query language | PromQL | per datasource | own | NRQL | ○ | ○ | ○ | ○ | own | own | ○ | **PromQL** |
| Rules as code | ● YAML | ● files + TF | ● TF | ● TF / NerdGraph | ● TF | ◐ | ● TF | ● TF | ◐ | ◐ | ○ / ◐ | ● yaml `alerts:` |
| Pending / for / keep-firing | ● `for`, `keep_firing_for` | ● + Recovering | ◐ eval delay, recovery threshold | ● windows | n/a | n/a | n/a | n/a | ◐ | ◐ | ◐ retries | ● `for`, flap reopen |
| No-data and error as states | ◐ `absent()` | ● NoData, Error | ● No Data | ● signal loss, gap fill | ○ | ◐ heartbeats | ○ | ○ | ○ | ○ | ● push monitors | ◐ rule words only |
| Routing tree | ● | ● policies | ◐ @-handles, teams | ● workflows | ● Event Orchestration | ● policies | ● | ● | ◐ | ◐ | ◐ | ○ console only |
| Group / dedupe | ● group_by, fingerprint | ● | ● multi-alert | ● decisions | ● dedup_key + AIOps | ● alias | ● AI grouping | ● | ● fingerprint | ○ | ◐ merging | ● fingerprint, runbook group, agent merge/split |
| Inhibition | ● | ● | ○ | ◐ | ◐ orchestration | ◐ | ◐ | ◐ | ○ | ○ | ○ | ● built-in, hard-coded |
| Silences / maintenance | ● silences, time intervals | ● mute timings | ● downtimes (RRULE) | ● muting | ● | ● | ● | ● | ◐ | ○ | ● | ◐ per rule, timed |
| Notify / escalate / on-call / ack | ◐ notify only | ◐ IRM is paid | ● On-Call | ◐ | ● | ● (ending) | ● | ● | ◐ notify | ◐ notify | ◐ / ● | ○ console only |
| SLO + burn-rate alerts | ◐ via Sloth / Pyrra | ● Grafana SLO | ● | ● | ○ | ○ | ○ | ○ | ○ | ● burn alerts | ○ | ○ |
| Anomaly detection | ○ | ◐ | ● anomaly, outlier, forecast, Watchdog | ● baseline, outlier | ◐ AIOps | ○ | ◐ | ◐ | ● dynamic thresholds | ◐ BubbleUp (exploration) | ○ | ○ |
| Runbooks | ◐ `runbook_url` convention | ◐ | ◐ | ◐ | ● Runbook Automation (paid) | ◐ | ● workflows | ● | ○ | ○ | ○ | ● on every alert |
| Incident lifecycle + postmortem | ○ | ◐ IRM | ● | ◐ | ● | ◐ | ● AI postmortems | ● AI retros | ○ | ○ | ◐ | ◐ incident + report |
| AI triage / RCA / fix | ○ | ◐ Assistant | ◐ Bits AI SRE | ◐ SRE Agent | ◐ SRE Agent | ○ | ◐ Investigations | ◐ AI SRE | ◐ Seer (PR) | ○ | ○ | ● triage; fix PR → approved deploy → verify |
| Multi-tenancy | ◐ Mimir tenant ID | ● orgs | ◐ multi-org | ◐ accounts | ● | ● | ● | ● | ● | ● | ○ / ● | ● project scope |
| Pricing model | free (OSS) | free / series + users | per host, metric, GB | per user + GB | per user + events | end of sale | per user | per user | per plan | per events | free / per seat | free (OSS) |

Sources for each cell are in §2 to §4.

---

## 2. The systems, briefly

### 2.1 Prometheus + Alertmanager: the standard BOSS already speaks

- **Rules** are YAML groups with one `interval`. An alert has `alert`, `expr`, `for` (time pending before it fires) and `keep_firing_for` (it stays firing after the condition clears, to damp flapping), plus `labels` and `annotations`. The lifecycle is Inactive → Pending → Firing. Recording rules (`record:`) precompute expensive expressions. ([alerting rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/), [recording rules](https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/))
- **Alertmanager:**
  - Routing is a tree, matched depth-first; the first match wins unless `continue: true`, and children inherit settings.
  - Timing: `group_by`, `group_wait` (default 30 s), `group_interval` (5 m) and `repeat_interval` (4 h).
  - `inhibit_rules` (source matchers, target matchers, `equal`), silences, and `time_intervals` for recurring mutes.
  - Source: [Alertmanager configuration](https://prometheus.io/docs/alerting/latest/configuration/).
- **Runbooks:** `runbook_url` is a community convention, not a reserved key ([alerting practices](https://prometheus.io/docs/practices/alerting/)).
- **Tenancy:** Mimir isolates tenants by a tenant ID header ([Mimir tenant IDs](https://grafana.com/docs/mimir/latest/configure/about-tenant-ids/)).
- **Pain:** a cottage industry of guides exists only to explain routing-tree semantics, so the config is hard to get right by hand ([alertkick](https://alertkick.com/blog/alertmanager-routing-tree-examples), [pulsetechops](https://pulsetechops.substack.com/p/alerting-as-code-alertmanager-routing)).

### 2.2 Grafana Alerting, OnCall/IRM and SLO

- **States:** Normal, Pending, Firing/Alerting, **Recovering**, **NoData** and **Error**. NoData and Error can each be mapped to alerting, normal or their own state ([state and health](https://grafana.com/docs/grafana/latest/alerting/fundamentals/alert-rule-evaluation/state-and-health/), [NoData and Error](https://grafana.com/docs/grafana/latest/alerting/fundamentals/alert-rule-evaluation/nodata-and-error-states/)). These states exist because scrape gaps were silently resolving real alerts ([community thread](https://community.grafana.com/t/firing-alerts-get-resolved-with-no-data/146947)).
- **Notification policies:** the Alertmanager tree under friendlier names (contact points, mute timings), with provisioning from files and a Terraform provider ([notification policies](https://grafana.com/docs/grafana/latest/alerting/fundamentals/notifications/notification-policies/)).
- **OnCall:**
  - **OnCall OSS entered maintenance mode on 2025-03-11** ([Grafana docs](https://grafana.com/docs/oncall/latest/set-up/open-source/), [blog](https://grafana.com/blog/grafana-oncall-maintenance-mode/)).
  - Third-party reports say it was archived around 2026-03-24, with push, SMS and voice no longer working for self-hosters ([bigiron](https://www.bigiron.cc/guides/grafana-oncall-is-gone-self-hosted-on-call-in-2026); secondary source).
  - The successor, Cloud IRM, is a paid add-on billed per monthly active user ([Grafana blog](https://grafana.com/blog/oncall-management-incident-response-grafana-cloud-irm/)).
- **SLO:**
  - Burn-rate alerts are built in. You pay for the recorded series ([product page](https://grafana.com/products/cloud/slo/)).
  - **SLO maintenance windows** (2026-07-29) pause budget burn and burn alerts during planned work ([what's new](https://grafana.com/whats-new/2026-07-29-slo-maintenance-windows/)).

### 2.3 Datadog: monitors, Watchdog, Incident Management, On-Call

- **Monitor types:** metric, anomaly, outlier (DBSCAN or MAD), forecast, composite, log, APM, synthetics, SLO and more. States are OK, Warn, Alert and No Data ([types](https://docs.datadoghq.com/monitors/types/), [status](https://docs.datadoghq.com/monitors/status/)).
- **Flap damping** comes from an evaluation delay, recovery thresholds and the choice of window ([notify](https://docs.datadoghq.com/monitors/notify/)).
- **Downtimes** mute notifications but **the monitor keeps changing state**: evaluation and notification are kept apart. They support RRULEs and scoping by tag ([downtimes](https://docs.datadoghq.com/monitors/downtimes/)).
- **Watchdog** learns baselines and runs RCA automatically. Incident Management offers SEV levels, roles and notebook postmortems. On-Call covers schedules, escalation, pages and routing rules ([Watchdog](https://docs.datadoghq.com/watchdog/), [incidents](https://docs.datadoghq.com/service_management/incident_management/)).
- **SLO burn alerts** use a 2 h window with two severities ([SLOs](https://docs.datadoghq.com/service_management/service_level_objectives/)).
- **Price:**
  - Infra costs $15–27 per host per month, and APM $31–48 per host.
  - Logs cost $0.10/GB to ingest, plus $1.70 per million events indexed.
  - Custom metrics cost about $5 per 100 beyond the allotment ([pricing](https://www.datadoghq.com/pricing/), [SigNoz analysis, July 2026](https://signoz.io/blog/datadog-pricing/)).

### 2.4 PagerDuty and Opsgenie: the paging layer

- **PagerDuty services and escalation:**
  - A service is owned by one team. An escalation policy has up to 20 levels, a 30-minute default timeout, and up to 9 repeats.
  - A schedule has up to 60 layers, with overrides and restrictions.
  - Urgency is high or low.
  - Sources: [services](https://support.pagerduty.com/main/docs/services-and-integrations), [escalation](https://support.pagerduty.com/main/docs/escalation-policies), [schedules](https://support.pagerduty.com/main/docs/schedules).
- **Events API v2** takes `event_action` (trigger, acknowledge or resolve), a `dedup_key` and a `payload` with `summary`, `severity` and `source`, up to 512 KB. Change events add context without paging. **Event Orchestration** runs global, then routing, then service rules. AIOps grouping can be intelligent, content-based, unified or time-based ([AIOps](https://www.pagerduty.com/platform/aiops/)).
- **Price:** Professional costs $21 per user per month. The bundles run from $2,800 a year (10 seats, 5,000 events) to $48,000 a year (50 seats) ([pricing](https://www.pagerduty.com/pricing/)).
- **Opsgenie:** dedupe uses a client `alias`, and heartbeats act as a dead-man's switch ([alert API](https://docs.opsgenie.com/docs/alert-api)). **End of sale was 2025-06-04, and end of support is 2027-04-05.** Customers are being moved to Jira Service Management ([Atlassian](https://www.atlassian.com/software/opsgenie)). Every Opsgenie team has to move somewhere within 6 months.

### 2.5 incident.io, Rootly and FireHydrant: lifecycle and learning

- **Slack-native lifecycle:** declare → coordinate → resolve → learn. Roles (lead, responders), a catalog, on-call, alert routing from 40+ sources, and AI postmortems ([incident.io](https://incident.io/incident-response-slack)).
- **Price and pain:** Team costs $15–19 per user, with on-call +$10. HN calls the tier gaps "some of the steepest price cliffs" ([HN](https://hn.algolia.com/api/v1/search?query=incident.io%20pricing)).
- **Rootly:** AI timelines and retrospectives, action items synced to Jira or Linear; incident response at $20 per user, on-call another $20 ([Rootly](https://rootly.com/retrospectives)).
- **FireHydrant** (Plan / Respond / Improve, with runbooks and a catalog) **acquired Blameless** ([TechCrunch, 2024-08-21](https://techcrunch.com/2024/08/21/two-incident-management-startups-join-forces-as-firehydrant-nabs-blameless/)).

### 2.6 Sentry and Honeycomb: errors and SLOs from events

- **Sentry:**
  - Events are grouped into issues by fingerprint (stack trace, type and message), with custom fingerprint rules ([grouping](https://docs.sentry.io/product/issues/grouping-and-fingerprints/)).
  - Metric monitors use fixed, dynamic (anomaly) or percent-change thresholds ([metric alerts](https://docs.sentry.io/product/alerts/create-alerts/metric-alert-config/)).
  - **Seer** finds root causes and drafts a fix PR. It needs a paid plan ([pricing](https://sentry.io/pricing/)).
- **Honeycomb:**
  - The SLI is a boolean per event, and burn alerts reuse Triggers. BubbleUp shows which dimensions are overrepresented in failures ([SLOs](https://docs.honeycomb.io/working-with-your-data/slos/)).
  - SLO count is gated by tier: 0 on Free, 2 on Pro, 100+ on Enterprise ([pricing](https://www.honeycomb.io/pricing)).

### 2.7 The Google SRE Workbook: multi-window, multi-burn-rate

For a 30-day SLO, page or ticket only when **both** windows burn faster than the rate. The long window shows the burn is real, and the short window (about 1/12 of it) lets the alert clear soon after recovery. Example shape: `ratio_rate1h > 14.4*0.001 and ratio_rate5m > 14.4*0.001` ([SRE Workbook](https://sre.google/workbook/alerting-on-slos/)).

| Budget spent | Long window | Short window | Burn rate | Action |
|---|---|---|---|---|
| 2% | 1 h | 5 m | 14.4 | page |
| 5% | 6 h | 30 m | 6 | page |
| 10% | 3 d | 6 h | 1 | ticket |

**Sloth** ([sloth.dev](https://sloth.dev/)) and **Pyrra** ([GitHub](https://github.com/pyrra-dev/pyrra)) generate the recording rules and burn alerts from an SLI plus an objective. They exist because writing this by hand is error-prone.

### 2.8 OpenTelemetry: the collection standard

- **OTLP** runs on gRPC (port 4317) or HTTP (port 4318, protobuf or JSON), with gzip ([OTLP spec](https://opentelemetry.io/docs/specs/otlp/)). OTel standardises collection, not queries.
- **Semantic-convention metrics:**
  - **Stable:** `http.server.request.duration` and `http.client.request.duration` (histograms in seconds; attributes `http.request.method`, `http.route`, `http.response.status_code`, `error.type`), and `db.client.operation.duration`.
  - **Still Development:** the `messaging.*` metrics.
  - Sources: [HTTP](https://opentelemetry.io/docs/specs/semconv/http/http-metrics/), [DB](https://opentelemetry.io/docs/specs/semconv/database/database-metrics/), [messaging](https://opentelemetry.io/docs/specs/semconv/messaging/messaging-metrics/).
- **Naming gap:** BOSS's platform standard (`http_requests_total{method,route,status}`) is the `prometheus_client` convention, not semconv. Both must be understood.

### 2.8a Synthetics: Uptime Kuma and Better Stack

- **Uptime Kuma:** MIT-licensed and self-hosted. It checks HTTP, TCP, keyword, DNS, push (a dead-man's switch) and Docker, offers 90+ notifiers and status pages, and has no SLOs or escalation ([GitHub](https://github.com/louislam/uptime-kuma)).
- **Better Stack** ([pricing](https://betterstack.com/pricing)):
  - 30-second global checks and Playwright transactions.
  - Incident merging and unlimited voice calls.
  - Free for 10 monitors; on-call responder seats cost $29–34.

---

## 3. AI-SRE and agentic-ops entrants

| Product | Claims | Access model | Fix path |
|---|---|---|---|
| Resolve.ai | goes on call for you; investigates and remediates; "5x faster MTTR" (self-reported) ([resolve.ai](https://resolve.ai)) | connects to the existing stack | remediation steps |
| Cleric | "autonomous AI SRE"; $4.3M seed in 2024 ([blog](https://www.cleric.ai/blog)) | **read-only by default** | hands complex fixes to coding tools |
| Traversal | causal model of production; BYOC and BYOM; 75–82% RCA accuracy (self-reported) ([traversal.com](https://traversal.com)) | read-only, no agents | diagnosis |
| PagerDuty SRE Agent | detect, triage, diagnose, "approved remediation"; memory of past incidents ([PD AI agents](https://www.pagerduty.com/platform/ai-agents/)) | inside PD | approved actions |
| Datadog Bits AI SRE | investigates alerts, suggests remediation, writes postmortems ([Bits AI](https://www.datadoghq.com/product/bits-ai/)) | inside Datadog | suggestions |
| incident.io Investigations | adversarial agent pressure-tests hypotheses; "the only change it makes is a pull request" ([incident.io/ai](https://incident.io/ai)) | read + PR | PR a person merges |
| Rootly AI SRE | code changes, telemetry and past incidents; "every change requires explicit human sign-off" ([rootly.com/ai](https://rootly.com/ai)) | read + approved | approved actions |
| Sentry Seer | RCA plus a drafted fix PR ([seer](https://sentry.io/product/seer/)) | Sentry data | PR |
| AWS DevOps Agent | alert → RCA → ticket → page → remediation PR; a 14-minute case study ([AWS blog, 2026-09-28](https://aws.amazon.com/blogs/devops/)) | AWS | PR |
| Azure SRE Agent | multilayer investigation; "choose how autonomous" ([Azure](https://azure.microsoft.com/en-us/products/sre-agent)) | Azure, with an autonomy dial | mitigations within policy |
| NeuBird | "94% accuracy" (self-reported); Suggest / Recommend / Act tiers; stores conclusions only ([neubird.ai](https://www.neubird.ai)) | zero-copy read | approved writes |
| Causely, Deductive, Ciroos | causal graphs, multi-agent hypotheses, human sign-off ([causely](https://www.causely.ai), [deductive](https://www.deductive.ai), [ciroos](https://www.ciroos.ai)) | read-only | diagnosis |

Not verified: Komodor Klaudia's pages refused fetches (HTTP 403), and no AI-SRE product named "Parity" could be found.

**Independent evidence is sobering.**
- **ITBench** (IBM, [arXiv 2502.05352](https://arxiv.org/abs/2502.05352), 2025-02): state-of-the-art agents resolved **13.8% of SRE scenarios**.
- **OpenRCA-family papers** put RCA correctness around 40–44% ([arXiv 2609.19825](https://arxiv.org/abs/2609.19825)). They also find that hallucinated data interpretation and incomplete exploration persist **at every model tier** ([arXiv 2602.09937](https://arxiv.org/abs/2602.09937)).
- A 2026 audit shows pooled leaderboards hide reversals between subsystems ([arXiv 2606.29159](https://arxiv.org/abs/2606.29159)).
- Vendor figures of 58–94% are measured on scenarios the vendors chose themselves.

**The pattern.** Nearly every entrant is a **read-mostly investigator bolted onto someone else's stack**. Its "fix" is at most a PR that a person merges and ships by some other path.

**What none of them do:**
- Own **code → deploy → runtime** as one loop, which is what gives the ground truth of which commit is serving, what changed, and how to roll back.
- **Deploy and verify** the fix from the same system's own measurements.
- Offer **graduated, auditable approval tied to a person's grant**. NeuBird's tiers and Azure's dial come closest.
- Claim **model-agnostic** behaviour with a conformance test.
- Publish accuracy on a **reproducible** benchmark.

---

## 4. What users complain about (and the lesson)

| Complaint | Evidence | Lesson for BOSS |
|---|---|---|
| Bill shock from cardinality and per-product metering | Coinbase's reported $65M/yr Datadog bill ([Pragmatic Engineer, 2023-05-11](https://blog.pragmaticengineer.com/datadog-65m-year-customer-mystery/)); one added tag multiplies series; invoices 2–3× the estimate ([SigNoz](https://signoz.io/blog/datadog-pricing/)) | Cardinality budgets at ingest; show "this rule returns N series" before save |
| Per-seat price creep | New Relic "10x over 5 years" ([HN](https://hn.algolia.com/api/v1/search?query=New%20Relic%20pricing%20per%20user)); incident.io price cliffs | OSS core; never meter alerts or responders |
| Alert fatigue and noise | PagerDuty's AIOps exists to fight it; "approval fatigue" in agent tools ([HN](https://hn.algolia.com/api/v1/search?query=Opsgenie%20sunset)) | Dedupe key, grouping, inhibition, incident-level notifications, agent noise triage, a measured noise ratio |
| Silent false resolution on data gaps | Grafana thread above | No data is a state, not a recovery; `keep_firing_for` |
| Routing that cannot be tested | Alertmanager guides above | Routing preview ("this alert would go to…") |
| Tool sunsets | Opsgenie EOL 2027-04-05; Grafana OnCall OSS archived | An open, self-hostable path; import paths from Opsgenie and Alertmanager |
| AI claims without evidence | ITBench 13.8% versus vendors' 58–94% | Cite evidence for every claim; publish bench results per engine |

---

## 5. The capabilities that matter most, ranked

1. **Accept any signal in the open formats.** That means OTLP, a Prometheus scrape or remote write, and webhooks in the PagerDuty Events v2 and Alertmanager shapes. *Why:* plug-and-play is the adoption gate. Every incumbent wins on integrations, and these four formats reach almost every app and tool.
2. **One alert state machine with pending, firing, resolved, keep-firing, flap and no-data handled honestly.** *Why:* flapping and false resolutions are the top complaints, and states separated from notification are what Grafana and Datadog learned the hard way.
3. **Dedupe, grouping, inhibition and incident-level notification.** *Why:* noise is the reason people stop trusting alerts. A caller dedupe key plus grouping is the proven core (PagerDuty, Opsgenie, Alertmanager).
4. **Notification that reaches a person (email, Slack, webhook) with ack and repeat.** *Why:* an alert seen only in a console is not an alert. This is the largest gap in BOSS v1.
5. **SLOs with multi-window, multi-burn-rate alerts, generated rather than hand-written.** *Why:* it is the best-evidenced way to page on what users feel. Sloth, Pyrra, Grafana and Datadog all converge on it.
6. **Change-aware detection: a deploy watch plus correlation with releases.** *Why:* most incidents follow a change (the premise of RB-17), and BOSS owns the change, so no read-only entrant can match it.
7. **Mitigate first: a governed rollback or restart, with approval.** *Why:* MTTR is dominated by time to mitigate, not time to a fix PR. Entrants stop at suggestions.
8. **Agent triage and investigation with cited evidence, under a budget.** *Why:* independent benchmarks show accuracy is weak, and ungrounded claims are the failure mode. Cited, checkable evidence is what makes an agent's answer trustworthy.
9. **Runbook and the breached query on every alert.** *Why:* the cheapest MTTR win, and the context an agent needs. BOSS v1 already does it.
10. **Silences, maintenance windows and deploy holds by label matcher.** *Why:* planned work must not page, and must not hide a deploy that breaks things (Datadog's lesson: keep evaluating while muted).
11. **Rules and SLOs as code, with a console editor, and a clear owner of the truth.** *Why:* platform teams need GitOps and solo developers need a form. Drift between the two is a known trap (Grafana provisioning).
12. **Graduated autonomy bound to a person's grant, with a full audit.** *Why:* the trust model every entrant gestures at, and nobody makes portable. It is BOSS's identity work applied to operations.
13. **Postmortem drafts from the evidence, with learning fed back as context.** *Why:* incident.io and Rootly show AI postmortems sell. Learning as context keeps the operator's no-keyword-rules principle.
14. **Synthetic checks and heartbeats.** *Why:* the zero-code onboarding path (Uptime Kuma, Better Stack), and the dead-man's switch for jobs.
15. **On-call schedules and escalation (later).** *Why:* required for a team, but commoditised. Opsgenie's end of life creates demand, so integrate first and build after.

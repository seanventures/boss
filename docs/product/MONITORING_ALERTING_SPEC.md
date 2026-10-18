# Engineering spec: monitoring, alerting and agentic response, Phase 1

**Status:** Draft for approval · **Ticket:** BOSSAPP-598 · **Date:** 2026-10-18
**Companions:** `MONITORING_ALERTING_PRD.md` (requirements `R…`; this spec builds its P1) and `MONITORING_ALERTING_RESEARCH.md` (the comparison, with sources).
**Builds on:** the v1 on `feat/app-alerts` (`app_alerts.py`, `promql_lite.py`, `routers/app_alerts.py`, the console Alerts tab; earlier spec `APP_ALERTS_SPEC.md`).

This spec covers **P1 only**. P2 items are listed in the PRD as one-line candidates. It contains shapes, not code.

---

## 1. Where BOSS stands (read from the code and the live DB, 2026-10-18)

**v1 (`feat/app-alerts`, not yet on the live install: no `app_alerts` tables there):**

| Area | What v1 has |
|---|---|
| Rules | PromQL rules in the Prometheus shape (`alert`, `expr`, `for`, `labels.severity`, `annotations.summary`/`description`/`runbook_url`) |
| Built-ins | 5 (error rate, p95 latency, health check, no traffic, deploy health), rendered from bounded params |
| Evaluation | `promql_lite`, which agrees with `promtool test rules` in a test; or the deployed Prometheus when its recorded check says it answers. One evaluator per rule. |
| Alert lifecycle | Prometheus semantics, plus a reopen within 5 min, so a flap is one alert |
| Noise control | Fingerprint dedupe; grouping by `group_key` or runbook into one incident; hard-coded inhibition (availability holds back symptoms); per-rule timed silences, audited |
| Triage | Model triage via `agent_decision` (open, join, move, split, noise, ask), recorded word for word, at most 5 per sweep |
| Respond automatically | Per rule, on the switcher's standing grant, which starts the `incident_response` workflow |
| As code | `boss.deploy.yaml` `alerts:`; console edits win and are audited; `export_rules` |

**Around it (older, live):**

- **`deployment_health`** probes every 120 s, and its `sweep` also drives the scrape, the app signals and the alert sweep: one loop for everything.
- **`app_metrics`** stores one aggregated snapshot per scrape (golden signals only) for 7 days. On live: 5 targets, about 4,500 scrapes, a mean interval of 45 s.
- **`platform_standards`** sets the metrics standard: `http_requests_total{method,route,status}` and `http_request_duration_seconds`.
- **`observability_stack`** deploys OTel Collector → Prometheus (15 d retention) → Grafana per host.
- **`incident_signals`**: signed app reports (the HelpDesk link).
- **The reliability pipeline (RB-1…20)** is live but idle (0 `telemetry_sources`, 0 `oncall_pages`, 0 `remediations`):
  - `POST /v1/reliability/alert` uses **one install-wide HMAC with no timestamp**, and **the payload names its own `project_id`**;
  - `investigation.py` uses hand-set priors;
  - `sre_ask` is a keyword map;
  - RB-12 has a coded "dismissed twice" threshold.
- **On-call, notification and postmortem pieces** exist, but none is wired to alerts: `oncall_sre` (schedules, policies, receipted pages), `email_adapter`, `slack_adapter`, `oncall_notify`, and `sre_postmortem` (evidence-only).
- **Incident Response** (`incident_response` workflow): investigate → build a fix PR → CI → report → **release approval** → deploy → verify recovery → report. It has no mitigation step.
- **Live incidents:** 1 (Order API at 6.6% errors, `diagnosing`, 20 evidence rows).

**The gaps P1 closes:**
- **G1.** Only BOSS-deployed apps can be monitored: `target_id NOT NULL`.
- **G2.** No notification outside the console.
- **G3.** Two incident pipelines.
- **G4.** Monitoring is welded to the health sweep.
- **G5.** No long windows (SLOs), and custom metrics only through Prometheus.
- **G6.** No ack, flap state, matcher silences or maintenance windows.
- **G7.** Response jumps to a code fix, with no rollback first.
- **G8.** Code, not a model, decides in `sre_ask`, `investigation._score` and RB-12.
- **G9.** Ingest security (the shared HMAC and the payload-named project).

---

## 2. Architecture (P1)

```
 any app / tool                                            BOSS
 ───────────────                                           ────
 OTel SDK / Collector ── OTLP/HTTP ──►  /v1/ingest/otlp ──► auth + label stamp ──► Prometheus (bundled, T2)
 Prometheus/Alloy ───── remote write ►  /v1/ingest/prom ──┘                         or host Prometheus (T1)
 /metrics URL ◄──────── scrape (existing app_metrics, egress-guarded) ──────────►  Postgres floor (T0)
 URL ◄───────────────── synthetic check (egress-guarded) ──────────────────────►  probe_* series
 Alertmanager/Grafana/Sentry/Datadog/CloudWatch/PDv2/generic ── /v1/ingest/{source}/{kind} ─┐
 app signals (existing) ────────────────────────────────────────────────────────────────┤
                                                                                         ▼
  rule scheduler (groups, leases) ──PromQL──► datasource ──► ALERT STATE MACHINE ◄── external mirror
                                                                │
                silences / maintenance / deploy hold → inhibit → group → INCIDENT (one model)
                                         │                                   │
                                notification router                      agent loop (jobs)
                          console · email · Slack · webhook     triage → deploy watch → investigate →
                          ack · repeat · urgency · outbox        propose (rollback first) → grant/tier →
                                                                 execute or ask → verify → postmortem
```

**Principles:**
- **One state machine and one incident model.** Sources are adapters.
- **PromQL for rules; OTLP and the Prometheus formats on the wire.**
- **A rule is bound to one datasource; BOSS owns the state.**
- **A floor that needs nothing** (T0, Postgres).
- **The model decides, and BOSS bounds it** (candidates, grant, tier, budget).
- **Monitoring never fails its caller.**

**Topologies.** A project's default datasource is the first available of:
- **T2:** Prometheus bundled with BOSS (a compose `monitoring` profile, with the OTLP and remote-write receivers on).
- **T1:** the host Prometheus from `observability_stack`, asked over HTTP, or over the host channel (read-only `stack_exec`) when only the host can reach it. This fixes the "registered at :3006 but not answering" class.
- **T0:** the Postgres floor (`promql_lite` over scrape snapshots, probes, synthetics, `boss_*` series and SLO rollups).

The console shows which datasource evaluated each alert (v1 `eval_source`).

**Processes:**
- **The monitor scheduler** is new: worker loops for rule groups, synthetics, SLO rollups and the self-check. `deployment_health.sweep` keeps only probing and scraping.
- **The API** serves ingest, CRUD, ack and reads.
- **Jobs** (the existing jobs table) run triage, investigation, notification delivery and actions, so they get leases, retries, metering and governance.

---

## 3. Data model (P1)

Every table carries `project_id`, and every read filters by it.

| Table | New or changed | Key columns |
|---|---|---|
| `monitored_services` | new | `name, environment, deployment_target_id NULL, kg_node_id NULL, owner, labels, onboarding JSONB (paths connected, last data)` |
| `datasources` | new (folds in `telemetry_sources` reads) | `kind (boss_local, prometheus_bundled, prometheus_host, prometheus_remote), base_url, via (http, host_channel), auth_secret_ref, label_scope JSONB, health, checked_at` |
| `signal_sources` | new (absorbs `incident_signals` tokens) | `service_id NULL, kind (otlp, remote_write, alertmanager, grafana, pagerduty_v2, sentry, datadog, cloudwatch_sns, generic, app_signals, heartbeat), token_hash, token_hint (2 chars), mapping JSONB, last_seen_at, accepted_24h, refused_24h, last_refusal_word, enabled` |
| `rule_groups` | new | `name, interval_s (60; min 15), datasource_id, managed_by (builtin, code, console, slo), source_ref, next_eval_at, lease_owner, lease_until, last_duration_ms, last_error` |
| `alert_rules` | `app_alert_rules` renamed + columns | `+ group_id, service_id, kind (threshold, slo_burn, absence, heartbeat, synthetic, external_mirror), keep_firing_for_s, no_data (ok, alert, keep), urgency (low, high), response_policy JSONB, managed_by` |
| `slos` | new | `service_id, name, good_expr, total_expr, objective, window_days (30), burn_policy JSONB, generated_rule_ids` |
| `slo_rollups` | new (T0 only) | `slo_id, minute, good, total`; 35 d retention, about 43k rows per SLO per month |
| `synthetic_checks` | new | `service_id, kind (http, tcp, dns), target, method, expect JSONB, interval_s, headers_secret_ref` |
| `alerts` | `app_alerts` renamed + columns | `+ service_id, source_id, source_kind (rule, external, signal, synthetic, heartbeat), external_ref, flapping, flap_score, acked_by, acked_at, ack_expires_at, urgency, notified_at, suppressed_by JSONB`; the v1 unique open-fingerprint index is kept |
| `alert_transitions` | new, append-only | `alert_id, at, from, to, value, reason, actor (evaluator, source, person, agent)` |
| `silences` | replaces `app_alert_silences` | `matchers JSONB [{name, op, value}], starts_at, ends_at, reason, kind (manual, maintenance, deploy_hold), created_by, expired_*` |
| `maintenance_windows` | new | `name, matchers, rrule, duration_min, timezone, mode (mute, hold)` |
| `inhibit_rules` | new | `source_matchers, target_matchers, equal[], origin (builtin, code, console)` |
| `contact_points` | new | `kind (console, email, slack_webhook, mattermost, webhook), config, secret_ref, verified_at` |
| `notification_policies` | new | `parent_id, position, matchers, continue, group_by[], group_wait_s (30), group_interval_s (300), repeat_interval_s (14400), contact_point_ids[], quiet_hours` |
| `notifications` | new: outbox + receipt | `group_key, contact_point_id, alert_ids[], incident_id, kind (firing, resolved, ack, digest), dedupe_key UNIQUE, status, attempts, next_attempt_at, error_word, sent_at` |
| `incidents` | + columns | `service_id, urgency, lifecycle (detected, acknowledged, investigating, mitigating, monitoring, resolved, dismissed), acked_by, acked_at, mitigated_at, resolved_at, postmortem_id`; `status` stays as a projection for back-compat |
| `incident_actions` | new | `kind (rollback, restart, silence, fix_pr, other), tier, proposed_by, rationale, evidence_refs[], grant JSONB, decision_id, status, executed_at, result, verify JSONB` |
| `incident_cases` | new | `incident_id, symptoms JSONB, cause, actions_worked JSONB, summary, embedding`: what a resolved incident teaches, retrieved as context |

---

## 4. Evaluation engine

### 4.1 Scheduling and scale

- **The unit is the rule group** (Prometheus' model). Its phase within the interval is `hash(group_id) mod interval`, which spreads load.
- **Claiming:** a worker loop claims due groups with `FOR UPDATE SKIP LOCKED` and a lease of 2 × the interval. Add loops or workers to scale, with no coordinator.
- **Datasource queries run outside transactions** (as v1 does). State is written in one short transaction per group, under v1's per-service advisory lock.
- **Writes follow change:** a transition writes an `alert_transitions` row. A steady state is one batched `last_eval_at` update per group.
- **No backfill of missed evaluations.** An overrun is recorded and raises a "slow group" self-alert.
- **Guards:**
  - per-datasource concurrency of 8;
  - a 10 s query timeout;
  - **a result cap of 1,000 series**. A rule over it is refused in words ("returns 4,210 series; aggregate by fewer labels").

| P1 target | Services | Rules | Evals/s (60 s) | Active series | Footprint |
|---|---|---|---|---|---|
| Solo one-box | ≤ 20 | ≤ 200 | ≤ 4 | ≤ 50 k | inside the existing worker |
| Small team | ≤ 200 | ≤ 5,000 | ≤ 85 | ≤ 1 M (T2) | 1–2 loops |

The platform scale (50k rules, external Mimir and similar) is P2. The design does not block it: it is query-bound, and the leases shard.

**Latency targets** (p95):
- a breach to `firing` within `for` + 1 interval;
- `firing` to the first notification sent within `group_wait` + 10 s;
- an ingested external event to a notification within 15 s.

### 4.2 Evaluators

- **`promql_lite`** (T0) gains only what P1 rules need: `absent`/`absent_over_time`, `on`/`ignoring` (for SLO ratios), and `offset` (for deploy-watch baselines). Each ships with a `promtool test rules` agreement case, which is v1's standard.
- **Remote PromQL** (T1/T2) goes through `/api/v1/query`, with `label_scope` injected (§8.3).
- **Recording rules** (golden signals §5.3, SLO windows §4.4) are provisioned to the bundled or host Prometheus as rule files. On T0, BOSS computes them itself into its snapshots and rollups.

### 4.3 State machine (v1's `lifecycle`, extended)

- **Kept from v1:** pending until `for`, then firing; a pending alert that clears is dropped; resolved; the same fingerprint within 5 min reopens.
- **New: `keep_firing_for`.** A firing alert whose series disappears stays firing that long first.
- **New: flap.** `flap_score` is the weighted share of state changes over a fingerprint's last 21 evaluations, with recent ones weighted more.
  - Above 0.5 the alert is `flapping`. It is notified once as flapping, and repeats are held until the score drops below 0.3.
  - The agent is asked once to propose a longer `for` or `keep_firing_for`. A person applies it.
- **New: no data.** It is a rule state (v1's `no_data`/`too_little` words) and by default never resolves a firing alert. The rule's `no_data` setting decides: `keep` is the default for firing alerts, `ok`, or `alert`. This closes the false-resolution failure Grafana documented (RESEARCH §2.2).
- **New: ack.** Ack stops repeats and escalation but **does not silence**. It expires after 4 h by default for an alert, and never for an incident.
- **New: external alerts.** BOSS mirrors the source's trigger, ack and resolve by `external_ref`, with no `for` machine. If a source goes quiet without resolving, the alert auto-resolves after 24 h with the reason "source went quiet".
- **Evaluation keeps running while notification is muted** (Datadog's downtime lesson). Silences and holds act on notification and triage, never on state.

### 4.4 SLOs (multi-window, multi-burn-rate)

The default policy follows the SRE Workbook:

| Severity | Long window | Short window | Burn rate | Budget spent | Urgency |
|---|---|---|---|---|---|
| Page | 1 h | 5 m | 14.4 | 2% | high |
| Page | 6 h | 30 m | 6 | 5% | high |
| Ticket | 3 d | 6 h | 1 | 10% | low |

Each row fires only when both windows exceed the rate. The SLO generates its recording rules (error ratio over 5m, 30m, 1h, 6h, 3d) and three alert rules (`managed_by: slo`, read-only in the editor; you edit the SLO).

- **T0:** window ratios are sums over `slo_rollups` (1-minute good/total, written by the scheduler from the floor's counters). That gives 30-day SLOs on a laptop, which the 7-day snapshots cannot.
- **Low traffic:** with fewer than 100 events in the long window, the alert is held and the rule says "too little traffic to judge".
- **Every BOSS-deployed service** gets two default SLOs, **proposed and off until accepted**: availability (non-5xx / all, 99.5%) and latency (requests under 800 ms / all, 99%). They are built from the golden signals.

### 4.5 P1 rule kinds

| Kind | What it is |
|---|---|
| `threshold` | v1 |
| `slo_burn` | generated from an SLO |
| `absence` | `absent_over_time`, or "no data from this service for N" |
| `heartbeat` | a missed `POST /v1/ingest/{source}/heartbeat` within N (a dead-man's switch) |
| `synthetic` | a threshold over `probe_*` series |
| `external_mirror` | holds routing and response policy for an external alert name |

### 4.6 Self-check

- BOSS exposes an always-firing `BossWatchdog` on its `/metrics`, for an optional external dead-man's switch.
- **Internal meta-alerts:**
  - evaluator lag above 2 × the interval;
  - a datasource not answering for 5 min;
  - an outbox backlog older than 5 min;
  - an ingest refusal spike;
  - the model not answering.
- Meta-alerts go to the install owner by **email and console**, so they never depend on the failing part alone.

---

## 5. Ingestion

### 5.1 Onboarding paths

The console sheet **Connect an app** asks one question: how does your app talk?

| Path | The person does | BOSS does |
|---|---|---|
| **BOSS builds it** | nothing | Injects the platform standard, derives the scrape, creates the service, the built-ins, the proposed SLOs and a deploy watch |
| **OpenTelemetry** (SDK or zero-code agent: Java, Python, Node, .NET) | sets `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer <token>`, `OTEL_SERVICE_NAME` (shown once, copyable) | Accepts OTLP/HTTP metrics; creates the service on first data; maps semconv to golden signals; proposes rules |
| **Has a collector or Prometheus** | pastes a generated `otlphttp` exporter or `remote_write` block | the same |
| **Has /metrics** | gives the URL | Scrapes it through the egress guard (the `app_metrics` path, generalised to external URLs) |
| **Just a URL** | gives the URL and expectations | Synthetic HTTP/TCP checks |
| **Already alerts elsewhere** | pastes the BOSS URL into Alertmanager, Grafana, Sentry, Datadog, CloudWatch SNS, or any PagerDuty Events v2 sender | The adapter mirrors its alerts |
| **Reports customer problems** | declares `signals:` (existing) | `incident_signals`, now writing `alerts` (`source_kind = signal`) |

Every path ends in a **live check**. The sheet waits for the first datapoint or event and says what arrived, for example "12 series from checkout-api; HTTP server metrics recognised; 3 rules proposed". If nothing arrives in 2 minutes, it says what to check.

### 5.2 Endpoints

```
POST /v1/ingest/otlp/v1/metrics      OTLP/HTTP (protobuf or JSON, gzip). Bearer <source token>
POST /v1/ingest/prom/remote_write    Prometheus remote write v1. Bearer <source token>
POST /v1/ingest/{source_id}/{kind}   kind ∈ events | alertmanager | grafana | pagerduty | sentry |
                                       datadog | cloudwatch | heartbeat
POST /v1/apps/{target_id}/signals    existing (kept)
POST /v1/reliability/alert           deprecated alias → generic adapter; needs a source token
GET  /v1/signal-sources ; POST …/{id}/rotate ; POST …/{id}/test   (console)
```

- **OTLP and remote write** are authenticated and stamped at BOSS. The resource attributes `service.name` and `deployment.environment`, and BOSS's `project` and `service_id`, become labels. Any client-sent `project` label is overwritten. The data is then forwarded to the project's Prometheus. Traces and logs return 501 with a sentence ("traces are not stored in this version; link your trace backend on the service page"), never a silent drop.
- **The generic event is the PagerDuty Events v2 shape, plus Prometheus labels and links,** so anything that pages PagerDuty can send to BOSS unchanged. Fields: `event_action` (trigger, acknowledge, resolve), `dedup_key`, `payload` (`summary`, `source`, `severity`, `timestamp`, `custom_details`), `labels`, `links`.
- **Responses:**
  - `202 {alert_id, status: accepted | deduplicated, word}`;
  - a 4xx with a sentence for any refusal;
  - idempotent on `(source_id, dedup_key, event_action, timestamp)`.

### 5.3 Golden signals: one name, many dialects

Built-ins and SLOs read **canonical recording-rule series**. These are produced from whichever dialect the service speaks, which fixes the two-naming-schemes class once and centrally (the generalisation of `app_metrics`' accepted naming variants).

| Signal | BOSS standard (today) | OTel semconv, stable (as Prometheus' OTLP translation names it) |
|---|---|---|
| requests | `http_requests_total{method,route,status}` | `http_server_request_duration_seconds_count{http_request_method,http_route,http_response_status_code}` |
| errors | `status=~"5.."` | `http_response_status_code=~"5.."` |
| latency | `http_request_duration_seconds_bucket` | `http_server_request_duration_seconds_bucket` |

The canonical series are `boss:svc_requests:rate5m{service,route,code_class}`, `boss:svc_errors:ratio_rate5m` and `boss:svc_latency_seconds:p95_5m`. The v1 built-ins are re-rendered against them. Raw series remain usable in custom rules. DB and messaging semconv metrics are accepted as raw series in P1. Canonical rules for them are P2, because the messaging metrics are still at Development stability.

### 5.4 Synthetic checks

- **Kinds:** HTTP (method, headers from a `secret_ref`, expected status, body contains or regex, TLS expiry warning at 14 days), TCP and DNS. The interval is at least 30 s.
- **Network path:** checks run from BOSS **through the egress container**. Public addresses are allowed. Private ranges are allowed only when the address is a registered service on a registered host. Without that rule, synthetics would be an SSRF probe.
- **Second vantage point:** on T1 the host's blackbox exporter also probes, so "down from BOSS, up from the host" separates the network from the app.
- **Output:** series with blackbox-exporter names (`probe_success`, `probe_duration_seconds`, `probe_http_status_code`, `probe_ssl_earliest_cert_expiry`), so one rule works on every topology.

---

## 6. Adapters

Each adapter is a pure function, `(headers, raw body, source) → [NormalizedEvent] | Refusal`. It has contract tests on payloads captured from the tool's documentation, for firing, resolved and refused cases. No adapter calls the network, except SNS subscription confirmation for a source still pending.

| Adapter | `external_ref` | Status from | Service from |
|---|---|---|---|
| Alertmanager webhook v4 | `alerts[].fingerprint` | `status` per alert | `labels.service`, `job` or `app` |
| Grafana alerting | `alerts[].fingerprint` | `status` | labels; keep `generatorURL`, `silenceURL` as links |
| PagerDuty Events v2 / generic | `dedup_key` (generated if absent) | `event_action` | `payload.source` / `labels.service` |
| Sentry (issue and metric alerts) | issue id; rule + project | action | the source's project → service mapping |
| Datadog webhook | `$ALERT_ID` + `$AGGREG_KEY` (BOSS supplies the template) | `$ALERT_TRANSITION` | `service:` tag |
| CloudWatch via SNS | `AlarmArn` + dimensions | `NewStateValue` | alarm tags |
| Heartbeat | the source | ping | the source's service |

The reliability pipeline's parsers (`reliability._parse_alert`) are the starting point for the first three adapters.

---

## 7. Notification

- **Policies** follow the Alertmanager tree: depth-first, first match unless `continue`, children inherit.
  - **The default root:** console, plus email to the project owner, `group_by: [alertname, service]`, 30 s / 5 m / 4 h.
  - **Most users never see the tree.** The rule sheet's "Notify" column (v1's placeholder) picks contact points and writes a one-level child policy.
- **Preview:** `POST /v1/notification-policies/preview {labels}` returns the matched policy, contact points and timings. The console's preview box uses it. The router and the preview share one function, so they cannot disagree.
- **Incident-first:** once alerts join an incident, people get **one message per incident**. Slack and Mattermost messages are updated in place where the webhook supports it; otherwise a threaded follow-up is posted. The agent's triage is appended when it lands, and **notification never waits for triage** (see §11, model risk).
- **Urgency:** high notifies at once and ignores quiet hours. Low respects quiet hours and may go into a daily digest.
- **Channels:**
  - console (existing);
  - **email** (`email_adapter`, SMTP), with signed single-use ack and resolve links valid 24 h;
  - **Slack and Mattermost incoming webhooks** (`slack_adapter`, `oncall_notify`);
  - **a signed generic webhook** (`X-BOSS-Signature: v1=HMAC(token, ts + "." + body)`, `X-BOSS-Timestamp`, the same scheme as the app channel).
- **A contact point must be verified** (a test message acknowledged) before a high-urgency policy may use it.
- **Outbox (`notifications`):**
  - an idempotent `dedupe_key`;
  - backoff up to 6 attempts;
  - a per-contact-point rate limit;
  - a receipt.
  A dead channel never blocks another channel or the evaluator. Failures raise a meta-alert.

---

## 8. Agent loop

**The shape** (the `agent_decision` / `step_recovery` pattern): BOSS gathers read-only evidence, BOSS lists candidate actions, the model decides with reasons and evidence citations, and BOSS applies the grant, tier, budget and its own computed confidence, then executes or asks.

```
alert firing ─► TRIAGE (v1+) ─► incident ─► INVESTIGATE ─► PROPOSE ─► POLICY ─┬─► EXECUTE ─► VERIFY ─► RESOLVE
deploy done ─► DEPLOY WATCH ───────────────────┘                                └─► ASK (decision)
incident resolved ─► POSTMORTEM DRAFT ─► CASE (context for next time)
```

### 8.1 Triage (v1, extended)
- **New candidates:** "page now" and "hold N minutes and recheck".
- **New context:**
  - SLO budget state;
  - the deploy-watch verdict;
  - synthetic results from both vantage points;
  - links from external alerts;
  - the top 3 similar `incident_cases`.
- **Storm mode:** more than 10 alerts in a project within 2 minutes means triage runs once per group, and the message says "storm: 37 alerts in 3 groups".
- **Skipped:** alerts already explained by inhibition or a silence are not triaged. That saves model calls.

### 8.2 Deploy watch (new)
- **Window:** for 15 minutes after every BOSS deploy (configurable), the service's golden signals and SLO burn are compared with the previous release. The baseline is the hour before the deploy, plus the same window the day before (`offset 1d`).
- **Verdict:** statistics are evidence, and the agent's verdict decides. For example: "6bf2673 raised checkout 5xx from 0.2% to 6.6%; previous release healthy; propose rollback".
- **Alerts during the window are held, not muted.** They stay visible, go to the agent first, and notify if the agent says so or if the breach outlives the window. A mute would hide exactly what a deploy breaks.

### 8.3 Investigate (`incident_response.investigate` and reliability, unified)
- **Read-only tools, metered and scoped to the incident's project:**
  - bounded PromQL;
  - redacted container logs (`read_app_logs`);
  - releases and diffs (`release_diff`, `change_correlation`);
  - the knowledge-graph neighbourhood;
  - read-only host state;
  - similar cases.
- **The model plans and reasons.** `investigation.py`'s priors and `telemetry_mining`'s detectors become **evidence labelled "BOSS's statistical guess"**, never the verdict. This closes G8 for investigation. `sre_ask`'s keyword map is replaced: the model writes PromQL, BOSS parses it, injects the scope, bounds it and runs it.
- **Output:** ranked hypotheses. Every hypothesis cites its evidence (a query plus a range, log line ids, or a commit). BOSS checks that each citation exists, as `self_fix` does, and a hypothesis with a broken citation is shown as "unverified".
- **Budget:** 20 tool calls, 10 minutes and the project's cost cap, on the Balanced tier. When it runs out, it reports what it has.

### 8.4 Actions: mitigate first

| Tier | P1 actions | Default | Pre-approvable per rule |
|---|---|---|---|
| 0 | investigate, query, read logs, draft status or postmortem | automatic | n/a |
| 1 | **roll back to the last healthy release BOSS deployed**; **restart the service's containers**; silence a flapping rule for at most 1 h | asks (a Decision) | yes: `response_policy.auto: [rollback, restart]` |
| 2 | fix PR (the existing `incident_response` workflow) | v1 Respond automatically; release approval stays with a person | as v1 |
| 3 | anything else | not offered by the agent | never |

- **The action catalogue is code.** Each action declares its tier, its permission, its scope (exactly one service and environment), how to verify it, and how to undo it. The model can only pick from the catalogue, and BOSS validates every parameter.
- **For a release-correlated incident, rollback is listed first** and the fix PR second. The `incident_response` workflow gains a `mitigate` step before `build`. The fix then proceeds against the rolled-back service without the pressure of an outage.
- **Guards:**
  - after an action, `verify` (the existing recovery check) must pass within N minutes, or BOSS undoes the action and asks;
  - a per-service limit of one tier-1 action per 15 minutes without a person.

### 8.5 Postmortem and learning
- **Draft:**
  - The draft is `sre_postmortem`'s evidence-only skeleton (timeline, impact, verification) plus a model narrative: summary, contributing factors, what went well, action items.
  - **Every claim cites an evidence row.** Uncited claims are marked.
  - The timing numbers (MTTD, MTTA, MTTM, MTTR) are computed by BOSS, never written by the model.
  - A person edits and publishes the draft.
- **Case:** on resolve, an `incident_cases` row (symptoms, confirmed cause, the actions that worked, a summary, an embedding) is written and linked in the knowledge graph (`record_root_cause`).
- **Learning is context, not rules.** RB-12's threshold is retired for this path. Repeated noise verdicts reach the agent as context, and the agent may *propose* a rule change for a person to apply.
- **Accuracy:**
  - triage accuracy is measured against the person's corrections;
  - the `sre_bench` fixtures are replayed **on every supported engine** via the scripted conformance engine;
  - a drop below threshold fails CI.

---

## 9. Security and governance

1. **Per-source tokens.**
   - 256-bit random; only the hash is stored; the console shows existence and a 2-character hint, never more.
   - On rotation the old token stays valid for 1 h.
   - **The project comes from the token, never from the payload** (closes G9).
2. **Signing:**
   - HMAC with a ±5 min timestamp and a 10-minute nonce cache where the sender can sign (generic, app signals, outbound webhooks);
   - Bearer over TLS where it cannot (OTLP, remote write, Alertmanager, Grafana, Datadog);
   - SNS signature verification for CloudWatch.
3. **Limits:**
   - body size: 1 MB, or 4 MB for OTLP;
   - 600 events per minute per source;
   - a per-project active-series budget (default 50 k on T0/T2), enforced at the stamping proxy and refused in words.
4. **Tenancy in queries.** Every PromQL that BOSS sends to a shared store gets the datasource's `label_scope` (for example `project="…"`) injected into **every selector, on the parsed AST** (`promql_lite.parse`), never as text. This applies to rules, agent queries and Explore links alike. A construct the parser does not know is refused on shared stores. A custom rule cannot read another project's series.
5. **Untrusted text reaching a model.** Annotations, log lines, customer reports and payload details are third-party data. They are passed as delimited evidence and never as instructions. Instruction-shaped strings are flagged (`self_fix.injection_markers`) and lower BOSS's confidence. **The model's only output is a choice among BOSS's candidates.** An injected "roll back prod" cannot widen the catalogue, the service scope or the grant.
6. **Grants: the agent never exceeds the person.**
   - Each `incident_actions` row carries `{on_behalf_of, grant_id}` (v1's `respond_link`).
   - The authorizer re-checks the action's permission and scope against that grant **at execution**.
   - Revoking or narrowing a grant stops pending actions, with a sentence.
   - Pre-approval is per rule and per action class, given once with consent (v1's 409 flow), and shown on the rule ("auto: rollback · granted by Param, Oct 18").
7. **Permissions** (they exist in `permissions_gen.py`):
   - `observability:read`: view;
   - `observability:manage`: rules, SLOs, silences, sources, contact points, policies;
   - `reliability:respond`: ack, approve actions;
   - `reliability:manage`: response policies.
8. **Audit:**
   - every change to rules, SLOs, silences, policies, sources and contact points goes to `monitoring_changes` (the renamed `app_alert_rule_changes`);
   - grants, approvals, tier-1 and higher actions, and token rotations also go to the hash-chained identity audit.
9. **Secrets** are stored by `secret_ref` only. They are never returned or logged. Diagnostics print presence and shape only.

---

## 10. Migration from v1, and what to reuse

v1 is not on the live install yet, so the migration is code-first. It is still written to be safe for any install that ran v1.

1. **Tables.**
   - Rename `app_alert_rules` → `alert_rules` and `app_alerts` → `alerts`, and add the new columns. `target_id` stays.
   - Backfill `service_id` from one `monitored_services` row per target.
   - Each `app_alert_silences` row becomes a matcher silence `{alert_rule_id="<id>"}`.
   - Rename `app_alert_rule_changes` → `monitoring_changes`.
   - Compatibility views keep the old names for one release.
2. **API.** The v1 routes stay, served by the new tables. `/v1/reliability/alert` becomes the deprecated alias. Its shared secret is accepted for one release only where it is set (it is unset on prod), and is then removed.
3. **Scheduler.** `app_alerts.sweep`'s per-target work runs from the group scheduler instead of `deployment_health.sweep`. The gate is v1's suite and the promtool agreement test, **unchanged**.
4. **Built-ins** are re-rendered over the canonical series. The agreement test gains fixtures in both dialects.
5. **Reliability.** `ingest_alert` becomes the generic adapter plus the unified triage. `reliability_policies` maps to a project default `response_policy`. Incident ids and dedupe keys are kept.
6. **`boss.deploy.yaml`.** `alerts:` stays. New optional keys: `slos:`, `synthetics:` and `notify:` (contact hints). A `response:` key declares wished-for pre-approvals, which still require a person's consent before they apply.

| Reuse as is | Extend | Redo |
|---|---|---|
| PromQL, the rule shape, `export_rules`, yaml `alerts:`; `promql_lite` and its agreement test; `lifecycle`, fingerprint, the one-open-alert index, reopen-on-flap; `agent_decision` triage and its word-for-word record; Respond automatically on a grant; `incident_response` investigate, release diff and recovery checks; `observability_stack` and the Grafana link checks; `email_adapter`, `slack_adapter`; `sre_postmortem` skeleton; the console Alerts tab and page | `promql_lite` (`absent`, `on`/`ignoring`, `offset`, AST scope injection); the state machine (keep-firing, flap, no-data, ack, mirror); `local_store` (+ SLO rollups); triage (storm, context, page/hold); grant checks at execution | **Scheduling** out of the health sweep; **silences** per rule → matchers; **inhibition** hard-coded → declarative; **reliability ingest** shared HMAC → per-source tokens; **`investigation.py` priors, `sre_ask` keywords, RB-12 threshold** → evidence and context for a model |

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| Sliding into building Datadog (log and trace storage) | P1 stores metrics in Prometheus and events in Postgres. Logs are read where they live. Traces are linked. Stores for them are P2 candidates, not commitments. |
| Model cost in a storm | Storm mode, v1's per-sweep cap, a per-incident budget, the Balanced tier, and no triage for alerts already explained by inhibition or silences |
| **Model down during an incident** | Notification never waits for triage. High urgency notifies at `group_wait`, and triage is appended when it lands. With no model, the alert says so (v1) and routing runs on labels. The model decides actions; it never gates telling a person. |
| A wrong agent action | Catalogue-only actions, one-service scope, the grant re-checked at execution, verify-or-undo, and the per-service action limit |
| Two evaluators disagreeing | One datasource per group; the promtool agreement test for every new `promql_lite` feature |
| A laptop BOSS that cannot be reached | Pull paths (scrape, signal polling), T1 host-channel queries, and the documented tunnel for webhooks |
| Cardinality blow-up | The series budget at the stamping proxy; the 1,000-series result cap; the platform standard's label rules; a top-series report on the service page |
| Undelivered notifications | Verified contact points, a second channel for high urgency, outbox receipts and the meta-alert |

---

## 12. The hardest open decisions (P1)

**D1. Where do metrics live?**
- **Options:**
  - (a) Postgres only;
  - (b) require each app host's Prometheus;
  - (c) bundle a Prometheus-compatible store with BOSS;
  - (d) bring your own only.
- **Recommendation:** tiered, with **(c) bundled Prometheus 3 as the default** whenever BOSS is reachable. The **Postgres floor stays** for laptops. The datasource interface keeps (d) open for P2.
- **Why Prometheus and not VictoriaMetrics:** Prometheus 3 has the OTLP and remote-write receivers, is Apache 2.0, and is the engine the promtool agreement test pins. VictoriaMetrics is more efficient, but MetricsQL differs from PromQL at the edges, and v1's bet is one semantics.

**D2. Who evaluates rules and owns alert state?**
- **Options:**
  - (a) BOSS queries and owns the state (v1);
  - (b) the store's ruler plus Alertmanager webhooks into BOSS;
  - (c) per rule.
- **Recommendation:** (a) for every rule BOSS manages, with external alerts mirrored. Owning the state is what makes ack, flap handling, holds, the incident link and the agent loop coherent. Teams that want their own Prometheus to evaluate use `export_rules` and the Alertmanager adapter, and it is still one record in BOSS. Recording rules go to the store's ruler, because that is computation, not state.

**D3. Metric naming: the BOSS standard or OTel semconv?**
- **Recommendation:** normalise both into canonical `boss:*` recording rules now (§5.3). Leave BOSS-built apps on today's standard until the stack defaults to OTel's Prometheus translation, then move new apps to semconv. External OTel apps work on day one, nothing already built changes, and the class is fixed once.

**D4. Rules as code or console edits: which wins?**
- **Options:**
  - (a) the console always wins (v1);
  - (b) code wins and the console is read-only (Grafana provisioning);
  - (c) `managed_by` per rule.
- **Recommendation: (c).**
  - A console rule applies at once.
  - A rule declared in the repo is `managed_by: code`, and is shown locked with "edit in boss.deploy.yaml". It has a **"Propose this change"** button that opens a PR to the repo. BOSS holds the repo, so this is cheap.
  - Drift between the yaml and the running rule is shown.
  - One source of truth per rule; nobody loses the console.

**D5. How far does the agent act on its own in P1?**
- **Options:**
  - (a) read-only, the AI-SRE norm (RESEARCH §3);
  - (b) act within policy on anything;
  - (c) tiers, with per-rule, per-action pre-approval checked against the person's grant at execution.
- **Recommendation: (c), with tier 1 limited to rollback and restart and defaulting to *ask*.**
  - Tier 0 is always on.
  - Tier 2 is v1 Respond automatically.
  - Tier 3 is never offered.
- **Why it is safe:** BOSS deployed the release it rolls back, knows the last healthy one, and verifies recovery from its own measurements. No read-only entrant can make that claim.

(Decided rather than open, and built into this spec: **unify the reliability pipeline with app alerts in P1** (G3). The RB pipeline is idle on live, so the cost is lowest now, and two pipelines writing `incidents` is the "two ways to ask one question" defect `deployment_health`'s docstring names.)

---

## 13. Tests (acceptance per milestone is in the PRD)

- **Semantics:** the promtool agreement test grows with every `promql_lite` feature, built-in and generated SLO rule, in both naming dialects.
- **Adapters:** a captured real payload per tool and status, plus refusals: bad signature, replay, wrong or rotated token, oversize, and a payload naming another project.
- **State machine:** property tests over random series:
  - nothing fires before `for`;
  - one open alert per fingerprint;
  - no data never resolves a firing alert under `keep`;
  - ack never silences;
  - flap score stays in bounds.
- **Routing:** table tests over the policy tree, through the same function the preview API calls.
- **Tenancy:** a custom rule and an agent query against a shared store, each written to read another project, return nothing of that project.
- **Agent:** `sre_bench` fixtures (triage, deploy watch, propose rollback) on every engine via the scripted conformance engine, with an accuracy floor.
- **E2E release gate:** Order API / HelpDesk with a bad release:
  - the deploy watch flags it;
  - an alert fires and an email plus a Slack message arrive with the triage;
  - the email ack link works;
  - rollback is proposed and approved;
  - recovery is verified;
  - the incident resolves;
  - a postmortem draft appears with citations.
  Then the same flow with an external OTel app, and with an Alertmanager source.
- **Load:** 5,000 rules over 200 synthetic services on the small-team profile; evaluator lag and the Postgres write rate measured against §4.1.

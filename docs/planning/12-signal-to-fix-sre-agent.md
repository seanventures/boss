# 12 — AI SRE for Autonomous Oncall: implementation design

> **Status:** BUILD SPEC — approved for build (extends spec 11). **Owner:** paramg. **Date:** 2026-07-21.
> **Reframe (2026-07-21):** repositioned from "Signal→Fix / data-mining" to **AI SRE for autonomous
> oncall**, benchmarked against **Cleric** (cleric.ai / docs.cleric.ai). Heavy log/trace *data-mining* is
> out of scope; we keep **anomaly detection over integrated metrics (Grafana/Prometheus) + logs** as the
> SRE signal layer. The BOSS **engine stays generic** — autonomous oncall is one use case it builds, not a
> bespoke product. Emphasis added on **autonomy**: autonomous investigation, verification, and a governed
> auto-remediation spectrum. See **§1.5** for the Cleric gap analysis.
> **Decision:** build the flagship **AI SRE agent** at the **system level** in BOSS, as the **enterprise
> sales wedge**, with **out-of-the-box connectors** (skills/MCP) for the popular observability tools and a
> **BOSS-native learning loop**.

> **The pitch (one line):** *Connect your Grafana / Datadog / Splunk + your repo → BOSS autonomously
> investigates production alerts, finds root cause with confidence, ships a reviewed & tested fix PR, and
> verifies recovery — then learns your systems. Self-hosted, model-agnostic, governed.*

---

## 1. What we're building

A durable, system-level agent that closes the incident loop:

```
telemetry (logs · metrics · traces)  → detect anomalies (hybrid) → cluster into named INCIDENTS
   → RCA against the customer's repo → GOVERNED remediation PR (code pipeline / IaC lane)
   → capture the outcome → LEARN (native loop, gets smarter per incident)
```

Three build pillars, each a real subsystem:

1. **Connector layer** (§2) — out-of-the-box, standardized skills/MCP for the popular tools. The enterprise
   on-ramp: connect in minutes with presets.
2. **Reliability pipeline** (§3) — durable job flow + data model + APIs + console, mirroring the `code.task`
   architecture we already run.
3. **Native learning loop** (§4) — generalize the VAG ratchet (#107) into a first-class `LearningLoop`
   primitive that *any* BOSS agent uses; the SRE agent is its first heavy consumer.

Everything downstream of "named incident" reuses shipped machinery: APP-5 retriever (RCA), the governed code
pipeline + Plane bug-framing (remediation), the budget governor + pacing (cost), decisions/gates
(governance), the skills framework + MCP registry (connectors).

---

## 1.5 Benchmark vs Cleric (cleric.ai) — match, win, and the gaps we're closing

Cleric is the closest competitor: an **AI SRE that autonomously investigates production alerts**. It's the
right yardstick. Their shape (from cleric.ai + docs.cleric.ai): read-only agent deployed in the customer
VPC; ingests PagerDuty/Slack alerts and **groups related alerts into one investigation case**; a
**reasoning engine** (plan → execute → reflect) that **branches multiple hypotheses in parallel** and
"tests causation, not correlation"; a **layered knowledge graph** of infra (deterministic layers via
`kubectl`/cloud walk + fuzzy inferred service-dependency layers); **confidence-scored** ranked hypotheses
with evidence; **read-only by default**, autonomous only for *routine* fixes (config flips, env reverts,
line patches), otherwise **hands off to Claude Code / Cursor**; **production verification** (re-watch the
signal, 30-min no-recurrence window); **continuous learning via LangSmith** (compare strategies, feedback
API on traces, a knowledge hierarchy that strips env-specifics into generalized patterns). ~20 read-only
integrations (Grafana/Prometheus/Datadog/New Relic/Sentry, Splunk/Elasticsearch, k8s/EKS, AWS/GCP,
PagerDuty, GitHub, Unleash…), plus an MCP server exposing investigations.

### Where BOSS already **wins** (lean into these — they are the wedge)
| Dimension | Cleric | BOSS |
|---|---|---|
| **Remediation depth** | Autonomous only for *routine* fixes; complex work is **handed off** to Claude Code/Cursor | **Remediation-as-code is the product**: incident → bug-framed governed code task → **tested, review-paneled, fail-closed gated PR**. We *are* the coding agent Cleric hands off to. |
| **Governance of autonomy** | Read-only + human accept/reject | VAG + review panel + decision gates + budget governor make autonomous action **safe by construction**, with receipts/audit |
| **Engine** | Purpose-built SRE product | **Generic BOSS engine** — SRE is one use case; the same pipeline builds any autonomous agent |
| **Deployment** | Their VPC agent | Self-hosted, **model-agnostic** (not tied to one LLM vendor) |

### Where Cleric is **ahead** → the gaps we're closing (new tickets RB-15…RB-20)
| Gap | Cleric has | Our answer |
|---|---|---|
| **Infra knowledge graph** | Layered topology (deterministic k8s/cloud walk + inferred service deps) | **RB-15** service & infra dependency map — scopes anomalies to a service/repo and powers RCA |
| **Autonomous reasoning** | Parallel multi-hypothesis plan/execute/reflect, causation-not-correlation, confidence scores | **RB-16** upgrade `diagnose` into a parallel-hypothesis engine with ranked, confidence-scored root causes |
| **Causal signal** | Correlates deploys / config / feature-flags with onset | **RB-17** change-correlation layer (deploys/CI, config drift, flag flips ↔ anomaly onset) |
| **Close the loop** | Post-fix verification + stability window | **RB-18** production verification loop (re-watch signal, no-recurrence window, else reopen/escalate) |
| **Autonomy spectrum** | Auto for routine, handoff for complex | **RB-19** governed autonomy tiers (auto-apply reversible ops → gated code PR → human handoff, chosen by confidence × blast radius) |
| **Meet oncall where they are** | Slack + PagerDuty threads; investigation MCP | **RB-20** oncall conversational surface (Slack/PagerDuty/Mattermost) + investigation MCP endpoint |

**Net:** we reach *parity* on autonomous investigate → knowledge-graph → verify → learn, and keep a
**structural lead on remediation** (we ship the tested, governed fix; they hand it off) — all on a **generic
engine** that also builds every other autonomous use case. "AI SRE" is the wedge; the engine is the moat.

---

## 2. The connector layer (out-of-the-box, the enterprise on-ramp)

The whole "useful day one" promise lives here. One canonical read surface, two implementation paths, presets
per tool.

### 2.1 The `TelemetrySource` protocol (canonical, source-agnostic)
```python
class TelemetrySource(Protocol):
    kind: str                                            # "splunk" | "grafana" | "datadog" | ...
    def query_logs(self, q: str, window: TimeWindow, *, limit: int) -> list[LogEvent]: ...
    def query_metrics(self, q: str, window: TimeWindow) -> list[MetricSeries]: ...
    def query_traces(self, q: str, window: TimeWindow, *, limit: int) -> list[Span]: ...
    def list_alerts(self, window: TimeWindow) -> list[Alert]: ...     # PagerDuty/Datadog/Splunk alerts
    def comment_back(self, ref: str, body: str) -> bool: ...          # close the loop on the source
```
Every source normalizes into canonical shapes — `LogEvent{ts, service, level, message, fields}`,
`MetricPoint`, `Span`, `Alert` — so **the mining pipeline is 100% source-agnostic**. A new tool = a new
adapter, never a pipeline change.

### 2.2 Two implementation paths
- **MCP-backed** (preferred where a server exists): wrap an existing MCP server through the **existing MCP
  registry** — Grafana, Datadog, and Splunk all ship or are shipping MCP servers; "any MCP server wired for
  Claude Code works" (the same property Grok Build advertises). Zero bespoke client.
- **Native skill adapters** (for tools without MCP, or for tighter control): thin HTTP clients packaged as
  **skills** under `skills/telemetry/{splunk,grafana,elk,datadog,otel,prometheus,loki,cloudwatch,pagerduty}`.
  Discoverable in the marketplace; enable per project.

### 2.3 Auth, presets, security
- **Auth via SecretRefs** — scoped, read-only creds, never in agent context (enterprise-critical; contrast
  the Grok Build SSH-key-exposure incident). Per-source config: endpoint, index/datasource, default query.
- **Presets** — a query template per tool (`Splunk: index=main | ...`, `Grafana: datasource + PromQL`,
  `Loki: {app="x"}`) so a customer connects and it works with defaults, tunable later.
- **Read-only + bounded** — every query carries a time window + row cap; no unbounded pulls.

### 2.4 Connector matrix (full target set — parity with Cleric's supported list)

**Yes — we plan the whole supported-connector set, not a subset.** Every one implements the same
`TelemetrySource` surface, so breadth is additive adapters, never pipeline changes. Capability columns map to
the protocol: **L**=`query_logs`, **M**=`query_metrics`, **T**=`query_traces`, **A**=`list_alerts`,
**W**=`comment_back`/write-back. All read-only except where noted.

| Category | Connector | L | M | T | A | Notes |
|---|---|:-:|:-:|:-:|:-:|---|
| **Metrics/APM** | Grafana (+ Loki) | ✓ | ✓ |  | ✓ | your named priority; Prometheus/Loki datasources + dashboards |
| | Prometheus | | ✓ | | ✓ | PromQL + alert rules |
| | Datadog | ✓ | ✓ | ✓ | ✓ | metrics/logs/monitors/dashboards/SLOs |
| | New Relic | ✓ | ✓ | ✓ | ✓ | NRQL |
| | Sentry | ✓ | | ✓ | ✓ | issues/events/stack traces/releases |
| **Logs** | Splunk | ✓ | | | ✓ | your named priority; SPL, index-scoped |
| | Elasticsearch / OpenSearch | ✓ | | | | Lucene |
| | OTel collector | ✓ | ✓ | ✓ | | vendor-neutral on-ramp |
| **Cloud** | AWS CloudWatch | ✓ | ✓ | | ✓ | logs/metrics + EC2/LB/DNS/cert inventory (→ graph) |
| | GCP Cloud Logging/Monitoring | ✓ | ✓ | | ✓ | + Compute/LB/DNS inventory (→ graph) |
| | Cloudflare | ✓ | ✓ | | | DNS/WAF/Workers/analytics |
| **Kubernetes** | Kubernetes / EKS | ✓ | ✓ | | ✓ | events/logs/resources/metrics; **feeds the RB-15 deterministic graph walk** |
| **Incident/alert** | PagerDuty | | | | ✓ | incidents + services; alert-driven trigger (RB-10) |
| **Datastore** | MongoDB Atlas | ✓ | ✓ | | | slow queries/perf (no data access) |
| **Source/CI** | GitHub | | | | | repos/PRs **read-write** (remediation + change-correlation RB-17) |
| | BuildKite | ✓ | | | | pipeline/build/job logs (change-correlation) |
| **Feature flags** | Unleash | | | | | flag state/changes (change-correlation RB-17) |
| **Knowledge/ticket** | Confluence / Jira | | | | | runbooks + Jira **write** (ticket create); Linear/Notion similar |
| **Custom** | Generic **MCP** | ✓ | ✓ | ✓ | ✓ | any MCP server via the registry — the open-ended escape hatch |

**Sequencing** (breadth is RB-9, additive): ship **OTel + Grafana/Prometheus + Splunk** first (named
priorities + the k8s walk that seeds the graph), then Datadog → CloudWatch/GCP → PagerDuty (alert intake) →
the long tail. GitHub/BuildKite/Unleash land with change-correlation (RB-17); Kubernetes doubles as the
deterministic graph walker (RB-15).

---

## 3. The reliability pipeline (system-level)

A durable, resumable flow modeled on `code.task` — new job kinds behind a `reliability` service.

### 3.1 Job kinds (durable, crash-resumable)
| Job | Does |
|---|---|
| `reliability.scan` | pull a window from configured sources → template → detect → cluster → upsert incidents |
| `reliability.diagnose` | for a triaged incident → RCA vs the repo (APP-5) → root-cause hypothesis + evidence |
| `reliability.remediate` | diagnosed incident → governed code task (bug-framed) → gated PR; infra → IaC lane |
| `reliability.learn` | capture the outcome → a learning example (§4) |

### 3.2 Triggers
- **Scheduled scan** (cron per source) — steady-state mining.
- **Alert-driven** (webhook from PagerDuty/Datadog/Splunk alert → immediate `scan`+`diagnose` scoped to the
  alert) — the incident-response path; reuses the signed-inbound-webhook receiver we built for GitHub/Plane.
- **Manual** (console / API) — operator kicks a scan or a diagnose.

### 3.3 Data model (new tables)
```
telemetry_sources   (id, project_id, kind, config jsonb, secret_ref, enabled, created_at)
anomaly_signals     (id, project_id, source_id, kind, template, metric, score, window, sample jsonb, ts)
incidents           (id, project_id, dedupe_key, title, severity, status, confidence,
                     first_seen, last_seen, signal_count, service, finalize_ctx jsonb)
incident_evidence   (id, incident_id, kind, ref, payload jsonb)          -- templates, traces, screenshots
remediations        (id, incident_id, code_task_run_id, decision_id, status, pull_request_url)
learning_examples   (id, project_id, agent, kind, label, payload jsonb, outcome, source, created_at)
```
`incidents.status`: `open → triaged → diagnosing → remediating → resolved | dismissed`. `dedupe_key` groups
recurring signals into one named issue (LangSmith-Engine style), so the board shows **issues, not noise**.

### 3.4 APIs
```
POST /v1/reliability/sources                  register/enable a telemetry source (+ preset)
POST /v1/reliability/scan                      run a scan (source(s), window)  -> incidents
GET  /v1/reliability/incidents                 the incident board (filter by status/severity/service)
GET  /v1/reliability/incidents/{id}            evidence + linked remediation + learning
POST /v1/reliability/incidents/{id}/diagnose   RCA vs repo -> hypothesis
POST /v1/reliability/incidents/{id}/remediate  -> gated fix PR
POST /v1/reliability/incidents/{id}/feedback   {verdict: real|false_alarm|fix_good|fix_bad}  -> learning
POST /v1/reliability/alert                      alert webhook intake (signed)  -> scan+diagnose
```

### 3.5 Console area
**Reliability** — an incident board (severity stripe, status pills, service chips), an incident detail
(evidence timeline, root-cause hypothesis, the linked PR, the feedback controls), and a learning panel
(false-alarm rate, remediation success rate over time). It's a **UI to operate**, not read — surface the
summary before the detail (per the design guidance for dashboards).

---

### 3.6 The knowledge graph (RB-15) — RCA context + learning substrate

The topology the agent reasons over, and the memory it learns into. Two tables, two layers, two consumers.

```
kg_nodes (id, project_id, kind, node_key, name, attrs, layer, source, confidence, first_seen, last_seen)
kg_edges (id, project_id, src_id, dst_id, kind, attrs, layer, source, confidence, first_seen, last_seen)
   kind(node): service | resource | repo | deployment | datastore | external | incident | root_cause
   kind(edge): depends_on | runs_on | deployed_from | owns | emits | caused_by | correlates_with
   layer:      deterministic (k8s/cloud/infra_resources walk, conf 1.0)  |  inferred (traces/heuristics, <1)
```

- **Context for RCA** — `subgraph(service, depth)` returns the failing service's neighborhood (its
  dependencies, the resources it runs on, the owning repo). The investigation engine (RB-16) reasons over
  *this* subgraph + the repo (APP-5), not a blank page. An anomaly's `service` resolves to a graph node, so a
  signal immediately carries its blast radius.
- **Feedback & learning** — a confirmed investigation calls `record_root_cause(service, cause, confidence,
  evidence)`, written as a `caused_by` edge; `known_causes(service)` replays those on the next incident, so
  recurring failures are recognized fast and the graph compounds accuracy (the Cleric "operational memory"
  property, but owned by the customer).
- **Idempotent & layered** — upserts converge on repeated walks; a deterministic sighting outranks a prior
  inferred guess. Foundation shipped (service + schema + 7 tests); live k8s/cloud walkers + the trace-inferred
  layer are the remaining RB-15 work.

---

## 4. The native learning loop (BOSS-wide primitive — your explicit ask)

Generalize the VAG ratchet (#107) from "verifier corpus" into a **first-class `LearningLoop` primitive that
any BOSS agent uses by default.** This is a BOSS-native capability, not SRE-specific — the same primitive
already powers the code verifier; the SRE agent becomes its second consumer, and future agents get it free.

### 4.1 The primitive
```python
class LearningLoop:
    def record(self, agent: str, project_id: str, *, kind: str, label: str,
               payload: dict, outcome: str, source: str) -> str: ...        # -> example id
    def corpus(self, agent: str, project_id: str, *, kind: str = None) -> list[Example]: ...
    def evaluate(self, agent: str, project_id: str, judge_fn) -> Metrics: ...  # precision/recall/quality
    def suggest_tuning(self, agent: str, project_id: str) -> TuningPlan: ...    # thresholds, few-shots
```
Backed by `learning_examples`. `record` is the write; `corpus` is the growing labeled set; `evaluate` is
meta-eval (reuse `verifier_meta_eval` scoring); `suggest_tuning` proposes changes.

### 4.2 What "learning" means for the SRE agent (concrete)
Every incident outcome becomes a labeled example, and the loop **closes three ways** (exactly the
LangSmith-Engine three actions, on BOSS's rails):

| Outcome | Example label | The loop's action |
|---|---|---|
| flagged, human **dismissed** | `false_alarm` | **raise the detector threshold** for that template/signal (auto-tune) |
| real, **fix merged** | `good_detection + good_fix` | promote to a **confirmed few-shot** for RCA/remediation |
| real, **fix rejected** | `bugged` (missed) | add to the offline eval set; the highest-value signal |
| recurred after "resolved" | `regression` | reopen + escalate; the fix didn't hold |

`evaluate` then reports the agent's **false-alarm rate** and **remediation success rate** per customer/repo
over time — "gets smarter every incident," measurably. Tuning applies **per project** (each enterprise's
systems differ), and threshold changes are themselves gated (a human can review the tuning plan).

### 4.3 Why native matters
Making it a shared primitive means: (a) the SRE agent, the code verifier (VAG), and any future agent
(triage, PR-review) all learn through one audited path; (b) one console surface for "how is agent X
improving"; (c) one export path for the optional fine-tuned trace-judge. **Continual learning becomes a
BOSS platform feature, not a per-agent bolt-on** — a differentiator in itself.

---

## 5. Root-cause against the customer repo (reuse APP-5)

Given a named incident, `reliability.diagnose`:
1. **Extracts symptoms** — error templates, stack frames, affected service/file hints, the anomalous
   metric/span — from the incident evidence (already distilled, no raw volume).
2. **Retrieves the relevant code** via the **APP-5 `repo_retriever`** (pin the files the stack frames point
   at; rank by symptom overlap) + **recent diffs / "what changed"** (git blame at the error site, recent PRs
   near the deploy window).
3. **Reasons** — the frontier model produces a root-cause hypothesis + a proposed fix approach + a confidence,
   over the distilled evidence + retrieved code (never raw logs).

Output: `incident_evidence` rows (retrieved files, blame, hypothesis) + a confidence that gates whether we
auto-proceed to remediation or ask a human first.

---

## 6. Remediation (reuse the factory)

Diagnosed incident → `start_code_task` with **bug-framed instructions** (the Plane P3 framing shipped this
month) seeded with the incident: the failing behavior, a repro derived from the logs, the root-cause
hypothesis, and the retrieved code context. The **governed pipeline** then does its normal job — QA frozen
tests → cheap impl → BOSS re-runs tests → review panel → fail-closed gate → **PR**. Infra-class incidents
route to the **IaC verify lane** instead.

- **Always PR-gated** (enterprise governance; BOSS never auto-merges an SRE fix).
- **Close the loop on the source** — `comment_back` posts the PR link + status onto the originating alert
  (Splunk/PagerDuty/Datadog) and, if the incident came from a Plane ticket, the status-sync we shipped.
- `remediations` links `incident ↔ code_task_run ↔ decision ↔ PR` for a full audit trail.

---

## 7. Enterprise & security (the wedge specifics)

- **Data boundary** — telemetry is queried read-only via scoped SecretRef creds; the model sees only
  **distilled + redacted** signal (reuse the redaction lane). **No raw log egress.** This is the enterprise
  buying criterion and BOSS's structural advantage.
- **Deployment** — self-host / customer VPC (OSS engine + `LocalSubprocessProvider`); **model-agnostic** (use
  the enterprise's approved model / Azure / a private endpoint). No dependency on any one vendor's model.
- **Governance & compliance** — every step is a durable, audited record (signals → incident → decision →
  gated PR); RBAC; PR-gated remediation; the tuning plan is reviewable.
- **Cost** — budget-governed per source; the cascade keeps the LLM at <1% of the stream.
- **Sales motion** — land on the connector + Insight (read-only) value, expand to Signal→Fix. "Out of the
  box" = presets for their existing stack; "wedge" = the fix PR nobody else produces.

---

## 8. Phased plan + tickets

Plane module: **"AI SRE — Autonomous Oncall"** (renamed 2026-07-21; was "Data-Mining & Reliability Agents").
Phases 0–4 are the original loop (detect → incident → RCA → gated fix → learn); **Phase 5** adds the
Cleric-parity autonomy layer (RB-15…RB-20). RB-13 (trace-judge data-mining) is **dropped** in the reframe.

### Phase 0 — anomaly substrate + Insight (read-only) ✅ *done* *(no remediation risk)*
- **RB-1** `telemetry_mining/` lib: templating + metric/log statistical detector cascade
  (rate/z-score, new/spiking templates, metric z-score). Pure, model-free, unit-tested. *AC:* templates a
  raw log batch deterministically; detectors flag injected anomalies. **✅ merged (PR #48), 11 tests.**
- **RB-2** `TelemetrySource` protocol + canonical shapes + first adapters (in-memory + log-export). *AC:*
  pulls a bounded window; normalizes to `LogEvent[]`. **✅ merged (PR #48).**
- **RB-3** Insight agent (read-only): scan goldeneye logs → templated "top issues / what changed" report +
  `POST /v1/reliability/scan` (dry, no incidents persisted yet). *AC:* a real report from goldeneye's stream.

### Phase 1 — incident model + **knowledge graph** + RCA + the LearningLoop primitive
- **RB-15** *(pulled forward from Phase 5 — it's foundational, not a late add).* **Knowledge graph**
  (`kg_nodes`/`kg_edges`) + `KnowledgeGraphService`: the service/infra topology RCA reads context *from* and
  the learning loop writes confirmed causes *back into*. `subgraph()` = RCA-context bundle around a failing
  service; `record_root_cause()`/`known_causes()` = the learning write-back; `seed_from_infra_resources()` =
  deterministic first layer. **✅ foundation merged + 7 tests** (walkers/inferred layer/read-endpoint next).
- **RB-4** data model (`telemetry_sources`, `anomaly_signals`, `incidents`, `incident_evidence`) + upsert +
  **dedupe/cluster into named incidents**; incidents/services land as graph nodes. *AC:* recurring signals
  collapse to one incident with a stable key.
- **RB-5** `reliability.diagnose`: RCA over **the knowledge-graph subgraph + the repo (APP-5)** + prior
  `known_causes()` → hypothesis + evidence + confidence.
- **RB-6** **`LearningLoop` primitive** (`learning_examples` + record/corpus/evaluate/suggest_tuning),
  retro-fit the VAG ratchet onto it. *AC:* one primitive, two consumers (verifier + SRE).

### Phase 2 — Signal→Fix (the wedge)
- **RB-7** `reliability.remediate`: diagnosed incident → bug-framed governed code task → **gated PR**;
  `remediations` linkage; `comment_back` to the source. *AC:* a real goldeneye anomaly → a tested, gated PR.
- **RB-8** feedback API + learn: outcome → `LearningLoop.record` → threshold/few-shot tuning. *AC:* a
  dismissed incident raises that signal's threshold on the next scan.

### Phase 3 — out-of-the-box breadth + triggers + console
- **RB-9** connectors: Splunk, Grafana/Loki/Prometheus, Datadog (MCP where available, else skill adapters) +
  presets. *AC:* connect Splunk with a preset → incidents appear.
- **RB-10** alert-driven trigger (signed alert webhook → scan+diagnose) reusing the inbound-webhook receiver.
- **RB-11** Reliability console area (incident board + detail + learning panel).

### Phase 4 — learning-loop tuning + quality
- **RB-12** auto-threshold tuning + confirmed-few-shot selection (per project).
- ~~**RB-13** fine-tuned "trace-judge" export~~ — **dropped** (heavy trace data-mining, out of scope in the
  AI-SRE reframe; metrics+log anomaly detection stays).
- **RB-14** anomaly-agent **meta-eval** (precision/recall of the SRE agent itself — measure the measurer).

### Phase 5 — autonomous-oncall parity (close the Cleric gaps) — see §1.5
- **RB-15** ~~knowledge graph~~ — **moved to Phase 1** (foundational; foundation already merged). What
  remains here: the **live walkers** (k8s RBAC / cloud inventory) + the **inferred layer** (service deps from
  traces) + a read endpoint. *AC:* an anomaly resolves to a named service + owning repo automatically.
- **RB-16** **autonomous investigation engine**: `diagnose` becomes a parallel plan/execute/reflect loop with
  multiple hypotheses, causation-not-correlation testing, and **confidence-scored** ranked root causes.
- **RB-17** **change-correlation** layer (deploys/CI, config drift, feature-flag flips ↔ anomaly onset).
- **RB-18** **production verification loop** (post-fix re-watch the signal + no-recurrence stability window;
  else auto-reopen/escalate). *AC:* a shipped fix is auto-confirmed or auto-reopened.
- **RB-19** **autonomy tiers & policy** (Tier-0 auto-apply reversible ops → Tier-1 gated code PR → Tier-2
  human handoff, chosen by confidence × blast radius; every action receipted/audited).
- **RB-20** **oncall conversational surface** (Slack/PagerDuty/Mattermost investigation threads) +
  **investigation MCP endpoint** (Claude Code/Cursor pull incident context).

---

## 9. Risks & open decisions (delta from spec 11)

- **Connector security review** — read-only scoped creds, per-tool least privilege; this is an enterprise
  audit surface. *Owner: security.*
- **Per-tool query semantics** — SPL vs PromQL vs Lucene differ; presets + a small query-builder per adapter.
- **Alert-webhook auth** — reuse the signed-inbound receiver; per-source secret.
- **LearningLoop storage** — new `learning_examples` table vs extend the ratchet's tag-the-run. *Proposal:* a
  real table (this is now a platform primitive, not a per-run tag).
- **Dedupe/clustering quality** — start lexical (template + service + signature); embeddings later.
- **First design partner** — dogfood goldeneye; then one enterprise with Splunk/Grafana.

---

## 10. Starting now (Phase 0, RB-1/RB-2)

Kickoff is the **`telemetry_mining` foundation** — the Drain-style templating + statistical detector cascade
+ the `TelemetrySource` protocol — pure, tested, dogfooded on goldeneye's logs. It carries zero remediation
risk, proves the hardest technical claim (big-data → distilled signal), and everything else builds on it.

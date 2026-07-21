# 12 — Signal→Fix SRE Agent: implementation design

> **Status:** BUILD SPEC — approved for build (extends spec 11). **Owner:** paramg. **Date:** 2026-07-21.
> **Decision:** build the flagship **Signal→Fix SRE agent** at the **system level** in BOSS, as the
> **enterprise sales wedge**, with **out-of-the-box connectors** (skills/MCP) for the popular observability
> tools and a **BOSS-native learning loop**.

> **The pitch (one line):** *Connect your Splunk / Grafana / Datadog + your repo → BOSS turns production
> incidents into reviewed, tested fix PRs, and learns your systems. Self-hosted, model-agnostic, governed.*

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

### 2.4 Launch connector set (priority order)
`OTel` (vendor-neutral, first) → `Splunk` + `Grafana`/`Loki`/`Prometheus` (your named priorities) →
`Datadog` → `ELK/OpenSearch` → `CloudWatch` → `PagerDuty` (alert intake). Ship OTel + one log + one metric
source in Phase 0/3; the rest are additive adapters.

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

New Plane module: **"Data-Mining & Reliability Agents."**

### Phase 0 — mining foundation + Insight (read-only) *(no remediation risk)*
- **RB-1** `telemetry_mining/` lib: Drain-style templating + span/metric aggregation + the statistical
  detector cascade (rate/seasonal/z-score/iforest). Pure, model-free, unit-tested. *AC:* templates a raw log
  batch deterministically; detectors flag injected anomalies; benchmarked on a fixture.
- **RB-2** `TelemetrySource` protocol + canonical shapes + **one** connector (OTel or a log-file/Splunk-export
  adapter). *AC:* pulls a bounded window; normalizes to `LogEvent[]`.
- **RB-3** Insight agent (read-only): scan goldeneye logs → templated "top issues / what changed" report +
  `POST /v1/reliability/scan` (dry, no incidents persisted yet). *AC:* a real report from goldeneye's stream.

### Phase 1 — incident model + clustering + RCA + the LearningLoop primitive
- **RB-4** data model (`telemetry_sources`, `anomaly_signals`, `incidents`, `incident_evidence`) + upsert +
  **dedupe/cluster into named incidents**. *AC:* recurring signals collapse to one incident with a stable key.
- **RB-5** `reliability.diagnose`: RCA vs repo (reuse APP-5) → hypothesis + evidence + confidence.
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
- **RB-13** fine-tuned "trace-judge" export for volume/cost (the LangChain move).
- **RB-14** anomaly-agent **meta-eval** (precision/recall of the SRE agent itself — measure the measurer).

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

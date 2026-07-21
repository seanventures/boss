# 11 — Specialized Data-Mining Agents: from telemetry to governed remediation

> **Status:** DESIGN — for review. **Owner:** paramg. **Date:** 2026-07-21.
> **Prompted by:** "improving agents is a data-mining problem" (LangChain) + LangSmith Engine, and the
> question: *are frontier models useful for anomaly detection on **big-data** logs/traces (not just
> metrics), and should BOSS ship specialized out-of-the-box agents for this?*

---

## 0. TL;DR + recommendation

**Recommendation: invest — but with a sharp wedge, not "another anomaly detector."**

The anomaly-detection-on-telemetry market is *crowded* (Datadog Bits AI SRE, Splunk, Grafana, Elastic,
New Relic, Coroot, Mezmo, Resolve.ai). **Every one of them stops at "a remediation *plan* for a human to
verify."** None can produce the fix, because none is a software factory. **BOSS is.** So the differentiated
product is not detection — it's the **last mile the whole field can't reach: signal → diagnosis →
*tested, governed, gated fix PR* → learn from the outcome.**

Three reinforcing facts make this a natural (not a stretch) investment:

1. **BOSS already runs this exact loop — on its own agents.** The VAG work shipped this session *is* the
   LangSmith-Engine model: the ratchet (#107) mines rejected PRs into a labeled corpus, meta-eval (#105) is
   the eval suite, the governed pipeline generates PRs, the gate is the evaluator. We generalize it and
   point it at **customer telemetry** and at **BOSS itself**.
2. **The "big-data LLM" worry has a clean answer** (§2): you never feed raw logs to a model. A hybrid
   mining pipeline distills big data to signal; the frontier model reasons over the *distilled* anomalies —
   exactly what you've already seen work on metrics.
3. **It reuses BOSS's rails end-to-end** — the governed code pipeline (remediation), the APP-5 repo
   retriever (root cause against the codebase), the budget governor + model-call pacing (cost on volume),
   MCP (connectors), the sandbox (reproduction), decisions/gates (governance), the skills framework
   (packaging), and the **Plane integration we just shipped** (surface an issue as a ticket → auto-remediate).

**Do not** rebuild observability (that's Datadog's moat). **Do** be the remediation-and-learning layer on
top of whatever telemetry the customer already has, plus a self-contained log-miner for those who don't.

Start read-only and dogfood on **goldeneye's own logs** (we've watched its `#infra` anomaly stream all
session — a perfect first "customer" with zero acquisition cost).

---

## 1. The thesis, and why it's already in BOSS's DNA

The LangChain articles argue agent improvement *is* a data-mining problem: **"traces are the currency of
long-horizon agent improvement,"** and *"every continual-learning company is an observability company, and
vice versa."* The loop:

```
collect traces → mine (specialized "trace-judge" models) → curate failure patterns into eval sets
   → experiment against evals → integrate learnings back into the agent
```

**LangSmith Engine** operationalizes it: it reads every trace, clusters signals (errors, evaluator
failures, latency/token anomalies, negative feedback) into **named issues** (not individual failures),
diagnoses root cause against the codebase, and emits three things — **(1) a fix PR, (2) a scoped online
evaluator, (3) failing traces added to an offline eval set.** *"Every resolved issue improves your eval
coverage."*

**BOSS already does a version of this on its own agents** (shipped this session):

| LangSmith Engine | BOSS equivalent (already built) |
|---|---|
| cluster production failures into named issues | (gap — we cluster per-run, not cross-run yet) |
| generate a fix PR | the governed code-task pipeline (**core capability**) |
| add failing traces to an offline eval set | the **cross-run ratchet #107** (rejected PRs → labeled corpus) |
| scoped evaluator + "improves eval coverage" | **meta-eval #105** (measure the verifier on the corpus) |
| the eval/experiment substrate | `evals/`, `bakeoff.py`, `verifier_meta_eval.py` |

So this initiative is **generalizing machinery BOSS already owns** and aiming it at two new targets:
**(a) the customer's telemetry** (product), and **(b) BOSS's own operation** (dogfood + "the factory that
improves itself").

---

## 2. Does this work on *big-data* logs/traces? (the core technical question)

**Short answer: yes — but never by feeding raw logs to a frontier model.** Your instinct is right that
frontier models are excellent at anomaly *reasoning* over metrics and unproven on high-volume raw logs.
The resolution is architectural: a **hybrid data-mining pipeline** reduces big data to LLM-digestible
signal, and the LLM does only the semantic layer it's great at.

**The evidence (2026 AIOps literature):**
- **Templating first.** [Drain](https://arxiv.org/pdf/2502.00677)-style log parsing collapses millions of
  raw lines into a few hundred **event templates + parameters** — a 10³–10⁴× cardinality reduction. Traces
  reduce similarly via span-shape aggregation; metrics are already series.
- **Statistical/ML for the volume.** Rate/seasonality/z-score detectors and template-frequency models
  (Drain + Random Forest / isolation forest) are cheap, high-throughput, and catch known-pattern anomalies.
- **LLMs shine exactly where classical methods are weak** — the low-label, novel, semantic regime. Benchmarks:
  [GPT-4 + a light method hits **89% F1 with only 1% labels** vs 71% for Drain+RF and 82% for fine-tuned
  DeBERTa](https://arxiv.org/html/2604.12218v1); fine-tuned models overtake LLMs only around ~25% labels.
- So the LLM's job is **triage of novel/ambiguous candidates, cross-signal correlation, root-cause
  reasoning against the code, and remediation** — over the **distilled** anomaly set, not the raw stream.

**The cascade (cheap → expensive):**
```
raw logs/traces/metrics
  → [template + aggregate]         (Drain, span rollups)          — collapses volume, no model
  → [statistical/ML detectors]     (rate, seasonal, RF/iforest)   — flags candidates, cheap
  → [LLM trace-judge triage]       (only on novel/ambiguous)      — semantic, budget-governed
  → [cluster into named issues]    (across signals + time)
  → [LLM root-cause vs codebase]   (retrieval-augmented, distilled evidence)
  → [remediate + learn]
```

This directly answers the worry: **the model reasons over the same shape of distilled signal you've already
seen it handle well on metrics.** Logs/traces just need the templating + statistical front-end so the model
never drowns in volume. Cost stays bounded because the LLM only touches candidates (see §6).

---

## 3. Market reality + BOSS's wedge

**The field (2026):** Datadog **Bits AI SRE** (strongest general agentic investigator, per-investigation
pricing), **Splunk** (detector fires → agent analyzes metrics/events/logs/traces → suspected root causes +
impact + **a remediation plan for a human**), Grafana/Elastic/New Relic assistants, **Coroot** (OSS RCA),
**Mezmo**, **Metoro**, **Resolve.ai**. The [awesome-AI-SRE list](https://github.com/agamm/awesome-ai-sre)
alone has 100+ tools.

**They all converge on the same ceiling: detect → investigate → *recommend*.** A human still writes and
ships the fix.

**BOSS's wedge = remediation-as-code + continual learning:**

| Everyone else | BOSS |
|---|---|
| Detect + RCA | Detect + RCA **(consume theirs via MCP, don't rebuild)** |
| "Here's a remediation plan" | **A tested, gated fix PR** produced by the governed pipeline |
| Static prompts | **Self-improving** via the ratchet (each fix's accept/reject grows the eval set) |
| Tied to one telemetry stack | **Model- and stack-agnostic** (any engine, any source via connectors) |
| Closed SaaS | **OSS engine** + specialized agents as the vertical/paid layer (fits fork #1) |

**Positioning:** BOSS is not "an anomaly detector." It's **"the agent that closes the loop from a
production signal to a verified code (or IaC) change — and gets better every time."** Consume existing
observability where the customer has it; ship a self-contained log-miner where they don't.

---

## 4. The proposed agents (a small, focused library on shared rails)

Build the **mining pipeline once**; expose it as a few packaged, out-of-the-box agents.

- **A — Signal→Fix SRE agent (flagship, differentiated).** Telemetry (logs/traces/metrics via
  connectors/MCP) → hybrid detection → named issues → RCA against the codebase → **governed remediation
  PR** (code via the code-task pipeline; infra via the IaC lane) → learn from the outcome. *This is the one
  nobody else can ship.*
- **B — Log/Trace Insight agent (read-only data-mining).** Mine telemetry into **structured, queryable
  insights**: top error templates, emerging patterns, latency/cost/throughput regressions, "what changed
  after deploy X." Ships value on **day one with zero remediation risk** and is the safe first phase. Feeds A.
- **C — "BOSS Engine" (internal dogfood, the LangSmith-Engine analog).** Point the same machinery at
  **BOSS's own traces** to continuously improve the pipeline/verifier — generalize the ratchet into
  cross-run named-issue clustering + auto-fix PRs on BOSS itself. Doubles as the strongest demo: *"the
  factory that fixes itself."*

Recommendation: **B → A → C** in that order (prove the mining read-only, then remediate, then self-apply).

---

## 5. Architecture

### 5.1 The pipeline (stages)
```
Ingest → Normalize/Template → Detect → Cluster/Correlate → Triage → Diagnose(RCA) → Remediate → Learn
```

| Stage | What | Reuse / new |
|---|---|---|
| **Ingest** | pull a bounded window of logs/traces/metrics | connectors: OTel, Loki/ELK, CloudWatch, Datadog — via **MCP** where a server exists, else a thin adapter. Bounded window + sampling. |
| **Normalize/Template** | Drain-style log templates; span-shape rollups; metric series | new `telemetry_mining/` (pure, model-free, testable) |
| **Detect** | statistical (rate/seasonal/z-score), template-frequency, iforest/RF; LLM-triage only on novel | pluggable detector interface; **cheap-first cascade** |
| **Cluster/Correlate** | group anomalies into **named issues** across signals + time; dedupe vs known issues | new; mirrors the ratchet's dedupe-by-key discipline |
| **Triage** | severity + confidence; a **fail-closed gate** on what's worth escalating | reuse the **quorum/confidence gate** patterns from VAG (refute panel, `meta_eval_gate`) |
| **Diagnose (RCA)** | LLM root-cause against the **codebase** + distilled evidence | reuse **APP-5 `repo_retriever`** (pin relevant code, rank by the issue's symptoms) |
| **Remediate** | hand the diagnosed issue to the **governed code pipeline** as a bug-framed task → gated PR; infra → IaC lane | reuse `start_code_task` + the **Plane P3 bug-framing** we just shipped; IaC verify lane |
| **Learn** | outcome (PR merged? rejected? issue recurred?) → labeled corpus; measure the agent's precision/recall | reuse the **#107 ratchet** + **#105 meta-eval** (now scoring the *SRE agent*, not just the coder) |

### 5.2 The feedback loop (why this compounds)
Every issue the agent resolves — or gets wrong — is a labeled example:
- **False alarm** (flagged, human dismissed) → a "clean" corpus item (the detector was over-eager).
- **Real + fixed + merged** → a "good detection + good fix" example.
- **Real but the fix was rejected** → a bugged item (the remediation missed) — the highest-value signal.

Fed through the ratchet, meta-eval then reports the agent's **precision (false-alarm rate)** and
**remediation success rate** over time — *"every resolved issue improves eval coverage,"* on BOSS's rails.
This is the moat the pure detectors can't build: **BOSS learns which anomalies are worth acting on for
*this* customer's codebase.**

### 5.3 Cross-cutting reuse
Budget governor + **model-call pacing** (cost on volume — critical), sandbox (run detectors / reproduce),
decisions + fail-closed gates (governance; **never auto-merge a remediation** — PR-gated always, per BOSS's
model), skills framework (package A/B/C as installable skills), and the **Plane integration** (an anomaly
becomes a `boss:bug` ticket → the auto-trigger we shipped remediates it — a genuinely closed loop).

---

## 6. Big-data & cost handling (making it affordable)

- **Never LLM-on-raw.** Templating + aggregation + sampling collapse volume *before* any model call.
- **Cascade.** Statistical detectors run on everything (cheap); the LLM touches only candidates + novel
  templates. Empirically the LLM sees <1% of the stream.
- **Budget-governed.** Reuse the budget reservations + **anti-burst pacing** (#111) so a noisy log source
  can't run up a frontier bill; a per-source spend cap binds first.
- **Optional fine-tuned "trace-judge"** (the LangChain move): a small open model fine-tuned as the triage
  judge for volume, keeping the frontier model for RCA/remediation only. A cost follow-on, not phase 1.
- **Incremental windows + dedupe** so re-runs don't re-pay for known issues.

---

## 7. Out-of-the-box packaging (the customer ask)

"Useful out of the box" means: **point it at your telemetry → get named issues + fix PRs, day one.**
- **Presets/connectors** for common stacks (OTel first — vendor-neutral — then Loki/ELK/CloudWatch/Datadog).
- **Zero-config defaults** for the detector cascade + severity thresholds (tunable later).
- **Packaged as skills** in the marketplace; enable per project.
- **Dogfood on goldeneye**: BOSS's own `#infra` health/anomaly stream is right there — the flagship demo
  and the first eval corpus at zero acquisition cost.

---

## 8. Phased implementation + tickets

Suggested new Plane module: **"Data-Mining & Reliability Agents"** (or under the VAG module, since it reuses
that machinery).

### Phase 0 — Shared mining pipeline + Insight agent (B), read-only *(no remediation risk)*
- `telemetry_mining/`: Drain-style templating, span/metric aggregation, the statistical detector cascade
  (pure, model-free, unit-tested).
- Ingest a bounded window from **one** source (goldeneye logs) + the LLM triage-on-candidates step.
- **Exit:** point it at goldeneye logs → structured "top issues / what changed" report. Proves the mining.

### Phase 1 — Named-issue clustering + RCA against the codebase
- Cross-signal/time clustering into named issues + dedupe. RCA via the APP-5 retriever + distilled evidence.
- **Exit:** a named issue with a code-grounded root-cause hypothesis + evidence refs.

### Phase 2 — Signal→Fix loop (A) *(the wedge)*
- Diagnosed issue → governed code task (bug-framed) → **gated remediation PR**; the ratchet captures the outcome.
- **Exit:** a real anomaly on goldeneye auto-produces a tested, gated fix PR; the outcome feeds meta-eval.

### Phase 3 — Connectors/MCP breadth + presets (out-of-the-box) + internal BOSS Engine (C)
- OTel + 2–3 stacks; zero-config presets; point the same machinery at BOSS's own traces.
- **Exit:** a customer connects their telemetry and gets issues+PRs with no bespoke setup; BOSS self-improves.

### Phase 4 — Cost/quality hardening
- Fine-tuned trace-judge for volume; a **detection-quality meta-eval** (precision/recall of the anomaly agent
  itself — measure the measurer, exactly as #105 does for the code verifier).

**Ticket seeds (ACs abbreviated):** (1) templating+detector cascade lib; (2) telemetry ingest connectors
(OTel first); (3) named-issue clustering+dedupe; (4) code-grounded RCA (reuse APP-5); (5) remediation
handoff to the governed pipeline + ratchet feedback; (6) out-of-the-box presets + skills packaging;
(7) internal BOSS Engine; (8) cost governance + fine-tuned trace-judge; (9) anomaly-agent meta-eval.

---

## 9. Risks & open decisions

**Risks**
- **Crowded market.** *Mitigation:* compete only on remediation-as-code + continual learning; consume, don't
  rebuild, observability.
- **BOSS has no data plane / ingestion infra.** *Mitigation:* start by *consuming* via MCP/adapters with
  bounded windows; a heavy ingestion store is out of scope (partner or reuse the customer's).
- **Frontier cost on volume.** *Mitigation:* templating + cascade + budget governor + trace-judge (§6).
- **Scope dilution vs BOSS's core.** *Mitigation:* keep it a focused *agent library on existing rails*, not
  a new platform; every stage reuses something we already shipped.
- **RCA reliability.** *Mitigation:* measure it via the ratchet/meta-eval and keep remediation **PR-gated**;
  BOSS never auto-merges an SRE fix.

**Open decisions (need your / product input)**
1. **Which telemetry stack first?** Proposal: **OTel** (vendor-neutral) + goldeneye logs for dogfood.
2. **Build vs consume ingestion?** Proposal: **consume** via MCP/adapters; no ingestion store of our own.
3. **Fine-tuned trace-judge now or later?** Proposal: **later** (prompt-based cascade first; fine-tune when
   volume/cost demands).
4. **Remediation autonomy?** Proposal: **always PR-gated** (consistent with BOSS's whole governance model);
   auto-merge is never on the table for SRE fixes.
5. **Where does this live in Plane?** New module vs under VAG. Proposal: **new module**, since it's a product
   surface, but it *reuses* the VAG machinery.
6. **Product scope:** internal (BOSS Engine, C) only, or customer-facing (A/B) from the start? Proposal:
   dogfood C's machinery on goldeneye first (fast, free), ship B then A to customers.

---

## 10. Final recommendation

**Invest — scoped to the remediation-and-learning wedge, built on BOSS's existing rails, dogfooded on
goldeneye.** This is not a detour: it's the natural extension of "the factory that builds software" into
"the factory that keeps software healthy — and gets smarter every incident." It differentiates on the one
thing the entire AI-SRE field can't do (produce the fix), it makes BOSS's own operation self-improving, and
it turns the traces BOSS already emits into a compounding asset — exactly the LangChain thesis, but with the
remediation half that only a software factory can own.

Start with the **read-only Insight agent on goldeneye logs** (Phase 0) — cheap, safe, and it proves the
mining before we touch remediation. If it surfaces real, code-grounded issues on our own box, the wedge is
validated and Phase 2 (Signal→Fix) is the money shot.

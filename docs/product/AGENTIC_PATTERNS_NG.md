# Agentic Design Patterns → BOSS: what to learn from Andrew Ng's *The Batch*

> A study of Andrew Ng's agentic-design-pattern and evals letters (deeplearning.ai/the-batch),
> mapped onto BOSS's governed coding pipeline. Goal: find where BOSS is already ahead of the
> published advice, and where a concrete, high-leverage improvement is sitting unclaimed.
> Compiled 2026-07-11 from three parallel research sweeps.

## TL;DR

Ng's throughline maps almost one-to-one onto BOSS's **VAG** thesis: the bottleneck to agent
progress is not generation but the **quality of the verification loop**. His headline datum —
GPT-3.5 in an agentic workflow hitting ~95% on HumanEval vs 48% zero-shot — is our bet stated in
his vocabulary: *"the improvement from an agentic workflow can dwarf the improvement from moving to
a larger model."*

**Where BOSS is already ahead of the letters:** our verification-first design embodies Ng's most
reliable patterns (Reflection, Tool Use) *better than he describes* — the critic is a frontier model
independent of a cheap generator, the frozen-test-before-implementation rule keeps the grader out of
the generator's reach, and we have measured what Ng only gestures at (a weak verifier collapses
pass-rate to 0%). We also correctly **suppress** his least-reliable pattern (free-form Planning)
inside the governed pipeline, where unpredictability upstream of a fail-closed gate is unacceptable.

**Where the gaps are:** almost entirely about *measuring and calibrating the verifier* (confidence,
meta-eval, independence enforcement) and about the *design/visual axis* (BOSS is design-blind — it
never scaffolds a frontend and its visual gate is a no-op). Both are exactly where Ng's letters land
hardest.

---

## Part 1 — The four core patterns

| Pattern | BOSS today | Highest-leverage improvement |
|---|---|---|
| **Reflection** | **HAS, unusually well.** The repair loop is textbook tool-assisted reflection (failing-test evidence fed back ≤3×); the review/security/consistency/mutation stage is a heavyweight multi-agent critic — and, unlike Ng's version, the critic is a *frontier* model separate from the cheap generator. | **N-verifier refute panel** on adversarial review: k independent frontier critics, refute-framed, quorum vote → converts one noisy opinion into a de-noised signal + a confidence score. **← shipped, see §Roadmap.** |
| **Tool Use** | **HAS, one piece dormant.** MCP + the sandbox *are* Ng's "self-reflection with tools" (the sandbox is the code-execution tool that makes the frozen-test loop authoritative). The skills-registry executor is not yet wired. | Wire the skills executor as **verification-side tools** (static analyzers, dep-vuln scanners, type-checkers) so the critic grounds judgments in executable evidence, not opinion. |
| **Planning** | **PARTIAL — deliberately constrained.** The coding pipeline is a *fixed* workflow (governance feature, not a gap); real planning lives only in the discovery pipeline. Ng himself: *"Planning is a less mature technology… hard to predict what it will do."* | Introduce planning only in a **bounded, verifier-gated envelope**: a planner decomposes a large feature into a DAG, but each node emits its *own* frozen tests and the plan is reviewed before execution. Adaptivity, caged behind verification. |
| **Multi-Agent Collaboration** | **HAS-in-spirit.** Specialized roles (QA freezes tests, implementer codes, distinct critics) in a *pipelined* topology — variance-controlled vs Ng's free-interaction debate. The cheap-impl/frontier-verify split is heterogeneous-by-capability, which Ng only hints at. | **Bounded generator↔verifier debate on contested cases only**: when the repair loop and the reviewer still disagree after retries, run an N-round structured exchange whose transcript feeds the gate. Debate's upside on hard cases, round-capped. |

**Top 3 from this series:** (1) refute panel, (2) bounded debate on contested cases, (3) skills
executor as verification tools — all keep the cheap-generate / frontier-verify VAG shape intact.

---

## Part 2 — Evals & reliability (hardening the binding constraint)

Ng: *"the single biggest predictor of how rapidly a team makes progress building an AI agent lay in
their ability to drive a disciplined process for evals and error analysis."* The verifier is BOSS's
binding constraint, so this is the richest vein.

1. **Independence contract** *(Reflection)* — Ng: use *two different agents*, one to generate, one to
   criticize. BOSS is PARTIAL: enforce independence along model **and** prompt **and** context — a
   verifier that sees the generator's chain-of-thought inherits its blind spots. Contract: different
   model family, no access to the implementer's reasoning trace, spec-only re-derivation.
2. **Self-consistency / refute-panel + confidence** — a single LLM judgment is noisy; agreement across
   independent samples is a usable signal. Confidence = fraction of the panel that fails to refute;
   gate fail-closed below quorum, escalate the middle band. **← shipped.**
3. **Prefer objective/executable metrics; constrain LLM-as-judge** — Ng warns LLMs are poor at 1–5
   absolute scoring; use binary/rubric refute verdicts ("cite a concrete failing input or return
   PASS"), and never let a subjective judge silently override a passing objective test. BOSS's
   consistency check is the soft spot here.
4. **Test the tests (mutation as eval-of-eval)** — a frozen suite a bug can slip past is a weak
   verifier however good the reviewer. Use **mutation score as a gate on the QA suite *before*
   implementation begins** (inject faults into a reference solution, require the frozen suite to kill
   ≥X%), and add property-based invariants for what example tests can't cover.
5. **Meta-eval the verifier** — Ng's worked example: you add a fact-checker to a pipeline — *can you
   even tell if it helped?* BOSS has no standing benchmark that evaluates *the verifier*. Build a
   labeled corpus of known-good and deliberately-bugged PRs (seed from escaped-bug history); every
   verifier change is scored on true-positive/false-positive before it ships. **You cannot harden what
   you don't measure — this governs the whole VAG module.**
6. **Per-stage error analysis** — attribute each real defect to the stage that caught (or missed) it;
   compute each stage's historical precision/recall and use those weights when combining verdicts into
   a *calibrated* confidence, rather than an unweighted AND.
7. **Iterate on evals across runs (anti-reward-hacking ratchet)** — keep tests frozen *intra*-run
   (correct anti-tampering design), but mine every human-rejected-but-passed PR and every escaped bug
   into a new frozen regression test. A monotonic ratchet: the target keeps expanding, so a static
   verifier can't be gamed.
8. **Spend compute where stakes are highest** — risk-tier PRs (blast radius, security surface); scale
   panel size / model strength with risk; make every escaped bug permanent in the regression set.

**Top 5 verifier upgrades:** meta-eval corpus (#5) · refute panel + confidence (#2, shipped) ·
independence contract (#1) · mutation-gate the frozen tests (#4) · cross-run eval ratchet (#7+#8).

---

## Part 3 — Cost / quality / first-time output

1. **Workflow beats a bigger model** — validates VAG. Actionable knob: make **repair-loop depth a
   routed budget variable**, not a fixed ≤3 — Ng's evidence says iterations buy more than model tier,
   so spend cheap repair before escalating the implementer, triggered on `$/verified-outcome`.
2. **Frontend is the *most*-accelerated task** — because agents can *operate a browser* to iterate.
   BOSS forfeits exactly this: make the **visual gate real** — render the UI, screenshot it, feed the
   screenshot to a vision design-critic, loop repairs until it passes. The frontend analog of the
   frozen-test repair loop. (Directly closes the "vs polsia" epic; maps to APP-4.)
3. **Three loops** — BOSS has the inner (impl+tests) and outer (human PR) loops but nothing plays the
   middle **developer/design loop** ("where does the UI need improvement"). That void *is* the
   design-blindness. Ng frames taste as *learnable context*, not magic — so feed the critic concrete
   design context rather than expecting innate judgment. (Maps to APP-2.)
4. **AI tokens cheap, human tokens gold + a persistent SPEC.md** — before building full RAG, adopt the
   cheap proxy: a **machine-written per-run architecture/spec digest** (public interfaces,
   conventions, design tokens) injected into every stage's prompt — poor-man's whole-repo retrieval
   that buys coherence for pennies. (De-risks the RAG epic; maps to APP-5.)
5. **Lazy prompting is conditional** — only when the model has context *and you can quickly assess the
   output*. BOSS is maximally lazy on visual quality — the one axis the model can't self-assess —
   violating the precondition. Design prompts must be deliberately *non-lazy*.
6. **Open platforms beat power plays** — single-provider dependence is a point of instability. BOSS's
   four-lane routing already hedges; formalize a **hybrid frontier+OSS cascade on the design axis**
   (cheap OSS draft → frontier design-critic only when the visual gate flags). (Maps to APP-6.)

**Top 5 output upgrades:** real visual gate (browser screenshot→critique→repair) · per-stage
`$/verified` via trace error-analysis · design-axis cascade · machine-written SPEC digest ·
budget-variable repair depth.

---

## Roadmap — mapped to Plane (Engine & Model Quality module)

| Improvement | Ticket | Status |
|---|---|---|
| Refute panel + confidence score | #9 (child: refute-panel) | **DONE** — this branch (`feat/vag-phase1-refute-panel`) |
| Meta-eval the verifier (labeled bugged/clean corpus, TP/FP gate) | #9 child | queued (highest-order verifier upgrade) |
| Mutation-gate the frozen tests before impl + property tests | #9 child | queued |
| Cross-run eval ratchet from human rejections | #9 child | queued |
| Bounded generator↔verifier debate on contested cases | #9 child | queued |
| Budget-variable repair depth + escalate-on-`$/verified` | #9 child | queued |
| Generator/verifier independence contract | #9 child | partial (panel model-diversity landed) |
| Real visual gate (screenshot→critique→repair) | #94 (APP-4) | enriched with Ng's mechanism |
| Machine-written SPEC digest per stage | #95 (APP-5) | enriched — cheap RAG bridge |
| Design-axis frontier+OSS cascade | #96 (APP-6) | enriched |
| Skills executor as verification-side tools | Skills epic #44 | cross-module reference |

## Sources

Andrew Ng, *The Batch* / deeplearning.ai: Agentic Design Patterns Part 1–5 (Reflection, Tool Use,
Planning, Multi-Agent Collaboration); Improve Agentic Performance with Evals & Error Analysis Part 1–2;
We Iterate on Models, We Can Iterate on Evals Too; We Need Better Evals for LLM Applications; Three Key
Loops for Building Great Software; Coding Agents Accelerate Some Tasks More Than Others; Make All Your
Tokens (and Your Brainwork) Count; Open Platforms Beat Power Plays.

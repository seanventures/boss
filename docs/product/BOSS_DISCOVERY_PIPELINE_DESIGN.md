# BOSS Design — The Discovery Pipeline (Research‑Driven Outcome Discovery)

**Principle:** *Act on signal, not speculation* — by doing **deep, cited market research before
building**, then prototype → pilot with real users → use‑before‑buy. (We explicitly drop
"credit‑cards‑down" as the validation bar; it's one marketing tactic, not how serious buyers
commit.)

---

## 1. The gap this closes
BOSS is strong at two stages and missing the one *before* them:

| Stage | BOSS today |
|---|---|
| **Decide what to build** (market/idea research) | ❌ **missing** |
| Turn an outcome into a spec (PRD via stakeholder meetings) | ✅ `meeting_service` |
| Build it (governed coding pipeline) | ✅ code‑task pipeline |
| Ship/preview it (Tier‑1/2/3 deploy) | ✅ |

Today a human must *already know* the outcome. The Discovery Pipeline lets BOSS **research a
domain/idea/signal, reach real signal sources, and produce evidence‑backed candidate outcomes** —
then hand the chosen one to the *existing* PRD‑meeting → coding → deploy chain.

It is a **governed, evidence‑gated, multi‑agent pipeline** — a sibling to the meeting and coding
pipelines — and a **customizable skillset**, not a generic "do research" prompt.

---

## 2. What goes in / what comes out
**Input (a "seed"), any of:**
- a raw idea / hypothesis ("AI scribe for veterinary clinics"),
- a problem or domain ("reduce no‑shows for small clinics"),
- a signal ("we got 12 support asks for X this week"),
- an open brief ("find under‑served opportunities adjacent to our product"),
- **plus operator CONTEXT** (our ICP, market, constraints, existing product, preferred sources) —
  this is what makes it *customized*, not generic.

**Output:**
- a durable **Discovery Dossier** (the receipt: brief → findings → analysis → critique → outcomes,
  every claim cited),
- **ranked candidate Outcomes**, each scoped with rationale, confidence, the signals it rests on,
  a target pilot segment, and a recommended first **prototype slice**,
- surfaced in the **decision inbox** ("Pursue / Refine / Pass / Research‑more"); a *Pursue* becomes
  a durable **Initiative** that flows into the PRD meeting → coding pipeline.

---

## 3. The pipeline (phases)
Each phase is run by a role‑agent (reusing the meeting/coding agent‑runner), produces a durable,
**cited** artifact, and scales with budget‑intensity (deeper sweeps at higher intensity).

**P0 · Frame** — *research_lead*
Turn seed + context into a **Research Brief**: the hypothesis, the questions that must be answered
(Is the pain real? Frequent? Painful? Who has it? Who else solves it, and where's the gap? Why now?
What's our wedge?), the **signal sources to consult**, and the **evidence bar** for "worth
pursuing." Dedupes against prior dossiers so we don't re‑research. → `research_brief`

**P1 · Signal Sweep** — *signal_scout × N (parallel, multi‑modal)*
**Reach the market signal sources** — each scout takes a different angle so none is blind to what
the others miss:
- **Competitive/web scan** (web search+fetch): who's in the space, positioning, pricing, gaps.
- **Pain mining** (communities/forums/reviews): what people actually complain about / ask for —
  *frequency* and *intensity* of pain.
- **Demand/interest** (search trends, keyword volume, app‑store/review signals): is anyone looking?
- **First‑party signals** (if connected: support tickets, sales notes, churn reasons, requests).
- **Timing/why‑now** (news, regulation, tech, behavior shifts).
Every finding carries a **citation** (URL / source id). → `signal_findings[]`

**P2 · Synthesize** — *market_analyst*
Structured analysis, **every claim cited to a P1 finding**: problem & pain (graded), target
segment/ICP, demand evidence, competitive landscape + our wedge, rough market size, timing, and
**risks/why‑it‑might‑fail**. → `market_analysis`

**P3 · Adversarial Vetting** — *demand_skeptic* (the BOSS moat, applied to research)
Challenge the analysis the way the adversarial reviewer challenges code. For each key claim: is it
**evidence‑backed or wishful**? Are the sources **credible**? What's the **strongest disconfirming
evidence**? Would a sharp investor poke a hole? **Defaults to skeptical.** Unsupported claims are
removed or down‑confidenced; thin areas route back to P1/P2. This is how "signal over speculation"
is *enforced*, not just stated. → `critique` + adjusted confidence

**P4 · Shape Outcomes** — *outcome_shaper*
Convert vetted insight into 1–N candidate **Outcomes**, each: what to build (scoped), the wedge,
the **target pilot customers** (who needs it now), a recommended **first prototype slice** (feeds
the coding pipeline — a *decent prototype to pilot*, not a full build, not a paywall), rationale +
confidence + the citations it rests on, and a **validation plan = pilot → measure usage →
then monetize** (use‑before‑buy, your model). → `candidate_outcomes[]`

**P5 · Discovery Gate** — *fail‑closed, evidence thresholds*
An outcome is only **proposed** if it clears the bar (configurable): e.g. ≥N independent credible
sources, a cited real pain with frequency, a named target segment, a stated differentiation,
disconfirming evidence explicitly considered, confidence ≥ threshold. Speculation **fails** (or is
marked *"needs more research"*). Emits the **Discovery Dossier** as the durable receipt.

**P6 · Surface** — *decisions*
Candidate outcomes → **decision inbox** (Pursue / Refine / Pass / Research‑more) with the dossier
attached. *Pursue* → **Initiative** → existing **PRD meeting → coding → deploy** chain.

---

## 4. New role pack (skill‑packaged)
`research_lead · signal_scout · market_analyst · demand_skeptic · outcome_shaper` — defined like
the existing meeting/coding roles, packaged as **`discovery.*` skills**
(`discovery.frame`, `discovery.signal_sweep`, `discovery.synthesize`, `discovery.vet`,
`discovery.shape`) so they're **composable and customizable per domain** — the same way
`coding_pipeline.*` and the PRD‑meeting skills work today. Domain‑specific research templates
(e.g. "B2B SaaS", "clinical", "marketplace") are skill variants, not hardcode.

## 5. Tools & signal sources (the part BOSS lacks)
- **Agent‑accessible web** — search + fetch with mandatory citations. *(Open item: BOSS's
  in‑sandbox agents need a governed web tool; the harness has web search/fetch, the agents don't
  yet.)*
- **Configurable connectors** for first‑party signals (support inbox, CRM, analytics) — opt‑in,
  per‑operator.
- **Source registry** — operators pick/weight sources (so research is tailored, not one‑size).

## 6. How it reuses the existing kernel (so it "fits fluidly")
- **Agent‑runner + roles** — same seam as meetings/coding.
- **Phase machine + fail‑closed gate** — same pattern as the coding quality gate (here: an
  *evidence* gate).
- **Decisions + Initiatives + receipts** — outcomes are decisions; dossiers are receipts; no silent
  drop.
- **Budget‑intensity** — research *depth* scales with the intensity dial (cheap scan → deep sweep).
- **Cadence/autonomy** — later, the always‑on loop (R6) can run discovery on a schedule, bounded by
  autonomy level.

## 7. Data model (new)
- `signals` — raw inbound signals (source, payload, ts).
- `discovery_runs` — phase/status (like `meeting_runs` / `code_task_runs`), with the brief +
  per‑phase state + the gate verdict.
- `discovery_dossiers` — the durable artifact (brief, findings, analysis, critique, outcomes,
  citations).
- `candidate_outcomes` → on Pursue, become `initiatives`.

## 8. The BOSS moat, applied to research
Mandatory **citations**, **adversarial vetting** of the thesis, a **fail‑closed evidence gate**
(no speculation passes), the **dossier as a receipt**, and **human‑gated** outcome selection. This
is what makes BOSS discovery *trustworthy* vs. a model just "doing research" — same reason our
coding pipeline beats "an agent that writes code."

## 9. Build phasing (after design sign‑off)
- **D1 — Spine:** P0→P6 with **web‑only** signals (search+fetch), single scout, the evidence gate,
  the dossier, and decision surfacing. Proves the loop end‑to‑end on one domain.
- **D2 — Breadth & rigor:** parallel multi‑modal `signal_scout`s (pain mining, demand, timing),
  the `demand_skeptic` vetting pass, operator context injection + source registry.
- **D3 — Connectors & depth:** first‑party connectors, dedup vs prior dossiers, intensity‑scaled
  depth, and (with R6) scheduled/always‑on discovery.

## 10. Open questions to resolve before building
1. **Agent web access** — give BOSS's pipeline agents a governed web search/fetch tool (biggest
   prerequisite). In‑sandbox or a control‑plane tool the agent calls?
2. **First‑party connectors** — which sources first (support inbox? analytics? CRM?), and how are
   they authed/scoped?
3. **Evidence bar (P5)** — concrete default thresholds for "worth pursuing" (sources, confidence,
   pain frequency)?
4. **Validation reframe** — confirm we replace "credit‑cards‑down" with **prototype → pilot →
   usage signal → then monetize** as the demand‑validation model (changes R2 in the PRD too).
5. **Hand‑off** — does a Pursued outcome auto‑start the PRD meeting, or wait for a second human go?
6. **Scope of the first slice (D1)** — pick one concrete domain/seed to prove it on.

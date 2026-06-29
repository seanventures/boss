# BOSS PRD — From Directed Delivery to Autonomous Outcomes

**Status:** Draft for review (not yet scheduled)
**Author:** drafted with Claude (paramg)
**Context:** Competitive read of FeltSense ("agentic founders") surfaced where BOSS's autonomy
loop stops short. This PRD bridges those gaps **in BOSS's own idiom** — durable work kernel,
evidence‑backed gates, the decision inbox, budget‑intensity, autonomy levels, receipts — rather
than copying a founder‑bot. See `BOSS_GAP_ANALYSIS.md` for the prior phase plan this extends.

---

## 1. Where BOSS stands vs the autonomous frontier

BOSS today is a **trusted autonomous *delivery* + chief‑of‑staff OS**: a human gives an
*outcome* → BOSS plans → builds via the governed pipeline (design → tests → implement →
adversarial review → security → mutation → fail‑closed gate) → **gated PR** → Tier‑1/2/3 deploy
(preview → container → smoke canary), surfacing only the decisions that need human judgment, with
receipts for everything.

The frontier (e.g. FeltSense) runs a *wider* loop — **sense the market → validate demand with
real signal → build → go to market → measure unit economics**, across a **fleet** of bets. BOSS
is deeper on **trust/governance/quality**; it is missing the **front of the funnel** (what to
build + is it wanted), the **portfolio** layer (many outcomes, governed allocation), the **full
lifecycle** (live + monetized + measured), and **always‑on** proactivity.

**Thesis:** BOSS's moat is *trust* — governed, evidence‑backed, auditable autonomy. We extend the
loop **outward** (discovery, validation, portfolio, lifecycle, proactivity) **without diluting the
moat**: every new step is itself an *evidence‑gated, decision‑surfaced, receipted* BOSS primitive.

### What we deliberately do NOT copy (moat to protect)
Fail‑closed quality gates, "no 'done' without evidence", human override with recorded reason,
cross‑model adversarial review, the durable no‑silent‑drop kernel, chief‑of‑staff decision
surfacing. Speed‑first autonomy without these is exactly the failure mode BOSS exists to prevent.

---

## 2. Guiding principles for these features
1. **Every new step is evidence‑gated** — discovery, demand, promotion all produce a *gate* with a
   recorded verdict, like the coding quality gate.
2. **Everything is a durable kernel object** — Signals, Initiatives, Validation runs, Promotions,
   Ledger entries are tasks/decisions/receipts, not new silos.
3. **The human stays the editor‑in‑chief** — BOSS *proposes*; the decision inbox is where humans
   pick, kill, double‑down. Autonomy level + budget‑intensity bound what runs unattended.
4. **Outward, not just faster** — we add the missing *stages* of the loop, reusing the pipeline,
   canaries, budget, and deploy tiers we already built.

---

## 3. Roadmap items (use‑case‑driven)

### R1 — Outcome Discovery: Signal → proposed Initiatives
**Gap:** BOSS only acts on an outcome a human types. It can't answer *"what should we build?"*
**BOSS‑native design:** a **Signal intake** (operator notes, a feed/inbox, support tickets,
analytics, a pasted brief) → a discovery agent synthesizes **candidate Outcomes** each with a
rationale, scope sketch, confidence, and a cited signal → posted to the **decision inbox** as
*"Pursue / Refine / Pass"* cards. Approved → becomes a durable **Initiative**.
**Use case:** *Operator connects a signal source; each morning BOSS proposes 3 scoped outcomes
("Add team invites — 12 support asks this week"), each with rationale + confidence; operator
approves one → it becomes an Initiative with a plan + tasks.*
**Fits:** `intake` (initiatives/plans) + decision inbox + receipts. New: `signals` table, a
discovery agent role, `POST /v1/signals`, discovery → decision cards.
**Success:** ≥1 approved Initiative originates from a signal (not a human brief); every proposal
cites its signal + carries a confidence; passes are recorded (no silent drop).

### R2 — Demand Validation Gate: evidence before deep build  ★ highest impact
**Gap:** BOSS validates *code* quality, never *market* demand. It will happily build something
nobody wants. The frontier's core tenet is *"signal over speculation."*
**BOSS‑native design:** a new **gate type** in the outcome lifecycle. Before the full build, BOSS
ships a **minimal validation artifact** (landing page + intent capture / waitlist / pricing‑intent
/ a Stripe‑checkout stub) via the existing Tier‑1/2 preview+deploy, runs a **demand canary** that
measures real signal over a window (signups, clicks‑to‑intent, checkout starts) — or a simulated
panel in dev — and the **Validation Gate** returns go / no‑go / pivot, recorded as evidence + a
decision, exactly like the quality gate. Full build only proceeds past a passed validation gate.
**Use case:** *Outcome = "premium analytics tier." BOSS ships a pricing page + "Notify me" intent
capture first, runs the demand canary for N days (or a simulated cohort), and gates the full build
on ≥X% intent — otherwise surfaces a pivot decision. No deep build on speculation.*
**Fits:** reuses Tier‑1/2 deploy + the canary/smoke harness + the quality‑gate pattern + decisions.
New: `validation_runs`, a `demand_canary`, a `validation_gate` in the lifecycle, intent‑capture
templates the code agent can emit.
**Success:** an outcome can be killed/pivoted *before* a full build purely on demand evidence; the
decision cites the measured signal; the waitlist/pricing apps we already built are reusable probes.

### R3 — Initiative Portfolio & governed allocation (the "fleet", BOSS‑style)
**Gap:** BOSS runs **one outcome at a time** (known work‑graph/parallelism gap). No portfolio,
no resource allocation, no evidence‑driven kill/double‑down.
**BOSS‑native design:** Initiatives as durable work‑graphs; the **budget‑intensity dial + autonomy
level allocated *across* initiatives**; BOSS auto‑pauses initiatives failing their validation/eval
gate and **surfaces a "double‑down / reallocate / kill" decision** citing each initiative's signal.
A **Portfolio view** in the console.
**Use case:** *Operator runs 5 initiatives under a fixed budget envelope; BOSS allocates spend by
intensity, auto‑pauses the two whose demand gate failed, and surfaces one "double‑down on the
winner" decision with the comparative evidence.*
**Fits:** budget service + autonomy levels + decisions + the work‑graph item already flagged in the
gap analysis. New: portfolio allocation policy, `GET /v1/portfolio`, per‑initiative budget scoping.
**Success:** N initiatives run under one governed budget; allocation shifts are decisions with
evidence; killed initiatives stop spending automatically.

### R4 — Full‑lifecycle deploy (Tier 4: live + monetized + verified)
**Gap:** BOSS stops at staging (preview/container/smoke). The frontier ships *live, monetized,
customer‑facing* products.
**BOSS‑native design:** extend the deploy tiers → **promote to a live environment** with
integration/payment adapters (Stripe), **synthetic‑transaction canaries** against the live product
(a real test purchase, a real signup round‑trip), governed **promotion gates** + receipts +
**rollback**. Same fail‑closed discipline as code: no prod promotion without a green live canary.
**Use case:** *A validated outcome → BOSS promotes to a live URL with Stripe wired, runs a
synthetic‑purchase canary end‑to‑end, and gates prod on it; a failed canary auto‑rolls‑back.*
**Fits:** Tier‑1/2/3 deploy + canaries + the goldeneye/registry deploy path + receipts. New:
integration adapters (payments first), synthetic‑transaction canary, promotion gate + rollback.
**Success:** a validated outcome reaches a live, transaction‑verified URL through governed
promotion; rollback is automatic on a failed live canary.

### R5 — Outcome P&L / Value Ledger (unit economics, BOSS‑style)
**Gap:** BOSS measures *execution cost* (FinOps), not the *business value* of an initiative.
**BOSS‑native design:** extend FinOps into a per‑initiative **value ledger** — cost (build + run)
vs value (intent → signups → revenue), each entry tied to a receipt; surfaced on the dashboard and
**fed into R3's allocation decisions**.
**Use case:** *The dashboard shows each initiative's spend vs validated demand/revenue; the
portfolio "double‑down/kill" decisions quote the ledger ("$40 spent, 120 intents, 3 paid").*
**Fits:** budget/receipt kernel + dashboard. New: `value_ledger`, intent/revenue ingestion, a
dashboard P&L tile.
**Success:** every initiative has a cost‑vs‑value line; allocation decisions cite it.

### R6 — Always‑On Outcome Loop (proactive autonomy, bounded)
**Gap:** BOSS is request‑driven; the frontier is always‑on. BOSS has autonomy levels + a loop
skill but no continuous outcome engine.
**BOSS‑native design:** a **continuous cadence** that runs `sense (R1) → validate (R2) → build →
promote (R4)` on a schedule, **bounded by autonomy level + budget‑intensity**, surfacing only the
decisions that need a human. The chief‑of‑staff, awake.
**Use case:** *Operator sets intensity + autonomy once; BOSS runs the loop nightly; the operator
wakes to: 3 proposed outcomes, 1 with validated demand, 1 gated PR awaiting approval — no
unattended prod changes above the set autonomy level.*
**Fits:** autonomy auto‑upgrade/downgrade + budget caps + decisions + the existing loop mechanism.
New: a scheduler/cadence that chains R1‑R5, hard‑bounded by autonomy + budget.
**Success:** an overnight run produces evidence‑gated progress with zero out‑of‑policy actions;
everything above the autonomy ceiling waits in the decision inbox.

### R7 — Scoped human work‑orders at the edges *(optional / later)*
**Gap:** BOSS's human‑in‑the‑loop is *approvals*; the frontier also dispatches *paid scoped tasks*
(verify a Stripe account, handle a regulatory step).
**BOSS‑native design:** extend decisions into **human work‑orders** — a scoped task with inputs,
a definition‑of‑done, and a receipt on completion — for things agents can't do (account
verification, KYC, infra). Same durable kernel, no silent drop.
**Success:** a blocked task can emit a scoped human work‑order that, when completed, resumes the
initiative with a receipt.

---

## 4. Suggested phasing
- **Phase A (highest leverage):** **R2 Demand Validation Gate** — it's the single biggest tenet we
  lack, reuses everything we just built (preview/deploy/canary/gate), and immediately changes BOSS
  from "builds what it's told" to "builds what's *validated*." Pair with **R1 (lightweight)** so an
  outcome can *originate* from a signal.
- **Phase B:** **R3 Portfolio + R5 Value Ledger** — once multiple validated outcomes exist, govern
  them as a fleet with cost‑vs‑value allocation.
- **Phase C:** **R4 Tier‑4 live/monetized deploy** — close the lifecycle to live, transaction‑
  verified products.
- **Phase D:** **R6 Always‑On loop** (+ **R7 work‑orders**) — chain it all into a bounded,
  proactive engine.

## 5. What success looks like (whole‑PRD)
A founder gives BOSS *a signal* (not even a spec). BOSS proposes outcomes, **validates demand
before building**, builds only the validated ones through the governed pipeline, promotes the
winners to **live, transaction‑verified** products, runs them as a **governed portfolio** with a
**cost‑vs‑value ledger**, and does it **continuously** — surfacing only the decisions that need a
human, with a receipt for every step. Same trust moat, a much wider loop.

## 6. Open questions for review
- R2: live demand window vs. simulated panel in dev — what's the acceptance bar, and do we need
  real traffic (ads) or is organic/intent enough for v1?
- R4: which live target (goldeneye? a dedicated box? a cloud PaaS?) and is Stripe the first adapter?
- R3: allocation policy — purely demand‑evidence‑driven, or operator‑weighted?
- Scope of v1: do we start with R2 alone as a provable slice, then expand?

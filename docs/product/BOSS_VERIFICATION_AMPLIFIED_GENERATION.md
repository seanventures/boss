# BOSS — Verification‑Amplified Generation (VAG)
### The wedge: frontier‑quality autonomy at open‑model cost

**Tenet:**
> *A weaker, cheaper model wrapped in strong verification produces frontier‑quality outcomes, because the harness catches and fixes what the model gets wrong. So the cost play and the quality play are the same play — through the harness, not the weights.*

**Status:** design spec (phased). **Owner:** paramg. **Last updated:** 2026‑07‑03.

---

## 0. Why this is a wedge (thesis + business case)

Every autonomous‑coding competitor (feltsense and friends) is, under the hood, a **wrapper around a frontier model**. That makes their quality good but their **unit cost high and structurally fixed** — they can't offer a generous free tier, and their margin is hostage to Anthropic/OpenAI pricing.

BOSS can attack from a different axis. BOSS's differentiator was never the model — it's the **governed pipeline** (write‑tests → implement → adversarial review → security → mutation → fail‑closed gate). That pipeline is a **verification engine**, and a verification engine is a **quality amplifier**: it lets a *cheaper* generator reach *frontier‑quality outcomes*, because errors are caught and repaired instead of shipped.

That flips the economics:

| | Frontier‑wrapper (competitors) | BOSS (VAG) |
|---|---|---|
| Cost driver | $/token of a frontier model | $/**verified outcome** on a cheap model |
| Free trial | expensive → hard to offer | **cheap → can offer generously** |
| Quality lever | the model's weights | the **harness** (which BOSS owns) |
| Moat | none (anyone can call the same API) | the harness + the data flywheel |

**The GTM this unlocks:**
1. **Open‑source the BOSS engine** (harness + orchestration) so anyone can self‑host.
2. Stand up a **hosted cheap‑inference tier** so trial users **don't need to buy frontier API access** to try BOSS — the #1 friction today.
3. **Free tier** (hosted cheap models, capped) → **paid tier** (frontier escalation, private stacks, higher limits, fine‑tuned models). Convert on *value felt*, not on a paywall at the door.

The bet: **the harness is the moat, not the code.** Giving away the engine drives adoption; the money is in the hosted verification + inference + the proprietary data flywheel (below). This is exactly why open‑sourcing is safe here and dangerous for a frontier‑wrapper.

---

## 1. The principle, formalized

Treat outcome quality as a function of three inputs, not one:

```
quality(outcome) ≈ f(model_strength, verification_strength, inference_budget)
```

A frontier‑wrapper maximizes only `model_strength`. VAG says: **you can hold quality constant while lowering `model_strength` — if you raise `verification_strength` and/or `inference_budget`, both of which are cheap relative to frontier tokens.**

**The unit of value is `$/verified‑outcome`, not `$/token`.** A cheap model that needs 3 attempts + verification still wins massively if each token is 10–30× cheaper and the harness makes the attempts *productive*.

**The one invariant that makes or breaks this (read twice):**
> Verification must be **independent of the generator** and **hard to game.** If the tests are weak or written by the same model that writes the code, a cheap model will produce *plausible garbage that passes* — and you'll have amplified confidence in wrong output. **The failure mode of VAG is not low quality; it's false confidence.** Every phase below is ultimately in service of keeping the **false‑pass rate** near zero.

---

## 2. Architecture

Three planes. BOSS already has two of them; the third is the new work.

```
┌───────────────────────────────────────────────────────────────────────┐
│  ECONOMIC CONTROLLER  (new)                                            │
│  routes + scales by $/verified-outcome; owns the escalation ladder     │
│  and per-tenant cost/quality dials.                                    │
└───────────────┬───────────────────────────────────┬───────────────────┘
                │ picks model + budget               │ reads pass/fail + confidence
        ┌───────▼─────────┐                  ┌───────▼──────────────────┐
        │ GENERATOR PLANE │  produces diff   │ VERIFICATION PLANE       │
        │ swappable models│─────────────────▶│ (the amplifier)          │
        │ cheap → frontier│   candidate(s)   │ tests · adversarial      │
        │ model-agnostic  │◀─────────────────│ review · security ·      │
        └─────────────────┘   repair signal  │ mutation · fail-closed   │
                                              │ gate → confidence score  │
                                              └──────────────────────────┘
```

- **Generator plane** — already model‑agnostic (`BOSS_SANDBOX_GUEST_ENGINE`, `OPENAI_BASE_URL`, claude/OpenAI/Azure/any OpenAI‑compatible endpoint). Runs in the isolated micro‑VM.
- **Verification plane** — already exists as the governed pipeline. VAG *hardens* it and makes it emit a **confidence score**, and requires generator/verifier **independence**.
- **Economic controller** — new. Decides *which* model and *how much* inference to spend, driven by measured `$/verified‑outcome`.

---

## 3. Mechanisms

### 3.1 Model tiering & routing
Register models in tiers (e.g. `cheap`: DeepSeek‑V3/Qwen2.5‑Coder · `mid`: DeepSeek‑R1/larger open · `frontier`: Claude). Default the **generator** to `cheap`; reserve `frontier` for escalation and high‑risk tasks. Keep the **verifier engine separate from the generator engine** (BOSS already separates build vs review engine) — non‑negotiable for the independence invariant.

### 3.2 The verification harness (must be *strong* before anything else)
The amplifier only works if it's trustworthy. Investments, roughly in order of leverage:
- **Independent test generation** — tests written by a *different* model/seed than the implementer; ideally partly spec‑derived, not code‑derived.
- **Adversarial review panel** — reviewers prompted to *refute*, on a different engine; majority‑to‑pass.
- **Mutation / property testing** — proves the tests actually constrain behavior (kills "tests that pass on anything").
- **Fail‑closed gate** — no PR without green, independent verification.
- **A calibrated confidence score** per outcome (from gate margin + mutation score + reviewer agreement) — the controller's key input.

### 3.3 Inference‑time scaling (close the quality gap without training)
- **Best‑of‑N + verifier selection** — generate N cheap candidates, let the verifier pick/merge. N cheap shots often beat 1 frontier shot at lower cost.
- **Iterative repair loop** — generate → verify → feed the *specific* failures back → regenerate. Cheap models improve a lot with concrete failing‑test feedback (this is where the harness "fixes what the model gets wrong").
- **Verifier self‑consistency / debate** — for the *verification* side, to drive false‑pass down.

### 3.4 The escalation ladder
Spend the *least* compute that clears the gate:
```
cheap (1 shot) → cheap (best-of-N + repair) → mid → frontier → human decision
      └── stop the moment independent verification passes with confidence ≥ θ ──┘
```

### 3.5 Difficulty / risk pre‑routing
Cheap pre‑estimate of task difficulty + risk tier (BOSS already infers a risk tier) to *start* higher on the ladder for gnarly/high‑blast‑radius work — avoids wasting cheap attempts on tasks that will obviously escalate.

### 3.6 The economic controller
Turns the above into a policy driven by data: per (task‑type, model, tier) it knows the empirical pass‑rate and `$/verified‑outcome`, and picks the ladder rung that minimizes expected cost to a verified result — under a **per‑tenant budget + quality dial** (a free‑tier user gets a cheaper, more‑escalation‑averse policy than an enterprise one).

---

## 4. Metrics & evaluation

Track per model / tier / task‑type:
- **`$/verified‑outcome`** (primary) — total inference $ (gen + verify + retries) per gate‑passing PR.
- **Gate pass‑rate** and **escalation rate** (how often cheap → frontier).
- **`false‑pass rate`** (the guardrail — measured via human spot‑audits + a held‑out "known‑bad" suite). **A ceiling on this is a hard product invariant.**
- Wall‑clock latency; tokens/attempt.

Stand up an **eval suite** that grows from real tasks (start with the MVP use cases). Every phase is judged against baseline (**Claude‑solo cost + quality**) on this suite.

---

## 5. Phased roadmap

> Principle: **harden the amplifier before turning down the model.** Cutting cost on a weak harness manufactures false confidence — the one thing that would sink the product.

### Phase 0 — Instrument & baseline *(measure before optimizing)*
- Per‑call model‑agnostic routing hooks (mostly present) + log tokens, $, retries, gate outcome, confidence per role.
- Build the eval suite (MVP use cases + a growing set).
- **Exit:** a dashboard showing **Claude‑solo `$/verified‑outcome` and pass‑rate** on the suite = the number to beat.

### Phase 1 — Harden the verifier *(the amplifier)*
- Generator/verifier **independence** enforced; independent test‑gen; adversarial refute‑panel; mutation/property testing; the **confidence score**.
- Instrument **false‑pass rate** (human spot‑audit + known‑bad suite).
- **Exit:** false‑pass rate below the agreed ceiling with the *current* (frontier) generator — proving the harness is trustworthy *before* we weaken the model.

### Phase 2 — Model tiering & routing + bake‑off
- Wire DeepSeek‑V3/R1 + Qwen2.5‑Coder alongside Claude (via OpenRouter/Fireworks to start).
- **Bake‑off** on the eval suite → per‑task‑type routing policy (cheap default, frontier escalation).
- **Exit:** ≥X% of tasks completed on cheap models at equal gate‑pass + false‑pass, at materially lower `$/verified‑outcome`.

### Phase 3 — Inference‑time scaling
- Best‑of‑N + verifier selection; iterative repair loop; verifier self‑consistency.
- **Exit:** cheap‑model **outcome quality matches frontier‑solo** on the suite, still cheaper end‑to‑end.

### Phase 4 — Economic controller
- Data‑driven ladder + per‑tenant budget/quality dials; stop‑on‑verified.
- **Exit:** the system **auto‑minimizes `$/verified‑outcome`** per task under a budget, no hand‑tuning.

### Phase 5 — OSS engine + hosted cheap‑inference tier *(the wedge)*
- Package the engine (harness + orchestration) as **open source**, self‑hostable.
- Stand up a **hosted cheap‑inference tier** so trial users need **no frontier API key**.
- **Free tier** (hosted cheap, capped) → **paid** (frontier escalation, private stacks, higher limits).
- **Exit:** a new user goes idea → verified PR **for free, without any frontier API key.**

### Phase 6 — Data flywheel & fine‑tuning *(compounding moat)*
- Capture every `(task, diff, gate‑outcome, confidence)` as labeled data (byproduct of running BOSS).
- Fine‑tune small open models (LoRA) on **accepted PRs per stack/domain** → cheap models specialized to *your customers' code*.
- **Exit:** a fine‑tuned cheap model beats generic open models on customer‑stack tasks → cost drops *and* quality rises with usage.

---

## 6. Open‑source & hosting strategy

- **Open source:** the engine — harness, governed pipeline, orchestration, micro‑VM runner, model‑agnostic adapters. Anyone can self‑host and BYO‑keys.
- **Hosted (the business):** the cheap‑inference tier, the economic controller as a service, private/fine‑tuned models, the data flywheel, enterprise governance/SLA.
- **Why OSS is safe here:** a frontier‑wrapper open‑sourcing gives away everything (the value *is* the API call). BOSS open‑sourcing gives away the *client*; the value is the **hosted verification + cheap inference + proprietary data**, which don't travel with the code.
- **Tiering:** Free (hosted cheap, rate‑capped, public repos) → Pro (frontier escalation, private repos, higher limits) → Enterprise (dedicated/fine‑tuned models, on‑prem harness, SLA, audit).

---

## 7. Risks & how to get it right

1. **False confidence (the #1 risk).** Weak verification + cheap generator = plausible garbage that passes. **Mitigation is the whole spec:** independence, mutation/property tests, adversarial refute‑panels, a tracked false‑pass ceiling, human spot‑audit — and **Phase 1 before Phase 2.**
2. **Cost blow‑up from naive best‑of‑N.** Mitigation: the economic controller + stop‑on‑verified + per‑tenant budgets.
3. **Open‑model provider lock‑in / data residency** (e.g. DeepSeek's own API is China‑hosted). Mitigation: multi‑provider abstraction (US‑hosted weights on Together/Fireworks) + a self‑host path at volume.
4. **OSS cannibalization.** Mitigation: the moat is hosted harness + inference + data, not the code; keep the highest‑value pieces (fine‑tuned models, managed verification, enterprise governance) in the hosted tier.
5. **Latency of multi‑sample + verify.** Mitigation: parallelize generation/verification (the micro‑VM host has capacity), cache, and gate N by difficulty.
6. **Verifier is itself a model (could be wrong).** Mitigation: verification is *mostly executable* (tests, mutation) — grounded in code execution, not just LLM judgment — with LLM review as one weighted signal, not the sole arbiter.

---

## 8. First concrete step (next)

A **model bake‑off harness** (a slice of Phase 0 + 2): run the MVP use cases through Claude vs DeepSeek vs Qwen inside the *existing* governed pipeline, and report per model: gate pass‑rate, **false‑pass rate**, retries, and **`$/verified‑outcome`**. That single artifact turns this whole strategy from a thesis into decisions backed by numbers on BOSS's own workload — and it's mostly wiring, because BOSS is already model‑agnostic and gated.

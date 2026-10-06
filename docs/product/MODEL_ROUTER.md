# The model router: which model runs each piece of work

**Status:** built on `mr/model-router` (2026-10-05). **Owner:** operator. **Scope:** Community and
Enterprise. **Off by default:** an install keeps routing exactly as it did until someone switches
*Model routing* to **Smart**.

## Why

BOSS runs a lot of model calls, and they are not all equally hard:

- a one-line copy fix;
- a new full-stack app;
- the adversarial review that decides whether that app ships;
- a chat question about an error message;
- a panel facilitator writing a PRD;
- a research synthesis.

Until now, the model each one got came from several partial routers:

- `model_routing.resolve_job` picked a *source* per job (Build, Review, Chat, Decisions…) from
  settings alone. It never looked at the task.
- Pipeline profiles (`lean`, `balanced`, `strict`, `vag`) classed each build stage `low_cost` or
  `strong`.
- Two one-off escalations moved a stage to the strong lane: a final implement attempt, and a
  test-writing pass that wrote no tests.
- `spend_cap_failover` switched engine once, at Claude's monthly cap.
- Agent Studio stored a model policy (`router` / `pinned` / `local`) that nothing read.
- Meetings ran the facilitator and every panelist on one model.
- Discovery hard-coded Claude Code.

The operator's real policies did not fit any of these:

1. **Important work uses frontier models.** Adversarial review gets Claude Opus, on the Claude
   subscription.
2. **Coding uses a lower Claude model.** Sonnet or Haiku, on the same subscription.
3. **When the subscription is close to its weekly limit,** lower-value work moves to OpenRouter's
   cheap models. Frontier-critical work keeps the subscription's remaining headroom.
4. **Many more combinations are coming:** per role, per step, per task kind, per budget state,
   local models, other subscriptions.

LangChain measured the payoff on their coding agent ([How to build a model router in the
harness](https://www.langchain.com/blog/how-to-build-a-model-router-in-the-harness)). A router in
the harness, choosing a tier from the first message of each thread:

- **kept quality:** a 29.2% merged-PR rate against 27.3% when every thread ran the strongest model
  (not significant);
- **cut median cost by 64%.**

Their two admitted gaps were no mid-flight switching (to protect the prompt cache) and subagents
routing on their own.

BOSS can go further on both:

- **Boundaries.** It already has natural decision points: each workflow step, each build stage,
  each chat thread. A route chosen at a boundary never changes a running session.
- **Outcomes.** It already records outcomes: run scorecards, experiments, ratings.

## The shape

One router, `boss_engine.services.model_router`, sits in the harness. Every model call BOSS makes
reaches its model through it:

- build stages;
- reviews and refute panels;
- workflow steps (agents, skills, decisions);
- chat threads;
- meetings;
- research.

It runs once per boundary, never per token, and records every decision.

```
             ┌───────────────────────────────────────────────────────────────┐
 boundary →  │ RouteRequest: kind, step, role, task text, attempt, pins      │
             └──────────────┬────────────────────────────────────────────────┘
                            │  Snapshot (cached per project, one load per TTL):
                            │  policy · connected sources · headroom · budget state
                            ▼
   (a) CLASSIFY   task → tier + confidence      cache ▸ Jev ▸ rules ▸ cheap model
   (b) POLICY     pins ▸ floors ▸ budget pressure ▸ escalation ▸ allowed sources
   (c) SELECT     tier → the first source+model that can take it now (headroom, failures)
   (d) ESCALATE   signals recorded during the run raise the tier at the NEXT boundary
   (e) RECORD     model_routes row; llm_calls.route_id; scorecard routing summary
                            │
                            ▼
             RouteDecision: tier · engine · model · source · reason in words
```

The five concerns live in separate modules. Each has its own tests and none reaches into another's
data.

| Concern | Module | Pure? |
|---|---|---|
| Tiers and step vocabulary | `model_router/tiers.py` | yes |
| (a) Classifier | `model_router/classifier.py` | yes, except the optional Jev and cheap-model calls |
| (b) Policy: presets, floors, pins, migration from legacy settings | `model_router/policy.py` | yes |
| Subscription headroom | `model_router/headroom.py` | yes; observations are written by telemetry |
| (c) Source selection | `model_router/selection.py` | yes |
| (d) Escalation | `model_router/escalation.py` | yes |
| The facade, snapshot, recording | `model_router/router.py`, `model_router/store.py` | the facade is pure over a snapshot |
| OpenRouter catalogue and prices | `openrouter_catalog.py` | the fetch is the only network call, off the hot path |

### Tiers

There are three tiers, described by what the work needs rather than by a model name:

| Tier | Plain-language criterion (default, editable) | Default on the Claude subscription |
|---|---|---|
| **Fast** | One small, well-specified change or a quick answer. Copy, styling, a typo, a rename, a short question. | Haiku |
| **Balanced** | Real engineering with a clear goal. A feature, a bug fix with behaviour, a new app, tests, a plan, a research synthesis. | Sonnet |
| **Frontier** | Judgment where a wrong call is expensive. Adversarial and security review, sensitive surfaces (auth, money, secrets, migrations), architecture, facilitation, hard repairs. | Opus |

The tier-to-model mapping is a list per tier, not a single value. For example:

- Balanced → Claude Code · sonnet, then OpenRouter · a coder model.
- Frontier → Claude Code · opus, then OpenRouter · claude-opus.

The list is what selection walks when a source is out of headroom, failing or refused.

The legacy pipeline classes map onto tiers: `low_cost` → Balanced and `strong` → Frontier. Fast is
new.

### (a) The classifier: task → tier

A classification is `(tier, confidence 0–1, classifier name, reason)`. It always has a confidence
and it always answers. The chain is a cascade, cheapest first:

1. **Cache.** Results are keyed by a fingerprint of the step kind, task kind, size buckets and
   normalised task text (first 4,000 characters). Build stages share the task, so a build
   classifies once, not once per stage.
2. **Jev** (TypeSafe), used only when it is chosen, a key is held and live Jev calls are not held:
   - **The call.** One typed decision with three options whose descriptions are the operator's
     editable tier criteria. The same call also answers the rigor questions `model_risk_tier`
     already asks, so routing and rigor share one context.
   - **The hold.** Today live Jev calls *are* held (BOSS-451, `typed_decisions.GOVERNANCE_HOLD`).
     The router respects that hold and says so: the UI shows *Jev · paused*.
   - **The timeout.** 800 ms by default. A slower answer falls through to the next step.
3. **Rules** (the deterministic heuristic), which always answer. They read:
   - the step kind (review, facilitation, docs, chat…);
   - the task's own words, through the same boilerplate-stripping and whole-word matching that
     `infer_risk_tier` uses (`trivial` → Fast; incremental → Balanced; new build → Balanced;
     sensitive → Frontier);
   - brief size, repo size and task kind, plus a short list of judgment words (architecture,
     concurrency, threat model…).
   The output carries a confidence: high when signals agree, low when they conflict or the text is
   too short to judge.
4. **Cheap model**, off by default and switched on in *Classifier*. When the rules are unsure
   (below 0.6), a Fast-tier model answers the same typed question. It runs through the governed
   adapter (reserved, receipted, settled), so it never spends outside the budget. It has a 6 s
   timeout, and the rules' answer stands if it fails.

This ordering is FrugalGPT's cascade applied to the classifier itself. The free deterministic
answer goes first, and a model is paid only when that answer is unsure.

### (b) Policy: what the operator says

A `RoutingPolicy` is stored per project in `model_routing_policies` as one JSON document with a
version. It contains:

- **`mode`**: `fixed` (default) or `smart`. It is stored as the project setting
  `model_routing_mode`, so Govern logs a change of mode like any other knob, and an experiment can
  promote it.
- **`tiers`**: per tier, an ordered list of `{engine, model}`.
- **`floors`**: minimum tiers by step, role, job or task kind. Example: *Adversarial review ≥
  Frontier*.
- **`floor_preset`**: `lean`, `balanced`, `strict`, `vag` or `custom`. The pipeline profiles
  *become* floor presets: every step a profile classes `strong` gets a Frontier floor. A custom
  profile's map is converted the same way.
- **`headroom`**: rules like *when Claude Code is near its weekly limit, move Fast and Balanced
  work to OpenRouter*, with thresholds for *tight* and *near limit*.
- **`budget`**: what an orange or red budget does. The default drops one tier, never below a floor.
- **`escalation`**: signal → count → steps, for example *tests failed ×2 → implement up a tier*.
- **`allowed_sources`**: an optional allow-list.
- **`classifier`**: backend (`auto`, `jev`, `rules`, `model`), the timeouts, the model threshold
  and the three criteria texts.

**Pins** are the operator's explicit choices and always win. In order:

1. **A task pin:** `model_config` on the task. Spend-cap failover and experiments write this, so
   both keep working unchanged.
2. **An agent pin:** the Agent Studio model policy.
   - `pinned` → that engine and model;
   - `local` → only local sources are allowed;
   - `router` → routed like any step, with the role's floors.
3. **A job pin:** today's *Who does what* rows. A job set to a source *and* a model is a hard pin.
   A job set to a source only (say *Build → Claude Code*) pins the source and lets the tier choose
   that source's model: Haiku, Sonnet or Opus.

### (c) Source selection: which source takes the tier *now*

Selection walks the tier's list and takes the first target that is all of the following:

- connected;
- allowed;
- not failing (`working`);
- not exhausted (provider-limit capacity);
- not displaced by a headroom rule.

**Headroom.** Every subscription source has a headroom state:

| State | Meaning (defaults) |
|---|---|
| plenty | over 40% of the weekly window left |
| tight | 15–40% left, or the CLI reported `allowed_warning` |
| near limit | under 15% left |
| exhausted | a limit message or a `rejected` status, until its reset time |

Headroom is learned from three signals, best first:

1. **What the CLI reports.** Claude Code's `stream-json` emits a `rate_limit_event` on each call,
   with per-window utilisation and a reset time (five-hour, seven-day, seven-day Opus). BOSS's
   stream parser now keeps it, and telemetry folds it into the source's `observed_capacity`.
2. **Limit messages and their reset times.** These are the CLI's own words:
   - *"You've hit your weekly limit · resets Mon 12:00am"*;
   - *"…session limit · resets 3:45pm"*;
   - *"…Opus limit…"*;
   - the monthly spend limit.
   `provider_limits` recognises all four and parses the reset time.
3. **An estimate from calls.** This is the API-equivalent value of included calls in the trailing
   window, divided by a weekly allowance. The allowance is learned: when a weekly limit is hit,
   whatever was used since the last reset becomes the allowance. The operator can also set it.

A headroom rule displaces a target only when all three hold:

- the source is a subscription;
- its state is at or below the rule's trigger;
- the work's tier is one of the rule's tiers (Fast and Balanced by default).

**Frontier work is never displaced by a headroom rule.** That is the point of moving the rest:
the remaining headroom is kept for the work that needs it. Frontier work leaves the subscription
only when the subscription is *exhausted*, and then it goes to the next Frontier target. Opus has
its own window: an *Opus limit* makes Opus exhausted while Sonnet and Haiku stay available.

**Nothing is stranded.** If every target of a tier is unavailable, selection takes the following,
in order:

1. the same tier on any other connected source with a default model for it;
2. the least-bad target, so the failure that follows names a real source rather than "no model".

### (d) Escalation: start low, move up on evidence

The router starts at the lowest tier the policy allows and moves up only on a signal, and only at
the next boundary. That keeps the prompt cache: a resumed repair session is never switched to a
different model.

| Signal | Default rule | Replaces |
|---|---|---|
| `tests_failed` | implement/solo/repair up one tier after 2 | the single "final attempt on the strong engine" |
| `no_output` | write_tests to Frontier after 1 | the "no test files → retry strong" special case |
| `changes_requested` | implement up one tier after 2 review-repair rounds | nothing (new) |
| `gate_failed` | implement up one tier after 1 failed gate repair | nothing (new) |
| `final_attempt` | Frontier | the legacy last-attempt escalation, kept as a rule |

Every escalation is recorded on the route with its signal and count. The run view shows it as
*↑ escalated*.

### (e) Learning: routes and outcomes

Each decision is written once to `model_routes`. The record holds:

- the tier and the classified tier, with the classifier and its confidence;
- the rules applied, in order;
- the source, engine and model;
- the reason in words;
- any escalation and its signal;
- the latency.

The decision's id is put in the LLM context, so every `llm_calls` row of that boundary carries
`route_id` at no extra cost. Scorecards gain `route_mode`, `route_tier` and a `routing` summary
(tier mix, escalations, classifier), so Observability › Scorecards can group by them.

Experiments gain a **Routing** knob (Fixed vs Smart). An experiment *Smart routing vs fixed* on a
role or workflow splits builds deterministically. It is judged on the existing score, which
already weighs merged PRs, gates, ratings and cost, and Promote sets `model_routing_mode`. This
is the A/B LangChain ran by hand, as a product feature.

## Efficiency

- **The snapshot** loads the following once per project per 20 seconds, in-process:
  - the policy row;
  - project settings;
  - connected sources with capacity;
  - the headroom estimate;
  - the budget state.
  A build loads it once per run.
- **`route()`** is pure computation over the snapshot and the classifier cache. With no network,
  measured p95 is well under 1 ms (`test_model_router_overhead.py` asserts p95 < 5 ms on 5,000
  routes, a ceiling that leaves CI room).
- **Network on the hot path** is only the optional classifier (Jev 800 ms, cheap model 6 s), each
  with its own timeout and the rules' answer as fallback.
- **One write per boundary,** the `model_routes` row. Model calls inside the boundary add nothing:
  `route_id` rides the context they already carry.

## Backward compatibility and migration

- **Nothing changes until Smart is on.** In **Fixed** mode (the default, and what every existing
  install is in), the router runs the legacy resolution unchanged:
  - `route_for_step` → `apply_budget_pressure` → `materialize_route` for build stages;
  - `resolve_job`/`choose` for everything else;
  - the legacy escalations, as they were.
  It then records the result as a route ("Fixed: the balanced profile puts review_code on the
  strong lane"), so *Why this model* works in Fixed mode too. The existing routing, pipeline and
  failover tests pass unchanged.
- **Switching to Smart seeds the policy from what the install already has:**
  - tiers from the lanes:
    - Frontier ← the hard-problems lane (`code_agent_strong_*`) or the build engine's frontier
      model;
    - Balanced ← the routine lane (`code_agent_low_cost_*`) or the build engine's balanced model;
    - Fast ← the build engine's fast model;
    - OpenRouter appended to each tier when it is connected;
  - floors from the project's default pipeline profile;
  - job pins from the *Who does what* rows the operator set (they appear under *Pin a job*);
  - the default headroom rule (Claude near limit → Fast and Balanced to OpenRouter), added only
    when OpenRouter is connected.
- **The Coding Pipeline page** (`?area=pipeline`) redirects to Setup › Model routing. Its profile
  picker is the floor preset there, and custom profiles (Enterprise) remain under *Floors ›
  Custom*.
- **Spend-cap failover** keeps its one-hop requeue with a task pin. In Smart mode the monthly-cap
  message also marks Claude exhausted, so the other routes in the project move without a failed
  run.

### Migration notes

| What an install had | What it becomes | Changes anything on Fixed? |
|---|---|---|
| No routing settings | `model_routing_mode` unset → **Fixed**; no `model_routing_policies` row (the policy is seeded on read) | No |
| *Who does what* job choices (`code_agent_engine`, `code_review_engine`, `chat_llm_engine`, their models) | The same rows, shown under **Pin a job**; on Smart they are pins (engine-only = keep the tier's model on that source) | No |
| Hard problems / Routine work lanes (`code_agent_strong_*`, `code_agent_low_cost_*`) | Seed the Frontier and Balanced tiers when Smart is first used | No |
| Default pipeline profile (`pipeline_profiles.is_default`) | The **Floors** preset; choosing a preset also sets the default profile | No (it *is* the profile) |
| A task's `profile` override | Fixed: as before. Smart: the profile's floors are added to the project's for that build | No |
| Spend-cap failover (`model_config` pin + requeue) | Unchanged; on Smart the monthly-limit message also marks Claude exhausted | No |
| Experiments (harness, review rounds) | Plus **Model routing** (fixed vs smart); assignment writes `spec.routing_mode` | No |
| `?area=pipeline` bookmarks | Land on Setup › Model routing; the page's Factory scale card moved to Operations | — |

Schema (all additive, applied by `ensure_schema`): `model_routing_policies`, `model_routes`,
`provider_model_catalog`, `llm_calls.route_id`, `run_scorecards.route_mode / route_tier /
routing`. Settings: `BOSS_MODEL_ROUTING_MODE` (default `fixed`), `BOSS_ROUTER_JEV_LIVE`
(default off, for when BOSS-451 lifts).

### Where headroom is read from

A subscription connected in Setup carries its windows on its own entitlement row. One
connected through the environment (`CLAUDE_CODE_OAUTH_TOKEN`) has no connection row; telemetry
records its windows on the catalogue row that spends for it (`anthropic_claude_code`), and the
snapshot reads them from there.

## Realistic examples

Each row is an executable test in `engine/tests/test_model_router_examples.py`. The defaults
assumed: a Claude subscription (haiku/sonnet/opus) plus OpenRouter (`deepseek/deepseek-chat`,
`qwen/qwen3-coder`, `anthropic/claude-opus-4.1`), Smart mode, the `balanced` floor preset.

| # | Situation | Step | Expected route | Why (rules applied) |
|---|---|---|---|---|
| 1 | One-line copy fix on a small repo | implement | Fast · Claude Code · haiku | rules: trivial change |
| 2 | New full-stack app | implement | Balanced · Claude Code · sonnet | rules: new build |
| 2b | New full-stack app | review_code | Frontier · Claude Code · opus | floor: adversarial review ≥ Frontier |
| 3 | Claude at 8% weekly headroom, Thursday | docs step (workflow) | Fast · OpenRouter · deepseek-chat | headroom: near limit moves Fast |
| 3b | Same week | security_check | Frontier · Claude Code · opus | Frontier keeps the subscription |
| 4 | Tests failed twice | implement (attempt 3) | Frontier · Claude Code · opus | escalation: tests_failed ×2 |
| 5 | Monthly API budget red, sensitive task | implement | Balanced (from Frontier) | budget red drops a tier |
| 5b | Same | review_code | Frontier | floors never drop |
| 6 | Jev times out | implement | the rules' answer, classifier "rules (Jev timed out)" | classifier fallback |
| 7 | Agent pinned to a local model | agent step | Ollama · llama3.1, whatever the task | agent pin |
| 8 | Claude near limit, OpenRouter down | implement (Balanced) | Claude Code · sonnet | headroom move impossible → stays, never stranded |
| 9 | Chat question about an error | chat thread | Fast · Claude Code · haiku | rules: short question |
| 10 | Panel facilitator | panel_facilitator | Frontier · Claude Code · opus | floor: facilitation ≥ Frontier |
| 10b | Panelist | panel_member | Balanced · Claude Code · sonnet | rules: panel seat |
| 11 | Research synthesis | research_synthesis | Balanced · Claude Code · sonnet | floor: research ≥ Balanced |
| 12 | Fixed mode (untouched install) | implement, balanced profile | exactly the legacy `materialize_route` answer | Fixed |
| 13 | Build pinned to Codex in Smart | implement | Codex, tier still recorded | job pin |
| 14 | Claude weekly limit hit | security_check | Frontier · OpenRouter · claude-opus | exhausted → next Frontier target |
| 15 | write_tests wrote no files | write_tests (retry) | Frontier · opus | escalation: no_output ×1 |
| 16 | "Add OAuth login with refresh tokens" | implement | Frontier · opus | rules: sensitive surface |
| 17 | Opus limit hit, weekly fine | review_code / implement | review → OpenRouter claude-opus; implement stays sonnet | model-specific window |
| 18 | The same task routed twice | plan | second classification from cache | cache |

A week-long headroom simulation (`test_model_router_headroom_week.py`) drives synthetic usage
from Monday to Sunday:

- Fast and Balanced leave Claude when the subscription crosses into *near limit* on Thursday.
- Frontier work stays on Claude until the window is *exhausted*.
- Everything returns at Monday's reset.

## Decisions

1. **Route at boundaries, never mid-session.** One decision per build stage, workflow step, panel
   phase and chat thread. A repair that resumes the implementer's session keeps its model.
   Escalation waits for the next boundary. This is LangChain's cache argument, extended to every
   boundary BOSS already has.
2. **Fixed is the default, and it is the legacy code path.** Fixed mode is not a re-creation of
   the old behaviour in new code. It *is* the old code (`route_for_step`, `materialize_route`,
   `resolve_job`), wrapped to record a route. So "nothing changes until you switch" holds by
   construction, and the old tests prove it.
3. **Tiers name a need, not a model.** Fast, Balanced and Frontier are stable while models churn.
   The tier → model list is data the operator edits, and new models arrive without code changes.
   LangChain's tiers made the same choice.
4. **Floors are the policy primitive, and profiles become floor presets.** "Adversarial review ≥
   Frontier" is how the operator thinks. A profile's `strong` steps are exactly its floors, so
   `lean`, `balanced`, `strict` and `vag` keep their meaning without a second vocabulary.
5. **Pins always win, and pin only what they name.** A job pinned to a source keeps the tier's
   choice of model on that source. A pinned model is fixed. An agent's `local` policy restricts
   sources rather than naming one.
6. **Frontier work is never moved by a headroom rule, only by exhaustion.** Moving Fast and
   Balanced work off the subscription early is what keeps headroom for the work that needs it.
   Moving Frontier work early would defeat the purpose.
7. **Headroom trusts the CLI's own numbers first.** Claude Code reports utilisation and reset
   times per window. The estimate from calls is a fallback, and its allowance is learned from the
   first limit hit rather than guessed from a plan name.
8. **The classifier is a cascade, and the deterministic answer is never skipped.**
   - With Jev: Jev, then the rules if it fails or times out.
   - Without Jev: the rules, then a cheap model only when the rules are unsure and the operator
     turned it on.
   Routing therefore works with Jev held (today), absent (most installs) or slow.
9. **Respect the Jev hold.** Live Jev calls are paused under BOSS-451. The router does not
   bypass that with a direct HTTP call. It shows *Jev · paused* and uses the rules. The cheap-model
   classifier goes through the governed adapter: reserved, receipted and settled.
10. **Escalation is generic and recorded.** The two hard-coded escalations become rules over named
    signals. New signals (review asks for changes twice, a gate fails) use the same mechanism,
    and every escalation is visible on the route.
11. **One route record per decision, referenced by every call.** `model_routes` holds the why.
    `llm_calls.route_id` links cost to it with no extra query, and scorecards summarise it.
12. **OpenRouter is a source like any other.** It uses the existing OpenAI-compatible lane, a key
    stored as a SecretRef, and the existing Setup connection flow. The only addition is a model
    catalogue with prices (`/api/v1/models`), cached for six hours in-process and in the database.
    Those prices feed `price_for_model`, so OpenRouter costs stop being estimates.
13. **"Smart vs Fixed" is an experiment knob.** The router's own value is measured on the same
    score as every other harness change, not asserted.

## UI

Setup › **Model routing** replaces *Who does what*. It is laid out as follows:

- **Smart | Fixed.**
- **Tiers:** Fast, Balanced and Frontier, each a source · model picker with its fallback.
- **Floors:** a preset (the profiles) plus chips such as *Adversarial review ≥ Frontier*, and
  *+ Floor*.
- **Headroom:** each subscription's meter, state and reset, and the rule *When near limit → move
  Fast, Balanced to OpenRouter*.
- **Classifier:** Auto, Jev, Cheap model or Rules only, with *Jev · paused* when held, and the
  three criteria texts under *Criteria*.
- **Pin a job ⌄:** the old rows, as overrides.
- **This week:** tier mix, routes, saved compared with all-Frontier, escalations, and work moved
  off the subscription.

In Fixed mode the tier, headroom and classifier sections fold away, and Pin a job is open: the old
screen, in place.

Each stage of a build, and each step of a workflow, shows its tier chip. **Why this model** lists
the route's reasons in order.

Agent Studio's *Routed / Pinned / Local* model policy now decides an agent's model at run time.

## Verified end to end

On one local stack (`scripts/e2e.sh up`, the scripted `claude` CLI, OpenRouter pointed at the
model stub, Smart on through `PUT /v1/setup/routing`), production console build:

- **Escalation on the real governed build path.** The stub answers *implement* with code that
  fails its tests on any model but Opus. The build routed implement Balanced · sonnet, failed
  twice, and the next boundary routed it Frontier · opus (`tests failed ×2 → Frontier`, well
  before the legacy final attempt); it completed. Adversarial review, security and consistency
  ran Frontier · opus by floor.
- **Headroom.** The stub CLI reported Claude at 92% of its week (`rate_limit_event`). From the
  next boundary every Fast and Balanced stage ran on OpenRouter (`moved`), while the three
  Frontier review seats stayed on Claude · opus; both builds completed.
- **Why this model** on both builds, and Setup › Model routing, at 1440 and 390 with no
  horizontal scroll; `?area=pipeline` lands on the routing row.

## Deferred

- **Pooling headroom across projects** that share one subscription. CLI-reported utilisation is
  already account-wide. The estimate is per project.
- **Typed-decision steps** keep their own backend (Jev or the Claude subscription, through
  `typed_decision_spend`); they are not yet tiered by the router.
- **Creating a custom pipeline profile** from the console: the old page's form is gone with the
  page; existing custom profiles are offered as presets and the API still creates them, and Smart
  mode's custom floors cover the same need.
- **Routing the remaining direct `make_code_agent` paths:** the ungoverned code task, the docs
  agent, and the evaluation tools (`verifier_debate`, `verifier_meta_eval`). They are listed in
  `test_every_model_call_goes_through_the_router`'s exemptions with that reason.
- **Online learning** of the tier per task kind from scorecards (a contextual bandit). The data
  is recorded now: routes, outcomes and arms.
- **Mid-session rerouting with cache-aware cost.**

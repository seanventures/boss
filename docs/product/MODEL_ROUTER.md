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
   - **The hold.** Live Jev calls are held by default (BOSS-451, `typed_decisions.GOVERNANCE_HOLD`),
     and the UI shows *Jev · paused*. `BOSS_ROUTER_JEV_LIVE=true` lifts the hold for the router
     (BOSSAPP-579). Lifted, every call goes through the governed typed-decision path
     (`store.GovernedJevDecider` on `TypedDecisionBatchSpend`): a committed budget reservation,
     a job and model lease on TypeSafe's own source, one receipt per attempt, settlement with
     the actual cost. A missing one refuses the call, records why, and the rules answer.
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

**Pins** come in two strengths (Decision 5). In order:

1. **A task pin (hard):** `model_config` on the task. Spend-cap failover and experiments write
   this, so both keep working unchanged.
2. **An agent pin (hard), read in Fixed mode too:** the Agent Studio model policy.
   - `pinned` → that engine and model;
   - `local` → only local sources, ever — and the operator's explicit choice, so a minimum (the
     sensitive-work Frontier minimum, a floor) applies **within** the local sources: a Local agent
     with a local model runs sensitive work locally and says so ("Sensitive work stays local by
     this agent's policy"). With none connected, the call is **refused in words** ("No local
     model connected — connect one or change this agent's policy"); with one connected that
     cannot run now, the refusal names it and why. In Talk, panel seats, agent steps and builds;
     the policy travels with the agent's role, so every path that names the role gets it;
   - `router` → routed like any step, with the role's floors.
3. **A job preference (soft):** today's *Who does what* rows, migrated. On Smart a job's choice
   picks the source and model *within* the tier — a floor raises it ("Floor raised your pin:
   haiku → opus"), a model above the step's tier is lowered to that source's model for the tier
   (a soft Opus preference never runs trivial work on Opus), and headroom rules and exhaustion
   can still move it.
4. **A job pin marked *Always* (hard):** the explicit "Always use, even when limited" choice, with
   a visible warning. Even then a floor raises the model on that source.
5. **A profile's step pin (soft):** a profile that names a model for a step (`implement:
   claude_code:sonnet`) is a preference within the tier; one that names a Frontier-class model
   (`review_code: claude_code:opus`) is a Frontier floor, classified by tier, not by string.

Every route is recorded at **the model's own** tier — the operator's tier list first, then the
model's class by name — whatever chose it (a pin, a session, a preference, a stand-in), so a record
never shows a tier that disagrees with the model that ran.

### (c) Source selection: which source takes the tier *now*

Selection walks the tier's list and takes the first target that is all of the following:

- connected;
- allowed;
- not failing (`working`, or tripped in the **circuit breaker** in the last minutes);
- not exhausted (its account's windows, or a provider-limit capacity);
- not metered while the **budget is spent** (red);
- not displaced by a headroom rule.

**Headroom.** Every subscription source has a headroom state:

| State | Meaning (defaults) |
|---|---|
| plenty | over 40% of the weekly window left |
| tight | 15–40% left, or the CLI reported `allowed_warning` |
| near limit | under 15% left (left again only at 20%: hysteresis, so 15% does not flap) |
| exhausted | a limit message or a `rejected` status, until its reset time |

Headroom is learned from three signals, best first:

1. **What the CLI reports.** Claude Code's `stream-json` emits a `rate_limit_event` on each call,
   with per-window utilisation and a reset time (five-hour, seven-day, seven-day Opus). Units are
   explicit: `utilization` is a fraction (0.86), `used_percentage` a percent (86). BOSS's stream
   parser keeps it, and telemetry folds it into the **account's** windows — written only when a
   window's state changes (or at most every five minutes).
2. **Limit messages and their reset times.** One parser, with a corpus test of real messages
   (`test_model_router_limits.py`):
   - Claude: *"You've hit your weekly limit · resets Mon 12:00am"*, *"Weekly limit reached ∙
     resets Oct 13, 10am"*, *"5-hour/Session limit reached…"*, *"Opus weekly limit reached…"*,
     *"You've hit your limit · resets 3pm (TZ)"*, *"…resets Oct 13 at 10am
     (America/Los_Angeles)"*, the legacy *"usage limit reached|<epoch>"*, the monthly spend limit;
   - a 429 with `Retry-After` (a one-minute window), OpenAI's quota and TPM messages, Codex's
     *"try again at Sep 22nd, 2026 10:06 PM"*.
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

**Limit state is live and account-wide.** Windows live per **subscription account**
(`subscription_accounts`: a stored key's secret ref, a hash prefix of the install token, or the
host's CLI login), so one project hitting the weekly limit is known to every project on that
login, and the learned allowance is the account's. A later **successful** call clears the
rejected windows it proves wrong (a Sonnet success clears the week, not Opus's window). Writes
merge **per window** into the stored row (JSONB `-`/`||`), so two workers never erase each other's
windows from a stale cache. The CLI's status wins over its number: `utilization: 1` with status
`allowed` is near the limit, not exhausted. A reset given as a time of day alone ("resets
12:30am (Australia/Sydney)") is never read as the weekly window, however far away it falls.

**Nothing is stranded.**

1. **At call time:** a 5xx, a timeout, a refused connection or a limit trips the source's
   **circuit breaker** for a short time, and the *same* step retries at once on the next source in
   its tier (a failed call has no session to preserve). A weekly limit reached mid-build reroutes
   the stage automatically — no manual retry.
2. **A breaker is half-open, and a stage waits rather than fails** (Decision 21). When nothing else
   can take the work, a source out only because its breaker is open gets **one trial call** (at
   most one per source every 20 s); a success closes the breaker. When even that is not yet due,
   the refusal is marked temporary, and a build stage waits and asks again with a bounded backoff
   (5, 10, 20, 30, 60 s…, up to 10 minutes), recording `code_task.stage_waiting` with the reason
   in words. Only after the bound does the stage fail, and its message says how long it waited.
3. **At a boundary:** the same tier on any other connected source with a default model for it.
4. **Sticky sessions** (a chat thread, an agent run) keep their model only while its source can
   run; an exhausted or failing source — or a metered one while the budget is red — starts a new
   session elsewhere.
5. **Where nothing may run, a refusal in words:** Frontier goes subscription Frontier → metered
   Frontier (OpenRouter Opus) → *"No Frontier model can take this now…"* — never silently a local
   model, unless the operator allowed local for Frontier. A Local-only agent never leaves its
   allow-list. With the **budget spent**, nothing new is metered: the subscription's lower tier
   within the floors, else *"Waiting for budget or limit reset"*; an agent pin or an *Always* job pin
   on a metered source is refused while red (*"Budget spent: this work is pinned to OpenRouter…"*).

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
- **Fixed mode loads a lite snapshot** (the connected sources only): no policy seeding, no
  headroom, no budget, no pins.
- **Network on the hot path** is only the optional classifier (Jev 800 ms, cheap model 6 s), each
  with its own timeout, its own thread (a hung call never queues the next route), and the rules'
  answer as fallback. A timeout is cached for two minutes, and a timed-out Jev is skipped for one,
  so the stages of a build do not each wait again.
- **Headroom writes only on change:** a `rate_limit_event` per call is written when a window's
  state moves (status, a 5-point move, a new reset) or at most every five minutes, and snapshots
  are dropped only when a state actually changed.
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
| *Who does what* job choices (`code_agent_engine`, `code_review_engine`, `chat_llm_engine`, their models) | The same rows, shown under **Pin a job** (Fixed) / **Prefer for a job** (Smart). On Smart they are preferences: floors raise them, headroom and exhaustion can move them; tick *Always* to make one hard | No |
| Classifier or limit messages | `provider_limits` reads the one limit parser (Decision 18) | Yes, on purpose: more limit phrasings mark a source exhausted, with its reset |
| Hard problems / Routine work lanes (`code_agent_strong_*`, `code_agent_low_cost_*`) | Seed the Frontier and Balanced tiers when Smart is first used | No |
| Default pipeline profile (`pipeline_profiles.is_default`) | The **Floors** preset; choosing a preset also sets the default profile | No (it *is* the profile) |
| A task's `profile` override | Fixed: as before. Smart: the profile's floors are added to the project's for that build | No |
| Spend-cap failover (`model_config` pin + requeue) | Unchanged; on Smart the monthly-limit message also marks Claude exhausted | No |
| Experiments (harness, review rounds) | Plus **Model routing** (fixed vs smart); assignment writes `spec.routing_mode` | No |
| `?area=pipeline` bookmarks | Land on Setup › Model routing; the page's Factory scale card moved to Operations | — |

Schema (all additive, applied by `ensure_schema`): `model_routing_policies`, `model_routes`,
`provider_model_catalog`, `subscription_accounts`, `llm_calls.route_id`, `run_scorecards.route_mode / route_tier /
routing`. Settings: `BOSS_MODEL_ROUTING_MODE` (default `fixed`), `BOSS_ROUTER_JEV_LIVE`
(default off; on, the router's Jev calls run on the governed typed-decision path, BOSSAPP-579).

### Where headroom is read from

Windows live per subscription **account** in `subscription_accounts`, keyed by the credential
that spends: `<engine>:ref:<secret_ref_id>` for a key stored in Setup, `<engine>:tok:<hash
prefix>` for the install's `CLAUDE_CODE_OAUTH_TOKEN` (the token itself is never stored), or
`<engine>:install` for a CLI logged in on the host. Every project spending on the same account
reads the same windows. The estimate from calls is still per project (see Deferred).

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
5. **Migrated job pins are preferences; a hard pin is an explicit choice; floors beat both.**
   *(Revised after the independent test.)* A "Who does what" row picks the source and model
   within the tier, and headroom and exhaustion can still move it — otherwise a pin silently
   strands work on an exhausted subscription. "Always use, even when limited" is the only hard
   job pin, and the console warns beside it. A floor raises any job pin ("Floor raised your pin").
6. **Frontier work is never moved by a headroom rule, only by exhaustion — and never silently to a
   local model.** *(Revised.)* Frontier goes subscription → metered Frontier → a refusal in words,
   unless the operator allowed local for Frontier.
7. **Headroom trusts the CLI's own numbers first.** Claude Code reports utilisation and reset
   times per window. The estimate from calls is a fallback, and its allowance is learned from the
   first limit hit rather than guessed from a plan name.
8. **The classifier is a cascade, and the deterministic answer is never skipped.**
   - With Jev: Jev, then the rules if it fails or times out.
   - Without Jev: the rules, then a cheap model only when the rules are unsure and the operator
     turned it on.
   Routing therefore works with Jev held (today), absent (most installs) or slow.
9. **Respect the Jev hold.** Live Jev calls are paused under BOSS-451 until the operator sets
   `BOSS_ROUTER_JEV_LIVE`. Held, the router shows *Jev · paused* and uses the rules. Lifted, it
   never makes a direct HTTP call: each classification is one governed typed-decision attempt,
   reserved, leased, receipted and settled, and the same task replays from its receipt. The
   cheap-model classifier goes through the governed chat adapter the same way. Both have a
   one-minute cooldown after a timeout or failure, so a down source never costs every route its
   timeout.
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
    score as every other harness change, not asserted. The Smart arm on a Fixed project gets the
    full Smart snapshot, headroom included.
14. **Limit state is live and account-wide; nothing is stranded.** *(New; revised after the
    re-check.)* Windows are pooled per subscription account and merged per window, so concurrent
    workers never lose one another's; a success clears what it disproves; a call-time failure
    trips a circuit breaker and the same step retries on the next source; sticky sessions break
    when their source cannot run.
15. **Local means local — and Local is the operator's explicit choice.** *(New; revised after the
    re-check.)* A Local-only agent, or an allow-list with nothing connected in it, refuses in words
    rather than falling back to the cloud — in Talk, panel seats, agent steps and builds, in Fixed
    mode too. The policy is found by the agent's role at the router, not by each caller. A
    minimum (sensitive work, a floor) applies within the allowed sources, so a Local agent with a
    local model runs sensitive work locally and says so.
16. **A spent budget means no new metered spend.** *(New; revised after the re-check.)* With the
    budget red, the router keeps work on included sources (the subscription's lower tier within
    the floors) or waits, in words; it never moves work to OpenRouter. A sticky session on a
    metered source breaks onto an included one, and an agent pin or an *Always* job pin on a
    metered source is refused in words. A task pin (spend-cap failover, an experiment) is exempt.
17. **Sensitive work has a minimum no classifier can lower.** *(New.)* Auth/OAuth, secrets,
    passwords, production databases, migrations, payments and security set Frontier for work (and
    Balanced for a chat answer). Jev or a model may raise it, never lower it, and a stated
    `risk_tier: 0` does not override the words.
18. **One limit parser, with a corpus.** *(New.)* Every phrasing seen from Claude, Codex and
    OpenAI-compatible APIs goes through `headroom.windows_from_limit_message`. **This changes Fixed
    mode on purpose:** `provider_limits.classify_limit` now uses the same parser, so a Fixed
    install also marks a source exhausted (with its reset) for *"Weekly limit reached ∙ …"*, a 429
    with Retry-After and the other corpus messages — but still not for a one-family limit (Opus).
19. **Headroom has hysteresis, and validation is explicit.** *(New.)* Near limit is entered under
    15% and left at 20%; a policy value out of range is refused with its field, never clamped.
20. **OpenRouter defaults have a quality floor.** *(New.)* Tier suggestions come from a curated
    list per tier with a minimum context (Fast is a good small coder, not a 3B model), and a
    non-Frontier model is refused as the Frontier tier, with the reason. The catalogue is loaded
    from the database when a process starts (API and worker, Fixed mode too: it prices calls)
    and before the first suggestion. Model names are tiered by whole size tokens ("27b" is not
    "7b", "8x7b" is a mixture of experts) and markers (`-fast` is Fast).
21. **A temporary outage never fails a stage.** *(New, from the re-check.)* A source whose
    breaker is open gets one half-open trial call when nothing else can take the work; a
    refusal that is only temporary carries when to ask again, and a build stage waits with a
    bounded backoff (up to 10 minutes), saying why, before it fails. A soft preference stays
    within the step's tier, and every route is recorded at its model's own tier.

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

On one local stack (`scripts/e2e.sh up`, the scripted `claude` CLI, a stub OpenRouter in front
of the model stub, Smart on through `PUT /v1/setup/routing`), production console build, after the
independent test's fixes:

- **Escalation on the real governed build path:** implement failed its tests twice on Sonnet and
  the next boundary routed it Frontier · Opus (`tests failed ×2`); the build completed.
- **Headroom:** with Claude at 92% of its week, Fast and Balanced stages moved to OpenRouter while
  the three Frontier review seats stayed on Claude · Opus.
- **OpenRouter 503:** the first moved stage failed with a 503; the same stage retried at once on
  Claude (`code_task.stage_rerouted`, transient) and later stages stayed off OpenRouter; completed.
- **A weekly limit mid-build:** implement hit *"You've hit your weekly limit · resets Oct 13 at
  10am (America/Los_Angeles)"*; the stage rerouted to OpenRouter by itself, the account went
  *exhausted*, the Frontier seats went to OpenRouter Opus; completed — no manual retry.
- **A monthly limit, then success:** the limit moved the stage away and closed the account's
  monthly window; once half-open, the next build ran on Claude and that success cleared it.
- **A Local-only agent with no local model:** refused with *"No local model connected — connect
  one or change this agent's policy."* in Smart and in Fixed mode.
- **The cheap-model classifier, live:** a brief the rules could not judge ("fix") was classified
  by a Fast model through the governed adapter.
- **A workflow agent step** recorded its route under the run and step; the step panel shows
  *Why this model*. A meeting routed its facilitator Frontier and its panel Balanced.
- **Cost of the router per call:** `record_llm_call` issues the same 3 statements as main
  (a `rate_limit_event` adds a write only when a window's state changes); a Fixed route costs one
  insert after its first (4 statements cold), a Smart route one insert after a 23-statement
  snapshot load per 20 s.
- Setup › Model routing, the builds' *Why this model*, the workflow step, and Agent Studio's model
  policy at 1440 and 390 with no horizontal scroll; `?area=pipeline` lands on the routing row.

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

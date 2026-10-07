# Run scorecards: recording every run and its outcome

**Status:** approved 2026-10-05; built on `tm/improve-boss` (see "Implementation" for what was
built and how). **Owner:** operator. **Scope:** Community and Enterprise.

## Why

How a model does depends as much on the **harness** around it as on the model itself. Independent
work on agentic RL found the same model weights scoring 33% in one harness and 62% in another.
BOSS *is* a harness:

- its outer workflow (research → panel → spec → build → deploy → watch);
- its agents' prompts, skills and context;
- its review rounds, retry and stop rules;
- the choice of inner coding harness (Claude Code or Codex).

BOSS cannot train Claude or GPT, but it can **tune its own harness**. That needs one thing it does
not keep today in usable form: **for every run, what was tried and how it turned out.**

A run scorecard is that record. It has two jobs:

1. **Inside one install:** compare harness variants on real work and keep the winners.
2. **Across installs, with consent:** learn which defaults work best across many users' work and
   ship better defaults to everyone.

## What a scorecard is

A **unit** is one thing BOSS was asked to do and finished, for good or bad:

| Unit | Example |
|---|---|
| `workflow_run` | "Standard Product Delivery: Lead-Triage Inbox" |
| `build` | one Factory build attempt (code task run) |
| `step` | one workflow step (research, panel, build, review, deploy) |
| `panel` | one meeting / PRD panel |
| `research` | one discovery dossier |
| `quick_change` | one feedback-driven change |

Each unit gets one scorecard, written **when the unit reaches a terminal state**: completed,
failed, cancelled, rejected or rolled back. It is updated later when a late outcome arrives (PR
merged, deploy healthy after 24h, operator reverted it). It has three parts.

### 1. What was tried: the variant

What BOSS chose, so two runs can be compared like for like.

| Field | Example | Notes |
|---|---|---|
| `workflow_key`, `workflow_version` | `standard_product_delivery`, 5 | |
| `step_key` | `build` | for step units |
| `role_key` | `software_engineer` | the agent that did the work |
| `prompt_version` | hash of the role's resolved instructions | changes when instructions change |
| `skills` | sorted list of skill keys + versions | |
| `mcp_tools` | sorted list of tool keys | |
| `harness` | `boss_native` / `claude_code` / `codex` | the inner harness for builds |
| `model`, `provider` | `claude-sonnet-5`, `anthropic` | |
| `review_lanes`, `review_rounds` | 6, 2 | effective values, after memory limits |
| `turn_budget`, `token_guide` | per stage | |
| `retry_policy` | max attempts, backoff | |
| `context_policy` | what was carried in (research, PRD, prior attempts) | names, not content |
| `experiment` | `{id, arm}` | set when the run is part of an A/B test (see below) |
| `task_kind` | `new_app` / `add_feature` / `fix` / `research` / `panel` | |
| `task_size` | small / medium / large | from the plan, or a heuristic |

### 2. How it went: the outcome

Signals BOSS already produces, gathered in one place. Higher is better unless marked ↓.

| Signal | Source today | Meaning |
|---|---|---|
| `terminal_state` | run / step status | completed, failed, cancelled, rejected, rolled_back |
| `tests_passed` / `tests_total` | build stations (tests, check) | |
| `existing_tests_intact` | quality gate | for changes to an existing app |
| `gates_passed` / `gates_failed` | quality gates | names of the failed ones |
| `review_verdicts` | review seats | approved / changes_asked per seat, and **rounds used** ↓ |
| `operator_decisions` | decisions | plan / spec / merge / deploy: approved or rejected, and **time to decide** |
| `operator_steers` | steer_code_task / feedback | count ↓: each one means the agent went the wrong way |
| `pr_opened`, `pr_merged` | GitHub | merged is the strongest "this was good" signal |
| `pr_reverted` | GitHub (late) | ↓ strong negative |
| `deploy_healthy` | deployment runs + health checks | first check, and after 24h (late) |
| `rolled_back` | deployment runs | ↓ |
| `research_verdict`, `confidence` | dossier gate | |
| `panel_agreed`, `prd_approved` | meeting | |
| `cost_usd` | model calls (money, excluding subscription) | ↓ |
| `tokens_in`, `tokens_out`, `model_calls` | model calls | ↓ |
| `tool_calls`, `turns` | agent acts | ↓ efficiency, the "fewer steps" signal |
| `wall_time_s`, `wait_on_person_s` | timestamps | separate BOSS's time from time spent waiting on you |
| `retries`, `shepherd_interventions` | jobs, Shepherd | ↓ |
| `error_class` | failures | words, e.g. `no_repo_access`, `build_failed`, `health_failed` |

### 3. One number: the score

For quick comparison, every scorecard carries a **score from 0 to 1** with a fixed, documented
formula per unit kind, alongside the raw signals (never instead of them). For a build:

```
quality  = 0 if failed/cancelled/rejected
           else 0.5·tests_pass_rate + 0.2·(gates all passed) + 0.2·(review approved)
                + 0.1·(existing tests intact)
bonus    = +0.15 if PR merged, −0.4 if PR reverted, −0.3 if rolled back      (late outcomes)
cost     = small penalty for cost, turns and steers above the role's median
score    = clamp(quality + bonus − cost, 0, 1)
```

The formula is versioned (`score_version`), so comparisons only mix scores of the same version.
The operator can see and adjust the weights in Govern.

## Where it lives

- **One table:** `run_scorecards`, one row per unit, keyed by `(unit_kind, unit_id)`. Columns hold
  the variant and outcome fields above. `score`, `score_version`, `computed_at` and
  `late_outcomes_at` sit alongside.
- **Written by one function** (`record_scorecard(unit)`), called from each unit's terminal
  transition and from the late-outcome jobs (PR merged or reverted, 24h deploy health). Writing is
  idempotent: running it twice gives the same row.
- **Backfill:** a one-off job builds scorecards for existing history from the tables BOSS already
  has. Past runs then count from day one.
- **No content:** the scorecard stores **names, numbers and verdicts**: no prompts, code, briefs,
  file paths or secrets. Content stays in the existing tables (and their redaction rules). This
  matters for sharing (below) and keeps rows small (target ≤ 2 KB).

## Using it inside one install

### See it

**Observability › Scorecards**, a new view:

- score and key signals by role, workflow, harness and model, over a time window;
- a "what's costing you" list: lowest scores, most steers, most review rounds;
- each row opens the run.

### Compare variants (A/B)

An **experiment** says: for this role or workflow, run arm A or arm B, split by a fraction, until N
units finish. For example:

- "software_engineer: Claude Code vs Codex, 50/50, 20 builds";
- "review_rounds 2 vs 3".

BOSS picks the arm when the unit starts and stamps `experiment` on its scorecard. The experiment
page then shows both arms' scores with the spread, and says plainly whether there is a winner yet
or it is too close to call.

**Guardrails:**

- Experiments are off by default and started by the operator.
- They never change what needs a person's approval.
- Each arm has a cost cap.
- Any arm that fails gates noticeably more often stops early.

### Act on it

- The operator promotes the winning arm to the default with one press. The change is recorded in
  Govern's change log like any other setting.
- Later, BOSS can suggest experiments ("Codex scores 0.12 higher on small fixes; try it?"), but it
  never switches defaults by itself.

## Feedback the person gives

Scores say whether work *worked*; these say whether it was what the person *wanted*. All of them
are optional, one tap, and stored locally whether or not "Improve BOSS" is on, because they feed
the install's own scorecards and experiments.

| Where | Question | Reason chips |
|---|---|---|
| Finished build / PR | "Would you ship this?" Yes / With changes / No | wrong scope · poor quality · broke something · too slow · too expensive · misunderstood the brief |
| Research dossier | "Was this useful?" | shallow · wrong market · great sources · changed my mind |
| Panel PRD | "Did the panel get it right?" | missed requirements · over-scoped · good debate |
| Talk to BOSS answer | thumbs up / down | none |
| Deploy | "Working as expected?" | none |
| Rejecting a plan, spec or merge; steering a build | (the existing action) | the build-row chips |

The same chips on every rejection mean BOSS learns *why* it got a no, not only that it got one.

**Monthly check-in:** at most once a month, dismissible. "How disappointed would you be if you
could no longer use BOSS?" plus an optional one-line "What's the one thing to fix?"

**Share this run** (a button, not a setting): when something goes wrong, the person can send one
run's redacted bundle (steps, agent turns, errors and logs) with a note. They see and can edit the
exact bundle first. It is sent once, like a bug report, separate from the "Improve BOSS" switch.
It's the only way content ever reaches us.

## Sharing with the BOSS project: opt-in feedback from Community users

**Yes, this is possible**, and it is the most valuable dataset we could have: real work, across
many installs, scored by real outcomes. It must be **opt-in, transparent and content-free**.

### Consent

- **Off by default.** Nothing leaves an install unless its operator turns it on.
- Asked once, plainly, at the end of setup (and available any time in **Govern › Sharing**):

  > **Improve BOSS.** Share how BOSS is working for you: how runs turned out, your ratings, which
  > features you use, and errors. Never your code, prompts, briefs, file names, repo names or
  > secrets. You can see exactly what is sent, and turn it off any time.

- **One switch: "Improve BOSS", on or off.** No tiers and no sub-options. More choices would make
  it look as if there were more to collect. Everything listed under "What is sent" goes together.
- The choice is recorded with who made it and when (Govern change log).
- It can be changed or revoked any time. Revoking stops sending immediately and offers "delete
  what was sent" (see below).

### What is sent

Only allow-listed fields (never a deny-list), as names, labels, counts and ranges:

**Feedback from the person:** the one-tap ratings and their reason chips (see "Feedback the person
gives" below), rejection and steer reasons (chips), and how much a brief, PRD or plan was edited
before approval (a number, never the text). The monthly one-liner is sent only if the person types
it and presses send.

**Usage:** the first-week funnel (install → setup → first build → first PR → first merge → first
deploy, with times), which doors were used (new app vs add to an existing app, built-in
templates, build box, Talk to BOSS, research, panels), overrides used (Start anyway, Build anyway,
waived gates), abandoned work, and counts of custom roles, skills and MCP servers. Only built-in
catalogue keys are named.

**Setup context, as ranges and labels:** BOSS version and edition, install method, OS and chip,
memory and CPU range, model provider types (subscription vs API key, self-hosted or not), detected
repo language and framework, repo size range, team size range, deploy target types.

**Health:** error and crash categories, system doctor findings by kind, stuck builds and retries,
installer step failures by category, speed ranges, cost per outcome, budget-cap hits.

**Scorecards:**

- **Variant:** workflow key and version (built-in workflows only; custom ones are sent as
  `custom`), role key (built-in roles only, else `custom`), harness, model, provider, review
  lanes and rounds, retry policy, skill and MCP keys from the public catalogue (custom ones
  counted, not named), task kind and size.
- **Outcome:** terminal state, counts, pass rates, verdict labels, booleans, cost, token, turn and
  time numbers (rounded), error class, score and score version.
- **Context:** BOSS version and edition, plus a **random install id** (not derived from anything
  identifying, rotatable by the operator).

**Never sent:** prompts, instructions, code, diffs, briefs, PRDs, research text, file paths, repo
or org or project names, URLs, user names or emails, IP-derived data, hostnames, secrets.

### How it is sent

- **Batched daily** (not live), over HTTPS to one documented endpoint run by the BOSS project.
- Each batch passes the existing secret redaction gate **and** the allow-list. Anything outside
  the allow-list is dropped, not masked.
- **Preview:** Govern › Sharing shows the exact next batch as JSON before it is sent, and keeps a
  local log of everything sent.
- **Fails closed:** if the endpoint is unreachable, nothing is retried indefinitely and nothing
  builds up beyond 7 days. Sharing never slows or blocks work.
- **Air-gapped installs:** an "Export batch" button writes the same JSON to a file the operator can
  send manually, or not.

### On our side

- Stored keyed by the random install id only, kept 18 months, then aggregated and deleted.
- **Deletion on request:** the install sends a signed "delete my install id" request (one button),
  or the operator emails us the id.
- Used only to improve BOSS defaults and publish **aggregate** findings, e.g. "across 1,200
  builds, 3 review rounds beat 2 by 0.06 on large features". Never sold, never used to train
  third-party models. Any change to that needs new consent.
- The privacy policy and this document say the same thing. The endpoint and the allow-list are in
  the open-source repo, so anyone can verify them.

### Legal notes (to confirm with counsel before shipping)

- Opt-in consent plus content-free, pseudonymous data keeps this light under GDPR and CCPA. Still:
  publish a privacy notice, honour deletion, and document retention.
- The random install id is still pseudonymous personal data under GDPR, so it is treated as such
  (deletion, access on request).
- Enterprise contracts may forbid sharing entirely. Enterprise builds ship with sharing disabled
  and locked unless the contract allows it.

## Build plan

| Phase | What | Size |
|---|---|---|
| 1 | `run_scorecards` table, `record_scorecard()`, terminal + late-outcome hooks, backfill, score v1 | M |
| 2 | Observability › Scorecards view (by role / workflow / harness / model) | M |
| 3 | Experiments: define, assign arms, compare, promote; guardrails | M |
| 4 | Govern › Sharing: consent, allow-list, preview, daily batch, export, revoke and delete | M |
| 5 | Project-side endpoint + aggregate dashboard (separate service) | M |

Phases 1 and 2 are useful on their own and carry no privacy risk, because everything stays local.
Phase 4 should not ship until phase 1's allow-list has been reviewed.

## Tests that must exist

- Every terminal transition of every unit kind writes exactly one scorecard, and a rerun is
  idempotent.
- Late outcomes update the same row, and the score changes accordingly.
- **No content leaves:**
  - seed prompts, code, briefs, repo names and secrets into runs; build a sharing batch; assert
    none appears, not even partially;
  - fields outside the allow-list are dropped.
- Sharing **off** sends nothing (network test). Revoke stops the next batch.
- Preview equals what is sent, byte for byte.
- Experiments never bypass an approval gate, and an arm over its cost cap stops.

## Implementation

**Status:** built on branch `tm/improve-boss` (2026-10-05). This section is the contract: the
tables, the score, the payload and the routes as they are in the code. If the code and this
section disagree, the code is wrong.

### Operator decisions this follows

1. **One switch, "Improve BOSS", off by default.** No tiers. Asked once at the end of setup
   (the last block of Setup, under Try it), changeable in **Govern › Sharing**. Each change is a `settings.changed` event
   (key `improve_boss`) with actor and time, so it shows in the Log's "Govern changes".
2. **A daily heartbeat, on by default in principle, with one opt-out.** Separate from the switch.
   It says only "this install is alive" (fields below). Shown in the README, in Setup and in
   Govern › Sharing. `scripts/install/install.sh` is not changed; `boss doctor` prints one line.
   **It sends nothing until a collector is set** (decision 5).
3. **`BOSS_TELEMETRY=off` or `DO_NOT_TRACK=1` turns everything off,** the heartbeat included: no
   payload is sent and no connection is opened by ANY path — the daily send, Share this run,
   Delete, a rotation's delete. One list of words, `improve_boss.TELEMETRY_OFF_WORDS`
   (`off 0 false no disabled disable none`) and `DNT_ON_WORDS` (`1 true yes on`), used by the
   engine and by `checkup.sh` alike, with surrounding quotes ignored. Govern › Sharing shows the switches locked and why.
4. **The collector is part of BOSS,** active only when **Collector mode** is on (a console toggle in
   Govern › Sharing; a deploy-API-only host cannot set env).
5. **No default collector** (2026-10-05, after the independent test). With no collector URL
   configured nothing is sent, the heartbeat included, and Govern says "No collector set". The
   URL is set by `BOSS_TELEMETRY_URL` (which then locks the field) or in Govern › Sharing (by a
   member who can manage providers; https only, no credentials, ending `/v1/telemetry/ingest`).
   The heartbeat starts working for everyone once a release names a public collector. Preview
   and Export work without one (an air-gapped install exports by hand).

**One door.** Every outbound byte goes through `improve_boss.transmit()`, which refuses before
any sender is called when the environment turns sharing off or no collector is set; the routes
that send (`share-run`, `forget`) answer 409 with the same reason, and the console disables
those buttons and says why. A socket-level test covers each path.

### Units and where the scorecard is written

| Unit | Source row | Terminal states | Hook |
|---|---|---|---|
| `workflow_run` | `workflow_runs` | completed, failed, deadlettered→failed, cancelled, compensated→rolled_back | `workflow.run_completed / run_stopped / run_cancelled / compensation_completed` events |
| `step` | `workflow_step_runs` | succeeded→completed, failed, deadlettered→failed, cancelled (skipped steps get none) | `workflow.step_succeeded / step_failed / step_deadlettered` events |
| `build` | `code_task_runs` | completed, failed, cancelled (a `blocked` build is waiting, and is scored when it moves) | `agent_acts.settle_code_task` (every build runner's `finally`) and `code_task.*` events |
| `quick_change` | `code_task_runs` matching `workflow_costs._changes` | as build | as build |
| `panel` | `meeting_runs` | completed, failed, cancelled; completed with its PRD rejected → rejected | `meeting.completed` event; PRD decision events re-record it |
| `research` | `discovery_runs` | completed, failed | `discovery.completed` / `discovery.failed` events |

Only `product` workflows are scored (`workflow_definitions.namespace`); BOSS's own `system`
workflows are machinery, not work a person asked for.

`scorecards.record_scorecard(connection, unit_kind, unit_id)` reads the unit's own rows and writes
one `run_scorecards` row with `INSERT … ON CONFLICT (unit_kind, unit_id) DO UPDATE`, so it is
idempotent. It is called from `core.emit_event` (a savepoint after a terminal event; a failure is
logged and never undoes the event) and from `settle_code_task`. A **sweep** in the daily-tick
maintenance (below) records any terminal unit that still has no row — the safety net for a writer
that emits no event — and the same function is the **backfill** (`POST /v1/scorecards/backfill`,
and automatically, 2,000 units per tick, until history is covered; rows are marked
`source = 'backfill'`).

**Late outcomes** (same row, `late_outcomes_at` set, score recomputed):

- `pr_merged`: the run's pull-request decision executed/merged, or `delivery_pipeline.on_pull_request_merged`.
- `pr_reverted`: a later run in the project whose title starts `Revert` and names this PR's
  number. BOSS does not poll GitHub, so a revert made outside BOSS is not seen (stated in the view).
- `deploy_healthy_24h`: for a build or run with a `stack_deployments` row that went `healthy`, the
  health samples of that target between 23h and 25h later (all ok → true; any failed → false;
  none → stays unknown).
- `rolled_back`: a later `stack_deployments` row with `rollback_of` pointing at this one.
- PRD approved / rejected: the panel's decision.

### `run_scorecards`

One row per unit, keyed by `(unit_kind, unit_id)`. Variant columns: `workflow_key`,
`workflow_version`, `workflow_builtin`, `step_key`, `role_key`, `role_builtin`, `prompt_version`
(hash), `skills` and `mcp_tools` (sorted key lists), `harness`, `model`, `provider`,
`review_lanes`, `review_rounds`, `turn_budget`, `retry_max`, `context_policy` (names),
`experiment_id`, `experiment_arm`, `task_kind`, `task_size`. Outcome columns: `terminal_state`,
`tests_passed`, `tests_total` (test commands run, not test cases: that is what BOSS records),
`existing_tests_intact`, `gates_passed`, `gates_failed`, `failed_gates` (names),
`review_approved`, `review_rounds_used`, `approvals`, `rejections`, `decide_wait_s`, `steers`,
`pr_opened`, `pr_merged`, `pr_reverted`, `deploy_healthy`, `deploy_healthy_24h`, `rolled_back`,
`research_verdict`, `confidence`, `panel_agreed`, `prd_approved`, `cost_usd`, `tokens_in`,
`tokens_out`, `model_calls`, `tool_calls`, `turns`, `wall_time_s`, `wait_on_person_s`, `retries`,
`shepherd_interventions`, `error_class`, `rating`. Score columns: `over_median`, `score`,
`score_version`, `computed_at`, `late_outcomes_at`, `source`. Full DDL: `db/schema.sql`. No column
holds text a person or model wrote.

### Score v1

```
if terminal_state in (failed, cancelled, rejected):  quality = 0
else quality = weighted mean of the signals this unit HAS (unknown ones are left out, the rest
               re-normalised); a completed unit with no signal at all = completed_unverified (0.6)
  build / quick_change / workflow_run / step:
       tests 0.5 · tests_passed/tests_total     gates 0.2 · (gates_failed = 0)
       review 0.2 · review_approved              intact 0.1 · existing_tests_intact
       (workflow_run adds approvals 0.2 · approvals/(approvals+rejections))
  panel:     prd_approved 0.6 · panel_agreed 0.4
  research:  confidence (0..1)
bonus   = +0.15 merged  −0.4 reverted  −0.3 rolled back  −0.2 unhealthy after 24h
penalty = 0.04 · (cost > role median) + 0.03 · (turns > median) + 0.03 · (steers > median)
          (median over the same unit kind and role, last 90 days, needs ≥ 5 rows)
score   = clamp(quality + bonus − penalty, 0, 1), 3 decimals
```

The weights live in `system_state['score_weights']` and are shown and edited in **Govern ›
Sharing › Score**. Defaults are `score_version = "1"`; each edit makes `"1.<n>"`, and the tick
rescores every row whose version differs (2,000 per tick), so a view never mixes versions.

### Experiments

`experiments` (one A/B) and `experiment_assignments` (the arm a unit got). An experiment targets a
**role** (builds run as that agent) or a **workflow** (builds inside runs of it) and varies ONE
knob from an allow-list that never decides what needs approval:

| Knob | What changes on the build's spec |
|---|---|
| `harness` | `model_config.default.engine` (e.g. `claude_code` vs `codex`); Promote sets `code_agent_engine` |
| `review_rounds` | `review_rounds` (read before `code_review_inrun_max_rounds`); Promote sets that knob |

(Review lanes and repair retries are not offered: they have no project setting to promote into.)
The target is picked from this project's agents or workflows, never typed.

The arm is picked in `code_task_service.start_code_task` (the one door every build goes
through), deterministically from the run id and the split, and stamped on the scorecard. An
explicit choice on the build (a pinned engine) wins and the build is not enrolled.
Guardrails: off until the operator starts one; risk tier, profile, approvals and gates are never
touched (test-enforced); each arm has a **cost cap** — a capped arm takes no new units, both capped
stops it; an arm whose gate-failure rate is ≥ 0.3 above the other's after ≥ 4 units each **stops
the experiment**. The compare view shows each arm's n, mean, spread (sd) and the 95% interval of
the difference; it says **"Too close to call"** while the interval crosses 0 or either arm has
fewer than 5 units. **Promote** opens a confirm sheet that shows what changes ("Harness:
claude_code → codex"); it needs at least 5 finished units in each arm and a verdict that is not
"too close to call" — otherwise the server refuses (409) unless the operator ticks **Promote
anyway**. It writes the value as the project setting (`project_settings.set_one`, so it is in the
change log) and marks the experiment `promoted`.

### Feedback from the person (always local)

`person_feedback`: `subject_kind` ∈ build, research, panel, answer, deploy (one-tap ratings, one
row per person and subject, a second tap changes it), reject, steer (chips on the existing
action), edit (a number 0..1: how much a brief, PRD or plan changed before approval,
`1 − difflib ratio`, never the text), checkin. Where the console has an edit-before-start step
today — a Talk to BOSS proposal's brief, edited before Start — the server measures it
(`agent_converse.start_from_proposal`). Any other surface sends the number alone
(`POST /v1/feedback {subject_kind: "edit", decision_kind: brief|prd|plan, edit_ratio}`); the
console has no edit-before-approval step for a PRD or a plan yet, so none is reported for them.

Where the controls are: Factory build page (rating once the build is finished; chips on Steer;
Share this run in ⋯), research dossier, PRD panel, every Talk to BOSS answer (thumbs), a finished
deploy (Infra › deploy drawer), the reject press in the Inbox ("Why no?") and on attention cards,
the monthly check-in on the Command Center, and Share this run on a workflow run.

| Subject | Ratings | Chips |
|---|---|---|
| build | yes · with_changes · no | wrong_scope · poor_quality · broke_something · too_slow · too_expensive · misread_brief |
| research | useful · not_useful | shallow · wrong_market · great_sources · changed_my_mind |
| panel | right · wrong | missed_requirements · over_scoped · good_debate |
| answer | up · down | — |
| deploy | working · not_working | — |
| reject, steer | — | the build chips |
| checkin | very · somewhat · not (the PMF question) | — plus an optional one-liner |

`person_prompt_state` keeps the asks quiet: an open rating prompt is shown at most 3 times a day
per person (after that the control is a small "Rate" link); the check-in at most once in 30 days,
and a dismissal counts as the month's ask.

### Client: one POST a day

The worker's existing loop calls `POST /v1/telemetry/tick` every 10 minutes (the cheap guard is
in the worker; the work is in the API under `pg_try_advisory_xact_lock`). The tick does the
scorecard maintenance (sweep, backfill, late outcomes, rescore), and when due, the daily send. No
new process or thread.

- **Due:** once per UTC day, at a random time that day (`next_due_at` in
  `system_state['improve_schedule']`). After ANY attempt, success or failure, the next one is
  tomorrow. No retry loop, no backoff.
- **Nothing is cut and nothing is sent** when the env disables it, or when the heartbeat is opted
  out and the switch is off.
- **Cut once, frozen:** the next payload is built once (by the first preview or by the send) and
  stored as exact gzip bytes in `telemetry_sends` (`pending`). The send posts those bytes. Govern
  shows them. So the preview IS what is sent. Data after the cut goes to the next day's batch.
- **Nothing from before consent:** a batch's `since` is the later of the last batch's end and the
  moment the switch was last turned **on** — never `MAX(until_at)` alone — so the first batch
  starts at consent and an off period is never covered after turning it on again. Scorecards
  travel only for units that **ended** after consent (a backfill or late outcome after consent
  does not carry an older run). A check-in line is sendable only if it was ticked **and saved
  while the switch was on**. **Install-age facts, stated plainly:** `scale` and `setup`
  are ranges describing the install as it is when the batch is cut, and `usage.funnel` says, for
  each first-week milestone the install ever reached (model connected, first build, PR, merge,
  deploy), how many days after install it happened — as an age bucket (`0-1`, `2-7`, …), never a
  date or what the work was. These are the only fields that describe time before consent; they
  are coarse by design, carry no content, and are what "which features you use" covers.
- **Batches:** with the switch on, each day's batch covers `since` → `until` (see above → the
  cut), built from SQL aggregates, stored in `telemetry_batches` and kept until delivered.
  One POST carries the heartbeat plus every undelivered batch; more than 7 undelivered → the
  oldest are marked `dropped` and the envelope says how many. Turning the switch off drops
  undelivered batches at once.
- **Size:** target ≤ 64 KB gzip. Per-run scorecard rows are sent up to 200 per batch; past that the
  batch carries aggregates (counts, sums and a 10-bin score histogram). Groups coarsen level by
  level until there are at most 400 of them and the gzip fits: (1) kind · harness · model · role ·
  workflow · outcome · arm, (2) without workflow and arm, (3) without role, (4) kind · outcome.
  Names are folded (`custom`, `other`) by the database before grouping. The batch's `aggregated`
  note records the level, the groups, and units **covered** of units in total — always all of
  them; nothing is cut. Only if level 4 of every batch still does not fit do the oldest batches
  go, and `dropped_batches` says how many.
- **Wire:** `POST $BOSS_TELEMETRY_URL`, `Content-Type: application/json`,
  `Content-Encoding: gzip`, 5 s timeout, no redirects, no cookies. The body is
  `json.dumps(sort_keys=True, separators=(",", ":"))` gzipped with `mtime=0` (deterministic).
- **Allow-list, then redaction:** every payload is passed through
  `improve_allowlist.sanitize()` (a typed schema: anything not in it is dropped; strings must be
  an enum value, a built-in catalogue key, or are replaced by `custom`/`other`) and then
  `redaction.redact()`: a string the secret or PII gate would change is dropped. Custom workflow,
  role and skill names are sent as `custom`. A model is named only if, normalised (lower case; a
  provider prefix, a date stamp, `-latest`, `[1m]` removed), it is EXACTLY one of the public ids
  in `improve_allowlist.PUBLIC_MODELS` — a fine-tune or local model named after a company or a
  person (`gpt-4o-acmecorp`, `qwen-acme-payroll-7b`) is `other`, in rows and in aggregates.
- **Local log:** the last 30 sends (status, HTTP code, sizes, what was aggregated) are kept and
  each one's exact bytes can be opened.
- **Export batch** downloads the pending payload (`.json`) for an air-gapped install.
- **Rotate install id** makes a new random id and secret and discards the pending payload. If
  anything was sent under the old id, a signed delete for it goes first; if that cannot go now
  (off, no collector, unreachable), the old id **and its secret** are kept and retried with the
  next daily send, and the screen says "Old id kept until its delete goes through".
- **Delete what was sent** posts a signed delete for **every** id this install used and has not
  yet had deleted, then a new id; local batches are cleared, and turning sharing on again the
  same day may send that day's batch, starting from that moment.

#### The heartbeat (exactly these fields)

```json
{"age":"8-30","arch":"arm64","edition":"community","install_id":"bi_…26 chars…",
 "install_method":"compose","os":"linux","version":"1.0.0-rc.4","worked_24h":true}
```

`age` is days since the first project was created, bucketed `0-1 · 2-7 · 8-30 · 31-90 · 91-180 ·
181-365 · 365+`. `worked_24h` is whether any workflow run, build, panel or research changed in the
last 24 hours. `install_id` is random (`bi_` + 26 base32 chars), not derived from anything, and is
not the resource ledger's install id.

#### The envelope

```json
{"v":1,"day":"2026-10-06","install_id":"bi_…","secret_hash":"<sha256 hex of the install secret>",
 "heartbeat":{…the eight fields…},          // absent when opted out
 "batches":[{…batch…}],                       // absent when the switch is off
 "dropped_batches":0}                         // present only with batches
```

#### A batch (switch on)

```json
{"day":"2026-10-06","since":"2026-10-05","until":"2026-10-06",
 "context":{"version":"1.0.0-rc.4","edition":"community"},
 "scale":{"projects":"2-5","active_projects":"1","people":"1","agents_builtin":"21-100",
          "agents_custom":"2-5","workflows_run":"6-20","builds":"6-20","prs_merged":"2-5",
          "deploys":"1"},
 "feedback":{"ratings":{"build":{"yes":3,"no":1}},"chips":{"build":{"too_slow":1}},
             "rejects":{"plan":{"wrong_scope":1}},"steers":{"misread_brief":1},
             "edits":{"prd":{"0-10%":2,"10-30%":1}},"checkin":{"very":1},"lines":[]},
 "usage":{"funnel":{"setup":"0-1","first_build":"0-1","first_pr":"2-7","first_merge":null,
          "first_deploy":null},
          "workflows":{"standard_product_delivery":4,"custom":1},
          "overrides":{"start_anyway":0,"waived_gates":1},"abandoned":1,
          "custom":{"roles":"1","skills":"0","mcp_servers":"2-5"}},
 "setup":{"providers":{"subscription":1,"api_key":1},"deploy_targets":{"ssh_host":1}},
 "health":{"errors":{"build_failed":2},"retries":3,"budget_cap_hits":0},
 "scorecards":{"rows":[{"unit_kind":"build","role_key":"software_engineer","harness":"claude_code",
   "model":"claude-sonnet-5","terminal_state":"completed","tests_passed":1,"tests_total":1,
   "gates_failed":0,"review_approved":true,"pr_merged":true,"cost_usd":1.42,"turns":38,
   "wall_time_s":1460,"score":0.93,"score_version":"1"}],"agg":[]},
 "aggregated":{}}
```

Ranges for counts: `0 · 1 · 2-5 · 6-20 · 21-100 · 101-500 · 500+`. A per-run row also carries
`ref`, the first 12 hex of HMAC-SHA256(install secret, unit id): it lets the collector keep the
newest version of a row that a late outcome changed, and is meaningless without the secret. The full field list with types
is `improve_allowlist.SCHEMA` (engine/boss_engine/services/improve_allowlist.py); the collector
validates against the same schema.

#### Measured (local stack, 2026-10-05)

| Payload | JSON | gzip |
|---|---|---|
| Heartbeat only (the default install) | 339 B | 234 B |
| Heartbeat + one batch, 19 per-run scorecard rows | 10,953 B | 1,690 B |
| One batch built from 50,000 scorecards in 9,720 distinct groups (coarsened to level 2-3) | — | < 64 KB; built in < 1.5 s, < 16 MB peak Python memory (test-enforced; names folded and histograms counted in SQL) |

The real daily send on that stack posted to its own collector (Collector mode on): the preview's
SHA-256 equalled the sent bytes', the collector stored one heartbeat and one batch, and a second
tick the same day sent nothing.

#### Share this run (separate from the switch)

A button on a build and a workflow run. It opens the run's bundle — steps, agent turns (their
text), errors and log tails — after `share_scrub.scrub()`, measured for precision AND recall on
the independent tester's corpus (no known name leaks; 0 of 28 harmless tokens masked):
- **masked:** the Activity log's credential and PII scrub; URLs; IPv4 and IPv6; a word after
  `ssh`/`scp`/`ping`; a dotted name ending in a real public suffix (a bundled subset of the
  public-suffix list, without the suffixes that are also file extensions or code — `md`, `sh`,
  `py`, `map`, `name` …); path-shaped tokens (rooted, ≥ 2 separators, or ending in a file name);
  and this install's own names from every table that holds one — repositories, branches, apps,
  deploy hosts and targets, previews, workflow inputs, projects, orgs, workspaces, people (full
  names and handles; a name part only if ≥ 4 letters and not a role or common word) and the
  operator's "Always mask" words — each with its snake/kebab/camel variants;
- **kept:** timestamps, code (`self.items`, `os.path.join`, `package.json`, `api.v1`), module
  names (`react-dom/client`), role words (release, manager, console, BOSS …)., in an editable box under a plain notice: "Check for
anything private". The person edits it,
adds a note and presses Send once: `POST <collector>/v1/telemetry/share` (≤ 256 KB gzip). Nothing
is sent before that press, and the bundle is not kept by the client.

### Collector (collector mode)

Routes are mounted always but answer `404` unless `system_state['improve_collector'].on`. The
three machine routes are exempt from transport auth (like the signed webhooks) and, on the
console, from the session and same-origin checks only for a POST with no `Origin` header (a
browser always sends one), and the console's `/v1` proxy does not attach its API key to them.

- `POST /v1/telemetry/ingest`: ≤ 256 KB compressed (the client's own cap is 64 KB), ≤ 2 MB after
  a bounded gunzip, `v` must be 1, validated with `SCHEMA` (unknown fields dropped, a bad `install_id`
  refused). The body is streamed and refused the moment it passes the cap. Rate limits, in
  memory: 30 requests per address per hour and 6 per install id per day. The address is the real
  peer; `X-Forwarded-For` is believed only from `BOSS_TRUSTED_PROXIES` (default **loopback
  only**; a reverse proxy on another address must be listed there), taking the right-most
  untrusted hop and skipping malformed ones. An entry may be a host name: Community compose
  trusts `web`, the console's own container, so a request through the console is counted by the
  client the console vouches for and a request at the published API port by its own address. The
  console forwards ONE hop, never a client's X-Forwarded-For as is: the hop its own proxies
  appended (`BOSS_CONSOLE_PROXY_HOPS`, set it to 1 behind your own TLS proxy), or none (0, the
  compose default: no proxy ships, so a console reached directly is one bucket rather than a
  believed claim). The API is started by `python -m apps.api.serve`, which gives uvicorn the IP
  and CIDR entries of `BOSS_TRUSTED_PROXIES` as its `forwarded_allow_ips` — uvicorn otherwise
  believes loopback's X-Forwarded-For on its own. A host-name entry is resolved off the event
  loop, cached a minute, and a failed lookup is never cached. The console decides "is this a
  collector route" once, on the normalised path (query ignored, percent-encoding decoded,
  repeated and trailing slashes collapsed, case-sensitive): every such spelling gets the browser
  check, the cap, no console key, the plain upstream path and no relayed `Location`; a spelling
  middleware cannot skip (`%69ngest`, `//`) is refused there. The console's
  `/v1` route refuses any browser request to these paths (Origin, cookie or any `Sec-Fetch-*`
  header) before reading the body; a refused or over-cap POST answers with `Connection: close`
  and the body is cancelled, so Node stops reading it. One address may introduce 3 **new**
  install ids a day; more are stored but marked `suspicious` and kept out of every count (the
  dashboard says how many are held back). The address is hashed, held an hour in memory, never
  stored, and the API's access log writes "-" for it on `/v1/telemetry/*`. Idempotent per `(install_id, day)` for the
  heartbeat and per `(install_id, batch day)` for each batch: a repeat answers
  `{"ok":true,"duplicate":true}`.
- `POST /v1/telemetry/delete`: `{"install_id","ts","proof","sig"}` where `proof` is the install
  secret and `sig = HMAC-SHA256(secret, "delete|<install_id>|<ts>")`; `sha256(proof)` must equal
  the id's FIRST registered `secret_hash` (from its first heartbeat or its first shared run,
  whichever came earlier — an envelope offering another hash, or none, for a known id is refused,
  409, so a forged heartbeat cannot take an id over) and
  `ts` be within 10 minutes. Deletes every row for that id — heartbeats, batches, shared runs —
  and answers how many; the console reports that number, never "deleted" for nothing.
- `POST /v1/telemetry/share`: ≤ 256 KB gzip, same limits; stored in `tm_shares`. A share for a
  known id carrying a different hash, or none, is refused (409).
- Storage: `tm_installs`, `tm_heartbeats`, `tm_batches`, `tm_shares`, `tm_rollups`. No IP address,
  hostname or name. Retention: rows older than 18 months are rolled into `tm_rollups` (monthly
  active installs, batches, by version and edition) and deleted, daily, by the tick.
- **Observability › Installs** (only in collector mode): a proof card ("312 active installs this
  month, 41% still active after 30 days"), DAU/WAU/MAU, new installs per week, weekly retention
  cohorts, version, edition, OS and arch, install method; for opted-in installs: usage-scale
  totals and distributions, ship rate and reason chips, the first-week funnel, scorecards by
  harness, model and role, error classes. CSV export.

### Routes (console-facing)

| Route | What |
|---|---|
| `GET /v1/scorecards` | the Scorecards view: `group` (role, workflow, harness, model), `win` (7d, 30d, 90d), `kind`, "what's costing you" (each row's `href` is the console's own link to the run) |
| `POST /v1/scorecards/backfill` | build scorecards for history now |
| `GET /v1/experiments`, `POST /v1/experiments`, `POST /v1/experiments/{id}/stop`, `POST /v1/experiments/{id}/promote` | A/B |
| `POST /v1/feedback`, `GET /v1/feedback/prompts`, `POST /v1/feedback/checkin` | ratings, chips, check-in |
| `GET /v1/telemetry/sharing`, `PUT /v1/telemetry/sharing` | switch, heartbeat, collector mode, collector URL (`url`), weights (rescored at once), log; `blocked` says why nothing can be sent |
| `GET /v1/telemetry/preview`, `GET /v1/telemetry/sends/{id}`, `GET /v1/telemetry/export` | the exact payload |
| `POST /v1/telemetry/rotate`, `POST /v1/telemetry/forget` | rotate id, delete what was sent |
| `GET /v1/telemetry/share-bundle`, `POST /v1/telemetry/share-run` | Share this run |
| `POST /v1/telemetry/tick` | the worker's tick (worker principal) |
| `GET /v1/telemetry/collector`, `GET /v1/telemetry/collector.csv` | the collector dashboard |

### Tests

`apps/api/tests/test_improve_boss_*.py` and `engine/tests/test_improve_allowlist.py`: switch off
and heartbeat off (and `BOSS_TELEMETRY=off`, `DO_NOT_TRACK=1`) open **zero sockets** (a
socket-level patch); heartbeat only has exactly the documented fields; seeded prompts, code,
briefs, repo, file and user names, emails and secrets never appear, whole or as an 8-character
piece, in any payload; preview bytes equal sent bytes; a failed send is not retried before the
next day; the 7-batch cap; the size cap aggregates; collector validation, rate limits,
idempotency and delete; 50,000 scorecards build a batch in under 1.5 s and 16 MB peak; every unit
kind's terminal transition writes exactly one scorecard and a rerun is idempotent; late outcomes
rescore; experiments never touch approval fields and a capped arm stops. Web:
`apps/web/tests/improve-*.test.tsx` (copy budget, phone layout, tokens only).

## Open questions

1. Should operator **steers** count against the role's score, or be neutral when the brief was
   vague? Proposal: count them, but show them separately.
2. Should late outcomes (merged, reverted, 24h health) re-score old experiments, or freeze arms at
   decision time? Proposal: show both, decide on the frozen score.
3. ~~Sharing tiers~~: decided 2026-10-05: one "Improve BOSS" switch, off by default.

## How others compare (2026-10)

| Tool | Default | Collects |
|---|---|---|
| Kiro | On for free and individual tiers, off for enterprise | usage, performance, errors, **and content** (prompts, code) |
| Continue | On (opt-out) | suggestion accept/reject, model, tokens, OS and IDE |
| Aider | Opt-in | models, tokens, edit formats, feature use, errors |
| Cline | Consent prompt at install | interaction patterns, errors; no content |
| **BOSS** | **Off, one switch** | the above **plus outcomes** (merged, deployed, approved) and ratings; **no content** |

Lesson from Continue and Cline (bugs where telemetry was sent while disabled): the "switch off →
zero network traffic" test is mandatory.

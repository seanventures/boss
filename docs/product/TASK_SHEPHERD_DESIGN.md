# Task Shepherd — an internal AI developer that finishes what BOSS starts

**Status:** Phase 0 complete · Phase 1 (read-only) complete · Phase 2 not started
**Author:** drafted from the 2026-08-03/04 dogfood session
**Audience:** operator (CEO), engine maintainers

---

## 1. Why this exists

BOSS can build software. It cannot yet *see a piece of work through*.

On 2026-08-03/04 a single operator ran thirteen governed code tasks in one evening. The
outcome, measured from `llm_calls`:

| outcome | runs | spend |
|---|---:|---:|
| completed | 4 | $28.28 |
| failed | 6 | $21.21 |
| blocked | 3 | $17.16 |
| **total** | **13** | **$66.65** |

**$38.37 — 58% of spend — went to runs that produced nothing.** Not because the models were
bad at coding. Every single one of those failures was an *operational* problem: a lease that
was reclaimed, a provider spend cap, a merge that needed a rebase, an approval that needed a
human, a stale process serving a three-day-old tool list.

Each of them was recoverable. Each of them required someone to know that
`405 Method Not Allowed` on a merge means "the base moved, update the branch", or that
`Actor 'mcp' is not allowed to approve a Tier-2 decision` is correct behaviour and needs a
human with the `ceo` role, not a workaround.

That someone was a senior engineer with the codebase open. **No CEO using this product will
have that knowledge, and they should not need it.**

The Task Shepherd is the missing role: an internal AI developer who owns a piece of work from
"the operator asked for it" to "it is merged, or a human has been asked one clear question."

---

## 2. What it is not

Three failure modes to design against explicitly, because the naive version of this idea hits
all of them.

**It is not a retry loop.** During the incident, run `crun_b4b6383d` executed the same
pipeline three times — 15 agent sessions, $6.83, no PR. Nothing noticed it was repeating
itself. A shepherd that reacts to failure by re-running is that bug with a budget.

**It is not an autonomous merger.** BOSS refused `mcp` a Tier-2 approval and was *right* to.
Tier 2 requires the `ceo` role; an agent identity approving its own merge into `main` is
precisely what a governance product exists to prevent. The shepherd's authority stops at the
human gate, permanently and by design — not as a v1 limitation.

**It is not a chatbot over the events table.** Summarising failures is not the job. Choosing
and executing the right remedy is.

---

## 3. The core idea: model proposes, ledger disposes

The design tension is real. Pure LLM reasoning is flexible and good at diagnosis, but it can
loop, rationalise, and spend without bound. A pure state machine is bounded and cheap, but
brittle — it handles the failures you enumerated and nothing else.

**So split the two responsibilities:**

- **The ledger decides what is *permissible*.** For each failure class there is a finite,
  ordered list of remedies. Each remedy may be attempted **at most once per task**. This is
  hard-coded, cheap, and gives a provable termination bound.
- **The model decides what is *appropriate*.** Given the evidence, it chooses which
  permissible remedy fits, or argues that none do and the task should escalate. It must cite
  evidence; a proposal without evidence is rejected by the executor.

The model can never invent a new action, and can never repeat a spent one. It can only choose
among what remains, or stop. **Termination is a property of the ledger, not of the model's
good judgement.**

This is the difference between "intelligent" and "unbounded". The shepherd is intelligent
about *diagnosis and selection*; it is mechanical about *limits*.

---

## 4. The remedy ledger

Derived from the failure classes BOSS already records (`classify_pipeline_exception`,
`is_system_failure`, `needs_conflict_resolution`, gate verdicts). Rows marked **automatic**
were fixed in the engine during the incident and no longer need a shepherd at all — they are
listed so the ledger stays honest about what it actually owns.

| # | Failure class | Evidence signature | Remedies, in order | Est. cost |
|---|---|---|---|---|
| 1 | Provider spend/rate cap | `"spend limit"`, `"rate limit exceeded"` | (a) wait for reset, resume from checkpoint · (b) re-issue on `lean` · (c) escalate | $0 → ~$3 |
| 2 | Turn cap exhausted | `error_max_turns` | (a) retry with raised turn cap on the same branch · (b) split the task · (c) escalate | ~$3 |
| 3 | Stale merge base | merge `405`, `CONFLICTING` | (a) update branch + retry — **automatic** · (b) `extend_code_task` to rebase · (c) escalate | $0 → ~$5 |
| 4 | True merge conflict | update-branch refused | (a) `extend_code_task` with a conflict-resolution brief · (b) escalate with the conflicting paths | ~$5 |
| 5 | Approval required | `ProjectMemberPermissionError`, tier ≥ 2 | (a) **escalate immediately** — no automated remedy exists or should | $0 |
| 6 | Quality gates failed | gate verdicts on a shipped PR | (a) none — PR already shipped with findings; a follow-up task exists — **automatic** | $0 |
| 7 | No code produced | `code_written=false` | (a) re-run implement on the checkpointed branch · (b) re-scope the task · (c) escalate | ~$3 |
| 8 | Duplicate execution | lease reassigned, `state_version` bursts | (a) none — fencing rejects the stale writer — **automatic** | $0 |
| 9 | Infrastructure | `connection refused`, `Executable is not allowlisted` | (a) substrate fallback · (b) escalate — the platform is broken, not the task | $0 |
| 10 | Unknown | anything unmatched | (a) **escalate immediately** with the raw evidence | $0 |

**Row 10 is load-bearing.** An unrecognised failure must never be guessed at. Every expensive
mistake of the incident came from treating an unclassified failure as a familiar one — a
90-minute run was restarted three times on the assumption it was "just slow".

---

## 5. Termination

The shepherd stops when **any** of these is true. They are checked before every remedy, and
each is cheap:

1. **Success.** The task's PR is merged, or its terminal condition is met.
2. **Ledger exhausted.** Every remedy for the observed class has been spent.
3. **Loop detected.** The same `(remedy, failure signature)` pair recurs. One repeat is a
   coincidence; the shepherd treats the second as proof the remedy does not work here.
4. **Budget exhausted.** The task's remaining budget is less than the next remedy's estimated
   cost. It does not start what it cannot finish.
5. **Human gate.** Anything requiring an approval the shepherd cannot hold.
6. **Wall-clock ceiling.** A per-task deadline, so a wait-and-resume remedy cannot idle
   forever.

**Worst-case bound:** with ≤3 remedies per class and 10 classes, a task can consume at most
30 remedy attempts before mandatory escalation — and in practice the budget check binds far
earlier. A run costs ~$5–7, so a $25 task budget permits four attempts, not thirty.

Contrast with the incident: the duplicate-execution bug produced three full passes with **no
bound at all**, because nothing was counting.

---

## 6. Cost discipline

The shepherd must reason about money the way an engineer does — marginal cost against
expected value — not by counting attempts.

```
before each remedy:
    remaining     = task_budget - spent_so_far
    est           = remedy.estimated_cost         # from historical llm_calls, per class
    p_success     = remedy.historical_success_rate  # measured, not guessed
    if est > remaining:                    -> escalate ("budget exhausted")
    if est > 0.4 * task_budget:            -> ask the operator before spending
    if p_success < 0.25 and cheaper alternatives remain -> reorder
```

**The numbers below are a COLD-START PRIOR, not a price list.** They came from one
operator's 13-run local sample. An enterprise repository, a different model tier or a larger
codebase moves them by an order of magnitude — the same remedy measured between $0.88 and
$12.47 within a single evening on one repo. A shepherd that budgeted against a constant would
be uselessly timid on a large project and reckless on a trial.

So both inputs are read from the project, not from the code:

- `estimate_cost()` takes the **75th percentile of that project's own `llm_calls`** over 30
  days, scaled by the remedy's share of a full run. A high percentile, not the mean, because
  under-estimating is the expensive direction: the shepherd must not start what it cannot
  finish.
- `task_budget()` derives from the project's **`budget_policies` daily cap** (10% of it, so
  one task cannot consume the day), never below a floor. `boss-dogfood` resolves to $10,000
  per task; a community trial resolves to the floor.
- Every escalation option carries `estimate_source: measured | prior`, so the operator is
  never quoted a constant as though it were a measurement.

The priors below are only what to assume before the first observation exists:

- a full governed run: **~$6.50** (median of 4 completed)
- a retry that resumes from checkpoint: **~$3** (design + tests skipped)
- `write_tests`: **$1.11–$3.01** · `implement`: **$1.38–$2.66** · panel: **~$2.10**
- a wasted duplicate pass: **~$6.83**

`_tool_budget_and_costs` in `agent_loop.py` already exposes budget state; the estimates come
from `llm_calls` aggregated by stage and failure class, and improve as the corpus grows.

---

## 7. How it reasons — the part that matters

The operator's framing was *"like you and me discussing to resolve the issue"*. That is the
right bar, and it means the shepherd's unit of work is not an action — it is an **argument**.

Every step produces a structured reasoning record:

```
OBSERVE   crun_c867b12a failed after 89.7 min, $4.44, no PR.
          Signals: two write_tests calls 29s apart; 6 agent processes;
                   phase moved implement -> write_tests; lease_owner changed.
HYPOTHESIS Duplicate concurrent execution. The phase moving backwards cannot happen
          in a single execution, and two identical stages 29s apart cannot be one agent.
CHECK     jobs.lease_owner = worker-local-3, but worker-local is still heartbeating (409).
          -> hypothesis confirmed, evidence recorded.
CLASS     #8 duplicate execution.
LEDGER    Remedy (a) fencing — automatic, already applied at engine level.
          No shepherd remedy remains for this class.
DECIDE    The work itself is intact: plan + 9 test files are checkpointed on the branch.
          Re-issue as a fresh attempt that resumes from the checkpoint. Cost ~$3 of a $25
          budget. Expected: reaches implement in ~5 min based on 4 prior resumes.
ACT       extend_code_task(run_id=..., instructions="continue from the checkpoint")
OUTCOME   Reached implement in 5.7 min. PR #68 opened. Cost $6.38.
```

Three properties this enforces:

- **A hypothesis must precede an action.** No remedy without a stated cause.
- **A hypothesis must be checked against data**, not assumed. During the incident I declared
  a run "a zombie with 0 agents" from a single `ps` sample taken between stages — it was
  running fine. A `CHECK` step with a second observation would have caught it.
- **The cost and the expectation are stated *before* acting**, so a wrong call is visible as
  a wrong prediction rather than lost in a total.

This record is the primary artifact. The console renders it; the operator reviews it.

---

## 8. Console: "how is it thinking?"

The operator's stated interest is not the actions — it is the reasoning. So the console leads
with thought, not status.

### 8.1 Shepherd timeline (primary view)

A vertical stream of reasoning records, newest last, one card per step:

```
┌─ 14:22  crun_c867b12a ────────────────────── class #8 · duplicate execution ─┐
│ OBSERVE     89.7 min · $4.44 · no PR                                          │
│             ⚠ phase moved backwards   ⚠ 2× write_tests in 29s   ⚠ 6 agents   │
│ HYPOTHESIS  two executions of one job          confidence ●●●●○               │
│ CHECK       lease_owner=worker-local-3, worker-local still heartbeating (409) │
│             → confirmed                                                        │
│ DECIDE      resume from checkpoint · est $3 of $25 budget · expect implement   │
│             within ~6 min (n=4 prior resumes)                                  │
│ ACT         extend_code_task(...)                                             │
│ OUTCOME     ✓ PR #68 · 17.1 min · $6.38   (predicted $3, actual $6.38 ⚠)      │
└───────────────────────────────────────────────────────────────────────────────┘
```

Every card shows **prediction vs actual**. A shepherd that consistently under-estimates is
visible immediately, and that discrepancy is the main signal of whether it is actually
reasoning or pattern-matching.

### 8.2 Remedy ledger (why it stopped, or didn't)

Per task, what has been spent and what remains — the answer to "why is it still going?" and
"why did it give up?":

```
crun_c867b12a          budget $25.00 · spent $10.82 · remaining $14.18
  class #8  duplicate execution
    (a) fencing (automatic)          ✓ applied
  class #1  spend cap
    (a) wait + resume                ✓ used 04:12   → succeeded
    (b) re-issue on lean             ○ available   est $3
    (c) escalate                     ○ available
  loop detector: no repeats
  next stop condition: budget (4 remedies of headroom)
```

### 8.3 Escalation inbox — one question, not a dump

When the shepherd stops, it produces a **decision-shaped question**, not a log:

```
⏸ Needs you — crun_00bf9f27                                        waiting 6 min
   PR #67 cannot merge: a real conflict in tests/ after #66 landed.
   I can have BOSS rebase and resolve it (~$5, ~15 min, succeeded 3/4 times),
   or you can merge manually.
   Everything else is done: app builds, tests pass, preview live.
   [ Have BOSS rebase ]  [ I'll handle it ]  [ Abandon this PR ]
```

### 8.4 Spend, honestly

A running comparison the operator can trust, because it is the number that got away from us:

```
This week   productive $28.28   ·   recovered $12.40   ·   wasted $3.10
Baseline (pre-shepherd, 2026-08-03):  productive 42%, wasted 58%
```

"Recovered" = spend on remedies that led to a merged PR. "Wasted" = remedies that failed and
were superseded. **If wasted spend is not falling, the shepherd is not working**, and this
panel should make that impossible to miss.

---

## 9. Architecture

```
                    ┌──────────────────────────────────────┐
   operator  ◄──────┤  Console: timeline · ledger · inbox   │
                    └───────────────▲──────────────────────┘
                                    │ reasoning records
                    ┌───────────────┴──────────────────────┐
                    │           TASK SHEPHERD              │
                    │  observe → hypothesise → check →     │
                    │  classify → select remedy → act      │
                    │                                      │
                    │  ledger (bounded)  budget (bounded)  │
                    │  loop detector     escalation writer │
                    └───────┬──────────────────────▲───────┘
                            │ MCP tools            │ events, decisions,
                            ▼                      │ llm_calls, quality_gates
                    ┌───────────────────────────────┴──────┐
                    │            BOSS ENGINE               │
                    │  governed pipeline · fencing ·       │
                    │  decisions · receipts · budgets      │
                    └──────────────────────────────────────┘
```

**Deliberate choices:**

- **The shepherd is a client, not a privileged subsystem.** It acts through the same 31 MCP
  tools an operator has. It cannot do anything a human could not, which means the governance
  gates constrain it identically. If it needs a capability BOSS lacks, the answer is to add
  the capability to BOSS — as `extend_code_task` and merge-recovery were added this session —
  not to give the shepherd a side door.
- **It is stateless between steps.** All state is BOSS's (`events`, `decisions`,
  `shepherd_reasoning`, `shepherd_ledger`). A shepherd crash resumes by re-reading, and two
  shepherds cannot corrupt a task — the same reasoning that produced run fencing.
- **It runs on the cheap lane by default.** Diagnosis is a reading task. Escalate to a
  frontier model only when a hypothesis is rejected twice, and record that escalation as a
  cost line.


### 9.2 One shepherd per task, one coordinator per project

**Decided.** A shepherd instance owns exactly one task; a single coordinator per project owns
what is shared between them.

The argument is from this session, not from theory. When PR #66 merged, #67 and #68 went
CONFLICTING in the same instant. A per-task shepherd cannot see that: each would hit 405,
each would correctly decide "rebase", and three agents would rewrite the same shared branch
against a base that keeps moving. Three correct local decisions, one incoherent global
outcome — the identical shape as two executions writing one run before fencing existed.

Shared mutable state needs a single owner. Independent work does not.

| | Task Shepherd (per task) | Coordinator (per project) |
|---|---|---|
| owns | diagnosis, remedy selection, its ledger + budget | merge ordering, cross-task interference, global budget, concurrency slots |
| reasons about | one task's evidence | relationships between tasks |
| may | choose and execute a remedy | sequence, defer or block a shepherd's action |
| may not | see other tasks | diagnose a failure |

For #66/#67/#68 the coordinator's ruling is: merge #66, hold the others, and on success tell
their shepherds the base moved. Each shepherd then rebases with full local context.

**A shepherd is not a process.** It is event-driven and stateless: woken by a task event,
reads state from BOSS, reasons, acts, exits. A per-task daemon would inherit the exact
failure class that cost this session three separate times — MCP servers serving three-day-old
tool lists, workers holding pre-fix code. Stateless invocation also means a crashed shepherd
resumes by re-reading, and two cannot corrupt a task.

The coordinator is likewise stateless and holds a short advisory lease while arbitrating, so
two coordinators cannot both sequence merges. Fenced by the same mechanism as run execution.

### 9.1 New tables

```sql
shepherd_ledger      task_id, failure_class, remedy_id, status(available|used|blocked),
                     used_at, cost_usd_micros, outcome
shepherd_reasoning   id, task_id, step_no, observe, hypothesis, confidence, check_result,
                     failure_class, remedy_chosen, predicted_cost, predicted_outcome,
                     actual_cost, actual_outcome, created_at
shepherd_escalations id, task_id, question, options(jsonb), evidence(jsonb),
                     status, answered_by, answered_at
coordinator_rulings  id, project_id, subject(jsonb: task ids), ruling, rationale,
                     created_at        -- why a shepherd was told to wait
```

`shepherd_reasoning` carrying both predicted *and* actual is what makes the console's
calibration view possible, and what turns "is it intelligent?" into a measurable question.

---

## 10. Guardrails

1. **No self-approval.** Tier ≥ 2 always escalates. Not configurable.
2. **No new capabilities.** MCP tools only; a missing capability is an engine change.
3. **No unbounded waits.** Wait-and-resume has a deadline; the deadline is a stop condition.
4. **No silent spending.** Any remedy over 40% of remaining budget asks first.
5. **No repeated remedies.** Enforced by the ledger, not by the model's memory.
6. **Escalation is a first-class success.** A shepherd that escalates early with a good
   question outperforms one that spends $30 discovering the same thing. Its scorecard must
   reward this, or it will learn to thrash.

---

## 11. Implementation plan

**Phase 0 — instrumentation. ✅ DONE.** Four tables, the bounded core (`task_shepherd.py`), and a replay proving 10/12 of the session's real failures classify and 10 are auto-recoverable — $50.85 of $66.65 salvageable.

**Phase 0 — instrumentation (½ day).** Tables + reasoning records written by the *existing*
retry paths. No autonomy. Produces a corpus of real classifications and costs, and makes the
console buildable and reviewable before anything acts on its own.

**Phase 1 — read-only shepherd. ✅ DONE.** `shepherd_service.py`. Observes, hypothesises,
CHECKS, classifies, proposes — executes nothing. Verified against the session's own failures:
8 proposals, each with a measured cost and a corroboration verdict.

Two design bugs were caught by running it against real data, before a penny was spent:

- *the deadline measured RUN AGE.* Every day-old failure stopped instantly as "past
  deadline", when the shepherd had not started. A deadline bounds the shepherd's effort, not
  the task's staleness. Now `shepherd_minutes`, measured from its first reasoning step.
- *the PR_STEP_FAILED check refuted itself.* It asked for a PR as corroboration that the PR
  step had failed — the absence of a PR is precisely what that class predicts.

Both are the kind of error unit tests cannot find, because both were written into the tests
and the code by the same mistaken assumption. This is the argument for Phase 1 existing at
all.

**Behaviour observed:** a corroborated hypothesis produces a costed proposal; an
uncorroborated one escalates rather than spending. The shepherd will not put money behind a
diagnosis it could not confirm.

**Phase 1 — read-only shepherd (1 day).** Observes, hypothesises, classifies, proposes a
remedy — and **executes nothing**. Every proposal lands in the escalation inbox as
"I would do X, because Y, costing ~$Z". The operator approves or rejects. This is the phase
where the reasoning is judged, and it is also a real product on its own: it is what a CEO
needs even if it never gains autonomy.

**Phase 2 — bounded autonomy (1 day).** Executes only the classes with an unambiguous remedy
and a low cost ceiling: #1 spend cap, #2 turn cap, #3 stale base, #7 no code. Everything else
still escalates. Ledger + budget + loop detection enforced.

**Phase 3 — calibration (ongoing).** Track predicted vs actual. Reorder remedies by measured
success rate. Retire remedies that never work; promote classes to autonomy once their
success rate and cost variance justify it.

Phase 1 is the one to build first and judge, because it makes the reasoning visible without
letting it spend anything.

---

## 12. How we will know it works

Measured against the 2026-08-03/04 baseline, which is real and unflattering:

| metric | baseline | target |
|---|---:|---:|
| spend on runs producing nothing | **58%** | < 15% |
| operator interventions per completed task | ~6 | ≤ 1 |
| operator messages needing engine knowledge | most | 0 |
| median time-to-PR incl. recovery | 90 min (worst) | < 25 min |
| escalations with a specific answerable question | 0 | 100% |

The last row is the honest one. During the incident the operator was repeatedly handed
*"here are five defects, what would you like to do?"* — which is not a question, it is a
transfer of work. The shepherd's job is to arrive with one decision, its evidence, and its
cost.

---

## Appendix A — incidents this design is derived from

Each of these cost real money and is the reason for a specific rule above.

| incident | cost | rule it produced |
|---|---:|---|
| duplicate execution, 3 passes, no PR | $6.83 | §5.3 loop detection · §4 row 8 |
| cancelled run kept executing 40 min | $6.73 | §10.3 bounded waits · cooperative cancellation |
| spend cap destroyed all in-flight work | ~$10 | §4 row 1 · checkpoint-then-resume |
| 90-min run restarted 3× on an assumption | $4.44 | §7 hypothesis must be *checked* |
| merge stopped at the first conflict | — | §4 rows 3–4 |
| approval path broken end to end (`force` NameError) | — | §4 row 10 · never guess an unknown class |
| `org_members` empty → no actor could approve | — | §10.1 escalate, never work around a gate |
| operator handed a defect list instead of a question | — | §12 last row · §8.3 |

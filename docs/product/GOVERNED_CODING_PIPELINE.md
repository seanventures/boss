# The Governed Coding Pipeline — BOSS's Default Pattern for All Coding Tasks

Status: Pattern defined in specpack; runtime wiring is the follow-on (see §9)
Date: 2026-06-19
Owner: param
Canonical shareable skill: `skills/boss-governed-coding-pipeline/SKILL.md`

## 1. Why this exists

Frontier models are commoditized — everyone has them. The bottleneck in agentic engineering
is no longer *generating* code; it is *establishing trust* in generated code. The core failure
mode is **correlated error**: when one agent (one context) writes the code, writes the tests,
and reviews the result, the tests and the review inherit the same misunderstanding that
produced the bug — the suite green-lights the very defect it should catch, and an after-the-fact
reviewer faces a large, fully formed diff with none of the shared context a human accrues while
writing. This is the **separation-of-duties** principle — an author must not audit their own
work, the same control behind code review and SOX/SOC2 segregation — applied to AI agents.

BOSS's moat is therefore not "an orchestrator" (ADK/Azure/LangGraph all ship those). It is
**governed, durable, auditable agentic engineering**: quality made *structural and enforced*
rather than *prompted and hoped-for*. This pattern makes that concrete and the default for
every coding task.

> Note: in our 2026-06 dogfood, BOSS's real code path (`code.task` → single `ClaudeCodeAgent`)
> had one agent write the code *and* its tests *and* iterate to green — the exact anti-pattern.
> This pipeline fixes that by construction.

## 2. The spine — break correlation by role + context separation

Across the whole pipeline, **whoever writes something does not approve it.** Five separations
are enforced as policy rules, each step with its own context:

- **plan reviewer ≠ planner.** A fresh agent tries to break the plan *before* any code.
- **tester ≠ coder.** Tests are written from the spec, *before* the code (`write tests`), so they
  check the intent, not the coder's mistakes.
- **code reviewer ≠ coder.** A fresh reviewer (`review code`) tries to break the change and
  defaults to "not safe" when unsure.
- **security reviewer ≠ coder.** A separate security pass.
- **full-test runner ≠ coder.** Runs the whole suite and confirms nothing else broke.

And **gate ≠ prompt**: standards become a fail-closed `QualityGate` object (missing evidence =
fail), not instructions an agent forgets when context summarizes away.

## 3. What it borrows from the field (in plain steps)

The plain steps above encode the proven agentic-engineering patterns: tests written by a
different agent than the coder (`write tests` ≠ `implement`); a fresh agent that tries to break
the change (`review code`); a separate security pass (`security check`); proving the tests
actually catch bugs (`check tests`); standards the change is checked against (`standards`);
checks that fail closed (`final gate`); bounded loops; and human sign-off only where it pays.
The difference from those frameworks is that here they are **enforced** by BOSS, not suggested.

## 4. The pipeline (workflow `governed_coding_pipeline`)

Plain steps, run by different agents:

```
standards → spec → refine → plan → review plan → approve plan(human) → tasks →
write tests → implement → review code → security check → check tests → run all tests →
consistency check → final gate(fail-closed) → approve(human; two people at top risk)
```
- `implement` makes the tests it did **not** write pass, and may not touch them.
- `review plan`, `review code`, `security check`, `run all tests` run in **their own context**;
  failures loop back (max 3 each, then it asks a human).
- `final gate` **fails closed**: missing evidence counts as fail.

> The full flowchart, per-step briefs, and the "what's different" table live in the canonical,
> shareable **`skills/boss-governed-coding-pipeline/SKILL.md`** — the default skill for all
> coding tasks.

## 5. How many steps run, by risk (`governed_coding_pipeline_policy`)

Same levels as the autonomy ladder, so it's not overkill for a tiny change and full for production:

| Risk | Example | Steps that run | Human sign-off | Approval |
|---|---|---|---|---|
| tiny | docs, refactor already covered by tests | implement → final gate | — | none |
| low | small internal change | standards, spec, refine, plan, review plan, tasks, write tests, implement, final gate | — | one |
| standard | something users see, real logic | + approve plan, review code, security check, check tests, run all tests, consistency check | plan + final | one |
| high | login, payments, secrets, migrations, deploys | all of it, stricter bars | plan + final | **two people** |

The rule holds at every level: whoever writes something doesn't approve it, the final gate fails
closed, every step leaves a receipt, loops are bounded, nothing merges without the final gate.
Risk jumps to high automatically when the change touches login/payments/secrets/migrations/deploys.

## 6. Worked example — one real task through the steps

A follow-up to the leads app (`paramg/boss-sandbox`):

> **Task:** "Add a `list_by_status(status)` method that returns only leads with that status."
> (something users rely on → **standard risk**)

The trap: `set_status` already *rejects* an unknown status, so `list_by_status` should reject one
too — not silently return `[]`.

| Step | Who | What happens |
|---|---|---|
| spec | `pm` | Writes clear criteria, incl. *"an invalid status is rejected, like `set_status`"* and *"empty result when none match"* |
| refine | `pm` | Pins the ambiguity: should an invalid status raise or return `[]`? — locks "raise" |
| review plan | `design_critic` *(≠ planner)* | Challenges the design: case sensitivity? empty store? loops back if unresolved |
| approve plan | human | Signs off before any code |
| write tests | `qa` *(≠ coder)* | Tests first, from the spec: happy path, **invalid-status raises**, empty result, case sensitivity |
| implement | `software_engineer` | Makes those tests pass — **forced** to add the validation it might skip; can't touch the tests |
| review code | `adversarial_reviewer` *(fresh)* | Tries to break it: `list_by_status('WON')`? whitespace? |
| security check | `security_reviewer` | Could a crafted status leak data in a DB-backed version? |
| check tests | `qa` | Flips the guard on purpose; if tests still pass, they're too weak |
| run all tests | `integration_validator` | Full suite — did `add_lead`/`set_status` still work? |
| consistency check | `qa` | Standards + spec + tests + code all agree? |
| final gate | (automatic) | Collects all evidence; **fails closed** |
| approve | `release_manager` | Human sign-off (two people at top risk) |

Every step leaves a **receipt**; nothing merges until the **final gate** passes. Each review loop
sends findings back — at most 3 times, then it asks a human instead of spinning.

## 7. Before vs. after this change

**The bug that ships "before" and is caught "after":** the implementer adds `list_by_status`
but writes a self-test that only checks the happy path; `list_by_status('WON')` silently
returns `[]` instead of raising. Because the *same* agent wrote the code and the test, the
test never probes the gap — the PR opens green and the inconsistency ships.

| | **Before (today's `code.task`)** | **After (governed pipeline)** |
|---|---|---|
| Who writes the tests | the implementer (same agent) | `qa`, **before** the code, from the spec |
| Who reviews | the implementer / no fresh review | `adversarial_reviewer`, **isolated context**, refute-first |
| Standards | in the prompt (forgotten as context summarizes) | **fail-closed `QualityGate`** object |
| Test quality | unmeasured (green ≠ good) | **checked** — tests are broken on purpose to prove they catch bugs |
| The trap above | **ships** — self-test misses it | **caught** — independent test asserts it; reviewer probes it; `check tests` fails a weak suite |
| Rigor | same for a typo and for auth code | **scales with risk tier** (0→3); dual-control at tier 3 |
| Evidence | a PR + the agent's say-so | standards, spec, independent tests, the change, code review, security, test-strength, final gate — all **receipted & replayable** |
| When it's "done" | tests (it wrote) pass | every **separation invariant + fail-closed gate** satisfied |

The observed "before" is real: dogfood run `crun_860c68df…` had **one** `ClaudeCodeAgent`
produce `leads_app.py` **and** `test_leads.py` and iterate to green — a self-graded exam. The
"after" makes a different role set the exam, a different role grade it, and the grade itself
get audited.

## 8. Design lineage — standard controls, made enforceable

The pipeline composes well-understood software-delivery controls and applies them to AI agents,
with the difference that here they are **enforced**, not advisory:

- **Separation of duties** — the author of an artifact never approves it (the control behind
  code review and SOX/SOC2 segregation).
- **Critique before code** — defects are cheaper to fix in design than in implementation.
- **Independent / test-first authorship** — tests encode intent, written by a different role.
- **Adversarial and dedicated security review** — independent passes that try to break the change.
- **Mutation grading** — measure whether the tests can actually catch bugs.
- **Bounded feedback loops** — every loop has a ceiling, then escalates to a human decision.
- **Human gates only where they pay** — design sign-off and pre-merge approval.
- **Full audit trail** — every step leaves durable evidence.

The contribution is not the patterns themselves but making them a **control** rather than a
recommendation: enforced by the BOSS kernel (receipts, Decisions, budget, dual-control) and
scaled to risk.

## 9. Status & the follow-on (runtime wiring)

**Done (this change):** the pattern is fully expressed in the specpack and registered as the
**default** — roles (`adversarial_reviewer`, `design_critic`, `security_reviewer`,
`integration_validator`), object (`QualityGate`), skill set (`coding_pipeline.*`, 8 skills),
policy (`governed_coding_pipeline_policy`), workflow (`governed_coding_pipeline`), and the
shareable orchestrator (`skills/boss-governed-coding-pipeline/SKILL.md`) — all added to
`core_v1`/`core_v2`. All 8 skills pass `validate_skill_spec`; all 4 roles pass
`validate_role_spec`; both packs' installs resolve.

**Next (runtime):** `code_task_service` currently runs a single agent. To execute this pattern,
it should orchestrate the stages as separate agent invocations with **isolated context per
role**, persist a `QualityGate` row, and block the merge Decision until it passes. The
governance primitives it needs already exist (roles, receipts, decisions, dual-control on
ChangeSet, budget governor, the sandbox) — this is composition, not new infrastructure.

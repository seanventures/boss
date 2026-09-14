# The Governed Coding Pipeline

How BOSS builds code: every coding task runs through plain, role-separated steps — different
agents write, test, and review — with fail-closed checks, full receipts, a cost/rigor dial the
customer controls, and any model behind it.

Status: runtime **live** end-to-end at tier ≥ 2 — design phase + test-first build + concurrent
review panel (mutation + security + full-suite) + fail-closed gate, model-agnostic (proven on
Claude + Azure gpt-4o). Cost-control feature **complete** (runtime + API + UI). The human
approve-plan gate is the remaining increment (§11). Date: 2026-06-19.
Canonical shareable skill: `skills/boss-governed-coding-pipeline/SKILL.md`.
Diagrams render inline on GitHub; each also has a **"View as zoomable image →"** link to a
standalone SVG (`docs/product/diagrams/`) for infinite vector zoom + fullscreen. Re-render with
`python3 docs/product/build_diagrams.py`.

---

## 1. Why this feature exists

AI can write code in seconds. The hard part is no longer *generating* code — it's **trusting**
it, **at a cost you control**. Two problems block teams from shipping AI-written code to
production:

**Problem 1 — Correlated error (a trust problem).** When *one* agent writes the code, writes the
tests, and reviews the result, the tests and the review inherit the same misunderstanding that
produced the bug. The suite green-lights the very defect it should catch; an after-the-fact
reviewer faces a large, fully formed diff with none of the context a human builds while writing.
It's a *self-graded exam*. We watched this happen in our own dogfood: a single agent produced
`leads_app.py` **and** `test_leads.py` and iterated to green — and a later independent review
found a real bug (non-ASCII input handling) that the passing tests never probed.

**Problem 2 — Unbounded cost (a budget problem).** The obvious fix — more agents, more reviews —
costs more tokens and more time. AI budget is finite. If "more rigorous" always means "more
expensive," teams either overpay on every trivial change or skip rigor where it matters.

**This feature solves both.** It applies the **separation-of-duties** principle — *the author of
something never approves it*, the same control behind code review and SOX/SOC2 segregation — and
makes the **amount of rigor a dial customers tune to the work and their budget**.

> The differentiator isn't "an orchestrator" (every framework ships those). It's that here the
> separation, the checks, and the sign-offs are **enforced by BOSS** — receipts, decisions,
> budget, two-person sign-off — not merely *suggested in a prompt*. Quality is a **control**,
> not a recommendation.

---

## 2. What we built (three pillars)

```mermaid
flowchart LR
    T([Coding task]) --> DIAL{{"Cost/rigor dial<br/>(profile + risk)"}}
    DIAL --> P["Governed pipeline<br/>role-separated steps"]
    P --> M["Any model<br/>(Claude · OpenAI · Azure · local)"]
    P --> G[("Fail-closed gate<br/>+ receipts")]
    G --> OUT([Mergeable change<br/>+ evidence trail])
    classDef a fill:#e6f3ff,stroke:#333,color:#000
    classDef b fill:#fff3cd,stroke:#333,color:#000
    classDef c fill:#d4edda,stroke:#333,color:#000
    class P,M a
    class DIAL b
    class G,OUT c
```

📂 **[View as zoomable image →](./diagrams/01-value-flow.svg)** _(opens in a new tab — use your browser's zoom / scroll-wheel + fullscreen)_

1. **A governed, role-separated pipeline** — different agents write, test, and review; quality is
   a property of the workflow, not a hope placed on one agent (§3).
2. **A cost/rigor dial** — profiles (lean / balanced / strict / custom) scale *how much* of the
   pipeline runs and *which models* run it, so prototypes run low-cost and production runs strict (§4).
3. **Model-agnostic execution** — the steps are guidance any model impersonates; route per step
   (low-cost vs strong), on Claude, OpenAI, Azure, or local (§5).

All three are wired into BOSS's durable kernel: receipts, decisions, budget reservations,
dual-control, and a full audit trail.

---

## 3. The pipeline — plain steps, run by different agents

> **What the gate does by default.** BOSS ships `BOSS_CODE_GATE_NEVER_BLOCK=true`. A failing
> gate drives up to three repair rounds; if those are spent and reviewers are still unhappy, the
> run **ships with the dissent recorded** — the unresolved gates, the reviewer's reasoning and the
> confidence land on the run and become their own follow-up task — rather than halting for a
> human. That is a deliberate autonomy choice: a system that stops whenever its own reviewers stay
> unhappy delivers nothing on exactly the runs that need it most, and the operator ends up
> adjudicating the small defects the agents were hired to handle. Two failures are treated as facts
> rather than opinions and still block: a regression against previously-passing tests, and no code
> produced at all. Set `BOSS_CODE_GATE_NEVER_BLOCK=false` for fail-closed.
>
> Likewise `BOSS_CODE_REVIEW_PANEL_SIZE` defaults to **1**. The refute panel scales to *k*
> reviewers with quorum aggregation; at 1 it is a single adversarial reviewer. Raise it for
> higher assurance, at proportional cost and latency.

**The one rule: whoever writes something does not approve it.** Five separations are enforced as
policy, each step in its own isolated context:

- **plan reviewer ≠ planner** — a fresh agent tries to break the plan *before* any code.
- **tester ≠ coder** — tests are written from the spec, *before* the code, so they check the
  intent, not the coder's mistakes.
- **code reviewer ≠ coder** — a fresh reviewer tries to break the change; defaults to "not safe."
- **security reviewer ≠ coder** — a separate, dedicated security pass.
- **full-test runner ≠ coder** — runs the whole suite and confirms nothing else broke.

And **gate ≠ prompt**: standards become a **fail-closed** check (missing evidence = fail), not an
instruction an agent forgets when context summarizes away.

### Flowchart

Diamonds are checks (their result gates the loop); yellow are **human** sign-offs; curved arrows
are loops with a limit (≤3, then it asks a human instead of spinning).

```mermaid
flowchart TD
    Start([Task + risk]) --> S0["standards"] --> S1["spec"] --> S2["refine"] --> S3["plan"]
    S3 --> S4{"review plan"}
    S4 -->|"issues · loop<3"| S3
    S4 -->|clean| G1{{"approve plan (human, risk≥2)"}}
    G1 -->|approve| S6["tasks"] --> S7["write tests"] --> S8["implement"]
    S8 --> S9{"review code"}
    S9 -->|"not safe · loop<3"| S8
    S9 -->|safe| S10{"security check"}
    S10 -->|"issues · loop<3"| S8
    S10 -->|clean| S11{"check tests"}
    S11 -->|"too weak"| S7
    S11 -->|strong| S12{"run all tests"}
    S12 -->|"broke · loop<3"| S8
    S12 -->|green| S13{"consistency check"}
    S13 -->|"drift"| S8
    S13 -->|agree| S14{"final gate · records dissent"}
    S14 -->|fail| S8
    S14 -->|pass| G2{{"approve (human; 2 people at top risk)"}}
    G2 -->|approve| Done([Mergeable change + receipts])
    classDef gate fill:#ffd700,stroke:#000,color:#000
    classDef chk fill:#ffcccc,stroke:#000,color:#000
    classDef stp fill:#e6f3ff,stroke:#000,color:#000
    classDef dn fill:#d4edda,stroke:#000,color:#000
    class G1,G2 gate
    class S4,S9,S10,S11,S12,S13,S14 chk
    class S0,S1,S2,S3,S6,S7,S8 stp
    class Start,Done dn
```

📂 **[View as zoomable image →](./diagrams/02-pipeline.svg)** _(opens in a new tab — use your browser's zoom / scroll-wheel + fullscreen)_

**What each step does** (internal harness names — the AI agents use them; never developer-facing):

| Step | Plain meaning |
|---|---|
| `standards` | the project's coding rules, from the repo + org rules — every step is checked against them |
| `spec` | what to build, with a change list (added / changed / removed) |
| `refine` | clear up anything vague → clear, testable acceptance criteria |
| `plan` | the technical design |
| `review plan` | a different agent critiques the design before any code |
| `approve plan` | human sign-off (risk ≥ standard) |
| `tasks` | break into small pieces, each given only the context it needs |
| `write tests` | tests first, by a different agent than the coder |
| `implement` | the smallest change that makes those tests pass; can't touch the tests |
| `review code` | a fresh agent tries to break it |
| `security check` | a separate security pass |
| `check tests` | break the code on purpose to prove the tests catch it |
| `run all tests` | the full suite — confirm nothing else broke |
| `consistency check` | standards, spec, tests, and code all agree |
| `final gate` | collect all evidence. Failing gates drive repair (≤3 rounds); if repair is spent the run **ships with the dissent recorded** and re-queued as its own task, unless `BOSS_CODE_GATE_NEVER_BLOCK=false`, which restores fail-closed |
| `approve` | human sign-off (two people at top risk) |

---

## 4. The cost/rigor dial — tunable efficiency

The same pipeline can run low-cost or strict. A **profile** scales *how much runs* and *which models
run it*, and the risk tier is inferred per task and clamped to the profile's range.

```mermaid
flowchart LR
    A["Profile<br/>lean · balanced · strict · custom"] --> C
    B["Task risk<br/>(inferred; sensitive=high)"] --> C
    C{{"Resolved tier (0–3)"}} --> D["Steps that run<br/>tier 0 = 1 agent · tier 2 = 3+"]
    C --> E["Model per step<br/>low_cost vs strong"]
    D --> F([Cost & latency])
    E --> F
    classDef x fill:#e6f3ff,stroke:#333,color:#000
    classDef y fill:#fff3cd,stroke:#333,color:#000
    class A,B,D,E x
    class C y
```

📂 **[View as zoomable image →](./diagrams/03-cost-dial.svg)** _(opens in a new tab — use your browser's zoom / scroll-wheel + fullscreen)_

| Profile | For | Tier range | Models | Relative cost |
|---|---|---|---|---|
| **Lean** | prototypes, throwaway | 0–1 | low-cost everywhere | ≈ 1× |
| **Balanced** (default) | normal work | 0–3 (inferred) | low-cost for low-cost steps, strong for implement/review/security | ≈ 1.5–3× |
| **Strict** | production / enterprise | 2–3 | strong everywhere, two-person at top | ≈ 4–8× |
| **Custom** (enterprise) | your call | any | any per-step | — |

**Proven, not claimed.** Same `is_even` task, same model (gpt-4o): **Lean ran 2 steps for
$0.0261 and opened a PR**; **Strict ran 3 steps for $0.0378, and its independent review caught a
problem → blocked.** ~31% lower cost from step-count alone; per-step model routing widens it
further. Every run records its profile, tier, steps, and **per-step cost** — visible in the UI.

**Safety override:** a sensitive change (auth, payments, secrets, migrations, deploys) is **never
cost-capped below standard rigor**, even under Lean. Cost dials must not silently de-risk.

**Editions & defaults:** community edition uses the lean/balanced/strict **presets** and defaults
new projects to **lean** (a first trial never looks expensive); **enterprise** can define
**custom profiles** with their own tier ranges and per-step model routing.

---

## 5. Model-agnostic — the pattern is the guidance, any model plays the role

The step descriptions are the *guidance*; the model impersonates the role. BOSS resolves the
agent through a single seam (`make_code_agent`): **`claude_code`** (Claude Code CLI) or
**`native`** (OpenAI / Azure via the chat API), with local models routable through the gateway.
Each step can run on a low-cost or a strong model. The recorded run shows exactly **which engine
and model ran each step** — proven live on both Claude and Azure gpt-4o.

This is the point of "rent the model, own the orchestration": customers are never locked to one
vendor, and they place strong models where reasoning is hard and low-cost models where it isn't.

---

## 6. What customers get (benefits)

| Benefit | How this feature delivers it |
|---|---|
| **Trustworthy AI code** | a different agent writes the tests, another tries to break the change, a separate pass checks security — correlated error is broken by construction |
| **Pay for rigor only where it's worth it** | the cost/rigor dial: prototypes run ≈1×, production runs strict; the average workload stays low-cost |
| **No "expensive surprise"** | trials default to Lean; a visible per-tier cost comparison in the console; effort auto-scales to risk |
| **No vendor lock-in** | model-agnostic — Claude, OpenAI, Azure, or local; low-cost vs strong per step |
| **Auditable & compliant** | every step leaves a receipt; unresolved gates are recorded on the run and re-queued rather than discarded; two-person sign-off on high-risk; a replayable evidence trail (the spec, tests, reviews, costs) |
| **Safe by default** | sensitive changes (auth/payments/secrets) can't be cost-capped below standard rigor; a regression or an empty diff blocks even in advisory mode |
| **Customizable for the enterprise** | define custom profiles per kind of work; per-project standards and defaults; edition-gated |
| **Consistent quality across the org** | the same enforced pipeline runs for every coding task — quality doesn't depend on which engineer or which prompt |

---

## 7. Worked example — one real task through the steps

A follow-up to the leads app (`paramg/boss-sandbox`):

> **Task:** "Add a `list_by_status(status)` method that returns only leads with that status."
> (something users rely on → **standard risk**)

The trap: `set_status` already *rejects* an unknown status, so `list_by_status` should reject one
too — not silently return `[]`.

| Step | Who | What happens |
|---|---|---|
| spec | `pm` | Clear criteria, incl. *"an invalid status is rejected, like `set_status`"* and *"empty result when none match"* |
| refine | `pm` | Pins the ambiguity: should an invalid status raise or return `[]`? — locks "raise" |
| review plan | `design_critic` *(≠ planner)* | Challenges the design: case sensitivity? empty store? |
| approve plan | human | Signs off before any code |
| write tests | `qa` *(≠ coder)* | Tests first: happy path, **invalid-status raises**, empty result, case sensitivity |
| implement | `software_engineer` | Makes those tests pass — **forced** to add the validation; can't touch the tests |
| review code | `adversarial_reviewer` *(fresh)* | Tries to break it: `list_by_status('WON')`? whitespace? |
| security check | `security_reviewer` | Could a crafted status leak data in a DB-backed version? |
| check tests | `qa` | Flips the guard on purpose; if tests still pass, they're too weak |
| run all tests | `integration_validator` | Full suite — did `add_lead`/`set_status` still work? |
| consistency check | `qa` | Standards + spec + tests + code all agree? |
| final gate | (automatic) | Collects all evidence; drives repair, then ships with dissent recorded |
| approve | `release_manager` | Human sign-off (two people at top risk) |

**Before vs. after.** Before (one agent): it writes a happy-path self-test; `list_by_status('WON')`
silently returns `[]` and the inconsistency ships, because the same agent wrote the code and the
test. After: a different role sets the exam, a different role grades it, `check tests` proves the
suite has teeth, and the grade itself is receipted. The bug is caught at three independent points.

---

## 8. How many steps run, by risk

| Risk | Example | Steps that run | Human sign-off | Approval |
|---|---|---|---|---|
| tiny | docs, refactor already covered by tests | implement → final gate | — | none |
| low | small internal change | standards, spec, refine, plan, review plan, tasks, write tests, implement, final gate | — | one |
| standard | something users see, real logic | + approve plan, review code, security check, check tests, run all tests, consistency check | plan + final | one |
| high | login, payments, secrets, migrations, deploys | all of it, stricter bars | plan + final | **two people** |

Risk jumps to high automatically when the change touches login/payments/secrets/migrations/deploys.
The rule holds at every level: whoever writes something doesn't approve it, the final gate fails
evidence is recorded, every step leaves a receipt, loops are bounded, and no gate result is discarded.

---

## 9. How it works (architecture)

- **Specpack** defines the pattern declaratively: workflow `governed_coding_pipeline`, policy
  `governed_coding_pipeline_policy` (risk tiers + always-rules), skills `coding_pipeline.*`,
  the `Standards` and `QualityGate` objects, and the roles (`design_critic`,
  `adversarial_reviewer`, `security_reviewer`, `integration_validator`, plus pm/qa/SE/cao/
  program_manager/release_manager). Registered as default in `core_v1` / `core_v2`.
- **Runtime** (`governed_code_task_service.py`): provisions one sandbox, then runs each step as a
  **separate, isolated agent invocation** (no shared session) on the chosen model; re-runs the
  tests itself (never trusts the agent's self-report); enforces the implementer can't edit the
  tests; records per-step cost + model + engine; writes a fail-closed `quality_gates` row; opens
  the PR only if the gate passes.
- **Cost control**: `pipeline_profiles` (presets in code, custom in the DB) + the profile API
  (`/v1/pipeline-profiles`) + edition gating; per-task profile/tier override on `/v1/code-tasks`.
- **UI**: the "Coding Pipeline" console area — profile cards + the per-tier cost comparison.
- **Governance integration**: budget reservation per task, receipts per step, decisions for human
  gates, dual-control (two named approvers) at high risk — all primitives BOSS already owns.

---

## 10. Design lineage — standard controls, made enforceable

The pipeline composes well-understood software-delivery controls and applies them to AI agents —
the difference being that here they are **enforced**, not advisory: separation of duties; critique
before code; independent / test-first authorship; adversarial and dedicated security review;
proving the tests catch bugs; bounded feedback loops; human gates only where they pay; a full
audit trail. The contribution is making them a **control** (receipts, decisions, budget,
dual-control), scaled to risk.

---

## 11. Status & roadmap

**Live today (runtime, model-agnostic).** At tier ≥ 2 the runner executes the pipeline end to end
as separate agents — on any model (proven on Claude and Azure gpt-4o), with per-step cost recorded
and the PR gated:

```
standards → spec → plan → review plan        (design phase — spec-driven; refined spec + plan
                                              feed the coding steps)
  → write tests → implement                  (test-first; the implementer can't touch the tests)
  → ┌ review code ┐
    ├ security    ┤  run CONCURRENTLY (read-only)   → check tests (mutation, serial)
    └ run all     ┘                                  → run all tests
  → final gate (repair ≤3, then ship-with-dissent; fail-closed is opt-in) → PR
```

- **Design phase (Wire-Phase 2):** `standards → spec → plan → review plan` run before any code;
  the refined spec + plan are fed forward so write-tests/implement are spec-driven. (The human
  *approve plan* sign-off is Wire-Phase 2b; today `review plan` records a `PLAN: OK/REVISE` note.)
- **Review panel (Wire-Phase 1/1b):** the three read-only checks (`review code` ∥ `security` ∥
  `run all tests`) run **concurrently**, bounded by a lane-capacity limit
  (`BOSS_CODE_AGENT_MAX_CONCURRENCY`); the **mutation** check (`check tests`) is serial — it plants
  a bug, confirms the tests fail, then restores the real code. The adversarial review judges
  against the task's acceptance criteria (defaults to *survived* — no refute-first false positives).
- So `strict` genuinely runs **~9 steps (~$0.06)** vs `balanced`'s 3 (~$0.04); the tiers and custom
  profiles produce a real cost/rigor difference. Budget settles at **actual** model spend.
- **Cost-control feature complete:** profiles, tier inference, per-step model routing, edition
  gating, and the console UI.

**Next increment (Wire-Phase 2b / 3):** the human **approve plan** sign-off gate (a Decision that
blocks before code), `consistency check`, and `tasks`/work-graph (parallel sub-tasks). Remaining
efficiency lever: **prompt caching** of the stable repo context across steps. See
`docs/product/PIPELINE_WIRING_PHASE1.md` and `docs/product/PIPELINE_COST_CONTROL.md`.

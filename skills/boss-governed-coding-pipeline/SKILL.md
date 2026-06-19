---
name: boss-governed-coding-pipeline
description: The default steps every BOSS coding task follows. Takes a change from a spec to a tested, evidence-backed, mergeable diff through plain, role-separated steps — each run by a different agent, with checks that fail closed and a number of steps that scales with risk. Unlike an advisory skill, every step is enforced by BOSS (receipts, decisions, budget, two-person sign-off). Internal to the harness (the AI agents follow it); not developer-facing. Use for any non-trivial coding task; skip only tiny changes.
---

# BOSS Governed Coding Pipeline

This is how BOSS builds code. A task goes from a spec to a tested, reviewed, mergeable change
through **plain steps run by different agents** — the agent that writes something never judges
it. The steps, the checks, and the sign-offs are **enforced by BOSS** (receipts, decisions,
budget, two-person sign-off), not just suggested in a prompt.

These names are **internal** — the AI agents use them to do the work to the team's standard.
Developers never see them; they just get a change that meets the bar.

## The one rule

> **Whoever writes something does not get to approve it.** The planner ≠ the plan reviewer,
> the tester ≠ the coder, the coder ≠ the code reviewer ≠ the security reviewer. Different
> agents, fresh context. That's what stops one mistake from sailing through unnoticed.

## The steps

Diamonds are checks (their result decides whether the loop continues). Yellow are **human**
sign-offs. Curved arrows are loops with a limit (≤3, then it asks a human instead of spinning).

```mermaid
flowchart TD
    Start([Coding task + risk level]) --> S0
    S0["standards<br/>the project's coding rules"] --> S1
    S1["spec<br/>what to build + what changes"] --> S2
    S2["refine<br/>clear up anything vague"] --> S3
    S3["plan<br/>the technical design"] --> S4
    S4{"review plan<br/>(different agent)"}
    S4 -->|"issues · loop<3"| S3
    S4 -->|clean| G1
    G1{{"approve plan<br/>(human, risk≥2)"}}
    G1 -->|approve| S6
    S6["tasks<br/>small pieces, focused context"] --> S7
    S7["write tests<br/>(tester ≠ coder, tests first)"] --> S8
    S8["implement<br/>make the tests pass"] --> S9
    S9{"review code<br/>(fresh agent, tries to break it)"}
    S9 -->|"not safe · loop<3"| S8
    S9 -->|safe| S10
    S10{"security check<br/>(separate pass)"}
    S10 -->|"issues · loop<3"| S8
    S10 -->|clean| S11
    S11{"check tests<br/>(do they catch bugs?)"}
    S11 -->|"too weak"| S7
    S11 -->|strong| S12
    S12{"run all tests<br/>(nothing else broke)"}
    S12 -->|"broke · loop<3"| S8
    S12 -->|green| S13
    S13{"consistency check<br/>(rules + spec + tests + code agree)"}
    S13 -->|"drift"| S8
    S13 -->|agree| S14
    S14{"final gate<br/>FAILS CLOSED"}
    S14 -->|fail| S8
    S14 -->|pass| G2
    G2{{"approve<br/>(human; two people at top risk)"}}
    G2 -->|approve| Done([Mergeable change + receipts])

    classDef gate fill:#ffd700,stroke:#000,color:#000,stroke-width:2px
    classDef check fill:#ffcccc,stroke:#000,color:#000,stroke-width:2px
    classDef step fill:#e6f3ff,stroke:#000,color:#000
    classDef done fill:#d4edda,stroke:#000,color:#000
    class G1,G2 gate
    class S4,S9,S10,S11,S12,S13,S14 check
    class S0,S1,S2,S3,S6,S7,S8 step
    class Start,Done done
```

### Plain-text version

```
  standards  → the project's coding rules (from the repo + org rules)
  spec       → what to build + what's added/changed/removed
  refine     → clear up anything vague; clear, testable criteria
  plan       → the technical design
  review plan◇ (different agent) ──issues──► back to plan
  approve plan▣ (human, risk≥2)
  tasks      → small pieces, each with only the context it needs
  write tests→ tests first, by a different agent than the coder
  implement  → smallest change that makes the tests pass (can't touch the tests)
  review code◇ (fresh agent, tries to break it) ──not safe──► back to implement
  security check◇ (separate pass) ──issues──► back to implement
  check tests◇ (do the tests actually catch bugs?) ──too weak──► back to write tests
  run all tests◇ (nothing else broke) ──broke──► back to implement
  consistency check◇ (rules + spec + tests + code agree) ──drift──► back to implement
  final gate◇ FAILS CLOSED (missing evidence = fail) ──fail──► back to implement
  approve▣ (human; two people at top risk) → mergeable change + full receipts
```

## What's different here (not just a checklist)

| Idea | An advisory skill | **BOSS (enforced)** |
|---|---|---|
| Different agent writes vs. judges | *asks* the model to | a same-agent approval is **rejected** |
| Standards | a note in the prompt | a **`standards` record** every step is checked against |
| Final check | "looks good" | a **fail-closed gate** — missing evidence = fail |
| Test quality | "write good tests" | **check tests** breaks the code on purpose to prove tests catch it |
| Human sign-off | "pause and ask" | real **decisions**; top risk needs **two people** |
| Loops | a number in the prompt | bounded, then it **asks a human** — never spins on budget |
| Trail | markdown files | **receipts + decisions** in the durable store, replayable |
| Cost | none | **budget reserved** per step |
| Effort vs. risk | on/off | **scales by risk level** (tiny → top) |

The short version: an advisory skill is a *suggestion*; this is a *control*. The agent **can't**
skip the tester, approve its own code, or call it done without evidence — BOSS won't let it.

## Who does each step

Each step is run by a different agent with its own context. Most map to existing BOSS roles:
`standards`→cao · `spec`/`refine`→pm · `plan`→cao · `review plan`→design_critic ·
`approve plan`/`approve`→release_manager (human) · `tasks`→program_manager ·
`write tests`/`check tests`/`consistency check`→qa · `implement`→software_engineer ·
`review code`→adversarial_reviewer · `security check`→security_reviewer ·
`run all tests`→integration_validator · `final gate`→deterministic.

## How many steps run, by risk

| Risk | Example | Steps that run | Human sign-off | Approval |
|---|---|---|---|---|
| **tiny** | docs, a refactor already covered by tests | implement → final gate | — | none |
| **low** | a small internal change | + standards, spec, refine, plan, review plan, tasks, write tests | — | one |
| **standard** | something users see, real logic | + approve plan, review code, security check, check tests, run all tests, consistency check | plan + final | one |
| **high** | login, payments, secrets, migrations, deploys | all of it, stricter bars | plan + final | **two people** |

Risk jumps to **high** automatically when the change touches login, payments, secrets,
migrations, or deploys. The rule holds at every level: whoever writes something doesn't approve
it, the final gate fails closed, every step leaves a receipt, loops are bounded.

## Model-agnostic

The step descriptions are the *guidance*; any model can play each role. BOSS resolves the agent
through the model-agnostic seam (Claude, OpenAI, Azure, or a local model) — the recorded run
shows which model ran each step.

## Status

Defined and registered as the default (workflow `governed_coding_pipeline`, skills
`coding_pipeline.*`, policy `governed_coding_pipeline_policy`, object `Standards`). The runtime
today runs the core separation live (write tests → implement → review code → fail-closed gate),
proven on both Claude and OpenAI/Azure. Wiring the remaining steps (refine, plan review,
security, check tests, run all tests, consistency) into the live runner is the next increment.
See docs/product/GOVERNED_CODING_PIPELINE.md.

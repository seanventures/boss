# The Governed Coding Pipeline — BOSS's Default Pattern for All Coding Tasks

Status: Pattern defined in specpack; runtime wiring is the follow-on (see §6)
Date: 2026-06-19
Owner: param

## 1. Why this exists

Frontier models are commoditized — everyone has them. The bottleneck in agentic engineering
is no longer *generating* code; it is *establishing trust* in generated code. The failure mode
the whole field warns about is **correlated error**: when one agent (one context) writes the
code, writes the tests, and reviews the result, the tests and the review inherit the same
misunderstanding that produced the bug. As the Salesforce piece puts it, *"the tests inherit
whatever the agent misunderstood,"* and *"the reviewer faces a large, fully formed diff with
none of the shared context."*

BOSS's moat is therefore not "an orchestrator" (ADK/Azure/LangGraph all ship those). It is
**governed, durable, auditable agentic engineering**: quality made *structural and enforced*
rather than *prompted and hoped-for*. This pattern makes that concrete and the default for
every coding task.

> Note: in our 2026-06 dogfood, BOSS's real code path (`code.task` → single `ClaudeCodeAgent`)
> had one agent write the code *and* its tests *and* iterate to green — the exact anti-pattern.
> This pipeline fixes that by construction.

## 2. The spine — break correlation by role + context separation

Three roles must be distinct, with isolated context:

- **Test author ≠ implementer.** Tests are authored from the spec, *before* implementation, by
  a different role (`qa`). They encode intent, not the coder's mistakes.
- **Reviewer ≠ implementer.** A fresh-context `adversarial_reviewer` tries to *refute* the diff
  (correctness, security, reproduction) and defaults to "refuted" under uncertainty.
- **Gate ≠ prompt.** Standards become a fail-closed `QualityGate` object (missing evidence =
  fail), not instructions an agent forgets when context summarizes away.

## 3. The patterns it encodes → BOSS primitive

| Agentic pattern (research) | Encoded as |
|---|---|
| Author ≠ Judge / evaluator-optimizer | `test_authoring` + `adversarial_review` phases (separate roles) |
| Independent / spec-first tests | `coding_pipeline.author_tests` (policy: `author_must_not_be_implementer`) |
| Adversarial review | `adversarial_reviewer` role + `coding_pipeline.adversarial_review` (refute-first) |
| Quality gates over prompts | `QualityGate` object + `coding_pipeline.quality_gate` (`fail_closed`) |
| Grade the tests (mutation testing) | `coding_pipeline.mutation_grade` + `mutation_min` gate |
| Design around failure modes | `architecture_review` phase → `FailureModeCatalog` |
| Spec-as-contract | `spec_contract` phase → executable `AcceptanceCriteria` |
| Bounded execution / circuit breaker | `max_pipeline_iterations: 3` → escalate to a Decision |
| Human-in-the-loop at the right altitude | `merge_decision` (release_manager) + dual-control by tier |
| Trajectory logging / evidence | every stage emits a Receipt; artifacts_required on completion |
| Tool sandboxing | reuses the existing microvm/Docker sandbox |
| Scale rigor to stakes | `governed_coding_pipeline_policy` risk tiers (the key knob) |

## 4. The pipeline (workflow `governed_coding_pipeline`)

```
spec_contract(pm) → architecture_review(cao) → test_authoring(qa) →
implementation(software_engineer) → adversarial_review(adversarial_reviewer) →
mutation_grade(qa) → quality_gate(deterministic, fail-closed) → merge_decision(release_manager)
```
- `implementation` consumes the tests it did **not** write and must make them pass.
- `adversarial_review` runs in **isolated context**; failures route findings back to
  `implementation` (a bounded evaluator→optimizer loop, max 3 iterations, then a Decision).
- `quality_gate` is **fail-closed**: missing/unavailable evidence counts as fail.

## 5. Scaling rigor to risk (`governed_coding_pipeline_policy`)

Same tiers as the autonomy ladder, so it's not overkill for a throwaway script and full-panel
for production:

| Tier | Example | Required stages | Approval |
|---|---|---|---|
| 0 trivial | docs, pure refactor w/ existing tests | implement → quality_gate | none |
| 1 low | small internal slice | +author_tests | single |
| 2 standard | user-facing logic | +adversarial_review +mutation_grade | single |
| 3 high | auth / payments / secrets / migration / deploy | +security_scan, higher thresholds | **dual-control** |

Invariants hold at every tier: test author ≠ implementer, reviewer ≠ implementer, fail-closed
gate, every stage receipted, no merge without a passing QualityGate. Tier auto-escalates to 3
when changed paths touch auth/payment/secrets/migration/deploy.

## 6. Status & the follow-on (runtime wiring)

**Done (this change):** the pattern is fully expressed in the specpack and registered as a
**default** — new role (`adversarial_reviewer`), object (`QualityGate`), skill set
(`coding_pipeline.*`), policy (`governed_coding_pipeline_policy`), workflow
(`governed_coding_pipeline`), all added to `core_v1`/`core_v2`. Skills pass
`validate_skill_spec`; role passes `validate_role_spec`; pack installs resolve.

**Next (runtime):** `code_task_service` currently runs a single agent. To execute this pattern,
it should orchestrate the stages as separate agent invocations with **isolated context per
role**, persist a `QualityGate` row, and block the merge Decision until it passes. The
governance primitives it needs already exist (roles, receipts, decisions, dual-control on
ChangeSet, budget governor, the sandbox) — this is composition, not new infrastructure.

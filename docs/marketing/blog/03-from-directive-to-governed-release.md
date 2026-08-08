---
title: "From Directive to Governed Release"
description: "A practical walkthrough of how BOSS turns a software outcome into a reviewable, evidence-backed release."
audience: "Engineering leaders, QA, platform teams"
status: "launch-ready draft"
---

# From Directive to Governed Release

Most AI coding demos stop when a model returns code. That is precisely where
software delivery becomes risky: someone still has to make the work
reviewable, testable, attributable, deployable, and reversible.

BOSS is built for the rest of that journey. Give it a bounded outcome and it
creates the durable work around the generation: a design, role-separated work,
approval points, budget controls, verification evidence, and receipts for the
effects it is allowed to perform.

This walkthrough follows the operating shape used in the BOSS demo runbook:

```text
directive → design panel → approved spec → governed code task
          → independent verification → gated PR → preview/canary → receipts
```

The important claim is not that every arrow always succeeds. It is that every
arrow is visible, bounded, and backed by a record. A missing test, failed
review, unavailable provider, or unapproved production action must stop or
escalate the workflow rather than disappear behind a cheerful status message.

## 1. Start with an outcome, not an implementation prompt

Consider a directive such as:

> Add a thread-safe token-bucket rate limiter to our Java service. Make the
> rate and capacity configurable, provide a clock seam for deterministic tests,
> and keep it safe under concurrent requests.

That is useful intent, but it is not yet a safe implementation plan. It leaves
open questions about the public API, rejection behavior, time handling,
concurrency, acceptance criteria, and rollout risk.

In BOSS, that directive is a durable task with an owner, priority, risk tier,
and workflow context. A task is not merely a chat transcript: the durable work
record is what lets an operator see the status, recover a stalled run, and
trace the decisions and evidence that followed.

## 2. Use a panel to turn ambiguity into a reviewable design

The directive first goes to a role panel. The runbook uses Engineer/Architect,
QA, Security, and Product Management: each brings a deliberately different
failure mode to the same problem. The result is not a synthetic consensus;
attribution and dissent remain available for the operator to inspect.

![Agent Studio shows versioned agents, executable panels, reusable workflows, and the A2A audit surface.](./assets/agent-studio-console.svg)

The panel should produce a design with testable acceptance criteria, rather
than an unstructured explanation. For the rate-limiter example, it would make
the clock seam, error semantics, locking strategy, and JUnit test plan
explicit. At the relevant risk tier, the design is held for human approval
before implementation proceeds.

This is a practical distinction. The implementation agent reads the approved
document from the repository; it does not rely on a copied summary surviving
from one agent context to the next.

## 3. Select the amount of rigor before work begins

Not every change deserves the same execution cost, and a cost control must not
quietly downgrade a sensitive change. The Coding Pipeline presents explicit
profiles:

- **Lean** for low-risk prototypes: separate test author and builder.
- **Balanced** for ordinary changes: plan, tests, implementation, and
  independent review.
- **Strict** for production or high-risk work: stronger independent checks,
  security review, regression/mutation evidence, and tighter sign-off.

Risk-sensitive changes such as authentication, payments, secrets, migrations,
and deploys are not permitted to run below the required safety floor. Budget
reservations are made before spendy work; actual cost is recorded afterward.

![The Coding Pipeline makes the cost-versus-rigor choice visible and explains the independent verification path.](./assets/coding-pipeline-console.svg)

The pipeline is deliberately role-separated. A QA agent writes the tests
before the implementation agent changes code; a fresh reviewer and security
pass try to refute the resulting change. The final gate is fail-closed: it
opens only when the required evidence exists. The person or agent that wrote
the change does not grade its own work.

For the full flow and the exact role boundaries, see the
[Governed Coding Pipeline](../../product/GOVERNED_CODING_PIPELINE.md).

## 4. Execute the smallest reviewable code task

Once the design is approved, BOSS creates a scoped code task. The worker
clones the authorized repository into its execution environment, reads the
approved design, changes only the work in scope, and runs the declared test
commands. The task is shown as a sequence of observable phases rather than as
an opaque "agent working" spinner.

The completed-task view carries the useful engineering packet: the execution
engine and model, cost, phase history, change summary, touched-file manifest,
test command and exit result, and the PR reference when one was opened.

![A local seeded BOSS demo task displays the completed task, model and cost metadata, durable phase progression, and the change manifest.](./assets/code-task-evidence-console.svg)

The screenshot is a locally seeded demo record used to verify the console
surface; it is not presented as proof of a live external deployment. In a real
run, the corresponding task, test output, approval, and provider effects must
all be linked to the run's evidence and receipt chain.

## 5. Treat a pull request as a gate, not the finish line

A passing local test command does not authorize a merge or a deployment. The
governed pipeline collects the independent review, security result, mutation
or regression evidence where required, and the release decision. If evidence
is missing, the quality gate fails closed and the task returns for repair or
human escalation.

When a PR is permitted, it is the reviewable change packet—not an implicit
authorization to merge. The demo flow keeps an explicit merge decision, and
higher-risk work can require dual approval. This lets a team keep the speed of
automated implementation without converting a green model message into an
unguarded production write.

## 6. Deploy only through an approved, receipt-backed path

The last part of the loop is intentionally conditional. A preview, canary,
production deployment, rollback, DNS update, or provider write requires its
configured adapter, required approval, budget reservation, rollback plan, and
unique receipt/dedupe identity. BOSS records the result as evidence; if the
provider is unavailable or policy/configuration is incomplete, it reports a
visible blocked state rather than pretending the release occurred.

That matters during an incident as much as during a launch. An operator can
answer four concrete questions from the record:

| Question | Required evidence |
| --- | --- |
| What was requested? | directive, spec, task scope, and approvals |
| What changed? | PR/diff summary and execution artifacts |
| Why was it allowed? | policy, risk tier, budget reservation, and gate results |
| What happened externally? | receipt, provider/deployment status, canary result, and rollback reference |

## The operating model in one sentence

Generation is an input to delivery, not proof of delivery. BOSS turns a
directive into governed execution by keeping the design, work, verification,
approvals, costs, and effects in one auditable loop.

To rehearse the full operator sequence, including the two human gates and
pre-flight checks, use the repository's `BOSS_Demo_Runbook.md`.

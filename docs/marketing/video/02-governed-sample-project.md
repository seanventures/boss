---
title: "Video 02 — Execute a Governed Sample Project with BOSS"
audience: "Engineering leaders and developers evaluating governed code delivery"
duration_target: "12–15 minutes"
status: "recording kit — live PR segment gated on sandbox protection"
verified: "2026-08-08"
---

# Video 02 — Execute a Governed Sample Project with BOSS

This is the recording script and evidence checklist for a real, governed code
task. It uses the intentionally small, private
[`seanheiney/boss-sandbox`](https://github.com/seanheiney/boss-sandbox) target,
never a customer repository. The goal is to show the durable BOSS record around
a change: scope, approval, sandboxed implementation, independent tests, a
review packet, and the resulting receipt trail.

It is not permission to demonstrate ambient GitHub access. A live pull request
is allowed only after the target repository's protection and the BOSS delivery
connection have been explicitly verified. Until then, record the local,
read-only portions and describe the final delivery boundary truthfully.

## What the viewer should learn

1. A BOSS code task has explicit instructions, acceptance criteria, tests, and
   a target repository before an agent begins work.
2. The agent's work proceeds through visible phases instead of being treated as
   an unobservable chat response.
3. Passing tests are evidence for a review decision, not authority to merge.
4. Repository mutation remains behind a scoped connection, a human review gate,
   and a receipt/audit record.

## Current, verified sandbox facts

These were verified by read-only GitHub inspection on 2026-08-08:

- Target: private, real-only `seanheiney/boss-sandbox`, default branch `main`.
- The sample is a small Node project with `npm run lint` and `npm test`.
- The repository contains a pull-request CI workflow, but GitHub Actions are
  disabled for this project globally and `main` was not protected at the time
  of verification.

The missing protected-main policy is tracked separately in Plane. Do not work
around it with a direct push, an administrator bypass, or a claim that CI has
run. The recording may show a completed BOSS task only when its visible evidence
matches what actually ran in the chosen environment.

## Pre-flight checklist

Run these before recording. The local BOSS database must be disposable; the
sandbox repository must remain private and free of customer data.

```bash
make seed-demo
make dev

gh repo view seanheiney/boss-sandbox \
  --json nameWithOwner,isPrivate,defaultBranchRef,url
gh api repos/seanheiney/boss-sandbox/branches/main \
  --jq '{name, protected, commit: .commit.sha}'
```

- [ ] The local API, worker, and web console are running from `make dev`.
- [ ] The console is seeded with demo data using `make seed-demo`.
- [ ] The target is the private `seanheiney/boss-sandbox`, not a customer repo.
- [ ] The repository branch-protection result has been reviewed. If it reports
  `protected: false`, do **not** perform the live external PR segment.
- [ ] A BOSS GitHub connection and a model entitlement are configured only if
  an explicit, disposable live run was approved.
- [ ] No GitHub token, provider key, customer content, or full database URL is
  visible in the capture.
- [ ] The presenter has a rollback plan: close the demo PR and delete its
  branch only through the normal reviewed GitHub workflow.

## Recording script

### 0:00–1:15 — Establish the delivery boundary

Say:

> “BOSS does not make a pull request because a model sounded confident. It
> turns a scoped engineering request into a durable work item with tests,
> policy, review, and evidence. The code remains in the target repository; the
> BOSS record tells us what happened and why.”

Show the target repository overview and the local BOSS console. State that the
repository is a private demo target. Do not call it public, production-ready,
or protected unless the live inspection proves it.

### 1:15–3:00 — Show the sample project and its objective tests

Open the sample's `package.json`, `src/index.js`, and `tests/smoke.test.js`.
Show the exact commands BOSS will require:

```bash
npm run lint
npm test
```

Say:

> “The sample is intentionally small so the evidence is legible. We supply
> the test commands before the agent starts; BOSS re-runs them rather than
> accepting a self-reported success message.”

### 3:00–5:00 — Create a bounded code task

In **Code Tasks**, choose **New code task**. Use this small, deterministic
change; it is intended for the private sandbox only:

| Field | Value |
| --- | --- |
| Title | `Normalize sandbox greeting names` |
| Owner | `seanheiney` |
| Repo | `boss-sandbox` |
| Base branch | `main` |
| Test commands | `npm run lint` then `npm test` |

Use the following instructions:

```text
In src/index.js, add and export normalizeGreetingName(name). It must return
"BOSS sandbox" when the supplied value is missing, empty, or whitespace-only;
otherwise it returns the trimmed name. Refactor greet(name) to use that helper
without changing greet()'s existing default output. Add node:test coverage for
the default, whitespace, and trimmed-name cases. Keep the change limited to
this behavior and leave the existing smoke test passing.
```

Add these acceptance criteria, one per line:

```text
normalizeGreetingName is exported and handles missing, blank, and trimmed names
greet still returns "hello BOSS sandbox" with no argument
node:test coverage proves the new behavior
npm run lint and npm test pass
```

Before selecting **Start coding task**, pause on the fields and say:

> “This is the contract. The repository, branch, expected behavior, and test
> gate are visible before an agent receives authority to work.”

If the environment is not explicitly approved for a live run, stop here and
transition to the seeded/rehearsed evidence in the next section. Do not press
the button merely for a more dramatic recording.

### 5:00–8:00 — Watch the governed run (only with approved live configuration)

After starting an approved task, show the Code Tasks phase ribbon as it
actually advances. The expected labels are **cloning**, **coding**,
**testing**, and **opening PR**, but narrate only labels that are visible in the
running task.

While it works, say:

> “The task is leased to a worker and has a durable state. An interruption is
> not silently forgotten: it must retry, dead-letter, or become a decision.
> The agent can propose a change, but BOSS still re-runs the declared tests and
> keeps the delivery action governed.”

If a task fails, show the recorded failure and stop the segment. Do not replay a
different task while presenting it as the same run.

### 8:00–10:30 — Review evidence and the merge gate

Open the completed task card. Show, in this order:

1. The task state and phase history.
2. The changed-file and diff summary.
3. The exact test evidence, including failures or skipped checks if present.
4. The pull-request link only if BOSS actually opened it.
5. The review/merge decision and the associated receipt or audit entries.

Say:

> “A green test is evidence, not an automatic merge. The human gate decides
> whether this exact diff is acceptable. That decision is part of the same
> durable record as the task and its cost.”

Do not merge during the recording while branch protection remains unresolved.
When a protected-main policy and approved CI path are demonstrably configured,
the reviewer—not the agent—may mark the PR ready and merge it through the
normal repository workflow.

### 10:30–12:00 — Receipts, rollback, and close

Show the task's receipts/events and explain the distinction between a proposal,
an executed effect, and a human approval. Close with:

> “The feature is small on purpose. What scales is the operating contract:
> explicit scope, a leased worker, a budget and policy boundary, reproducible
> tests, human review, and an evidence trail. BOSS makes the work inspectable
> even when the agent is not.”

If a live PR was opened, state its number and the reviewer decision on screen.
If not, state that the recording showed the governed preparation path and that
external delivery stays blocked until its safety gate is satisfied.

## Evidence required before publishing the episode

- [ ] The recording visibly proves the target repo identity and privacy.
- [ ] The code-task form shows instructions, acceptance criteria, branch, and
  the two declared test commands before the task is started.
- [ ] Every claimed phase, test result, PR, decision, or receipt is visible in
  the same run's BOSS record.
- [ ] Any failed, skipped, or unavailable check is named accurately.
- [ ] A live PR segment has a verified protected-main policy and reviewer path;
  otherwise it is omitted and described as blocked.
- [ ] No credential, customer data, direct push, provider write, deployment, or
  unsupported production claim appears in the final cut.
- [ ] The final edit labels the sandbox as a private demonstration repository.

## Cleanup after an approved rehearsal

1. Review the task's decision and receipts before changing any external state.
2. Close an unmerged demonstration PR through the normal GitHub UI; do not
   force-push or bypass a review gate.
3. Remove the demo branch only when the repository owner approves it.
4. Reset only the disposable local BOSS database after preserving the evidence
   needed for the recording and its Plane issue.


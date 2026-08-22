# Why BugBox never merged, and why nothing said so

Investigated 2026-08-22 after BugBox was found deployed from a branch whose PR (#98) had never
merged. The specific answer is small. The general one is not: **20 of 30 merge decisions in this
project have expired unapproved.** Expiry is the majority outcome, not an edge case.

---

## 1. What happened to BugBox

```
dec_c5c65e5f5d894a6c860c
  created  2026-08-16
  deadline 2026-08-17 11:50 UTC   (24h)
  default_action  hold
  status   expired
```

The merge gate opened, nobody approved it inside 24 hours, and `expire_due_decisions` closed it
with the safe default: **hold**. PR #98 is still open. `main` is at PR #70.

Nothing malfunctioned. Every component did exactly what it was built to do — which is the
problem.

## 2. Why BOSS did not merge it

It was never asked to. `review_pull_request` is a **Tier-2 human gate**: an agent merging its own
build to production is the thing a governance product exists to prevent. Expiry applying `hold`
is the correct conservative outcome for an unanswered gate.

The defect is not that it held. It is that **holding is indistinguishable from being forgotten**.

## 3. Why nothing detected it

Three independent dead ends, each individually defensible:

**a. The expiry event is written and never read.**
`decisions.expire_due_decisions` emits `decision.expired` with `safe_default_applied`. Nothing
consumes that event — the only other reference in the codebase is an assertion in a test. An
event nobody subscribes to is a log line, not a signal.

**b. The Task Shepherd does not watch decisions.**
This is the direct answer to "why didn't the shepherd tell BOSS to do the job". The shepherd
classifies *code task run* failures — `shepherd_watch` reads runs, not `decisions`. An expired
decision is not a failed run: the run **succeeded**. It built the app, opened a green PR, and
finished. Nothing about that row is wrong.

Class 5 `APPROVAL_REQUIRED` exists, but it is matched from error text like
`"not allowed to approve"` — a permission failure *during* a run. It has never meant "an
approval was requested and then nobody came".

**c. An expired decision is terminal.**
There is no transition out of `expired`. The work is finished, reviewed and mergeable; the only
way forward is to open a new task and rebuild what already exists.

## 4. The scale

```sql
SELECT status, count(*) FROM decisions
WHERE recommendation->>'action' = 'review_pull_request' GROUP BY status;
```

| status | count |
|---|---|
| **expired** | **20** |
| approved | 8 |
| executed | 2 |

Two thirds of everything BOSS has built and opened a PR for was silently abandoned at the gate.
Each of those is a paid-for code task, a green PR, and a branch nobody merged. This is the single
largest source of wasted work in the pipeline, and until now nothing measured it.

## 5. What to build

Three changes, smallest first. None of them lets an agent merge its own work — the gate stays.

**F8 — expiry becomes a shepherd class (½ d).**
`MERGE_GATE_EXPIRED` (class 17). Its remedy is not a retry: the build is fine. It is
`reopen_decision` — return the gate to `pending` with a fresh deadline and escalate once, since
the operator plainly did not see it the first time. Ships in `shadow` like 13–16 so it observes
before it acts.

**F9 — expiry is reversible (½ d).**
Allow `expired -> pending` via an explicit `reopen` transition, recorded with who reopened it and
why. Today the only path back is rebuilding work that already exists, which is how BugBox came to
be deployed from an unmerged branch in the first place.

**F10 — the gate is visible where the work is (½ d).**
An app card whose merge decision is pending or expired should say so, next to Approve & ship.
A gate an operator has to go to a different area to discover is a gate that expires.

## 6. What NOT to do

- **Do not auto-merge on expiry.** `default_action=hold` is right. The fix is making the gate
  impossible to miss, not removing it.
- **Do not lengthen the deadline and call it fixed.** A 72-hour gate nobody sees expires in 72
  hours instead of 24.
- **Do not have the shepherd re-run the task.** The run succeeded; re-running it pays twice for
  the same code and opens a second PR against the same branch.

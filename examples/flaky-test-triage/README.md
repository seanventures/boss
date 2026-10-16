# Flaky test triage

**Paste a failing test. An agent says whether it is flaky or real; a flaky test becomes a
ticket, a real failure is escalated — each after you approve.**

---

## What it does

A red test is either telling you something or wasting your time, and finding out which means
reading the test, the code, the history of both and the last few CI runs. This workflow has an
agent do that reading and classify the failure, then routes it to the right effect.

| # | Step | Agent | What it does |
|---|------|-------|--------------|
| 1 | **diagnose** | `engineering_captain` | Reads the test, the code, git history and CI. Declares `classification`, `summary`, `actions` |
| 2 | **route** | — | A `condition` on `classification`. Flaky → step 3. Anything else → step 4 |
| 3 | **File the ticket** | `program_manager` | One ticket to quarantine or fix the test. **Stops for you** |
| 4 | **Escalate to the owner** | `engineering_captain` | Emails the diagnosis. **Stops for you** |

The brief has one rule: **no invented cause.** A classification needs evidence — the same test
passing and failing on the same commit, or a change that explains the failure. Without it the
agent says `unknown`, and `unknown` goes to a person.

---

## What you need

**A connected model.** The diagnosis uses one.

**A bound repository.** The agent reads it through `repo.read` and `ci.status`.

**A work tracker** for step 3 — Plane, Jira, Linear, GitHub Issues or Azure DevOps, connected
in **Skills & MCP → File tickets**. **Email configured** for step 4. A run with only one of the
two still installs; `Start` says which path cannot run yet.

---

## Run it

1. Set `BOSS_API_KEY` as described in [examples/README.md](../README.md).
2. Install the workflow. Start with `--dry-run`; re-running is safe.

```bash
python3 examples/flaky-test-triage/setup.py \
    --api  http://127.0.0.1:8000 \
    --project YOUR_PROJECT \
    --actor   ceo
```

3. Open **Workflows → Library**, find "Flaky test triage", then **⋯ → Start** and fill in:
   - `test` — the test's name and its failure output, pasted
   - `to` — who is emailed when the failure is real or unclear
   - `repository` — optional: `owner/name`, when more than one is bound

---

## What you'll review

One of two steps stops. For a flaky test, the ticket as a sentence:

```
Tickets to create (1):
  · Quarantine tests/api/test_webhooks.py::test_retry_order — passes and fails on 3f2a1c
    without a code change; the order of two deliveries depends on worker scheduling
```

For a real or unknown failure, the email:

```
Classification: real
tests/api/test_webhooks.py::test_retry_order started failing at 9d81e4 (#422), which changed
the retry backoff from fixed to exponential; the test asserts the old 1s spacing. Next step:
update the assertion or the backoff, whichever was intended.
```

---

## How it stays safe

The diagnosis reads only (**tier 0** tools). `tracker.issues.create` is **tier 2** — the
approval floor — and `email.send_real_guarded` requires an approval in its own policy.
**Nothing is filed or sent until a person approves it.** The path not taken is recorded on the
run as skipped, with the reason.

---

## Build it by hand

1. **Enable the skills.** **Skills → Enable:** `repo.read`, `ci.status`, `tracker.issues.create`,
   `email.send_real_guarded`.
2. **Give the agents their tools.** `engineering_captain`: `repo.read`, `ci.status`,
   `email.send_real_guarded`. `program_manager`: `tracker.issues.create`.
3. **Define two steps.** "File the ticket" on `tracker.issues.create` (input `actions`); "Escalate
   to the owner" on `email.send_real_guarded` (`to`, `body`, `subject` optional). Both show
   **always needs a person**.
4. **Create the workflow.** **Agent task** `diagnose` (agent `engineering_captain`, instructions
   asked at start as `diagnose_brief`, context `test` and `repository`, producing
   `classification`, `summary`, `actions`) → **Branch** `route` on
   `steps.diagnose.output.classification` **equals** `flaky` → `file` (`actions` from
   `diagnose → actions`) or `escalate` (`body` from `diagnose → summary`). Both approvals manual.

---

## Troubleshooting

| What you see | What it means |
|---|---|
| *diagnose is blocked* | The agent could not read the repository or CI; open the step for what it needed |
| *this project has no connected work tracker* | Connect one in Skills & MCP → File tickets |
| `classification: unknown` | Not enough evidence either way. The email says what would settle it |

---

## Customize

Start a run from CI instead of by hand: create a **webhook trigger** on the workflow in the
designer and have the CI job post `{"test": "...", "to": "..."}` with the signed headers (see
[Starting a workflow from a webhook](../../docs/community/WORKFLOW_WEBHOOKS.md)). The first step
is the same; only who presses Start changes.

# Security finding triage

**Paste one finding. The security reviewer reads the code it points at and decides whether it
is real; a true positive becomes a ticket, anything else a note to the owner — after you
approve.**

---

## What it does

Scanners and bug-bounty inboxes produce findings faster than anyone confirms them, and an
unconfirmed finding is either a risk nobody owns or a false alarm somebody will spend a day on.
This workflow has the security reviewer agent read the code behind one finding and give a
verdict with evidence, then routes the verdict to the right effect.

| # | Step | Agent | What it does |
|---|------|-------|--------------|
| 1 | **judge** | `security_reviewer` | Reads the code, how it is reached, and what is validated. Declares `verdict`, `severity`, `summary`, `actions` |
| 2 | **route** | — | A `condition` on `verdict`. True positive → step 3. Anything else → step 4 |
| 3 | **File the finding** | `program_manager` | One ticket, severity first in the title, evidence in the body. **Stops for you** |
| 4 | **Tell the owner** | `security_reviewer` | Emails the verdict and the reasoning. **Stops for you** |

The brief's rule: **never report a vulnerability you did not confirm in the code.** When the
repository alone cannot settle it, the verdict is `needs_info`, which goes to a person with the
question that would settle it.

**One finding per run**, so each verdict is approved on its own.

---

## What you need

**A connected model.** The judgement uses one.

**A bound repository.** The agent reads it through `repo.read` (read-only).

**A work tracker** for step 3 and **email configured** for step 4. With only one, the workflow
installs and `Start` says which path cannot run yet.

---

## Run it

1. Set `BOSS_API_KEY` as described in [examples/README.md](../README.md).
2. Install the workflow. Start with `--dry-run`; re-running is safe.

```bash
python3 examples/security-finding-triage/setup.py \
    --api  http://127.0.0.1:8000 \
    --project YOUR_PROJECT \
    --actor   ceo
```

3. Open **Workflows → Library**, find "Security finding triage", then **⋯ → Start** and fill in:
   - `finding` — one finding, pasted, with the file it points at
   - `to` — who is told the verdict
   - `repository` — optional: `owner/name`, when more than one is bound

---

## What you'll review

For a true positive, the ticket as a sentence:

```
Tickets to create (1):
  · [high] SQL built from the `sort` query parameter in api/reports.py:88
    — reachable from GET /reports without authentication; parameterise the ORDER BY
```

Otherwise, the email:

```
Verdict: false_positive (severity: none)
The scanner flagged string formatting in api/reports.py:88, but `sort` is checked against
an allow-list at line 81 before it is used, and the route requires an admin session.
No change recommended.
```

---

## How it stays safe

The judgement reads only (**tier 0** tools). `tracker.issues.create` is **tier 2** — the
approval floor — and `email.send_real_guarded` requires an approval in its own policy.
**Nothing is filed or sent until a person approves it.** The untaken path is recorded on the
run as skipped, with the reason.

---

## Build it by hand

1. **Enable the skills.** `repo.read`, `tracker.issues.create`, `email.send_real_guarded`.
2. **Give the agents their tools.** `security_reviewer`: `repo.read`, `email.send_real_guarded`.
   `program_manager`: `tracker.issues.create`.
3. **Define two steps.** "File the finding" on `tracker.issues.create` (input `actions`); "Tell
   the owner" on `email.send_real_guarded` (`to`, `body`, `subject` optional).
4. **Create the workflow.** **Agent task** `judge` (agent `security_reviewer`, instructions
   asked at start as `judge_brief`, context `finding` and `repository`, producing `verdict`,
   `severity`, `summary`, `actions`) → **Branch** `route` on `steps.judge.output.verdict`
   **equals** `true_positive` → `file` (`actions` from `judge → actions`) or `tell` (`body`
   from `judge → summary`). Both approvals manual.

---

## Troubleshooting

| What you see | What it means |
|---|---|
| *judge is blocked* | The agent could not read the file the finding names; check the path and the bound repository |
| `verdict: needs_info` | The code alone did not settle it. The email says what would |
| *no connected work tracker* | Connect one in Skills & MCP → File tickets |

---

## Customize

Feed it from a scanner by creating a **webhook trigger** and posting each finding as
`{"finding": "...", "to": "..."}` — one delivery per finding, one run per finding. To file every
verdict as a ticket (false positives included, for the audit trail), point `on_false` at `file`
too and let the agent write a one-item `actions` list in every case.

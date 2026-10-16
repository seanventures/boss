# Dependency upgrade PR

**Name a package. An agent assesses the upgrade, a person opens the gate, BOSS's governed build
opens the pull request, CI runs, and the owner is told. Nothing is merged.**

---

## What it does

Dependency upgrades are routine until one is not. The work is mostly reading — where the
package is used, what changed between versions, whether CI is green to begin with — followed by
a small, exact change. This workflow has an agent do the reading and write the plan, puts a
person in front of the change, and lets the governed build make it.

| # | Step | Agent | What it does |
|---|------|-------|--------------|
| 1 | **assess** | `engineering_captain` | Reads the manifest, lockfile, usages and CI. Declares `verdict` (upgrade or skip), `plan`, `title` |
| 2 | **route** | — | A `condition` on `verdict`. Skip → step 3. Upgrade → step 4 |
| 3 | **tell_skip** | `engineering_captain` | Emails the owner why nothing was done. **Stops for you** |
| 4 | **gate** | *a person* | "Open a pull request for this upgrade? Read the plan first." **Stops for you** |
| 5 | **build** | `engineering_captain` | BOSS's governed build: a branch and a pull request from the plan |
| 6 | **ci** | `release_manager` | Waits for the PR's CI checks |
| 7 | **tell** | `engineering_captain` | Emails the owner the PR link and the CI result. **Stops for you** |
| 7′ | **tell_failed** | `engineering_captain` | Taken instead of 6 and 7 if the build fails (`on_error`). **Stops for you** |

Three shapes worth copying:

- **A branch on a value the agent declared.** `verdict` is one of the things the agent is asked
  to decide; the condition reads it from the step's output.
- **A gate before the write.** The build is risk tier 1, so BOSS would run it unattended. It
  writes a branch and a PR to GitHub, which is an effect outside BOSS — so the workflow puts a
  person in front of it with the plan to read.
- **An error path.** A build that fails goes to `tell_failed` rather than stopping the run with
  nobody told. The run records `workflow.error_path_taken` with the reason.

**The merge is yours.** This workflow opens the PR and reports on CI. It never merges.

---

## What you need

**A connected model.** The assessment and the build use one.

**A bound repository**, with **Open pull requests** set up in **Skills & MCP**. The build creates
a branch and a PR there; the assessment reads it through `repo.read` and `ci.status`.

**Email configured.** Every `tell` step sends through the project's SMTP settings.

A run also asks for consent to the tools it will use (the build's code task) when it starts.

---

## Run it

1. Set `BOSS_API_KEY` as described in [examples/README.md](../README.md).
2. Install the workflow. Start with `--dry-run`; re-running is safe.

```bash
python3 examples/dependency-upgrade-pr/setup.py \
    --api  http://127.0.0.1:8000 \
    --project YOUR_PROJECT \
    --actor   ceo
```

3. Open **Workflows → Library**, find "Dependency upgrade PR", then **⋯ → Start** and fill in:
   - `package` — as named in the manifest: `requests`
   - `target_version` — optional; blank means the latest release
   - `to` — who is told the outcome
   - `repository` — optional: `owner/name`, when more than one is bound

---

## What you'll review

**At the gate**, the agent's plan:

```
verdict: upgrade
title:   Upgrade requests to 2.32.4
plan:
  - pyproject.toml: requests>=2.31,<3 → requests>=2.32.4,<3
  - uv lock
  - run: pytest tests/http
  - 2.32.x changes the default for `verify` on redirects; tests/http/test_client.py::test_redirect covers it
```

Reject if the version is wrong, the plan touches files it should not, or CI is already red.
Approve, and the build runs. **At the end**, the email to the owner: the PR link as the subject,
the CI summary as the body.

---

## How it stays safe

The assessment reads only (**tier 0** tools). The build is **tier 1** and is behind a
**gate** a person answers; the branch and PR it writes are recorded as receipts with dedupe
keys. CI waiting is tier 0. Every email is **tier 2** and stops for a person.
**Nothing touches GitHub until a person approves the gate; nothing is sent until a person
approves the email; nothing is merged by this workflow at all.**

---

## Build it by hand

1. **Enable the skills.** **Skills → Enable:** `repo.read`, `ci.status`, `email.send_real_guarded`.
2. **Give the agent its tools.** **Agents → engineering_captain → Skills:** `repo.read`,
   `ci.status`, `email.send_real_guarded`.
3. **Define one step.** **Workflows → Library → Steps → + New step:** "Tell the owner" on
   `email.send_real_guarded` with inputs `to`, `body`, `subject` (optional). It shows **always
   needs a person**.
4. **Create the workflow.** From the palette:
   - **Agent task** `assess`, agent `engineering_captain`, instructions asked at start as
     `assess_brief` (default in `setup.py`), context `package`, `target_version`, `repository`,
     producing `verdict`, `plan`, `title`;
   - **Branch** `route`: `steps.assess.output.verdict` **equals** `upgrade` → `gate`, otherwise
     `tell_skip`;
   - "Tell the owner" as `tell_skip`: `to` from input, `subject` a literal, `body` from
     `assess → plan`, approval manual;
   - **Approval gate** `gate` with the question above, then `build`;
   - the built-in **Build** step: `title` from `assess → title`, `instructions` from
     `assess → plan`, `repository` from input; `max_attempts` 1; **on error** → `tell_failed`;
   - the built-in **Wait for CI** step `ci`: `pull_request_url` from `build → pull_request_url`;
   - "Tell the owner" as `tell`: `subject` from `build → pull_request_url`, `body` from
     `ci → ci_summary`, approval manual;
   - "Tell the owner" as `tell_failed`: `body` from `assess → plan`, approval manual.

---

## Troubleshooting

| What you see | What it means |
|---|---|
| *assess is blocked: cannot read the repository* | Bind a repository, or pass `repository` |
| *Needs Open pull requests set up* on save | Connect GitHub with PR access in Skills & MCP |
| The run went to `tell_failed` | Open the build step: its reason is on the run. The email carries the plan, not the reason |
| `verdict` was `skip` and you disagree | Start again with `assess_brief` edited, or a `target_version` |

---

## Customize

Make the gate stricter by editing its `guidance`. To retry a red CI once with a fix, wrap
`build` and `ci` in a **Repeat** (`bounded_loop`, `max_iterations` 2) on
`steps.ci.output.ci_status` not equal to `success` — it is a real pattern, and a spendy one,
which is why this example does not ship it on.

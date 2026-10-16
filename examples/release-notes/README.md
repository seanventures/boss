# Release notes

**Name a tag. An agent reads what was merged since, two drafts are written at once, and the
combined notes reach a reviewer after you approve.**

---

## What it does

Release notes are two documents pretending to be one: what customers can now do, and what the
team shipped, broke and should watch. Writing them from the merge log is mechanical; deciding
what a customer cares about is not. This workflow separates the reading from the writing, and
writes both versions in parallel.

| # | Step | Agent | What it does |
|---|------|-------|--------------|
| 1 | **collect** | `release_manager` | Reads merged pull requests since `since` with the repository tools. Declares `changes` and `highlights` |
| 2a | **Write the customer notes** | `docs_writer` | From the change list: what changed for a customer |
| 2b | **Write the internal notes** | `docs_writer` | From the same list: every PR, risks, follow-ups |
| 3 | **Combine the drafts** | `docs_writer` | One document, customer section first |
| 4 | **Email the notes** | `docs_writer` | Sends to the reviewer. **Stops and waits for you** |

2a and 2b run **at the same time**: a `parallel` step starts both and a `join` waits for both
before the combine step runs. Open the run to watch two step runs progress side by side.

The collection brief has one rule: **no invented PRs.** If the repository cannot be read or
`since` cannot be found, the agent declares the step blocked and the run stops there, visibly,
rather than making up a changelog.

---

## What you need

**A connected model.** Steps 1 to 3 use one.

**A bound repository.** Step 1 reads it through `repo.read` — the engine's own read tools (git
history, pull requests, files), scoped to the repositories bound to the project in **Settings →
Repo and models**. Without one, the agent reports it cannot read the repository.

**Email configured.** Step 4 sends through the project's SMTP settings.

---

## Run it

1. Set `BOSS_API_KEY` as described in [examples/README.md](../README.md).
2. Install the workflow. Start with `--dry-run`; re-running is safe.

```bash
python3 examples/release-notes/setup.py \
    --api  http://127.0.0.1:8000 \
    --project YOUR_PROJECT \
    --actor   ceo
```

3. Open **Workflows → Library**, find "Release notes", then **⋯ → Start** and fill in:
   - `since` — a tag, a date or a commit: `v1.4.0`
   - `to` — who reviews the notes
   - `repository` — optional: `owner/name`, when more than one repository is bound
   - `subject` — optional

---

## What you'll review

The last step stops. Open it to see the combined document:

```
## Customer notes
### New
- Export invoices as CSV from the Billing page (#412)
### Fixed
- Dates in the activity feed now follow your locale (#418)
### Breaking
- The v1 webhook payload is removed; switch to v2 before upgrading (#405)

## Internal notes
- #412 feat: CSV export (A. Lee) — large exports stream; watch memory on the worker
- #418 fix: locale dates (M. Osei)
...
```

Approve, and the reviewer gets it by email. The notes are **not published anywhere** by this
workflow; the reviewer pastes them where releases are announced.

---

## How it stays safe

Steps 1 to 3 read and write text: **tier 0**. Step 4 is **tier 2** and its own policy requires
an approval, so it stops for a person whatever the workflow asks for. **Nothing is sent until a
person approves it.** The agent's repository access is read-only.

---

## Build it by hand

The script only calls the public API; every step below does the same thing in the console.

1. **Enable the skills.** **Skills → Enable:** `repo.read`, `docs.draft`, `model.generate_text`,
   `email.send_real_guarded`.

2. **Give the collector its tools.** **Agents → release_manager → Skills:** `repo.read`.
   (Every agent holds it by default; adding it again is harmless and makes the dependency
   visible.) Give `docs_writer` the other three.

3. **Define four steps.** **Workflows → Library → Steps → + New step.**

   | Name | Skill | Inputs |
   |---|---|---|
   | Write the customer notes | `docs.draft` | `brief`, `context` (optional), `audience` (optional) |
   | Write the internal notes | `docs.draft` | the same |
   | Combine the drafts | `model.generate_text` | `prompt`, `context` (optional), `system` (optional) |
   | Email the notes | `email.send_real_guarded` | `to`, `body`, `subject` (optional) |

4. **Create the workflow.** **+ New workflow**, then from the palette:
   - an **Agent task** step `collect`, agent `release_manager`, instructions asked at start as
     `collect_brief` (default: the brief in `setup.py`), context `since` and `repository`,
     producing `changes` and `highlights`;
   - a **Do at the same time** step `fork` with branches `customer` and `internal`, joined at `join`;
   - the two draft steps, each with `next` = `join`, `brief` a literal, `context` from
     `collect → changes`;
   - a **Wait for branches** step `join` waiting for both, then `combine`;
   - `combine`: `system` the combine brief, `prompt` from `customer → markdown`, `context` from
     `internal → markdown`;
   - `send`: `to` and `subject` from the run's input, `body` from `combine → text`, approval
     **manual**.

---

## Troubleshooting

| What you see | What it means |
|---|---|
| *collect is blocked: cannot read the repository* | Bind a repository in Settings → Repo and models, or pass `repository` |
| *`since` not found* | Use a tag or commit that exists on the default branch |
| *not ready yet: email* | Configure SMTP in Settings |
| The join never completes | One branch failed; open it. The join waits for both (`success_policy: all`) |

---

## Customize

Change the audience of either draft by editing the step's `audience`. Add a third branch — a
changelog entry in Keep-a-Changelog format, say — by adding a step to `branches` and to the
join's `wait_for`. To post the notes to a channel instead of email, replace the last step with
`slack.post_real_guarded`; it stops for a person too.

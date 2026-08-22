# PRD — Platform Resiliency: stop losing operator hours to stuck work

**Status:** draft for review
**Date:** 2026-08-22
**Author:** written from a single session in which an operator spent ~8 hours trying to build one
small URL-shortener app and never got it built.

---

## 1. Why this exists

On 2026-08-22 an operator set out to build a self-hosted URL shortener — the smallest realistic
end-to-end exercise of BOSS: Discovery → PRD panel → code task → preview → deploy. It did not
finish. Not because any single component is broken beyond repair, but because **the path had
never been run end to end**, and every stage failed the first time it was executed for real.

Twelve distinct defects surfaced. Individually each looks like a small bug. Collectively they
share five root-cause shapes, and those shapes are the actual product problem:

> Work stops, and nothing notices. The console keeps rendering the last thing it was told.
> An operator is the only monitor the system has.

This document records every incident with evidence, names the shapes, states what has been
fixed at the platform layer, and proposes the work still required.

**The bar this proposes:** an operator should never be the first to notice that work is stuck.

---

## 2. Incident log

Each row: what the operator saw, what was actually true, and the defect underneath.

### I1 — Host SSH keys destroyed on every deploy
* **Saw:** `secret 'ssh_key' is bound but unusable: BOSS_DH_… does not exist`
* **Cause:** `FileBackend`/`LocalVaultBackend` write under `~/.config/boss/secrets`, which in a
  container is the container's own disk. Every redeploy destroyed BOSS-generated host keys while
  `deployment_hosts.key_secret_ref` still pointed at them.
* **Fix:** `DatabaseBackend` (AES-256-GCM in Postgres); default backend `file` → `database`.
* **Status:** shipped, verified surviving a container replacement.

### I2 — Buttons acted on the wrong run
* **Saw:** `BOSS API request failed: 409` on "Run preview"; "no reviewed build to deploy"; a card
  badged SHIPPED whose spine read "Build blocked".
* **Cause:** the card enabled actions from an APP-level predicate (`anyShipped` — does *any*
  attempt have something to show) and fired them at `members[0]`, the NEWEST attempt. For an app
  whose latest run was a blocked follow-up, that previewed the blocked run.
* **Fix:** `pickBuiltRun()`; artifact consumers take the built run, work-creators keep the newest.
* **Status:** shipped, with a wiring guard test (a unit test on the selector cannot catch a caller
  that never uses it).

### I3 — API "unreachable" while perfectly healthy
* **Saw:** `API returned 404` / "API unreachable — start the control plane".
* **Cause:** first query after idle took **28.9s** (cold Postgres cache). `def` endpoints run in a
  bounded threadpool; a few such queries saturate it, `/health` can no longer be *scheduled*,
  Docker marks the container unhealthy, Traefik evicts it. It recovers when something warms it,
  which is why it looked intermittent.
* **Fix:** warm the hot read paths at boot in a daemon thread. First request 28.9s → **0.32s**.
* **Status:** shipped. Does not make the cold query fast — see R2.

### I4 — Console ships 11.4 MB per page
* **Saw:** pages taking 18–100s; "API unreachable" caused by the console's own fetch timing out.
* **Cause (two layers):**
  1. `finalize_ctx.pending_diff` — the verifier ratchet's scratch space, `{path, content}` for
     every file the agent wrote — was serialized to the browser. Eight complete
     `package-lock.json` files, ~139–176 KB each.
  2. Naming eight fields was not enough: bulk was spread across `ratchet.files[].content` (19%),
     `spec.instructions` (18%), `state.design.plan` (11%), `state.design.spec` (10%) and four more.
* **Fix:** a rule about SIZE, not names — a list row is bounded at 500 chars/string, calibrated
  against the live response. API 3.86 MB → **1.47 MB**; page 11.4 MB → **7.6 MB**.
* **Status:** partially fixed. The remaining 7.6 MB is R1.

### I5 — Duplicate meeting panels
* **Saw:** two identical PRD panels from one Discovery outcome; a coding task apparently waiting
  on the duplicate.
* **Cause:** `dedupe_key=make_id(...)` at **seven call sites**. `tasks.dedupe_key` is checked
  before every INSERT, project-scoped and fail-closed — and a freshly generated id can never
  match, so the check did nothing, seven times over.
* **Why it recurred:** each occurrence was fixed where it surfaced; the other six kept producing
  duplicates.
* **Fix:** `stable_dedupe_key(kind, *identity)` at all seven sites; promote returns the existing
  meeting rather than raising; **a test that fails on the anti-pattern anywhere**, including in
  code not yet written.
* **Status:** shipped.

### I6 — Priority ranking was inverted
* **Saw:** a PRD meeting queued behind eleven code tasks for over two hours.
* **Cause:** `code.task` = 46, `meeting.step` = 48. `claim_job` orders by `priority ASC`, so
  **lower runs sooner** — batch work outranked the person. Invisible until something produced
  batch work in bulk. Numbers were invented per call site (40/45/46/48/80) with no shared table.
* **Fix:** one named table — `URGENT 10 / INTERACTIVE 20 / REQUESTED 46 / BACKGROUND 90 / BULK 95`
  — ranked by *who is waiting*; provenance decides, not kind. A sweep test fails on any bare
  literal below INTERACTIVE.
* **Status:** shipped.

### I7 — A second priority bypass
* **Saw:** benchmark jobs holding both worker slots at `priority_score=9000`.
* **Cause:** `metatron._reprioritize()` set `priority=5, priority_score=9000` for every QA
  benchmark. `claim_job` sorts by `priority_score DESC` **first**, so that outranked every band at
  once, permanently, past any later fix.
* **Fix:** boost within the batch band; never touch `priority_score`. QA benchmarks enqueue at
  `BULK`.
* **Status:** shipped.

### I8 — Runs that claim to be working forever
* **Saw:** 19 cards reading "Building" while exactly 2 jobs existed; "PANEL SITTING" two hours
  after the job was gone.
* **Cause:** a run's execution IS its job. When the job is cancelled, deadlettered or lost with the
  process, nothing can advance the run — but the row still said `running`, and the UI rendered it
  faithfully. Ten sat at `phase=queued`; **eight at `phase=done`** had finished every stage and
  were never marked complete.
* **Fix:** `sweep_abandoned_runs` over `code_task_runs`/`meeting_runs`/`discovery_runs`, with the
  terminal state derived from the phase (`done` → completed, else cancelled). 19 → 2, and the two
  remaining had live jobs.
* **Status:** shipped.

### I9 — Previews stuck "Starting…" forever
* **Saw:** five consecutive preview failures rendered as a pending spinner.
* **Cause (two):** no `failed` branch in the UI; and previews are **not job-driven** (built inline),
  so the no-live-job rule could never cover them.
* **Fix:** a `failed` branch showing the reason and a retry; plus `sweep_stale_previews`, a
  time-based rule (25 min at `starting` → failed, `failure_kind=never_started`).
* **Status:** shipped.

### I10 — Spinners with no end
* **Saw:** "Convening the panel…" forever; "Starting preview…" forever.
* **Cause:** a boolean set true on submit that nothing ever reset; and — underneath both — the API
  client had **no request timeout**, so a slow call never returned and `pending` never cleared.
* **Fix:** `useFormStatus` (derived from the form's lifecycle, clears however the action ends), and
  a 120s deadline on every request in the shared client.
* **Status:** shipped.

### I11 — Unreadable titles
* **Saw:** `Web app — # Product Requirements Document (PRD) **Project:** Lightweight Self-`; four
  different benchmark apps all displaying as "QA benchmark".
* **Cause:** `title = ("Web app — " + directive[:70])` — truncating prose straight through the
  project's own name. The PRD names it on the next line: **"Shorty"**.
* **Fix:** `naming.project_name()` reads what the document calls itself, strips wrappers, lifts
  run-kind into a badge. Applied at the source and mirrored in the UI for existing rows.
* **Status:** shipped.

### I12 — A wedged run nobody noticed
* **Saw:** "Running tests — 53m 15s", 0 files changed.
* **Cause:** the run made **one** LLM call and stopped. The sandbox command timeout is 120
  *seconds*, so nothing was executing. The wedge detector used ELAPSED TIME (2h) and would never
  have fired.
* **Fix:** `NO_PROGRESS_MINUTES = 25` — wedged means nothing has happened since, not that time has
  passed. A long run still calling models is working.
* **Status:** shipped.

---

## 3. The five shapes

The incidents are twelve; the shapes are five. Fixing shapes is what stops recurrence.

| # | Shape | Incidents |
|---|---|---|
| **S1** | **A state with no exit.** A pending flag, a spinner, a `running` row — set on the way in, with no path back on failure. | I8, I9, I10, I12 |
| **S2** | **A mechanism neutered at its call sites.** The check exists, is correct, and every caller passes a value that defeats it. | I5 (7×), I6, I7 |
| **S3** | **Fix one path, miss its twin.** The same defect in 2–8 places; fixed where it surfaced. | I2 (3×), I5 (7×), I8, I9 |
| **S4** | **Only failure is watched.** Every sensor is error-triggered. Starvation and stalling produce no error, so nothing sees them. | I6, I8, I12 |
| **S5** | **Internals leak to the operator.** Scratch space, store keys, raw prose, snake_case identifiers rendered as if they were product. | I1, I4, I11 |

**The counter-measure to each is the same: a test that rejects the SHAPE, not the instance.**
Three such tests now exist (dedupe anti-pattern, bare priority literals, artifact-consumer
wiring). Each one found violations the moment it was written — which is the point.

---

## 4. What the Shepherd was, and why it did nothing

The Shepherd had a complete failure taxonomy, a remedy ledger, measured cost estimates and an
operator-confirmation gate. It never fixed anything, for one reason:

```
callers of shepherd_service.act():   NONE
```

`/shepherd/watch` and `/shepherd/sweep` only ever **wrote proposals**. The deciding half was
built; the acting half had no caller anywhere in the codebase. Of six declared remedy actions,
the executor implemented **two**; the rest fell through to `no_action`.

**Now:** `autopilot()` proposes and acts on a 180s worker tick, bounded so repair work cannot
starve the operator's work (≤2 repairs/tick, pauses above 3 queued jobs — a lesson learned by
flooding the queue during this session). Two new sensors cover S4: `sweep_starved_work` (queued
and not progressing) and `sweep_abandoned_runs` (working with no job).

---

## 5. Ask BOSS as the doctor

**Position:** Ask BOSS is the operator's first responder. It diagnoses AND mitigates. It stops at
changes that require a code review.

**Shipped:** `cancel_job` (incl. force), `set_job_priority`, `close_orphaned_runs`,
`link_run_origin`, `find_starved_work`, `consistency_report` — plus `query_db` and the
investigation tools.

**Boundary:** operational state (priorities, wedged jobs, orphaned runs, lineage) — yes.
Spending money and editing BOSS's own source — no; the latter goes through the governed pipeline
behind a PR.

**Three defects found while using it (open):**
1. **Misroutes questions into planning.** A question containing SQL or a numbered list returns
   *"I drafted N sequenced phases"* instead of an answer. Observed twice.
2. **Abandons mid-investigation** with "reached the step limit while working".
3. **Not proactive.** It can act, but nothing makes it volunteer a diagnosis; it waits to be asked
   precisely.

---

## 6. The plan

Ordered by operator hours recovered per unit of risk.

### R1 — Load only the active area *(largest remaining win)*
`page.tsx` awaits ~15 loaders plus a `Promise.all` of 14 feeds **before reading `?area=`**. Every
area pays for every other. All three area URLs return within 200 bytes of each other because they
are the same payload.
* **Evidence:** 7.6 MB and 18–100s per page. The console's own fetches time out, producing "API
  unreachable" about a healthy API (I3/I4).
* **Work:** wire the `lib/area-data.ts` loaders and `shouldLoad*` gates that already exist and were
  never connected — this is the abandoned BOSS-13 refactor its own test still asserts.
* **Acceptance:** any single area < 1.5 MB and < 3s server render; `area-data.test.ts` passes.

### R2 — Index the grown tables
Warmup masks a 28.9s cold query; it does not make it fast.
* **Blocked on:** goldeneye's `DATABASE_URL`, which is not in the ops secret store.
* **Acceptance:** cold first query < 3s; remove the warmup or keep it as belt-and-braces.

### R3 — Make Ask BOSS a reliable first responder
Fix the three defects in §5. Add a proactive opening move: on any "stuck/slow/not started"
question, run `find_starved_work` + `consistency_report` before answering.
* **Acceptance:** "why is my meeting stuck?" returns a diagnosis **and** the action taken, in one
  turn, without a step-limit abort.

### R4 — Decision gates that cannot silently kill work
**127 decisions expired vs 18 approved.** Every gate is 24h with `default_action: hold`, so an
unanswered gate kills its run silently. Shorty was gated as *"touches auth"* — it has no auth; a
false positive is therefore fatal, not merely annoying.
* **Work:** a `MERGE_GATE_EXPIRED` shepherd class; reversible `expired → pending`; the gate visible
  on the app card; tighten the risk classifier.
* **Acceptance:** no run reaches a terminal state solely because nobody answered.

### R5 — Lineage visible end to end
Discovery → PRD → Build → Ship carry three unrelated names and nothing links them, so an operator
cannot tell that a completed panel started a build.
* **Acceptance:** from any card, one click to the artefact of every completed upstream stage.

### R6 — The invariant check runs itself
`consistency_report` exists and is read-only. Every incident here was visible in the data for
hours before it was reported.
* **Work:** run it on the autopilot tick; surface non-zero counts in the console; alert on a
  transition from zero.
* **Acceptance:** an operator learns from BOSS, not from a stuck card.

---

## 7. Success criteria

1. A new app goes Discovery → PRD → build → preview → deploy with **no manual intervention**.
2. `consistency_report` reads all-zero, continuously, and a non-zero count raises itself.
3. No card renders a state the data cannot produce (S1 closed).
4. Any single console area loads in **< 3s**.
5. No run ends because a gate went unanswered.
6. When an operator reports something stuck, Ask BOSS answers **and fixes it** in one turn.

---

## 8. Honest notes

* Several defects in this log were introduced **during** the session while fixing others: the
  autopilot flooding the queue (I6 exposure), a safety gate that read the wrong key and could not
  refuse anything, and a cancel that closed jobs while stranding their runs. Each is fixed and
  tested; they are listed because a resiliency plan that hides its own regressions is not one.
* The consistency check's first live run reported 12 violations that were **not** violations —
  blocked runs counted as inconsistent. Corrected. A report with false positives is worse than no
  report, because zero has to mean something.
* Roughly 22 commits landed during the session. The ones that matter are the three shape-tests;
  the rest are instances.

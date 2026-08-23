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

Twenty distinct defects surfaced. Individually each looks like a small bug. Collectively they
share nine root-cause shapes, and those shapes are the actual product problem:

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

### I13 — BOSS killed its own build
* **Saw:** `claude code agent exited -9: [substrate=local_subprocess]`
* **Cause:** `_kill_stray_review_processes` reaps review seats by scanning `ps` for a claude
  binary under a sandbox root. A review seat and an IMPLEMENT agent are the same binary under
  the same root. SIGTERM, then SIGKILL, to a build that was working. Its own docstring records
  the same bug **twice before**, each time patched by tightening the pattern.
* **Fix:** kill by OWNERSHIP. Each subprocess registers the seat it occupies; the reaper checks
  that before signalling. The seat is decided by whether the role can write.
* **Status:** shipped.

### I14 — A sandbox command with no shell
* **Saw:** `Executable is not allowlisted: [`
* **Cause:** `[` is the shell's `test`. `sandbox.run` does `shlex.split` + `subprocess.run`
  **without a shell**, so the first word became the executable. Worse, the check looked at
  `argv[0]` only — so `npm run build && npm test` PASSES and runs only the first half: a verify
  that reports success having never run the tests.
* **Fix:** check the whole command, before `argv[0]`, and name shell syntax as the cause.
* **Status:** shipped. (My first version scanned the raw string and rejected
  `python3 -c 'print(42)'` — quotes matter; now tokenised with `posix=False`.)

### I15 — A confirmation nobody could type
* **Saw:** "Delete permanently" did nothing.
* **Cause:** the guard requires typing the app name back. The name held an EN DASH (U+2013),
  which is not on a keyboard, and the placeholder was truncated so the operator could not even
  read the character they were failing to reproduce. The same docstring had already fixed this
  for a stray SPACE.
* **Fix:** normalise dashes, quotes, unicode spaces, repeated whitespace and case; still refuse
  a genuinely different name.
* **Status:** shipped.

### I16 — "Never deployed" about an app serving traffic
* **Saw:** NEVER DEPLOYED on a card whose URL returns 200.
* **Cause:** the badge reads `stage.deploy_status`, and a stage exists only for a deployment
  that came from a code task with a PR. An operator pressing Deploy creates a `deployment_run`
  with `trigger='manual'` and no stage. The badge was answering *"does this have a merge-driven
  stage?"* while showing the word "never". Stages also carried no `target_id`, and targets no
  history of their own, so the UI could not recover.
* **Fix:** stages name their target; every target carries its latest deployment.
* **Status:** shipped.

### I17 — Two runs of one app, and the guard that could not see it
* **Saw:** two "Shorty" runs queued, neither progressing.
* **Cause:** at 19:14 the autopilot retried a failed build; at 19:16 the operator restarted the
  same build by hand. There IS a duplicate guard — `work_fingerprint` — but it hashes the title
  and instructions **verbatim**, and every retry mutates both by design (`· retry N`, an
  appended ITERATION FEEDBACK block). **A retry could never collide with its own original,
  which is the exact case the guard exists for.**
* **Fix:** the fingerprint identifies the WORK — retry suffix stripped, appended blocks
  removed — plus a second, coarser guard: one in-flight build per (repo, app name), because the
  fingerprint is exact by design and one reworded sentence must not buy a second pipeline.
* **Status:** shipped.

### I18 — An autonomous spender, on by default
* **Saw:** an "Auto-repair: turn cap exhausted" run holding the only worker slot for 33 minutes
  while the operator's work queued behind it.
* **Cause:** I enabled the shepherd autopilot. Inside its own bounds it opened repair tasks for
  historical failures nobody had asked about, duplicated a retry, and did both every 180s.
  Nothing malfunctioned — **the default was wrong.**
* **Fix:** `BOSS_SHEPHERD_AUTOPILOT_ENABLED`, default FALSE. The free half (closing rows that
  claim to be working, unsticking a starved queue) still runs unconditionally.
* **Status:** shipped.


### I19 — The queue has exactly one lane
* **Saw:** two runs sitting at `phase=queued` for a long time with nothing visibly wrong.
* **Cause:** `run_worker_cycle` claims ONE job and runs it **synchronously** in the main loop:

  ```python
  job = client.poll(...)   # one job
  ...                       # executed inline; the loop does not continue until it returns
  ```

  There is no concurrency setting because there is no concurrency — one worker process, one job
  at a time, start to finish. Lease renewal is bounded by `job_max_runtime_seconds` (**5400s =
  90 minutes**), so a single long or hung job can hold the entire system for an hour and a half.
  On the day, an autopilot-invented `Auto-repair: turn cap exhausted` held the lane while the
  operator's work waited behind it.
* **Why the priority work did not save us:** priority bands decide *who goes next*. They cannot
  make anything *overlap*. A 3-minute PRD meeting still waits behind a 90-minute build. **I
  fixed the ordering of a queue that has one server.**
* **Status:** diagnosed, NOT fixed. See R7.

### I20 — BOSS cannot see the machine it runs on
* **Saw:** `exited -9`, and no way to tell OOM from a deploy restart from an internal kill.
* **Cause:** BOSS can query its own database in fine detail and has `inspect_host` for
  *deployment* hosts, but nothing reports its own containers' memory, CPU or restart history.
  The ops API exposes container status only.
* **Consequence:** every diagnosis of a killed process is a guess, and the operator waits while
  hypotheses are tested one at a time. In this session that cost roughly an hour before the real
  cause (I13, BOSS's own reaper) was found by reading code rather than data.
* **Status:** open. See R8.


### I21 — Four cards for one app, and a building app that read BLOCKED
* **Saw:** five runs of Shorty rendered as four separate cards; three showed `BLOCKED` while
  the fifth was actively building. The operator reasonably read the board as "Shorty is stuck".
* **Cause:** `finalize_ctx.app_id` was stamped on the two RETRY paths only. `start_code_task`
  set nothing. Lineage answers *"what did this run come from"*, and the runs that most need
  grouping are exactly the ones with no common ancestor — starting an app again after a failure
  creates a root, not a child. The console had no fact tying the five together.
* **Consequence:** the badge logic was correct on every card and the board was still wrong.
  Status was never the defect; identity was.
* **Fix:** `stable_app_id(project_id, repo, title)` derived from the same `project_name`
  normalisation `active_same_app` already trusts to REFUSE to start a duplicate, stamped at
  creation. The console derives the same token so runs created before the change group too.
  *A definition good enough to block work is good enough to group a card.*
* **Status:** fixed, pending deploy.

### I22 — A blocked run whose job was cancelled sits blocked forever
* **Saw:** `crun_1e60f170` at `blocked / awaiting_plan_approval` with no job, indefinitely.
* **Cause:** `blocked` was deliberately excluded from the abandoned-run sweep, reasoning that
  it means "waiting on a human" and is a legitimate resting state. That holds **only while a
  job exists to resume**. Approving the decision on a run whose job is gone resumes nothing.
* **Consequence:** a permanently dead run rendered identically to one genuinely awaiting the
  operator, and sat next to the live build feeding I21.
* **Fix:** `blocked` added to the sweep, kept safe by the existing no-live-job rule.
* **Status:** fixed, pending deploy.

### I23 — 28 identical preview failures, no circuit breaker
* **Saw:** `crun_b6a1bf93` produced **28 consecutive failed previews** over six hours, every
  ~3 minutes, every one the same rollup parse error, every one a full npm install and build.
* **Cause:** nothing counts consecutive failures of the same kind against the same run. Retry
  is unconditional.
* **Consequence:** hours of compute on an outcome that was decided at attempt one, on the only
  worker lane (I19), while the operator's real work queued behind it.
* **Status:** open. See R9.

### I24 — A preview is "running" because something answered on the port
* **Saw:** the operator asked why `Open preview` was enabled for a preview showing no result.
* **Cause:** `status='running'` is set when the port serves HTTP 200. A blank SPA — a JS crash,
  an unmounted root, a bad base path — returns 200 with a full HTML shell.
* **Consequence:** the button asserts more than was verified. *In this instance the preview was
  genuinely healthy* (verified in a real browser: 200 with and without auth, CSS and JS both
  fetched, no console errors, full page rendered) — but the check would not have caught it if
  it had not been.
* **Status:** open. See R10.

### I25 — Preview routes serve unauthenticated under an authed prefix
* **Saw:** `/v1/previews/<id>/web/` returns 200 with no `Authorization` header while the rest
  of `/v1/` requires a Bearer key under `BOSS_REQUIRE_API_AUTH=true`.
* **Consequence:** anything a preview builds is readable by anyone who can reach the host.
  Tailnet-only limits the blast radius; it does not make it intended.
* **Status:** open, needs a decision — previews may be deliberately shareable. See R11.

### I26 — Two analyses in this session were wrong, and why
Recorded because the *pattern* cost more time than either bug.
* **Claimed:** 90+ console feeds are awaited in `Promise.all`, so one rejection blanks the page.
  **Actually:** every loader fails soft — module-local `getJson` helpers catch and return
  `null`, and `fetchAssuranceFeed` already uses `allSettled`. `Promise.all` cannot reject.
  The detector matched on syntax inside one function body instead of following the call chain.
* **Claimed:** `lib/area-data.ts` was half-wired. **Actually:** the file does not exist; the
  test asserts a design never built (see R1).
* **Shape:** a plausible mechanism, confirmed against the shape of the code rather than its
  behaviour. Both would have produced a real-looking fix for a defect that was not there.
* **Counter-measure:** for any claim about control flow, follow it to the primitive — the bare
  `fetch`, the actual `os.kill` — before proposing a change.


### I27 — The worker had no shutdown, so every deploy was a gamble
* **Saw:** every deploy in this session had to be held until nothing was running, because a
  container recreate SIGKILLs an in-flight agent — the cause of Shorty attempt 1's `exited -9`.
* **Cause:** `apps/worker/boss_worker/main.py` installed **no signal handler at all**. Docker
  sends SIGTERM then SIGKILLs after its grace period; Python's default terminated the process
  instantly, mid-job, logging nothing about what was interrupted.
* **Mitigating fact, found while fixing it:** the lease reclaimer already recovers the job —
  the lease expires within one TTL and it is claimed again. So a deploy cost a restarted phase
  and up to a 5-minute delay, not lost work. That is materially less bad than it appeared, and
  it is why R12 below is deliberately *not* being rushed.
* **Fix:** SIGTERM/SIGINT set a stop flag; the loop exits between cycles; the idle sleep is
  interruptible so a quiet worker does not hold a deploy open for a whole poll interval; the
  interrupted job id is logged.
* **Status:** fixed, pending deploy.


### I28 — `awaiting_plan_approval` is set but never cleared *(latent)*
* **Saw:** a run at `running / review_panel`, well past its approval gate, still carrying
  `state.awaiting_plan_approval = true`.
* **Not the BLOCKED bug.** The console renders `phase`, not this flag; checked before assuming.
* **Why it still matters:** `governed_code_task_service.py:1323` reads it to decide whether to
  restore a prior design on resume. Left set, a later resume of the same run can resurrect a
  stale plan that was already superseded.
* **Shape:** S1 — a flag set on the way in with no path back.
* **Status:** open, deliberately not fixed mid-session. One line, in the governed resume path,
  with no observed failure attached to it; the right time is with the R7 work, not now.


### I29 — Grouping fixed, naming still leaks *(residual, verified on live data)*
After I21's fix, 60 live runs collapse from a fragmented board into 14 app cards — Shorty 5
runs on one card, HookWatch 20, BugBox 12, ApproveIt 6 — and the Shorty card now contains its
`running` run, so it reads RUNNING rather than BLOCKED. Three names are still wrong:
* `Official BugBox` did not fold into `BugBox`. `LABELS` carries `^Official\s+` and should have
  lifted it to a badge, so either the title does not start with it or an explicit
  `**Project:**` line won first. Two cards for one app.
* `/healthz endpoint before LinkNest` — a fragment of a sentence, not a name.
* `Lightweight Self‐` — truncated mid-word, on a non-ASCII hyphen `_norm` does not fold.
* **Status:** open. Same family as I11; the grouping is correct and the label is not.


### I30 — `npm start` runs an app; it does not compile one
* **Saw:** Shorty finished, passed its own suite, opened PR #106 — 6,988 lines, routes, db,
  metrics, Playwright specs — and then **could not be previewed**. The operator watched
  "Starting preview…" for minutes and got a timeout.
* **Cause:** its start script is `node dist/server.js`. The preview ran `npm install` then
  `npm start`, never `npm run build`, so `dist/` never existed and node died instantly on
  `MODULE_NOT_FOUND`. The error was three levels down a log the console does not surface.
* **The shape, exactly (S3):** the requirement was already understood in three places — both
  Vite branches build first, and so does the generated Dockerfile. `_detect_command_in` had
  **four** command branches and one of them went straight from install to start. The rule was
  expressed four times and satisfied three times.
* **Fix:** the missed branch builds first, `--if-present` so apps without a build script are
  unaffected, and `dev` excluded because a dev server compiles on the fly.
* **Counter-measure:** the guard test reads the **whole module** rather than the one function
  that was wrong — pinning it to `_detect_command_in` is how the fifth branch gets missed too.
  It also verifies the `{build}` f-string hole can only ever hold a real build command, so the
  interpolation cannot become the loophole.
* **Status:** fixed, pending deploy.


### I31 — "No verified host yet", while a verified host existed
* **Saw:** the operator reported no host available to "Set target" on the Shorty card.
* **The data was fine.** `/v1/deployment-hosts` returned `dh_ff3948d78950` — `hetzner-ci-01`,
  `2.29.2.202`, arch `amd64`, **status `verified`** — the whole time.
* **Cause (two defects, one shape):**
  1. `hosts` initialises to `[]` and `ready = hosts.length > 0`, so between first paint and the
     `useEffect` resolving, the component stated **"No verified host yet"** — a definitive claim
     of absence made before it had looked. On an 8 MB console page hydration is slow enough
     that the false claim is what gets read.
  2. `if (r.ok) setHosts(...)` had **no else**. A failed lookup left the list empty forever, so
     an infrastructure failure rendered as a missing host — silently, and it sends the operator
     to onboard a host they already own.
* **Shape:** S7/I24 — a state asserting more than was verified. "Not asked yet", "asked and it
  broke" and "asked, genuinely none" are three facts; one render served all three.
* **Fix:** explicit `loadState`, a distinct render for each, cancellation on unmount, and
  `.catch` so a thrown action is not mistaken for an empty list.
* **Status:** fixed, pending deploy.

### I32 — "Spec: none" is correct, and the e2e claim was overstated
* **Saw:** the Shorty card shows `Spec: none`.
* **Not a bug.** `journey-spine-ui.tsx` claims Spec only when the run RECORDS coming from a
  panel (`origin.kind === "meeting"`). Shorty has `task_id: null` and no origin, and the
  tooltip already says "No panel: this task was written directly".
* **What it exposes is mine.** Shorty was created directly as a code task, bypassing
  Discovery → PRD panel. So the "full end-to-end run verified" claim covered **Build → Ship**
  only; Research and Spec were skipped and the spine was honestly reporting two hollow stages.
* **Lesson:** the spine is the product telling the truth about coverage. Reading it as a defect
  would have removed the one control that caught an overstated claim.
* **Status:** no code change. The genuine four-stage run is the outstanding work.


---

## 2b. Second-pass review — what the first pass missed

Written after re-reading the log above and going looking for what was *not* in it. Every item
here was verified against the running system, not inferred.

### I38 — Shepherd retry chains are unbounded, and each link costs money *(highest severity)*
* **The guard that exists:** `shepherd_ledger` refuses to apply the same remedy to the same run
  twice — `WHERE task_ref = :t AND status = 'used'`.
* **Why it does not bind:** `retry_code_task` creates a **new run with a new id**. That child is
  a fresh `task_ref` with an empty ledger. If it fails, the same remedy applies again, to the
  grandchild, and so on. **The ledger keys on the very identity the remedy replaces.**
* **Cost:** a preview retry storm (I23) burned CPU. This burns **$4–15 per link**, unattended.
  `MAX_ACTIONS_PER_TICK = 2` bounds the *rate*, not the total; the chain has no depth limit.
* **Why it did not fire today:** `BOSS_SHEPHERD_AUTOPILOT_ENABLED` is `false` on goldeneye (I18).
  The bound that is actually holding the money is a feature flag, not a limit.
* **Fixable cheaply:** lineage is already recorded — `retry_code_task` sets
  `root = fctx_prev.get("root_run_id") or run_id`. Count the chain and stop at N.
* **Status:** open. This is R9 generalised, and R9 as written covers only previews.

### I39 — Preview workdirs are never deleted
* **Evidence:** `preview_service.py` contains **no `rmtree`, no `unlink`, no cleanup path** of
  any kind. `PREVIEW_ROOT = /tmp/boss-previews`, one directory per attempt, each holding a full
  `npm install`.
* **Scale:** 50 preview rows exist for this project alone; 31 are `failed` — and a failed preview
  has still done its install before dying. `node_modules` for a Vite/Playwright app is commonly
  100–300 MB.
* **Consequence:** unbounded disk growth inside the API container, whose disk is also where the
  agent works. Disk exhaustion presents as unrelated, confusing failures everywhere at once.
* **Not measurable from here** — which is R8's argument made concrete: BOSS cannot report its own
  container's disk, so the size of this cannot be stated, only bounded from below.
* **Status:** open.

### I33 — A 404 from a feature this edition does not have is not an error
* **Saw:** 10 × "API returned 404" on every console load. Deterministic: 10 on load 1, 10 on
  load 2, 10 on load 3.
* **Actually:** 5 panels, each counted twice (rendered HTML + RSC flight payload), and **none of
  them in the area being viewed** — `/v1/self-improvement/*`, `/v1/marketplace/listings`,
  `/v1/marketplace/sync-runs`, and three SSO/IdP cards. All are defined in `main.py` but mounted
  on **paid-edition routers**; this instance is Community, so 404 is the correct answer.
* **Consequence:** the console prints an infrastructure error for a licensing fact, on every
  load, five times. It reads as a broken control plane, which is exactly how it was reported.
* **The rule already exists in one place:** `self-improvement.ts` says the feature is not in this
  edition and hides the CTAs. Marketplace and SSO do not. **S3.**
* **Status:** open.

### I34 — Recreating `web` briefly 404s the `api` route
* **Saw:** immediately after a `--recreate web` deploy, *every* API path returned Traefik's
  plain-text `404 page not found` — including `/health`, which touches neither the database nor
  editions. It recovered on its own within minutes.
* **Distinguishing evidence:** Traefik's 404 is `text/plain "404 page not found"`; FastAPI's is
  JSON `{"detail":"Not Found"}`. The former means *no router matched* — deregistration, not a
  slow or failing backend.
* **Consequence:** every deploy today looked like an outage, and cost time proving it was not.
  The console handles it correctly (`OrgReconnect` holds the session and retries every 5s) —
  which is why it degraded gracefully instead of dumping the operator to the org picker.
* **Status:** open. Deploy-window defect, not an availability defect.

### I35 — Two BOSS instances share one project id, silently
* **Saw:** a meeting started through the MCP tool could not be found on goldeneye — its run, task
  and job all 404'd there.
* **Cause:** `boss-mcp.sh` exports `DATABASE_URL=…localhost:5432/boss` and drives the **local**
  engine. The console drives **goldeneye**. Both call the project `boss-dogfood`.
* **Consequence:** identical ids, identical project name, two different databases, no signal
  anywhere about which one you are looking at. Work started in one is invisible in the other, and
  a cancel issued against the wrong one silently succeeds.
* **Aggravating:** the local API is an **older build** — `/v1/jobs/{id}/cancel` returns 404 there
  (route absent) and 409 on goldeneye (route present, job unknown). The *shape* of the error is
  the only way to tell the versions apart.
* **Status:** open. Needs an instance identity surfaced in every response and in the console.

### I36 — The deploy client times out before the deploy finishes
* **Saw:** `ops.py deploy` raised `TimeoutError` twice while the deploy **succeeded**.
* **Cause:** a 60s client timeout against an api+worker recreate with health checks that takes
  longer. The failure is indistinguishable from a real failure without an independent check.
* **Consequence:** the honest response to a timeout is "I do not know", and re-running risks
  double-applying. Confirmation had to be behavioural — watching a `blocked` run flip to
  `cancelled`, something only the new code does.
* **Aggravating:** `ops.py diagnose` does not report image tags, so "what is actually deployed"
  cannot be read back directly.
* **Status:** open.

### I37 — Cancelling a meeting does not stop its next step
* **Saw:** cancelling `mrun_31a5f9bf` set the run to `cancelled`; the worker then enqueued a
  **new** `meeting.step` job for it. Cancellation had to be applied repeatedly until it stayed
  clean.
* **Cause:** each step enqueues the next on completion; nothing consults the run's status before
  doing so. `cancel_queued_job` closes the run and the job *that exists* — it cannot close the
  one that has not been created yet.
* **Consequence:** cancel is not a terminal instruction, it is a race. On the paid path this is
  spend continuing after an operator said stop.
* **Status:** open. The fix is a status check at enqueue time, not a better cancel.

### I40 — Running a job degrades the control plane, because the agent lives inside it
*Measured 2026-08-23 while a code task was in `check_tests`.*

| Probe | While a job runs | Between phases |
|---|---|---|
| `/health` (15s-cached) | 0.6–2.5s | 0.2s |
| `/v1/code-tasks?limit=5` | **47.1s, then 39.5s** | 0.86–1.1s |
| next call after those | **404 in 0.3s — Traefik's plain-text 404** | 200 |
| console page | 79.5s, then a 200s timeout | — |

* **Cause:** the code agent runs **inside the API container** — the worker POSTs to the control
  plane and the API executes the pipeline. `check_tests` runs the app's own suite (npm install,
  build, Playwright) in that process's container. The API's threadpool and CPU are the agent's
  threadpool and CPU.
* **Why the I3 fix does not cover it:** boot warm-up addresses a COLD cache. This is live
  contention, recurring, on a warm process. Same symptom, different cause.
* **Consequence:** the control plane becomes unreliable exactly when there is something worth
  watching. Every "the console is down" report today happened while a run was in flight, and the
  poller watching that very run failed with JSON parse errors because it was handed Traefik's
  plain-text 404 instead of a response.
* **This changes what R7 means.** The obvious reading of "one lane" is *add lanes*. Adding lanes
  **inside the same container makes this worse** — N agents contending with the API instead of
  one. The first move is ISOLATION (get the agent out of the API process), and only then
  concurrency. A fix at the wrong layer here would be S9 again, and this time I would have
  measured the evidence for it myself.
* **Status:** open. Reframes R7; see 6.0 Phase 1.


---

## 3. The shapes

The incidents are forty; the shapes are fourteen. Fixing shapes is what stops recurrence.

| # | Shape | Incidents |
|---|---|---|
| **S1** | **A state with no exit.** A pending flag, a spinner, a `running` row — set on the way in, with no path back on failure. | I8, I9, I10, I12 |
| **S2** | **A mechanism neutered at its call sites.** The check exists, is correct, and every caller passes a value that defeats it. | I5 (7×), I6, I7 |
| **S3** | **Fix one path, miss its twin.** The same defect in 2–8 places; fixed where it surfaced. | I2 (3×), I5 (7×), I8, I9, I30 |
| **S4** | **Only failure is watched.** Every sensor is error-triggered. Starvation and stalling produce no error, so nothing sees them. | I6, I8, I12 |
| **S5** | **Internals leak to the operator.** Scratch space, store keys, raw prose, snake_case identifiers rendered as if they were product. | I1, I4, I11 |
| **S6** | **A guard patched instead of fixed.** The check is wrong in kind; each failure tightens the heuristic rather than replacing it. Pattern-matching `ps`; an exact-match confirmation. | I13, I15 |
| **S7** | **A label that answers a different question.** The field is computed correctly and measures something other than what its name claims. | I16, I12 |
| **S8** | **Identity derived from text that legitimately varies.** Fingerprints over prose the engine itself rewrites. | I17, I5 |
| **S9** | **A fix at the wrong layer.** The diagnosis is right and the remedy addresses a different property of the system — ordering when the constraint is capacity. | I19 |
| **S10** | **Identity assigned late.** The linking fact is written on the exceptional path (retry) and not on the ordinary one (create), so the common case has no identity at all. | I21 |
| **S11** | **Unbounded retry of a decided outcome.** The same failure repeated N times because nothing counts. | I23 |
| **S12** | **A conclusion checked against the shape of the code, not its behaviour.** | I26 |
| **S13** | **Bounded per instance, unbounded per lineage.** The guard keys on an id that the remedy itself replaces, so every application starts with a clean slate. | I38, I17 |
| **S14** | **A path's first real execution is its first test.** Every stage in §2 failed the first time it ran against a real app. Nothing had ever gone end to end, so the whole log is one integration test, run manually, at operator expense. | all of §2 |

**The counter-measure to each is the same: a test that rejects the SHAPE, not the instance.**
**Eight** now exist — dedupe anti-pattern, bare priority literals, artifact-consumer wiring,
no-run-sits-forever, starvation sensors, bounded preview retry, build-before-start (whole-module),
and empty-is-not-an-answer. Each found violations the moment it was written, which is the point.

S14 is the one no test can close, and it is the reason this document exists. The counter-measure
is not a test but a habit: **the pipeline must be run end to end, on a real app, on a schedule** —
otherwise the next unexercised path is discovered the same way these were.

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

### 6.0 Sequence — what to build, in what order, and why that order

The plan below grew by accretion and lists twelve workstreams with no ordering. That is not
executable. This is the order, and the reasoning is dependency and blast radius, not preference.

**Phase 0 — stop the bleeding.** Small, isolated, no design required. Each one is currently
prevented only by luck or by a feature flag.

| # | Item | Why first |
|---|---|---|
| 0.1 | **Bound retry chains by lineage** (I38, R9 generalised) | The only unbounded *spend* path in the system. Today it is held by `autopilot_enabled=false` — a flag, not a limit. If that flag is ever flipped, this bills money in a loop. |
| 0.2 | **Delete preview workdirs** (I39) | Unbounded disk growth on the container the agent also works in. Disk exhaustion presents as unrelated failures everywhere at once, which is the most expensive kind to diagnose. |
| 0.3 | **Edition-aware feeds** (I33) | Five panels print "API returned 404" on every load. Cheap, and it removes the single loudest false signal an operator sees. |
| 0.4 | **Status check at enqueue** (I37) | Cancel is currently a race, not an instruction. On a paid path that means spend continues after stop. |

**Phase 1 — the constraint.** Nothing else in this document changes the queueing behaviour.

| # | Item | Why here |
|---|---|---|
| 1.1a | **Isolate the agent from the API process** (I40) | Measured: while a code task runs, `/v1/code-tasks?limit=5` takes **40+ seconds** and the route is then **evicted from Traefik**. The agent executes inside the API container, so its test suite competes with every console read. This must come FIRST — adding lanes inside the same container multiplies the contention rather than relieving it. |
| 1.1b | **R7 — more than one lane** | I19 is upstream of most of §2. One worker, one job, inline, 90-minute ceiling. Priority bands order a queue with one server; they cannot make work overlap. Only useful once 1.1a has removed the shared-process contention. |
| 1.2 | **R12 — cooperative cancellation between phases** | Deliberately after 1.1: a phase boundary is the natural place to hand a job back, and multi-lane makes losing one lane cheap. Doing it now, into a single blocked thread, is the risky version. |

**Phase 2 — make the system observable before making it more autonomous.**

| # | Item | Why here |
|---|---|---|
| 2.1 | **R1 — load only the active area** | 8 MB and 17–30s per load, measured. Every "API unreachable" report today traced back to console load, not API health. |
| 2.2 | **R6 + R6a — invariants that run themselves, and an event stream** | Every incident here was visible in the data for hours. Note the ordering rule from I18: free mitigations fire, spending mitigations raise an event and wait. |
| 2.3 | **R8 — BOSS can see its own machine** | I39 could not be *measured* from here, only bounded from below. That is this item's argument, made concrete. |
| 2.4 | **R2 — measure before indexing** | Explicitly after 2.3, because the measurement needs the observability. |

**Phase 3 — correctness of the human boundary.**

| # | Item |
|---|---|
| 3.1 | **R4 — gates that cannot silently kill work** (127 expired vs 18 approved) |
| 3.2 | **R5 — lineage visible end to end** (and I32: a run must record where it came from) |
| 3.3 | **R3 — Ask BOSS as a reliable first responder** |
| 3.4 | **R10 / R11 — preview verification honesty, and the preview-auth decision** |

**Continuous, starting now — the S14 counter-measure.** Run the full pipeline end to end on a
real app, on a schedule, and treat a failure as a release blocker. Every defect in §2 was found
by an operator doing this by hand, once, at their own expense. That is the actual root cause and
no individual fix addresses it.

**Deliberately not scheduled:** I34 (deploy-window 404) and I36 (deploy client timeout) are
operator-experience defects in the *deploy* path, not the product. They cost me hours today and
they will cost the next person the same, but they do not affect a running BOSS. Fix them when
touching deployment; do not let them displace Phase 0.

---

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

### R2 — Find out what is actually slow before indexing anything
*Premise re-checked and it does not hold.* `db/schema.sql` already carries **242** indexes,
including on every table this item named — `idx_jobs_claimable` and `idx_jobs_ranked_claimable`,
`idx_events_project_created`, `idx_code_task_runs_project`, `idx_app_previews_project/_run`.
A 28.9s cold query against well-indexed tables is not a missing index; it is cold connection
setup, an empty page cache, or pool exhaustion.

Adding indexes on that reasoning would have been S9 — a real diagnosis answered at the wrong
layer — and would have left the actual cause untouched while looking like progress.

* **Not blocked on `DATABASE_URL` either.** The API self-migrates via `ensure_schema` on boot,
  so idempotent DDL ships with a normal deploy. That was a wrong assumption, not a constraint.
* **Do first:** measure cold vs warm latency per endpoint from outside, and get one `EXPLAIN
  (ANALYZE, BUFFERS)` of the slow path. Then decide — index, pool sizing, or pre-warm.
* **Acceptance:** cold first query < 3s, *with the reason named*.

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

### R7 — More than one lane *(the constraint behind most of §2)*
One worker, one job, executed inline, with a 90-minute lease ceiling. Every queueing symptom in
this document is downstream of it, and no amount of priority tuning changes it.
* **Work, in order of risk:** (a) run N worker processes — the job table already leases safely
  with `FOR UPDATE SKIP LOCKED`, so this is mostly deployment; (b) separate the lanes by class
  so interactive work is never behind batch, whatever its priority; (c) shorten
  `job_max_runtime_seconds` for classes that should never take 90 minutes.
* **Acceptance:** a PRD meeting starts within a minute while a code task is building; one hung
  job cannot stall unrelated work.

### R8 — BOSS can see its own machine
`exited -9` cost an hour of guessing because nothing reports memory, CPU or restart history for
BOSS's own containers. `inspect_host` exists for deployment hosts and has no equivalent here.
* **Acceptance:** "why did this process die?" is answerable from BOSS, with evidence, in one
  question.

### R6a — Monitoring events as a first-class subsystem *(requested)*
Today a run sat dead for 53 minutes and **nothing emitted an event**. Status is polled by
whoever happens to look.
* **Work:** an append-only event stream for every state transition worth noticing (run stalls,
  queue starves, invariant becomes non-zero, deploy health flips, a gate approaches expiry);
  the console subscribes rather than polls; each event carries the mitigation the shepherd
  would apply and whether it was applied.
* **Boundary, and it must be explicit:** mitigation that costs nothing (reprioritise, close an
  abandoned row, cancel a wedged job, restart a preview) fires automatically. Mitigation that
  SPENDS (retry a code task, open a repair task) raises an event and waits — see I18.
* **Acceptance:** every incident in §2 would have produced an event before an operator noticed;
  no event results in unrequested spend.

### R9 — A preview that fails the same way twice stops trying
Count consecutive failures per `(run_id, error signature)`. At **2** identical signatures, stop
and surface the error instead of rebuilding. I23 burned six hours of the single lane proving a
rollup error was still a rollup error. Cheap: the signature is already stored on each row.

### R10 — Verify what is actually verifiable, and claim no more
*Revised after writing it.* The first draft said "promote to `running` only once the HTML
yields a non-empty mount". **That cannot be done server-side.** An SPA shell is
`<div id="root"></div>` whether the app works or not; only a browser knows if it rendered.
Shipping that check would have been I24 again — a status asserting more than it verified.

What IS verifiable without a browser, and worth doing:
* the root document returns 200 **and is HTML**, not an error page;
* every same-origin `src`/`href` it references also returns 200.

That catches the common blank-preview cause — a broken base path or a missing bundle — and it
is honest about the one it cannot catch, a runtime JS exception. A real render check needs a
headless browser in the preview path; worth costing separately, not worth faking.

### R11 — Decide whether previews are public *(needs a product call)*
I25. Either gate `/v1/previews/*` behind the same Bearer key as the rest of `/v1/`, or state
explicitly that previews are shareable links and scope them with an unguessable id. Not a bug
until decided — but it must be decided rather than inherited.


### R12 — Hand the job back on shutdown *(deliberately deferred)*
On SIGTERM the worker could report its in-flight job retryable-failed, requeuing it in seconds
instead of waiting out a lease TTL. It is **not** done, and the reason is worth recording: the
main thread is blocked deep inside `execute_job`, so releasing the lease means either raising
an exception into arbitrary in-progress code or completing the job from a second thread while
the first may be completing it too. Both are real double-completion and torn-state risks, and
the reclaimer already makes the outcome correct — just slower.

Cost of not doing it: up to one lease TTL (default 300s) and a restarted phase per deploy.
Cost of doing it wrong: a job marked failed while it is still running. Not a good trade under
time pressure; revisit with a proper cooperative-cancellation point between phases, which is
where it belongs anyway.


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
* I18 is the sharpest lesson in this document: **every individual bound I built was correct,
  and the system still spent the operator's money on work they had not asked for.** Bounds make
  autonomy safe to switch on; they do not make it right to switch on by default.
* Roughly 30 commits landed during the session. The ones that matter are the three shape-tests;
  the rest are instances.

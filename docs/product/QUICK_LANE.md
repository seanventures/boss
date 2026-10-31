# Quick lane: small fixes in minutes, through one agent and its own queue

Status: proposed, P1. Release branch: `rel/rc10.10-kg`. Written 2026-10-31.
Related: [PRINCIPAL_ENGINEER_INTELLIGENCE.md](PRINCIPAL_ENGINEER_INTELLIGENCE.md) (BOSSAPP-783).
The quick lane must keep that spec's quality floor.

## 1. Why

A one-file fix takes BOSS 50–90 minutes. A coding agent working directly takes 10–20 minutes for
the same fix. On 2026-10-31, four small KG fixes were made by hand while BOSS was available,
because routing them through a build would have stalled the case-study video for hours. BOSS needs
a lane where a small, well-understood change is as fast as a direct agent, without becoming the
shallow-fix path the principal-engineer spec warns about. Quick means less overhead, not
less context or less validation (§3.4).

## 2. Measured: where build time goes

Last 14 days, `finalize_ctx.phase_timings`:

| Phase | boss-1: runs, median, p90 | laptop: runs, median, p90 | Total (boss-1 / laptop) |
|---|---|---|---|
| implement | 153, 5.5 min, 19 min | 149, 4.4 min, 18.6 min | 19 h / 16 h |
| run_tests | 191, 4.5 min, 12 min | 162, 2.4 min, 10.7 min | 19 h / 11 h |
| design | 28, 9.3 min, 16 min | 45, 7.0 min, 14 min | 4 h / 6 h |
| review_panel | 75, 2.7 min, 10.5 min | 78, 2.3 min, 11.3 min | 5 h / 6 h |
| opening_pr | 42, 1.5 min, 2.2 min | 62, 1.4 min, 2.7 min | 2.6 h / 1.5 h |

**Findings:**
1. **The PR step is not slow.** The 17–28 minute "opening PR" seen on 2026-10-31 was one incident:
   boss-1 ran out of memory around 19:15–19:38. Every writer stalled, including overwatch,
   process_manager ticks and build-services Postgres (OOM-killed). Two builds happened to be in
   their PR step at the time.
2. **Latency is the loop.** On boss-1, 42 builds produced 153 implement rounds (about 3.6 each)
   and 191 test runs (about 4.5 each). Each round restarts a session; each test run re-runs the
   selected suite.
3. **Fixed stages before any code:** a design stage (7–9 min median) and, for workflow-started
   builds, a PRD panel and its approval, which can wait hours for a person.
4. **Shared hosts run out of memory under concurrent builds,** and the deploy smoke test did not
   notice that build-services was dead.

## 3. Design

### 3.1 One agent job, not a pipeline

A quick-lane job is one agent session in a sandbox on the repository and branch. It reads, edits
and runs tests itself, the way a developer with a coding agent works, inside a single warm session.
There is no meeting, no PRD gate, no design chain, no multi-seat panel, and no separate run_tests
rounds between sessions.

### 3.2 Its own queue

- Each host reserves one quick-lane slot. Jobs run **one at a time per host**, so they never wait
  behind long builds, and long builds never starve.
- **Admission is memory-aware.** A job starts only when the host has the memory its sandbox needs;
  otherwise it waits with a stated reason. This prevents the 2026-10-31 stall.
- **Priority:** person-asked quick jobs come before system-started ones.

### 3.3 Limits and escalation

- **Caps:** 20 minutes wall clock, a cost cap, and a diff cap (files and lines, as
  `fix_lane_triage` already defines). Protected paths are never touched.
- **Hand-off.** When the change outgrows a cap, touches a protected path, or its blast radius
  reaches many callers, the job stops and hands the work to the full workflow. It carries its study
  notes, reproduction, partial diff and the reason (`fix_lane_runner.check_escalation`). Nothing
  is thrown away.

### 3.4 Quick, never careless: full context in, independent validation out

Quick removes overhead (meetings, design chains, panels, repeated full runs). It never removes
context, and never skips independent validation. Being quick is not licence for the obvious,
fundamental mistakes every model makes when it works without context or without a second pair of
eyes: synthetic fixtures, an unchecked caller, a cause claimed but not reproduced, a failure
relabelled as success.

**Context going in.** The quick agent starts with what a principal engineer would have:
- the Study record for the area, when one exists;
- the KG: callers, dependents, precedent, and why the code is the way it is;
- the project's learnings and corrections, including past steers on this area;
- scoped standards and the repository's own conventions;
- the evidence the request came with (run ids, errors, logs).

All of it is loaded before the first edit (BOSSAPP-784, BOSSAPP-785).

**The builder's own floor:**
1. **Reproduce first.** Show the failure (the error, or a failing test) before the fix, and name
   the cause with file:line. Check for an existing mechanism before adding one.
2. **Blast radius.** Use `kg_callers` / `kg_blast_radius`. Run the tests that reach the changed
   code and its callers.
3. **Real data shapes.** Tests use the shapes the system really produces (for example
   `code_index.symbol_key`), never invented fixtures.

**Independent validation, by a different agent, before the PR opens.** The builder never validates
its own work. A separate validator agent runs in a fresh context, on a different model where one is
available. It gets the request, the diff, the Study notes and the test output, and does both:
- **Review:** reads the whole diff; checks every claim in the PR text against evidence; checks
  callers and existing flows in the blast radius; flags out-of-scope changes (for example a
  permission change in a KG fix).
- **Independent test:** writes or runs its own check of the intended behaviour against real data
  or the real run. Where the request came from a failing run, it replays that run. It does not
  reuse the builder's tests.

Each finding goes back to the builder in the same warm session, for a bounded number of rounds. A
disagreement the two cannot settle escalates through the decision ladder (BOSSAPP-788), then the
full workflow.

**The PR carries the evidence:**
- the reproduction;
- the causes, as file:line;
- the targeted test output;
- the validator's review and its independent test;
- what was left out and why.

The full suite runs in CI after the PR opens; a CI failure reopens the job (bounded).

### 3.5 Entry points

The quick lane is a thin client of the one agent path (BOSSAPP-759):
- Ask BOSS ("fix X in Y");
- the console's New build, with "Quick fix" chosen or suggested;
- MCP `create_code_task` with `lane: quick`;
- a Plane or GitHub label (`boss:quick`).

Triage (`fix_lane_triage.classify`) suggests the lane; a person or caller may force it.

### 3.6 Outside a workflow, still governed

Quick jobs keep identity (the caller's grant), budget reservations and receipts, the review
comments on the PR (P1/P2/P3), and the run record. The merge stays a person's decision, as today.

## 4. Also fixed (helps every build)

- **Host health:** memory-aware admission for all builds. The deploy smoke test checks that
  build-services is healthy; it passed on 2026-10-31 while build-services was dead.
- **Loop cost:** keep the implement session warm across repair rounds (resume instead of restart),
  and select tests by dependency (BOSSAPP-787) so each run is smaller.

## 5. Targets and how we measure

- **Quick lane:** p50 ≤ 15 minutes, p90 ≤ 25 minutes from request to PR, for changes within caps.
  Measured per job from the same phase timings, and compared against a plain coding agent on the
  same fixes.
- **Quality:**
  - share of quick PRs merged without rework;
  - escalation rate;
  - CI failures after open;
  - regressions found after merge.

  A quick lane that is fast but needs rework has failed.
- **First proof:** route the next five real bugs from the KG case study through the quick lane
  and report the time and outcome of each.

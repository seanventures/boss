# Case study: does the BOSS Knowledge Graph make agents better on a real enterprise codebase?

Companion to [KNOWLEDGE_GRAPH_SPEC.md](KNOWLEDGE_GRAPH_SPEC.md). This is the proof plan. It also becomes the next enterprise demo, "the retail app".

## 1. The question

On a large, real retail codebase, does BOSS with the knowledge graph (arm **B**) beat BOSS without it (arm **A**):
- on **outcome** (tasks solved, hidden tests pass);
- on **cost and time** (dollars, tokens, turns and minutes per solved task);
- on **learning** (later tasks get cheaper; mistakes are not repeated);

on **both** a frontier engine and an open model on an API key, with no loss of quality?

## 2. The setting: "NorthPeak Retail" (fictional company, real code)

- **Main app: [Saleor](https://github.com/saleor/saleor)** (BSD-3-Clause, Python/Django + GraphQL, about 250 MB repository). It is a production-grade commerce platform: catalogue, checkout, orders, payments, warehouses, promotions.
  - Fork it to the demo GitHub org and pin a commit (record the SHA in the harness).
  - It is an *existing app*: every task is "add to / fix / change" on it.
- **Second repository (multi-repo couplings): `northpeak-fulfilment`.** A small Python service that BOSS itself builds in task T1.
  - It consumes Saleor order webhooks and writes to a shared `stock_reservation` table, so there are real cross-repo edges (a webhook, a shared table).
- **Deploy target:** the existing staging host pattern, using `203.0.113.x` in docs. Monitoring comes from Beacon's pack, as today.

## 3. The task sequence (12 tasks, run in order: knowledge accumulates)

Each task has:
- a one-paragraph brief in plain language (what a retail product owner would write);
- a **hidden acceptance suite**, written before any run and never shown to agents, which decides "solved";
- a tag for what knowledge should help.

| # | Brief (summary) | Kind | Knowledge that should help |
|---|---|---|---|
| T1 | Build `northpeak-fulfilment`: reserve stock when an order is placed (webhook) | new service, multi-repo | webhook contract, order model |
| T2 | Add "click & collect" as a shipping method with store pickup slots | major feature | checkout and shipping invariants, T1 couplings |
| T3 | Promotions: "buy 3, pay 2" on a category | feature | discount pipeline structure |
| T4 | **Controlled fault:** release with an off-by-one in stock reservation; customers oversold | incident (Beacon → fix) | T1 coupling, reservation invariant |
| T5 | Add VAT handling for a new country | feature | tax module, T3 discount ordering |
| T6 | Migrate `stock_reservation` to a new schema without downtime | data migration | coupling T1 ↔ Saleor, the migration decision |
| T7 | **Repeat-class:** a promotion type similar to T3 ("spend 100, get 10% off") | feature | lessons from T3's review findings |
| T8 | Security: restrict staff API tokens to their warehouse | security change | the governed decision "auth changes need security sign-off" (recorded in T2/T5) |
| T9 | **Repeat-class incident:** a different oversell path (returns re-stock twice) | incident | T4's root cause and runbook |
| T10 | Performance: checkout p95 > 1 s under load; fix the N+1 queries | performance | topology, hot paths, past alerts |
| T11 | Refactor: split the checkout "complete" mutation into a service class | refactor | blast radius, invariants |
| T12 | Jev decision drill: 10 operator messages where the right action depends on precedent | decisions | decision precedents and outcomes |

**Repeat-class pairs:** T3→T7 and T4→T9. These measure *learning*. Arm B should solve the second faster, cheaper and without repeating the first one's review findings.

## 4. Protocol

- **Arms.**
  - **A:** BOSS as of the pinned commit, knowledge features off.
  - **B:** the same commit with the knowledge graph on (code graph, lifecycle graph, Archivist, packs, Jev precedent).

  Both use the same workflows, gates, reviewers, budgets and approvals. The *person's* approvals are scripted identically, using the same answers file, so a human never biases one arm.
- **Engines.**
  - **E1:** Claude Code (Sonnet 5).
  - **E2:** an open model via an OpenAI-compatible API key, for example Qwen3-Coder or DeepSeek-Coder, pinned by name and date. E2 tests the "helps open models most" hypothesis.
- **Isolation.**
  - Each arm × engine runs on a fresh BOSS install and a fresh Saleor fork at the same SHA, with no cache shared across arms.
  - Arm B starts with an empty knowledge store plus the code index only. Knowledge must be *earned* during T1–T11.
- **Repeats.**
  - **Full study:** one sequence per arm per engine (4 sequences × 12 tasks = 48 builds), plus **3 extra repeats** of T7, T9 and T10 per arm per engine (36 builds) for variance on the learning tasks.
  - **Cost estimate:** ~84 builds × ~$6–10 ≈ **$500–850** of model spend. This is too much for the current $200 key, so see §6.
- **Measured per build, from `llm_calls`, the build profile and `events`:**
  - solved (hidden suite), gate pass rate, review rounds, number and severity of review findings;
  - dollars (cache read, cache write and output split), tokens, turns, wall time;
  - file reads and rereads, context size per turn (per-turn logging, from BOSSAPP-600);
  - **knowledge:** facts used, fact precision (sampled), pack tokens, ingest cost (counted in arm B's total);
  - **repeat-mistake rate:** findings in T7 or T9 that match a T3 or T4 finding;
  - **Jev precedent accuracy** (T12).
- **Statistics.**
  - Report per-task deltas with the repeats' spread.
  - Use paired comparisons over the 12 tasks (a sign test, or a Wilcoxon signed-rank test on cost per solved task).
  - Never report savings only over the tasks both arms solved; count failed attempts in cost, as LatentGraph's per-resolved-task figure does.

## 5. Success criteria (decided before running)

**Primary.** Both must hold on **both** engines.
1. **Outcome:** B solves ≥ A tasks, and on E2 B solves **≥ 2 more** of 12.
2. **Efficiency:** B's **cost per solved task ≤ 0.8 × A's** (≥ 20% cheaper), including ingest cost.

**Secondary.**
- **Learning:** in B, T7 costs ≤ 70% of T3 and T9 ≤ 70% of T4 (normalised by A's same pairs). The repeat-mistake rate is ≤ 50% of A's.
- **Decisions:** Jev's T12 accuracy in B is ≥ A + 20 points, and every B decision cites facts.
- **Quality:** no increase in high-severity review findings or post-release incidents in B.
- **Speed:** B's median wall time per solved task ≤ 0.85 × A's.

If the primary criteria fail, the feature does not ship as on-by-default. The report says why, per component (code graph, lifecycle, Archivist, Jev), from ablations of the T7 and T9 repeats.

## 6. Staged execution (to fit the budget)

1. **Smoke.** T1–T4, arms A and B, once each. This proves the harness, the hidden suites and ingestion.
   - **Operator decision, 2026-10-21:** not approved yet. It runs after Phases 1–3 (arm B needs the knowledge switch).
   - It runs on the open-model engine E2 first (≈$5–15). The Sonnet 5 smoke on E1 (≈$48–80) comes after.
2. **Learning core (≈$150).** E1 and E2, T3→T7 and T4→T9 pairs, arms A and B, plus 3 repeats of T7 and T9. This is the cheapest test of "it learns".
3. **Full (remaining).** All 12 tasks on both engines. Run when budget allows (BOSSAPP-600's token savings lower the cost of every stage).

The harness must support resuming at any task, and running a single task × arm × engine.

## 7. Harness (to build in Phase 0)

- `scripts/case-study/knowledge/`:
  - `tasks/Txx/brief.md`, `tasks/Txx/hidden_tests/`, `tasks/Txx/fault.patch` (T4, T9);
  - `answers.yaml` (scripted human approvals);
  - `run.py --arm A|B --engine E1|E2 --tasks T1..T12 [--repeat n]`;
  - `report.py` (writes `report.md` with tables and charts as SVG).
- The harness drives BOSS **through its own surfaces** (Ask BOSS, workflows, decisions API). It never calls internals, so the demo shows the real product.
- Results are stored under `artifacts/case-study/knowledge/<date>/` (gitignored). The final `report.md` is committed to `docs/case-studies/`.

## 8. The demo it becomes (enterprise retail)

A 10–12 minute film in the existing demo style (Kokoro narration, the console filmed crisp):
1. The NorthPeak brief, BOSS on a 250 MB codebase, and the code graph built in minutes (the explorer filmed).
2. T2 click & collect: the reviewer's "why" panel shows the checkout invariants it respected.
3. T4 oversell incident: Beacon's root cause becomes a governed fact.
4. **T9: the same class of bug is caught and fixed in a fraction of the time.** The "based on" panel cites T4. This is the money shot.
5. T12: Jev decides with precedent, and its citations are shown.
6. The scoreboard: A vs B on both engines, with costs, times, solves and the learning curve, all from the real report.

## 9. Ownership and hand-off

- The implementing agent (cloud) builds Phase 0 and the harness, then runs the **smoke** stage only after the operator approves the spend.
- Every stage's spend is reserved through BOSS's budget, and the operator approves each stage.
- Reports go in the PR description and the final message; the operator copies them to Plane.

"""THE HARNESS BENCHMARK (BOSSAPP-959, phase 0 of the harness kernel epic BOSSAPP-958).

One fixed suite of tasks (bench/harness/suite.yaml), run through BOSS's REAL governed build
pipeline on one engine, measured from BOSS's own records (llm_calls, agent_turns, code_task_runs,
quality_gates, events, decisions) and from acceptance checks the benchmark runs itself, stored
(harness_bench_runs / harness_bench_results and a JSON report), and compared with a named
baseline under stated noise and guard rules. Every later harness change is accepted or rejected
by this comparison, so nothing here takes an agent's word for anything.

    suite.py    load, validate and fingerprint the suite (fixtures pinned by sha256)
    metrics.py  what one task run measured, read from the database
    compare.py  baseline vs candidate: means, spread, noise bands, guards, verdict
    store.py    the two tables, the JSON report, baselines, and the harness score per engine

The runner (it needs the e2e harness's local GitHub) is scripts/harness_bench.py, entered as
`scripts/harness-bench`. docs/architecture/HARNESS_BENCHMARK.md explains how to use it.
"""
from __future__ import annotations

LEVELS = ("delegate",)
"""Scaffold levels a run can be measured at. Only `delegate` exists today (hand the engine the
work and verify it); phase 3 adds `guided` and `stepwise` (docs/architecture/HARNESS_BENCHMARK.md)."""

REPORT_SCHEMA = "boss-harness-bench/1"

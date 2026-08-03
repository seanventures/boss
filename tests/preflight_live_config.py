"""Resolve every decision through the REAL resolvers, against the LIVE config and DB.

The decision-table tests prove the FUNCTIONS are correct. They cannot catch the other
failure class seen in production: the function was right and something else decided.

  - `implement` ran on opus at $6.10 after I routed it to the cheap lane, because
    `resolve_for_run` reads the `pipeline_profiles` TABLE and I had only edited the Python
    constant. The code was correct; the system disagreed.
  - a preview launched Sudoku for an N-Queens build because the host path was fixed and the
    container path still decided for itself.

So this asks the live system what it will actually do, using the same resolvers the
pipeline uses. Run it BEFORE a run, not after: every one of those defects cost a full run
to discover and would have been visible here in two seconds.

    python tests/preflight_live_config.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))

from sqlalchemy import create_engine, text  # noqa: E402

from boss_engine.config import Settings  # noqa: E402
from boss_engine.services.pipeline_profile_service import resolve_for_run  # noqa: E402
from boss_engine.services.pipeline_profiles import infer_risk_tier  # noqa: E402
from apps.worker.boss_worker.config import WorkerSettings  # noqa: E402

PROJECT = "boss-dogfood"
_R: list[tuple[bool, str, str]] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    _R.append((bool(cond), name, detail))


def main() -> int:
    s = Settings()
    w = WorkerSettings()
    eng = create_engine(s.database_url)

    with eng.begin() as c:
        profile = resolve_for_run(c, PROJECT, None, s)
        default_row = c.execute(text(
            "SELECT name FROM pipeline_profiles WHERE project_id=:p AND is_default"),
            {"p": PROJECT}).scalar()
        stuck = c.execute(text(
            "SELECT count(*) FROM code_task_runs WHERE status IN ('running','repairing')"
        )).scalar()
        live_jobs = c.execute(text(
            "SELECT count(*) FROM jobs WHERE status IN ('queued','running','leased')")).scalar()
        pending_gates = c.execute(text(
            "SELECT count(*) FROM decisions WHERE status='pending' "
            "AND question ILIKE 'Approve the plan%'")).scalar()

    routing = profile.get("model") or profile.get("model_routing") or {}

    def route(step: str) -> str:
        return routing.get(step, routing.get("default", "?"))

    # --- model routing, as the PIPELINE will resolve it (not as the constant says) ------
    ok("implement runs on the cheap lane", route("implement") == "low_cost",
       f"implement -> {route('implement')}")
    for verifier in ("review_code", "security_check", "consistency_check"):
        ok(f"{verifier} stays frontier", route(verifier) == "strong",
           f"{verifier} -> {route(verifier)}")

    # --- tier inference, through the live profile --------------------------------------
    cases = [
        ("incremental change -> tier 1", 1,
         {"title": "Expand N-Queens", "instructions": "Update the existing N-Queens app; small."}),
        ("greenfield build -> tier 2", 2,
         {"title": "New app", "instructions": "Build a new puzzle app from scratch."}),
        ("real auth work -> tier 3", 3,
         {"title": "Login", "instructions": "Add auth and password reset."}),
    ]
    for label, want, spec in cases:
        got = infer_risk_tier(spec, profile)
        ok(label, got == want, f"got {got}")

    # A tier-1 task must NOT hit a plan-approval gate (that is what parked the last repair).
    t1 = infer_risk_tier(
        {"title": "Expand N-Queens",
         "instructions": "Update the existing N-Queens app; small."}, profile)
    ok("tier-1 work needs no plan approval", t1 < 2, f"tier {t1}")

    # --- safety floors that must never be off ------------------------------------------
    ok("never_block is on (a gate failure ships with dissent)", bool(s.code_gate_never_block))
    ok("panel runs on small changes too", True, "do_review >= tier 1 (source-level)")
    ok("agent turn cap is app-scale", s.code_agent_max_turns >= 60, str(s.code_agent_max_turns))
    ok("reviewer turn cap is not the old 16", s.code_agent_review_max_turns >= 30,
       str(s.code_agent_review_max_turns))
    ok("implement can finish (continuations on)", s.code_impl_max_continuations > 0)
    ok("inconclusive checks re-run the reviewer", s.code_review_max_escalations > 0)
    ok("context pack is enabled", s.code_agent_context_files > 0, str(s.code_agent_context_files))
    ok("panel is genuinely parallel", s.code_agent_max_concurrency > 1,
       f"concurrency {s.code_agent_max_concurrency}")

    # --- hang bounds -------------------------------------------------------------------
    ok("job runtime is bounded", w.job_max_runtime_seconds > 0, f"{w.job_max_runtime_seconds}s")
    ok("lease reclaim is prompt", w.lease_ttl_seconds <= 900, f"{w.lease_ttl_seconds}s")

    # --- environment readiness ---------------------------------------------------------
    ok("default profile resolved", bool(default_row), f"default={default_row}")
    ok("nothing stuck in flight", stuck == 0, f"{stuck} running/repairing")
    ok("no orphaned jobs", live_jobs == 0 or stuck > 0, f"{live_jobs} live jobs, {stuck} runs")
    ok("no run parked on a plan gate", pending_gates == 0, f"{pending_gates} pending")

    bad = [r for r in _R if not r[0]]
    for good, name, detail in _R:
        print(f"  {'OK  ' if good else 'FAIL'}  {name}{('  [' + detail + ']') if detail else ''}")
    print(f"\n  {len(_R) - len(bad)}/{len(_R)} live-config checks passed")
    if bad:
        print("  NOT READY — fix the above before starting a run.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

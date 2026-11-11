"""Where benchmark runs live: harness_bench_runs / harness_bench_results, a JSON report per run,
and named baselines (bench/harness/baselines/<name>.json, or a run saved under that name).

A report is one JSON document:
    {"schema": "boss-harness-bench/1",
     "run": {id, suite, suite_version, suite_sha256, engine, model, level, boss_sha, boss_dirty,
             config, config_fingerprint, rules, repeat, task_ids, budget_usd, status,
             started_at, finished_at, baseline_name},
     "results": [{task_id, kind, iteration, code_task_run_id, success, components, metrics, ...}],
     "summary": {metric: {n, mean, stdev, min, max}}}
The database rows hold the same: the run's columns plus the report, one row per task run.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text

from . import REPORT_SCHEMA
from .compare import compare, pass_values, stats
from .suite import BASELINES_DIR, engine_class

_RESULT_COLUMNS = ("wall_s", "tokens_in", "tokens_out", "cache_read_tokens", "cache_write_tokens",
                   "cost_usd", "model_calls", "turns", "tool_calls", "repair_rounds",
                   "interventions")


def new_run_id() -> str:
    return f"hbr_{uuid.uuid4().hex[:20]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = sorted({r["task_id"] for r in results if not r.get("skipped")})
    values = pass_values(results, tasks) if tasks else {}
    return {"tasks": tasks, "runs": sum(1 for r in results if not r.get("skipped")),
            "skipped": sum(1 for r in results if r.get("skipped")),
            "successes": sum(1 for r in results if r.get("success")),
            "metrics": {k: stats(v) for k, v in values.items()}}


# ── the database ─────────────────────────────────────────────────────────────────────────────

def create_run(connection, run: dict[str, Any]) -> None:
    connection.execute(text("""
        INSERT INTO harness_bench_runs (id, suite_name, suite_version, suite_sha256, engine, model,
            level, boss_sha, boss_dirty, config, config_fingerprint, rules, task_ids, repeat,
            budget_usd_micros, status, project_id)
        VALUES (:id, :suite, :version, :sha, :engine, :model, :level, :boss_sha, :dirty,
                CAST(:config AS JSONB), :fp, CAST(:rules AS JSONB), CAST(:tasks AS JSONB),
                :repeat, :budget, 'running', :project)
    """), {"id": run["id"], "suite": run["suite"], "version": run["suite_version"],
           "sha": run["suite_sha256"], "engine": run["engine"], "model": run.get("model"),
           "level": run["level"], "boss_sha": run.get("boss_sha"),
           "dirty": bool(run.get("boss_dirty")), "config": json.dumps(run.get("config") or {}),
           "fp": run["config_fingerprint"], "rules": json.dumps(run.get("rules") or {}),
           "tasks": json.dumps(run.get("task_ids") or []), "repeat": int(run.get("repeat") or 1),
           "budget": (int(round(float(run["budget_usd"]) * 1_000_000))
                      if run.get("budget_usd") is not None else None),
           "project": run.get("project_id")})


def add_result(connection, run_id: str, r: dict[str, Any]) -> None:
    m = r.get("metrics") or {}
    params = {k: m.get(k) for k in _RESULT_COLUMNS}
    params["cost_usd_micros"] = int(round(float(m.get("cost_usd") or 0) * 1_000_000))
    params["wall_ms"] = int(round(float(m.get("wall_s") or 0) * 1000))
    connection.execute(text("""
        INSERT INTO harness_bench_results (id, bench_run_id, task_id, task_kind, iteration,
            code_task_run_id, success, skipped, status, wall_ms, tokens_in, tokens_out,
            cache_read_tokens, cache_write_tokens, cost_usd_micros, model_calls, turns, tool_calls,
            repair_rounds, interventions, components, gates, acceptance, detail, error)
        VALUES (:id, :run, :task, :kind, :it, :crun, :success, :skipped, :status, :wall_ms,
                :tokens_in, :tokens_out, :cache_read_tokens, :cache_write_tokens,
                :cost_usd_micros, :model_calls, :turns, :tool_calls, :repair_rounds,
                :interventions, CAST(:components AS JSONB), CAST(:gates AS JSONB),
                CAST(:acceptance AS JSONB), CAST(:detail AS JSONB), :error)
    """), {**{k: int(params[k] or 0) for k in ("tokens_in", "tokens_out", "cache_read_tokens",
                                               "cache_write_tokens", "model_calls", "turns",
                                               "tool_calls", "repair_rounds", "interventions")},
           "cost_usd_micros": params["cost_usd_micros"], "wall_ms": params["wall_ms"],
           "id": f"hbx_{uuid.uuid4().hex[:20]}", "run": run_id, "task": r["task_id"],
           "kind": r.get("kind"), "it": int(r.get("iteration") or 1),
           "crun": r.get("code_task_run_id"), "success": bool(r.get("success")),
           "skipped": bool(r.get("skipped")), "status": r.get("status"),
           "components": json.dumps(r.get("components") or {}),
           "gates": json.dumps(r.get("gates") or {}),
           "acceptance": json.dumps(r.get("acceptance") or {}),
           "detail": json.dumps(r, default=str), "error": (r.get("error") or None)})


def finish_run(connection, run_id: str, *, status: str, summary: dict[str, Any],
               report_path: Optional[str]) -> None:
    connection.execute(text(
        "UPDATE harness_bench_runs SET status = :s, summary = CAST(:sum AS JSONB), "
        "report_path = :p, finished_at = now() WHERE id = :i"),
        {"s": status, "sum": json.dumps(summary, default=str), "p": report_path, "i": run_id})


def record_baseline(connection, run_id: str, name: str) -> None:
    connection.execute(text("UPDATE harness_bench_runs SET baseline_name = :n WHERE id = :i"),
                       {"n": name, "i": run_id})


def record_comparison(connection, run_id: str, baseline: str, cmp: dict[str, Any]) -> None:
    connection.execute(text(
        "UPDATE harness_bench_runs SET compared_to = :b, verdict = :v, "
        "comparison = CAST(:c AS JSONB) WHERE id = :i"),
        {"b": baseline, "v": cmp.get("verdict"), "c": json.dumps(cmp, default=str), "i": run_id})


def _run_meta(row: dict[str, Any]) -> dict[str, Any]:
    def j(v):
        return json.loads(v) if isinstance(v, str) else v
    return {
        "id": row["id"], "suite": row["suite_name"], "suite_version": row["suite_version"],
        "suite_sha256": row["suite_sha256"], "engine": row["engine"], "model": row.get("model"),
        "level": row["level"], "boss_sha": row.get("boss_sha"),
        "boss_dirty": bool(row.get("boss_dirty")), "config": j(row.get("config")) or {},
        "config_fingerprint": row["config_fingerprint"], "rules": j(row.get("rules")) or {},
        "task_ids": j(row.get("task_ids")) or [], "repeat": row.get("repeat"),
        "budget_usd": (row["budget_usd_micros"] / 1_000_000
                       if row.get("budget_usd_micros") is not None else None),
        "status": row["status"], "baseline_name": row.get("baseline_name"),
        "compared_to": row.get("compared_to"), "verdict": row.get("verdict"),
        "started_at": str(row.get("started_at") or ""),
        "finished_at": str(row.get("finished_at") or ""),
    }


def load_report(connection, run_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text("SELECT * FROM harness_bench_runs WHERE id = :i"),
                             {"i": run_id}).mappings().first()
    if row is None:
        return None
    results = []
    for r in connection.execute(text(
            "SELECT detail FROM harness_bench_results WHERE bench_run_id = :i "
            "ORDER BY iteration, created_at, id"), {"i": run_id}).mappings():
        d = r["detail"]
        results.append(json.loads(d) if isinstance(d, str) else d)
    return {"schema": REPORT_SCHEMA, "run": _run_meta(dict(row)), "results": results,
            "summary": summarize(results)}


def list_runs(connection, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT * FROM harness_bench_runs ORDER BY started_at DESC LIMIT :n"),
        {"n": max(1, min(int(limit), 500))}).mappings().all()
    return [_run_meta(dict(r)) for r in rows]


# ── files and baselines ──────────────────────────────────────────────────────────────────────

def write_report(path: Path, report: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1, sort_keys=False, default=str) + "\n",
                    encoding="utf-8")
    return path


def read_report(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != REPORT_SCHEMA:
        raise ValueError(f"{path} is not a harness benchmark report ({REPORT_SCHEMA})")
    return data


def baseline_path(name: str, baselines_dir: Path = BASELINES_DIR) -> Path:
    return Path(baselines_dir) / f"{name}.json"


def load_baseline(name: str, connection=None, *,
                  baselines_dir: Path = BASELINES_DIR) -> Optional[dict[str, Any]]:
    """A baseline by name: a report file path, bench/harness/baselines/<name>.json, or the latest
    run saved under that name in the database. None when there is no such baseline."""
    p = Path(name)
    if name.endswith(".json") or "/" in name:
        return read_report(p) if p.exists() else None
    f = baseline_path(name, baselines_dir)
    if f.exists():
        return read_report(f)
    if connection is not None:
        rid = connection.execute(text(
            "SELECT id FROM harness_bench_runs WHERE baseline_name = :n "
            "ORDER BY started_at DESC LIMIT 1"), {"n": name}).scalar()
        if rid:
            return load_report(connection, rid)
    return None


# ── the harness score: the latest run per engine and level, against its baseline ───────────

def default_baseline_name(engine: str, level: Optional[str]) -> str:
    """The baseline a run is scored against when it named none: the engine class for the
    pipeline's own level, `<engine class>-<level>` for any other (scripted-bare, claude_code-bare
    ...), so a bare run is never scored against a pipeline baseline by accident."""
    base = engine_class(engine)
    return base if (level or "delegate") == "delegate" else f"{base}-{level}"


def harness_score(connection, *, baselines_dir: Path = BASELINES_DIR) -> list[dict[str, Any]]:
    """One row per (engine, level): the latest completed run and its verdict per metric against
    the baseline it was compared with, else the default baseline for its engine class and level
    (default_baseline_name)."""
    rows = connection.execute(text("""
        SELECT DISTINCT ON (engine, level) id, engine, level, compared_to
          FROM harness_bench_runs WHERE status = 'completed'
         ORDER BY engine, level, started_at DESC
    """)).mappings().all()
    out = []
    for row in rows:
        cand = load_report(connection, row["id"])
        name = row.get("compared_to") or default_baseline_name(row["engine"], row["level"])
        base = load_baseline(name, connection, baselines_dir=baselines_dir)
        entry: dict[str, Any] = {"engine": row["engine"], "level": row["level"],
                                 "run": cand["run"], "baseline": name}
        if base is None or (base.get("run") or {}).get("id") == cand["run"]["id"]:
            entry.update(verdict="no_baseline", metrics={}, guards=[])
        else:
            cmp = compare(base, cand, cand["run"].get("rules") or {})
            entry.update(verdict=cmp["verdict"], reasons=cmp.get("reasons") or [],
                         metrics={k: {"baseline": v["baseline"]["mean"],
                                      "candidate": v["candidate"]["mean"], "delta": v["delta"],
                                      "noise_band": v["noise_band"], "verdict": v["verdict"]}
                                  for k, v in cmp.get("metrics", {}).items()},
                         guards=cmp.get("guards") or [])
        out.append(entry)
    return out


def render_score(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no completed harness benchmark run in this database"
    lines = []
    for e in rows:
        run = e["run"]
        lines.append(f"{e['engine']} / {e['level']}: {e['verdict'].upper()} vs {e['baseline']}  "
                     f"(run {run['id']}, boss {str(run.get('boss_sha') or '')[:10]}, "
                     f"config {run.get('config_fingerprint')}, {run.get('finished_at')})")
        for name, m in (e.get("metrics") or {}).items():
            lines.append(f"    {name:<14} {m['baseline']!s:>12} -> {m['candidate']!s:<12} "
                         f"{m['verdict']}")
        for g in e.get("guards") or []:
            if g["status"] == "fail":
                lines.append(f"    guard FAILED: {g['metric']} {g['rule']}")
        for r in e.get("reasons") or []:
            lines.append(f"    not comparable: {r}")
    return "\n".join(lines)

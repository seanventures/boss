"""GOVERNED VERSUS BARE, WITH NOISE BANDS: the harness bench's product claim as an API (AP-P4).

docs/design/AGENT_PLATFORM.md §4.9 ("Harness bench"), §5 Phase 4 acceptance (d). The thesis is
"harness efficiency is the product": the same engine run through BOSS's governed pipeline
(`level: delegate`) against its own bare loop (`level: bare`), metric by metric, a difference
counted only when it is larger than its noise band (harness_bench/compare.py — the rules in
bench/harness/suite.yaml, the same comparison `scripts/harness_bench.py --baseline` prints).

WHERE THE NUMBERS COME FROM. Only RECORDED runs: no request here starts a benchmark (a real-engine
run costs money and an hour). In order:

  1. bench/harness/baselines/recorded/<engine>-<level>.json — real-engine reports exported from
     the bench database and committed (the image ships them);
  2. the newest completed run in this install's `harness_bench_runs` for that engine and level,
     when the install has run the bench itself.

The answer says which (`source`), when each run was recorded, how many passes it had (one pass
has no spread: only the floors guard against noise, and the comparison says so in `notes`).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

LEVELS = ("delegate", "bare")


def recorded_dir() -> Path:
    from ..harness_bench.suite import BASELINES_DIR

    return BASELINES_DIR / "recorded"


def _slug(engine: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in engine).strip("-")


def _from_file(engine: str, level: str) -> Optional[dict]:
    path = recorded_dir() / f"{_slug(engine)}-{level}.json"
    if not path.is_file():
        return None
    report = json.loads(path.read_text())
    return {"report": report, "source": "recorded_file", "path": f"{path.parent.name}/{path.name}"}


def _from_db(connection: Any, engine: str, level: str) -> Optional[dict]:
    from sqlalchemy import text

    from ..harness_bench import store

    try:
        with connection.begin_nested():
            run_id = connection.execute(text(
                "SELECT id FROM harness_bench_runs WHERE engine = :e AND level = :l "
                "AND status = 'completed' ORDER BY finished_at DESC NULLS LAST LIMIT 1"),
                {"e": engine, "l": level}).scalar()
            report = store.load_report(connection, run_id) if run_id else None
    except Exception:  # noqa: BLE001 — an install that never ran the bench
        report = None
    return {"report": report, "source": "harness_bench_runs"} if report else None


def engines() -> list:
    """Engines with BOTH levels recorded in the committed files."""
    found: dict = {}
    folder = recorded_dir()
    if folder.is_dir():
        for path in folder.glob("*.json"):
            try:
                run = json.loads(path.read_text()).get("run") or {}
            except ValueError:
                continue
            found.setdefault(str(run.get("engine")), set()).add(str(run.get("level")))
    return sorted(e for e, levels in found.items() if set(LEVELS) <= levels)


def _meta(got: dict) -> dict:
    run = got["report"].get("run") or {}
    summary = got["report"].get("summary") or {}
    return {"source": got["source"], "path": got.get("path"), "run_id": run.get("id"),
            "engine": run.get("engine"), "model": run.get("model"), "level": run.get("level"),
            "repeat": run.get("repeat"), "tasks": len(run.get("task_ids") or []),
            "boss_sha": run.get("boss_sha"), "suite_sha256": run.get("suite_sha256"),
            "started_at": run.get("started_at"), "finished_at": run.get("finished_at"),
            "successes": summary.get("successes"), "runs": summary.get("runs")}


def governed_vs_bare(engine: str, *, connection: Any = None) -> Optional[dict]:
    """The comparison for one engine, or None when it has not been recorded at both levels.
    Baseline = bare, candidate = governed: `better` means BOSS's pipeline beat the bare loop
    beyond noise."""
    from ..harness_bench.compare import compare
    from ..harness_bench.suite import load_suite, rules_for

    pair = {}
    for level in LEVELS:
        got = _from_file(engine, level)
        if got is None and connection is not None:
            got = _from_db(connection, engine, level)
        if got is None:
            return None
        pair[level] = got
    rules = rules_for(load_suite(), engine)
    cmp = compare(pair["bare"]["report"], pair["delegate"]["report"], rules,
                  allow_cross_level=True)
    metrics = {name: {"better_is": m["better_is"], "unit": m["unit"],
                      "bare": {k: m["baseline"][k] for k in ("n", "mean", "stdev", "min", "max")},
                      "governed": {k: m["candidate"][k] for k in ("n", "mean", "stdev", "min",
                                                                   "max")},
                      "delta": m["delta"], "delta_pct": m["delta_pct"],
                      "noise_band": m["noise_band"], "verdict": m["verdict"]}
               for name, m in (cmp.get("metrics") or {}).items()}
    return {"engine": engine, "baseline": "bare", "candidate": "governed",
            "governed": _meta(pair["delegate"]), "bare": _meta(pair["bare"]),
            "verdict": cmp.get("verdict"), "moved": cmp.get("moved"),
            "tasks": cmp.get("tasks"), "metrics": metrics, "guards": cmp.get("guards"),
            "notes": cmp.get("notes"), "reasons": cmp.get("reasons"),
            "rules": {"k_stdev": rules.get("k_stdev")}}


__all__ = ["engines", "governed_vs_bare", "recorded_dir"]

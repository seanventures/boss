"""The keyword decision eval can fail (evals/task_rigor/run.py --check, thresholds.json).

CI's eval-decisions workflow runs `make eval-decisions`, which passes --check: a family whose
accuracy falls below its floor, or whose false negatives rise above the most allowed, exits 1.
The floors are today's measured numbers (rounded down by less than one case), so the eval passes
now and any rule that gets worse fails it.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "evals" / "task_rigor" / "run.py"
THRESHOLDS = json.loads((ROOT / "evals" / "task_rigor" / "thresholds.json").read_text())


def _runner():
    sys.path.insert(0, str(ROOT / "engine"))
    spec = importlib.util.spec_from_file_location("task_rigor_run", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(*args):
    env = {**os.environ, "PYTHONPATH": "engine:."}
    return subprocess.run([sys.executable, str(RUNNER), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, check=False)


def test_today_the_eval_is_at_its_floors():
    done = _run("--check")
    assert done.returncode == 0, done.stderr
    assert "at or above its floor" in done.stderr


def test_without_check_the_table_is_unchanged_and_never_fails():
    assert _run().returncode == 0
    def table(out):   # the p95 latency column is measured, so it differs run to run
        return [line.rsplit("|", 2)[0] for line in out.splitlines()]

    assert table(_run().stdout) == table(_run("--check").stdout)


def test_one_more_miss_in_any_family_fails():
    mod = _runner()
    for family, floor in THRESHOLDS["families"].items():
        corpus = json.loads((RUNNER.parent / "corpus.json").read_text())
        total = len(corpus[family])
        correct = math.ceil(round(float(floor["min_accuracy"]) * total, 6)) - 1
        scores = {f: (total, 0, total, 0.0) for f in THRESHOLDS["families"]}
        scores[family] = (correct, int(floor["max_false_negatives"]), total, 0.0)
        failures = mod.check(scores, THRESHOLDS)
        assert any(family in f and "accuracy" in f for f in failures), (family, failures)
        scores[family] = (total, int(floor["max_false_negatives"]) + 1, total, 0.0)
        assert any(family in f and "false negatives" in f for f in mod.check(scores, THRESHOLDS))


def test_the_workflow_runs_the_check():
    make = (ROOT / "Makefile").read_text()
    assert "evals/task_rigor/run.py --check" in make
    assert "make eval-decisions" in (ROOT / ".github" / "workflows" / "eval-decisions.yml").read_text()

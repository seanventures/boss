"""The keyword decision eval reports production-rule scores and leaves model rows unmeasured.

A change to the runner's printed table, the corpus labels, or the production keyword
functions is what makes these tests fail. They call those functions; they do not copy them.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "evals" / "task_rigor" / "run.py"
CORPUS = ROOT / "evals" / "task_rigor" / "corpus.json"
BASE = "73d3ebecc6d5f65763a8071ded79f67c76206667"

FAMILIES = ("sensitivity", "rigor", "look", "verdict")
COLUMNS = ("accuracy", "false_negatives", "p95_latency_ms", "cost_per_call")
MODEL_ROWS = ("jev", "jev+claude", "claude")
NOT_MEASURED = "not measured"
NUMERIC = re.compile(r"^\d+(?:\.\d+)?$")

# Hard cases stay in the corpus even when the keyword rule misses them. Dropping one
# to make the false-negative count zero is a failure, not a better score.
REQUIRED_SENSITIVITY = {
    "Add authentication to the API": True,
    "Store user passwords hashed": True,
    "Rotate API tokens and secrets": True,
    "Add OAuth sign-in with Google": True,
    "Add Stripe checkout": True,
    "Run the database migrations": True,
    "Change the button label to Save": False,
    "Rename the heading on the about page": False,
}
REQUIRED_RIGOR = {
    "Restyle the login button colour": 0,
    "Adjust the existing theme: drop the CSRF middleware": 3,
    "Fix a typo in the readme": 0,
    "Add a small validation rule to the existing form": 1,
    "Build a new status page for the existing app": 2,
    "Add authentication to the API": 3,
}
REQUIRED_LOOK = {
    "Why is it stuck? Stop, do not touch anything, just tell me": "look",
    "As I said, it is slow. What is going on?": "look",
    "I told you it was slow yesterday too, why?": "look",
    "Can you check why the build is slow?": "look",
    "Fix and retry the deployment": "act",
}
# The review-verdict grammar's hard cases: the substring parse (`"REVISE" not in summary`) got
# the first two wrong on every run measured, and a bare unsubstantiated refutation must not
# fail-close the gate. Dropping one to make the score perfect is a failure, not a better score.
REQUIRED_VERDICT = {
    "No need to revise anything; the sequencing is right.\nPLAN: OK": "ok",
    "I considered saying PLAN: REVISE for the test section, but it is adequate.\nPLAN: OK": "ok",
    "Section 2 skips the migration ordering and must be rewritten.\nPLAN: REVISE": "revise",
    "VERDICT: REFUTED": "survived",
}


def _load_corpus() -> dict:
    assert CORPUS.is_file(), "evals/task_rigor/corpus.json is missing"
    return json.loads(CORPUS.read_text())


def _spec(row: dict) -> dict:
    return {
        "title": row.get("title", ""),
        "instructions": row.get("instructions", ""),
        "acceptance_criteria": row.get("acceptance_criteria", []) or [],
        "repo": row.get("repo", ""),
    }


def _task_sentence(row: dict) -> str:
    from boss_engine.services.pipeline_profiles import _task_haystack

    return " ".join(_task_haystack(_spec(row)).split())


def _open_profile() -> dict:
    from boss_engine.services.pipeline_profiles import PROFILES

    matches = [p for p in PROFILES.values() if p.get("tier_floor") == 0 and p.get("tier_cap") == 3]
    assert len(matches) == 1
    return matches[0]


def _keyword_sensitive(spec: dict) -> bool:
    from boss_engine.services.pipeline_profiles import SENSITIVE_MARKERS, _matches, _task_haystack

    haystack = _task_haystack(spec)
    return any(_matches(marker, haystack) for marker in SENSITIVE_MARKERS)


def _expected_sensitivity(rows: list[dict]) -> tuple[int, int, int]:
    false_negatives = 0
    correct = 0
    for row in rows:
        predicted = _keyword_sensitive(_spec(row))
        label = bool(row["label"])
        if label and not predicted:
            false_negatives += 1
        if predicted == label:
            correct += 1
    return correct, false_negatives, len(rows)


def _expected_rigor(rows: list[dict]) -> tuple[int, int, int]:
    from boss_engine.services.pipeline_profiles import infer_risk_tier

    profile = _open_profile()
    false_negatives = 0
    correct = 0
    for row in rows:
        predicted = infer_risk_tier(_spec(row), profile)
        label = int(row["label"])
        if label == 3 and predicted < 3:
            false_negatives += 1
        if predicted == label:
            correct += 1
    return correct, false_negatives, len(rows)


def _expected_look(rows: list[dict]) -> tuple[int, int, int]:
    from boss_engine.services.intent_router import asked_only_to_look

    false_negatives = 0
    correct = 0
    for row in rows:
        predicted_look = asked_only_to_look(row["text"])
        label_look = row["label"] == "look"
        if label_look and not predicted_look:
            false_negatives += 1
        if predicted_look == label_look:
            correct += 1
    return correct, false_negatives, len(rows)


def _expected_verdict(rows: list[dict]) -> tuple[int, int, int]:
    from boss_engine.services.review_verdict import parse_verdict, plan_verdict

    false_negatives = 0
    correct = 0
    for row in rows:
        fn = plan_verdict if row["kind"] == "plan" else parse_verdict
        predicted = fn(row["text"])
        if row["label"] in ("revise", "refuted") and predicted != row["label"]:
            false_negatives += 1
        if predicted == row["label"]:
            correct += 1
    return correct, false_negatives, len(rows)


def _run_report() -> str:
    assert RUNNER.is_file(), "evals/task_rigor/run.py is missing"
    env = os.environ.copy()
    env["PYTHONPATH"] = "engine:."
    proc = subprocess.run(
        [sys.executable, str(RUNNER)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _parse_report(text: str) -> dict[str, dict[str, dict[str, str]]]:
    sections: dict[str, dict[str, dict[str, str]]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("method"):
            continue
        if line.startswith("family "):
            current = line.split(" ", 1)[1]
            assert current in FAMILIES
            sections[current] = {}
            continue
        assert current is not None, line
        cells = [part.strip() for part in line.split("|")]
        assert len(cells) == 1 + len(COLUMNS), line
        method, *values = cells
        sections[current][method] = dict(zip(COLUMNS, values))
    return sections


def test_corpus_keeps_the_hard_cases():
    corpus = _load_corpus()
    for text, label in REQUIRED_SENSITIVITY.items():
        assert any(
            _task_sentence(row) == text.lower() and bool(row["label"]) is label
            for row in corpus["sensitivity"]
        ), text
    for text, label in REQUIRED_RIGOR.items():
        assert any(
            _task_sentence(row) == text.lower() and int(row["label"]) == label
            for row in corpus["rigor"]
        ), text
    for text, label in REQUIRED_LOOK.items():
        assert any(row["text"] == text and row["label"] == label for row in corpus["look"]), text
    for text, label in REQUIRED_VERDICT.items():
        assert any(row["text"] == text and row["label"] == label
                   for row in corpus["verdict"]), text


def test_report_measures_keyword_rules_and_leaves_models_unmeasured():
    corpus = _load_corpus()
    sections = _parse_report(_run_report())
    assert tuple(sections) == FAMILIES
    expected = {
        "sensitivity": _expected_sensitivity(corpus["sensitivity"]),
        "rigor": _expected_rigor(corpus["rigor"]),
        "look": _expected_look(corpus["look"]),
        "verdict": _expected_verdict(corpus["verdict"]),
    }
    for family, (correct, false_negatives, total) in expected.items():
        assert total > 0
        rows = sections[family]
        assert tuple(rows) == ("keyword",) + MODEL_ROWS
        keyword = rows["keyword"]
        for column in COLUMNS:
            assert NUMERIC.fullmatch(keyword[column]), keyword[column]
        assert math.isclose(float(keyword["accuracy"]), correct / total, rel_tol=0, abs_tol=1e-4)
        assert int(keyword["false_negatives"]) == false_negatives
        assert float(keyword["p95_latency_ms"]) >= 0
        assert float(keyword["cost_per_call"]) == 0
        for method in MODEL_ROWS:
            for column in COLUMNS:
                cell = rows[method][column]
                assert cell == NOT_MEASURED
                assert re.search(r"\d", cell) is None


def test_runner_source_does_not_reference_a_network_client():
    assert RUNNER.is_file(), "evals/task_rigor/run.py is missing"
    source = RUNNER.read_text()
    for banned in ("urllib", "httpx", "requests", "socket"):
        assert banned not in source


def test_keyword_rule_modules_are_not_in_the_diff():
    watched = [
        "engine/boss_engine/services/pipeline_profiles.py",
        "engine/boss_engine/services/intent_router.py",
    ]
    diff = subprocess.check_output(
        ["git", "diff", "--name-only", BASE, "--", *watched],
        cwd=ROOT,
        text=True,
    )
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--", *watched],
        cwd=ROOT,
        text=True,
    )
    assert diff.strip() == ""
    assert status.strip() == ""

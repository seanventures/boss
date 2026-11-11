"""The harness benchmark's suite (bench/harness/suite.yaml) is valid, pinned and hard to fool.

  * it validates, and the validator catches what a careless edit does (a fixture changed without
    re-pinning, a task with no acceptance checks, an unknown fault, a duplicate id);
  * it has the shape the design asks for: 3 fixes and 3 features on existing apps (HelpDesk among
    them) and 2 new apps;
  * every task's held acceptance tests FAIL on the untouched fixture and PASS on the scripted
    engine's solution, so a run that changed nothing cannot pass and the scripted baseline is real;
  * the committed scripted baseline was made on THIS suite (same sha256), completed, and passed.
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from boss_engine.harness_bench.suite import (
    BASELINES_DIR, DEFAULT_SUITE, SuiteError, load_suite, resolve, rules_for, select_tasks,
    tree_files, tree_sha256, validate,
)


@pytest.fixture(scope="module")
def suite():
    return load_suite()


def _raw():
    return yaml.safe_load(DEFAULT_SUITE.read_text(encoding="utf-8"))


def test_the_committed_suite_is_valid_and_pinned(suite):
    assert suite["_sha256"] and len(suite["_sha256"]) == 64
    for name, fx in suite["fixtures"].items():
        assert tree_sha256(resolve(suite, fx["path"])) == fx["sha256"], name


def test_the_suite_has_the_designed_shape(suite):
    kinds = [t["kind"] for t in suite["tasks"]]
    assert kinds.count("fix") == 3 and kinds.count("feature") == 3 and kinds.count("new_app") == 2
    existing = [t for t in suite["tasks"] if t["kind"] in ("fix", "feature")]
    assert all(suite["fixtures"][t["fixture"]]["path"] != "fixtures/empty" for t in existing)
    assert any(t["fixture"] == "helpdesk-lite" for t in existing), "HelpDesk is in the suite"
    assert any((t.get("scripted") or {}).get("faults") for t in suite["tasks"]), \
        "at least one task exercises a fix round"
    assert suite["guards"], "a benchmark that cannot fail is not a gate"


@pytest.mark.parametrize("mutate, words", [
    (lambda r: r["tasks"][0].pop("acceptance"), "acceptance"),
    (lambda r: r["tasks"][0]["scripted"].update(faults=["gremlins"]), "unknown fault"),
    (lambda r: r["tasks"][1].update(id=r["tasks"][0]["id"]), "used twice"),
    (lambda r: r["tasks"][0].update(kind="refactor"), "kind"),
    (lambda r: r["tasks"][0]["acceptance"].update(commands=[]), "commands"),
    (lambda r: r["tasks"][0]["acceptance"].update(hidden="acceptance/nope"), "does not exist"),
    (lambda r: r["tasks"][0]["acceptance"].update(hidden="../../../../etc"), "outside the repository"),
    (lambda r: r["fixtures"]["helpdesk-lite"].update(sha256="0" * 64), "changed since they were pinned"),
    (lambda r: r.update(guards={}), "at least one guard"),
    (lambda r: r["guards"].update(cost_usd={"max_rise_pct": 10, "max_rise": 1}), "exactly one"),
    (lambda r: r["metrics"].update(speed={"abs_floor": 1}), "unknown metric"),
])
def test_the_validator_catches_a_careless_edit(mutate, words):
    raw = _raw()
    mutate(raw)
    errors = validate(raw, DEFAULT_SUITE.parent)
    assert any(words in e for e in errors), errors


def test_a_fixture_edit_without_a_re_pin_is_refused(tmp_path):
    copy_dir = tmp_path / "harness"
    shutil.copytree(DEFAULT_SUITE.parent, copy_dir, ignore=shutil.ignore_patterns("baselines"))
    (copy_dir / "fixtures" / "helpdesk-lite" / "src" / "classify.js").write_text("// edited\n")
    # The copy lives outside the repository, so point the paths back at a valid in-repo layout
    # by validating the parsed copy against its own directory with the repo check relaxed.
    raw = yaml.safe_load((copy_dir / "suite.yaml").read_text())
    from boss_engine.harness_bench import suite as suite_mod

    real = suite_mod.REPO_ROOT
    suite_mod.REPO_ROOT = tmp_path
    try:
        errors = validate(raw, copy_dir)
    finally:
        suite_mod.REPO_ROOT = real
    assert any("helpdesk-lite" in e and "pinned" in e for e in errors), errors


def test_tasks_are_selected_by_id_and_unknown_ids_are_refused(suite):
    assert [t["id"] for t in select_tasks(suite, "new-tempconv,helpdesk-stats")] == \
        ["helpdesk-stats", "new-tempconv"]
    assert len(select_tasks(suite, "all")) == len(suite["tasks"])
    with pytest.raises(SuiteError, match="unknown task"):
        select_tasks(suite, "helpdesk-stats,nope")


def test_engine_overrides_change_only_that_engines_rules(suite):
    real = rules_for(suite, "claude_code")
    scripted = rules_for(suite, "scripted")
    assert "wall_s" in real["guards"] and "wall_s" not in scripted["guards"]
    assert "model_calls" in scripted["guards"] and "model_calls" not in real["guards"]
    assert rules_for(suite, "native:qwen")["guards"] == real["guards"]


# ── the acceptance tests are held, and they discriminate ─────────────────────────────────────

def _materialize(suite, task, dest: Path, *, with_solution: bool) -> None:
    fixture = resolve(suite, suite["fixtures"][task["fixture"]]["path"])
    for rel, data in tree_files(fixture).items():
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_bytes(data)
    if with_solution:
        for rel, data in tree_files(resolve(suite, task["scripted"]["solution"])).items():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            (dest / rel).write_bytes(data)
    held = dest / "bench_acceptance"
    shutil.copytree(resolve(suite, task["acceptance"]["hidden"]), held)
    for inc in task["acceptance"].get("include") or []:
        shutil.copy2(resolve(suite, inc), held / Path(inc).name)


def _passes(commands, cwd: Path) -> bool:
    import shlex

    for command in commands:
        done = subprocess.run(shlex.split(command), cwd=cwd, capture_output=True, text=True,
                              timeout=120)
        if done.returncode != 0:
            return False
    return True


def _needs(task) -> str:
    return "node" if any(c.startswith(("node", "npm")) for c in task["acceptance"]["commands"]) \
        else "python3"


@pytest.mark.parametrize("task_id", [t["id"] for t in load_suite()["tasks"]])
def test_held_acceptance_fails_on_the_fixture_and_passes_on_the_solution(suite, task_id, tmp_path):
    task = next(t for t in suite["tasks"] if t["id"] == task_id)
    if not shutil.which(_needs(task)):
        pytest.skip(f"{_needs(task)} is not installed")
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    _materialize(suite, task, before, with_solution=False)
    _materialize(suite, task, after, with_solution=True)
    assert not _passes(task["acceptance"]["commands"], before), \
        f"{task_id}: the held tests pass on the untouched fixture, so they prove nothing"
    assert _passes(task["acceptance"]["commands"], after), \
        f"{task_id}: the held tests fail on the scripted solution"
    assert _passes(task["test_commands"], after), f"{task_id}: the solution's own tests fail"
    for rel in task["acceptance"].get("expect_files") or []:
        assert (after / rel).is_file(), rel
    fixture = resolve(suite, suite["fixtures"][task["fixture"]]["path"])
    base = tree_files(fixture)
    for rel in task["acceptance"].get("expect_changed") or []:
        assert (after / rel).read_bytes() != base.get(rel), f"{task_id}: {rel} is not changed"


# ── the committed baseline ───────────────────────────────────────────────────────────────────

def test_the_scripted_baseline_was_made_on_this_suite_and_passed(suite):
    path = BASELINES_DIR / "scripted.json"
    assert path.is_file(), "bench/harness/baselines/scripted.json is missing"
    base = json.loads(path.read_text())
    run = base["run"]
    assert run["suite_sha256"] == suite["_sha256"], (
        "the suite changed since the scripted baseline was made: re-run "
        "`scripts/harness-bench run --engine scripted --save-baseline scripted` and commit it")
    assert run["engine"] == "scripted" and run["status"] == "completed"
    assert sorted(run["task_ids"]) == sorted(t["id"] for t in suite["tasks"])
    assert all(r["success"] for r in base["results"]), "the scripted baseline must pass everywhere"
    assert all((r.get("metrics") or {}).get("cost_usd") == 0 for r in base["results"])


def test_rules_are_recorded_with_the_baseline(suite):
    base = json.loads((BASELINES_DIR / "scripted.json").read_text())
    assert base["run"]["rules"] == copy.deepcopy(rules_for(suite, "scripted"))

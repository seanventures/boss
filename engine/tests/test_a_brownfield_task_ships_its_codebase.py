"""A bake-off that only measures greenfield tasks cannot see the gap it exists to find.

Every task in `evals/bakeoff/tasks.json` was "create <newfile>.py". That is the one shape
where an agent with no exploration tools is not handicapped: a whole-file write IS the
natural output, and there is nothing to navigate. So a 46-run study on that suite could not
distinguish a single-completion agent from a tool-using one, and BOSS-407 went unnoticed
for months while the numbers looked fine.

A brownfield task ships the codebase it starts from, so "find the right file among many" is
measured rather than dependent on what previous runs happened to leave behind.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from boss_engine.services.bakeoff import (
    build_run_request,
    fixture_for_run,
    stage_fixture,
)

TASKS = Path("evals/bakeoff/tasks.json")


@pytest.fixture(scope="module")
def suite() -> dict:
    if not TASKS.exists():  # pragma: no cover - engine installed standalone
        pytest.skip(f"{TASKS} not present in this checkout")
    return json.loads(TASKS.read_text())


@pytest.fixture(scope="module")
def brownfield(suite: dict) -> dict:
    return next(t for t in suite["tasks"] if t.get("shape") == "brownfield")


# --- the suite now measures both shapes ---------------------------------------

def test_the_suite_has_at_least_one_brownfield_task(suite: dict) -> None:
    shapes = {t.get("shape", "greenfield") for t in suite["tasks"]}
    assert "brownfield" in shapes, (
        "without one, the suite cannot detect an agent that cannot navigate a repository"
    )


def test_the_target_file_is_not_the_obvious_one(brownfield: dict) -> None:
    """The decoy is the point: `legacy_rules.py` already contains a bulk-discount
    function, and using it is the wrong answer."""
    files = brownfield["fixture_files"]
    assert "inventory/pricing/rules.py" in files
    assert "legacy_bulk_discount" in files["inventory/pricing/legacy_rules.py"]
    assert "must not be extended" in files["docs/pricing.md"]
    assert any("legacy_rules.py is not imported" in c
               for c in brownfield["acceptance_criteria"])


def test_the_fixture_is_a_real_multi_file_tree(brownfield: dict) -> None:
    files = brownfield["fixture_files"]
    assert len(files) >= 10, "a handful of files is still a context window"
    assert len({p.split("/")[0] for p in files}) >= 3


def test_the_fixture_brings_its_own_passing_tests(brownfield: dict) -> None:
    """A change that breaks what already worked has to be detectable."""
    assert "test_inventory.py" in brownfield["fixture_files"]
    assert any("still pass, unmodified" in c for c in brownfield["acceptance_criteria"])


def test_the_fixture_python_is_valid(brownfield: dict) -> None:
    import ast

    for path, content in brownfield["fixture_files"].items():
        if path.endswith(".py"):
            ast.parse(content)  # raises on a typo in the fixture


def test_the_fixture_actually_runs_and_its_tests_pass(tmp_path: Path, brownfield: dict) -> None:
    """A fixture whose own suite is red would make every run fail for the wrong reason."""
    import subprocess

    stage_fixture(tmp_path, brownfield["fixture_files"])
    proc = subprocess.run(
        ["python3", "-m", "unittest", "discover", "-v"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- staging ------------------------------------------------------------------

def test_staging_writes_every_file_where_it_belongs(tmp_path: Path, brownfield: dict) -> None:
    written = stage_fixture(tmp_path, brownfield["fixture_files"])
    assert set(written) == set(brownfield["fixture_files"])
    assert (tmp_path / "inventory" / "pricing" / "rules.py").is_file()


def test_a_fixture_path_cannot_escape_the_sandbox(tmp_path: Path) -> None:
    """A fixture is data from a config file, and config files get edited."""
    with pytest.raises(ValueError, match="outside the sandbox root"):
        stage_fixture(tmp_path, {"../escaped.py": "x = 1\n"})
    assert not (tmp_path.parent / "escaped.py").exists()


def test_a_normal_run_stages_nothing(tmp_path: Path) -> None:
    """Fixture staging must be invisible to every run that is not a brownfield bake-off."""
    assert stage_fixture(tmp_path, None) == []
    assert stage_fixture(tmp_path, {}) == []
    assert list(tmp_path.iterdir()) == []
    assert fixture_for_run(None) is None
    assert fixture_for_run({}) is None
    assert fixture_for_run({"bakeoff": {"config": "x"}}) is None


def test_the_fixture_reaches_the_run_through_finalize_ctx(brownfield: dict) -> None:
    req = build_run_request(brownfield, {"name": "cfg", "profile": "strict"}, "batch1", 1)
    carried = fixture_for_run(req["finalize_ctx"])
    assert carried == brownfield["fixture_files"]
    assert req["finalize_ctx"]["bakeoff"]["shape"] == "brownfield"


def test_a_greenfield_task_carries_no_fixture(suite: dict) -> None:
    green = next(t for t in suite["tasks"] if t["key"] == "slugify")
    req = build_run_request(green, {"name": "cfg"}, "batch1", 1)
    assert fixture_for_run(req["finalize_ctx"]) is None
    assert req["finalize_ctx"]["bakeoff"]["shape"] == "greenfield"

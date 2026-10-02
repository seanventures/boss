"""Every workflow version a run can start from protects the agents its steps name.

THE INCIDENT (review of milestone/agents, round 2). The agent-delete guard read each workflow's
NEWEST published version. publish_version makes an older version active again when its content
is re-saved (the restore-after-a-bad-save flow) and can publish without activating, and
start_run takes any published version by id, through a mapping, or as a subworkflow child. A
probe published v1 naming agent K, v2 naming qa, re-saved v1 (active again), and DELETE K
answered 200 — K tombstoned, so no seed brings it back, and every run of v1 refused with "not a
role this workforce has".

ENFORCED BY ENUMERATION, not by listing the selectors we know today:
  * start_run is the only writer of workflow_runs, and every version it starts passes
    `_version_row`. A second writer would be a door around both — this fails on one.
  * For every (version status, definition status) pair the schema allows, `_version_row`
    accepting it implies the guard reads it. So whatever new selector picks a version — active
    pointer, explicit id, mapping, subworkflow binding, something not written yet — the version
    it picks is one the guard already reads.
  * The guard labels each reachable version; only `older` (nothing starts it by default) may
    be retired to free an agent, and every selector that points at a version has its label.
The behaviour end to end, through POST /v1/agents and real publishes, is in
apps/api/tests/test_one_agent_model.py.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from boss_engine.services import agents, workflows

ROOT = Path(__file__).resolve().parents[2]
SCANNED = [ROOT / "engine" / "boss_engine", ROOT / "apps" / "api" / "app", ROOT / "apps" / "worker"]


def _schema_statuses(table: str) -> set[str]:
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    body = schema[schema.index(f"CREATE TABLE IF NOT EXISTS {table} ("):]
    body = body[:body.index(");")]
    found = re.search(r"status TEXT[^,]*CHECK \(status IN \(([^)]*)\)\)", body)
    assert found, f"the {table}.status CHECK moved; this test must read it"
    return {s.strip().strip("'") for s in found.group(1).split(",")}


class _OneRow:
    def __init__(self, row):
        self._row = row

    def execute(self, *_a, **_k):
        return self

    def mappings(self):
        return self

    def first(self):
        return self._row


def test_start_run_is_the_only_writer_of_workflow_runs():
    writers = []
    for base in SCANNED:
        for path in base.rglob("*.py"):
            if "__pycache__" in str(path) or "/tests/" in str(path):
                continue
            source = path.read_text(encoding="utf-8")
            if "INSERT INTO workflow_runs" not in source:
                continue
            for fn in ast.walk(ast.parse(source)):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                        isinstance(n, ast.Constant) and isinstance(n.value, str)
                        and "INSERT INTO workflow_runs" in n.value for n in ast.walk(fn)):
                    writers.append((str(path.relative_to(ROOT)), fn.name))
    assert writers == [("engine/boss_engine/services/workflows.py", "start_run")], (
        f"workflow runs are created in {writers}. A run started anywhere but start_run skips "
        "_version_row, so the agent-delete guard (workflows.versions_a_run_can_reach) cannot "
        "know which versions it may start. Start runs through start_run.")
    start = Path(workflows.__file__).read_text(encoding="utf-8")
    body = start[start.index("def start_run("):]
    body = body[:body.index("\ndef ", 1)]
    assert "_version_row(connection" in body, "start_run no longer checks the version it starts"


def test_every_version_a_run_can_start_is_one_the_guard_reads():
    version_statuses = _schema_statuses("workflow_versions")
    definition_statuses = _schema_statuses("workflow_definitions")
    assert {"published", "retired"} <= version_statuses
    assert {"draft", "active", "retired"} <= definition_statuses
    startable = []
    for vs in sorted(version_statuses):
        for ds in sorted(definition_statuses):
            row = {"status": vs, "definition_status": ds, "version": 1, "workflow_name": "W"}
            try:
                workflows._version_row(_OneRow(row), "p", "v")
            except workflows.WorkflowError:
                continue
            startable.append((vs, ds))
            assert vs in workflows.STARTABLE_VERSION_STATUSES, (vs, ds)
            assert ds in workflows.GUARDED_DEFINITION_STATUSES, (vs, ds)
    assert startable == [("published", "active")], startable
    # A draft is not startable yet, but one publish makes every version it has startable by
    # id; the guard must already be reading them.
    assert "draft" in workflows.GUARDED_DEFINITION_STATUSES
    # And a retired version is refused with a sentence that says what to do.
    with pytest.raises(workflows.WorkflowError, match="was retired.*current version"):
        workflows._version_row(_OneRow({"status": "retired", "definition_status": "active",
                                        "version": 3, "workflow_name": "Ship it"}), "p", "v")


def test_the_guard_reads_every_reachable_version_not_a_selection():
    """steps_naming must take its versions from versions_a_run_can_reach — never its own
    'newest per workflow' query, which is how the round-2 hole was made."""
    source = Path(agents.__file__).read_text(encoding="utf-8")
    body = source[source.index("def steps_naming("):source.index("def _where(")]
    assert "versions_a_run_can_reach(" in body
    assert "DISTINCT ON" not in body and "version DESC" not in body


def test_only_a_version_nothing_starts_by_default_may_be_retired():
    assert set(workflows.VERSION_REACH) == {
        "active", "not yet active", "mapped", "subworkflow", "older"}
    assert workflows.RETIRABLE_REACH == {"older"}
    # Every way a version is pointed at has a label that is NOT retirable. Selectors today:
    # the definition's active pointer, an enabled mapping, a subworkflow binding, and a newer
    # version published without activating (one switch away). An explicit id with no pointer
    # is 'older' — guarded, and freed only by retiring it, which _version_row then refuses.
    for pointer in ("active", "mapped", "subworkflow", "not yet active"):
        assert pointer not in workflows.RETIRABLE_REACH


def test_the_refusal_says_which_version_names_the_agent():
    base = {"version": 1}
    assert agents._where({**base, "because": "active"}) == "v1, the version it runs"
    assert "older version still startable by id" in agents._where({**base, "because": "older"})
    assert agents._where({**base, "because": "live run"}) == "a run in flight"
    for reach in workflows.VERSION_REACH:
        assert agents._where({**base, "because": reach}).startswith("v1")

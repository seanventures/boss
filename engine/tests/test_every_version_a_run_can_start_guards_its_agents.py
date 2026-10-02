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


#: One step naming the agent, reached every way there is, with a pointer of each kind — what
#: steps_naming hands `_where` and `_remedy`.
_NAMED = {
    "workflow": "Ship it", "version": 1, "step": "Audit the release",
    "pointed_by": [
        {"via": "mapped", "mapping_id": "wfm_1", "selector_kind": "work_class",
         "selector_value": "project.new"},
        {"via": "subworkflow", "workflow_key": "release_train", "workflow": "Release train",
         "version": 4, "step": "Run the audit"},
    ],
}


def test_the_refusal_says_which_version_names_the_agent():
    """Rewritten in review round 3: it pinned 'a run in flight' with no version, and asked only
    that each reach's text start with the version — the mapped and subworkflow texts did, while
    naming neither the kind of work nor the parent workflow."""
    said = {r: agents._where({**_NAMED, "because": r}) for r in agents.REACH_SAID}
    assert said["active"] == "v1, the version it runs"
    assert "older version still startable by id" in said["older"]
    assert said["live run"] == "a run in flight on v1"
    assert "stopped at a failed step" in said["stopped run"]
    assert "new projects" in said["mapped"], said["mapped"]
    assert "Release train" in said["subworkflow"] and "Run the audit" in said["subworkflow"]
    for reach in workflows.VERSION_REACH:
        assert said[reach].startswith("v1")


def test_every_way_a_version_is_reached_has_its_own_remedy():
    """THE INCIDENT (review of milestone/agents, round 3). Every reach but 'older' was told
    "give those steps to another agent and save the workflow, then delete again". Saving moves
    neither a mapping (it pins one exact version) nor a parent's subworkflow binding, so an
    operator who did exactly what the refusal said was refused again, for good.

    Walks every label — each VERSION_REACH way a start reaches a version and each RUN_REACH way
    a run does — so a new one cannot ship borrowing another's sentence."""
    assert set(agents.REACH_SAID) == set(workflows.VERSION_REACH) | set(agents.RUN_REACH)
    remedy = {r: agents._remedy({**_NAMED, "because": r, "reasons": [r]})
              for r in agents.REACH_SAID}
    # Distinct per reach, except the two a save of the workflow really does fix.
    assert remedy["active"] == remedy["not yet active"]
    others = [remedy[r] for r in agents.REACH_SAID if r not in ("active", "not yet active")]
    assert len(set(others)) == len(others), remedy
    assert remedy["active"] not in others
    # "Save the workflow" only where saving is the fix.
    for reach, text in remedy.items():
        assert ("save the workflow" in text) == (reach in ("active", "not yet active")), reach
    # A mapping: name the kind of work, and the control that moves exactly that mapping.
    assert "point new projects at the current version of 'Ship it'" in remedy["mapped"]
    assert "/v1/workflow-mappings/wfm_1/repoint" in remedy["mapped"]
    # A parent: name the parent workflow and its step, and say to re-bind it.
    assert ("re-bind step 'Run the audit' of 'Release train' to the current version of "
            "'Ship it'") in remedy["subworkflow"]
    assert "stop" in remedy["live run"] and "stop the run" in remedy["stopped run"]
    assert "retire" in remedy["older"]
    # A step reached two ways is told both, or fixing one is refused again for the other.
    both = agents._remedy({**_NAMED, "because": "live run", "reasons": ["live run", "mapped"]})
    assert remedy["live run"] in both and remedy["mapped"] in both


def _run_statuses() -> set[str]:
    return _schema_statuses("workflow_runs")


def test_a_run_that_can_still_go_on_holds_its_version():
    """THE INCIDENT (review of milestone/agents, round 3). Only runs in flight held their
    version, so a run stopped at a failed step — still offering Try again — did not: its agent
    was deleted (its version retired if older), and Try again carried the run on to a later
    step naming that agent, which failed with "not a role this workforce has".

    For every run status the schema allows: a run whose actions still offer Try again on a
    failed step, or which is still going, must count; one in which nothing runs again need not."""
    from boss_engine.services import workflow_run_actions as actions

    statuses = _run_statuses()
    assert {"failed", "deadlettered", "running", "completed", "cancelled"} <= statuses
    for status in sorted(statuses):
        verdicts = actions._verdicts({"status": status, "input": {}},
                                     [{"step_id": "a", "status": "failed"}])
        retry = next(v for v in verdicts if v["key"] == "retry_step")
        goes_on = retry["state"] != actions.ABSENT or status in actions.ACTIVE_RUN_STATES
        reach = agents.run_reach(status)
        if goes_on:
            assert reach in agents.RUN_REACH, (status, reach)
        else:
            assert reach is None, (status, reach)
    assert agents.run_reach("failed") == "stopped run"
    assert agents.run_reach("running") == "live run"
    source = Path(agents.__file__).read_text(encoding="utf-8")
    body = source[source.index("def steps_naming("):source.index("\nRUN_REACH")]
    assert "CLOSED_RUN_STATES" in body and "'queued'" not in body, (
        "steps_naming must read the closed statuses, not keep its own list of live ones")


#: SQL that changes which version a run can start from, or which version a run is on.
_REACH_WRITES = (
    re.compile(r"INSERT INTO workflow_mappings"),
    re.compile(r"UPDATE workflow_mappings"),
    re.compile(r"INSERT INTO workflow_runs"),
    re.compile(r"UPDATE workflow_runs\s+SET\s+workflow_version_id"),
    re.compile(r"UPDATE workflow_definitions\s+SET[^;]*active_version_id", re.S),
    re.compile(r"UPDATE workflow_versions\s+SET\s+status\s*=\s*'published'"),
)


def _reach_writers() -> list[tuple[str, str, bool]]:
    found = []
    for base in SCANNED:
        for path in base.rglob("*.py"):
            if "__pycache__" in str(path) or "/tests/" in str(path):
                continue
            source = path.read_text(encoding="utf-8")
            if not any(p.search(source) for p in _REACH_WRITES):
                continue
            tree = ast.parse(source)
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                writes = any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                             and any(p.search(n.value) for p in _REACH_WRITES)
                             for n in ast.walk(fn))
                if not writes:
                    continue
                holds = any(isinstance(n, ast.Call) and (
                    getattr(n.func, "attr", None) == "hold_workflow_reach"
                    or getattr(n.func, "id", None) == "hold_workflow_reach")
                    for n in ast.walk(fn))
                found.append((str(path.relative_to(ROOT)), fn.name, holds))
    return found


def test_every_writer_of_what_a_run_can_reach_holds_the_reach_lock():
    """THE INCIDENT (review of milestone/agents, round 3). delete_agent read what names the
    agent, then retired versions and deleted the role, with nothing held in between: a publish
    committing in that gap (re-saving an older version makes it active again) left the agent
    deleted under the version that now runs, and `retire_versions` quietly skipped it.

    Every function whose SQL changes which version a run can start from — a mapping, a run, a
    migration, the active pointer, a republish — must take workflows.hold_workflow_reach, and
    delete_agent must take it exclusive before it reads anything."""
    writers = _reach_writers()
    names = {(path, fn) for path, fn, _ in writers}
    # The scan still sees the writers it was written against.
    for expected in [("engine/boss_engine/services/workflows.py", "publish_version"),
                     ("engine/boss_engine/services/workflows.py", "create_mapping"),
                     ("engine/boss_engine/services/workflows.py", "repoint_mapping"),
                     ("engine/boss_engine/services/workflows.py", "start_run"),
                     ("engine/boss_engine/services/workflows.py", "migrate_run"),
                     ("engine/boss_engine/services/workflows.py", "rollback_migration"),
                     ("engine/boss_engine/services/workflow_catalogue.py", "_repoint_default"),
                     ("apps/api/app/main.py", "apply_delivery_workflow")]:
        assert expected in names, f"{expected} no longer found by the scan; update it"
    unheld = [(path, fn) for path, fn, holds in writers if not holds]
    assert not unheld, (
        f"{unheld} change which workflow version a run can reach without "
        "workflows.hold_workflow_reach, so an agent delete can decide on what they are about to "
        "change. Take the lock (shared) before reading what you will write.")

    source = Path(agents.__file__).read_text(encoding="utf-8")
    body = source[source.index("def delete_agent("):]
    body = body[:body.index("\ndef ", 1)] if "\ndef " in body[1:] else body
    held = body.find("hold_workflow_reach(connection, project_id, exclusive=True)")
    assert 0 <= held < body.index("steps_naming("), (
        "delete_agent must hold the reach lock exclusive before it reads what names the agent")

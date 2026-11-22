"""A standard workflow is edited in place, and Reset to standard brings back what BOSS shipped.

    "we should make all the workflows editable, and if they make mistakes we should go back to
     factory mode which is reset to original value. Then they can start from beginning.. if
     they need tiny bit of change they can directly do it right?"

Pinned here: an edit makes the standard Modified with a one-line-per-step diff; Reset makes the
shipped spec active again and keeps the edit in history; a newer shipped standard updates an
unmodified standard by itself and never overwrites a modified one (it is offered instead); a run
in flight keeps the version it started on through all of it.
"""
from __future__ import annotations

import copy
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import workflow_catalogue as catalogue
from boss_engine.services import workflow_standards as standards
from boss_engine.services import workflows

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"
)
KEY = "standard_product_delivery"


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM workflow_definitions LIMIT 1"))
    except Exception as error:                      # pragma: no cover
        pytest.skip(f"boss_test unavailable: {error}")
    with engine.begin() as c:
        for column in ("standard_version_id", "standard_content_hash",
                       "standard_available_version_id", "standard_dismissed_version_id"):
            c.exec_driver_sql(
                f"ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS {column} TEXT")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("boss_test has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'standards test', :s)"), {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        for table in ("workflow_step_runs", "workflow_runs", "workflow_mappings",
                      "workflow_versions", "workflow_definitions", "events", "receipts"):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
            except Exception:
                pass
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


def _definition(c, project):
    return dict(c.execute(text("SELECT * FROM workflow_definitions WHERE project_id=:p "
                               "AND workflow_key=:k"), {"p": project, "k": KEY}).mappings().one())


def _active_spec(c, definition):
    return c.execute(text("SELECT spec FROM workflow_versions WHERE id=:v"),
                     {"v": definition["active_version_id"]}).scalar()


def _edited(spec):
    """A small edit: one step renamed, and a step added at the end."""
    spec = copy.deepcopy({k: v for k, v in spec.items() if k != workflows.AGENT_RULES_KEY})
    steps = spec["steps"]
    steps[0]["title"] = "Read the brief twice"
    last = steps[-1]
    added = {"id": "notify_partner", "kind": "capability", "title": "Notify partner",
             "binding": {"capability": "notify", "inputs": {"message": "Shipped"}}, "next": []}
    last["next"] = ["notify_partner"]
    steps.append(added)
    return spec


def _edit(c, project):
    d = _definition(c, project)
    result = workflows.publish_from_editor(c, project_id=project, actor="test", name=d["name"],
                                           spec=_edited(_active_spec(c, d)), definition_id=d["id"])
    return d, result


def test_an_edit_publishes_the_standards_next_version_and_reads_modified(eng, project):
    with eng.begin() as c:
        catalogue.seed(c, project)
        before = _definition(c, project)
        assert before["standard_version_id"] == before["active_version_id"]
        assert standards.state(c, before)["modified"] is False
        _, result = _edit(c, project)
        after = _definition(c, project)
        state = standards.state(c, after)
        # In place: the same definition, the next version, still a standard.
        assert result["definition"]["id"] == before["id"] and result["created"] is False
        assert after["is_catalogue"] is True
        assert after["active_version_id"] != before["active_version_id"]
        assert after["standard_version_id"] == before["standard_version_id"]
        assert state["modified"] is True
        lines = [line["text"] for line in state["changes"]]
        assert any(t.startswith("+ Notify partner, after") for t in lines), lines
        assert any(t.startswith("~ ") and "name" in t for t in lines), lines
        listed = next(w for w in workflows.list_definitions(c, project, namespace="product")
                      if w["workflow_key"] == KEY)
        assert listed["modified"] is True and listed["new_standard_available"] is False


def test_a_standard_keeps_its_name_and_cannot_be_deleted(eng, project):
    """Saved under another name, a standard is not renamed: that is the operator's copy."""
    with eng.begin() as c:
        catalogue.seed(c, project)
        d = _definition(c, project)
        made = workflows.publish_from_editor(c, project_id=project, actor="test",
                                             name="Lumen Pay delivery",
                                             spec=_edited(_active_spec(c, d)),
                                             definition_id=d["id"])
        assert made["created"] is True and made["definition"]["id"] != d["id"]
        assert _definition(c, project)["name"] == d["name"]
        with pytest.raises(workflows.WorkflowNamespaceError):
            workflows.delete_definition(c, project, d["id"], "test")
        with pytest.raises(workflows.WorkflowNamespaceError):
            workflows.rename_definition(c, project, d["id"], actor="test", name="Other")


def test_save_as_copy_still_makes_the_operators_own(eng, project):
    with eng.begin() as c:
        catalogue.seed(c, project)
        d = _definition(c, project)
        result = workflows.publish_from_editor(
            c, project_id=project, actor="test", name=f"{d['name']} (mine)",
            spec=_edited(_active_spec(c, d)), definition_id=d["id"], as_copy=True)
        assert result["created"] is True and result["definition"]["id"] != d["id"]
        assert standards.state(c, _definition(c, project))["modified"] is False


def test_reset_brings_back_the_shipped_spec_and_keeps_the_edit_in_history(eng, project):
    with eng.begin() as c:
        catalogue.seed(c, project)
        shipped = _definition(c, project)
        _edit(c, project)
        edited = _definition(c, project)
        out = standards.reset_to_standard(c, project, shipped["id"], "test")
        after = _definition(c, project)
        assert after["active_version_id"] == shipped["active_version_id"]
        assert out["standard"]["modified"] is False and out["standard"]["changes"] == []
        # The edit is still a version, and Restore (re-publishing it) brings it back.
        kept = c.execute(text("SELECT spec FROM workflow_versions WHERE id=:v"),
                         {"v": edited["active_version_id"]}).scalar()
        assert kept is not None
        workflows.publish_version(c, project_id=project, definition_id=shipped["id"],
                                  spec=kept, actor="test")
        assert _definition(c, project)["active_version_id"] == edited["active_version_id"]


def _ship_a_newer_standard(monkeypatch):
    """The catalogue as a newer BOSS ships it: one more step, before the last one."""
    entries = []
    for entry in catalogue.CATALOGUE:
        if entry["key"] == KEY:
            entry = copy.deepcopy(entry)
            steps = entry["steps"]
            scan = {"id": "security_scan", "kind": "capability", "title": "Security scan",
                    "binding": {"capability": "notify", "inputs": {"message": "Scanned"}},
                    "next": [steps[-1]["id"]]}
            steps[-2]["next"] = ["security_scan"]
            steps.insert(len(steps) - 1, scan)
        entries.append(entry)
    monkeypatch.setattr(catalogue, "CATALOGUE", entries)


def test_a_newer_standard_updates_an_unmodified_one_by_itself(eng, project, monkeypatch):
    with eng.begin() as c:
        catalogue.seed(c, project, only=frozenset({KEY}))
        before = _definition(c, project)
        _ship_a_newer_standard(monkeypatch)
        catalogue.seed(c, project, only=frozenset({KEY}))
        after = _definition(c, project)
        assert after["active_version_id"] != before["active_version_id"]
        assert after["standard_version_id"] == after["active_version_id"]
        state = standards.state(c, after)
        assert state["modified"] is False and state["new_standard"] is None


def test_a_newer_standard_never_overwrites_a_modified_one(eng, project, monkeypatch):
    with eng.begin() as c:
        catalogue.seed(c, project, only=frozenset({KEY}))
        _edit(c, project)
        edited = _definition(c, project)
        _ship_a_newer_standard(monkeypatch)
        catalogue.seed(c, project, only=frozenset({KEY}))
        after = _definition(c, project)
        # Untouched, and flagged.
        assert after["active_version_id"] == edited["active_version_id"]
        assert after["standard_version_id"] == edited["standard_version_id"]
        state = standards.state(c, after)
        assert state["modified"] is True
        offer = state["new_standard"]
        assert offer is not None
        assert any("Security scan" in line["text"] for line in offer["changes"]), offer
        # The operator's edit (a renamed first step, a step at the end) and the standard's (a
        # step before the last) do not touch the same fields: it applies cleanly.
        assert offer["applies_cleanly"] is True, offer["conflicts"]
        # Booting again changes nothing.
        catalogue.seed(c, project, only=frozenset({KEY}))
        assert _definition(c, project)["active_version_id"] == edited["active_version_id"]

        # Take the new standard AND keep my changes.
        out = standards.take_new_standard(c, project, after["id"], "test", keep_changes=True)
        merged = _definition(c, project)
        spec = _active_spec(c, merged)
        ids = [s["id"] for s in spec["steps"]]
        assert "security_scan" in ids and "notify_partner" in ids
        assert spec["steps"][0].get("title") == "Read the brief twice"
        assert merged["standard_version_id"] == after["standard_available_version_id"]
        assert out["standard"]["new_standard"] is None
        assert out["standard"]["modified"] is True      # still has the operator's changes


def test_take_new_only_and_not_now(eng, project, monkeypatch):
    with eng.begin() as c:
        catalogue.seed(c, project, only=frozenset({KEY}))
        _edit(c, project)
        _ship_a_newer_standard(monkeypatch)
        catalogue.seed(c, project, only=frozenset({KEY}))
        d = _definition(c, project)
        dismissed = standards.dismiss_new_standard(c, project, d["id"], "test")
        assert dismissed["standard"]["new_standard"] is None
        listed = next(w for w in workflows.list_definitions(c, project, namespace="product")
                      if w["workflow_key"] == KEY)
        assert listed["new_standard_available"] is False
        out = standards.take_new_standard(c, project, d["id"], "test", keep_changes=False)
        after = _definition(c, project)
        assert after["active_version_id"] == d["standard_available_version_id"]
        assert out["standard"]["modified"] is False


def test_a_clash_is_refused_for_keep_my_changes():
    base = {"entry_step": "a", "steps": [{"id": "a", "title": "A", "next": ["b"]},
                                         {"id": "b", "title": "B", "next": []}]}
    ours = copy.deepcopy(base)
    ours["steps"][1]["title"] = "Mine"
    theirs = copy.deepcopy(base)
    theirs["steps"][1]["title"] = "Theirs"
    merged, conflicts = standards.three_way(base, ours, theirs)
    assert conflicts, merged
    assert "Mine" in conflicts[0]


def test_a_run_in_flight_keeps_its_version_through_edit_and_reset(eng, project):
    with eng.begin() as c:
        catalogue.seed(c, project, only=frozenset({KEY}))
        d = _definition(c, project)
        run_id = f"wfr_{uuid.uuid4().hex[:12]}"
        c.execute(text(
            "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
            "workflow_version_id, status, input, actor, dedupe_key) "
            "VALUES (:id, :p, :d, :v, 'running', '{}'::jsonb, 'test', :id)"),
            {"id": run_id, "p": project, "d": d["id"], "v": d["active_version_id"]})
        _edit(c, project)
        assert _definition(c, project)["active_version_id"] != d["active_version_id"]
        pinned = c.execute(text("SELECT workflow_version_id FROM workflow_runs WHERE id=:r"),
                           {"r": run_id}).scalar()
        assert pinned == d["active_version_id"]
        spec = workflows._run_spec(c, run_id)
        assert "notify_partner" not in [s["id"] for s in spec["steps"]]
        standards.reset_to_standard(c, project, d["id"], "test")
        assert c.execute(text("SELECT workflow_version_id FROM workflow_runs WHERE id=:r"),
                         {"r": run_id}).scalar() == d["active_version_id"]

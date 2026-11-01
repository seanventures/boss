"""The workflows BOSS ships, and pointing a project at one.

A catalogue that does not validate is twelve workflows a customer can choose and never run. A
catalogue that re-seeds into a new version on every boot fills the version table with identical
rows and makes "which version is this run on" unanswerable. Both are cheap to get wrong and
expensive to notice, so they are pinned here.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import workflow_catalogue as catalogue
from boss_engine.services import workflows

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"
)


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM workflow_definitions LIMIT 1"))
    except Exception as error:                      # pragma: no cover
        pytest.skip(f"boss_test unavailable: {error}")
    with engine.begin() as c:
        for ddl in (
            "ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT 'system'",
            "ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS is_catalogue BOOLEAN NOT NULL DEFAULT false",
            "ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS forked_from_version_id TEXT",
        ):
            c.exec_driver_sql(ddl)
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("boss_test has no workspace")
        c.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:i, :w, 'catalogue test', :s)"),
            {"i": pid, "w": wid, "s": pid},
        )
    yield pid
    with eng.begin() as c:
        for table in ("workflow_mappings", "workflow_versions", "workflow_definitions",
                      "events", "receipts"):
            try:
                c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
            except Exception:
                pass
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


# ── The catalogue itself ───────────────────────────────────────────────────────

def test_every_shipped_workflow_validates():
    """A catalogue entry that does not validate is a workflow a customer can pick and never
    run. They go through the same validator a customer's own workflow does."""
    for entry in catalogue.CATALOGUE:
        spec = {"entry_step": entry["steps"][0]["id"],
                "steps": [dict(s) for s in entry["steps"]]}
        workflows.validate_workflow_spec(spec)      # raises if wrong


def test_there_are_twelve_and_each_has_a_stable_key():
    keys = [e["key"] for e in catalogue.CATALOGUE]
    assert len(keys) == 20
    assert len(set(keys)) == 20, "duplicate workflow key"
    for key in keys:
        assert key.replace("_", "").isalnum(), key


def test_each_declares_the_class_of_work_it_is_for():
    """Applying "Fast Fix" to every new project is a mistake worth preventing at the point of
    choosing, not after the first one ships without a spec."""
    for entry in catalogue.CATALOGUE:
        assert entry["work_class"] in catalogue.WORK_CLASSES, entry["key"]


def test_the_five_step_standard_path_is_present_and_in_order():
    """The workflow from the original request: research → meeting → build → deploy → monitor."""
    standard = next(e for e in catalogue.CATALOGUE if e["key"] == "standard_product_delivery")
    caps = [(s.get("binding") or {}).get("capability") for s in standard["steps"]]
    for earlier, later in (("research", "meeting"), ("meeting", "build"),
                           ("build", "deploy"), ("deploy", "monitor")):
        assert caps.index(earlier) < caps.index(later), f"{earlier} must precede {later}"


def test_anything_that_ships_to_production_is_gated():
    """Every workflow with a deploy step has a human gate on it. If this fails, some path
    through the catalogue puts software in production with nobody agreeing to it."""
    for entry in catalogue.CATALOGUE:
        caps = [(s.get("binding") or {}).get("capability") for s in entry["steps"]]
        if "deploy" not in caps:
            continue
        effective = workflows.validate_workflow_spec(
            {"entry_step": entry["steps"][0]["id"],
             "steps": [dict(s) for s in entry["steps"]]}
        )
        deploy = next(s for s in effective["steps"]
                      if (s.get("binding") or {}).get("capability") == "deploy")
        assert deploy["approval"]["mode"] == "manual", entry["key"]


def test_the_summary_counts_gates_from_the_effective_plan():
    """Counting from the raw entry reported "Minor Feature: 0 gates" for a workflow that stops
    at the door to production — the opposite of the truth, and the number a customer chooses on.
    """
    summary = {s["key"]: s for s in catalogue.catalogue_summary()}
    assert len(summary["minor_feature"]["gates"]) >= 1
    # REWRITTEN (2026-10-02). This asserted ">= 2" for Standard Product Delivery, which only
    # held by counting the PRD approval as a person's gate. That approval is answered by the
    # panel's own decision — automatically by default (boss-1: approved_by auto_approval twenty
    # seconds after the panel) — so the card promised a sign-off nobody gives. The release is
    # the person's gate; the PRD approval is listed apart, as automatic.
    standard = summary["standard_product_delivery"]
    assert "deploy" in standard["gates"]
    assert "spec_review" not in standard["gates"]
    assert "spec_review" in standard["automatic_approvals"]


def test_the_summary_says_what_a_project_must_already_have():
    summary = {s["key"]: s for s in catalogue.catalogue_summary()}
    assert "repository" in summary["standard_product_delivery"]["requires_project"]
    assert "deployment_target" in summary["standard_product_delivery"]["requires_project"]
    # A workflow that never builds needs neither.
    assert summary["research_led_discovery"]["requires_project"] == []


def test_a_grace_period_is_a_real_step_not_a_promise():
    """Deprecation removes something people depend on. The waiting is part of the workflow."""
    sunset = next(e for e in catalogue.CATALOGUE if e["key"] == "deprecation_sunset")
    timer = next(s for s in sunset["steps"] if s["kind"] == "timer")
    assert timer["wait_seconds"] >= 86_400


# ── Installing it ──────────────────────────────────────────────────────────────

def test_seeding_installs_every_workflow_as_read_only_product(eng, project):
    with eng.begin() as c:
        result = catalogue.seed(c, project)
        rows = workflows.list_definitions(c, project, namespace="product")
    assert result["count"] == len(catalogue.CATALOGUE)
    assert len(rows) == len(catalogue.CATALOGUE)
    for row in rows:
        assert row["namespace"] == "product"
        assert row["is_catalogue"] is True, f"{row['workflow_key']} is editable in place"


def test_seeding_twice_does_not_create_a_second_copy(eng, project):
    """Called on every boot. Duplicate definitions would make "which workflow" ambiguous and
    duplicate versions would make "which version is this run on" unanswerable."""
    with eng.begin() as c:
        catalogue.seed(c, project)
        catalogue.seed(c, project)
        definitions = workflows.list_definitions(c, project, namespace="product")
        versions = c.execute(
            text("SELECT count(*) FROM workflow_versions WHERE project_id=:p"), {"p": project}
        ).scalar()
    assert len(definitions) == len(catalogue.CATALOGUE)
    n = len(catalogue.CATALOGUE)
    assert versions == n, f"re-seeding published {versions - n} redundant versions"


def test_a_seeded_workflow_cannot_be_edited_in_place(eng, project):
    with eng.begin() as c:
        catalogue.seed(c, project)
        row = next(r for r in workflows.list_definitions(c, project, namespace="product")
                   if r["workflow_key"] == "standard_product_delivery")
        with pytest.raises(workflows.WorkflowNamespaceError):
            workflows.require_editable(row)


def test_seeding_does_not_touch_the_system_namespace(eng, project):
    with eng.begin() as c:
        catalogue.seed(c, project)
        system = workflows.list_definitions(c, project, namespace="system")
    assert system == []


# ── Applying one ───────────────────────────────────────────────────────────────

def test_applying_a_workflow_makes_it_resolve_for_that_class_of_work(eng, project):
    """The end of Phase 2: "new projects here use Standard Product Delivery", and the runtime
    resolves it the way it resolves everything else."""
    with eng.begin() as c:
        catalogue.seed(c, project)
        definition = next(r for r in workflows.list_definitions(c, project, namespace="product")
                          if r["workflow_key"] == "standard_product_delivery")
        version = c.execute(
            text("SELECT id FROM workflow_versions WHERE workflow_definition_id=:d "
                 "ORDER BY version DESC LIMIT 1"),
            {"d": definition["id"]},
        ).scalar()
        workflows.create_mapping(
            c, project_id=project, workflow_version_id=version,
            selector_kind="work_class", selector_value="project.new",
            scope_type="project", scope_id=project, priority=100, actor="test",
        )
        resolved = workflows.resolve_mapping(
            c, project_id=project, selector_kind="work_class", selector_value="project.new",
        )
    assert resolved["status"] == "selected"
    assert resolved["selected"]["workflow_version_id"] == version
    # It also says WHY that one won, which is what makes a surprising resolution debuggable.
    assert resolved["reason"]


def test_an_unmapped_class_of_work_resolves_to_nothing_rather_than_a_guess(eng, project):
    """Picking "some workflow" because none was chosen is how work silently follows a process
    nobody agreed to."""
    with eng.begin() as c:
        catalogue.seed(c, project)
        resolved = workflows.resolve_mapping(
            c, project_id=project, selector_kind="work_class", selector_value="bugfix",
        )
    # Reports "unmapped" with a reason rather than raising OR guessing. Seeding the catalogue
    # deliberately does not apply anything: installing twelve workflows is not the same as
    # choosing one, and a project that silently acquired a process nobody picked would be
    # worse than a project with none.
    assert resolved["status"] == "unmapped"
    assert resolved["selected"] is None
    assert "No approved workflow mapping" in resolved["reason"]


def test_the_list_the_console_reads_includes_declared_inputs(eng, project):
    """Start one decides whether to open the form from this field.

    The list query loaded input_schema and then dropped it, so every workflow looked like
    it declared nothing. The button sent a sample brief, and a workflow that needed a
    video link never asked for one.
    """
    schema = {
        "required": ["video_url"],
        "properties": {
            "video_url": {"type": "string", "format": "uri", "title": "Video link"},
        },
    }
    spec = {
        "entry_step": "hold",
        "input_schema": schema,
        "steps": [{"id": "hold", "kind": "timer", "wait_seconds": 1,
                   "binding": {}, "next": []}],
    }
    with eng.begin() as c:
        made = workflows.create_definition(
            c, project_id=project, workflow_key="needs_a_video", name="Needs a video",
            description=None, actor="test", namespace=workflows.PRODUCT_NAMESPACE,
        )
        workflows.publish_version(
            c, project_id=project, definition_id=made["id"], spec=spec, actor="test",
        )
        listed = {
            row["workflow_key"]: row
            for row in workflows.list_definitions(c, project, namespace="product")
        }
    assert listed["needs_a_video"]["input_schema"] == schema

"""A customer workflow surface can never reach BOSS's own machinery.

Customer delivery workflows and BOSS's internal ones share this runtime on purpose: leasing,
retries, per-step evidence, mid-run approval and compensation are the hard parts and are already
correct here. Rebuilding them for a second engine would duplicate every one of those and then
diverge from it.

What they must never share is an EDITING surface. `governed_coding_pipeline` governs every build
in the system and `meeting_consensus` produces every PRD; a customer-facing builder that could
reach either is one mis-scoped query away from rewriting the thing that reviews its own code.

`namespace` is that line, and these tests are what make it real rather than advisory.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import workflows

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"
)

MINIMAL_SPEC = {
    "steps": [{"id": "only", "kind": "task", "binding": {}, "next": []}],
    "start": "only",
}


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM workflow_definitions LIMIT 1"))
    except Exception as error:                      # pragma: no cover - no database here
        pytest.skip(f"boss_test unavailable: {error}")
    # The column under test has to exist for the suite to mean anything.
    with engine.begin() as c:
        c.exec_driver_sql(
            "ALTER TABLE workflow_definitions "
            "ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT 'system'"
        )
        c.exec_driver_sql(
            "ALTER TABLE workflow_definitions "
            "ADD COLUMN IF NOT EXISTS is_catalogue BOOLEAN NOT NULL DEFAULT false"
        )
        c.exec_driver_sql(
            "ALTER TABLE workflow_definitions "
            "ADD COLUMN IF NOT EXISTS forked_from_version_id TEXT"
        )
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("boss_test has no workspace to attach a project to")
        c.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:i, :w, 'workflow ns test', :s)"),
            {"i": pid, "w": wid, "s": pid},
        )
    yield pid
    with eng.begin() as c:
        # `events` too: creating a definition emits one, and it holds a foreign key to the
        # project. Order matters — children before the row they point at.
        # Children before parents. A run row holds a foreign key to its definition, so
        # deleting definitions first aborts the transaction and every later DELETE in this
        # block fails with "current transaction is aborted" — which reads as a broken test
        # rather than a teardown that deleted things in the wrong order.
        for table in ("workflow_step_runs", "workflow_runs", "workflow_mappings",
                      "project_skills", "workflow_versions", "workflow_definitions",
                      "decisions", "events", "receipts"):
            try:
                c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
            except Exception:                      # a table this schema does not have
                pass
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


def _make(c, project_id, key, *, namespace, catalogue=False):
    return workflows.create_definition(
        c, project_id=project_id, workflow_key=key, name=key,
        description=None, actor="test", namespace=namespace, is_catalogue=catalogue,
    )


# ── The line itself ────────────────────────────────────────────────────────────

def test_a_definition_declares_which_tier_it_belongs_to(eng, project):
    with eng.begin() as c:
        system = _make(c, project, "sys_one", namespace="system")
        product = _make(c, project, "prod_one", namespace="product")
    assert system["namespace"] == "system"
    assert product["namespace"] == "product"


def test_existing_rows_are_system_without_a_migration(eng, project):
    """The column defaults to 'system', so BOSS's thirty-three keep their meaning untouched.

    A default of 'product' would have silently exposed every internal workflow to the customer
    builder the moment it shipped.
    """
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO workflow_definitions "
                 "(id, project_id, workflow_key, name, status, created_by) "
                 "VALUES (:i, :p, 'legacy', 'legacy', 'active', 'test')"),
            {"i": f"wfd_{uuid.uuid4().hex[:10]}", "p": project},
        )
        rows = workflows.list_definitions(c, project, namespace="system")
    assert any(r["workflow_key"] == "legacy" for r in rows)


def test_the_product_list_never_shows_a_system_workflow(eng, project):
    with eng.begin() as c:
        _make(c, project, "sys_two", namespace="system")
        _make(c, project, "prod_two", namespace="product")
        product = workflows.list_definitions(c, project, namespace="product")
        system = workflows.list_definitions(c, project, namespace="system")
    assert [r["workflow_key"] for r in product] == ["prod_two"]
    assert [r["workflow_key"] for r in system] == ["sys_two"]


def test_reaching_across_the_line_is_refused_and_says_so(eng, project):
    """Refused, not "not found".

    A missing workflow is a typo; this is a customer surface touching BOSS's own machinery, and
    somebody needs to be able to see that in the logs. Collapsing the two into 404 would hide
    exactly the event worth alerting on.
    """
    with eng.begin() as c:
        system = _make(c, project, "sys_three", namespace="system")
        with pytest.raises(workflows.WorkflowNamespaceError) as caught:
            workflows.require_namespace(c, project, system["id"], "product")
    message = str(caught.value)
    assert "system workflow" in message
    assert "product surface" in message


def test_the_guard_still_refuses_a_workflow_that_does_not_exist(eng, project):
    with eng.begin() as c:
        with pytest.raises(workflows.WorkflowNotFoundError):
            workflows.require_namespace(c, project, "wfd_missing", "product")


def test_a_product_workflow_passes_its_own_guard(eng, project):
    with eng.begin() as c:
        product = _make(c, project, "prod_three", namespace="product")
        got = workflows.require_namespace(c, project, product["id"], "product")
    assert got["workflow_key"] == "prod_three"


def test_a_system_caller_is_refused_a_product_workflow(eng, project):
    """The line cuts both ways. BOSS's own machinery must not start executing a workflow a
    customer wrote just because the ids happen to be in the same table."""
    with eng.begin() as c:
        product = _make(c, project, "prod_four", namespace="product")
        with pytest.raises(workflows.WorkflowNamespaceError):
            workflows.require_namespace(c, project, product["id"], "system")


def test_an_unknown_namespace_is_rejected_rather_than_stored(eng, project):
    with eng.begin() as c:
        with pytest.raises(workflows.WorkflowValidationError):
            _make(c, project, "prod_bad", namespace="whatever")
        with pytest.raises(workflows.WorkflowValidationError):
            workflows.list_definitions(c, project, namespace="whatever")


# ── Catalogue workflows are read-only ──────────────────────────────────────────

def test_a_catalogue_workflow_cannot_be_edited_in_place(eng, project):
    """Editing in place would quietly make every fleet's "Standard Product Delivery" a
    different thing, and there would be nothing left to reset to."""
    with eng.begin() as c:
        standard = _make(c, project, "standard_delivery", namespace="product", catalogue=True)
        with pytest.raises(workflows.WorkflowNamespaceError) as caught:
            workflows.require_editable(standard)
    # REWRITTEN: this pinned "Fork it". The editor saves a standard as the operator's copy, and
    # the refusal names that act — the one the console offers.
    assert "save it as your copy" in str(caught.value).lower()


def test_a_customers_own_workflow_is_editable(eng, project):
    with eng.begin() as c:
        mine = _make(c, project, "my_delivery", namespace="product")
        workflows.require_editable(mine)          # does not raise


def test_a_fork_records_what_it_came_from(eng, project):
    """Without this the diff against the standard is unrecoverable, and "what did we change"
    becomes a question nobody can answer."""
    with eng.begin() as c:
        forked = workflows.create_definition(
            c, project_id=project, workflow_key="forked_delivery", name="Forked",
            description=None, actor="test", namespace="product",
            forked_from_version_id="wfv_original",
        )
    assert forked["forked_from_version_id"] == "wfv_original"
    assert forked["is_catalogue"] is False


# ── Custom steps (Phase 6): the floor applies to a customer, not retroactively to BOSS ──

def _skill(c, project_id, skill_id, *, risk, trusted, enabled=True):
    c.execute(
        text("INSERT INTO project_skills (id, project_id, skill_id, source, enabled, config, "
             "provenance) VALUES (:i, :p, :s, 'tenant', :e, CAST(:c AS JSONB), CAST(:pr AS JSONB)) "
             "ON CONFLICT (project_id, skill_id) DO UPDATE SET enabled=EXCLUDED.enabled, "
             "config=EXCLUDED.config, provenance=EXCLUDED.provenance"),
        {"i": f"psk_{uuid.uuid4().hex[:10]}", "p": project_id, "s": skill_id, "e": enabled,
         "c": f'{{"risk_tier": {risk}}}', "pr": '{"trusted": true}' if trusted else '{}'},
    )


def _skill_workflow(c, project, key, skill_id, namespace, approval=None):
    step = {"id": "s", "kind": "skill", "binding": {"skill_id": skill_id}, "next": []}
    if approval:
        step["approval"] = approval
    definition = workflows.create_definition(
        c, project_id=project, workflow_key=key, name=key, description=None,
        actor="test", namespace=namespace,
    )
    version = workflows.publish_version(
        c, project_id=project, definition_id=definition["id"],
        spec={"entry_step": "s", "steps": [step]}, actor="test",
    )
    return workflows.start_run(
        c, project_id=project, actor="test", input_data={},
        dedupe_key=f"dk_{uuid.uuid4().hex[:10]}", workflow_version_id=version["id"],
    )


def test_a_customers_untrusted_high_risk_skill_is_refused(eng, project):
    """"Attach your own skill" must not be the documented way around every gate in the
    product."""
    with eng.begin() as c:
        _skill(c, project, "customer.dangerous", risk=3, trusted=False)
        with pytest.raises(workflows.WorkflowBindingBlockedError) as caught:
            _skill_workflow(c, project, "risky_custom", "customer.dangerous", "product")
    assert "untrusted skill" in str(caught.value)


def test_a_customers_high_risk_skill_cannot_be_auto_approved(eng, project):
    with eng.begin() as c:
        _skill(c, project, "customer.reviewed", risk=3, trusted=True)
        with pytest.raises(workflows.WorkflowBindingBlockedError) as caught:
            _skill_workflow(c, project, "auto_custom", "customer.reviewed", "product",
                            approval={"mode": "auto"})
    assert "requires a person" in str(caught.value)


def test_a_customers_ordinary_skill_runs(eng, project):
    with eng.begin() as c:
        _skill(c, project, "customer.harmless", risk=0, trusted=False)
        run = _skill_workflow(c, project, "ok_custom", "customer.harmless", "product")
    assert run["status"] in {"queued", "running", "validating"}


def test_a_disabled_skill_is_refused_rather_than_silently_doing_nothing(eng, project):
    """A workflow that appears to run and does nothing is worse than one that refuses to
    publish."""
    with eng.begin() as c:
        _skill(c, project, "customer.off", risk=0, trusted=True, enabled=False)
        with pytest.raises(workflows.WorkflowBindingBlockedError):
            _skill_workflow(c, project, "off_custom", "customer.off", "product")


def test_the_new_rule_is_not_applied_to_bosss_own_workflows(eng, project):
    """THE CONSTRAINT. BOSS's definitions were written before this rule existed and are
    governed by the machinery around them. Applying a new check to them here would fail to
    publish workflows that have been running correctly for months."""
    with eng.begin() as c:
        # The exact binding refused above, in the system namespace.
        _skill(c, project, "internal.thing", risk=3, trusted=False)
        run = _skill_workflow(c, project, "internal_skill_wf", "internal.thing", "system")
    assert run["status"] in {"queued", "running", "validating"}


def test_an_unclassified_skill_is_treated_as_dangerous_not_as_safe(eng, project):
    """Guessing risk 0 for something nobody classified is how an ungoverned step ships."""
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO project_skills (id, project_id, skill_id, source, enabled) "
                 "VALUES (:i, :p, 'customer.unlabelled', 'tenant', true)"),
            {"i": f"psk_{uuid.uuid4().hex[:10]}", "p": project},
        )
        with pytest.raises(workflows.WorkflowBindingBlockedError):
            _skill_workflow(c, project, "unlabelled_custom", "customer.unlabelled", "product")

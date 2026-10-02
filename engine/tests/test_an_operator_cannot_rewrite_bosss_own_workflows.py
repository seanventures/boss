"""An operator surface cannot publish over a workflow BOSS ships as its own machinery.

Agent Studio's Flows tab listed all 32 system workflows — governed_coding_pipeline,
meeting_consensus, decision_inbox, deployment_run, self_improvement — with a JSON editor and
"Publish version", through an endpoint that applied no namespace check. One paste could replace
the pipeline that reviews every build. test_workflow_namespace_separation says this must never
be possible from a customer surface; this pins the guard the generic endpoint now applies.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.services import workflows

ROOT = Path(__file__).resolve().parents[2] / "packages" / "specpack" / "workflows"


def test_every_shipped_workflow_is_known_by_its_key():
    keys = workflows.shipped_workflow_keys(ROOT)
    assert len(keys) == len(list(ROOT.glob("*.yaml")))
    for key in ("governed_coding_pipeline", "meeting_consensus", "decision_inbox",
                "deployment_run", "self_improvement"):
        assert key in keys


@pytest.mark.parametrize("key", sorted(workflows.shipped_workflow_keys(ROOT)))
def test_each_one_is_refused_with_advice_that_can_be_followed(key):
    # REWRITTEN: this pinned "Fork it in Workflow > Catalogue". The catalogue lists the
    # product templates, not BOSS's own workflows, so the advice could not be followed.
    with pytest.raises(workflows.WorkflowNamespaceError) as refused:
        workflows.refuse_operator_edit_of_boss_workflow(
            {"namespace": "system", "workflow_key": key}, ROOT)
    assert "not editable from the console" in str(refused.value)
    assert "Design a workflow" in str(refused.value)
    assert "Fork it in Workflow > Catalogue" not in str(refused.value)


def test_a_customers_workflow_is_not_touched_by_the_guard():
    workflows.refuse_operator_edit_of_boss_workflow(
        {"namespace": "product", "workflow_key": "governed_coding_pipeline"}, ROOT)


def test_a_system_definition_someone_created_themselves_is_theirs():
    """The generic API can still create system-namespace definitions (its own tests do)."""
    workflows.refuse_operator_edit_of_boss_workflow(
        {"namespace": "system", "workflow_key": "release-train"}, ROOT)


def test_a_missing_specpack_protects_nothing_rather_than_crashing():
    assert workflows.shipped_workflow_keys(Path("/nonexistent")) == frozenset()

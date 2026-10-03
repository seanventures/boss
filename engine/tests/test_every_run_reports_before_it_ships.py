"""Every workflow that can ship produces a report first, and it runs last of the work steps.

Placed by the operator, twice. First:

    "I want this report to be added before the Preview"

then, once Production Launch Readiness was shown to be doing real work:

    "can we move the report after the plr => preview => report ?? I want to see the summary of
     what plr did in the workflow pipeline"

So the report sits immediately before `deploy`: after readiness and the preview that follows
it, so it can say what PLRR actually changed — and BEFORE the gate, because deploy is the one
step a person decides and many runs stop there for good. A report only produced after a
successful deploy is a report most runs never get.
"""

from __future__ import annotations

import pytest

from boss_engine.services.workflow_catalogue import CATALOGUE

SHIPPING = [w for w in CATALOGUE if any(s["id"] == "deploy" for s in w["steps"])]


def _ids(workflow) -> list[str]:
    return [s["id"] for s in workflow["steps"]]


def test_there_are_workflows_to_check():
    """A test that silently checks nothing is worse than no test."""
    assert len(SHIPPING) >= 10


@pytest.mark.parametrize("workflow", SHIPPING, ids=lambda w: w["key"])
def test_a_workflow_that_can_ship_reports_first(workflow):
    assert "report" in _ids(workflow)


@pytest.mark.parametrize("workflow", SHIPPING, ids=lambda w: w["key"])
def test_the_report_comes_before_the_deploy_gate(workflow):
    """Before, not after: a run parked on an unapproved deploy still has its report — and most
    runs park there, because not every app that builds should ship."""
    ids = _ids(workflow)
    assert ids.index("report") < ids.index("deploy")


@pytest.mark.parametrize("workflow", SHIPPING, ids=lambda w: w["key"])
def test_everything_that_does_work_has_happened_by_then(workflow):
    """The report is worth reading only once the steps it describes have run. Anything that
    builds, previews, reviews readiness or changes code must precede it."""
    ids = _ids(workflow)
    at = ids.index("report")
    for step in ("build", "preview", "plrr", "ux_review", "security_review"):
        if step in ids:
            assert ids.index(step) < at, f"{step} runs after the report that describes it"


def test_the_full_delivery_path_reads_in_the_order_it_was_asked_for():
    """The operator's own sequence, spelled out: readiness, then its preview, then the
    report."""
    ids = _ids(next(w for w in CATALOGUE if w["key"] == "standard_product_delivery"))
    assert ids.index("plrr") < ids.index("report")
    assert "preview_final" not in ids


@pytest.mark.parametrize("workflow", SHIPPING, ids=lambda w: w["key"])
def test_the_report_step_is_wired_into_the_run(workflow):
    """Most of the catalogue derives `next` from list order via `_chain`, but not all of it —
    `deprecation_sunset` writes its edges by hand, and a step dropped into that list without
    them is present in the plan and never run. That is exactly how `plrr` spent its life."""
    by_id = {s["id"]: s for s in workflow["steps"]}
    reachable: set[str] = set()
    frontier = [_ids(workflow)[0]]
    while frontier:
        current = frontier.pop()
        if current in reachable:
            continue
        reachable.add(current)
        frontier.extend(by_id[current].get("next") or [])
    assert "report" in reachable, f"{workflow['key']} lists a report step nothing leads to"

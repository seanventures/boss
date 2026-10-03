"""A step may only read output from a step that has certainly run before it.

Dragging Preview above Build in the Workflow Studio left Preview reading Build's run id. Check
said Ready and Save would have published a workflow whose first step waits on output that
cannot exist yet. Check and publish share one collector, so both are pinned here.
"""
from __future__ import annotations

import copy
from uuid import uuid4

PROJECT = "boss-dogfood"


def _post(client, path: str, **body):
    return client.post(f"/v1/delivery-workflows/{path}",
                       json={"project_id": PROJECT, "actor": "ceo", **body})


def _build() -> dict:
    return {"id": "build", "kind": "capability", "next": ["preview"],
            "binding": {"capability": "build",
                        "inputs": {"title": {"from_input": "title"},
                                   "instructions": {"from_input": "directive"}}}}


def _preview(nxt: list[str]) -> dict:
    return {"id": "preview", "kind": "capability", "next": nxt,
            "binding": {"capability": "preview",
                        "inputs": {"run_id": {"from_step": "build",
                                              "field": "code_task_run_id"}}}}


IN_ORDER = {
    "entry_step": "build",
    "input_schema": {"directive": "string", "title": "string"},
    "steps": [_build(), _preview([])],
}


def _reordered() -> dict:
    """What the lane sends after Preview is dragged above Build."""
    spec = copy.deepcopy(IN_ORDER)
    build, preview = spec["steps"]
    build["next"], preview["next"] = [], ["build"]
    spec["entry_step"] = "preview"
    spec["steps"] = [preview, build]
    return spec


def _branched(build_on_both_paths: bool) -> dict:
    """A condition first; Preview after the branches meet. Build is on one path or both."""
    gate = {"id": "is_big", "kind": "condition",
            "expression": {"path": "input.title", "op": "eq", "value": "big"},
            "on_true": "build", "on_false": "build" if build_on_both_paths else "preview"}
    return {"entry_step": "is_big",
            "input_schema": {"directive": "string", "title": "string"},
            "steps": [gate, _build(), _preview([])]}


def _order_problems(problems: list[dict]) -> list[dict]:
    return [p for p in problems if p["step_id"] == "preview" and p["field"] == "run_id"]


def test_the_right_order_passes(client):
    body = _post(client, "check", spec=IN_ORDER).json()
    assert _order_problems(body["problems"]) == [], body["problems"]


def test_a_step_moved_above_its_source_is_refused_by_check_and_publish_alike(client):
    checked = _post(client, "check", spec=_reordered())
    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert body["ok"] is False
    found = _order_problems(body["problems"])
    assert [p["message"] for p in found] == [
        "Gets run_id from “Build, review and test”, which runs after it."]

    published = _post(client, "publish", name=f"Order test {uuid4().hex[:8]}",
                      spec=_reordered())
    assert published.status_code == 422, published.text
    assert _order_problems(published.json()["problems"]) == found


def test_a_source_on_only_one_branch_is_not_enough(client):
    body = _post(client, "check", spec=_branched(build_on_both_paths=False)).json()
    found = _order_problems(body["problems"])
    assert [p["message"] for p in found] == [
        "Gets run_id from “Build, review and test”, which does not always run "
        "before it."], body["problems"]


def test_a_source_on_every_branch_is(client):
    body = _post(client, "check", spec=_branched(build_on_both_paths=True)).json()
    assert _order_problems(body["problems"]) == [], body["problems"]


def test_a_source_that_is_not_in_the_workflow_is_named(client):
    spec = copy.deepcopy(IN_ORDER)
    spec["steps"][1]["binding"]["inputs"]["run_id"]["from_step"] = "gone"
    body = _post(client, "check", spec=spec).json()
    assert [p["message"] for p in _order_problems(body["problems"])] == [
        "Gets run_id from step 'gone', which is not in this workflow."]


def _research_on_one_branch(meeting_inputs: dict) -> dict:
    """Research behind 'do we have research?'; the panel after the branches meet."""
    gate = {"id": "want_research", "kind": "condition",
            "expression": {"path": "input.title", "op": "eq", "value": "deep"},
            "on_true": "research", "on_false": "meeting"}
    research = {"id": "research", "kind": "capability", "next": ["meeting"],
                "binding": {"capability": "research",
                            "inputs": {"seed": {"from_input": "directive"}}}}
    meeting = {"id": "meeting", "kind": "capability", "next": [],
               "binding": {"capability": "meeting", "inputs": meeting_inputs}}
    return {"entry_step": "want_research",
            "input_schema": {"directive": "string", "title": "string"},
            "steps": [gate, research, meeting]}


def _meeting_problems(problems: list[dict]) -> list[dict]:
    return [p for p in problems if p["step_id"] == "meeting"]


def test_an_optional_input_may_read_from_a_step_on_one_branch(client):
    """The runner leaves an optional input out when its source did not run, so it is fine."""
    spec = _research_on_one_branch({
        "directive": {"from_input": "directive"},
        "research": {"from_step": "research", "field": "dossier"}})
    body = _post(client, "check", spec=spec).json()
    assert _meeting_problems(body["problems"]) == [], body["problems"]


def test_a_required_input_may_not(client):
    spec = _research_on_one_branch({
        "directive": {"from_step": "research", "field": "dossier"}})
    body = _post(client, "check", spec=spec).json()
    assert [p["message"] for p in _meeting_problems(body["problems"])
            if p["field"] == "directive"] == [
        "Gets directive from “Research”, which does not always run before it."], body["problems"]


def test_an_optional_input_reading_a_later_step_is_still_refused(client):
    spec = _research_on_one_branch({
        "directive": {"from_input": "directive"},
        "research": {"from_step": "research", "field": "dossier"}})
    gate, research, meeting = spec["steps"]
    # Panel first, research after it.
    gate["on_true"], gate["on_false"] = "meeting", "meeting"
    meeting["next"], research["next"] = ["research"], []
    body = _post(client, "check", spec=spec).json()
    assert [p["message"] for p in _meeting_problems(body["problems"])] == [
        "Gets research from “Research”, which runs after it."], body["problems"]


def test_a_custom_steps_own_optional_set_is_the_one_asked():
    """A custom step's optional inputs come from its definition, as they do in the runner."""
    from boss_engine.services.workflows import binding_order_problems

    spec = _research_on_one_branch({})
    spec["steps"][2]["binding"] = {"custom_step": "overlay",
                                   "inputs": {"image": {"from_step": "research",
                                                        "field": "dossier"}}}
    assert binding_order_problems(spec, lambda step: frozenset({"image"})) == []
    assert [p["field"] for p in binding_order_problems(spec, lambda step: frozenset())] == [
        "image"]

"""A `for_each` step runs one step once for every item of a list (#512).

    {"kind": "for_each", "items": "steps.fetch.output.urls", "body": "visit",
     "max_items": 100, "concurrency": 1, "next": [...]}

* `items` is a path into what the run knows (`input.<name>`, `steps.<id>.output.<field>`) or a
  literal list. It is resolved once, when the for_each is reached, and the list is kept on the
  step so a retry runs the same items.
* The body runs once per item, as its own step run (`iteration` = the item's index), with the
  item on the row's `input` (`item`, `item_index`, `item_count`) and readable from its binding as
  `{"from_item": "<path or empty>"}` / `{"from_item_index": true}`. At most `concurrency` run at
  once.
* The for_each succeeds with `results`, the body outputs in item order (not finishing order).
  An empty list skips the body and succeeds with []. A list longer than `max_items` is refused
  at run time with a sentence, never cut short. A body that fails for good (after its own
  attempts) fails the for_each and the run stops there; Retry runs the failed items again.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text

from boss_engine.services import workflows
from boss_engine.services.workflow_capability_runner import _resolve_inputs, item_context

from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    _start,
    eng,
    project,
)

WORKER = "worker-a"


# ── helpers ────────────────────────────────────────────────────────────────────────────────

def _problems(steps, entry="a"):
    _, problems = workflows.normalise_workflow_spec(
        {"entry_step": entry, "steps": steps}, check_agents=False)
    return problems


def _fields(problems):
    return sorted((p["step_id"], p["field"]) for p in problems)


def _spec(**fe):
    return [
        {"id": "a", "kind": "task", "next": ["each"]},
        {"id": "each", "kind": "for_each", "items": "input.urls", "body": "visit",
         "next": ["after"], **fe},
        {"id": "visit", "kind": "task",
         "binding": {"inputs": {"url": {"from_item": ""}, "n": {"from_item_index": True}}}},
        {"id": "after", "kind": "task"},
    ]


def _run(c, project_id, steps, input_data=None, entry="a"):
    key = f"fe_{uuid.uuid4().hex[:6]}"
    definition = workflows.create_definition(
        c, project_id=project_id, workflow_key=key, name=key, description=None,
        actor="test", namespace="product",
    )
    version = workflows.publish_version(
        c, project_id=project_id, definition_id=definition["id"],
        spec={"entry_step": entry, "steps": steps}, actor="test",
    )
    return _start(c, project_id, version["id"], input_data or {})["id"]


def _claim(c, project_id, run_id):
    return workflows.claim_step(c, project_id=project_id, run_id=run_id, worker_id=WORKER,
                                lease_seconds=600)


def _complete(c, project_id, claimed, output):
    workflows.complete_step(c, project_id=project_id, step_run_id=claimed["id"],
                            worker_id=WORKER, output=output, evidence=[])


def _finish(c, project_id, run_id, step_id, output=None):
    claimed = _claim(c, project_id, run_id)
    assert claimed is not None and claimed["step_id"] == step_id, claimed
    _complete(c, project_id, claimed, output if output is not None else {step_id: True})
    return claimed


def _rows(c, run_id, step_id):
    return c.execute(text("SELECT * FROM workflow_step_runs WHERE workflow_run_id=:r "
                          "AND step_id=:s ORDER BY iteration"),
                     {"r": run_id, "s": step_id}).mappings().all()


def _row(c, run_id, step_id):
    return _rows(c, run_id, step_id)[0]


def _run_row(c, run_id):
    return c.execute(text("SELECT * FROM workflow_runs WHERE id=:r"),
                     {"r": run_id}).mappings().one()


def _events(c, run_id, event_type):
    return c.execute(text("SELECT data FROM events WHERE entity_id=:r AND event_type=:t "
                          "ORDER BY created_at, id"),
                     {"r": run_id, "t": event_type}).scalars().all()


def _due_now(c, run_id):
    """Time passes: a retried item's (zero) backoff is over in this transaction's clock."""
    c.execute(text("UPDATE workflow_step_runs SET run_after = now() - interval '1 second' "
                   "WHERE workflow_run_id=:r AND status='ready'"), {"r": run_id})


def _ready_bodies(c, run_id):
    return [int(r["iteration"]) for r in _rows(c, run_id, "visit")
            if r["status"] in ("ready", "leased", "running")]


# ── the spec ───────────────────────────────────────────────────────────────────────────────

def test_a_well_formed_for_each_publishes_with_its_defaults():
    spec, problems = workflows.normalise_workflow_spec(
        {"entry_step": "a", "steps": _spec()}, check_agents=False)
    assert problems == []
    each = next(s for s in spec["steps"] if s["id"] == "each")
    assert (each["max_items"], each["concurrency"]) == (100, 1)
    assert "for_each" in workflows.CONTROL_KINDS


def test_a_literal_list_is_a_list_to_go_through():
    assert _problems(_spec(items=["x", "y"])) == []


def test_every_for_each_mistake_is_said_on_its_field():
    problems = _problems([
        {"id": "a", "kind": "task", "next": ["each"]},
        {"id": "each", "kind": "for_each", "items": "", "max_items": 5000, "concurrency": 0},
    ])
    assert _fields(problems) == [("each", "body"), ("each", "concurrency"),
                                 ("each", "items"), ("each", "max_items")]
    said = " ".join(p["detail"] for p in problems)
    assert "between 1 and 1000" in said and "between 1 and 20" in said
    assert "which list" in said


def test_items_must_be_a_path_the_run_knows_or_a_list():
    for items in ("urls", "steps.", {"path": "input.urls"}, 7):
        assert ("each", "items") in _fields(_problems(_spec(items=items))), items
    assert ("each", "items") in _fields(_problems(_spec(items="steps.nowhere.output.x")))
    assert ("each", "items") in _fields(_problems(_spec(items=list(range(101)))))


def test_the_body_is_one_step_that_does_work_and_only_its_for_each_starts():
    steps = _spec()
    steps[2] = {"id": "visit", "kind": "timer", "wait_seconds": 1}
    assert ("each", "body") in _fields(_problems(steps))

    steps = _spec()
    steps[2] = {**steps[2], "next": ["after"]}
    assert ("visit", "next") in _fields(_problems(steps))

    steps = _spec()
    steps[2] = {**steps[2], "on_error": "after"}
    assert ("visit", "on_error") in _fields(_problems(steps))

    steps = _spec()
    steps[0] = {**steps[0], "next": ["each", "visit"]}
    assert ("a", "next") in _fields(_problems(steps))

    steps = _spec(body="each")
    assert ("each", "body") in _fields(_problems(steps))


def test_a_for_each_cannot_be_the_body_of_a_loop_or_another_for_each():
    problems = _problems([
        {"id": "a", "kind": "task", "next": ["loop"]},
        {"id": "loop", "kind": "bounded_loop", "body": "each", "max_iterations": 2,
         "expression": {"path": "context.again", "op": "eq", "value": True}},
        {"id": "each", "kind": "for_each", "items": "input.urls", "body": "visit"},
        {"id": "visit", "kind": "task"},
    ])
    assert ("loop", "body") in _fields(problems)


def test_only_a_for_each_body_can_read_the_item():
    steps = _spec()
    steps[3] = {"id": "after", "kind": "task",
                "binding": {"inputs": {"url": {"from_item": ""}}}}
    problems = _problems(steps)
    assert ("after", "url") in _fields(problems)
    assert "only the step a for_each repeats" in " ".join(p["detail"] for p in problems)


def test_a_spec_without_for_each_normalises_as_before():
    steps = [{"id": "a", "kind": "task", "next": ["b"]}, {"id": "b", "kind": "task"}]
    spec, problems = workflows.normalise_workflow_spec(
        {"entry_step": "a", "steps": steps}, check_agents=False)
    assert problems == []
    for step in spec["steps"]:
        assert not {"items", "max_items", "concurrency"} & set(step)


def test_the_body_reads_what_ran_before_the_for_each():
    steps = [
        {"id": "a", "kind": "task", "next": ["each"]},
        {"id": "each", "kind": "for_each", "items": "steps.a.output.urls", "body": "visit"},
        {"id": "visit", "kind": "task",
         "binding": {"inputs": {"seed": {"from_step": "a", "field": "seed"}}}},
    ]
    spec, problems = workflows.normalise_workflow_spec(
        {"entry_step": "a", "steps": steps}, check_agents=False)
    assert problems == []
    assert workflows.binding_order_problems(spec) == []


def test_items_must_come_from_a_step_that_ran_before():
    steps = [
        {"id": "a", "kind": "task", "next": ["each"]},
        {"id": "each", "kind": "for_each", "items": "steps.z.output.urls", "body": "visit",
         "next": ["z"]},
        {"id": "visit", "kind": "task"},
        {"id": "z", "kind": "task"},
    ]
    spec, problems = workflows.normalise_workflow_spec(
        {"entry_step": "a", "steps": steps}, check_agents=False)
    assert problems == []
    found = workflows.binding_order_problems(spec)
    assert [(p["step_id"], p["field"]) for p in found] == [("each", "items")]


# ── the item, as a body step reads it ─────────────────────────────────────────────────────

def test_a_body_step_reads_its_item_and_index():
    claimed = {"input": {"for_each": "each", "item": {"url": "u2", "tags": ["x"]},
                         "item_index": 1, "item_count": 3}}
    context = {"input": {}, "steps": {}, **item_context(claimed)}
    resolved = _resolve_inputs({"whole": {"from_item": ""}, "url": {"from_item": "url"},
                                "tag": {"from_item": "tags.0"},
                                "n": {"from_item_index": True}, "lit": 5}, context)
    assert resolved == {"whole": {"url": "u2", "tags": ["x"]}, "url": "u2", "tag": "x",
                        "n": 1, "lit": 5}
    assert item_context({"input": {}}) == {}


# ── the run ────────────────────────────────────────────────────────────────────────────────

def test_each_item_runs_the_body_once_in_order_and_the_results_keep_item_order(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, _spec(), {"urls": ["u0", "u1", "u2"]})
        _finish(c, project, run_id, "a")
        each = _row(c, run_id, "each")
        assert each["status"] == "waiting"
        assert each["output"]["count"] == 3 and each["output"]["done"] == 0
        seen = []
        for index in range(3):
            assert _ready_bodies(c, run_id) == [index]   # one at a time (concurrency 1)
            claimed = _claim(c, project, run_id)
            assert claimed["step_id"] == "visit" and claimed["iteration"] == index
            assert claimed["input"]["item"] == f"u{index}"
            assert claimed["input"]["item_index"] == index
            seen.append(claimed["input"]["item"])
            _complete(c, project, claimed, {"visited": claimed["input"]["item"]})
            if index < 2:
                assert _row(c, run_id, "each")["output"]["done"] == index + 1
        each = _row(c, run_id, "each")
        assert each["status"] == "succeeded"
        assert each["output"]["results"] == [{"visited": "u0"}, {"visited": "u1"},
                                             {"visited": "u2"}]
        assert each["output"]["count"] == 3
        _finish(c, project, run_id, "after")
        assert _run_row(c, run_id)["status"] == "completed"
        started = _events(c, run_id, "workflow.for_each_started")
        items = _events(c, run_id, "workflow.for_each_item_started")
        finished = _events(c, run_id, "workflow.for_each_finished")
        assert [e["count"] for e in started] == [3]
        assert [e["item_index"] for e in items] == [0, 1, 2]
        assert [e["count"] for e in finished] == [3]


def test_concurrency_runs_that_many_at_once_and_results_follow_items_not_finish(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, _spec(concurrency=2), {"urls": ["u0", "u1", "u2"]})
        _finish(c, project, run_id, "a")
        assert _ready_bodies(c, run_id) == [0, 1]
        first = _claim(c, project, run_id)
        second = _claim(c, project, run_id)
        assert {first["iteration"], second["iteration"]} == {0, 1}
        assert _claim(c, project, run_id) is None          # never more than two at once
        later = second if second["iteration"] == 1 else first
        earlier = first if later is second else second
        _complete(c, project, later, {"n": 1})             # item 1 finishes first
        assert _ready_bodies(c, run_id) == [0, 2]
        third = _claim(c, project, run_id)
        assert third["iteration"] == 2
        _complete(c, project, third, {"n": 2})
        assert _row(c, run_id, "each")["status"] == "waiting"
        _complete(c, project, earlier, {"n": 0})
        each = _row(c, run_id, "each")
        assert each["status"] == "succeeded"
        assert [r["n"] for r in each["output"]["results"]] == [0, 1, 2]


def test_items_from_an_earlier_steps_output(eng, project):
    steps = _spec(items="steps.a.output.urls")
    with eng.begin() as c:
        run_id = _run(c, project, steps)
        _finish(c, project, run_id, "a", {"urls": ["p", "q"]})
        assert _claim(c, project, run_id)["input"]["item"] == "p"


def test_an_empty_list_skips_the_body_and_succeeds_with_nothing(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, _spec(), {"urls": []})
        _finish(c, project, run_id, "a")
        each = _row(c, run_id, "each")
        assert each["status"] == "succeeded"
        assert each["output"]["results"] == [] and each["output"]["count"] == 0
        body = _rows(c, run_id, "visit")
        assert [r["status"] for r in body] == ["skipped"]
        assert body[0]["output"]["reason"] == "no_items"
        _finish(c, project, run_id, "after")
        assert _run_row(c, run_id)["status"] == "completed"


def test_a_list_over_max_items_is_refused_not_cut_short(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, _spec(max_items=2), {"urls": ["u0", "u1", "u2"]})
        _finish(c, project, run_id, "a")
        each = _row(c, run_id, "each")
        assert each["status"] == "failed"
        assert "3 items" in each["last_error"] and "at most 2" in each["last_error"]
        assert [r["status"] for r in _rows(c, run_id, "visit")] == ["pending"]
        run = _run_row(c, run_id)
        assert run["status"] == "failed"
        assert "3 items" in run["blocked_reason"]
        assert _events(c, run_id, "workflow.for_each_failed")


def test_a_value_that_is_not_a_list_is_refused_with_what_it_was(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, _spec(), {"urls": "just-one"})
        _finish(c, project, run_id, "a")
        each = _row(c, run_id, "each")
        assert each["status"] == "failed"
        assert "not a list" in each["last_error"] and "input.urls" in each["last_error"]


def test_a_failed_item_fails_the_for_each_after_its_own_attempts_and_retry_reruns_it(
        eng, project):
    steps = _spec(concurrency=1)
    steps[2] = {**steps[2], "max_attempts": 2}
    with eng.begin() as c:
        run_id = _run(c, project, steps, {"urls": ["u0", "u1", "u2"]})
        _finish(c, project, run_id, "a")
        _finish(c, project, run_id, "visit", {"ok": 0})
        for attempt in (1, 2):
            _due_now(c, run_id)
            claimed = _claim(c, project, run_id)
            assert claimed["iteration"] == 1
            workflows.fail_step(c, project_id=project, step_run_id=claimed["id"],
                                worker_id=WORKER, error="the page was gone")
            if attempt == 1:
                # It still has an attempt: the for_each waits on it.
                assert _row(c, run_id, "each")["status"] == "waiting"
        each = _row(c, run_id, "each")
        assert each["status"] == "failed"
        assert "item 2 of 3" in each["last_error"] and "the page was gone" in each["last_error"]
        assert [r["status"] for r in _rows(c, run_id, "visit")] == ["succeeded", "deadlettered"]
        run = _run_row(c, run_id)
        assert run["status"] == "failed"
        assert "item 2 of 3" in run["blocked_reason"]
        assert _events(c, run_id, "workflow.for_each_failed")

        result = workflows.retry_failed_step(c, project_id=project, run_id=run_id,
                                             step_id="each", actor="test")
        assert result["run_status"] == "running"
        assert _row(c, run_id, "each")["status"] == "waiting"
        _due_now(c, run_id)
        claimed = _claim(c, project, run_id)
        assert claimed["step_id"] == "visit" and claimed["iteration"] == 1
        _complete(c, project, claimed, {"ok": 1})
        _finish(c, project, run_id, "visit", {"ok": 2})
        each = _row(c, run_id, "each")
        assert each["status"] == "succeeded"
        assert each["output"]["results"] == [{"ok": 0}, {"ok": 1}, {"ok": 2}]
        _finish(c, project, run_id, "after")
        assert _run_row(c, run_id)["status"] == "completed"


def test_skipping_a_failed_for_each_carries_the_run_on(eng, project):
    steps = _spec()
    steps[2] = {**steps[2], "max_attempts": 1}
    with eng.begin() as c:
        run_id = _run(c, project, steps, {"urls": ["u0"]})
        _finish(c, project, run_id, "a")
        claimed = _claim(c, project, run_id)
        workflows.fail_step(c, project_id=project, step_run_id=claimed["id"],
                            worker_id=WORKER, error="no")
        assert _run_row(c, run_id)["status"] == "failed"
        workflows.skip_failed_step(c, project_id=project, run_id=run_id, step_id="each",
                                   actor="test", reason="not needed today")
        _finish(c, project, run_id, "after")
        assert _run_row(c, run_id)["status"] == "completed"

"""The plan an operator agrees to, rather than the graph a builder edits.

    "I want to see the workflow and its definition, what it will do, then trigger the run…
     we need to know what kind of results it will bring, any agents associated or any task
     associated — right now those info are missing."

The builder drew four labels and four approval dropdowns. What each step DOES, what it
PRODUCES, which agent CAPTAINS it, where each input comes FROM, and which steps stop and wait
were all already known — in the capability palette, in the stored bindings, in the custom-step
table — and none of it was ever joined up and shown.

Executed against a database, not grepped: this is a join across three sources and the only way
to know it holds together is to run it.
"""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.services.workflow_explain import explain_version

SPEC = {
    "entry_step": "edit",
    "input_schema": {"media_artifact_id": "string", "title": "string"},
    "steps": [
        {"id": "edit", "kind": "capability", "next": ["publish"],
         "binding": {"capability": "video_edit",
                     "inputs": {"media_artifact_id": {"from_input": "media_artifact_id"}}}},
        {"id": "publish", "kind": "capability", "next": [],
         "approval": {"mode": "auto"},
         "binding": {"capability": "social_publish",
                     "inputs": {"media_artifact_id": {"from_step": "edit",
                                                      "field": "media_artifact_id"},
                                "title": {"from_input": "title"}}}},
    ],
}


def _seed(engine, project_id: str, key: str = "plan_demo") -> None:
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
            "        description, created_by, active_version_id) "
            "VALUES ('wfd_p1', :p, :k, 'Family channel video', 'Cut it and publish it.', "
            "        'ceo', 'wfv_p1') ON CONFLICT (id) DO NOTHING"),
            {"p": project_id, "k": key})
        c.execute(text(
            "INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, "
            "        status, spec, content_hash, created_by) "
            "VALUES ('wfv_p1', :p, 'wfd_p1', 1, 'published', CAST(:s AS JSONB), :h, 'ceo') "
            "ON CONFLICT (id) DO NOTHING"),
            {"p": project_id, "s": json.dumps(SPEC),
             "h": hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode()).hexdigest()})


def _plan(engine, project_id):
    with engine.begin() as c:
        return explain_version(c, project_id=project_id, workflow_key="plan_demo")


def test_every_step_says_what_it_does(engine):
    """A step called "Document" tells you nothing about what documenting is."""
    pid = get_settings().default_project_id
    _seed(engine, pid)
    plan = _plan(engine, pid)
    assert len(plan["steps"]) == 2
    for step in plan["steps"]:
        assert step["does"], f"step {step['id']} has no description of what it does"
        assert step["label"]


def test_every_step_names_what_it_actually_executes(engine):
    """Most built-in capabilities declare no skill — they ARE the unit of work.

    `runs` fell through to empty for every step of a normal workflow, so the field vanished
    from the panel and "what does this actually execute" had no answer on screen.
    """
    pid = get_settings().default_project_id
    _seed(engine, pid)
    plan = _plan(engine, pid)
    assert all(s["runs"] for s in plan["steps"]), \
        [s for s in plan["steps"] if not s["runs"]]


def test_each_step_names_the_agent_that_answers_for_it(engine):
    """"any agents associated" — the captain was decided by the capability and never shown."""
    pid = get_settings().default_project_id
    _seed(engine, pid)
    plan = _plan(engine, pid)
    assert all(s["agent"] for s in plan["steps"])
    assert plan["agents"], "the plan does not say which agents are involved"


def test_inputs_say_where_they_come_from_in_words(engine):
    """`{"from_step": "edit"}` is precise and unreadable."""
    pid = get_settings().default_project_id
    _seed(engine, pid)
    plan = _plan(engine, pid)
    by_id = {s["id"]: s for s in plan["steps"]}

    first = {i["name"]: i["source"] for i in by_id["edit"]["inputs"]}
    assert "you supply it" in first["media_artifact_id"]

    second = {i["name"]: i["source"] for i in by_id["publish"]["inputs"]}
    # Named by LABEL, not by step id, because that is what the operator sees on screen.
    assert "Edit" in second["media_artifact_id"] or "step" in second["media_artifact_id"]


def test_it_says_what_the_operator_must_supply_to_start_it(engine):
    pid = get_settings().default_project_id
    _seed(engine, pid)
    plan = _plan(engine, pid)
    assert set(plan["you_must_supply"]) == {"media_artifact_id", "title"}


def test_it_says_what_results_it_will_bring(engine):
    """"we need to know what kind of results it will bring"."""
    pid = get_settings().default_project_id
    _seed(engine, pid)
    plan = _plan(engine, pid)
    assert plan["results"], "the plan does not say what it produces"


def test_a_forced_gate_is_shown_as_forced_not_as_a_choice(engine):
    """The spec asks for `auto` on publish; the risk floor overrides it.

    A plan that reads "auto" next to a step that will stop and wait is a plan nobody can
    trust — so the EFFECTIVE mode is reported, with the reason.
    """
    pid = get_settings().default_project_id
    _seed(engine, pid)
    plan = _plan(engine, pid)
    publish = next(s for s in plan["steps"] if s["id"] == "publish")
    assert publish["approval"] == "manual"
    assert publish["approval_is_forced"] is True
    assert "risk tier" in publish["approval_note"]
    assert plan["gates"] >= 1


def test_the_summary_is_one_paragraph_a_person_would_read(engine):
    pid = get_settings().default_project_id
    _seed(engine, pid)
    plan = _plan(engine, pid)
    summary = plan["summary"]
    assert "→" in summary                      # the chain, in order
    assert "You supply" in summary             # what it needs from them
    assert "approval" in summary               # that it will stop


def test_a_definition_with_no_version_explains_itself_instead_of_404ing(engine):
    """The builder creates a definition before anything is saved."""
    pid = get_settings().default_project_id
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO workflow_definitions (id, project_id, workflow_key, name, created_by) "
            "VALUES ('wfd_p2', :p, 'empty_demo', 'Nothing yet', 'ceo') "
            "ON CONFLICT (id) DO NOTHING"), {"p": pid})
        plan = explain_version(c, project_id=pid, workflow_key="empty_demo")
    assert plan["published"] is False
    assert plan["steps"] == []
    assert "no published version" in plan["summary"]


def test_custom_step_object_inputs_render_a_plan_over_http(engine, client):
    """A real custom-step schema must not make the operator's plan throw 500."""
    project_id = get_settings().default_project_id
    suffix = uuid4().hex[:10]
    key = f"video_plan_{suffix}"
    step_key = f"publish_video_{suffix}"
    definition_id = f"wfd_{suffix}"
    version_id = f"wfv_{suffix}"
    custom_inputs = [
        {"name": "title", "required": True, "description": "Publication title"},
        {"name": "channel", "required": False, "description": "Optional channel"},
    ]
    spec = {
        "entry_step": "publish",
        "steps": [{
            "id": "publish", "kind": "capability", "next": [],
            "binding": {
                "custom_step": step_key,
                "inputs": {
                    "title": {"from_input": "title"},
                },
            },
        }],
    }
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO workflow_custom_steps (
                id, project_id, step_key, label, summary, binding_kind,
                skill_id, inputs, produces, created_by
            ) VALUES (
                :id, :project_id, :step_key, 'Publish video', 'Publishes a video',
                'skill', 'social_publish', CAST(:inputs AS JSONB),
                '["published_url"]'::jsonb, 'ceo'
            )
        """), {
            "id": f"wcs_{suffix}", "project_id": project_id,
            "step_key": step_key, "inputs": json.dumps(custom_inputs),
        })
        connection.execute(text("""
            INSERT INTO workflow_definitions (
                id, project_id, workflow_key, name, created_by, active_version_id
            ) VALUES (
                :id, :project_id, :key, 'Video edit and publish', 'ceo', :version_id
            )
        """), {"id": definition_id, "project_id": project_id,
                 "key": key, "version_id": version_id})
        connection.execute(text("""
            INSERT INTO workflow_versions (
                id, project_id, workflow_definition_id, version, status,
                spec, content_hash, created_by
            ) VALUES (
                :id, :project_id, :definition_id, 1, 'published',
                CAST(:spec AS JSONB), :content_hash, 'ceo'
            )
        """), {
            "id": version_id, "project_id": project_id,
            "definition_id": definition_id, "spec": json.dumps(spec),
            "content_hash": hashlib.sha256(
                json.dumps(spec, sort_keys=True).encode()).hexdigest(),
        })

    response = client.get(f"/v1/delivery-workflows/{key}/plan",
                          params={"project_id": project_id})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["published"] is True
    assert plan["you_must_supply"] == ["title"]
    assert plan["can_start"] is True
    inputs = {item["name"]: item for item in plan["steps"][0]["inputs"]}
    assert inputs["title"]["required"] is True
    assert inputs["channel"]["required"] is False
    assert inputs["channel"]["ready"] is True
    assert "optional" in inputs["channel"]["source"]
    assert "may supply" in inputs["channel"]["source"]

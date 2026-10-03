"""The workflow editor's endpoints, driven the way the console drives them.

    "past experience was very poor quality for workflow working e2e (especially the management
     one)"

What went wrong before, each pinned here against a real database:

  * Save was two calls — create a definition, then publish into it — so a refused publish left
    an empty workflow behind, and saving a standard again made another "(copy)". One project
    had fifty-four of them. `/publish` is one transaction: a refused save writes nothing, a
    standard is copied once, and the copy is then updated rather than copied again.
  * The builder learned what was wrong one problem per save. `/check` returns every problem at
    once, each on its step and field — and it is the collector publish raises from, so Check
    and Save can never disagree.
  * A standard could be published over in place through the generic versions endpoint.
  * Describe wrote a workflow before the operator had seen it. `/draft` writes nothing.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

PROJECT = "boss-dogfood"
STANDARD = "standard_product_delivery"
FIXTURES = Path(__file__).resolve().parents[2] / "web/tests/fixtures/catalogue-specs.json"


# ── helpers ───────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _catalogue(client):
    installed = client.post("/v1/delivery-workflows/catalogue/install",
                            json={"project_id": PROJECT, "actor": "ceo"})
    assert installed.status_code == 200, installed.text


def _count(engine, sql: str, **params) -> int:
    with engine.begin() as c:
        return int(c.execute(text(sql), {"p": PROJECT, **params}).scalar() or 0)


def _own_definitions(engine) -> int:
    return _count(engine, "SELECT count(*) FROM workflow_definitions WHERE project_id = :p "
                          "AND NOT COALESCE(is_catalogue, false)")


def _writes(engine) -> tuple[int, ...]:
    """Every table a save, a draft or a check could write to."""
    return tuple(_count(engine, f"SELECT count(*) FROM {table} WHERE project_id = :p")
                 for table in ("workflow_definitions", "workflow_versions",
                               "workflow_custom_steps", "events"))


def _post(client, path: str, **body):
    return client.post(f"/v1/delivery-workflows/{path}",
                       json={"project_id": PROJECT, "actor": "ceo", **body})


def _standard(client) -> dict:
    loaded = client.get(f"/v1/delivery-workflows/{STANDARD}/spec",
                        params={"project_id": PROJECT})
    assert loaded.status_code == 200, loaded.text
    return loaded.json()


def _name() -> str:
    return f"Editor test {uuid4().hex[:8]}"


def _media_spec(**publish_extra) -> dict:
    return {
        "entry_step": "edit",
        "input_schema": {"media_artifact_id": "string", "title": "string"},
        "steps": [
            {"id": "edit", "kind": "capability", "next": ["gate"],
             "binding": {"capability": "video_edit",
                         "inputs": {"media_artifact_id": {"from_input": "media_artifact_id"}}}},
            {"id": "gate", "kind": "decision", "captain": "human", "next": ["publish"],
             "question": "Publish this cut?"},
            {"id": "publish", "kind": "capability", "next": [],
             "approval": {"mode": "manual"},
             "binding": {"capability": "social_publish",
                         "inputs": {"media_artifact_id": {"from_step": "edit",
                                                          "field": "media_artifact_id"},
                                    "title": {"from_input": "title"}}},
             **publish_extra},
        ],
    }


#: Three different things wrong, on three steps: an input nothing supplies, an agent the
#: project does not have, and an approval gate that does not say what it asks.
THREE_WRONG = {
    "entry_step": "research",
    "input_schema": {"directive": "string"},
    "steps": [
        {"id": "research", "kind": "capability", "captain": "no_such_agent_here",
         "binding": {"capability": "research", "inputs": {"seed": {"from_input": "directive"}}},
         "next": ["preview"]},
        {"id": "preview", "kind": "capability", "binding": {"capability": "preview",
                                                            "inputs": {}},
         "next": ["gate"]},
        {"id": "gate", "kind": "decision", "captain": "human", "next": []},
    ],
}


# ── Check ─────────────────────────────────────────────────────────────────────────────────────


def test_check_names_every_problem_in_one_answer_each_on_its_step_and_field(client, engine):
    before = _writes(engine)
    checked = _post(client, "check", spec=THREE_WRONG)
    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert body["ok"] is False
    where = {(p["step_id"], p["field"]) for p in body["problems"]}
    assert ("preview", "run_id") in where, body["problems"]
    assert ("research", "captain") in where, body["problems"]
    assert ("gate", "question") in where, body["problems"]
    for item in body["problems"]:
        assert set(item) == {"step_id", "field", "message"}
        # The editor already shows the step; the message does not repeat it.
        assert not item["message"].lower().startswith("step '")
    assert body["summary"]["steps"] == 3
    assert _writes(engine) == before, "a check wrote something"


def test_check_passes_a_standard_and_sums_it_up(client):
    standard = _standard(client)
    body = _post(client, "check", spec=standard["spec"],
                 definition_id=standard["definition_id"]).json()
    assert body["ok"] is True, body["problems"]
    summary = body["summary"]
    assert summary["steps"] == len(standard["spec"]["steps"])
    assert summary["approvals"], "the standard stops for a person somewhere"
    assert summary["typical_minutes"] and summary["typical_minutes"] > 0
    assert "model" in summary["needs"]


def _check_equals_publish_cases() -> list[tuple[str, dict]]:
    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    cases = [(w["workflow_key"], w["spec"]) for w in fixtures["catalogue"]]
    cases.append(("branched", fixtures["branched"]["spec"]))
    cases.append(("three_wrong", THREE_WRONG))
    cases.append(("unknown_scope", {
        "entry_step": "work",
        "steps": [{"id": "work", "kind": "capability", "next": [],
                   "binding": {"agent_work": True, "skills": ["no_such_skill_anywhere"],
                               "inputs": {"instructions": "Do the work."}}}]}))
    cases.append(("long_title", _media_spec(title="x" * 81)))
    return cases


def test_what_check_passes_save_publishes_and_what_it_refuses_save_refuses_alike(client, engine):
    """One collector behind both: a spec Check passes is published by Save, and one it fails is
    refused by Save with exactly the same problems — and nothing written."""
    for label, spec in _check_equals_publish_cases():
        checked = _post(client, "check", spec=spec).json()
        before = _own_definitions(engine)
        saved = _post(client, "publish", definition_id=None, name=_name(), spec=spec)
        if checked["ok"]:
            assert saved.status_code == 200, (label, saved.text)
            assert saved.json()["created"] is True
            assert _own_definitions(engine) == before + 1
        else:
            assert saved.status_code == 422, (label, saved.text)
            assert saved.json()["problems"] == checked["problems"], label
            assert _own_definitions(engine) == before, (label, "a refused save wrote")


# ── Publish: one copy, updated, never duplicated ──────────────────────────────────────────────


def test_saving_a_standard_makes_one_copy_and_saving_again_updates_it(client, engine):
    standard = _standard(client)
    assert standard["is_catalogue"] is True
    before = _own_definitions(engine)
    name = _name()

    first = _post(client, "publish", definition_id=standard["definition_id"], name=name,
                  spec=standard["spec"], from_workflow_key=STANDARD)
    assert first.status_code == 200, first.text
    made = first.json()
    assert made["created"] is True and made["version"] == 1
    assert made["definition"]["workflow_key"] != STANDARD
    assert _own_definitions(engine) == before + 1

    # The standard now knows its copy, so the editor offers "Update my copy".
    again = _standard(client)
    assert again["copy"]["definition_id"] == made["definition"]["id"]
    assert again["version"] == standard["version"], "the standard itself was changed"

    changed = copy.deepcopy(standard["spec"])
    changed["steps"][0]["title"] = "Look into it first"
    second = _post(client, "publish", definition_id=standard["definition_id"], name=name,
                   spec=changed, from_workflow_key=STANDARD,
                   update_copy_id=made["definition"]["id"])
    assert second.status_code == 200, second.text
    assert second.json()["created"] is False
    assert second.json()["version"] == 2
    assert second.json()["definition"]["id"] == made["definition"]["id"]
    assert _own_definitions(engine) == before + 1, "saving the copy again made another one"

    # Nothing changed: nothing published, and the editor says so.
    third = _post(client, "publish", definition_id=made["definition"]["id"], name=name,
                  spec=changed)
    assert third.json() == {"unchanged": True}

    # The library groups it under its source and knows it has never run.
    rows = {r["id"]: r for r in client.get(
        "/v1/delivery-workflows", params={"project_id": PROJECT}).json()["items"]}
    row = rows[made["definition"]["id"]]
    assert row["forked_from_workflow_key"] == STANDARD
    assert row["forked_from_name"] == standard["name"]
    assert row["run_count"] == 0 and row["last_run"] is None
    assert row["version"] == 2 and row["definition_id"] == row["id"]


def test_a_refused_save_of_a_new_workflow_leaves_nothing_behind(client, engine):
    before = _writes(engine)
    refused = _post(client, "publish", definition_id=None, name=_name(), spec=THREE_WRONG)
    assert refused.status_code == 422, refused.text
    assert len(refused.json()["problems"]) >= 3
    assert _writes(engine) == before


def test_a_name_already_in_use_is_refused_with_one_that_is_free(client, engine):
    name = _name()
    made = _post(client, "publish", definition_id=None, name=name, spec=_media_spec())
    assert made.status_code == 200, made.text
    before = _own_definitions(engine)
    clash = _post(client, "publish", definition_id=None, name=f"  {name.upper()} ",
                  spec=_media_spec())
    assert clash.status_code == 409, clash.text
    assert clash.json()["detail"] == "Name in use"
    assert clash.json()["suggested_name"].casefold() == f"{name} 2".casefold()
    assert _own_definitions(engine) == before


def test_update_my_copy_keeps_the_copys_name_and_no_copy_takes_a_standards(client, engine):
    """The editor titles a standard with the standard's name and sent it back on "Update my
    copy" — which renamed the copy to the standard's name, so the library listed the two under
    one name. Only standards were left out of the name check, so nothing refused it."""
    standard = _standard(client)
    mine = _name()
    made = _post(client, "publish", definition_id=standard["definition_id"], name=mine,
                 spec=standard["spec"], from_workflow_key=STANDARD).json()
    copy_id = made["definition"]["id"]

    for round_, title in enumerate(("Once", "Twice"), start=2):
        changed = copy.deepcopy(standard["spec"])
        changed["steps"][0]["title"] = title
        saved = _post(client, "publish", definition_id=standard["definition_id"],
                      name=standard["name"], spec=changed, from_workflow_key=STANDARD,
                      update_copy_id=copy_id)
        assert saved.status_code == 200, saved.text
        assert saved.json()["definition"]["name"] == mine, "the copy took the standard's name"
        assert saved.json()["version"] == round_ and saved.json()["created"] is False

    # Named on purpose, a standard's name is refused like any other name in use — on a new
    # copy and on a rename — offering the name the editor suggests for a copy.
    before = _own_definitions(engine)
    clash = _post(client, "publish", definition_id=standard["definition_id"],
                  name=standard["name"].upper(), spec=standard["spec"],
                  from_workflow_key=STANDARD)
    assert clash.status_code == 409, clash.text
    assert clash.json() == {"detail": "Name in use",
                            "suggested_name": f"{standard['name'].upper()} (mine)"}
    assert _own_definitions(engine) == before
    renamed = client.patch(f"/v1/delivery-workflows/{copy_id}", json={
        "project_id": PROJECT, "actor": "ceo", "name": standard["name"]})
    assert renamed.status_code == 409 and renamed.json()["detail"] == "Name in use"

    # A different name on "Update my copy" is a rename, and it sticks.
    other = _name()
    again = _post(client, "publish", definition_id=standard["definition_id"], name=other,
                  spec=standard["spec"], from_workflow_key=STANDARD, update_copy_id=copy_id)
    assert again.status_code == 200, again.text
    assert again.json()["definition"]["name"] == other


@pytest.mark.parametrize("second, outcome", [
    ("!", "the next free key"),     # a different name, the same key: race_x and race_x
    (".", "name in use"),           # the very same name
])
def test_two_saves_at_once_are_taken_in_turn(engine, second, outcome):
    """The name check and the key choice read the table and then insert, with nothing held:
    two saves that derive one key got 200 and a 409 "workflow key 'race_x' already exists",
    which the console shows as "Name in use" for a name nobody uses — and two saves of one
    name could both pass the check. The second save now waits for the first and then sees it.
    """
    import threading

    from boss_engine.services import workflows as runtime

    stem = f"Race {uuid4().hex[:6]}"
    first = engine.connect()
    transaction = first.begin()
    made = runtime.publish_from_editor(first, project_id=PROJECT, actor="ceo",
                                       name=f"{stem}.", spec=_media_spec())
    result: dict = {}

    def save_the_second():
        try:
            with engine.begin() as connection:
                result["saved"] = runtime.publish_from_editor(
                    connection, project_id=PROJECT, actor="ceo", name=f"{stem}{second}",
                    spec=_media_spec())
        except Exception as error:  # noqa: BLE001 — the assertion below names it
            result["error"] = error

    racer = threading.Thread(target=save_the_second)
    racer.start()
    racer.join(timeout=1.0)
    try:
        assert racer.is_alive(), f"the second save did not wait for the first: {result}"
    finally:
        transaction.commit()
        first.close()
        racer.join(timeout=30)
    assert not racer.is_alive()

    key = made["definition"]["workflow_key"]
    if outcome == "the next free key":
        assert "error" not in result, result
        assert result["saved"]["definition"]["workflow_key"] == f"{key}_2"
    else:
        assert isinstance(result.get("error"), runtime.WorkflowNameInUseError), result
        assert result["error"].suggested_name == f"{stem}. 2"


def test_a_title_round_trips_and_names_the_step_everywhere(client):
    name = _name()
    saved = _post(client, "publish", definition_id=None, name=name,
                  spec=_media_spec(title="Post it to the channel"))
    assert saved.status_code == 200, saved.text
    key = saved.json()["definition"]["workflow_key"]

    loaded = client.get(f"/v1/delivery-workflows/{key}/spec",
                        params={"project_id": PROJECT}).json()
    publish = next(s for s in loaded["spec"]["steps"] if s["id"] == "publish")
    assert publish["title"] == "Post it to the channel"
    assert "title" not in next(s for s in loaded["spec"]["steps"] if s["id"] == "edit"), \
        "a step nobody titled must not gain a title"

    row = next(r for r in client.get("/v1/delivery-workflows",
                                     params={"project_id": PROJECT}).json()["items"]
               if r["workflow_key"] == key)
    outline = {s["id"]: s for s in row["steps"]}
    assert outline["publish"]["title"] == "Post it to the channel"
    assert outline["publish"]["capability"] == "social_publish"
    # Untitled, a built-in step is called by its capability's label, never its key or id.
    from boss_engine.services.workflow_capabilities import get as capability

    assert outline["edit"]["title"] == capability("video_edit").label

    plan = client.get(f"/v1/delivery-workflows/{key}/plan",
                      params={"project_id": PROJECT}).json()
    assert next(s for s in plan["steps"] if s["id"] == "publish")["label"] == \
        "Post it to the channel"


def test_an_earlier_version_can_be_opened(client):
    name = _name()
    first = _post(client, "publish", definition_id=None, name=name, spec=_media_spec()).json()
    definition_id = first["definition"]["id"]
    _post(client, "publish", definition_id=definition_id, name=name,
          spec=_media_spec(title="Second"))
    key = first["definition"]["workflow_key"]
    latest = client.get(f"/v1/delivery-workflows/{key}/spec",
                        params={"project_id": PROJECT}).json()
    assert latest["version"] == 2
    assert [v["version"] for v in latest["versions"]] == [2, 1]
    assert [v["is_active"] for v in latest["versions"]] == [True, False]
    older = client.get(f"/v1/delivery-workflows/{key}/spec",
                       params={"project_id": PROJECT, "version": 1}).json()
    assert older["version"] == 1
    assert "title" not in next(s for s in older["spec"]["steps"] if s["id"] == "publish")
    missing = client.get(f"/v1/delivery-workflows/{key}/spec",
                         params={"project_id": PROJECT, "version": 9})
    assert missing.status_code == 404


# ── Rename ────────────────────────────────────────────────────────────────────────────────────


def test_renaming_publishes_no_version_and_refuses_a_standard_and_a_taken_name(client, engine):
    taken = _name()
    _post(client, "publish", definition_id=None, name=taken, spec=_media_spec())
    made = _post(client, "publish", definition_id=None, name=_name(), spec=_media_spec()).json()
    definition_id = made["definition"]["id"]
    versions = _count(engine, "SELECT count(*) FROM workflow_versions "
                              "WHERE workflow_definition_id = :d", d=definition_id)

    renamed = client.patch(f"/v1/delivery-workflows/{definition_id}", json={
        "project_id": PROJECT, "actor": "ceo", "name": "Renamed by the editor",
        "description": "Cut it, check it, post it."})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["definition"]["name"] == "Renamed by the editor"
    assert _count(engine, "SELECT count(*) FROM workflow_versions "
                          "WHERE workflow_definition_id = :d", d=definition_id) == versions
    with engine.begin() as c:
        row = c.execute(text("SELECT name, description FROM workflow_definitions "
                             "WHERE id = :d"), {"d": definition_id}).mappings().one()
    assert dict(row) == {"name": "Renamed by the editor",
                         "description": "Cut it, check it, post it."}

    clash = client.patch(f"/v1/delivery-workflows/{definition_id}", json={
        "project_id": PROJECT, "actor": "ceo", "name": taken.lower()})
    assert clash.status_code == 409 and clash.json()["detail"] == "Name in use"

    standard = _standard(client)
    refused = client.patch(f"/v1/delivery-workflows/{standard['definition_id']}", json={
        "project_id": PROJECT, "actor": "ceo", "name": "Mine now"})
    assert refused.status_code == 403, refused.text


# ── The generic versions endpoint ─────────────────────────────────────────────────────────────


def test_a_standard_cannot_be_published_over_through_the_generic_endpoint(client, engine):
    standard = _standard(client)
    changed = copy.deepcopy(standard["spec"])
    changed["steps"][0]["title"] = "Edited in place"
    before = _count(engine, "SELECT count(*) FROM workflow_versions "
                            "WHERE workflow_definition_id = :d", d=standard["definition_id"])
    refused = client.post(f"/v1/workflows/{standard['definition_id']}/versions", json={
        "project_id": PROJECT, "actor": "ceo", "spec": changed})
    assert refused.status_code == 403, refused.text
    assert _count(engine, "SELECT count(*) FROM workflow_versions "
                          "WHERE workflow_definition_id = :d",
                  d=standard["definition_id"]) == before


# ── Describe and Auto-wire ────────────────────────────────────────────────────────────────────


def test_a_draft_from_a_sentence_writes_nothing(client, engine, monkeypatch):
    from boss_engine.services import workflow_drafting

    monkeypatch.setattr(workflow_drafting, "model_connected", lambda *a, **k: True)
    answer = {
        "name": "Short video pipeline",
        "description": "Cut a clip and post it after a look.",
        "inputs": ["media_artifact_id", "title"],
        "steps": [
            {"id": "edit", "capability": "video_edit"},
            {"id": "look", "gate": {"question": "Post this cut?"}},
            {"id": "post", "capability": "social_publish", "approval": "manual"},
            {"id": "thumbs", "skill": "no_such_custom_step_or_skill"},
        ],
    }
    monkeypatch.setattr(workflow_drafting, "_ask_model",
                        lambda *a, **k: "Here it is:\n```json\n" + json.dumps(answer) + "\n```")
    before = _writes(engine)
    drafted = _post(client, "draft", description="Cut my clip and post it to the channel.")
    assert drafted.status_code == 200, drafted.text
    body = drafted.json()
    assert body["name"] == "Short video pipeline"
    steps = {s["id"]: s for s in body["spec"]["steps"]}
    assert list(steps) == ["edit", "look", "post", "thumbs"]
    # Wired the way a person would: the cut feeds the post, the title comes from the start form.
    assert steps["post"]["binding"]["inputs"]["media_artifact_id"] == {
        "from_step": "edit", "field": "media_artifact_id"}
    assert steps["post"]["binding"]["inputs"]["title"] == {"from_input": "title"}
    assert steps["look"]["kind"] == "decision" and steps["look"]["question"] == "Post this cut?"
    # A step naming something the project cannot run as a step is an agent task, not a write.
    assert steps["thumbs"]["binding"]["agent_work"] is True
    assert not any(k.startswith("_") for s in steps.values() for k in s)
    assert _writes(engine) == before, "a draft wrote something"


def test_a_draft_without_a_model_says_so(client, engine, monkeypatch):
    from boss_engine.services import workflow_drafting

    monkeypatch.setattr(workflow_drafting, "model_connected", lambda *a, **k: False)
    asked = []
    monkeypatch.setattr(workflow_drafting, "_ask_model",
                        lambda *a, **k: asked.append(1) or "{}")
    refused = _post(client, "draft", description="Anything at all.")
    assert refused.status_code == 409 and refused.json()["detail"] == "no model"
    assert asked == []


def test_auto_wire_follows_the_graph_not_the_list(client, engine):
    """A sibling branch is not upstream: what it makes cannot feed the other branch."""
    spec = {
        "entry_step": "split",
        "input_schema": {"type": "object", "required": [],
                         "properties": {"media_artifact_id": {"type": "string"},
                                        "title": {"type": "string"}}},
        "steps": [
            {"id": "split", "kind": "parallel", "branches": ["cut", "post_raw"], "join": "both",
             "next": []},
            {"id": "cut", "kind": "capability", "next": ["both"],
             "binding": {"capability": "video_edit", "inputs": {}}},
            {"id": "post_raw", "kind": "capability", "next": ["both"],
             "binding": {"capability": "social_publish", "inputs": {}}},
            {"id": "both", "kind": "join", "wait_for": ["cut", "post_raw"], "next": ["post"]},
            {"id": "post", "kind": "capability", "next": [],
             "binding": {"capability": "social_publish",
                         "inputs": {"title": {"value": "Fixed title"}}}},
        ],
    }
    before = _writes(engine)
    wired = _post(client, "wire", spec=spec)
    assert wired.status_code == 200, wired.text
    body = wired.json()
    steps = {s["id"]: s for s in body["spec"]["steps"]}
    assert steps["cut"]["binding"]["inputs"]["media_artifact_id"] == {
        "from_input": "media_artifact_id"}
    assert steps["post_raw"]["binding"]["inputs"]["media_artifact_id"] == {
        "from_input": "media_artifact_id"}, "wired from a sibling branch"
    assert steps["post"]["binding"]["inputs"]["media_artifact_id"] == {
        "from_step": "cut", "field": "media_artifact_id"}
    assert steps["post"]["binding"]["inputs"]["title"] == {"value": "Fixed title"}, \
        "an input already set was changed"
    assert {"step_id": "post", "input": "media_artifact_id",
            "source": {"from_step": "cut", "field": "media_artifact_id"}} in body["wired"]
    # The branches themselves are untouched.
    assert steps["split"]["branches"] == ["cut", "post_raw"]
    assert _writes(engine) == before


# ── The agent's delete is the console's delete ───────────────────────────────────────────────


def test_the_agents_delete_retires_a_workflow_that_has_run(client, engine):
    from apps.api.app.config import get_settings
    from boss_engine.context import EngineContext
    from boss_engine.tools import build_default_registry

    made = _post(client, "publish", definition_id=None, name=_name(), spec=_media_spec()).json()
    definition_id = made["definition"]["id"]
    with engine.begin() as c:
        version_id = c.execute(text("SELECT active_version_id FROM workflow_definitions "
                                    "WHERE id = :d"), {"d": definition_id}).scalar()
        c.execute(text(
            "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
            "        workflow_version_id, actor, status, input) "
            "VALUES (:r, :p, :d, :v, 'ceo', 'completed', CAST('{}' AS JSONB))"),
            {"r": f"wfr_{uuid4().hex[:10]}", "p": PROJECT, "d": definition_id, "v": version_id})

    row = next(r for r in client.get("/v1/delivery-workflows",
                                     params={"project_id": PROJECT}).json()["items"]
               if r["id"] == definition_id)
    assert row["run_count"] == 1 and row["last_run"]["status"] == "completed"

    registry = build_default_registry()
    ctx = EngineContext(settings=get_settings(), engine=engine)
    result = registry.invoke("delete_workflow", ctx, project_id=PROJECT, actor="ceo",
                             args={"workflow_key": made["definition"]["workflow_key"]},
                             operator_confirmed=True)
    assert result["status"] == "retired", result
    with engine.begin() as c:
        status = c.execute(text("SELECT status FROM workflow_definitions WHERE id = :d"),
                           {"d": definition_id}).scalar()
    assert status == "retired", "the definition of a workflow with history was deleted"

    standard = registry.invoke("delete_workflow", ctx, project_id=PROJECT, actor="ceo",
                               args={"workflow_key": STANDARD}, operator_confirmed=True)
    assert standard["ok"] is False


def test_the_agents_draft_refuses_before_it_creates_anything(engine):
    """The tool validated with its own list, created the definition, and only then published —
    so a spec publish refused (here, a gate with no question) left an empty workflow behind.
    It now asks the collector publish raises from first."""
    from apps.api.app.config import get_settings
    from boss_engine.context import EngineContext
    from boss_engine.tools import build_default_registry

    registry = build_default_registry()
    ctx = EngineContext(settings=get_settings(), engine=engine)
    key = f"editor_draft_{uuid4().hex[:8]}"
    before = _own_definitions(engine)
    refused = registry.invoke("draft_workflow", ctx, project_id=PROJECT, actor="ceo", args={
        "workflow_key": key, "name": _name(), "inputs": ["media_artifact_id"],
        "steps": [{"id": "edit", "capability": "video_edit"}, {"id": "look", "gate": True}]})
    assert refused["ok"] is False, refused
    assert "no question" in refused["error"]
    assert _own_definitions(engine) == before, "the refused draft left a definition behind"

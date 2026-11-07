"""People see and steer ambient work over HTTP (BOSSAPP-902; docs/product/AUTONOMOUS_AGENTS_SPEC.md
§7; routers/ambient.py).

* The board lists as flow diagrams (badges + a strip per item) and opens one item as its graph.
* A person takes an item over or dismisses it; overrides a decision (once); reads and changes
  the autonomy matrix within the catalogue's ceilings; stops and resumes ambient work.
* Permissions run the safe direction easy: a viewer reads; an operator may also take over,
  dismiss and STOP; only an owner/admin (autonomy:manage) may RESUME or give agents more room.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main

HOME = "boss-dogfood"


def _item(engine, project: str = HOME, *, task_class: str = "doc_drift") -> dict:
    from boss_engine.services.ambient import board, signals

    fp = f"fp:http-{uuid4().hex[:8]}"
    with engine.begin() as c:
        sig = signals.record_signal(c, project_id=project, source="scan", kind="docs.drift",
                                    title="README names a removed env var",
                                    fingerprint_value=fp, source_ref=fp)["signal"]
        return board.open_item(c, api_main.settings, project_id=project, fingerprint=fp,
                               title="README drift", task_class=task_class,
                               subject_ref="repo:acme/shop", signal_ids=[sig["id"]],
                               actor="agent:chief_of_staff")["item"]


def _decision(engine, item: dict, project: str = HOME) -> dict:
    from boss_engine.services.ambient import ledger

    with engine.begin() as c:
        return ledger.record_decision(
            c, project_id=project, decided_by="agent:docs_writer", question="Fix the README?",
            chosen="propose_fix", options=["propose_fix", "ignore"],
            rationale="The env var was removed in 6bf2673.", reversibility="reversible",
            undo={"action": "dismiss"}, evidence=[{"kind": "work_item", "ref": item["id"]}],
            work_item_id=item["id"], action="propose")


@pytest.fixture()
def no_stop(engine):
    yield
    with engine.begin() as c:
        c.execute(text("DELETE FROM ambient_settings WHERE project_id = :p"), {"p": HOME})
        c.execute(text("DELETE FROM system_state WHERE key = 'ambient'"))


def test_the_board_lists_items_as_strips_and_opens_one_as_its_graph(client, engine):
    item = _item(engine)
    listed = client.get("/v1/ambient/work-items", params={"project_id": HOME,
                                                           "filter": "running"})
    assert listed.status_code == 200, listed.text
    row = next(i for i in listed.json()["items"] if i["id"] == item["id"])
    assert row["badges"]["problem_type"] == "Doc drift" and row["strip"][0]["type"] == "signal"
    assert listed.json()["counts"]["running"] >= 1
    one = client.get(f"/v1/ambient/work-items/{item['id']}", params={"project_id": HOME})
    assert one.status_code == 200, one.text
    body = one.json()
    assert {"item", "badges", "nodes", "edges", "links"} <= set(body)
    assert body["nodes"][0]["type"] == "signal" and body["edges"][0]["kind"] == "raised"
    assert client.get("/v1/ambient/work-items", params={"project_id": HOME,
                                                        "filter": "bogus"}).status_code == 422


def test_a_person_takes_an_item_over_and_dismisses_another(client, engine):
    from boss_engine.services.ambient import board

    item = _item(engine)
    with engine.begin() as c:
        board.claim(c, item["id"], owner="agent:docs_writer")
    taken = client.post(f"/v1/ambient/work-items/{item['id']}/take-over",
                        json={"project_id": HOME, "actor": "ceo", "reason": "mine now"})
    assert taken.status_code == 200, taken.text
    assert taken.json()["item"]["lease_owner"] == "person:ceo"
    messages = client.get(f"/v1/ambient/work-items/{item['id']}/messages",
                          params={"project_id": HOME}).json()["messages"]
    assert [m["kind"] for m in messages] == ["claim", "handoff"]
    other = _item(engine)
    assert client.post(f"/v1/ambient/work-items/{other['id']}/dismiss",
                       json={"project_id": HOME, "actor": "ceo"}).status_code == 422
    gone = client.post(f"/v1/ambient/work-items/{other['id']}/dismiss",
                       json={"project_id": HOME, "actor": "ceo", "reason": "already fixed"})
    assert gone.status_code == 200 and gone.json()["item"]["lifecycle"] == "dismissed"
    again = client.post(f"/v1/ambient/work-items/{other['id']}/dismiss",
                        json={"project_id": HOME, "actor": "ceo", "reason": "twice"})
    assert again.status_code == 409


def test_a_person_overrides_a_decision_once(client, engine):
    item = _item(engine)
    decision = _decision(engine, item)
    listed = client.get("/v1/ambient/decisions", params={"project_id": HOME,
                                                         "work_item_id": item["id"]})
    assert [d["act_id"] for d in listed.json()["decisions"]] == [decision["act_id"]]
    over = client.post(f"/v1/ambient/decisions/{decision['act_id']}/override",
                       json={"project_id": HOME, "actor": "ceo", "chosen": "ignore",
                             "rationale": "Docs are frozen for the release."})
    assert over.status_code == 200, over.text
    assert over.json()["decision"]["overrides_act_id"] == decision["act_id"]
    original = client.get(f"/v1/ambient/decisions/{decision['act_id']}",
                          params={"project_id": HOME}).json()["decision"]
    assert original["outcome"] == "overridden"
    twice = client.post(f"/v1/ambient/decisions/{decision['act_id']}/override",
                        json={"project_id": HOME, "actor": "ceo", "chosen": "propose_fix",
                              "rationale": "Changed my mind."})
    assert twice.status_code == 409


def test_the_autonomy_matrix_reads_and_changes_within_the_catalogues_ceilings(client, engine):
    matrix = client.get("/v1/ambient/autonomy", params={"project_id": HOME})
    assert matrix.status_code == 200, matrix.text
    classes = {c["task_class"]: c for c in matrix.json()["classes"]}
    assert classes["doc_drift"]["level"] == "A1"
    assert {a["key"] for a in matrix.json()["actions"] if a["always_a_person"]} == {
        "merge", "deploy_prod", "send_external", "pay"}
    raised = client.put("/v1/ambient/autonomy/doc_drift",
                        json={"project_id": HOME, "actor": "ceo", "level": "A2",
                              "reason": "docs PRs have been fine"})
    assert raised.status_code == 200 and raised.json()["class"]["level"] == "A2"
    assert client.put("/v1/ambient/autonomy/iac_drift",
                      json={"project_id": HOME, "actor": "ceo", "level": "A2",
                            "ceiling": "A2"}).status_code == 422
    assert client.put("/v1/ambient/autonomy/doc_drift",
                      json={"project_id": HOME, "actor": "ceo"}).status_code == 422
    with engine.begin() as c:
        c.execute(text("DELETE FROM ambient_autonomy WHERE project_id = :p"), {"p": HOME})


def test_stop_and_resume_and_the_settings_show_the_envelope(client, engine, no_stop):
    stopped = client.post("/v1/ambient/stop", json={"project_id": HOME, "actor": "ceo",
                                                    "reason": "too noisy"})
    assert stopped.status_code == 200 and stopped.json()["stopped"] is True
    state = client.get("/v1/ambient/settings", params={"project_id": HOME}).json()
    assert state["stop"]["stopped"] and state["stop"]["reason"] == "too noisy"
    assert state["envelope"]["cap_usd_micros"] == 5_000_000
    resumed = client.post("/v1/ambient/resume", json={"project_id": HOME, "actor": "ceo"})
    assert resumed.status_code == 200 and resumed.json()["stop"]["stopped"] is False
    changed = client.put("/v1/ambient/settings", json={"project_id": HOME, "actor": "ceo",
                                                       "max_concurrent": 4})
    assert changed.status_code == 200 and changed.json()["settings"]["max_concurrent"] == 4
    assert client.put("/v1/ambient/settings", json={"project_id": HOME, "actor": "ceo",
                                                    "max_spawn_depth": 99}).status_code == 422
    everywhere = client.post("/v1/ambient/stop-all", json={"actor": "ceo", "reason": "host"})
    assert everywhere.status_code == 200 and everywhere.json()["scope"] == "global"
    assert client.post("/v1/ambient/resume-all", json={"actor": "ceo"}).status_code == 200


def test_signals_and_the_scorecard_read_and_a_sweep_runs_on_request(client, engine):
    _item(engine)
    sigs = client.get("/v1/ambient/signals", params={"project_id": HOME, "status": "triaged"})
    assert sigs.status_code == 200 and sigs.json()["signals"]
    swept = client.post("/v1/ambient/signals/sweep", json={"project_id": HOME, "actor": "ceo",
                                                           "only": ["shepherd"]})
    assert swept.status_code == 200 and set(swept.json()["adapters"]) == {"shepherd"}
    card = client.get("/v1/ambient/scorecard", params={"project_id": HOME, "days": 7})
    assert card.status_code == 200
    assert {"decisions_per_day", "by_class", "noise_rate", "cost", "guards"} <= set(card.json())


def _world(engine):
    from apps.api.tests.identity_fixtures import IdentityWorld

    return IdentityWorld.create(engine)


def test_a_viewer_reads_but_cannot_steer_or_stop(client, engine):
    world = _world(engine)
    item = _item(engine, world.project_a)
    viewer = world.person(f"viewer-{uuid4().hex[:6]}")
    world.bind(viewer, "viewer", project=world.project_a)
    pa = world.project_a
    with world.acting_as(world.ctx(viewer)):
        assert client.get("/v1/ambient/work-items", params={"project_id": pa}).status_code == 200
        assert client.get(f"/v1/ambient/work-items/{item['id']}",
                          params={"project_id": pa}).status_code == 200
        assert client.get("/v1/ambient/autonomy", params={"project_id": pa}).status_code == 200
        assert client.post("/v1/ambient/stop", json={"project_id": pa}).status_code == 403
        assert client.post(f"/v1/ambient/work-items/{item['id']}/take-over",
                           json={"project_id": pa}).status_code == 403
        assert client.put("/v1/ambient/autonomy/doc_drift",
                          json={"project_id": pa, "level": "A2"}).status_code == 403


def test_an_operator_may_stop_and_take_over_but_never_give_agents_more_room(client, engine):
    world = _world(engine)
    item = _item(engine, world.project_a)
    operator = world.person(f"operator-{uuid4().hex[:6]}")
    world.bind(operator, "operator", project=world.project_a)
    pa = world.project_a
    with world.acting_as(world.ctx(operator)):
        took = client.post(f"/v1/ambient/work-items/{item['id']}/take-over",
                           json={"project_id": pa, "reason": "on it"})
        assert took.status_code == 200, took.text
        assert took.json()["item"]["lease_owner"] == f"person:{operator.id}"
        assert client.post("/v1/ambient/stop", json={"project_id": pa,
                                                     "reason": "pause"}).status_code == 200
        assert client.post("/v1/ambient/resume", json={"project_id": pa}).status_code == 403
        assert client.put("/v1/ambient/autonomy/doc_drift",
                          json={"project_id": pa, "level": "A2"}).status_code == 403
        assert client.put("/v1/ambient/settings", json={"project_id": pa,
                                                        "max_concurrent": 9}).status_code == 403
        assert client.post("/v1/ambient/stop-all", json={}).status_code == 403


def test_an_owner_resumes_and_raises_a_level_and_another_projects_item_is_not_found(
        client, engine):
    world = _world(engine)
    pa, pb = world.project_a, world.project_b
    foreign = _item(engine, pb)
    owner = world.person(f"owner-{uuid4().hex[:6]}")
    world.bind(owner, "owner", project=pa)
    with world.acting_as(world.ctx(owner)):
        assert client.post("/v1/ambient/stop", json={"project_id": pa}).status_code == 200
        resumed = client.post("/v1/ambient/resume", json={"project_id": pa})
        assert resumed.status_code == 200, resumed.text
        raised = client.put("/v1/ambient/autonomy/doc_drift", json={"project_id": pa,
                                                                    "level": "A2"})
        assert raised.status_code == 200, raised.text
        assert raised.json()["class"]["set_by"] == owner.id
        assert client.get(f"/v1/ambient/work-items/{foreign['id']}",
                          params={"project_id": pa}).status_code == 404


def test_review_always_on_builds_is_off_by_default_and_set_per_project_class_and_install(
        client, engine, no_stop):
    """Always-on builds start on the project's Captain authority unless a person asks to review
    them first (identity/captain_authority.py): Project › Settings, a class, or Govern's default."""
    with engine.begin() as c:
        c.execute(text("DELETE FROM system_state WHERE key = 'ambient_review_builds'"))
        c.execute(text("UPDATE ambient_autonomy SET review_builds = NULL WHERE project_id = :p"), {"p": HOME})
    try:
        state = client.get("/v1/ambient/settings", params={"project_id": HOME}).json()["review_builds"]
        assert state == {"install": False, "project": None, "classes": {}, "effective": False,
                         "source": "install"}
        on = client.put("/v1/ambient/review-builds", json={"project_id": HOME, "actor": "ceo", "review": True})
        assert on.status_code == 200 and on.json()["review_builds"]["effective"] is True
        cls = client.put("/v1/ambient/review-builds", json={"project_id": HOME, "actor": "ceo",
                                                            "review": False, "task_class": "security_patch"})
        assert cls.status_code == 200 and cls.json()["review_builds"]["classes"] == {"security_patch": False}
        assert client.put("/v1/ambient/review-builds", json={"project_id": HOME, "actor": "ceo", "review": True,
                                                             "task_class": "no_such_class"}).status_code == 422
        cleared = client.put("/v1/ambient/review-builds", json={"project_id": HOME, "actor": "ceo", "review": None})
        assert cleared.json()["review_builds"]["project"] is None
        install = client.put("/v1/ambient/review-builds/install", json={"actor": "ceo", "review": True})
        assert install.status_code == 200 and install.json()["review_builds"]["install"] is True
        after = client.get("/v1/ambient/settings", params={"project_id": HOME}).json()["review_builds"]
        assert after["effective"] is True and after["source"] == "install"
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM system_state WHERE key = 'ambient_review_builds'"))
            c.execute(text("UPDATE ambient_autonomy SET review_builds = NULL WHERE project_id = :p"), {"p": HOME})

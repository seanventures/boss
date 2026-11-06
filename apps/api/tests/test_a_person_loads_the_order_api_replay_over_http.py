"""A person loads the Order API incident replay over HTTP, only where demos are on
(BOSSAPP-910; routers/incident_squad.py).

* Off by default: the demo loader answers 404 and says how to turn it on.
* With BOSS_DEMO_SCENARIOS=1 it seeds the scenario into the project, runs the squad on the
  scripted engine and returns the item's flow graph for Agent Operations.
* "Work it now" on an item that is not here is a 404.
"""
from __future__ import annotations

from sqlalchemy import text

HOME = "boss-dogfood"


def _cleanup(engine, out: dict) -> None:
    with engine.begin() as c:
        c.execute(text("SET LOCAL boss.ledger_purge = 'on'"))
        item = out["item_id"]
        c.execute(text("DELETE FROM agent_act_decisions WHERE work_item_id = :w"), {"w": item})
        c.execute(text("DELETE FROM agent_acts WHERE subject_id = :w"), {"w": item})
        for table in ("work_item_messages", "work_item_links"):
            c.execute(text(f"DELETE FROM {table} WHERE work_item_id = :w"), {"w": item})
        c.execute(text("DELETE FROM signals WHERE work_item_id = :w OR id = :s OR "
                       "(source = 'kg' AND app = 'order-api' AND project_id = :p)"),
                  {"w": item, "s": out["signal_id"], "p": HOME})
        c.execute(text("DELETE FROM work_items WHERE id = :w"), {"w": item})
        c.execute(text("DELETE FROM app_alerts WHERE incident_id = :i"),
                  {"i": out["incident_id"]})
        c.execute(text("DELETE FROM sre_postmortems WHERE incident_id = :i"),
                  {"i": out["incident_id"]})
        c.execute(text("DELETE FROM incidents WHERE id = :i"), {"i": out["incident_id"]})
        c.execute(text("DELETE FROM kg_nodes WHERE project_id = :p AND (attrs->>'demo' = "
                       "'true' OR attrs->>'incident_id' = :i OR node_key = :k)"),
                  {"p": HOME, "i": out["incident_id"], "k": f"incident:{out['incident_id']}"})


def test_the_demo_loader_is_off_unless_demos_are_on(client, monkeypatch):
    monkeypatch.delenv("BOSS_DEMO_SCENARIOS", raising=False)
    got = client.post("/v1/ambient/demo/order-api-incident",
                      json={"project_id": HOME, "actor": "ceo"})
    assert got.status_code == 404, got.text
    assert "BOSS_DEMO_SCENARIOS=1" in got.json()["detail"]


def test_with_demos_on_a_person_loads_the_replay_and_gets_its_flow(client, engine,
                                                                   monkeypatch):
    monkeypatch.setenv("BOSS_DEMO_SCENARIOS", "1")
    got = client.post("/v1/ambient/demo/order-api-incident",
                      json={"project_id": HOME, "actor": "ceo", "environment": "staging"})
    assert got.status_code == 200, got.text
    out = got.json()
    try:
        assert out["stages"] == ["resolved"]
        graph = out["graph"]
        assert graph["nodes"][0]["type"] == "signal"
        assert graph["nodes"][-1]["type"] == "outcome"
        one = client.get(f"/v1/ambient/work-items/{out['item_id']}",
                         params={"project_id": HOME})
        assert one.status_code == 200 and one.json()["item"]["lifecycle"] == "resolved"
    finally:
        _cleanup(engine, out)


def test_work_it_now_on_an_unknown_item_is_not_found(client):
    got = client.post("/v1/ambient/incidents/work_nope/route",
                      json={"project_id": HOME, "actor": "ceo"})
    assert got.status_code == 404, got.text

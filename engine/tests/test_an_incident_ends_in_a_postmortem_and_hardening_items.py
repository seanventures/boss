"""An incident the squad resolves ends in a postmortem in the knowledge graph and hardening items
for the Recommended roadmap (BOSSAPP-910; design §12.6).

* The incident is resolved on its own row, its evidence-only postmortem (sre_postmortem) holds
  the squad's verdict, and the KG incident node carries the squad's narrative: cause, the
  learning applied, the resolution, who worked it — linked to the code it was about.
* The squad leaves a precedent learning from it (the next incident's "seen before"), produced by
  the incident node. When it applied an earlier lesson and judges the cause the same, that lesson
  is STRENGTHENED (seen again, the incident added) rather than duplicated or replaced.
* Hardening items become `kg` signals (`recommendation.hardening`), at most three, each citing the
  incident, linked to the item as recommendations, and recorded once however often it closes.
"""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.services import incident_squad as squad
from boss_engine.services import incident_squad_demo as demo
from boss_engine.services.ambient import board, ledger

from ._sre_squad_support import eng, project  # noqa: F401


def _resolve(c, pid, env="staging"):
    prepared = demo.prepare(c, pid, environment=env)
    out = squad.route_incident(c, project_id=pid, item_id=prepared["item_id"],
                               mind=demo.demo_mind(env), effects=demo.FixtureEffects())
    return prepared, out


def test_the_incident_is_resolved_with_its_postmortem(eng, project):
    with eng.begin() as c:
        prepared, out = _resolve(c, project)
        inc = c.execute(text("SELECT status, lifecycle, resolved_at FROM incidents "
                             "WHERE id = :i"), {"i": prepared["incident_id"]}).mappings().one()
        pm = c.execute(text("SELECT payload FROM sre_postmortems WHERE incident_id = :i"),
                       {"i": prepared["incident_id"]}).scalar()
    assert out["stage"] == "resolved"
    assert (inc["status"], inc["lifecycle"]) == ("resolved", "resolved") and inc["resolved_at"]
    assert pm["winner_hypothesis"]["key"] == "Release 6bf2672"
    assert pm["winner_hypothesis"]["by"] == "agent:operations_engineer"
    assert pm["verify_outcome"]["state"] == "recovered"


def test_the_postmortem_is_an_incident_node_with_a_new_precedent(eng, project):
    with eng.begin() as c:
        prepared, out = _resolve(c, project)
        node = c.execute(text("SELECT id, body, attrs FROM kg_nodes WHERE project_id = :p AND "
                              "kind = 'incident' AND node_key = :k"),
                         {"p": project, "k": f"incident:{prepared['incident_id']}"}
                         ).mappings().one()
        about = set(c.execute(text("SELECT dst_id FROM kg_edges WHERE src_id = :n AND "
                                   "kind = 'about'"), {"n": node["id"]}).scalars())
        produced = c.execute(text(
            "SELECT n.id, n.attrs FROM kg_edges e JOIN kg_nodes n ON n.id = e.dst_id "
            "WHERE e.src_id = :n AND e.kind = 'produced'"), {"n": node["id"]}).mappings().all()
        close = [d for d in ledger.list_decisions(c, project, work_item_id=prepared["item_id"])
                 if d["question"] == squad.Q_CLOSE][0]
    assert out["kg_node"] == node["id"] and out["learning"] == produced[0]["id"]
    assert "Cause:" in node["body"] and "Learning applied" in node["body"]
    assert node["attrs"]["mitigation"] == "rollback"
    assert node["attrs"]["learning_used"] == prepared["precedent_id"]
    assert prepared["code"]["app.pricing.bulk_discount"] in about
    assert prepared["code"]["endpoint"] in about
    learned = produced[0]["attrs"]
    assert learned["learning_kind"] == "precedent"
    # The demo's earlier occurrence taught it; this incident strengthened the same lesson.
    assert produced[0]["id"] == prepared["precedent_id"]
    assert [i["id"] for i in learned["incidents"]][-1] == prepared["incident_id"]
    assert learned["seen_count"] == 2 and learned["incident_ref"] == "INC-1"
    assert "POST /orders" in learned["detail"]["applies_to"]
    assert {"kind": "kg_node", "ref": node["id"], "checked": "exists"} in close["evidence"]


def test_the_next_incident_finds_the_one_just_closed(eng, project):
    with eng.begin() as c:
        prepared, out = _resolve(c, project)
        found = squad.similar_incidents(c, project, app="order-api", route="POST /orders",
                                        exception="TypeError", symbols=["bulk_discount"])
    assert out["learning"] in {f["node_id"] for f in found}


def test_hardening_items_go_to_the_roadmap_as_kg_signals(eng, project):
    with eng.begin() as c:
        prepared, out = _resolve(c, project, env="production")
        assert out["stage"] == "needs_you"
        out = squad.resume(c, project_id=project, item_id=prepared["item_id"], answer="approve",
                           actor="ceo", mind=demo.demo_mind(), effects=demo.FixtureEffects())
        rows = c.execute(text("SELECT id, source, kind, title, evidence, app FROM signals "
                              "WHERE project_id = :p AND source = 'kg'"),
                         {"p": project}).mappings().all()
        links = {l["ref"] for l in board.links(c, prepared["item_id"])
                 if l["kind"] == "recommendation"}
    assert 1 <= len(rows) <= squad.MAX_HARDENING
    assert {r["kind"] for r in rows} == {"recommendation.hardening"}
    titles = {r["title"] for r in rows}
    assert "Regression test for POST /orders" in titles
    assert "Canary releases for order-api" in titles
    for r in rows:
        assert {"kind": "incident", "ref": prepared["incident_id"]} in [
            {k: e[k] for k in ("kind", "ref")} for e in r["evidence"]]
    assert links == {r["id"] for r in rows}
    assert {r["signal_id"] for r in out["recommendations"]} == links

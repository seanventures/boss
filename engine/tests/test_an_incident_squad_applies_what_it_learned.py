"""The incident squad applies what BOSS learned from past incidents, and says which learning it
used (BOSSAPP-910; design §12.4 "Applying learnings").

* A past incident with the same signature (a precedent learning in the KG) becomes a hypothesis
  with its old evidence, and its known fix is tried first: the incident node gets `consulted`
  edges to every candidate and a `reused` edge to the one applied, the precedent's reuse count
  goes up, and the ledger says "Reuse INC-1 fix" citing it.
* With no precedent the squad says so and works from today's evidence.
* An unrelated precedent is not a match.
"""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.services import incident_squad as squad
from boss_engine.services import incident_squad_demo as demo
from boss_engine.services.ambient import collab, ledger

from ._sre_squad_support import eng, project  # noqa: F401


def _edges(c, pid, kind):
    return c.execute(text(
        "SELECT s.node_key AS src, e.dst_id, e.attrs FROM kg_edges e JOIN kg_nodes s "
        "ON s.id = e.src_id WHERE e.project_id = :p AND e.kind = :k"),
        {"p": pid, "k": kind}).mappings().all()


def test_a_past_incident_with_the_same_signature_is_reused_and_recorded(eng, project):
    with eng.begin() as c:
        prepared = demo.prepare(c, project)
        squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                             mind=demo.demo_mind(), effects=demo.FixtureEffects())
        consulted = _edges(c, project, "consulted")
        reused = _edges(c, project, "reused")
        reuse = c.execute(text("SELECT attrs->'detail'->>'reuse_count' FROM kg_nodes "
                               "WHERE id = :n"), {"n": prepared["precedent_id"]}).scalar()
        learn = [d for d in ledger.list_decisions(c, project, work_item_id=prepared["item_id"])
                 if d["question"] == squad.Q_LEARN]
        hyps = collab.messages(c, prepared["item_id"], kinds=("hypothesis",))
        finding = [m for m in collab.messages(c, prepared["item_id"], kinds=("finding",))
                   if m["body"]["summary"].startswith("Applying learning")]
    key = f"incident:{prepared['incident_id']}"
    assert [(e["src"], e["dst_id"]) for e in consulted] == [(key, prepared["precedent_id"])]
    assert [(e["src"], e["dst_id"]) for e in reused] == [(key, prepared["precedent_id"])]
    assert reused[0]["attrs"]["how"] == "applied_fix"
    assert reuse == "1"
    assert learn[0]["chosen"] == "Reuse INC-1 fix"
    assert {"kind": "kg_node", "ref": prepared["precedent_id"], "checked": "exists"} in \
        learn[0]["evidence"]
    assert any(h["body"]["summary"].startswith("Seen before") for h in hyps)
    assert finding and finding[0]["sender"] == "agent:sre"


def test_without_a_precedent_the_squad_says_so_and_works_from_todays_evidence(eng, project):
    with eng.begin() as c:
        prepared = demo.prepare(c, project)
        c.execute(text("UPDATE kg_nodes SET valid_to = now() - interval '1 day' "
                       "WHERE id = :n"), {"n": prepared["precedent_id"]})
        squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                             mind=demo.demo_mind(), effects=demo.FixtureEffects())
        learn = [d for d in ledger.list_decisions(c, project, work_item_id=prepared["item_id"])
                 if d["question"] == squad.Q_LEARN]
        reused = _edges(c, project, "reused")
    assert learn[0]["chosen"] == "No precedent"
    assert reused == []


def test_an_unrelated_precedent_is_not_the_same_signature(eng, project):
    from boss_engine.services.knowledge import learning_writeback as lw

    with eng.begin() as c:
        lw.record(c, project_id=project, learning_kind="precedent",
                  title="billing: nightly export timed out",
                  statement="Nightly CSV export to the warehouse timed out",
                  detail={"symptom": "warehouse export job timeout at 02:00",
                          "cause_summary": "the warehouse was under maintenance",
                          "fix_summary": "retry after the window", "applies_to": ["billing"]},
                  about={}, evidence=[{"type": "log", "ref": "job:export",
                                       "excerpt": "timeout"}], confidence=0.5)
        found = squad.similar_incidents(c, project, app="order-api", route="POST /orders",
                                        exception="TypeError", symbols=["bulk_discount"])
    assert found == []

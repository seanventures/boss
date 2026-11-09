"""The learning loop, end to end, from two real incidents (no hand-seeded lesson).

The operator: "cover the knowledge graph where the incident learnings go, next time the learning
should be applied". So:

1. Incident 1 is worked by the squad and resolved; its close-out writes the lesson (a precedent)
   — produced by the incident, credited to it ("from INC-1").
2. Incident 2 has the same cause. The squad's triage RECALLS the lesson: it is in the agent's
   context (the investigator's verdict sees it) and in its output ("Seen before INC-1",
   "Reuse INC-1 fix", "Applying learning").
3. The fix reuses it: the fix brief cites it ([L:<id>]) with the known fix.
4. The use is recorded (kg_usage, anchored on incident 2), so the counts show: Learned on the job
   says "reused 1×", the Graph's node panel lists INC-1 (taught) and INC-2 (applied), and the
   lesson is strengthened, not duplicated (seen twice, one node).
5. The incident's flow diagrams draw "INC-1 lesson" as a node (Agents at work, Health).
6. The resolve re-evaluates the Incident hotspot detector at once (event-driven): two incidents
   with one cause put "Stop repeat incidents" on the Recommended roadmap without a manual run.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from boss_engine.services import incident_squad as squad
from boss_engine.services import incident_squad_demo as demo
from boss_engine.services.ambient import collab, ledger, policy, read_model

from ._sre_squad_support import eng, project  # noqa: F401


class Watching:
    """The scripted engine, recording what each seat was shown (the agent's context)."""

    def __init__(self, env: str):
        self.inner = demo.demo_mind(env)
        self.seen: list[tuple[str, str, dict]] = []

    def choose(self, connection, **kw):
        self.seen.append((kw["seat"], kw["kind"], dict(kw["context"])))
        return self.inner.choose(connection, **kw)


class Effects(demo.FixtureEffects):
    def __init__(self):
        self.briefs: list[str] = []

    def start_fix(self, connection, *, project_id, ctx, brief, actor):
        self.briefs.append(brief)
        return super().start_fix(connection, project_id=project_id, ctx=ctx, brief=brief,
                                 actor=actor)


def _incident(eng, pid):
    mind, effects = Watching("staging"), Effects()
    with eng.begin() as c:
        prepared = demo.prepare(c, pid, environment="staging", learn=False)
        out = squad.route_incident(c, project_id=pid, item_id=prepared["item_id"], mind=mind,
                                   effects=effects)
    assert out["stage"] == "resolved", out
    return prepared, out, mind, effects


@pytest.fixture()
def learning_on(eng):
    """The Recommended-roadmap learning runs only while always-on mode is on: a person turns it
    on in Govern for this test, and it is put back after."""
    with eng.begin() as c:
        before = c.execute(text("SELECT value FROM system_state WHERE key = :k"),
                           {"k": policy.MODE_STATE_KEY}).scalar()
        policy.set_mode(c, SimpleNamespace(ambient_cos_mode=""), mode="route",
                        actor="person:owner")
    yield
    with eng.begin() as c:
        if before is None:
            c.execute(text("DELETE FROM system_state WHERE key = :k"),
                      {"k": policy.MODE_STATE_KEY})
        else:
            c.execute(text("UPDATE system_state SET value = CAST(:v AS JSONB) WHERE key = :k"),
                      {"k": policy.MODE_STATE_KEY, "v": json.dumps(before)})


def test_one_incidents_lesson_is_recalled_applied_counted_and_drawn(eng, project, learning_on,
                                                                   monkeypatch):
    monkeypatch.delenv("BOSS_AMBIENT_COS", raising=False)
    monkeypatch.delenv("BOSS_AMBIENT_LEARNING", raising=False)

    # 1. incident 1: no lesson exists yet; resolving it writes one.
    first, out1, mind1, _ = _incident(eng, project)
    assert first["precedent_id"] is None
    lesson = out1["learning"]
    with eng.begin() as c:
        row = c.execute(text("SELECT kind, attrs FROM kg_nodes WHERE id = :n"),
                        {"n": lesson}).mappings().one()
        learn1 = [d for d in ledger.list_decisions(c, project, work_item_id=first["item_id"])
                  if d["question"] == squad.Q_LEARN]
    assert row["kind"] == "precedent" and row["attrs"]["incident_ref"] == "INC-1"
    assert row["attrs"]["incident_id"] == first["incident_id"]
    assert learn1[0]["chosen"] == "No precedent"

    # 2. incident 2, the same cause: the triage recalls it, in context and in output.
    second, out2, mind2, effects2 = _incident(eng, project)
    assert second["precedent_id"] == lesson
    verdicts = [ctx for seat, kind, ctx in mind2.seen if kind == "verdict"]
    assert any(p["node_id"] == lesson for p in verdicts[0]["precedents"]), \
        "the lesson is in the investigator's context"
    with eng.begin() as c:
        decisions = ledger.list_decisions(c, project, work_item_id=second["item_id"])
        hyps = collab.messages(c, second["item_id"], kinds=("hypothesis",))
        findings = collab.messages(c, second["item_id"], kinds=("finding",))
    assert [d["chosen"] for d in decisions if d["question"] == squad.Q_LEARN] == \
        ["Reuse INC-1 fix"]
    assert any(h["body"]["summary"].startswith("Seen before") for h in hyps)
    assert any(f["body"]["summary"].startswith("Applying learning") for f in findings)

    # 3. the fix reuses it: the brief cites the lesson with its known fix.
    assert effects2.briefs and f"[L:{lesson}]" in effects2.briefs[-1]
    assert "Known fix from INC-1" in effects2.briefs[-1]

    # 4. usage recorded and counted; the lesson strengthened, not duplicated.
    from boss_engine.services.knowledge import view
    from boss_engine.services.project_experience import knowledge as px

    with eng.begin() as c:
        usage = c.execute(text(
            "SELECT use_kind, run_id FROM kg_usage WHERE project_id = :p AND fact_id = :n"),
            {"p": project, "n": lesson}).all()
        precedents = c.execute(text(
            "SELECT count(*) FROM kg_nodes WHERE project_id = :p AND kind = 'precedent' "
            "AND valid_to IS NULL"), {"p": project}).scalar()
        attrs = c.execute(text("SELECT attrs FROM kg_nodes WHERE id = :n"),
                          {"n": lesson}).scalar()
        learned = {x["id"]: x for x in px.learned(c, project)}
        panel = view._usage(c, project, lesson)
    assert ("reused", second["incident_id"]) in {(u[0], u[1]) for u in usage}
    assert precedents == 1 and out2["learning"] == lesson
    assert attrs["seen_count"] == 2
    assert [i["ref"] for i in attrs["incidents"]] == ["INC-1", "INC-2"]
    assert learned[lesson]["reused"] == 1 and "reused 1×" in learned[lesson]["sub"]
    assert "from INC-1" in learned[lesson]["sub"]
    assert panel["reused_runs"] == 1
    assert [(i["label"], i["how"]) for i in panel["incidents"]][:1] == [("INC-1", "taught")]
    assert ("INC-2", "reused") in {(i["label"], i["how"]) for i in panel["incidents"]}

    # 5. drawn as a node of the incident's flow, in both diagrams.
    from boss_engine.services.project_experience import health

    with eng.begin() as c:
        graph = read_model.graph(c, second["item_id"], project_id=project)
        flows = health._flows(c, project, [{"id": second["item_id"], "lifecycle": "resolved",
                                            "title": "x"}])
    lessons = [n for n in graph["nodes"] if n["type"] == "learning"]
    assert [n["label"] for n in lessons] == ["INC-1 lesson"]
    assert lessons[0]["detail"].startswith("Applied learning:")
    assert any(e["from"] == lessons[0]["id"] and e["kind"] == "applied" for e in graph["edges"])
    learning_nodes = [n for n in flows[second["item_id"]] if n.get("type") == "learning"]
    assert [n["label"] for n in learning_nodes] == ["INC-1 lesson"]

    # 6. the resolve re-evaluates the Incident hotspot at once (event-driven).
    from boss_engine.services.knowledge import ingest

    ingest.ingest_queued(eng, project_id=project, limit=10)
    with eng.begin() as c:
        recs = c.execute(text(
            "SELECT detector, title FROM ambient_recommendations WHERE project_id = :p"),
            {"p": project}).mappings().all()
        node_attrs = c.execute(text(
            "SELECT attrs FROM kg_nodes WHERE project_id = :p AND kind = 'incident' "
            "AND node_key = :k"), {"p": project, "k": f"incident:{second['incident_id']}"}
        ).scalar()
    assert any(r["detector"] == "incident_cause" for r in recs), recs
    # ...and the resolve-time ingest MERGED into the squad's incident node, never replaced it.
    assert node_attrs["source_id"] == second["incident_id"]
    assert node_attrs["learning_used"] == lesson and node_attrs.get("cause")


def test_a_different_cause_keeps_both_lessons_and_never_resets_the_first(eng, project):
    """Same symptom, another cause: a second lesson, linked to the first; the first stays open
    with its count."""
    from boss_engine.services.knowledge import learning_writeback as lw

    first, out1, _, _ = _incident(eng, project)
    lesson = out1["learning"]
    with eng.begin() as c:
        c.execute(text("UPDATE kg_nodes SET attrs = jsonb_set(attrs, '{detail,reuse_count}', "
                       "'3') WHERE id = :n"), {"n": lesson})
        detail = dict(c.execute(text("SELECT attrs->'detail' FROM kg_nodes WHERE id = :n"),
                                {"n": lesson}).scalar())
        about = dict(c.execute(text("SELECT attrs->'about' FROM kg_nodes WHERE id = :n"),
                               {"n": lesson}).scalar())
        other = lw.record(
            c, project_id=project, learning_kind="precedent", title="order-api: a bad config",
            statement="POST /orders 5xx TypeError after a release: a config flag was flipped",
            detail={**detail, "cause_summary": "A config flag disabled the pricing table",
                    "reuse_count": 0},
            about={"symbols": about["symbols"]},
            evidence=[{"type": "metric", "ref": "inc_other", "excerpt": "5xx"}],
            confidence=0.7)
        still = c.execute(text("SELECT valid_to, attrs->'detail'->>'reuse_count' FROM kg_nodes "
                               "WHERE id = :n"), {"n": lesson}).one()
        link = c.execute(text("SELECT count(*) FROM kg_edges WHERE src_id = :a AND dst_id = :b "
                              "AND kind = 'correlates_with'"),
                         {"a": other["node"]["id"], "b": lesson}).scalar()
    assert still[0] is None and still[1] == "3"
    assert link == 1

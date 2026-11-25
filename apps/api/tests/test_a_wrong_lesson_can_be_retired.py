"""A wrong lesson in the knowledge graph can be retired — by the agent's `forget` and by the API.

Laptop, 2026-11-21: `forget` retired only recall memory (agent_memory). A knowledge-graph lesson
(a bad "workaround" an agent recorded, a kgn_… node) could not be retired directly; Helm had to
supersede it with another record_learning. And the close itself was wrong twice over: it wrote
valid_to from the APPLICATION clock while the AGE sync filtered `valid_to > now()` inside a long
transaction (now() = the transaction's start), so the retired node was mirrored back into the
graph; and nothing in kg_audit said it had been retired, or why.

Now: KnowledgeService.retire closes it on the database's clock, records the reason on the node
and in kg_audit, drops it from the AGE mirror, and the projection reads validity on the same
clock. Scoped to the node's own project; an agent may retire only what agents inferred.
"""
from __future__ import annotations

from sqlalchemy import text

PROJECT = "boss-dogfood"
OTHER = "northpeak-bank-b"


def _lesson(engine, *, project: str = PROJECT, tier: str = "inferred", key: str = "w1") -> str:
    from boss_engine.services.knowledge.store import PostgresKnowledgeStore

    actor_kind = "agent" if tier == "inferred" else "person"
    with engine.begin() as c:
        return PostgresKnowledgeStore().upsert_node(
            c, project_id=project, kind="workaround", node_key=f"retire-test-{key}",
            name="Restart the worker to clear the queue",
            body="When the queue sticks, restart the worker container.",
            trust_tier=tier, actor_kind=actor_kind, source="learning")


def _invoke(engine, args: dict, project: str = PROJECT) -> dict:
    from apps.api.app.config import get_settings
    from boss_engine.context import EngineContext
    from boss_engine.tools import build_default_registry

    ctx = EngineContext(settings=get_settings(), engine=engine)
    return build_default_registry().invoke("forget", ctx, project_id=project, actor="ceo",
                                           args=args)


def _node(engine, node_id: str) -> dict:
    with engine.begin() as c:
        return dict(c.execute(text(
            "SELECT valid_to, valid_to <= clock_timestamp() AS closed, attrs FROM kg_nodes "
            "WHERE id = :i"), {"i": node_id}).mappings().one())


def _audit(engine, node_id: str) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT action, actor_kind, actor_ref, detail FROM kg_audit WHERE entity_id = :i"),
            {"i": node_id}).mappings()]


def test_the_agents_forget_retires_a_graph_lesson_with_its_reason(engine):
    node_id = _lesson(engine)
    said = _invoke(engine, {"memory_id": node_id,
                            "reason": "restarting hides the real cause; fix the lease instead"})
    assert said.get("ok") is True, said
    assert said["retired"] is True and said["audit_id"]
    node = _node(engine, node_id)
    assert node["closed"] is True
    assert node["attrs"]["retired_reason"].startswith("restarting hides")
    [audit] = _audit(engine, node_id)
    assert audit["action"] == "knowledge.retire" and audit["actor_kind"] == "agent"
    assert audit["actor_ref"] == "ceo"
    assert audit["detail"]["reason"].startswith("restarting hides")
    # Once retired, it is retired: a second forget says so rather than writing a second row.
    again = _invoke(engine, {"memory_id": node_id, "reason": "again"})
    assert again["ok"] is False and "already retired" in again["error"]


def test_a_graph_lesson_needs_a_reason(engine):
    node_id = _lesson(engine, key="noreason")
    said = _invoke(engine, {"memory_id": node_id})
    assert said["ok"] is False and "reason" in said["error"]
    assert _node(engine, node_id)["valid_to"] is None


def test_an_agent_cannot_retire_what_a_person_approved(engine):
    node_id = _lesson(engine, tier="approved", key="approved")
    said = _invoke(engine, {"memory_id": node_id, "reason": "I disagree"})
    assert said["ok"] is False
    assert _node(engine, node_id)["valid_to"] is None


def test_another_projects_lesson_is_out_of_reach(client, engine):
    node_id = _lesson(engine, project=OTHER, key="other")
    said = _invoke(engine, {"memory_id": node_id, "reason": "not yours"})
    assert said.get("ok") is not True
    over_api = client.post(f"/v1/memory/{node_id}/forget", json={
        "project_id": PROJECT, "actor": "ceo", "reason": "not yours"})
    assert over_api.status_code == 404, over_api.text
    assert _node(engine, node_id)["valid_to"] is None


def test_the_api_retires_a_graph_lesson_as_a_person(client, engine):
    node_id = _lesson(engine, tier="approved", key="api")
    said = client.post(f"/v1/memory/{node_id}/forget", json={
        "project_id": PROJECT, "actor": "ceo", "reason": "superseded by the new runbook"})
    assert said.status_code == 200, said.text
    assert said.json()["retired"] is True
    assert _node(engine, node_id)["closed"] is True
    [audit] = _audit(engine, node_id)
    assert audit["actor_kind"] == "person" and audit["detail"]["reason"].startswith("superseded")
    missing_reason = client.post(f"/v1/memory/{_lesson(engine, key='api2')}/forget", json={
        "project_id": PROJECT, "actor": "ceo"})
    assert missing_reason.status_code == 400


def test_a_node_retired_inside_a_long_transaction_is_not_mirrored_back(engine):
    """The sync's validity reads the clock the close wrote with. Inside one transaction that has
    been open a while (now() is its start), the retired node is already gone from what the AGE
    projection would write."""
    from boss_engine.services.knowledge import age
    from boss_engine.services.knowledge.service import KnowledgeService

    node_id = _lesson(engine, key="clock")
    with engine.begin() as c:
        c.execute(text("SELECT pg_sleep(0.2)"))           # the transaction has been open a while
        assert [r["id"] for r in age._node_rows(c, PROJECT, [node_id])] == [node_id]
        KnowledgeService().retire(c, project_id=PROJECT, node_id=node_id,
                                  reason="wrong", actor_kind="agent", actor_ref="helm")
        assert age._node_rows(c, PROJECT, [node_id]) == []


def test_recording_a_retired_lesson_again_does_not_serve_it_again(engine):
    from boss_engine.services.knowledge import learning_writeback
    from boss_engine.services.knowledge.service import KnowledgeService

    lesson = dict(project_id=PROJECT, learning_kind="investigation",
                  title="Why the ledger stalled", statement="The ledger stalls when the lock is held.",
                  detail={"tried": "re-ran it", "ruled_out": "the database", "found": "a held lock"},
                  about={}, evidence=[{"type": "log", "ref": "chat", "excerpt": "lock wait"}],
                  confidence=0.5)
    with engine.begin() as c:
        node_id = learning_writeback.record(c, **lesson)["node"]["id"]
    with engine.begin() as c:
        KnowledgeService().retire(c, project_id=PROJECT, node_id=node_id, reason="wrong cause",
                                  actor_kind="agent", actor_ref="helm")
    with engine.begin() as c:
        again = learning_writeback.record(c, **lesson)
    assert again["node"]["id"] == node_id
    assert _node(engine, node_id)["closed"] is True


def test_a_memory_id_still_retires_a_memory(engine):
    from boss_engine.services import agent_memory as mem

    with engine.begin() as c:
        made = mem.remember(c, project_id=PROJECT, actor="ceo", kind="project",
                            body="the queue worker restarts nightly")
    said = _invoke(engine, {"memory_id": made["id"]})
    assert said["ok"] is True

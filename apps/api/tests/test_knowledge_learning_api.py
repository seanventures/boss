"""Learning, live and measured, over HTTP (BOSSAPP-768): /v1/knowledge/view/live and /proof.

Breaks covered: the live feed not answering in the KgLiveResponse shape the console polls; a
cursor that resends nothing new as new, or loses what came after it; an unreadable cursor taken
as "everything"; the proof route not scoring a run or not comparing a pair.
"""
from __future__ import annotations

from apps.api.app.db import begin
from boss_engine.services.knowledge import learning_writeback as lw
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context

PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"
LIVE_KEYS = {"cursor", "learnings", "rules", "edges", "active_runs", "poll_after_ms"}


def _learn(engine, run_id: str, statement: str) -> str:
    set_llm_context(role_key="sre", run_id=run_id, stage="investigate")
    try:
        with begin(engine) as connection:
            return lw.record(connection, project_id=PROJECT_ID, learning_kind="root_cause",
                             title="Pool exhausted", statement=statement, detail={}, about={},
                             evidence=[{"type": "log", "ref": run_id, "excerpt": "QueuePool limit reached"}],
                             confidence=0.6)["node"]["id"]
    finally:
        clear_llm_context()


def test_the_live_feed_answers_in_the_polled_shape_and_moves_with_its_cursor(client, engine):
    params = {"project_id": PROJECT_ID, "actor": ACTOR, "run": "crun_api_live"}
    first = client.get("/v1/knowledge/view/live", params=params)
    assert first.status_code == 200, first.text
    body = first.json()
    assert set(body) == LIVE_KEYS and body["learnings"] == [] and body["poll_after_ms"] == 5000
    node_id = _learn(engine, "crun_api_live", "the pool of 5 is exhausted under the 8am load")
    after = client.get("/v1/knowledge/view/live", params={**params, "since": body["cursor"]}).json()
    assert [i["node"]["id"] for i in after["learnings"]] == [node_id]
    assert any(e["kind"] == "produced" and e["dst"] == node_id and e["run_id"] == "crun_api_live"
               for e in after["edges"])
    bad = client.get("/v1/knowledge/view/live", params={**params, "since": "yesterday"})
    assert bad.status_code == 422


def test_the_proof_route_scores_a_run_and_compares_a_pair(client, engine):
    _learn(engine, "crun_api_proof_a", "the pool limit causes the 502s")
    base = {"project_id": PROJECT_ID, "actor": ACTOR}
    one = client.get("/v1/knowledge/view/proof", params={**base, "run": "crun_api_proof_a"})
    assert one.status_code == 200, one.text
    run = one.json()["run"]
    assert run["run_id"] == "crun_api_proof_a" and run["learned"] == 1 and run["consulted"] == []
    pair = client.get("/v1/knowledge/view/proof", params={**base, "run": "crun_api_proof_a",
                                                         "baseline": "crun_api_proof_a"}).json()
    assert pair["comparison"]["attributed"] is False and pair["run"] is None
    missing = client.get("/v1/knowledge/view/proof", params={**base, "run": "crun_no_such_run"})
    assert missing.status_code == 404


def test_a_press_in_an_agents_thread_credits_that_agent_and_the_proof_scores_the_conversation(client, engine):
    """The press path (POST /v1/engine/agent/action) carries the thread's addressed agent: a
    learning recorded by pressing "Record learning" in the SRE's thread is the SRE's, produced by
    that conversation (northpeak-bank, 2026-10-25: it was credited to "BOSS (general)")."""
    from apps.api.app.config import get_settings
    from boss_engine.services.agent_service import AgentService

    service = AgentService(get_settings())
    with begin(engine) as connection:
        thread_id = service.start_or_load_thread(connection, project_id=PROJECT_ID, actor=ACTOR,
                                                 agent_role="sre", new_thread=True)["id"]
        service.append_message(connection, thread_id=thread_id, role="user",
                               content="# INC-2088 · early repayments post more than paid")
    pressed = client.post("/v1/engine/agent/action", json={
        "project_id": PROJECT_ID, "actor": ACTOR, "thread_id": thread_id, "tool": "record_learning",
        "label": "Record learning", "args": {
            "learning_kind": "root_cause", "title": "Advance handler skips the decrement",
            "statement": "The advance-payment branch never decrements the remaining amount.",
            "detail": {}, "about": {}, "confidence": 0.7,
            "evidence": [{"type": "file", "ref": "Adv.java:159", "excerpt": "no minus after principal"}]}})
    assert pressed.status_code == 200, pressed.text
    body = pressed.json()
    assert body["ok"] is True, body
    node_id = body["result"]["learning"]["id"]
    with begin(engine) as connection:
        prov = lw.item_payload(connection, PROJECT_ID, node_id)["provenance"]
    assert prov["agent_role"] == "sre" and prov["agent_name"] not in (None, "BOSS (general)")
    proof = client.get("/v1/knowledge/view/proof", params={"project_id": PROJECT_ID, "actor": ACTOR,
                                                           "run": thread_id})
    assert proof.status_code == 200, proof.text
    run = proof.json()["run"]
    assert run["kind"] == "conversation" and run["learned"] == 1 and run["agent_role"] == "sre"
    assert run["time_to_root_cause_seconds"] is not None

"""AP-P0 (BOSSAPP-1051): the five dead or broken memory write paths of AGENT_PLATFORM.md §2.9,
and `record_outcome` made idempotent. Every case runs on a real Postgres.

  * person_wait: a resolution writes a learning node (every write used to fail validation, and
    the refusal was swallowed);
  * record_outcome: replaying a run's attribution leaves the counters unchanged;
  * set_archived: the archive sweep persists the archival the learning layer computes;
  * agent_converse: the persona's memory section is no longer always empty (key mismatch);
  * update_ledger: works with no injected `_thread_id` (MCP, agent steps);
  * Metatron: an answered session turn writes a memory.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import text

from ._ambient_support import eng, project  # noqa: F401


def _run(c, pid: str) -> str:
    run_id = f"crun_{uuid4().hex[:12]}"
    c.execute(text("INSERT INTO code_task_runs (id, project_id, title) VALUES (:i, :p, 'p0')"),
              {"i": run_id, "p": pid})
    return run_id


def _fact(c, pid: str) -> str:
    from boss_engine.services.knowledge.service import KnowledgeService

    tag = uuid4().hex[:6]
    proposed = KnowledgeService().propose_fact(
        c, project_id=pid, statement=f"run the frontend checks first {tag}",
        subject=f"frontend_checks_{tag}", predicate="catch", obj="review_rounds", kind="lesson")
    return proposed["fact_ids"][0]


def test_a_person_wait_resolution_writes_a_learning_node(eng, project):  # noqa: F811
    from boss_engine.services import person_wait

    with eng.begin() as c:
        run_id = _run(c, project)
        # project_id not passed: the run names its project.
        person_wait.record_resolution(c, run_id=run_id, project_id=None,
                                      blocker_type="authority_no_grant",
                                      resolution="grant_created",
                                      opened_at="2026-11-14T00:00:00+00:00")
    with eng.connect() as c:
        node = c.execute(text(
            "SELECT kind, node_key, attrs FROM kg_nodes WHERE project_id = :p "
            "AND node_key LIKE 'learning:decision:%'"), {"p": project}).mappings().all()
        history = c.execute(text(
            "SELECT finalize_ctx->'person_wait_history' FROM code_task_runs WHERE id = :i"),
            {"i": run_id}).scalar()
    assert len(node) == 1, node
    assert node[0]["attrs"]["detail"]["chosen"] == "grant_created"
    assert history and history[0]["resolution"] == "grant_created"


def test_replaying_record_outcome_leaves_the_counters_unchanged(eng, project):  # noqa: F811
    from boss_engine.services.knowledge.service import KnowledgeService

    svc = KnowledgeService()
    with eng.begin() as c:
        fact_id = _fact(c, project)
        svc.log_pack(c, project_id=project, run_id="run-a", step="review", fact_ids=[fact_id])
        svc.attribute_run(c, project_id=project, run_id="run-a", success=True)
        first = svc.store.fact_stat(c, project_id=project, fact_id=fact_id)
        svc.attribute_run(c, project_id=project, run_id="run-a", success=True)
        svc.attribute_run(c, project_id=project, run_id="run-a", success=False)
        again = svc.store.fact_stat(c, project_id=project, fact_id=fact_id)
        # A different run still counts.
        svc.log_pack(c, project_id=project, run_id="run-b", step="review", fact_ids=[fact_id])
        svc.attribute_run(c, project_id=project, run_id="run-b", success=False)
        other = svc.store.fact_stat(c, project_id=project, fact_id=fact_id)
    assert (first["success_count"], first["failure_count"]) == (1, 0)
    assert (again["success_count"], again["failure_count"]) == (1, 0)
    assert (other["success_count"], other["failure_count"]) == (1, 1)


def test_the_archive_sweep_persists_archival_both_ways(eng, project):  # noqa: F811
    from boss_engine.services.knowledge.service import KnowledgeService

    svc = KnowledgeService()
    with eng.begin() as c:
        stale, fresh = _fact(c, project), _fact(c, project)
        for fid, used in ((stale, datetime.now(timezone.utc) - timedelta(days=400)),
                          (fresh, datetime.now(timezone.utc))):
            c.execute(text("INSERT INTO kg_fact_stats (fact_id, project_id, failure_count, "
                           "last_used_at) VALUES (:f, :p, 3, :u)"),
                      {"f": fid, "p": project, "u": used})
        got = svc.archive_sweep(c, project_id=project)
        assert got["archived"] == 1, got
        assert svc.store.fact_stat(c, project_id=project, fact_id=stale)["archived"] is True
        assert svc.store.fact_stat(c, project_id=project, fact_id=fresh)["archived"] is False
        assert svc.archive_sweep(c, project_id=project)["changed"] == 0
        c.execute(text("UPDATE kg_fact_stats SET last_used_at = now() WHERE fact_id = :f"),
                  {"f": stale})
        svc.archive_sweep(c, project_id=project)
        assert svc.store.fact_stat(c, project_id=project, fact_id=stale)["archived"] is False


def test_the_conversation_memory_section_is_not_empty(eng, project):  # noqa: F811
    from boss_engine.services import agent_converse, agent_persona

    act = f"act_{uuid4().hex[:12]}"
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO agent_acts (act_id, project_id, role_key, kind, status, root_act_id, "
            "handed_by_kind, how, accountable, label) VALUES (:a, :p, 'qa_lead', 'step', "
            "'complete', :a, 'boss', 'named', 'owner', 'Review · checkout flow')"),
            {"a": act, "p": project})
        memory = agent_converse._memory(c, project, "qa_lead")
    section = agent_persona._l4(memory)
    assert "Review · checkout flow" in section and f"[act:{act}]" in section


def test_update_ledger_works_without_an_injected_thread(eng, project):  # noqa: F811
    from boss_engine.tools.builtin import _ledger_thread, build_default_registry

    assert _ledger_thread({"_thread_id": "thr_1"}, project_id=project, actor="a") == "thr_1"
    assert _ledger_thread({"session_id": "s9"}, project_id=project, actor="a") \
        == f"session:{project}:s9"
    ctx = SimpleNamespace(begin=eng.begin, engine=eng)
    fn = build_default_registry().get("update_ledger").handler
    got = fn(ctx, project_id=project, actor="mcp_client",
             args={"goal": "ship the P0 hygiene", "session_id": "cli-1"})
    assert got["ok"] is True, got
    got = fn(ctx, project_id=project, actor="mcp_client",
             args={"rejected": "rerun everything", "session_id": "cli-1"})
    assert got["ok"] is True and got["rejected"] == ["rerun everything"], got


def test_an_answered_metatron_turn_is_remembered(eng, project, monkeypatch):  # noqa: F811
    from apps.api.app.routers import metatron_captain as router
    from boss_engine.schemas import AgentChatRequest, AgentChatResponse

    monkeypatch.setattr(router, "get_engine", lambda: eng)
    monkeypatch.setattr(router, "begin", lambda e: e.begin())
    router._remember_turn(
        AgentChatRequest(project_id=project, message="What failed overnight?"),
        AgentChatResponse(thread_id="thr_m1", reply="Two builds; both re-queued."))
    router._remember_turn(       # nothing answered: nothing remembered
        AgentChatRequest(project_id=project, message="hello"),
        AgentChatResponse(thread_id="thr_m1", reply=""))
    with eng.connect() as c:
        rows = c.execute(text("SELECT thread_id, kind, source, content FROM metatron_memories "
                              "WHERE project_id = :p"), {"p": project}).mappings().all()
        c.rollback()
    with eng.begin() as c:
        c.execute(text("DELETE FROM metatron_memories WHERE project_id = :p"), {"p": project})
    assert len(rows) == 1
    assert rows[0]["thread_id"] == "thr_m1" and rows[0]["source"] == "chat"
    assert "What failed overnight?" in rows[0]["content"]

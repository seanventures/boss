"""A build is signed under the work that started it, and its person is a real one (§5.1 hook 4).

A build is started by a tool in the tool's own transaction, so the chat turn or step that called
it may not be visible when the build's first model call is signed. Before this was fixed the
stages then hung off a `Build · …` root accountable to "operator" — a person nobody is — and that
root was never closed. Now the starter records itself (`agent_acts.origin`), the tool writes it
into the build's finalize_ctx, the stages wait for it with no parent and the right person, and
the starting act adopts them as soon as it is visible. A build nobody recorded is BOSS's, said
as BOSS, and its root closes when the build ends.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

PROJECT = "boss-dogfood"


def _task(engine, finalize_ctx: dict) -> str:
    run_id = f"ctr_{uuid.uuid4().hex[:16]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, "
            "finalize_ctx) VALUES (:i, :p, 'running', 'coding', 'Dark mode', '{}'::jsonb, "
            "CAST(:f AS JSONB))"), {"i": run_id, "p": PROJECT, "f": json.dumps(finalize_ctx)})
    return run_id


def _spend(engine, run_id: str, stage: str) -> None:
    from boss_engine.services.llm_telemetry import (
        clear_llm_context, record_llm_call, set_llm_context)

    set_llm_context(db_engine=engine, project_id=PROJECT, run_id=run_id, stage=stage)
    try:
        record_llm_call(engine_name="stub", usage={
            "model": "stub-model", "prompt_tokens": 10, "completion_tokens": 4,
            "total_tokens": 14}, cost_usd_micros=1_000, stage=stage)
    finally:
        clear_llm_context()


def _acts(engine, run_id: str) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT * FROM agent_acts WHERE code_task_run_id = :c OR "
            "(subject_kind = 'code_task_run' AND subject_id = :c) ORDER BY started_at, act_id"),
            {"c": run_id}).mappings()]


def test_a_build_a_chat_turn_started_waits_for_the_turn_and_never_invents_a_person(engine):
    from boss_engine.services import agent_acts

    turn = agent_acts.new_act_id()  # the turn's act: still in its own, uncommitted transaction
    run_id = _task(engine, {"origin": {"act_id": turn, "actor": "ceo"}})

    _spend(engine, run_id, "plan")
    early = _acts(engine, run_id)
    assert [(a["kind"], a["parent_act_id"], a["accountable"]) for a in early] == [
        ("stage", None, "ceo")], "a stage hung off a guess before its starter was visible"

    with engine.begin() as c:  # the turn commits
        agent_acts.open_act(c, project_id=PROJECT, kind="chat", role_key="qa",
                            how="asked_by_person", handed_by_kind="person", handed_by_id="ceo",
                            accountable="ceo", label="Chat · build dark mode", act_id=turn)
    _spend(engine, run_id, "implement")

    stages = [a for a in _acts(engine, run_id) if a["kind"] == "stage"]
    assert [a["step_id"] for a in stages] == ["plan", "implement"]
    assert {(a["parent_act_id"], a["root_act_id"], a["accountable"]) for a in stages} == {
        (turn, turn, "ceo")}
    assert not [a for a in _acts(engine, run_id) if a["kind"] == "run"]
    with engine.begin() as c:
        assert not c.execute(text(
            "SELECT 1 FROM agent_acts WHERE accountable = 'operator' "
            "OR handed_by_id = 'operator'")).scalar()


def test_a_build_nobody_recorded_is_boss_s_and_its_root_closes_with_it(engine):
    from boss_engine.services import agent_acts

    run_id = _task(engine, {})
    _spend(engine, run_id, "implement")
    root = [a for a in _acts(engine, run_id) if a["kind"] == "run"]
    assert [(a["accountable"], a["handed_by_kind"], a["handed_by_id"]) for a in root] == [
        (agent_acts.BOSS, "boss", None)]

    with engine.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status = 'completed' WHERE id = :i"),
                  {"i": run_id})
    agent_acts.settle_code_task(engine, run_id)
    assert [a for a in _acts(engine, run_id) if a["ended_at"] is None] == [], \
        "a finished build left an act open for ever"
    with engine.begin() as c:
        footer = agent_acts.pr_trailers(c, run_id)
    assert "accountable: BOSS" in footer


def test_a_build_someone_started_names_them(engine):
    run_id = _task(engine, {"actor": "ceo"})
    _spend(engine, run_id, "implement")
    root = [a for a in _acts(engine, run_id) if a["kind"] == "run"]
    assert [(a["accountable"], a["handed_by_kind"], a["handed_by_id"]) for a in root] == [
        ("ceo", "person", "ceo")]


def test_the_build_tool_records_who_started_it(monkeypatch):
    """create_code_task writes the caller's origin into the build's finalize_ctx."""
    from boss_engine.services import agent_acts, code_task_service, repo_registry
    from boss_engine.services.workflow_capability_runner import _tool_registry

    seen: dict = {}
    monkeypatch.setattr(repo_registry, "resolve_repository_target", lambda *a, **k: {
        "owner": "o", "repo": "r", "full_name": "o/r", "source": "test", "binding_id": None})
    monkeypatch.setattr(code_task_service, "start_code_task",
                        lambda c, **kw: seen.update(kw) or "ctr_seen")

    class Ctx:
        settings = None
        jobs = None

        @staticmethod
        def begin():
            from contextlib import nullcontext

            return nullcontext(None)

    spec = {s.name: s for s in _tool_registry().specs()}["create_code_task"]
    with agent_acts.origin(act_id="act_turn", actor="ceo"):
        spec.handler(Ctx(), project_id=PROJECT, actor="ceo",
                     args={"title": "Dark mode", "instructions": "Add it."})
    assert seen["finalize_ctx"]["origin"] == {"act_id": "act_turn", "actor": "ceo"}

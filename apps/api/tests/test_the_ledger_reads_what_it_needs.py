"""The ledger's hot paths read what they need, not the project's history (Agent World §4.3).

`agent_acts` is append-only and kept for ever, and `llm_calls` is the biggest telemetry table.
So every hot read and write is held to a budget of ROWS IT TOUCHES, measured by Postgres itself
(EXPLAIN ANALYZE of the exact statements each call runs), with thousands of unrelated rows in the
way: an act's close (every step settle, every build stage change, every chat turn), the 4s live
poll, an agent's presence, the Passport's Lineage tab, a build's stage lookup and an Act card.

Before this was fixed, close_act scanned every llm_call, the live poll sorted every act the
project had ever ended, and lineage read every act in the project's window.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Callable

from sqlalchemy import event, text

PROJECT = "boss-dogfood"
LEDGER = {"agent_acts", "llm_calls", "agent_turns", "agent_messages"}
HISTORY = 3000


def _seed_history(engine) -> None:
    """A busy project: thousands of ended acts by other agents, each with a model call."""
    with engine.begin() as c:
        c.execute(text(
            """
            INSERT INTO agent_acts (act_id, project_id, role_key, kind, status, root_act_id,
                                    handed_by_kind, how, accountable, label, started_at,
                                    ended_at, code_task_run_id, meeting_run_id,
                                    subject_kind, subject_id)
            SELECT 'act_hist' || g, :p, 'busy_' || (g % 7), 'stage', 'complete',
                   'act_hist' || g, 'boss', 'next_stage', 'ceo', 'History · ' || g,
                   now() - make_interval(mins => g), now() - make_interval(mins => g) + interval
                   '30 seconds', 'ctr_hist_' || (g % 300), 'mrun_hist_' || (g % 300),
                   'code_task_run', 'ctr_hist_' || (g % 300)
            FROM generate_series(1, :n) g
            """
        ), {"p": PROJECT, "n": HISTORY})
        c.execute(text(
            """
            INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, model, role,
                                   prompt_tokens, completion_tokens, total_tokens,
                                   cost_usd_micros, ok, act_id)
            SELECT 'llmc_hist_' || g, :p, 'ctr_hist_' || (g % 300), 'implement', 'stub',
                   'stub-model', 'busy', 10, 5, 15, 100, true, 'act_hist' || g
            FROM generate_series(1, :n) g
            """
        ), {"p": PROJECT, "n": HISTORY})
        c.execute(text("ANALYZE agent_acts"))
        c.execute(text("ANALYZE llm_calls"))


@contextmanager
def _statements(connection):
    seen: list[tuple[str, object]] = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        seen.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        yield seen
    finally:
        event.remove(connection, "before_cursor_execute", capture)


def _walk(plan: dict):
    yield plan
    for child in plan.get("Plans") or []:
        yield from _walk(child)


def _rows_touched(connection, statements) -> dict[str, int]:
    """Rows each ledger table's scans touched, across these statements, as Postgres counts them.
    Scans are made to use an index whenever one can serve them (enable_seqscan off), so a scan
    of a whole table here means no index could."""
    touched: dict[str, int] = {}
    for statement, parameters in statements:
        head = statement.lstrip().split(None, 1)[0].upper()
        if head not in ("SELECT", "WITH", "UPDATE"):
            continue
        point = connection.begin_nested()
        try:
            connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
            plan = connection.exec_driver_sql(
                "EXPLAIN (ANALYZE, FORMAT JSON) " + statement, parameters).scalar()
        finally:
            point.rollback()
        plan = plan if isinstance(plan, list) else json.loads(plan)
        for node in _walk(plan[0]["Plan"]):
            table = node.get("Relation Name")
            if table in LEDGER:
                loops = int(node.get("Actual Loops") or 1)
                read = (int(node.get("Actual Rows") or 0)
                        + int(node.get("Rows Removed by Filter") or 0)
                        + int(node.get("Rows Removed by Index Recheck") or 0)) * loops
                touched[table] = touched.get(table, 0) + read
    return touched


def _measure(engine, call: Callable) -> dict[str, int]:
    """Run the call, undo what it wrote, then have Postgres measure each statement it ran from
    the state the call started in (a close re-run after the close would find nothing to do)."""
    with engine.connect() as connection:
        with connection.begin():
            point = connection.begin_nested()
            with _statements(connection) as seen:
                call(connection)
            point.rollback()
            return _rows_touched(connection, seen)


def _open(connection, **extra) -> str:
    from boss_engine.services import agent_acts

    fields = dict(project_id=PROJECT, kind="step", role_key="lineage_probe", how="named",
                  handed_by_kind="boss", accountable="ceo", label="Probe · now")
    fields.update(extra)
    return agent_acts.open_act(connection, **fields)


BUDGET = 200  # rows; the history in the way is HISTORY acts and HISTORY calls


def test_closing_an_act_reads_its_own_calls_not_the_table(engine):
    from boss_engine.services import agent_acts

    _seed_history(engine)
    with engine.begin() as c:
        act = _open(c)
    touched = _measure(engine, lambda c: agent_acts.close_act(c, act, status="complete"))
    assert touched.get("llm_calls", 0) < BUDGET, touched


def test_the_live_poll_does_not_grow_with_history(engine):
    from boss_engine.services import agent_acts

    _seed_history(engine)
    with engine.begin() as c:
        _open(c)
    got: dict = {}
    touched = _measure(engine, lambda c: got.update(agent_acts.live(c, PROJECT)))
    assert [i["role_key"] for i in got["items"]] == ["lineage_probe"]
    assert touched.get("agent_acts", 0) < BUDGET, touched


def test_presence_reads_one_probe_per_agent(engine):
    from boss_engine.services import agent_acts

    _seed_history(engine)
    got: dict = {}
    touched = _measure(engine, lambda c: got.update(agent_acts.presence_map(c, PROJECT)))
    assert {k for k, v in got.items() if v["state"] == "idle"} == {f"busy_{i}" for i in range(7)}
    assert got["busy_1"]["act_id"] == "act_hist1"  # the most recent one each agent ended
    assert touched.get("agent_acts", 0) < BUDGET, touched
    one = _measure(engine, lambda c: got.update(
        agent_acts.presence_map(c, PROJECT, role_key="busy_3")))
    assert one.get("agent_acts", 0) < 50, one


def test_lineage_costs_what_this_agent_did(engine):
    from boss_engine.services import agent_acts

    _seed_history(engine)
    with engine.begin() as c:
        mine = _open(c)
        _open(c, role_key="qa", parent_act_id=mine, label="Review · now")
    graph: dict = {}
    touched = _measure(engine, lambda c: graph.update(
        agent_acts.lineage(c, PROJECT, "lineage_probe", days=365)))
    assert ("agent:lineage_probe", "agent:qa") in {(e["from"], e["to"]) for e in graph["edges"]}
    assert touched.get("agent_acts", 0) < BUDGET, touched


def test_a_build_stage_and_an_act_card_are_index_reads(engine):
    from boss_engine.services import agent_acts

    _seed_history(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, "
                       "finalize_ctx) VALUES ('ctr_probe', :p, 'running', 'coding', 'Probe', "
                       "'{}'::jsonb, '{\"actor\": \"ceo\"}'::jsonb)"), {"p": PROJECT})
        first = agent_acts.stage_act(c, project_id=PROJECT, code_task_run_id="ctr_probe",
                                     stage="implement")
    touched = _measure(engine, lambda c: agent_acts.stage_act(
        c, project_id=PROJECT, code_task_run_id="ctr_probe", stage="review_code"))
    assert touched.get("agent_acts", 0) < BUDGET, touched
    card = _measure(engine, lambda c: agent_acts.get_act(c, PROJECT, first["act_id"]))
    assert sum(card.values()) < BUDGET, card
    with engine.begin() as c:
        c.execute(text("INSERT INTO meeting_runs (id, project_id, status, phase, directive, "
                       "role_keys, title, finalize_ctx) VALUES ('mrun_hist_1', :p, 'running', "
                       "'research', 'Probe?', '[\"qa\"]'::jsonb, 'Probe', "
                       "'{\"origin\": {\"actor\": \"ceo\"}}'::jsonb)"), {"p": PROJECT})
    seats = _measure(engine, lambda c: agent_acts.sync_meeting(c, "mrun_hist_1"))
    assert seats.get("agent_acts", 0) < BUDGET, seats

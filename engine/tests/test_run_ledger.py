"""THE RUN LEDGER (BOSSAPP-961, K0; docs/architecture/HARNESS_KERNEL_DESIGN.md §3).

"What did I already try, and what did it cost?" had no answer an agent could ask for: spend sat
in llm_calls, the transcript in agent_turns, tool decisions in tool_call_audit, none of them
carrying a run sequence, a retry lineage or the rules that produced the row. Pinned here:

  * the schema: run_events partitioned by month, the new columns, policy_versions, the
    run_ledger view over all three sources;
  * the database refuses to rewrite the ledger: run_events and policy_versions take no UPDATE or
    DELETE (a purge says so), llm_calls only fills its ledger columns once;
  * dual-write completeness: while BOSS_LEDGER_EVENTS is on, EVERY llm_calls row has its run and
    seq (a call outside any run is its own run), tool calls and transcript turns join the run;
    while it is off nothing changes;
  * secrets never reach a ledger row, and a big body is clipped, not stored;
  * a retry's rows carry the lineage of the first run, so the lineage reads as one history;
  * the reader dedupes attempts and the read_run_ledger tool can only see its own run.
"""
from __future__ import annotations

import json
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import text

from ._ambient_support import eng, project  # noqa: F401

SECRET = "sk-ant-api03-" + "A" * 40


@pytest.fixture()
def ledger_on(monkeypatch):
    monkeypatch.setenv("BOSS_LEDGER_EVENTS", "1")
    from boss_engine.kernel import ledger

    ledger._LINEAGE_CACHE.clear()


@pytest.fixture()
def llm_ctx():
    from boss_engine.services import llm_telemetry

    yield llm_telemetry.set_llm_context
    llm_telemetry.clear_llm_context()


def _run_row(c, pid: str, *, root: str = "", attempt: int = 1) -> str:
    run_id = f"crun_{uuid4().hex[:12]}"
    fctx = {"root_run_id": root, "attempt": attempt} if root else {}
    c.execute(text("INSERT INTO code_task_runs (id, project_id, title, finalize_ctx) "
                   "VALUES (:i, :p, 'ledger test', CAST(:f AS jsonb))"),
              {"i": run_id, "p": pid, "f": json.dumps(fctx)})
    return run_id


# ── Schema ────────────────────────────────────────────────────────────────────────────────────

def test_the_ledger_tables_view_and_columns_exist(eng):  # noqa: F811
    with eng.connect() as c:
        kind = c.execute(text("SELECT relkind FROM pg_class WHERE relname = 'run_events'")).scalar()
        parts = set(c.execute(text(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = 'run_events'::regclass")).scalars())
        cols = {(t, col) for t, col in c.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_name IN ('llm_calls', 'tool_call_audit')"))}
        month = c.execute(text("SELECT 'run_events_' || to_char(now(), 'YYYY_MM')")).scalar()
        view = c.execute(text("SELECT to_regclass('run_ledger'), to_regclass('policy_versions')")
                         ).one()
    assert kind == "p", "run_events is partitioned"
    assert {"run_events_default", month} <= parts
    for column in ("run_id", "seq", "lineage_id", "policy", "scaffold_level", "workspace_ref"):
        assert ("llm_calls", column) in cols
    for column in ("act_id", "seq", "lineage_id", "idempotency_key"):
        assert ("tool_call_audit", column) in cols
    assert all(view)


def test_an_event_lands_in_this_months_partition(eng, project):  # noqa: F811
    from boss_engine.kernel import ledger

    with eng.begin() as c:
        row = ledger.append(c, project_id=project, run_id="krun_part", kind="escalation",
                            data={"to": "frontier"})
        where = c.execute(text("SELECT tableoid::regclass::text FROM run_events WHERE id = :i"),
                          {"i": row["id"]}).scalar()
        month = c.execute(text("SELECT 'run_events_' || to_char(now(), 'YYYY_MM')")).scalar()
    assert where == month


# ── Append-only, enforced by the database ─────────────────────────────────────────────────────

def test_run_events_refuse_update_and_delete(eng, project):  # noqa: F811
    from boss_engine.kernel import ledger

    with eng.begin() as c:
        row = ledger.append(c, project_id=project, run_id="krun_ao", kind="finding",
                            data={"source": "test", "severity": "major", "message": "red"})
    for statement in ("UPDATE run_events SET kind = 'outcome' WHERE id = :i",
                      "DELETE FROM run_events WHERE id = :i"):
        with eng.connect() as c:
            with pytest.raises(Exception, match="append-only"):
                with c.begin():
                    c.execute(text(statement), {"i": row["id"]})
    with eng.begin() as c:   # retention says so, and only then
        c.execute(text("SET LOCAL boss.ledger_purge = 'on'"))
        gone = c.execute(text("DELETE FROM run_events WHERE id = :i"), {"i": row["id"]}).rowcount
    assert gone == 1


def test_policy_versions_are_immutable(eng):  # noqa: F811
    from boss_engine.kernel import ledger

    pid = f"stop.test@{uuid4().hex[:6]}"
    with eng.begin() as c:
        assert ledger.register_policy(c, pid, kind="stop", body={"max_steps": 6},
                                      source="kernel/presets.py")
        assert not ledger.register_policy(c, pid, kind="stop", body={"max_steps": 9},
                                          source="elsewhere")
    with eng.connect() as c:
        with pytest.raises(Exception, match="append-only"):
            with c.begin():
                c.execute(text("UPDATE policy_versions SET body = '{}' WHERE id = :i"), {"i": pid})


def test_llm_calls_fill_their_ledger_columns_once_and_change_nothing_else(eng, project):  # noqa: F811
    row = f"llmc_{uuid4().hex[:12]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO llm_calls (id, project_id, engine, model) "
                       "VALUES (:i, :p, 'native', 'm')"), {"i": row, "p": project})
        c.execute(text("UPDATE llm_calls SET act_id = 'act_1', role = 'qa' WHERE id = :i"),
                  {"i": row})   # a NULL filled: agent_acts' back-fill, the BOSS-280 backfill
        c.execute(text("UPDATE llm_calls SET role = role WHERE id = :i"), {"i": row})  # no-op
    for statement in ("UPDATE llm_calls SET model = 'other' WHERE id = :i",
                      "UPDATE llm_calls SET act_id = 'act_2' WHERE id = :i",
                      "UPDATE llm_calls SET cost_usd_micros = 1 WHERE id = :i"):
        with eng.connect() as c:
            with pytest.raises(Exception, match="append-only"):
                with c.begin():
                    c.execute(text(statement), {"i": row})


# ── Dual-write ────────────────────────────────────────────────────────────────────────────────

def test_with_the_flag_off_nothing_is_added(eng, project, monkeypatch, llm_ctx):  # noqa: F811
    monkeypatch.delenv("BOSS_LEDGER_EVENTS", raising=False)
    from boss_engine.services.llm_telemetry import record_llm_call

    with eng.begin() as c:
        run_id = _run_row(c, project)
    llm_ctx(db_engine=eng, project_id=project, run_id=run_id, stage="implement")
    record_llm_call(engine_name="native", usage={"model": "m", "prompt_tokens": 3},
                    cost_usd_micros=5)
    with eng.connect() as c:
        rows = c.execute(text("SELECT run_id, seq FROM llm_calls WHERE project_id = :p"),
                         {"p": project}).all()
    assert rows and all(r.run_id is None and r.seq is None for r in rows)


def test_every_llm_call_written_with_the_flag_on_has_its_run_and_seq(  # noqa: F811
        eng, project, ledger_on, llm_ctx):
    from boss_engine.services.llm_telemetry import clear_llm_context, record_llm_call

    with eng.begin() as c:
        run_id = _run_row(c, project)
    llm_ctx(db_engine=eng, project_id=project, run_id=run_id, stage="implement")
    for n in range(3):
        record_llm_call(engine_name="native", usage={"model": "m", "prompt_tokens": 10 + n},
                        cost_usd_micros=100)
    record_llm_call(engine_name="native", usage={"model": "m"}, ok=False, error="boom")
    clear_llm_context()     # a call outside any run: it becomes its own one-call run
    llm_ctx(db_engine=eng, project_id=project)
    record_llm_call(engine_name="native", usage={"model": "m"}, cost_usd_micros=1)
    with eng.connect() as c:
        rows = c.execute(text("SELECT id, run_id, seq, lineage_id FROM llm_calls "
                              "WHERE project_id = :p ORDER BY created_at, seq"),
                         {"p": project}).all()
        in_view = c.execute(text("SELECT count(*) FROM run_ledger WHERE project_id = :p "
                                 "AND src = 'model'"), {"p": project}).scalar()
    assert len(rows) == 5
    assert all(r.run_id and r.seq for r in rows), rows
    run_rows = [r for r in rows if r.run_id == run_id]
    assert len(run_rows) == 4 and [r.seq for r in run_rows] == sorted(r.seq for r in run_rows)
    assert len({r.seq for r in rows}) == 5
    assert all(r.lineage_id == run_id for r in run_rows)
    lone = [r for r in rows if r.run_id != run_id]
    assert lone[0].run_id in (lone[0].id,) or lone[0].run_id.startswith("act_")
    assert in_view == 5


def test_a_tool_call_joins_its_run_and_a_write_runs_once(eng, project, ledger_on):  # noqa: F811
    from boss_engine.services.tool_call_audit import record_tool_call

    common = dict(project_id=project, agent="kernel", principal=None, server_id=None,
                  decision="allow", matched_policy_ids=[], decision_id=None,
                  outcome="succeeded", latency_ms=3)
    with eng.begin() as c:
        run_id = _run_row(c, project)
        first = record_tool_call(c, run_id=run_id, tool="write_note",
                                 arguments={"body": "x", "token": SECRET},
                                 idempotency_key=f"{run_id}:1:call_1", **common)
        row = c.execute(text("SELECT seq, lineage_id, args_redacted FROM tool_call_audit "
                             "WHERE id = :i"), {"i": first}).one()
    assert row.seq and row.lineage_id == run_id
    assert SECRET not in json.dumps(row.args_redacted)
    with eng.connect() as c:
        with pytest.raises(Exception, match="uq_tool_call_idem|duplicate"):
            with c.begin():
                record_tool_call(c, run_id=run_id, tool="write_note", arguments={"body": "x"},
                                 idempotency_key=f"{run_id}:1:call_1", **common)


def test_each_transcript_turn_is_a_delegate_turn_event(eng, project, ledger_on):  # noqa: F811
    from boss_engine.services.agent_turns import record_turns

    with eng.begin() as c:
        run_id = _run_row(c, project)
        written = record_turns(
            c, [{"direction": "completion", "content": "reading the tests", "turn_index": 0},
                {"direction": "tool_call", "tool_name": "Bash",
                 "content": {"command": f"export KEY={SECRET}"}, "turn_index": 1}],
            project_id=project, code_task_run_id=run_id, stage="implement", engine="claude_code",
            prompt="fix it")
        events = c.execute(text("SELECT kind, content_ref, data FROM run_events WHERE run_id = :r "
                                "ORDER BY seq"), {"r": run_id}).all()
        turn_ids = set(c.execute(text("SELECT id FROM agent_turns WHERE code_task_run_id = :r"),
                                 {"r": run_id}).scalars())
    assert written == 3 and len(events) == 3
    assert {e.kind for e in events} == {"delegate.turn"}
    assert {e.content_ref for e in events} == turn_ids
    assert SECRET not in json.dumps([e.data for e in events])


# ── Redaction and clipping ────────────────────────────────────────────────────────────────────

def test_no_secret_reaches_an_event_and_a_big_body_is_clipped(eng, project):  # noqa: F811
    from boss_engine.kernel import ledger

    with eng.begin() as c:
        small = ledger.append(c, project_id=project, run_id="krun_red", kind="escalation",
                              data={"why": f"the key {SECRET} failed",
                                    "env": "DATABASE_URL=postgres://u:hunter22@db/x",
                                    "nested": [{"password": "hunter2hunter2"}]})
        big = ledger.append(c, project_id=project, run_id="krun_red", kind="context.compacted",
                            data={"chunks": ["x" * 1500 for _ in range(20)]})
        stored = dict(c.execute(text("SELECT id, data FROM run_events WHERE run_id = 'krun_red'"
                                     )).all())
    body = json.dumps(stored[small["id"]])
    assert SECRET not in body and "hunter22" not in body and "hunter2hunter2" not in body
    assert len(json.dumps(stored[big["id"]]).encode()) <= 8_192 + 64
    assert stored[big["id"]].get("_clipped") is True


def test_the_kinds_readers_depend_on_are_held_to_their_shape(eng, project):  # noqa: F811
    from boss_engine.kernel import ledger

    with eng.begin() as c:
        for kind, data in (("finding", {"source": "test", "message": "x"}),
                           ("outcome", {"scope": "run", "result": "maybe"}),
                           ("decision", {"question": "q", "chosen": "a", "mood": "x"}),
                           ("not.a.kind", {})):
            with pytest.raises(ledger.LedgerDataError):
                ledger.append(c, project_id=project, run_id="krun_shape", kind=kind, data=data)


# ── Lineage across a retry ────────────────────────────────────────────────────────────────────

def test_a_retry_reads_as_one_lineage(eng, project, ledger_on, llm_ctx):  # noqa: F811
    from boss_engine.kernel import ledger_read
    from boss_engine.services.llm_telemetry import record_llm_call
    from boss_engine.services.tool_call_audit import record_tool_call

    with eng.begin() as c:
        first = _run_row(c, project)
        retry = _run_row(c, project, root=first, attempt=2)
    for run_id, cost in ((first, 1_000_000), (retry, 500_000)):
        llm_ctx(db_engine=eng, project_id=project, run_id=run_id, stage="implement")
        record_llm_call(engine_name="native", usage={"model": "m", "prompt_tokens": 100},
                        cost_usd_micros=cost)
        with eng.begin() as c:
            for _ in range(2):
                record_tool_call(c, project_id=project, run_id=run_id, agent="kernel",
                                 principal=None, server_id=None, tool="run_tests",
                                 arguments={"suite": "api"}, decision="allow",
                                 matched_policy_ids=[], decision_id=None, outcome="failed",
                                 latency_ms=1)
    with eng.connect() as c:
        lineages = set(c.execute(text("SELECT DISTINCT lineage_id FROM run_ledger "
                                      "WHERE run_id IN (:a, :b)"), {"a": first, "b": retry}
                                 ).scalars())
        scope = ledger_read.Scope("lineage", first, project)
        spent = ledger_read.spend(c, scope)
        tried = ledger_read.tried(c, scope)
        only_retry = ledger_read.spend(c, ledger_read.Scope("run", retry, project))
        answer = ledger_read.answer(c, scope, "tried")
    assert lineages == {first}
    assert spent.cost_usd_micros == 1_500_000 and only_retry.cost_usd_micros == 500_000
    assert [(a.what, a.times, a.result) for a in tried] == [("run_tests", 4, "failed")]
    assert "run_tests ×4: failed" in answer and "$1.50" in answer


# ── The read_run_ledger tool ──────────────────────────────────────────────────────────────────

def test_read_run_ledger_sees_only_its_own_run(eng, project, ledger_on):  # noqa: F811
    from boss_engine.kernel.ledger_tool import SCHEMA, DbLedgerReader, LedgerTool
    from boss_engine.kernel.state import RunState
    from boss_engine.kernel.types import ToolCall
    from boss_engine.services.tool_call_audit import record_tool_call

    with eng.begin() as c:
        mine, theirs = _run_row(c, project), _run_row(c, project)
        for run_id, tool in ((mine, "read_logs"), (theirs, "drop_database")):
            record_tool_call(c, project_id=project, run_id=run_id, agent="kernel", principal=None,
                             server_id=None, tool=tool, arguments={}, decision="allow",
                             matched_policy_ids=[], decision_id=None, outcome="ok", latency_ms=1)
    state = RunState(run_id=mine, project_id=project, lineage_id=mine, system=[], messages=[])
    tool = LedgerTool(DbLedgerReader(eng))
    asked = tool.invoke(ToolCall("c1", "read_run_ledger",
                                 {"scope": "run", "question": "tried", "stage": None,
                                  "after_seq": None, "run_id": theirs}), state=state)
    assert asked.status == "ok"
    assert "read_logs" in asked.content["answer"]
    assert "drop_database" not in asked.content["answer"]
    assert "run_id" not in SCHEMA.parameters["properties"], "the model never names a run"
    assert SCHEMA.kind == "read"


# ── The workspace snapshot ────────────────────────────────────────────────────────────────────

def test_a_tree_sha_names_the_files_and_leaves_the_index_alone(tmp_path):
    from boss_engine.kernel.workspace import tree_sha

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True,
                              text=True).stdout

    git("init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    git("add", "a.py")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one")
    before = tree_sha(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\n")
    (tmp_path / ".boss").mkdir()
    (tmp_path / ".boss" / "progress.md").write_text("step 1")
    status_before = git("status", "--porcelain")
    after = tree_sha(tmp_path)
    assert before and after and before != after
    assert git("status", "--porcelain") == status_before, "the real index is untouched"
    (tmp_path / ".boss" / "progress.md").write_text("step 2")
    assert tree_sha(tmp_path) == after, ".boss/ is never part of the tree"
    assert tree_sha(tmp_path / "nowhere") is None

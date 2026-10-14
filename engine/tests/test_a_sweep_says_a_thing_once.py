"""A sweep says a thing once, and again only when it changed.

2026-10-13, one laptop: 3,330 shepherd.proposed (≈10 per sweep over 332 sweeps; 2,402 the same
"stop" with no remedy — crun_a3a733ed alone had 704 reasoning steps for a run untouched since the
day before), 2,191 captain.coordinated (2,130 of them "20 plans, 0 assignments"), and 939 each of
metatron.watch.ticked / overwatch.completed / drive.completed / command.staff / command.loop.

  (1) the shepherd proposes once per run state: a reasoning step written after the run's last
      change already said what a new one would; superseded and escalated runs are skipped; a
      proposal that carried a remedy stays an autopilot candidate without being re-proposed;
  (2) `core.emit_if_changed` writes an event only when its meaningful fields moved;
  (3) the captain says "coordinated" only when it staffed someone, and metatron's commands and
      ticks go through (2).
"""
from __future__ import annotations

import inspect
import json
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import core, shepherd_service

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")

TABLES = """
CREATE TABLE code_task_runs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL, title TEXT NOT NULL DEFAULT 'x',
  spec JSONB NOT NULL DEFAULT '{}'::jsonb, finalize_ctx JSONB NOT NULL DEFAULT '{}'::jsonb,
  pull_request_url TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE shepherd_escalations (id TEXT PRIMARY KEY, project_id TEXT, task_ref TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE shepherd_reasoning (id TEXT PRIMARY KEY, project_id TEXT, task_ref TEXT NOT NULL,
  step_no INTEGER NOT NULL, failure_class INTEGER, remedy_chosen TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE events (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, event_type TEXT NOT NULL,
  actor TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
  data JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())
"""


@pytest.fixture()
def db(monkeypatch):
    """A private schema holding only the tables these rules read: nothing shared is touched."""
    if not URL:
        pytest.skip("BOSS_TEST_DATABASE_URL is not set")
    assert "test" in URL.rsplit("/", 1)[-1]
    schema = f"t_once_{uuid.uuid4().hex[:8]}"
    url = URL.replace("postgresql://", "postgresql+psycopg://")
    base = create_engine(url)
    with base.begin() as c:
        c.execute(text(f"CREATE SCHEMA {schema}"))
    eng = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    with eng.begin() as c:
        for stmt in TABLES.split(";"):
            c.execute(text(stmt))

    def fake_emit(connection, project_id, event_type, actor, entity_type, entity_id, data):
        eid = f"evt_{uuid.uuid4().hex[:10]}"
        connection.execute(text(
            "INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, data) "
            "VALUES (:i, :p, :t, :a, :et, :ei, CAST(:d AS jsonb))"),
            {"i": eid, "p": project_id, "t": event_type, "a": actor, "et": entity_type,
             "ei": entity_id, "d": json.dumps(data)})
        return eid

    monkeypatch.setattr(core, "emit_event", fake_emit)
    try:
        yield eng
    finally:
        eng.dispose()
        with base.begin() as c:
            c.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        base.dispose()


def _events(eng, event_type):
    with eng.begin() as c:
        return c.execute(text("SELECT count(*) FROM events WHERE event_type = :t"),
                         {"t": event_type}).scalar()


# ------------------------------------------------------------------------------------------ #
# (1) the shepherd proposes once per run state
# ------------------------------------------------------------------------------------------ #

def test_the_shepherd_proposes_once_per_run_state(db, monkeypatch):
    from boss_engine.services import run_liveness

    monkeypatch.setattr(run_liveness, "superseded_by",
                        lambda c, rid: "crun_newer" if rid == "crun_superseded" else None)
    proposed: list[str] = []

    def fake_propose(engine, settings, run_id, **_):
        proposed.append(run_id)
        with engine.begin() as c:
            step = c.execute(text("SELECT COALESCE(max(step_no),0)+1 FROM shepherd_reasoning "
                                  "WHERE task_ref = :t"), {"t": run_id}).scalar()
            # "stop" with no remedy, the 2,402 case — except one run that has a remedy.
            c.execute(text("INSERT INTO shepherd_reasoning (id, task_ref, step_no, failure_class, "
                           "remedy_chosen) VALUES (:i, :t, :n, 10, :r)"),
                      {"i": uuid.uuid4().hex, "t": run_id, "n": step,
                       "r": "retry" if run_id == "crun_remedy" else None})
        return {"run_id": run_id, "class": 10}

    monkeypatch.setattr(shepherd_service, "propose", fake_propose)
    with db.begin() as c:
        for rid, status in (("crun_dead", "blocked"), ("crun_remedy", "failed"),
                            ("crun_superseded", "failed"), ("crun_escalated", "failed"),
                            ("crun_shipped", "failed")):
            c.execute(text("INSERT INTO code_task_runs (id, project_id, status, updated_at) "
                           "VALUES (:i, 'p1', :s, now() - interval '1 day')"),
                      {"i": rid, "s": status})
        c.execute(text("UPDATE code_task_runs SET pull_request_url = 'https://x/pr/1' "
                       "WHERE id = 'crun_shipped'"))
        c.execute(text("INSERT INTO shepherd_escalations (id, task_ref) VALUES ('e1', 'crun_escalated')"))

    settings = SimpleNamespace()
    shepherd_service.sweep(db, settings, "p1")
    assert sorted(proposed) == ["crun_dead", "crun_remedy"]  # not superseded/escalated/shipped
    for _ in range(5):  # five more sweeps, nothing changed: nothing re-proposed
        shepherd_service.sweep(db, settings, "p1")
    assert sorted(proposed) == ["crun_dead", "crun_remedy"]

    # The remedy-carrying proposal is still an autopilot candidate; the bare "stop" is not.
    with db.begin() as c:
        still = [r[0] for r in c.execute(text(shepherd_service.STILL_ACTIONABLE_SQL),
                                         {"p": "p1", "l": 20, "naf": shepherd_service.NOT_A_FAILURE})]
    assert still == ["crun_remedy"]

    # The run changes: it is proposed again, once.
    with db.begin() as c:
        c.execute(text("UPDATE code_task_runs SET updated_at = now() "
                       "WHERE id = 'crun_dead'"))
    shepherd_service.sweep(db, settings, "p1")
    shepherd_service.sweep(db, settings, "p1")
    assert sorted(proposed) == ["crun_dead", "crun_dead", "crun_remedy"]


# ------------------------------------------------------------------------------------------ #
# (2) emit_if_changed
# ------------------------------------------------------------------------------------------ #

def test_an_unchanged_event_is_not_written_again(db):
    for receipt in ("r1", "r2", "r3"):
        with db.begin() as c:
            core.emit_if_changed(c, "p1", "metatron.drive.completed", "metatron", "tick", "t",
                                 {"applied": 2, "rejected": 0, "receipt_id": receipt},
                                 compare=("applied", "rejected"))
    assert _events(db, "metatron.drive.completed") == 1
    with db.begin() as c:
        assert core.emit_if_changed(c, "p1", "metatron.drive.completed", "metatron", "tick", "t",
                                    {"applied": 2, "rejected": 1, "receipt_id": "r4"},
                                    compare=("applied", "rejected"))
        # Another project's state is its own.
        assert core.emit_if_changed(c, "p2", "metatron.drive.completed", "metatron", "tick", "t",
                                    {"applied": 2, "rejected": 1}, compare=("applied", "rejected"))
    assert _events(db, "metatron.drive.completed") == 3


def test_a_store_that_cannot_answer_still_emits(monkeypatch):
    sent = []
    monkeypatch.setattr(core, "emit_event", lambda *a, **k: sent.append(a) or "evt")

    class NoSql:
        def execute(self, *a, **k):
            raise RuntimeError("raw SQL is unavailable")

    assert core.emit_if_changed(NoSql(), "p1", "x.y", "a", "e", "i", {"n": 1}, compare=("n",)) == "evt"
    assert len(sent) == 1


# ------------------------------------------------------------------------------------------ #
# (3) captain and metatron emit on change
# ------------------------------------------------------------------------------------------ #

def test_the_captain_says_coordinated_only_when_it_staffed_someone(monkeypatch):
    from boss_engine.services import captain_coordination, factory_flow

    sent: list[str] = []
    monkeypatch.setattr(core, "emit_event", lambda c, p, t, *a, **k: sent.append(t))
    monkeypatch.setattr(factory_flow.FactoryFlowService, "board",
                        lambda self, c, p: {"items": [{"id": "crun_1"}, {"id": "crun_2"}]})
    svc = captain_coordination.CaptainCoordinationService(SimpleNamespace())
    monkeypatch.setattr(svc, "ensure_schema", lambda c: None)
    monkeypatch.setattr(captain_coordination, "plan_staffing", lambda item: {"assignments": []})
    monkeypatch.setattr(svc, "_persist_assignments", lambda *a, **k: 0)
    for _ in range(3):
        svc.coordinate(object(), "p1")
    assert sent == []
    monkeypatch.setattr(svc, "_persist_assignments", lambda *a, **k: 1)
    svc.coordinate(object(), "p1")
    assert sent == ["captain.coordinated"]


def test_metatron_ticks_and_commands_go_through_the_change_check():
    from boss_engine.services import metatron_captain, metatron_command

    captain = inspect.getsource(metatron_captain)
    for event in ("metatron.watch.ticked", "metatron.overwatch.completed", "metatron.drive.completed"):
        at = captain.index(f'"{event}"')
        assert "emit_if_changed(" in captain[max(0, at - 160):at], event
    command = inspect.getsource(metatron_command.MetatronCommand)
    assert "changed=bool(changed)" in command and 'changed=bool(result.get("assignments_written"))' in command
    assert "emit_if_changed(" in inspect.getsource(metatron_command.MetatronCommand._receipt)


def test_a_staff_command_that_staffed_no_one_says_nothing(monkeypatch):
    from boss_engine.services import metatron_command

    sent: list[str] = []
    monkeypatch.setattr(core, "emit_event", lambda c, p, t, *a, **k: sent.append(t))
    cmd = metatron_command.MetatronCommand(SimpleNamespace())
    cmd._receipt(object(), "p1", "staff", {"assignments": 0}, changed=False)
    assert sent == []
    cmd._receipt(object(), "p1", "staff", {"assignments": 2}, changed=True)
    assert sent == ["metatron.command.staff"]

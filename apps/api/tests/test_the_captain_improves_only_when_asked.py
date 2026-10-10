"""The Captain's own improve passes are opt-in on a new install (first-run walk, 2026-10-10).

Right after a stranger's first PR, the Captain started its own "Captain: QUALITY IMPROVE PASS"
build: noise on a new install, and their model budget. Now:
  * a pass the Captain decides on itself (the worker's overwatch tick, a drive with no directive)
    starts only when the project turned `captain_auto_improve` on in Govern; otherwise it is held
    with that said, and nothing is started;
  * a pass somebody asked for (an operator's directive, a focused agent someone created) is not
    affected;
  * an install that existed before this keeps its passes on (db/schema.sql's one-time step), and
    the step never runs twice, so a project created later starts off.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.api.tests import _factory_support as fx
from boss_engine.config import get_settings
from boss_engine.db import split_statements
from boss_engine.services import project_settings as ps
from boss_engine.services.metatron_captain import (
    AUTO_IMPROVE_KNOB,
    MetatronCaptain,
    captain_auto_improve_on,
    captain_launch_gate,
)

ONCE_KEY = "captain_auto_improve_kept_for_existing"


@pytest.fixture(autouse=True)
def _fresh(engine):
    def wipe():
        with engine.begin() as c:
            c.execute(text("DELETE FROM project_settings WHERE key = :k"), {"k": AUTO_IMPROVE_KNOB})
            c.execute(text("DELETE FROM code_task_runs WHERE project_id = :p AND title ILIKE 'Captain:%'"),
                      {"p": fx.PROJECT})
    wipe()
    yield
    wipe()


def _launch(engine, monkeypatch, *, directive):
    """The Captain's apply step for a factory launch, with the build itself faked."""
    started: list[dict] = []

    def fake_start(connection, **kw):
        started.append(kw)
        return f"ctr_fake_{len(started)}"

    monkeypatch.setattr("boss_engine.services.code_task_service.start_code_task", fake_start)
    move = {"kind": "launch_factory_job", "detail": "QUALITY IMPROVE PASS. Measured scorecard gaps: ux=40.",
            "finding_id": "scorecard_gaps", "apply": True}
    with engine.begin() as c:
        result = MetatronCaptain(get_settings())._apply_validated_move(
            c, project_id=fx.PROJECT, actor="system", move=move, verify_run=None, directive=directive)
    return result, started


def test_a_new_install_has_the_captains_own_passes_off(engine):
    with engine.begin() as c:
        assert get_settings().captain_auto_improve == 0
        assert captain_auto_improve_on(c, fx.PROJECT, get_settings()) is False
        held = captain_launch_gate(c, fx.PROJECT, settings=get_settings(), asked=False)
    assert held and held["reason"] == "captain_auto_improve_off"
    assert "Govern" in held["message"] and "Captain self-improves" in held["message"]


def test_the_overwatch_pass_is_held_and_nothing_starts(engine, monkeypatch):
    result, started = _launch(engine, monkeypatch, directive=None)
    assert result["status"] == "rejected" and result["held"]["reason"] == "captain_auto_improve_off"
    assert started == []


def test_turned_on_in_govern_the_pass_starts(engine, monkeypatch):
    with engine.begin() as c:
        ps.set_one(c, fx.PROJECT, AUTO_IMPROVE_KNOB, 1, "ceo")
    result, started = _launch(engine, monkeypatch, directive=None)
    assert result["status"] == "applied", result
    assert len(started) == 1 and started[0]["title"].startswith("Captain: QUALITY IMPROVE PASS")


def test_a_pass_somebody_asked_for_is_not_affected(engine, monkeypatch):
    result, started = _launch(engine, monkeypatch, directive="Make the empty state say what to do")
    assert result["status"] == "applied", result
    assert len(started) == 1


def test_govern_shows_it_as_an_off_switch_and_saves_it(client):
    body = client.get("/v1/govern", params={"project_id": fx.PROJECT}).json()
    control = {c["key"]: c for c in body["controls"]}[AUTO_IMPROVE_KNOB]
    assert control["unit"] == "switch" and control["value_effective"] == 0
    assert control["name"] == "Captain self-improves" and control["source"] == "default"
    assert control["write"] == {"method": "PUT", "path": f"/v1/settings/{AUTO_IMPROVE_KNOB}"}
    with pytest.raises(ValueError):
        ps.coerce(AUTO_IMPROVE_KNOB, 2)


def _once_statement() -> str:
    sql = get_settings().schema_path.read_text(encoding="utf-8")
    [stmt] = [s for s in split_statements(sql) if ONCE_KEY in s and "DO $$" in s]
    return stmt


def test_an_install_that_existed_keeps_its_passes_on_and_the_step_runs_once(engine):
    stmt = _once_statement()
    with engine.begin() as c:
        # As an upgrade sees it: projects exist, the step has never run.
        c.execute(text("DELETE FROM schema_once WHERE key = :k"), {"k": ONCE_KEY})
        c.exec_driver_sql(stmt)
        assert c.execute(text("SELECT value FROM project_settings WHERE project_id = :p AND key = :k"),
                         {"p": fx.PROJECT, "k": AUTO_IMPROVE_KNOB}).scalar() == 1
        assert captain_auto_improve_on(c, fx.PROJECT, get_settings()) is True
        # The next deploy re-applies the schema: the step has run, so a project without the row
        # (created after the upgrade) stays at the default, off.
        c.execute(text("DELETE FROM project_settings WHERE key = :k"), {"k": AUTO_IMPROVE_KNOB})
        c.exec_driver_sql(stmt)
        assert c.execute(text("SELECT count(*) FROM project_settings WHERE key = :k"),
                         {"k": AUTO_IMPROVE_KNOB}).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM schema_once WHERE key = :k"),
                         {"k": ONCE_KEY}).scalar() == 1

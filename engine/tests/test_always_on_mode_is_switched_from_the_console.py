"""Always-on agents can be switched on from the console, not only from .env (design §9: "one
switch for all ambient work — UI plus API plus env var").

* Nothing set anywhere → off.
* A person (autonomy:manage) chooses propose | route | off in Govern; it is stored install-wide,
  audited (who, when, from what), and the tick, the learning tick and the dossier follow it.
* BOSS_AMBIENT_COS in .env pins the mode: the console shows it and cannot change it.
* Turning it on gives no class more room: the per-class level still decides (gate unchanged).
* The kill switch still wins.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text

from boss_engine.services.ambient import chief_of_staff, policy

from ._ambient_support import eng, project  # noqa: F401


def _settings(cos: str = "", **kw):
    return SimpleNamespace(ambient_cos_mode=cos, ambient_signals=None, ambient_learning=None,
                           ambient_cos_model_calls=0, ambient_squads=0, **kw)


@pytest.fixture()
def conn(eng):
    """One transaction rolled back at the end: the mode row is install-wide."""
    with eng.connect() as c:
        tx = c.begin()
        c.execute(text("DELETE FROM system_state WHERE key = :k"), {"k": policy.MODE_STATE_KEY})
        try:
            yield c
        finally:
            tx.rollback()


def test_nothing_set_is_off(conn):
    state = policy.mode_state(conn, _settings())
    assert state == {"mode": "route", "source": "default", "pinned": False, "by": None, "at": None}


@pytest.mark.parametrize("mode", ["propose", "route", "off"])
def test_a_person_turns_it_on_in_the_console_and_it_is_audited(project, conn, mode):
    state = policy.set_mode(conn, _settings(), mode=mode, actor="person:owner", reason="demo")
    assert state["mode"] == mode and state["source"] == "console" and state["by"] == "person:owner"
    assert policy.effective_mode(conn, _settings()) == mode
    assert policy.signals_on(_settings(), state["mode"]) is (mode != "off")
    assert policy.learning_on(_settings(), state["mode"]) is (mode != "off")
    event = conn.execute(text(
        "SELECT data FROM events WHERE project_id = :p AND event_type = 'ambient.mode_changed' "
        "ORDER BY created_at DESC LIMIT 1"), {"p": project}).scalar()
    assert event is not None and event["to"] == mode and event["from"] == "route"   # the default: on


def test_env_pins_the_mode_and_the_console_cannot_change_it(conn):
    policy.set_mode(conn, _settings(), mode="route", actor="person:owner")
    pinned = _settings(cos="propose")
    state = policy.mode_state(conn, pinned)
    assert state["mode"] == "propose" and state["source"] == "env" and state["pinned"]
    with pytest.raises(ValueError, match="pinned"):
        policy.set_mode(conn, pinned, mode="off", actor="person:owner")


def test_an_unknown_mode_is_refused(conn):
    with pytest.raises(ValueError):
        policy.set_mode(conn, _settings(), mode="yolo", actor="person:owner")


def test_the_tick_follows_the_console_mode_and_the_stop_still_wins(eng):
    with eng.begin() as c:
        before = c.execute(text("SELECT value FROM system_state WHERE key = :k"),
                           {"k": policy.MODE_STATE_KEY}).scalar()
        policy.set_mode(c, _settings(), mode="propose", actor="person:owner")
    try:
        report = chief_of_staff.tick(eng, _settings(), project_id="p_no_such_project",
                                     sweep=False)
        assert report["mode"] == "propose"
        with eng.begin() as c:
            policy.set_mode(c, _settings(), mode="off", actor="person:owner")
        assert chief_of_staff.tick(eng, _settings(), project_id="p_no_such_project",
                                   sweep=False)["mode"] == "off"
    finally:
        with eng.begin() as c:
            if before is None:
                c.execute(text("DELETE FROM system_state WHERE key = :k"),
                          {"k": policy.MODE_STATE_KEY})
            else:
                c.execute(text("UPDATE system_state SET value = CAST(:v AS JSONB) WHERE key = :k"),
                          {"k": policy.MODE_STATE_KEY, "v": __import__("json").dumps(before)})


def test_turning_it_on_gives_no_class_more_room(project, conn):
    """The mode decides whether agents look; the class's level decides what they may do."""
    before = policy.gate(conn, project, "incident_response", "staging_rollback")
    policy.set_mode(conn, _settings(), mode="route", actor="person:owner")
    after = policy.gate(conn, project, "incident_response", "staging_rollback")
    assert before["allowed"] == after["allowed"] and before["level"] == after["level"]

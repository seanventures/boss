"""A STALE INCIDENT (HelpDesk AI 2026-11-07): Observability said "SEV3 · Recovered · p95 latency
940.9 ms · Recovered at 07:37; the incident is still open" for hours. Its latency alert had
cleared because traffic stopped (`too_little`), the rule kept reading quiet, the squad's item
had been closed — and the monitor holds an incident while a rule reads quiet ("quiet is not
recovery"), forever.

Now the quiet hold ends: QUIET_HOLD after the last alert cleared, with no breach since and
nobody working it, the monitor closes the incident and records why. While it is held, every
card says why it is open and when it closes on its own; while a squad works it, the squad
closes it, never the monitor.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from boss_engine.services import app_alerts as aa
from boss_engine.services import app_alerts_view as view
from boss_engine.services import incident_response

from .test_app_alerts import T0, _alerts, _app, _eval, _scrape, pg  # noqa: F401

FAST = {"0.1": 95, "1": 99, "+Inf": 100}
SLOW = {"0.1": 20, "1": 98, "+Inf": 100}


def _slow_then_silent(eng, pid):
    """Six slow minutes (latency fires), then a trickle: two requests a minute, too few for
    the rule's 20-request guard (staging nobody calls)."""
    plan = ["slow"] * 7 + ["trickle"] * 130
    with eng.begin() as c:
        t = _app(c, pid)
        aa.ensure_rules(c, t)
        _play(c, t, plan)
    return t


TRICKLE = {"0.1": 2, "1": 2, "+Inf": 2}


def _play(c, t, plan):
    cum, req = {k: 0.0 for k in FAST}, 0
    for i, kind in enumerate(plan):
        _scrape(c, t, T0 + timedelta(minutes=i), req, 0, dict(cum))
        step = SLOW if kind == "slow" else TRICKLE
        req += step["+Inf"]
        cum = {k: cum[k] + step[k] for k in cum}


def _incident(eng, inc_id):
    with eng.begin() as c:
        return dict(c.execute(text("SELECT * FROM incidents WHERE id = :i"),
                              {"i": inc_id}).mappings().one())


def _states(eng, pid, inc_id):
    with eng.begin() as c:
        return next(s for s in view.incident_states(c, pid) if s["id"] == inc_id)


def test_an_incident_held_by_a_quiet_rule_says_why_and_closes_on_its_own(pg):  # noqa: F811
    eng, pid = pg
    t = _slow_then_silent(eng, pid)
    _eval(eng, t, T0 + timedelta(minutes=6))
    lat = _alerts(eng, t, "latency_p95")[0]
    assert lat["state"] == "firing" and lat["incident_id"]
    inc_id = lat["incident_id"]

    # The window empties: the alert clears as too little traffic, the incident is HELD.
    _eval(eng, t, T0 + timedelta(minutes=14))
    lat = _alerts(eng, t, "latency_p95")[0]
    assert lat["state"] == "resolved" and lat["cleared_reason"] == "too_little"
    cleared = lat["cleared_at"]
    assert _incident(eng, inc_id)["status"] not in ("resolved", "dismissed"), \
        "quiet is not recovery: just after it went quiet, the incident is held"

    st = _states(eng, pid, inc_id)
    assert st["word"].startswith("Recovered; too little traffic to confirm it")
    assert "closes on its own" in st["word"] and "still open" not in st["word"]
    assert st["open_reason"]["key"] == "quiet" and st["open_reason"]["who"] == "boss"
    assert st["open_reason"]["closes_at"] == (cleared + aa.QUIET_HOLD).isoformat()

    # Inside the hold: still open.
    _eval(eng, t, cleared + aa.QUIET_HOLD - timedelta(minutes=2))
    assert _incident(eng, inc_id)["status"] not in ("resolved", "dismissed")

    # The hold is over with no breach and nobody on it: closed, and the record says why.
    at = cleared + aa.QUIET_HOLD + timedelta(minutes=1)
    out = _eval(eng, t, at)
    inc = _incident(eng, inc_id)
    assert inc["status"] == "resolved" and inc["lifecycle"] == "resolved"
    assert inc["resolved_at"] == at
    assert {"incident_id": inc_id, "action": "resolved"}.items() <= next(
        i for i in out["incidents"] if i["incident_id"] == inc_id).items()
    with eng.begin() as c:
        note = c.execute(text(
            "SELECT payload FROM incident_evidence WHERE incident_id = :i AND kind = 'note' "
            "AND ref = 'quiet_hold'"), {"i": inc_id}).scalar()
        assert note and "no breach for 30 min" in note["text"]
        assert view.incident_states(c, pid, incident_ids=[inc_id]) == [], \
            "a closed incident is no longer listed as open anywhere"


def test_a_squad_working_it_holds_the_incident_past_the_quiet_hold(pg, monkeypatch):  # noqa: F811
    eng, pid = pg
    t = _slow_then_silent(eng, pid)
    _eval(eng, t, T0 + timedelta(minutes=6))
    inc_id = _alerts(eng, t, "latency_p95")[0]["incident_id"]
    _eval(eng, t, T0 + timedelta(minutes=14))
    cleared = _alerts(eng, t, "latency_p95")[0]["cleared_at"]

    monkeypatch.setattr(incident_response, "squad_working", lambda c, i: "work_sq")
    st = _states(eng, pid, inc_id)
    assert st["open_reason"]["key"] == "squad" and st["open_reason"]["who"] == "squad"
    assert "SRE squad still has it" in st["word"]
    _eval(eng, t, cleared + aa.QUIET_HOLD + timedelta(minutes=5))
    assert _incident(eng, inc_id)["status"] not in ("resolved", "dismissed"), \
        "a squad on it closes it by its own recovery check, never the monitor's quiet hold"


def test_a_breach_during_the_hold_keeps_it_one_incident(pg):  # noqa: F811
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid)
        aa.ensure_rules(c, t)
        # slow (fires) → a trickle → slow again 20 min later (inside the hold) → a trickle
        _play(c, t, ["slow"] * 7 + ["trickle"] * 13 + ["slow"] * 7 + ["trickle"] * 80)
    _eval(eng, t, T0 + timedelta(minutes=6))
    inc_id = _alerts(eng, t, "latency_p95")[0]["incident_id"]
    _eval(eng, t, T0 + timedelta(minutes=14))
    assert _incident(eng, inc_id)["status"] not in ("resolved", "dismissed")
    _eval(eng, t, T0 + timedelta(minutes=26))
    rows = _alerts(eng, t, "latency_p95")
    assert rows[-1]["state"] == "firing" and rows[-1]["incident_id"] == inc_id, \
        "a breach while held joins the same open incident"
    _eval(eng, t, T0 + timedelta(minutes=34))
    second = _alerts(eng, t, "latency_p95")[-1]["cleared_at"]
    # The hold counts from the LAST alert that cleared, not the first.
    _eval(eng, t, second + aa.QUIET_HOLD - timedelta(minutes=1))
    assert _incident(eng, inc_id)["status"] not in ("resolved", "dismissed")
    _eval(eng, t, second + aa.QUIET_HOLD + timedelta(minutes=1))
    assert _incident(eng, inc_id)["status"] == "resolved"


def test_health_never_calls_an_open_incident_resolved_by_the_squad(pg):  # noqa: F811
    """The squad's item was closed (no progress) while its incident stayed open: Health said
    "resolved by the squad", Observability "the incident is still open". Now Health lists the
    open incident with Observability's words — why it is open, and when it closes."""
    from boss_engine.services.project_experience import health

    eng, pid = pg
    t = _slow_then_silent(eng, pid)
    _eval(eng, t, T0 + timedelta(minutes=6))
    inc_id = _alerts(eng, t, "latency_p95")[0]["incident_id"]
    _eval(eng, t, T0 + timedelta(minutes=14))
    with eng.begin() as c:
        wid = f"work_q_{inc_id[-8:]}"
        c.execute(text(
            "INSERT INTO work_items (id, project_id, subject_type, subject_id, title, ambient, "
            " fingerprint, lifecycle, task_class, created_by) VALUES (:i, :p, 'incident', :s, "
            " 'p95 latency on Order API', true, :s, 'resolved', 'incident_staging_mitigation', "
            " 'test')"), {"i": wid, "p": pid, "s": f"inc:{inc_id}"})
        c.execute(text("INSERT INTO work_item_links (work_item_id, kind, ref, project_id) "
                       "VALUES (:w, 'incident', :i, :p)"), {"w": wid, "i": inc_id, "p": pid})
        rows = {r["id"]: r for r in health.incidents(c, pid)}
        word = view.incident_states(c, pid, incident_ids=[inc_id])[0]["word"]
    assert inc_id in rows, "the open incident is listed, not hidden behind the closed item"
    row = rows[inc_id]
    assert row["outcome_word"].startswith("recovered; too little traffic to confirm it")
    assert word.lower().endswith(row["outcome_word"].split("; ", 1)[1])
    assert row["state"] == "running" and row["closes_at"]

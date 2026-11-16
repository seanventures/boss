"""A firing alert with silenced_by or inhibited_by set is off the incident list.

Count those rows. A quiet incident list is not a healthy system. Pending and resolved
do not count. Another project does not count. A firing row with both columns null
does not count.
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from boss_engine.services.silenced_firing import silenced_firing

from ._ambient_support import eng, make_project, project, purge  # noqa: F401


def _alert(c, pid, *, state="firing", silenced_by=None, inhibited_by=None):
    """Same required columns as the insert in test_monitoring_state_and_noise."""
    aid = f"alrt_{uuid4().hex[:8]}"
    c.execute(text(
        "INSERT INTO app_alerts (id, project_id, rule_kind, fingerprint, alertname, "
        " state, active_at, silenced_by, inhibited_by) "
        "VALUES (:a, :p, 'error_rate', :f, 'AppErrorRate', :st, now(), :s, :i)"),
        {"a": aid, "p": pid, "f": uuid4().hex[:16], "st": state,
         "s": silenced_by, "i": inhibited_by})
    return aid


def test_no_held_back_firing_alert_is_not_called_healthy(eng, project):
    with eng.begin() as c:
        out = silenced_firing(c, project)
    assert out["project_id"] == project
    assert out["held_back"] == 0
    assert out["words"] == "No firing alert is held back."
    assert "healthy" not in out["words"].lower()


def test_one_silenced_firing_alert_is_named_in_the_singular(eng, project):
    with eng.begin() as c:
        _alert(c, project, silenced_by="sil_load")
        out = silenced_firing(c, project)
    assert out == {
        "project_id": project,
        "held_back": 1,
        "words": "1 firing alert held back.",
    }


def test_one_inhibited_firing_alert_is_held_back(eng, project):
    with eng.begin() as c:
        _alert(c, project, inhibited_by="alrt_health")
        out = silenced_firing(c, project)
    assert out["held_back"] == 1
    assert out["words"] == "1 firing alert held back."


def test_silenced_and_inhibited_firing_alerts_count_once_each(eng, project):
    with eng.begin() as c:
        _alert(c, project, silenced_by="sil_a")
        _alert(c, project, inhibited_by="alrt_health")
        _alert(c, project, silenced_by="sil_b", inhibited_by="alrt_health")
        out = silenced_firing(c, project)
    assert out["held_back"] == 3
    assert out["words"] == "3 firing alerts held back."
    assert out["project_id"] == project


def test_pending_resolved_plain_firing_and_other_projects_do_not_count(eng, project):
    other = make_project(eng)
    try:
        with eng.begin() as c:
            _alert(c, project, state="pending", silenced_by="sil_pending")
            _alert(c, project, state="resolved", inhibited_by="alrt_resolved")
            _alert(c, project, state="firing")
            _alert(c, other, silenced_by="sil_other")
            _alert(c, other, inhibited_by="alrt_other")
            out = silenced_firing(c, project)
            other_out = silenced_firing(c, other)
        assert out == {
            "project_id": project,
            "held_back": 0,
            "words": "No firing alert is held back.",
        }
        assert other_out["held_back"] == 2
        assert other_out["words"] == "2 firing alerts held back."
        assert other_out["project_id"] == other
    finally:
        purge(eng, other)

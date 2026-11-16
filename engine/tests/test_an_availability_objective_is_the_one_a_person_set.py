"""The availability objective is the percent a person stored, and the burn uses it.

99.5 is the install default until that row exists. Probe uptime is a measurement, never
an objective, including when someone writes the measured number into the configuration
as text.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project, sample, target
from boss_engine.services.project_experience import health

INSTALL_DEFAULT = "SLO 99.5% is the install default, not your objective."


def _stage(c):
    pid = project(c)
    tid = target(c, pid, f"{pid}_t", "invoice", env="staging")
    now = c.execute(text("SELECT now()")).scalar()
    # 124 successes and one failure inside the 24h window. The failure is older than
    # the newest five probes, so the probe verdict stays up and the burn is the signal.
    for i in range(124):
        sample(c, pid, tid, now - timedelta(minutes=10 + i), True, 40)
    sample(c, pid, tid, now - timedelta(minutes=200), False, None)
    return pid, tid, now


def _put(c, pid: str, body: str) -> None:
    c.execute(text(
        "INSERT INTO project_configurations (id, project_id, config_key, body, updated_by) "
        "VALUES (:id, :p, 'availability_objective', CAST(:body AS JSONB), 'person:ada') "
        "ON CONFLICT (project_id, config_key) DO UPDATE "
        "SET body = EXCLUDED.body, updated_by = EXCLUDED.updated_by, updated_at = now()"),
        {"id": f"cfg_slo_{pid}", "p": pid, "body": body})


def test_the_burn_uses_the_availability_objective_a_person_set():
    eng = pg_engine()
    with eng.begin() as c:
        pid, tid, now = _stage(c)
        env = health.environments(c, pid, now=now)[0]
        # 124 / 125 = 99.2. That measured percent is not stored as the objective.
        assert env["uptime_pct"] == 99.2
        assert env["slo"] == {"target": 99.5, "burn": "warning", "budget_used_pct": 100}
        assert env.get("slo_words") == INSTALL_DEFAULT
        held = health.target_states(c, [tid], now=now)[tid]
        assert held["health"] == "degraded"
        assert held["health_reason"] == "SLO at risk: 99.2% up, target 99.5%"

        _put(c, pid, '{"percent": 99.0}')
        chosen = health.environments(c, pid, now=now)[0]
        assert chosen["uptime_pct"] == 99.2
        assert chosen["slo"] == {"target": 99.0, "burn": "ok", "budget_used_pct": 80}
        assert chosen.get("slo_words") is None
        assert chosen["health"] == "healthy" and chosen["health_reason"] is None
        assert health.target_states(c, [tid], now=now)[tid]["health"] == "healthy"

        # A probe reading written as text is not an objective.
        _put(c, pid, '{"percent": "99.2"}')
        invented = health.environments(c, pid, now=now)[0]
        assert invented["slo"] == {"target": 99.5, "burn": "warning", "budget_used_pct": 100}
        assert invented.get("slo_words") == INSTALL_DEFAULT
        _put(c, pid, '{"note": "no percent"}')
        assert health.environments(c, pid, now=now)[0]["slo"]["target"] == 99.5

        assert health.set_availability_objective(c, pid, 99.9, actor="person:ada") == 99.9
        breached = health.environments(c, pid, now=now)[0]
        assert breached["slo"] == {"target": 99.9, "burn": "breach", "budget_used_pct": 100}
        assert breached["health_reason"] == "SLO breached: 99.2% up, target 99.9%"
        assert health.target_states(c, [tid], now=now)[tid]["health"] == "degraded"
        stored = c.execute(text(
            "SELECT body, updated_by FROM project_configurations "
            "WHERE project_id = :p AND config_key = 'availability_objective'"),
            {"p": pid}).mappings().one()
        assert stored["body"]["percent"] == 99.9 and stored["updated_by"] == "person:ada"
        assert health.chosen_objective(c, pid) == 99.9

        for bad in (None, True, "99.9", 0, 100.1, 101, -1, float("nan"), float("inf")):
            with pytest.raises(ValueError):
                health.set_availability_objective(c, pid, bad, actor="person:ada")
        for actor in ("", "   "):
            with pytest.raises(ValueError):
                health.set_availability_objective(c, pid, 99.0, actor=actor)
        assert health.chosen_objective(c, pid) == 99.9
        assert health.environments(c, pid, now=now)[0]["slo"]["target"] == 99.9

        assert health.set_availability_objective(c, pid, 99, actor="person:ada") == 99.0
        assert health.chosen_objective(c, pid) == 99.0

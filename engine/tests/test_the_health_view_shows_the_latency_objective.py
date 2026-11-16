"""Each environment on the health view shows the latency objective a person stored.

A missing row is null, with the sentence for none. A probe p95 — 40 ms on one environment
and 300 ms on another — is a measurement, not an objective, and a measured number written
into the configuration as text is not one either. The availability budget is left alone.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from ._obs_seed import pg_engine, project, sample, target
from boss_engine.services import latency_objective as latency
from boss_engine.services.project_experience import health

INSTALL_DEFAULT = "SLO 99.5% is the install default, not your objective."
NONE = "No latency objective is stored."


def _stage(c):
    pid = project(c)
    staging = target(c, pid, f"{pid}_s", "invoice", env="staging")
    production = target(c, pid, f"{pid}_p", "invoice", env="production")
    now = c.execute(text("SELECT now()")).scalar()
    for i in range(5):
        at = now - timedelta(minutes=10 + i)
        sample(c, pid, staging, at, True, 40)
        sample(c, pid, production, at, True, 300)
    return pid, now


def _by_key(envs):
    return {e["key"]: e for e in envs}


def _assert_none(env: dict) -> None:
    assert "latency_objective_ms" in env
    assert env["latency_objective_ms"] is None
    assert env["latency_words"] == NONE
    assert env["latency_objective_ms"] != 300
    assert env["slo"]["target"] == 99.5
    assert env["slo_words"] == INSTALL_DEFAULT


def test_the_health_view_shows_the_latency_objective_a_person_set():
    eng = pg_engine()
    with eng.begin() as c:
        pid, now = _stage(c)
        other = project(c)
        other_target = target(c, other, f"{other}_s", "ledger", env="staging")
        sample(c, other, other_target, now - timedelta(minutes=3), True, 90)

        envs = _by_key(health.environments(c, pid, now=now))
        viewed = _by_key(health.view(c, pid, now=now)["environments"])
        assert set(envs) == {"staging", "production"}
        assert set(viewed) == {"staging", "production"}
        assert envs["staging"]["p95_ms"] == 40
        assert envs["production"]["p95_ms"] == 300
        for source in (envs, viewed):
            for env in source.values():
                _assert_none(env)
        held = {key: (env["slo"], env["slo_words"], env["p95_ms"]) for key, env in envs.items()}

        assert latency.set_latency_objective(c, pid, 250, actor="person:ada") == 250
        envs = _by_key(health.environments(c, pid, now=now))
        viewed = _by_key(health.view(c, pid, now=now)["environments"])
        for source in (envs, viewed):
            for env in source.values():
                assert env["latency_objective_ms"] == 250
                assert env["latency_words"] == "Latency objective is 250 ms."
                assert env["slo"]["target"] == 99.5
        for key, env in envs.items():
            assert (env["slo"], env["slo_words"], env["p95_ms"]) == held[key]

        # A probe reading written as text is not an objective, including the number 300.
        c.execute(text(
            "UPDATE project_configurations SET body = CAST(:body AS JSONB) "
            "WHERE project_id = :p AND config_key = 'latency_objective'"),
            {"p": pid, "body": '{"ms": "300"}'})
        envs = _by_key(health.environments(c, pid, now=now))
        for env in envs.values():
            _assert_none(env)
            assert env["p95_ms"] in (40, 300)

        assert latency.set_latency_objective(c, pid, 180.5, actor="person:grace") == 180.5
        envs = _by_key(health.environments(c, pid, now=now))
        for env in envs.values():
            assert env["latency_objective_ms"] == 180.5
            assert env["latency_words"] == "Latency objective is 180.5 ms."
            assert (env["slo"], env["slo_words"]) == (held[env["key"]][0], held[env["key"]][1])

        # Another project's stored objective does not fill a project with no row.
        c.execute(text(
            "DELETE FROM project_configurations WHERE project_id = :p "
            "AND config_key = 'latency_objective'"), {"p": pid})
        assert latency.set_latency_objective(c, other, 80, actor="person:ada") == 80
        gone = _by_key(health.environments(c, pid, now=now))
        kept = _by_key(health.environments(c, other, now=now))
        for env in gone.values():
            _assert_none(env)
        assert kept["staging"]["latency_objective_ms"] == 80
        assert kept["staging"]["latency_words"] == "Latency objective is 80 ms."
        assert kept["staging"]["p95_ms"] == 90

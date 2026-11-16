"""The latency objective is the milliseconds a person stored.

Nothing is invented when that row is missing. A measured p95 written as text is not an
objective, and neither is zero, a negative, or a blank actor.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.services import latency_objective as latency


def test_the_latency_objective_is_the_one_a_person_set():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
        assert latency.chosen_latency_ms(c, pid) is None
        assert latency.latency_words(None) == "No latency objective is stored."
        assert latency.latency_words(latency.chosen_latency_ms(c, pid)) == (
            "No latency objective is stored.")

        assert latency.set_latency_objective(c, pid, 250, actor="person:ada") == 250
        assert latency.chosen_latency_ms(c, pid) == 250
        assert latency.latency_words(latency.chosen_latency_ms(c, pid)) == (
            "Latency objective is 250 ms.")
        stored = c.execute(text(
            "SELECT id, body, updated_by FROM project_configurations "
            "WHERE project_id = :p AND config_key = 'latency_objective'"),
            {"p": pid}).mappings().one()
        assert stored["id"] == f"cfg_latency_{pid}"
        assert stored["body"] == {"ms": 250}
        assert stored["updated_by"] == "person:ada"

        # A probe reading written as text is not an objective, and neither is a body with no ms.
        c.execute(text(
            "UPDATE project_configurations SET body = CAST(:body AS JSONB) "
            "WHERE project_id = :p AND config_key = 'latency_objective'"),
            {"p": pid, "body": '{"ms": "180"}'})
        assert latency.chosen_latency_ms(c, pid) is None
        assert latency.latency_words(latency.chosen_latency_ms(c, pid)) == (
            "No latency objective is stored.")
        c.execute(text(
            "UPDATE project_configurations SET body = CAST(:body AS JSONB) "
            "WHERE project_id = :p AND config_key = 'latency_objective'"),
            {"p": pid, "body": '{"note": "no ms"}'})
        assert latency.chosen_latency_ms(c, pid) is None

        assert latency.set_latency_objective(c, pid, 180.5, actor="person:grace") == 180.5
        assert latency.chosen_latency_ms(c, pid) == 180.5
        assert latency.latency_words(180.5) == "Latency objective is 180.5 ms."
        replaced = c.execute(text(
            "SELECT id, body, updated_by FROM project_configurations "
            "WHERE project_id = :p AND config_key = 'latency_objective'"),
            {"p": pid}).mappings().one()
        assert replaced["id"] == f"cfg_latency_{pid}"
        assert replaced["body"]["ms"] == 180.5
        assert replaced["updated_by"] == "person:grace"

        for bad in (None, True, False, "250", "300", 0, 0.0, -1, -0.5,
                    float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError):
                latency.set_latency_objective(c, pid, bad, actor="person:ada")
        for actor in ("", "   ", None):
            with pytest.raises(ValueError):
                latency.set_latency_objective(c, pid, 200, actor=actor)
        with pytest.raises(ValueError):
            latency.set_latency_objective(c, pid, 200)
        assert latency.chosen_latency_ms(c, pid) == 180.5
        assert replaced["body"]["ms"] != 300
        assert latency.chosen_latency_ms(c, pid) != 300

        assert latency.set_latency_objective(c, pid, 200, actor="  person:ada  ") == 200
        kept = c.execute(text(
            "SELECT id, body, updated_by FROM project_configurations "
            "WHERE project_id = :p AND config_key = 'latency_objective'"),
            {"p": pid}).mappings().one()
        assert kept["id"] == f"cfg_latency_{pid}"
        assert kept["body"]["ms"] == 200
        assert kept["updated_by"] == "person:ada"
        assert latency.latency_words(latency.chosen_latency_ms(c, pid)) == (
            "Latency objective is 200 ms.")

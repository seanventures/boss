"""`Probe now` probes ONLY the open app (operator decision §11.5), not the project sweep.

POST /v1/observability/apps/{target_id}/probe asks that one target through the connector the
sweep uses, records one sample for it and nothing for its neighbours, and answers the state
word it leaves the app in. Another project's target is a 404.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.tests._obs_api_seed import PROJECT_ID, deployed_target, second_project


def test_probe_now_touches_one_app(client, engine, monkeypatch):
    from boss_engine.services import deployment_health

    asked: list[str] = []

    def fake_probe(target, settings):
        asked.append(target["id"])
        return {"ok": True, "status_code": 200, "latency_ms": 87, "error": None}

    monkeypatch.setattr(deployment_health, "probe", fake_probe)
    other = second_project(engine)
    with begin(engine) as c:
        one = deployed_target(c, PROJECT_ID, "t_probe_one", "invoice")
        two = deployed_target(c, PROJECT_ID, "t_probe_two", "billing")
        theirs = deployed_target(c, other, "t_probe_theirs", "theirs")

    r = client.post(f"/v1/observability/apps/{one}/probe", json={"project_id": PROJECT_ID})
    assert r.status_code == 200, r.text
    body = r.json()
    assert asked == [one]
    assert body["word"] == "Up" and body["tone"] == "green" and body["checked_at"]
    with begin(engine) as c:
        counts = dict(c.execute(text(
            "SELECT deployment_target_id, count(*) FROM deployment_health_samples "
            "WHERE deployment_target_id = ANY(:ids) GROUP BY 1"),
            {"ids": [one, two, theirs]}).all())
    assert counts == {one: 1}

    miss = client.post(f"/v1/observability/apps/{theirs}/probe", json={"project_id": PROJECT_ID})
    assert miss.status_code == 404
    assert asked == [one]

"""Project A never sees project B's model calls or findings (spec §9.1, Observability half).

Model calls are a project's own ledger. System findings carry a project when the doctor knows
one; an install-wide finding (no project) is everyone's machine and shows in both.
"""
from __future__ import annotations

from apps.api.app.db import begin
from apps.api.tests._obs_api_seed import (PROJECT_ID, deployed_target, llm_call, sample,
                                          second_project)


def test_calls_findings_and_apps_stay_in_their_project(client, engine):
    other = second_project(engine)
    with begin(engine) as c:
        llm_call(c, PROJECT_ID, model="mine-model", cost=1_000)
        llm_call(c, other, model="their-model", cost=9_000_000)
        tid = deployed_target(c, other, "t_theirs", "their-app")
        sample(c, other, tid)
        for fid, pid, kind in (("sf_mine", PROJECT_ID, "k_mine"), ("sf_theirs", other, "k_theirs"),
                               ("sf_install", None, "k_install")):
            c.exec_driver_sql(
                "INSERT INTO system_findings (id, project_id, kind, title) VALUES (%s, %s, %s, %s)",
                (fid, pid, kind, f"Finding {fid}"))
    p = {"project_id": PROJECT_ID}

    models = client.get("/v1/observability/models", params=p).json()
    keys = {g["key"] for g in models["groups"]}
    assert "mine-model" in keys and "their-model" not in keys
    assert models["totals"]["spend_micros"] < 9_000_000

    apps = client.get("/v1/observability/apps", params=p).json()
    assert all(r["target_id"] != "t_theirs" for r in apps["rows"])
    assert apps["spend_24h_micros"] < 9_000_000
    assert client.get("/v1/observability/apps/t_theirs", params=p).status_code == 404

    found = {f["finding_id"] for f in client.get("/v1/observability/system",
                                                 params=p).json()["findings"]}
    assert {"sf_mine", "sf_install"} <= found and "sf_theirs" not in found
    theirs = {f["finding_id"] for f in client.get("/v1/observability/system",
                                                  params={"project_id": other}).json()["findings"]}
    assert {"sf_theirs", "sf_install"} <= theirs and "sf_mine" not in theirs

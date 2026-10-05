"""Every Observability GET answers 304 when nothing changed (infra-observability spec §6.2, §9.1).

Each view carries an ETag (a hash of the body without generated_at); sending it back is a
304 with no body; a seeded change is a new ETag. Also: the app drawer is a 404 for a target
that is not this project's, and GET /v1/llm-calls takes `since` for the group drawer.
"""
from __future__ import annotations

import pytest

from apps.api.app.db import begin
from apps.api.tests._obs_api_seed import PROJECT_ID, deployed_target, llm_call, sample


@pytest.fixture
def steady(monkeypatch):
    """The machine gauges are read live; hold them still so 'unchanged' means the data."""
    from boss_engine.services import admission, obs_view

    cap = admission.Capacity(total_mb=8192, available_mb=4096, cores=4, disk_used_pct=41.0,
                             disk_free_gb=60.0, load_per_core=0.3)
    monkeypatch.setattr(admission, "sample", lambda: cap)
    monkeypatch.setattr(obs_view, "_pressure", lambda cap: {
        "bands": {"memory": {"used_pct": 50.0, "band": "ok"},
                  "cpu": {"load_per_core": 0.3, "band": "ok"},
                  "disk": {"used_pct": 41.0, "band": "ok"}},
        "state": "ok", "reasons": [], "scope": "machine", "oom_kills_new": 0,
        "swap_measured": True})
    obs_view.forget_sources()
    yield
    obs_view.forget_sources()


def _cycle(client, url, params):
    first = client.get(url, params=params)
    assert first.status_code == 200, first.text
    tag = first.headers.get("etag")
    assert tag and tag == first.json()["etag"]
    again = client.get(url, params=params, headers={"If-None-Match": tag})
    assert again.status_code == 304
    assert again.content == b""
    return tag


def test_every_obs_view_is_a_304_when_unchanged_and_new_when_changed(client, engine, steady):
    with begin(engine) as c:
        tid = deployed_target(c, PROJECT_ID, "t_live_304", "invoice")
        sample(c, PROJECT_ID, tid, ok=True, latency=140, ago_s=300)
        llm_call(c, PROJECT_ID)
    p = {"project_id": PROJECT_ID}
    tags = {
        "apps": _cycle(client, "/v1/observability/apps", p),
        "app": _cycle(client, f"/v1/observability/apps/{tid}", p),
        "models": _cycle(client, "/v1/observability/models", p),
        "system": _cycle(client, "/v1/observability/system", p),
        "sources": _cycle(client, "/v1/observability/sources", p),
    }
    for win in ("7d", "30d"):
        _cycle(client, "/v1/observability/apps", {**p, "win": win})
        _cycle(client, "/v1/observability/models", {**p, "win": win, "group": "stage",
                                                    "metric": "spend"})
    _cycle(client, "/v1/observability/apps", {**p, "cells": 48})

    with begin(engine) as c:
        sample(c, PROJECT_ID, tid, ok=False, latency=None, ago_s=30)
        llm_call(c, PROJECT_ID, model="m-b", ok=False)
        c.exec_driver_sql(
            "INSERT INTO system_findings (id, project_id, kind, severity, title, detail, "
            "needs_choice) VALUES ('sf_304', NULL, 'memory_high_304', 'warn', 'Memory high', "
            "'78%% used', false)")
    changed = {
        "apps": client.get("/v1/observability/apps", params=p),
        "app": client.get(f"/v1/observability/apps/{tid}", params=p),
        "models": client.get("/v1/observability/models", params=p),
        "system": client.get("/v1/observability/system", params=p),
    }
    for key, res in changed.items():
        assert res.status_code == 200 and res.headers["etag"] != tags[key], key
    assert any(f["finding_id"] == "sf_304" for f in changed["system"].json()["findings"])


def test_an_app_of_another_project_is_not_found(client):
    r = client.get("/v1/observability/apps/t_nowhere", params={"project_id": PROJECT_ID})
    assert r.status_code == 404


def test_llm_calls_take_since(client, engine):
    with begin(engine) as c:
        llm_call(c, PROJECT_ID, model="old-one", ago_s=3 * 86400)
        llm_call(c, PROJECT_ID, model="new-one", ago_s=60)
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    r = client.get("/v1/llm-calls", params={"project_id": PROJECT_ID, "since": since})
    assert r.status_code == 200, r.text
    models = {i["model"] for i in r.json()["items"]}
    assert "new-one" in models and "old-one" not in models
    bad = client.get("/v1/llm-calls", params={"project_id": PROJECT_ID, "since": "yesterday"})
    assert bad.status_code == 422

"""Infra views are project scoped (spec §9.1, the Infra half): project B never sees project A's
machines, apps, deploys or previews, and A's objects are 404 when asked for under B — reads and
writes alike."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from apps.api.tests.infra_seed import HOSTS, TARGETS, clear, seed

A = "boss-dogfood"
B = "p_infra_other"


@pytest.fixture()
def two(engine):
    seed(engine, A, samples=False)
    with engine.begin() as c:
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) SELECT :b, "
                       "workspace_id, 'Other', 'p-infra-other' FROM projects WHERE id = :a "
                       "ON CONFLICT DO NOTHING"), {"a": A, "b": B})
    yield
    with engine.begin() as c:
        clear(c, A)


def test_b_sees_none_of_a(client, two):
    ids = set(HOSTS) | set(TARGETS) | {"sd_failed", "sd_ok", "sd_wait", "dr_live", "rr_created",
                                       "pv_live", "pv_fail", "pv_ended", "pa_aws"}
    for path, extra in (("/v1/infra/fleet", {}), ("/v1/infra/deploys", {}),
                        ("/v1/infra/previews", {}), ("/v1/infra/cloud", {"view": "accounts"}),
                        ("/v1/infra/cloud", {"view": "resources"})):
        r = client.get(path, params={"project_id": B, **extra})
        assert r.status_code == 200, (path, r.text)
        blob = json.dumps(r.json())
        leaked = [i for i in ids if f'"{i}"' in blob or f":{i}" in blob]
        assert not leaked, (path, leaked)
    a = client.get("/v1/infra/deploys", params={"project_id": A}).json()
    assert a["counts"]["all"] == 9


def test_a_objects_are_404_under_b(client, two):
    for path in ("/v1/infra/hosts/h_ready", "/v1/infra/targets/t_live",
                 "/v1/infra/deploys/stack:sd_failed", "/v1/infra/deploys/delivery:dr_live"):
        assert client.get(path, params={"project_id": B}).status_code == 404, path
    for path, body in (("/v1/infra/targets/t_live/deploy", {}),
                       ("/v1/infra/targets/t_live/move", {"host_id": "h_ready"}),
                       ("/v1/infra/deploys/stack:sd_failed/rollback", {}),
                       ("/v1/infra/hosts/h_down/retire", {"confirm_name": "edge-2"}),
                       ("/v1/infra/previews/pv_live/restart", {})):
        r = client.post(path, json={"project_id": B, "actor": "ceo", **body})
        assert r.status_code in (403, 404), (path, r.status_code, r.text)

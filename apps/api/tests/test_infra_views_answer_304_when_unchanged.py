"""Every Infra view GET carries an ETag; the same ETag is a 304; a seeded change is a new ETag.

The console polls these through `usePolled` (spec §6.2, §6.8): an unchanged view must cost a
304 and no render, so the ETag hashes the body WITHOUT `generated_at` and is stable while the
stored rows are.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.api.tests.infra_seed import seed

HOME = "boss-dogfood"

GETS = [
    ("/v1/infra/fleet", {}),
    ("/v1/infra/hosts/h_ready", {}),
    ("/v1/infra/hosts/this", {}),
    ("/v1/infra/targets/t_live", {}),
    ("/v1/infra/deploys", {}),
    ("/v1/infra/deploys", {"state": "failed"}),
    ("/v1/infra/deploys/stack:sd_failed", {}),
    ("/v1/infra/deploys/delivery:dr_live", {}),
    ("/v1/infra/deploys/registry:rr_created", {}),
    ("/v1/infra/previews", {}),
    ("/v1/infra/cloud", {"view": "accounts"}),
    ("/v1/infra/cloud", {"view": "resources"}),
    ("/v1/infra/cloud", {"view": "capacity"}),
    ("/v1/infra/cloud", {"view": "spend"}),
    ("/v1/infra/cloud", {"view": "changes"}),
]


@pytest.fixture()
def world(engine):
    seed(engine, HOME)
    yield
    with engine.begin() as c:
        from apps.api.tests.infra_seed import clear
        clear(c, HOME)


@pytest.mark.parametrize("path,extra", GETS, ids=[p + str(e) for p, e in GETS])
def test_every_infra_view_answers_304_when_unchanged(client, world, path, extra):
    params = {"project_id": HOME, **extra}
    first = client.get(path, params=params)
    assert first.status_code == 200, first.text
    etag = first.headers.get("etag")
    assert etag and etag == first.json()["etag"]
    assert first.json().get("partial", []) == [], first.json().get("partial")

    again = client.get(path, params=params, headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert len(again.content) <= 1024


def test_a_seeded_change_is_a_new_etag(client, engine, world):
    views = [("/v1/infra/fleet", {}), ("/v1/infra/deploys", {}), ("/v1/infra/previews", {}),
             ("/v1/infra/cloud", {"view": "changes"})]
    before = {p + str(e): client.get(p, params={"project_id": HOME, **e}).headers["etag"]
              for p, e in views}
    with engine.begin() as c:
        c.execute(text("UPDATE deployment_runs SET status = 'failed', "
                       "failure_reason = 'boom' WHERE id = 'dr_live'"))
        c.execute(text("UPDATE app_previews SET status = 'stopped' WHERE id = 'pv_live'"))
        c.execute(text("INSERT INTO changesets (id, project_id, title, summary, created_by) "
                       "VALUES ('cs_etag', :p, 'Scale up', 'one more worker', 'ceo')"),
                  {"p": HOME})
    for p, e in views:
        after = client.get(p, params={"project_id": HOME, **e},
                           headers={"If-None-Match": before[p + str(e)]})
        assert after.status_code == 200, p
        assert after.headers["etag"] != before[p + str(e)], p

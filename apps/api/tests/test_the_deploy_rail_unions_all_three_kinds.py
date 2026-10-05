"""The deploy rail unions all three kinds (spec §4.6, §6.3): stack deployments, registry runs
(deployment_runs planned straight through the registry, no trigger) and delivery deploys
(deployment_runs the delivery routes stamped with a trigger). Each appears once, newest first by
coalesce(finished, started, created); the keyset cursor pages without gaps or duplicates; the
chip counts equal a SQL recount."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.api.tests.infra_seed import clear, seed

HOME = "boss-dogfood"

UNION_SQL = """
SELECT 'stack:' || id AS ref, COALESCE(finished_at, apply_started_at, created_at) AS ts
FROM stack_deployments WHERE project_id = :p
UNION ALL
SELECT CASE WHEN trigger IS NULL THEN 'registry:' ELSE 'delivery:' END || id,
       COALESCE(finished_at, started_at, created_at)
FROM deployment_runs WHERE project_id = :p
ORDER BY ts DESC, ref DESC
"""


@pytest.fixture()
def world(engine):
    seed(engine, HOME, samples=False)
    yield
    with engine.begin() as c:
        clear(c, HOME)


def _all(client, **params):
    rows, cursor, pages = [], None, 0
    while True:
        body = client.get("/v1/infra/deploys", params={"project_id": HOME, "cursor": cursor,
                                                       **params}).json()
        rows += body["rows"]
        pages += 1
        cursor = body["next_cursor"]
        if not cursor:
            return rows, pages


def test_each_kind_appears_once_newest_first(client, engine, world):
    body = client.get("/v1/infra/deploys", params={"project_id": HOME}).json()
    with engine.begin() as c:
        expected = [r[0] for r in c.execute(text(UNION_SQL), {"p": HOME})]
    refs = [r["ref"] for r in body["rows"]]
    assert refs == expected
    assert len(refs) == len(set(refs))
    assert {r["kind"] for r in body["rows"]} == {"stack", "registry", "delivery"}
    kinds = {r["ref"]: r["kind"] for r in body["rows"]}
    assert kinds["registry:rr_created"] == "registry"
    assert kinds["delivery:dr_live"] == "delivery"
    assert kinds["stack:sd_failed"] == "stack"


def test_the_cursor_pages_without_gaps_or_duplicates(client, engine, world):
    with engine.begin() as c:
        # Two deploys finishing at the same instant: the ref breaks the tie, so neither is
        # skipped nor repeated across a page boundary.
        c.execute(text(
            "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, "
            "environment, repo, target_kind, target_ref, status, trigger, created_by, "
            "finished_at) SELECT x, :p, 't_live', 'invoice', 'prod', 'acme/invoice', 'ssh', 'x', "
            "'succeeded', 'manual', 'ceo', now() - interval '30 minutes' "
            "FROM unnest(ARRAY['dr_tie_a', 'dr_tie_b', 'dr_tie_c']) AS x"), {"p": HOME})
        expected = [r[0] for r in c.execute(text(UNION_SQL), {"p": HOME})]
    rows, pages = _all(client, limit=2)
    assert [r["ref"] for r in rows] == expected
    assert pages == -(-len(expected) // 2)


def test_chip_counts_equal_a_recount(client, engine, world):
    body = client.get("/v1/infra/deploys", params={"project_id": HOME}).json()
    with engine.begin() as c:
        total = c.execute(text(f"SELECT count(*) FROM ({UNION_SQL}) u"), {"p": HOME}).scalar()
        failed = c.execute(text(
            "SELECT (SELECT count(*) FROM stack_deployments WHERE project_id = :p "
            "        AND status = 'failed') + (SELECT count(*) FROM deployment_runs "
            "        WHERE project_id = :p AND status IN ('failed', 'blocked'))"),
            {"p": HOME}).scalar()
        waiting = c.execute(text(
            "SELECT count(*) FROM stack_deployments s JOIN decisions d ON d.id = s.decision_id "
            "WHERE s.project_id = :p AND s.status = 'awaiting_approval' "
            "AND d.status = 'pending'"), {"p": HOME}).scalar()
        # Live: the newest successful deploy of each target that has one.
        live = c.execute(text(
            "SELECT count(DISTINCT target_id) FROM ("
            " SELECT target_id FROM stack_deployments WHERE project_id = :p AND status = 'healthy'"
            " UNION ALL SELECT deployment_target_id FROM deployment_runs WHERE project_id = :p "
            " AND status IN ('succeeded', 'deployed')) x"), {"p": HOME}).scalar()
    assert body["counts"] == {"all": total, "failed": failed, "needs_you": waiting, "live": live}
    assert body["counts"] == {"all": 9, "failed": 2, "needs_you": 1, "live": 4}


def test_filters_narrow_the_rail(client, world):
    failed = client.get("/v1/infra/deploys", params={"project_id": HOME, "state": "failed"}).json()
    assert {r["ref"] for r in failed["rows"]} == {"stack:sd_failed", "delivery:dr_down_fail"}
    needs = client.get("/v1/infra/deploys", params={"project_id": HOME,
                                                    "state": "needs_you"}).json()
    assert [r["ref"] for r in needs["rows"]] == ["stack:sd_wait"]
    assert needs["rows"][0]["word"] == "Needs you" and needs["rows"][0]["can"]["approve"]
    live = client.get("/v1/infra/deploys", params={"project_id": HOME, "state": "live"}).json()
    assert {r["ref"] for r in live["rows"]} == {"delivery:dr_live", "delivery:dr_down_ok",
                                                "delivery:dr_slow", "stack:sd_ok"}
    assert all(r["word"] == "Live" for r in live["rows"])
    shop = client.get("/v1/infra/deploys", params={"project_id": HOME, "app": "shop",
                                                  "env": "production"}).json()
    assert {r["ref"] for r in shop["rows"]} == {"stack:sd_ok", "stack:sd_failed"}
    assert shop["counts"]["all"] == 2
    assert "shop" in shop["filters"]["apps"] and "production" in shop["filters"]["envs"]


def test_a_row_says_who_approved_and_what_it_rolled_back(client, engine, world):
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO stack_deployments (id, project_id, target_id, host_id, repository, ref, "
            "commit_sha, status, rollback_of, created_by, finished_at) VALUES ('sd_rb', :p, "
            "'t_stack', 'h_ready', 'acme/shop', 'main', :sha, 'running', 'sd_failed', 'ceo', "
            "NULL)"), {"p": HOME, "sha": "77aa01b4e5f60718293a4b5c6d7e8f9012345678"})
    rows = {r["ref"]: r for r in client.get(
        "/v1/infra/deploys", params={"project_id": HOME, "viewer": "ceo"}).json()["rows"]}
    assert rows["stack:sd_rb"]["trigger"] == "rollback"
    assert rows["stack:sd_rb"]["rollback_of_sha7"] == "e1f2a3b"
    assert rows["stack:sd_rb"]["word"] == "Deploying"
    assert rows["stack:sd_failed"]["approver"] == {"actor": "you"}
    other = {r["ref"]: r for r in client.get(
        "/v1/infra/deploys", params={"project_id": HOME, "viewer": "someone"}).json()["rows"]}
    assert other["stack:sd_failed"]["approver"] == {"actor": "ceo"}
    assert rows["delivery:dr_live"]["trigger"] == "merge"
    assert rows["delivery:dr_slow"]["trigger"] == "workflow"
    assert rows["delivery:dr_live_old"]["word"] == "Done"

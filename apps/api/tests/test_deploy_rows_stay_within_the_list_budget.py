"""Every deploy row stays within the list budget (spec §6.3, §6.8): ≤ 2 KB each, a 30-row page
≤ 60 KB, and the list never carries `manifest`, `log_tail` or `preflight` (the drawer does)."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from apps.api.tests.infra_seed import clear, seed

HOME = "boss-dogfood"
FORBIDDEN = {"manifest", "manifest_text", "log_tail", "preflight", "release_packet", "log"}


@pytest.fixture()
def world(engine):
    seed(engine, HOME, samples=False)
    long_steps = json.dumps([{"step": f"s{i}", "label": "Install the dependencies and build "
                              f"the release bundle number {i} " + "x" * 60, "status": "ok",
                              "log": "y" * 4000} for i in range(40)])
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, "
            "environment, repo, target_kind, target_ref, commit_ref, status, trigger, steps, "
            "release_packet, created_by, created_at) SELECT 'dr_fat_' || g, :p, 't_live', "
            "'invoice', 'prod', 'acme/invoice', 'ssh', 'x', 'feature/' || repeat('z', 200), "
            "'succeeded', 'manual', CAST(:s AS JSONB), CAST(:rp AS JSONB), 'ceo', "
            "now() - make_interval(mins => g) FROM generate_series(1, 35) g"),
            {"p": HOME, "s": long_steps, "rp": json.dumps({"blob": "q" * 20000})})
        c.execute(text("UPDATE stack_deployments SET log_tail = repeat('L', 12000), "
                       "manifest = jsonb_build_object('blob', repeat('m', 20000))"))
    yield
    with engine.begin() as c:
        clear(c, HOME)


def _keys(value, out):
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(k)
            _keys(v, out)
    elif isinstance(value, list):
        for v in value:
            _keys(v, out)
    return out


def test_every_row_is_at_most_2kb_and_carries_no_bulk(client, world):
    response = client.get("/v1/infra/deploys", params={"project_id": HOME, "limit": 30})
    body = response.json()
    assert len(body["rows"]) == 30
    for row in body["rows"]:
        size = len(json.dumps(row).encode())
        assert size <= 2048, (row["ref"], size)
        assert not (_keys(row, set()) & FORBIDDEN), row["ref"]
        assert len(row["steps"]) <= 24
    assert len(response.content) <= 60 * 1024, len(response.content)


def test_the_target_drawer_recent_rows_obey_the_same_budget(client, world):
    body = client.get("/v1/infra/targets/t_live", params={"project_id": HOME}).json()
    assert len(body["recent"]) == 5
    for row in body["recent"]:
        assert len(json.dumps(row).encode()) <= 2048
        assert not (_keys(row, set()) & FORBIDDEN)


def test_the_drawer_has_the_log_and_holds_it_to_64kb(client, engine, world):
    with engine.begin() as c:
        c.execute(text("UPDATE stack_deployments SET log_tail = :l WHERE id = 'sd_failed'"),
                  {"l": "\n".join(f"line {i} " + "w" * 120 for i in range(1200))})
    body = client.get("/v1/infra/deploys/stack:sd_failed", params={"project_id": HOME}).json()
    assert body["log_tail"] and len(body["log_tail"].encode()) <= 64 * 1024
    assert body["log_tail"].rstrip().endswith("line 1199 " + "w" * 120)
    assert body["log_tail"].startswith("line "), "cut at a line boundary"
    assert body["failure"]["line"] == "Health check failed"
    assert body["plan"] is None or "blob" not in json.dumps(body["plan"])

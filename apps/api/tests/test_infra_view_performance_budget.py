"""Infra views meet their budget at size (spec §6.8, the Infra half). Marked `slow`: the final gate.

Seeds 50 machines, 100 app targets, 10 of them with 30 days of 120-second probes (216k samples),
300 deploys and 20 previews. Fleet, Deploys and Previews must answer within 150 ms p95 (asserted
with 3× slack); the Fleet body at 20 machines stays ≤ 64 KB and a Deploys page ≤ 60 KB.
"""
from __future__ import annotations

import statistics
import time

import pytest
from sqlalchemy import text

HOME = "boss-dogfood"
SLACK = 3
BUDGET_MS = {"fleet": 150, "deploys": 150, "previews": 150}

pytestmark = pytest.mark.slow


def _hosts(c, start: int, stop: int) -> None:
    c.execute(text(
        "INSERT INTO deployment_hosts (id, project_id, name, address, status, max_apps, "
        "last_preflight, last_preflight_at, created_by) "
        "SELECT 'hp_' || g, :p, 'perf-host-' || g, '198.51.100.' || g, "
        "  CASE WHEN g % 10 = 0 THEN 'degraded' WHEN g % 7 = 0 THEN 'registered' "
        "       ELSE 'verified' END, 8, "
        "  CASE WHEN g % 10 = 0 THEN '{\"reachable\": false, \"problems\": [\"timeout\"]}' "
        "       ELSE '{\"reachable\": true}' END::jsonb, now(), 'perf' "
        "FROM generate_series(CAST(:a AS int), CAST(:b AS int)) g"), {"p": HOME, "a": start, "b": stop - 1})
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
        "target_kind, target_ref, domains, host_id, status, created_by) "
        "SELECT 'tp_' || g || '_' || e, :p, 'perf-app-' || g, 'acme/perf-' || g, "
        "  CASE WHEN e = 0 THEN 'prod' ELSE 'dev' END, 'ssh', 'x', "
        "  jsonb_build_array('198.51.100.' || g || ':' || (8000 + e)), 'hp_' || g, 'active', 'perf' "
        "FROM generate_series(CAST(:a AS int), CAST(:b AS int)) g, generate_series(0, 1) e"),
        {"p": HOME, "a": start, "b": stop - 1})


@pytest.fixture()
def big(engine):
    with engine.begin() as c:
        _hosts(c, 1, 21)
    yield
    # conftest clean_tables empties the deployment tables before the next test.


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, int(round(0.95 * len(ordered))) - 1)]


def test_infra_views_meet_the_budget_at_size(client, engine, big):
    fleet20 = client.get("/v1/infra/fleet", params={"project_id": HOME})
    assert fleet20.status_code == 200, fleet20.text
    assert len(fleet20.content) <= 64 * 1024, len(fleet20.content)

    with engine.begin() as c:
        _hosts(c, 21, 51)
        # 10 targets × 30 days of 120 s probes = 216,000 samples.
        c.execute(text(
            "INSERT INTO deployment_health_samples (id, project_id, deployment_target_id, "
            "checked_at, ok, status_code, latency_ms) "
            "SELECT 'hsp_' || t || '_' || g, :p, 'tp_' || t || '_0', "
            "  now() - make_interval(secs => g * 120), (g % 97) <> 0, 200, 100 + (g % 50) "
            "FROM generate_series(1, 10) t, generate_series(0, 21599) g"), {"p": HOME})
        c.execute(text(
            "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, "
            "environment, repo, target_kind, target_ref, commit_ref, status, trigger, steps, "
            "created_by, created_at, started_at, finished_at) "
            "SELECT 'drp_' || g, :p, 'tp_' || (1 + g % 50) || '_0', 'perf-app', 'prod', 'acme/x', "
            "  'ssh', 'x', md5(g::text) || 'abcdefgh', "
            "  CASE WHEN g % 9 = 0 THEN 'failed' ELSE 'succeeded' END, 'manual', "
            "  '[{\"step\":\"a\",\"status\":\"ok\"},{\"step\":\"b\",\"status\":\"ok\"},"
            "    {\"step\":\"c\",\"status\":\"ok\"}]'::jsonb, 'ceo', "
            "  now() - make_interval(mins => g * 17), now() - make_interval(mins => g * 17), "
            "  now() - make_interval(mins => g * 17 - 2) FROM generate_series(1, 200) g"),
            {"p": HOME})
        c.execute(text(
            "INSERT INTO stack_deployments (id, project_id, target_id, host_id, repository, ref, "
            "commit_sha, status, steps, log_tail, created_by, created_at, apply_started_at, "
            "finished_at) SELECT 'sdp_' || g, :p, 'tp_' || (1 + g % 50) || '_1', "
            "  'hp_' || (1 + g % 50), 'acme/x', 'main', md5(g::text) || 'abcdefgh', "
            "  CASE WHEN g % 11 = 0 THEN 'failed' ELSE 'healthy' END, "
            "  '[{\"name\":\"prepare\",\"status\":\"ok\"}]'::jsonb, repeat('log ', 3000), 'ceo', "
            "  now() - make_interval(mins => g * 23), now() - make_interval(mins => g * 23), "
            "  now() - make_interval(mins => g * 23 - 3) FROM generate_series(1, 100) g"),
            {"p": HOME})
        c.execute(text(
            "INSERT INTO app_previews (id, project_id, run_id, repo, branch, port, url, status, "
            "created_at, expires_at) SELECT 'pvp_' || g, :p, 'run_' || g, 'acme/x', 'b' || g, "
            "  4100 + g, 'http://127.0.0.1:' || (4100 + g), "
            "  CASE WHEN g % 4 = 0 THEN 'failed' ELSE 'running' END, now(), now() + interval '1 hour' "
            "FROM generate_series(1, 20) g"), {"p": HOME})
        c.execute(text("ANALYZE deployment_health_samples"))
        c.execute(text("ANALYZE deployment_runs"))
        c.execute(text("ANALYZE stack_deployments"))

    timings: dict[str, list[float]] = {k: [] for k in BUDGET_MS}
    sizes: dict[str, int] = {}
    for name, path in (("fleet", "/v1/infra/fleet"), ("deploys", "/v1/infra/deploys"),
                       ("previews", "/v1/infra/previews")):
        client.get(path, params={"project_id": HOME})  # warm
        for _ in range(12):
            began = time.perf_counter()
            r = client.get(path, params={"project_id": HOME})
            timings[name].append((time.perf_counter() - began) * 1000)
            assert r.status_code == 200, r.text
            assert r.json()["partial"] == []
        sizes[name] = len(r.content)
    report = {k: (round(_p95(v), 1), round(statistics.median(v), 1)) for k, v in timings.items()}
    print("infra view p95/median ms:", report, "bytes:", sizes)
    for name, budget in BUDGET_MS.items():
        assert _p95(timings[name]) <= budget * SLACK, (name, report)
    assert sizes["deploys"] <= 60 * 1024, sizes

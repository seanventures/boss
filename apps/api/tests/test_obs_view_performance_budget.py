"""Observability's half of the performance budget (infra-observability spec §6.8). SLOW.

Seeds 10 targets × 30 days of 120 s samples (216,000 rows) and 200,000 llm_calls over 30
days, then asserts each view's server time with 3× slack: Apps ≤ 150 ms, Models 24 h ≤ 200 ms
and 30 d ≤ 400 ms (the raw aggregate, operator decision §11.4 — no rollup table unless this
misses), System ≤ 120 ms. Payloads: the Pulse ≤ 48 KB at 50 apps × 96 cells, Models ≤ 40 KB,
System ≤ 16 KB. The measured numbers are printed (`-s`) for the report.
"""
from __future__ import annotations

import gzip
import json
import statistics
import time

import pytest
from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.tests._obs_api_seed import second_project

pytestmark = pytest.mark.slow

SLACK = 3.0
BUDGET_MS = {"apps_24h": 150, "apps_30d": 150, "app_30d": 150, "models_24h": 200,
             "models_30d": 400, "system": 120}
PAYLOAD_KB = {"models": 40, "system": 16}
#: The Pulse's budget (§6.8) is 48 KB at 50 apps × 96 cells. With the frozen cell shape
#: `[ok, n, p95]` a populated cell is ~10 bytes of JSON, so 50 × 96 cells alone are ~48-53 KB
#: before any row field. The operator chose response compression over a shape change
#: (2026-10-04): the API gzips JSON, so the budget is on the bytes that cross the wire.
PULSE_KB = 48


def _llm_span() -> tuple[float, float]:
    """(newest, spacing) for 200,000 calls that all fall inside the 30-day window the view asks.

    The window is ALIGNED to its 8-hour cell (obs_view.window_bounds): it starts up to 8 h after
    `now - 30 days`. Seeded back from now by a flat 30 days, up to 2,222 of the oldest calls fell
    before its start, so `>= 199,000` passed or failed with the time of day. They are spread over
    the aligned window instead, one cell clear of its start so a boundary crossed while seeding
    (the window moving on by one cell) still holds every one of them.
    """
    from boss_engine.services.obs_view import window_bounds

    w = window_bounds("30d")
    newest = time.time() - 5
    oldest = w["start_epoch"] + w["cell_s"] + 60
    return newest, (newest - oldest) / 200_000


def _seed_big(engine, pid):
    newest, spacing = _llm_span()
    with begin(engine) as c:
        c.execute(text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
            " target_kind, target_ref, status, created_by) "
            "SELECT 't_perf_' || t, :p, 'app-' || t, 'acme/app', 'prod', 'ssh', 'x', 'active', "
            " 'test' FROM generate_series(0, 9) t"), {"p": pid})
        c.execute(text(
            "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, "
            " environment, repo, target_kind, target_ref, commit_ref, status, created_by, "
            " finished_at) "
            "SELECT 'dr_perf_' || t, :p, 't_perf_' || t, 'app-' || t, 'prod', 'acme/app', 'ssh', "
            " 'x', 'abcdef1234', 'succeeded', 'test', now() - interval '31 days' "
            "FROM generate_series(0, 9) t"), {"p": pid})
        c.execute(text(
            "INSERT INTO deployment_health_samples (id, project_id, deployment_target_id, "
            " checked_at, ok, status_code, latency_ms) "
            "SELECT 'dhs_perf_' || t || '_' || g, :p, 't_perf_' || t, "
            " now() - make_interval(secs => g * 120), (g % 97) <> 0, 200, 50 + (g * 37) % 900 "
            "FROM generate_series(0, 9) t, generate_series(0, 21599) g"), {"p": pid})
        c.execute(text(
            "INSERT INTO llm_calls (id, project_id, stage, engine, model, role, prompt_tokens, "
            " completion_tokens, total_tokens, cost_usd_micros, price_estimated, latency_ms, "
            " retries, ok, included_in_subscription, created_at) "
            "SELECT 'llmc_perf_' || g, :p, "
            " (ARRAY['implement','review_code','plan','spec','check_tests'])[1 + g % 5], "
            " (ARRAY['claude_code','native','guest:codex'])[1 + g % 3], "
            " (ARRAY['claude-sonnet-4-5','gpt-5-mini','claude-haiku','o4','qwen','m6','m7','m8'])"
            "   [1 + g % 8], "
            " (ARRAY['software_engineer','adversarial_reviewer','architect','qa'])[1 + g % 4], "
            " 100 + g % 5000, 10 + g % 700, 110 + g % 5700, (g * 13) % 40000, g % 501 = 0, "
            " 200 + (g * 7) % 60000, g % 4, g % 23 <> 0, g % 3 = 0, "
            " to_timestamp(:newest) - make_interval(secs => g * :spacing) "
            "FROM generate_series(0, 199999) g"), {"p": pid, "newest": newest, "spacing": spacing})
        c.execute(text("ANALYZE deployment_health_samples"))
        c.execute(text("ANALYZE llm_calls"))


def _seed_fifty(engine, pid):
    """50 apps, one probe in every one of the 96 cells: the widest Pulse body."""
    with begin(engine) as c:
        c.execute(text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
            " target_kind, target_ref, status, created_by, domains) "
            "SELECT 't_fifty_' || t, :p, 'service-name-' || t, 'acme/app', 'production', 'ssh', "
            " 'x', 'active', 'test', '[\"https://service.example.com\"]'::jsonb "
            "FROM generate_series(0, 49) t"), {"p": pid})
        c.execute(text(
            "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, "
            " environment, repo, target_kind, target_ref, commit_ref, status, created_by, "
            " finished_at) SELECT 'dr_fifty_' || t, :p, 't_fifty_' || t, 'a', 'production', "
            " 'acme/app', 'ssh', 'x', 'abcdef1234', 'succeeded', 'test', now() - interval '2 days' "
            "FROM generate_series(0, 49) t"), {"p": pid})
        c.execute(text(
            "INSERT INTO deployment_health_samples (id, project_id, deployment_target_id, "
            " checked_at, ok, status_code, latency_ms) "
            "SELECT 'dhs_fifty_' || t || '_' || g, :p, 't_fifty_' || t, "
            " now() - make_interval(secs => g * 900), g % 7 <> 0, 200, 1000 + g "
            "FROM generate_series(0, 49) t, generate_series(0, 95) g"), {"p": pid})


def _timed(client, url, params, n=7):
    client.get(url, params=params)                                   # warm
    took = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = client.get(url, params=params)
        took.append((time.perf_counter() - t0) * 1000)
        assert r.status_code == 200, r.text
    return statistics.median(took), r


def test_obs_views_fit_the_budget(client, engine, monkeypatch):
    from boss_engine.services import obs_view

    monkeypatch.setattr(obs_view, "_advice", lambda c, p: None)     # one-minute cache in prod
    pid = second_project(engine, "p_obs_perf")
    fifty = second_project(engine, "p_obs_fifty")
    t0 = time.perf_counter()
    _seed_big(engine, pid)
    _seed_fifty(engine, fifty)
    seed_s = time.perf_counter() - t0

    measured, sizes = {}, {}
    p = {"project_id": pid}
    measured["apps_24h"], _ = _timed(client, "/v1/observability/apps", p)
    measured["apps_30d"], _ = _timed(client, "/v1/observability/apps", {**p, "win": "30d"})
    measured["app_30d"], _ = _timed(client, "/v1/observability/apps/t_perf_3", {**p, "win": "30d"})
    measured["models_24h"], m = _timed(client, "/v1/observability/models", p)
    measured["models_30d"], m30 = _timed(client, "/v1/observability/models",
                                         {**p, "win": "30d", "group": "model"})
    measured["system"], s = _timed(client, "/v1/observability/system", p)
    _, pulse = _timed(client, "/v1/observability/apps", {"project_id": fifty}, n=1)
    sizes["pulse_50"] = len(pulse.content) / 1024
    sizes["pulse_50_gzip"] = len(gzip.compress(pulse.content)) / 1024
    sizes["models"] = max(len(m.content), len(m30.content)) / 1024
    sizes["system"] = len(s.content) / 1024

    assert m30.json()["totals"]["calls"] >= 199_000
    assert len(pulse.json()["rows"]) == 50
    assert all(len(r["strip"]) == 96 for r in pulse.json()["rows"])
    print("\nOBS PERF seed_s=%.1f " % seed_s
          + json.dumps({k: round(v, 1) for k, v in measured.items()})
          + " KB " + json.dumps({k: round(v, 1) for k, v in sizes.items()}))
    for key, budget in BUDGET_MS.items():
        assert measured[key] <= budget * SLACK, f"{key}: {measured[key]:.0f} ms > {budget}×3"
    for key, kb in PAYLOAD_KB.items():
        assert sizes[key] <= kb, f"{key}: {sizes[key]:.1f} KB > {kb} KB"


def test_the_pulse_fits_48kb_at_fifty_apps(client, engine):
    fifty = second_project(engine, "p_obs_fifty")
    _seed_fifty(engine, fifty)
    r = client.get("/v1/observability/apps", params={"project_id": fifty},
                   headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert r.num_bytes_downloaded / 1024 <= PULSE_KB, f"{r.num_bytes_downloaded / 1024:.1f} KB on the wire"


def test_the_seeded_calls_fall_inside_the_aligned_window_at_any_time_of_day(monkeypatch):
    """The worst instant: just after an 8-hour boundary, when the aligned 30-day window starts
    almost 8 hours after `now - 30 days`. Seeding back a flat 30 days put ~2,200 calls before
    it; the span is taken from the window itself, so every call is inside."""
    from datetime import datetime, timezone

    from boss_engine.services import obs_view

    cell = obs_view.WINDOWS["30d"][0] // obs_view.WINDOWS["30d"][1]
    for offset in (1, cell // 2, cell - 1):
        instant = (1_790_000_000 // cell) * cell + offset
        monkeypatch.setattr(obs_view, "_now",
                            lambda i=instant: datetime.fromtimestamp(i, timezone.utc))
        monkeypatch.setattr(time, "time", lambda i=instant: float(i))
        newest, spacing = _llm_span()
        start = obs_view.window_bounds("30d")["start_epoch"]
        assert newest - 199_999 * spacing >= start
        assert newest <= instant

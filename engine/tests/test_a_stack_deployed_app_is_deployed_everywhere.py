"""A stack-deployed app is deployed, in Infra, Observability and the sweep alike.

Before: the one DEPLOYED rule counted only `deployment_runs` that succeeded, so an app shipped
through a stack deploy (a VM, a manifest, an approval) read "Not deployed" in Observability while
Infra showed it live, and the sweep never probed it — there was no health connector for a stack
target at all (operator decision 2026-10-04). Now a `healthy` stack deployment counts, the
newest live deploy may be either kind, and a stack target is probed over HTTP at its public URL,
never over SSH.
"""
from __future__ import annotations

import http.server
import threading
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project, sample
from boss_engine.config import get_settings
from boss_engine.services import deployment_health, obs_view


def _stack_target(c, pid: str, tid: str, app: str, url: str) -> None:
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        " target_ref, status, created_by, domains, public_url) "
        "VALUES (:t, :p, :a, 'acme/app', 'prod', 'stack', :a, 'active', 'test', '[]'::jsonb, :u)"),
        {"t": tid, "p": pid, "a": app, "u": url})


def _stack_deploy(c, pid: str, tid: str, status: str, sha: str, ago_min: int) -> str:
    sid = f"sd_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, commit_sha, "
        " manifest, manifest_text, status, steps, log_tail, preflight, created_by, created_at, "
        " updated_at, finished_at) VALUES (:id, :p, :t, 'acme/app', 'main', :sha, '{}'::jsonb, '', "
        " :st, '[]'::jsonb, '', '{}'::jsonb, 'test', now() - make_interval(mins => :m), "
        " now() - make_interval(mins => :m), now() - make_interval(mins => :m))"),
        {"id": sid, "p": pid, "t": tid, "sha": sha, "st": status, "m": ago_min})
    return sid


@pytest.fixture(scope="module")
def app_server():
    class Ok(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):  # quiet
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Ok)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


@pytest.fixture(scope="module")
def seeded(app_server):
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
        _stack_target(c, pid, f"{pid}_t_stack", "invoice", app_server)
        _stack_deploy(c, pid, f"{pid}_t_stack", "healthy", "a1b2c3d4e5f6a7b8", 90)
        _stack_deploy(c, pid, f"{pid}_t_stack", "failed", "ffff00001111", 30)   # a later failure
        _stack_target(c, pid, f"{pid}_t_planned", "billing", app_server)
        _stack_deploy(c, pid, f"{pid}_t_planned", "awaiting_approval", "bbbb22223333", 5)
        now = c.execute(text("SELECT now()")).scalar()
        for i in range(4):
            sample(c, pid, f"{pid}_t_stack", now - timedelta(minutes=10 * i), True, 120)
    return eng, pid


def test_the_one_rule_counts_a_healthy_stack_deploy(seeded):
    eng, pid = seeded
    with eng.connect() as c:
        live = deployment_health.newest_deploys(c, [f"{pid}_t_stack", f"{pid}_t_planned"])
        found = c.execute(text(
            "SELECT t.id FROM deployment_targets t WHERE t.project_id = :p AND "
            + deployment_health.DEPLOYED_PREDICATE.format(alias="t")), {"p": pid}).scalars().all()
    assert set(live) == {f"{pid}_t_stack"}, "a stack deploy awaiting approval is not live"
    assert live[f"{pid}_t_stack"]["kind"] == "stack"
    assert live[f"{pid}_t_stack"]["commit_ref"] == "a1b2c3d4e5f6a7b8", "the newest HEALTHY deploy"
    assert found == [f"{pid}_t_stack"]


def test_observability_and_the_health_view_agree_it_is_up(seeded):
    eng, pid = seeded
    with eng.connect() as c:
        pulse = obs_view.pulse(c, get_settings(), pid, win="24h")
        view = deployment_health.health_view(c, pid)
    rows = {r["target_id"]: r for r in pulse["rows"]}
    assert rows[f"{pid}_t_stack"]["state"] == "up"
    assert rows[f"{pid}_t_planned"]["state"] == "not_deployed"
    by = {t["target_id"]: t for t in view["targets"]}
    assert by[f"{pid}_t_stack"]["status"] == "up"
    assert by[f"{pid}_t_stack"]["deployed_commit"] == "a1b2c3d4e5f6a7b8"


def test_a_stack_target_is_probed_over_http_at_its_public_url(seeded, app_server):
    eng, pid = seeded
    out = obs_view.probe_app(eng, get_settings(), pid, f"{pid}_t_stack")
    assert out["word"] == "Up"
    with eng.connect() as c:
        newest = c.execute(text(
            "SELECT ok, status_code FROM deployment_health_samples WHERE deployment_target_id = :t "
            "ORDER BY checked_at DESC LIMIT 1"), {"t": f"{pid}_t_stack"}).mappings().first()
    assert newest["ok"] is True and newest["status_code"] == 200


def test_the_stack_probe_says_unreachable_without_ssh():
    sample_ = deployment_health.probe(
        {"target_kind": "stack", "public_url": "http://127.0.0.1:1/"}, get_settings())
    assert sample_["ok"] is False and sample_["error"]

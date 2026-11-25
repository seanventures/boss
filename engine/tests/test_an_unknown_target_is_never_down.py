"""The sweep records what it asked, and nothing when it could ask nothing.

A config_dir target has no deployment connector. The sweep wrote "no connector for target_kind
'config_dir'" as a FAILED sample every two minutes, so the project header read "Down", the
health alert fired and the incident squad started a recovery for an app whose declared /readyz
answered 200 the whole time.

Now: a connector-less target is probed through its declared checks (and recovers on the first
good sample); one with no declared check is unknown — no sample, no alert, no incident — and
the failure samples the old non-probe wrote are withdrawn.
"""
from __future__ import annotations

import http.server
import json
import threading
from datetime import timedelta

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project, sample, target
from boss_engine.config import get_settings
from boss_engine.services import deployment_health
from boss_engine.services.project_experience.health import target_states

BOGUS = "no connector for target_kind 'config_dir'"


@pytest.fixture(scope="module")
def api_only():
    class Api(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200 if self.path == "/readyz" else 404)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture(scope="module")
def seeded(api_only):
    eng = pg_engine()
    url = f"http://127.0.0.1:{api_only}/readyz"
    with eng.begin() as c:
        pid = project(c)
        declared = target(c, pid, f"{pid}_cfg", "gateway-config")
        bare = target(c, pid, f"{pid}_bare", "gateway-bare")
        c.execute(text("UPDATE deployment_targets SET target_kind = 'config_dir', "
                       "domains = CAST(:d AS jsonb), health_checks = CAST(:h AS jsonb) "
                       "WHERE id = :t"),
                  {"t": declared, "d": json.dumps([url]),
                   "h": json.dumps([{"url": url, "expected_status": 200}])})
        c.execute(text("UPDATE deployment_targets SET target_kind = 'config_dir', "
                       "domains = '[]'::jsonb, health_checks = '[]'::jsonb WHERE id = :t"),
                  {"t": bare})
        now = c.execute(text("SELECT now()")).scalar()
        for i in range(1, 6):          # what the old sweep wrote: "Down"
            for tid in (declared, bare):
                sample(c, pid, tid, now - timedelta(minutes=2 * i), False, None, BOGUS)
    return eng, pid, declared, bare


def _states(eng, ids):
    with eng.connect() as c:
        return target_states(c, ids)


def test_before_the_sweep_both_read_down(seeded):
    eng, _pid, declared, bare = seeded
    states = _states(eng, [declared, bare])
    assert states[declared]["health"] == "down"
    assert states[bare]["health"] == "down"


def test_the_sweep_asks_the_declared_check_and_drops_the_non_samples(seeded):
    eng, pid, declared, bare = seeded
    out = deployment_health.sweep(eng, get_settings(), project_id=pid, interval_seconds=0)
    assert out.get("unknown") == 1, out
    with eng.connect() as c:
        newest = c.execute(text(
            "SELECT ok, status_code FROM deployment_health_samples WHERE deployment_target_id = :t "
            "ORDER BY checked_at DESC LIMIT 1"), {"t": declared}).mappings().first()
        bare_left = c.execute(text(
            "SELECT count(*) FROM deployment_health_samples WHERE deployment_target_id = :t"),
            {"t": bare}).scalar()
    assert newest["ok"] is True and newest["status_code"] == 200
    assert bare_left == 0, "an unknown target keeps no failure samples and gains none"


def test_after_the_sweep_neither_reads_down(seeded):
    eng, _pid, declared, bare = seeded
    states = _states(eng, [declared, bare])
    assert states[declared]["health"] != "down", "one good sample ends the outage"
    assert states[bare]["health"] == "unknown", "no check, no connector: unknown, not down"

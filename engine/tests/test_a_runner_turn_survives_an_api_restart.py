"""BOSSAPP-828: a coding-agent turn owned by boss-runner survives an API restart and is RE-ATTACHED.

In process, no docker: the real runner (apps/runner — sqlite outbox + journal, HTTP server), the
real dispatch seam (services/runner_dispatch), the real registry (services/runner_registry) on a
throwaway Postgres, and a stand-in API that does what the real route does with a push: verify the
DPoP proof against the runner-bound token, record it, answer the cumulative ack.

    start -> events -> BOSS drains (the waiting stage detaches; the turn keeps going) ->
    the API is away (pushes fail, the outbox holds) -> the reclaim path asks the registry: alive,
    so the job is handed back without spending an attempt -> the stage runs again in a NEW sandbox
    -> the same turn is re-attached (no second process) -> events resume from the acked seq ->
    the result is the original turn's, its working tree carried into the new sandbox.

Needs BOSS_RUNNER_TEST_DATABASE_URL naming a disposable database (e.g. boss_test_828).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.runner.boss_runner.daemon import Runner
from apps.runner.boss_runner.server import RunnerServer
from apps.runner.tests._support import Issuer, ReplayDb, verify_runner_call
from boss_engine.identity import jwks
from boss_engine.identity.delegation import TYP_RUN
from boss_engine.services import jobs as jobs_mod
from boss_engine.services import owned_process, run_liveness, runner_dispatch, runner_registry

ROOT = Path(__file__).resolve().parents[2]
DB_ENV = "BOSS_RUNNER_TEST_DATABASE_URL"
TASK = "crun_828i"
RUN = f"run_ct:{TASK}"


@pytest.fixture(scope="module")
def engine():
    url = os.environ.get(DB_ENV)
    if not url and os.environ.get(DB_ENV + "_FILE"):
        url = Path(os.environ[DB_ENV + "_FILE"]).read_text().strip()
    if not url:
        pytest.skip(f"{DB_ENV} not set (a throwaway Postgres)")
    if "test" not in url.rsplit("/", 1)[-1]:
        raise RuntimeError(f"{DB_ENV} must name a disposable database")
    from sqlalchemy import create_engine, text

    # A schema of its own: nothing else in the database is read or written.
    with create_engine(url).begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS runner828 CASCADE"))
        c.execute(text("CREATE SCHEMA runner828"))
    eng = create_engine(url, connect_args={"options": "-c search_path=runner828"})
    block = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8").split("-- BOSSAPP-828", 1)[1]
    with eng.begin() as c:
        from boss_engine.db import split_statements

        for stmt in split_statements(block):
            c.execute(text(stmt))
        c.execute(text("CREATE TABLE jobs (id TEXT PRIMARY KEY, project_id TEXT, kind TEXT, payload JSONB, "
                       "status TEXT, attempt INT, max_attempts INT, lease_owner TEXT, lease_token_hash TEXT, "
                       "lease_expires_at TIMESTAMPTZ, heartbeat_at TIMESTAMPTZ, run_after TIMESTAMPTZ, "
                       "updated_at TIMESTAMPTZ)"))
    yield eng
    eng.dispose()
    with create_engine(url).begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS runner828 CASCADE"))


class FakeApi:
    """The API's push route, minus FastAPI: DPoP check, registry write, cumulative ack."""

    def __init__(self, engine, issuer: Issuer):
        self.engine, self.issuer, self.replay = engine, issuer, ReplayDb()
        self.up = True
        self.batches: list[list[int]] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if not api.up:
                    status, answer = 503, {"error": "restarting"}
                else:
                    token = verify_runner_call(dict(self.headers), method="POST", url=api.url + self.path,
                                               db=api.replay)
                    _, claims = jwks.verify(token, typ=TYP_RUN, key_for_kid=api.issuer.key_for_kid)
                    sid = self.path.split("/")[4]
                    api.batches.append([e["seq"] for e in body["events"]])
                    with api.engine.begin() as c:
                        out = runner_registry.record_push(
                            c, session_id=sid, identity_run_id=claims["boss_task"]["run"],
                            runner_id=body["runner_id"], state=body["state"], alive=body["alive"],
                            events=body["events"])
                    status, answer = (200, {"ack": out["ack"]}) if out else (404, {"error": "unknown_session"})
                raw = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _git_checkout(path: Path, base: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(base), str(path)], check=True)
    return path


AGENT = r"""
import os, sys, time
open(os.environ["PIDS"], "a").write(f"{os.getpid()}\n")
print("step 1: read the repo", flush=True)
while not os.path.exists(os.environ["GO"]):
    time.sleep(0.05)
print("step 2: wrote the fix", flush=True)
open("fix.py", "w").write("FIXED = True\n")
os.remove("old.txt")
open(sys.argv[1], "w").write("summary: fixed it")
print("done", flush=True)
"""


def _wait(pred, timeout=20.0, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        value = pred()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def test_a_turn_survives_an_api_restart_and_is_re_attached(engine, tmp_path, monkeypatch):
    from sqlalchemy import text

    issuer = Issuer()
    api = FakeApi(engine, issuer)
    runner = Runner(tmp_path / "runner", api_url=api.url, key_for_kid=issuer.key_for_kid, interval=0.1)
    server = RunnerServer(runner, host="127.0.0.1", port=0).start()
    runner.start()
    jkt = runner.key.jkt
    owner = owned_process.Owner(kind="code_run", id=TASK)

    def handoff(url):
        return runner_dispatch.Handoff(runner_dispatch.RunnerClient(url), owner=owner, task_run_id=TASK,
                                       identity_run_id=RUN, mint=lambda: issuer.mint(run_id=RUN, jkt=jkt),
                                       api_base=api.url, engine=engine)

    monkeypatch.setenv(runner_dispatch.RUNNER_URL_ENV, server.url)
    monkeypatch.setattr(runner_dispatch, "HANDOFF_FACTORY", handoff)
    monkeypatch.setattr(runner_dispatch, "POLL_SECONDS", 0.05)
    draining = {"on": False}
    monkeypatch.setattr(owned_process, "draining", lambda: draining["on"])

    base = tmp_path / "origin"
    base.mkdir()
    (base / "old.txt").write_text("stale\n")
    subprocess.run(["git", "init", "-q", str(base)], check=True)
    subprocess.run(["git", "-C", str(base), "add", "."], check=True)
    subprocess.run(["git", "-C", str(base), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
                   check=True)
    pids, go = tmp_path / "pids.txt", tmp_path / "go"
    env = {"PATH": os.environ.get("PATH", ""), "PIDS": str(pids), "GO": str(go)}
    script = tmp_path / "agent.py"
    script.write_text(AGENT)

    def turn(sandbox: Path, final: Path):
        return runner_dispatch.run([sys.executable, str(script), str(final)], cwd=str(sandbox), input=None,
                                   capture_output=True, text=True, timeout=120, start_new_session=True, env=env,
                                   session_key="implement-turn", collect_files=[str(final)])

    try:
        # ---- API #1: the stage hands the turn to the runner and waits -------------------------
        sandbox1 = _git_checkout(tmp_path / "sandbox1", base)
        first: dict = {}

        def stage_one():
            try:
                first["result"] = turn(sandbox1, tmp_path / "final1.txt")
            except BaseException as exc:  # noqa: BLE001
                first["error"] = exc

        t1 = threading.Thread(target=stage_one)
        t1.start()

        def registry():
            with engine.begin() as c:
                return c.execute(text("SELECT * FROM runner_sessions WHERE run_id = :r"),
                                 {"r": TASK}).mappings().first()

        _wait(lambda: (registry() or {}).get("seq", 0) >= 2, what="the first events to be acked")
        sid = registry()["session_id"]

        # ---- BOSS drains for an update: the stage detaches, the turn keeps running ------------
        draining["on"] = True
        t1.join(timeout=10)
        assert isinstance(first.get("error"), runner_dispatch.RunnerDetached)
        assert runner_dispatch.retained(str(sandbox1))       # teardown would keep this checkout
        acked_before = registry()["seq"]

        # ---- the API is away: pushes fail, the turn finishes, the outbox holds ----------------
        api.up = False
        go.write_text("go")
        _wait(lambda: runner.store.session(sid)["state"] == "exited", what="the turn to finish")
        held = runner.store.pending(sid)
        assert held and held[0]["seq"] == acked_before + 1
        assert held[-1]["type"] == "exited"

        # ---- the reclaim path asks the registry before declaring the run dead ----------------
        with engine.begin() as c:
            c.execute(text("UPDATE runner_sessions SET state = 'running', alive = true WHERE session_id = :s"),
                      {"s": sid})
            assert run_liveness.runner_alive(c, TASK) is True
            c.execute(text("INSERT INTO jobs VALUES ('job_828', 'prj_1', 'code.task', CAST(:p AS jsonb), "
                           "'running', 3, 3, 'api-1', 'h', :exp, now(), now(), now())"),
                      {"p": json.dumps({"run_id": TASK}), "exp": datetime.now(timezone.utc) - timedelta(hours=2)})
            events: list = []
            monkeypatch.setattr(jobs_mod, "emit_event", lambda *a, **k: events.append(a))
            row = c.execute(text("SELECT id, project_id, kind FROM jobs WHERE id = 'job_828'")).mappings().first()
            assert jobs_mod.JobLeaseService._readopt_runner_turn(SimpleNamespace(), c, row, actor="reaper",
                                                                 silent=120) is True
            job = c.execute(text("SELECT status, attempt FROM jobs WHERE id = 'job_828'")).mappings().first()
            assert (job["status"], job["attempt"]) == ("queued", 2)      # no attempt spent
            assert events and events[0][2] == "job.readopted_runner"

        # ---- API #2 boots; the stage runs again in a NEW sandbox and re-attaches --------------
        runner_dispatch._RETAINED.clear()                    # a new process: nothing in memory
        draining["on"] = False
        api.up = True
        sandbox2 = _git_checkout(tmp_path / "sandbox2", base)
        out = turn(sandbox2, tmp_path / "final2.txt")

        assert out.returncode == 0
        assert "step 1: read the repo" in out.stdout and "done" in out.stdout     # the whole turn
        assert len(pids.read_text().split()) == 1                                 # ONE process, ever
        assert (tmp_path / "final2.txt").read_text() == "summary: fixed it"
        assert (sandbox2 / "fix.py").read_text() == "FIXED = True\n"              # carried over
        assert not (sandbox2 / "old.txt").exists()
        assert not sandbox1.exists()
        # The stream resumed from the acked seq: the first batch after the restart started there.
        after = [b for b in api.batches if b and b[0] > acked_before]
        assert after and after[0][0] == acked_before + 1
        with engine.begin() as c:
            final = c.execute(text("SELECT seq, state FROM runner_sessions WHERE session_id = :s"),
                              {"s": sid}).mappings().first()
            assert final["state"] == "consumed"
            assert run_liveness.runner_alive(c, TASK) is False
        assert runner.store.session(sid)["consumed"] == 1
    finally:
        server.stop()
        runner.stop()
        api.stop()


def test_nothing_alive_means_the_usual_reclaim(engine):
    from sqlalchemy import text

    with engine.begin() as c:
        c.execute(text("DELETE FROM runner_sessions"))
        c.execute(text("DELETE FROM jobs"))
        c.execute(text("INSERT INTO jobs (id, project_id, kind, payload, status, attempt, max_attempts) VALUES "
                       "('job_a', 'p', 'code.task', '{\"run_id\": \"crun_dead\"}', 'running', 1, 3), "
                       "('job_b', 'p', 'deploy.app', '{\"run_id\": \"crun_live\"}', 'running', 1, 3)"))
        runner_registry.open_session(c, session_id="rs_live", run_id="crun_live", identity_run_id="run_ct:crun_live",
                                     runner_id="rnr_1", session_key="k")
        runner_registry.open_session(c, session_id="rs_stale", run_id="crun_dead", identity_run_id="run_ct:crun_dead",
                                     runner_id="rnr_1", session_key="k")
        c.execute(text("UPDATE runner_sessions SET last_heartbeat = now() - interval '10 minutes' "
                       "WHERE session_id = 'rs_stale'"))
        assert run_liveness.runner_alive(c, "crun_dead") is False           # silent too long
        assert run_liveness.runner_alive(c, "crun_live") is True
        for job in ("job_a", "job_b"):                                      # stale / not a code task
            row = c.execute(text("SELECT id, project_id, kind FROM jobs WHERE id = :j"), {"j": job}).mappings().first()
            assert jobs_mod.JobLeaseService._readopt_runner_turn(SimpleNamespace(), c, row, actor="t", silent=1) is False


def test_a_push_for_another_runs_session_is_refused_and_the_ack_is_contiguous(engine):
    from sqlalchemy import text

    with engine.begin() as c:
        c.execute(text("DELETE FROM runner_sessions"))
        runner_registry.open_session(c, session_id="rs_x", run_id="crun_x", identity_run_id="run_ct:crun_x",
                                     runner_id="rnr_1", session_key="k")
        assert runner_registry.record_push(c, session_id="rs_x", identity_run_id="run_ct:other", runner_id="r",
                                           state="running", alive=True, events=[]) is None
        ev = lambda *seqs: [{"seq": s, "type": "stdout", "data": {}} for s in seqs]  # noqa: E731
        assert runner_registry.record_push(c, session_id="rs_x", identity_run_id="run_ct:crun_x", runner_id="r",
                                           state="running", alive=True, events=ev(1, 2, 4))["ack"] == 2
        assert runner_registry.record_push(c, session_id="rs_x", identity_run_id="run_ct:crun_x", runner_id="r",
                                           state="running", alive=True, events=ev(3, 4, 5))["ack"] == 5
        assert runner_registry.record_push(c, session_id="rs_x", identity_run_id="run_ct:crun_x", runner_id="r",
                                           state="running", alive=True, events=ev(2))["ack"] == 5
        lost = runner_registry.record_push(c, session_id="rs_x", identity_run_id="run_ct:crun_x", runner_id="r",
                                           state="lost", alive=False,
                                           events=[{"seq": 6, "type": "exited", "data": {"lost": True}}])
        assert lost == {"ack": 6, "state": "lost", "run_id": "crun_x"}

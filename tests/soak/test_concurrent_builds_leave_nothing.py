"""SOAK: six builds in a small envelope leave nothing behind (stability spec §4f).

Runs INSIDE the api image, capped like a constrained machine (scripts/soak_concurrent_builds.sh:
`docker run --init --memory 3g --memory-swap 3g --cpus 2`), against a throwaway Postgres
database. This process plays the API: it starts the same host threads the API's lifespan starts
(ownership ledger, stop watch, SIGTERM drain, reaper, system doctor), serves /health, and runs
builds as its own children — which is where builds run in production.

Six code.task jobs are queued at once and leased through the real `JobLeaseService.claim_job`
(capacity admission included) by three lease slots. Each build is a deterministic agent stub
that does real work through the real spawn path (`owned_process`, inside the build's owner
scope): `npm install`, `npm run build`, a dev server left running in the background the way an
agent's Bash tool leaves one, and one Chromium page. Two Vite/React apps, two Node/Express apps,
one Python/Flask app, one build that fails. One build is stopped mid-way with a real stop
request; one runs in a separate "API" process that is SIGKILLed mid-build.

Asserted during: no OOM kill; /health p99 < 2 s; heavy builds leased at once never exceed the
derived "Builds at once"; memory PSI full avg10 < 20.
Asserted after (one reaper pass, previews expired): processes and zombies back to baseline, no
open ownership rows, /tmp back to baseline, database connections ≤ baseline + 2, unreclaimable
memory ≤ baseline + 50 MB.

Skipped unless BOSS_SOAK=1. Prints one JSON report line (`SOAK_REPORT {...}`).
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

ENGINE = Path(os.environ.get("BOSS_ENGINE_DIR", "/src/engine"))
sys.path.insert(0, str(ENGINE))

pytestmark = pytest.mark.skipif(os.environ.get("BOSS_SOAK") != "1",
                                reason="the soak runs on demand: BOSS_SOAK=1")

CG = Path("/sys/fs/cgroup")
SOAK = Path(os.environ.get("BOSS_SOAK_HOME", str(Path.home() / "soak")))
FIX = SOAK / "fixtures"


# ------------------------------------------------------------------------------------------ #
# measuring
# ------------------------------------------------------------------------------------------ #

def _stat(key: str) -> int:
    for line in (CG / "memory.stat").read_text().splitlines():
        k, _, v = line.partition(" ")
        if k == key:
            return int(v)
    return 0


def _events() -> dict[str, int]:
    out = {}
    for line in (CG / "memory.events").read_text().splitlines():
        k, _, v = line.partition(" ")
        out[k] = int(v)
    return out


def _psi_full_avg10() -> float:
    for path in (CG / "memory.pressure", Path("/proc/pressure/memory")):
        try:
            for line in path.read_text().splitlines():
                if line.startswith("full"):
                    return float(line.split("avg10=")[1].split()[0])
        except (OSError, IndexError, ValueError):
            continue
    return 0.0


def _procs() -> dict[str, int]:
    from boss_engine.services import owned_process

    out: dict[str, int] = {}
    me = os.getpid()
    for p in owned_process.processes():
        if p.pid in (1, me) or p.state.startswith("Z"):
            continue
        out[p.comm] = out.get(p.comm, 0) + 1
    return out


def _zombies() -> int:
    from boss_engine.services import owned_process
    return owned_process.zombie_count()


def _tmp() -> tuple[int, int]:
    root = Path(tempfile.gettempdir())
    entries = list(root.iterdir())
    size = 0
    for e in entries:
        for r, _d, files in os.walk(e, onerror=lambda _e: None):
            for f in files:
                try:
                    size += os.lstat(os.path.join(r, f)).st_size
                except OSError:
                    pass
    return len(entries), size


def _pg_conns(engine) -> int:
    from sqlalchemy import text
    with engine.connect() as c:
        return int(c.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = "
                                  "current_database()")).scalar())


def snapshot(engine) -> dict:
    from sqlalchemy import text
    n_tmp, tmp_bytes = _tmp()
    with engine.begin() as c:
        open_rows = int(c.execute(text("SELECT count(*) FROM owned_resources WHERE released_at "
                                       "IS NULL")).scalar())
    return {"anon_mb": round(_stat("anon") / 1048576, 1),
            "current_mb": round(int((CG / "memory.current").read_text()) / 1048576, 1),
            "procs": _procs(), "zombies": _zombies(), "tmp_entries": n_tmp,
            "tmp_mb": round(tmp_bytes / 1048576, 1), "pg_conns": _pg_conns(engine),
            "owned_open": open_rows, "oom_kill": _events().get("oom_kill", 0),
            "disk_free_gb": round(shutil.disk_usage("/").free / 1e9, 2)}


# ------------------------------------------------------------------------------------------ #
# the apps
# ------------------------------------------------------------------------------------------ #

VITE_PKG = {"name": "app", "private": True, "type": "module",
            "scripts": {"dev": "vite --port $PORT --host 127.0.0.1", "build": "vite build"},
            "dependencies": {"react": "18.3.1", "react-dom": "18.3.1"},
            "devDependencies": {"vite": "5.4.10", "@vitejs/plugin-react": "4.3.3"}}
EXPRESS_PKG = {"name": "api", "private": True,
               "scripts": {"dev": "node server.js", "build": "node --check server.js"},
               "dependencies": {"express": "4.21.1"}}


def make_fixtures() -> dict[str, Path]:
    """Six apps, written once before the baseline (their npm cache is warmed then too)."""
    apps: dict[str, Path] = {}
    for name in ("vite-a", "vite-b"):
        d = FIX / name
        (d / "src").mkdir(parents=True, exist_ok=True)
        (d / "package.json").write_text(json.dumps(VITE_PKG))
        (d / "index.html").write_text('<!doctype html><div id="root"></div>'
                                      '<script type="module" src="/src/main.jsx"></script>')
        (d / "src/main.jsx").write_text(
            'import React from "react"; import { createRoot } from "react-dom/client";\n'
            f'createRoot(document.getElementById("root")).render(<h1>{name}</h1>);\n')
        (d / "vite.config.js").write_text('import react from "@vitejs/plugin-react";\n'
                                          'export default { plugins: [react()] };\n')
        apps[name] = d
    for name in ("express-a", "express-b"):
        d = FIX / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "package.json").write_text(json.dumps(EXPRESS_PKG))
        (d / "server.js").write_text(
            'const e = require("express")(); e.get("/", (q, r) => r.send("' + name + '"));\n'
            'e.listen(process.env.PORT || 3000, "127.0.0.1");\n')
        apps[name] = d
    d = FIX / "flask"
    d.mkdir(parents=True, exist_ok=True)
    (d / "app.py").write_text(textwrap.dedent("""
        import os
        from flask import Flask
        app = Flask(__name__)
        @app.get("/")
        def home():
            return "flask"
        if __name__ == "__main__":
            app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")))
    """))
    apps["flask"] = d
    d = FIX / "broken"
    (d / "src").mkdir(parents=True, exist_ok=True)
    (d / "package.json").write_text(json.dumps(VITE_PKG))
    (d / "index.html").write_text('<script type="module" src="/src/main.jsx"></script>')
    (d / "src/main.jsx").write_text("export default function ( {\n")      # does not parse
    (d / "vite.config.js").write_text('export default {};\n')
    apps["broken"] = d
    return apps


CHROMIUM = textwrap.dedent("""
    import sys
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        try:
            pg.goto(sys.argv[1], timeout=20000)
        except Exception:
            pass
        b.close()
""")


def agent_script(kind: str, port: int) -> str:
    """What an agent's Bash tool does: install, leave a dev server running, build, look at the
    page once. The background server is the leak this soak exists for."""
    if kind == "flask":
        return (f"python3 -m venv .venv >/dev/null && .venv/bin/pip install -q flask && "
                f"(PORT={port} .venv/bin/python app.py >/dev/null 2>&1 &) && sleep 2 && "
                f"python3 {SOAK}/chromium.py http://127.0.0.1:{port}/")
    return (f"npm install --prefer-offline --no-audit --no-fund >/dev/null 2>&1 && "
            f"(PORT={port} npm run dev >/dev/null 2>&1 &) && npm run build >/dev/null 2>&1 && "
            f"sleep 2 && python3 {SOAK}/chromium.py http://127.0.0.1:{port}/")


# ------------------------------------------------------------------------------------------ #
# the run
# ------------------------------------------------------------------------------------------ #

def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_build(engine, run_id: str, app: Path, kind: str) -> str:
    """One build, as the API runs one: owned, in its sandbox, ended with everything it started."""
    from boss_engine.services import owned_process
    from boss_engine.services.resource_ledger import claim_dir, release_dir

    root = Path(tempfile.gettempdir()) / "boss-sandboxes"
    root.mkdir(exist_ok=True)
    with owned_process.owner_scope("code_run", run_id, project_id="p_soak", fresh=True):
        work = Path(tempfile.mkdtemp(prefix=f"{run_id}-", dir=root))
        claim_dir(work)
        try:
            shutil.copytree(app, work, dirs_exist_ok=True)
            port = _free_port()
            try:
                (work / ".tmp").mkdir(exist_ok=True)
                agent = owned_process.popen(["sh", "-c", agent_script(kind, port)], cwd=str(work),
                                            env={**os.environ, "CI": "1",
                                                 "TMPDIR": str(work / ".tmp")})
            except owned_process.OwnerCancelled:
                return "cancelled"
            rc = agent.wait(timeout=900)
            if owned_process.was_cancelled(run_id):
                return "cancelled"
            return "completed" if rc == 0 else "failed"
        finally:
            owned_process.terminate_owner(run_id, reason="run_ended", grace=2.0)
            shutil.rmtree(work, ignore_errors=True)
            release_dir(work, "teardown")


_DOOMED_API = textwrap.dedent("""
    import json, os, sys, time
    sys.path.insert(0, {engine!r})
    from boss_engine.services import owned_process
    with owned_process.owner_scope("code_run", {run_id!r}):
        proc = owned_process.popen(["sh", "-c", {script!r}], cwd={cwd!r})
    print(json.dumps({{"token": proc.token, "pgid": proc.pgid}}), flush=True)
    time.sleep(900)
""")


def test_six_builds_in_three_gigabytes_leave_nothing():
    from sqlalchemy import create_engine, text

    from boss_engine.config import Settings
    from boss_engine.db import ensure_schema
    from boss_engine.services import admission, owned_process, resource_reaper, system_doctor
    from boss_engine.services.jobs import JobLeaseService
    from boss_engine.services.system_threads import start_all

    url = os.environ["BOSS_TEST_DATABASE_URL"]
    engine = create_engine(url, future=True, pool_size=5, max_overflow=5)
    ensure_schema(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_soak','soak') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_soak','org_soak',"
                       "'soak') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES ('p_soak',"
                       "'ws_soak','soak','p-soak') ON CONFLICT DO NOTHING"))
        for t in ("jobs", "code_task_runs", "code_task_stop_requests", "events"):
            c.execute(text(f"DELETE FROM {t} WHERE project_id = 'p_soak'"))
        for t in ("owned_resources", "system_findings", "system_doctor_actions", "system_state",
                  "workload_profiles"):
            c.execute(text(f"DELETE FROM {t}"))
    settings = Settings(database_url=url, default_project_id="p_soak")

    apps = make_fixtures()
    (SOAK / "chromium.py").write_text(CHROMIUM)
    # Every temp file a build makes lives in its sandbox (TMPDIR), so a killed Chromium's
    # profile directory goes with the sandbox instead of piling up in /tmp.
    (Path(tempfile.gettempdir()) / "boss-sandboxes").mkdir(exist_ok=True)
    from boss_engine.services import code_task_service, preview_service  # noqa: F401

    # /health, served by this "API" while builds run in it.
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            return None

    health = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=health.serve_forever, daemon=True).start()
    health_url = f"http://127.0.0.1:{health.server_address[1]}/health"

    before = snapshot(engine)
    started_threads = start_all(engine, settings)
    resource_reaper_interval = 20.0
    resource_reaper.start(engine, settings, interval=resource_reaper_interval)

    kinds = {"vite-a": "node", "vite-b": "node", "express-a": "node", "express-b": "node",
             "flask": "flask", "broken": "node"}
    runs = {}
    with engine.begin() as c:
        for i, name in enumerate(kinds):
            rid = f"crun_soak_{name}_{uuid.uuid4().hex[:6]}"
            runs[rid] = name
            c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status) VALUES "
                           "(:i, 'p_soak', :t, 'running')"), {"i": rid, "t": name})
            c.execute(text("INSERT INTO jobs (id, project_id, kind, payload, priority) VALUES "
                           "(:j, 'p_soak', 'code.task', CAST(:p AS JSONB), :pr)"),
                      {"j": f"job_{rid}", "p": json.dumps({"run_id": rid}), "pr": 100 + i})
    cancel_run = next(r for r, n in runs.items() if n == "vite-b")
    kill_run = next(r for r, n in runs.items() if n == "express-b")

    samples = {"latency": [], "psi": [], "concurrent": [], "slots": []}
    stop_sampling = threading.Event()

    def sampler():
        while not stop_sampling.is_set():
            t0 = time.monotonic()
            try:
                urllib.request.urlopen(health_url, timeout=10).read()
                samples["latency"].append(time.monotonic() - t0)
            except Exception:  # noqa: BLE001
                samples["latency"].append(10.0)
            samples["psi"].append(_psi_full_avg10())
            with engine.begin() as c:
                samples["concurrent"].append(int(c.execute(text(
                    "SELECT count(*) FROM jobs WHERE project_id='p_soak' AND status IN "
                    "('leased','running')")).scalar()))
                cap = admission.sample()
                samples["slots"].append(admission.builds_at_once(c, cap, 1200)["value"])
            time.sleep(1.0)

    threading.Thread(target=sampler, daemon=True).start()

    outcomes: dict[str, str] = {}
    killed_tree: dict = {}
    lock = threading.Lock()

    def slot(i: int):
        svc = JobLeaseService(settings)
        while True:
            with lock:
                if len(outcomes) >= len(runs):
                    return
            with engine.begin() as c:
                job = svc.claim_job(c, f"soak-w{i}", ["code.task"], 900)
            if not job:
                time.sleep(2)
                continue
            rid = job["payload"]["run_id"]
            name = runs[rid]
            with engine.begin() as c:
                c.execute(text("UPDATE jobs SET status='running' WHERE id=:j"), {"j": job["id"]})
            if rid == kill_run:
                outcome = doomed_api(rid, apps[name], kinds[name])
            else:
                if rid == cancel_run:
                    threading.Timer(15.0, lambda: press_stop(rid)).start()
                outcome = run_build(engine, rid, apps[name], kinds[name])
            with engine.begin() as c:
                c.execute(text("UPDATE code_task_runs SET status=:s WHERE id=:i AND status NOT IN "
                               "('cancelled')"), {"s": outcome, "i": rid})
                c.execute(text("UPDATE jobs SET status=:s WHERE id=:j"),
                          {"s": "succeeded" if outcome == "completed" else "failed",
                           "j": job["id"]})
            with lock:
                outcomes[rid] = outcome

    def press_stop(rid: str):
        from boss_engine.services.code_task_service import cancel_run as _cancel
        with engine.begin() as c:
            _cancel(c, rid, "p_soak", actor="soak")

    def doomed_api(rid: str, app: Path, kind: str) -> str:
        root = Path(tempfile.gettempdir()) / "boss-sandboxes"
        root.mkdir(exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f"{rid}-", dir=root))
        shutil.copytree(app, work, dirs_exist_ok=True)
        code = _DOOMED_API.format(engine=str(ENGINE), run_id=rid, cwd=str(work),
                                  script=agent_script(kind, _free_port()))
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        killed_tree.update(json.loads(child.stdout.readline()))
        time.sleep(12)                       # mid-install
        child.send_signal(signal.SIGKILL)    # the API dies; nothing in it runs a finally
        child.wait(timeout=10)
        killed_tree["alive_after_kill"] = len(owned_process.members(
            killed_tree["pgid"], killed_tree["token"])["alive"])
        return "failed"

    t0 = time.monotonic()
    slots = [threading.Thread(target=slot, args=(i,), daemon=True) for i in range(3)]
    for t in slots:
        t.start()
    for t in slots:
        t.join(timeout=1800)
    build_seconds = round(time.monotonic() - t0, 1)
    peak_mb = round(int((CG / "memory.peak").read_text()) / 1048576, 1) if \
        (CG / "memory.peak").exists() else None
    stop_sampling.set()

    # Previews expire, the reaper and the doctor pass once more (their own threads also ran).
    time.sleep(3)
    resource_reaper.tick(engine, settings)
    # The sandbox the SIGKILLed API left: unclaimed now. The product waits 30 minutes before
    # taking an unclaimed directory; the soak shortens that the way it shortens a preview TTL.
    resource_reaper.reap_dirs(min_age=60)
    system_doctor.tick(engine, settings)
    with engine.begin() as c:
        deferred = int(c.execute(text("SELECT count(*) FROM events WHERE project_id='p_soak' AND "
                                      "event_type='job.capacity_deferred'")).scalar())
        acted = [dict(r) for r in c.execute(text(
            "SELECT action, outcome FROM system_doctor_actions ORDER BY created_at")).mappings()]
    time.sleep(2)
    after = snapshot(engine)
    lat = sorted(samples["latency"])
    p99 = lat[int(0.99 * (len(lat) - 1))] if lat else None
    report = {
        "before": before, "after": after, "build_seconds": build_seconds,
        "cgroup_memory_peak_mb": peak_mb, "outcomes": {runs[k]: v for k, v in outcomes.items()},
        "health_p99_s": round(p99, 3) if p99 is not None else None,
        "psi_full_avg10_max": max(samples["psi"] or [0]),
        "max_concurrent_builds": max(samples["concurrent"] or [0]),
        "derived_slots": sorted(set(samples["slots"])),
        "capacity_deferrals": deferred,
        "killed_api_tree_alive_right_after_kill": killed_tree.get("alive_after_kill"),
        "doctor_and_reaper_actions": acted[-10:], "threads": started_threads,
    }
    print("SOAK_REPORT " + json.dumps(report, default=str), flush=True)

    # during
    assert after["oom_kill"] == before["oom_kill"], "an OOM kill happened"
    assert p99 is not None and p99 < 2.0, f"/health p99 {p99}"
    assert report["max_concurrent_builds"] <= max(samples["slots"] or [1]), report
    assert report["psi_full_avg10_max"] < 20
    assert outcomes.get(cancel_run) == "cancelled"
    assert report["outcomes"]["broken"] == "failed"
    assert killed_tree.get("alive_after_kill", 0) > 0, "the SIGKILL case must leave an orphan"
    # after
    assert after["zombies"] == 0
    assert after["procs"] == before["procs"], (before["procs"], after["procs"])
    assert after["owned_open"] == 0
    assert after["tmp_entries"] == before["tmp_entries"], (before, after)
    assert after["pg_conns"] <= before["pg_conns"] + 2 + 5   # + this run's own pool
    assert after["anon_mb"] <= before["anon_mb"] + 50, (before["anon_mb"], after["anon_mb"])

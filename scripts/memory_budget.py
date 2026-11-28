"""The API's memory budget, checked before a release (BOSSAPP-1506).

The laptop's API reached 5.2 GB in 18 h. Every call of the console's overview and of several
ticks read every build's saved files, and glibc kept each thread's high-water mark. Unit tests
hold the two causes (apps/api/tests/test_a_run_list_never_reads_the_build_files.py,
engine/tests/test_freed_memory_goes_back_to_the_machine.py). This is the whole-process check: the
REAL API app on uvicorn (one event loop, the thread pool, glibc, like an install), against a
scratch database seeded with install-shaped builds, driven by the console's and the worker's
request mix for N minutes. It fails when the resident set grows past a bound.

Run it in a Linux container of the API image (scripts/memory-budget.sh does), against a scratch
database: it WRITES builds into it.

    python scripts/memory_budget.py [--minutes 10] [--callers 4] [--growth-mb 200] [--seed-runs 150]

Exit 0 and one PASS line, or exit 1 with the samples.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import string
import sys
import threading
import time

os.environ.setdefault("BOSS_EDITION", "community")
os.environ.setdefault("BOSS_AUTHZ_MODE", "shadow")
os.environ.setdefault("BOSS_AUTONOMY_IMMEDIATE", "0")
os.environ.setdefault("BOSS_INSTALL_TIER", "dev")
os.environ.setdefault("BOSS_IDENTITY_SERVER_KEY",
                      "dGVzdC1vbmx5LWlkZW50aXR5LXNlcnZlci1rZXktLS0tLS0tLS0tLS0tLS0=")

PROJECT = "boss-dogfood"
#: What the console and the worker send, as the laptop's logs show it (apps/worker control_plane).
MIX = [
    ("get", f"/v1/code-tasks?project_id={PROJECT}&limit=200&exclude_internal=true", None),
    ("get", f"/v1/console/v2/overview?project_id={PROJECT}", None),
    ("post", "/v1/worker/poll", {"worker_id": "budget-1", "capabilities": ["x.none"], "lease_ttl_seconds": 60}),
    ("post", "/v1/delivery/health/sweep", None),
    ("post", "/v1/servers/tick", None),
    ("post", "/v1/monitoring/tick", None),
    ("get", "/v1/system/health", None),
    ("post", "/v1/knowledge/ingest/tick", {"sweep": False}),
    ("post", "/v1/agent-processes/tick-all", {"project_id": PROJECT, "actor": "system"}),
    ("post", "/v1/factory/flow/tick", {"project_id": PROJECT, "actor": "system"}),
    ("post", "/v1/ambient/tick", {}),
    ("post", "/v1/metatron/watch", {"project_id": PROJECT, "actor": "system"}),
    ("post", "/v1/shepherd/watch", {}),
    ("post", "/v1/bots/tick", {"project_id": PROJECT}),
]


def rss_mb() -> int:
    with open("/proc/self/status", encoding="ascii") as status:
        for line in status:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    raise RuntimeError("no /proc/self/status: run this on Linux")


def seed(runs: int) -> None:
    """Builds shaped like the laptop install's: ~330 KB of saved files each."""
    from sqlalchemy import text

    from apps.api.app.bootstrap import seed as bootstrap
    from boss_engine.db import ensure_schema, get_engine
    from apps.api.tests.db_guard import assert_safe_to_wipe
    from boss_engine.dev.test_databases import guard_test_url

    assert_safe_to_wipe(os.environ.get("DATABASE_URL", ""))   # a test-named database only
    guard_test_url(variable="DATABASE_URL")                    # and never in an install's server
    engine = get_engine()
    ensure_schema(engine)
    bootstrap()
    rnd = random.Random(7)
    body = "".join(rnd.choice(string.ascii_letters + " \n{}();") for _ in range(3000))
    with engine.begin() as c:
        have = c.execute(text("SELECT count(*) FROM code_task_runs WHERE id LIKE 'ctr_budget%'")).scalar()
        for i in range(int(have or 0), runs):
            ctx = {"saved_workspace": {"after": f"{i:040x}", "file_count": 100,
                                       "files": [{"path": f"src/f{j}.tsx", "content": body} for j in range(100)]},
                   "pending_diff": [{"path": "src/a.tsx", "content": body}] * 5,
                   "app_id": f"app{i % 12}"}
            c.execute(text(
                "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, finalize_ctx) "
                "VALUES (:i, :p, :s, 'done', :t, CAST(:sp AS jsonb), CAST(:st AS jsonb), CAST(:f AS jsonb))"),
                {"i": f"ctr_budget{i:05d}", "p": PROJECT, "s": ("blocked", "failed", "completed")[i % 3],
                 "t": f"build {i}", "sp": json.dumps({"repo": "r", "instructions": body[:2000]}),
                 "st": json.dumps({"log": body}), "f": json.dumps(ctx)})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--callers", type=int, default=4)
    ap.add_argument("--growth-mb", type=int, default=200,
                    help="fail when RSS after warm-up grows by more than this")
    ap.add_argument("--seed-runs", type=int, default=150)
    ap.add_argument("--port", type=int, default=18079)
    args = ap.parse_args(argv)

    seed(args.seed_runs)
    import httpx
    import uvicorn

    from apps.api.app.main import app
    from boss_engine.services import memory_trim

    memory_trim.start()                              # as the API's lifespan does
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, lifespan="off",
                                           log_level="warning", access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)
    failures: list[str] = []
    stop = time.monotonic() + args.minutes * 60

    def caller(mix: list) -> None:
        with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=300) as c:
            while time.monotonic() < stop:
                for method, path, body in mix:
                    r = c.get(path) if method == "get" else c.post(path, json=body)
                    if r.status_code >= 500:
                        failures.append(f"{path} {r.status_code}")
                time.sleep(random.uniform(0, 12))    # pool threads retire after 10 s idle
    # ONE worker sends the ticks (as an install's worker does: they are not built to race each
    # other); the other callers are console tabs polling the screens' reads.
    console = [call for call in MIX if call[0] == "get"]
    callers = [threading.Thread(target=caller, args=(MIX if i == 0 else console,), daemon=True)
               for i in range(args.callers)]
    for t in callers:
        t.start()
    samples: list[tuple[float, int]] = []
    began = time.monotonic()
    while any(t.is_alive() for t in callers):
        samples.append((round((time.monotonic() - began) / 60, 1), rss_mb()))
        time.sleep(15)
    time.sleep(70)                                   # one trim pass after the last call
    samples.append((round((time.monotonic() - began) / 60, 1), rss_mb()))
    warm = samples[min(len(samples) - 1, max(1, len(samples) // 5))][1]
    peak, last = max(s[1] for s in samples), samples[-1][1]
    verdict = last - warm <= args.growth_mb and not failures
    print(f"{'PASS' if verdict else 'FAIL'} api memory: warm {warm} MB, peak {peak} MB, settled {last} MB "
          f"(growth {last - warm:+d} MB, bound {args.growth_mb}) over {args.minutes:g} min, "
          f"{args.callers} callers, {len(failures)} 5xx")
    if not verdict:
        print("samples (min, MB):", samples)
        print("5xx:", failures[:20])
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())

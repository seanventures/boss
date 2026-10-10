"""The console performance walk: what a page load costs, measured on a seeded local stack.

    scripts/e2e.sh up                       # a stack (BOSS_E2E_SERVICE_ENV may add keys)
    scripts/e2e.sh perf --seed              # seed, sign in as the Owner, measure, check budgets

What it measures, signed in as the Owner exactly as the browser is (the session cookie):
  * each API GET the Factory and Command Center lean on, N times, p50/p95, with the API's own
    Server-Timing split (total, db + statement count, identity's share);
  * each console page (Factory, Overview, Identity): time to first byte, time to the last byte of
    the streamed HTML, its size — and, from the API's per-request log (BOSS_METRICS_LOG), how many
    API requests that ONE render made, and how many of them were the same request twice.

Budgets (PAGE_BUDGETS, API_P95_MS) fail the walk; `--no-budget` only reports. The API must be
started with BOSS_METRICS_LOG and BOSS_SERVER_TIMING (restart_api does that).
"""
from __future__ import annotations

import http.client
import json
import os
import signal
import statistics
import subprocess
import sys
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import REPO_ROOT, teardown
from .stack import StackState, service_env, wait_http

PROJECT = "boss-dogfood"
ORG = "org_boss"

API_ENDPOINTS = [
    f"/v1/factory/flow?project_id={PROJECT}",
    f"/v1/factory/apps?project_id={PROJECT}",
    f"/v1/code-tasks?project_id={PROJECT}",
    "/v1/identity/me",
    "/v1/identity/me/projects",
    "/v1/identity/me/permissions",
    "/v1/orgs",
    "/v1/projects",
    f"/v1/meetings?project_id={PROJECT}",
]

PAGES = {
    "factory": f"/?org={ORG}&area=code_tasks",
    "overview": f"/?org={ORG}&area=overview",
    "identity": "/identity",
}

#: The budgets a render must stay under, on the seeded local stack. API calls are what ONE server
#: render asks the API for (the browser's own calls after hydration are not in it).
#: Measured cold (no reuse) on the seeded stack, 2026-10-09: Factory 17 calls (rc6: 38),
#: Overview 12 (rc6: 34), Identity 13. The headroom is for a feed a new panel genuinely needs.
PAGE_BUDGETS = {
    "factory": {"full_ms": 1200, "api_calls": 22, "duplicate_calls": 0},
    "overview": {"full_ms": 1200, "api_calls": 16, "duplicate_calls": 0},
    "identity": {"full_ms": 1200, "api_calls": 15, "duplicate_calls": 0},
}
API_P95_MS = 80.0


# ---- processes -------------------------------------------------------------------------------------

def _kill(pid: Optional[int]) -> None:
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(50):
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def restart_api(state: StackState, *, root: Path = REPO_ROOT, log: Path,
                extra: Optional[dict[str, str]] = None) -> None:
    """The API again, from `root` (another checkout: rc5, rc6), logging every request to `log`."""
    _kill(state.pids.get("api"))
    env = service_env(state, extra={"BOSS_METRICS_LOG": str(log), "BOSS_SERVER_TIMING": "1",
                                    **(extra or {})})
    env["PYTHONPATH"] = os.pathsep.join([str(root), str(root / "engine")])
    handle = open(Path(state.run_dir) / "logs" / "api.log", "ab")
    proc = subprocess.Popen([sys.executable, "-m", "apps.api.serve", "--host", "127.0.0.1",
                             "--port", str(state.api_port)], cwd=str(root), env=env,
                            stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    state.pids["api"] = proc.pid
    state.fingerprints["api"] = teardown.fingerprint(proc.pid) or ""
    state.save()
    wait_http(f"http://127.0.0.1:{state.api_port}/health", timeout=240, what="API", proc=proc)


def restart_web(state: StackState, *, root: Path = REPO_ROOT, build: bool = True) -> None:
    """The console again, from `root`'s apps/web (built there first unless `build=False`)."""
    _kill(state.pids.get("web"))
    web_dir = root / "apps" / "web"
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
    env.update({
        "BOSS_API_URL": f"http://127.0.0.1:{state.api_port}",
        "NEXT_PUBLIC_BOSS_API_URL": f"http://127.0.0.1:{state.api_port}",
        "NEXT_PUBLIC_BOSS_PROJECT_ID": PROJECT,
        "BOSS_API_KEY": state.owner_key,
        "BOSS_CONSOLE_PASSWORD": state.console_password,
        "BOSS_CONSOLE_SESSION_SECRET": state.session_secret,
        "NEXT_TELEMETRY_DISABLED": "1",
        "BOSS_E2E_RUN_ID": state.run_id,
    })
    next_bin = web_dir / "node_modules" / ".bin" / "next"
    logs = Path(state.run_dir) / "logs"
    if build:
        subprocess.run([str(next_bin), "build"], cwd=str(web_dir), env=env, check=True,
                       stdout=open(logs / "web-build.log", "ab"), stderr=subprocess.STDOUT)
    proc = subprocess.Popen([str(next_bin), "start", "-p", str(state.web_port), "-H", "127.0.0.1"],
                            cwd=str(web_dir), env=env, stdout=open(logs / "web.log", "ab"),
                            stderr=subprocess.STDOUT, start_new_session=True)
    state.pids["web"] = proc.pid
    state.fingerprints["web"] = teardown.fingerprint(proc.pid) or ""
    state.save()
    wait_http(f"http://127.0.0.1:{state.web_port}/login", timeout=300, what="console", proc=proc)


# ---- signing in --------------------------------------------------------------------------------------

def owner_cookie(state: StackState) -> str:
    """The Owner's identity cookies, signed in through the console's /login (Playwright)."""
    from playwright.sync_api import sync_playwright

    from .identity import load_accounts, sign_in

    owner = load_accounts(Path(state.accounts_file))["owner"]
    origin = f"http://127.0.0.1:{state.web_port}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        sign_in(page, origin, owner, next_path="/identity")
        cookies = page.context.cookies()
        browser.close()
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def legacy_console_cookie(state: StackState) -> str:
    """rc5 and before: the shared console password (POST /api/auth/login)."""
    conn = http.client.HTTPConnection("127.0.0.1", state.web_port, timeout=30)
    body = urllib.parse.urlencode({"password": state.console_password, "next": "/"})
    conn.request("POST", "/api/auth/login", body=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": f"http://127.0.0.1:{state.web_port}"})
    res = conn.getresponse()
    res.read()
    cookies = [v.split(";", 1)[0] for k, v in res.getheaders() if k.lower() == "set-cookie"]
    if not cookies:
        raise RuntimeError(f"console sign-in refused ({res.status})")
    return "; ".join(cookies)


# ---- measuring ---------------------------------------------------------------------------------------

@dataclass
class Timed:
    status: int
    ttfb_ms: float
    full_ms: float
    size: int
    server_timing: str = ""


def timed_get(port: int, path: str, headers: dict[str, str]) -> Timed:
    t0 = time.perf_counter()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    conn.request("GET", path, headers={"Accept-Encoding": "identity", **headers})
    res = conn.getresponse()
    ttfb = time.perf_counter() - t0
    size = 0
    while True:
        chunk = res.read(65536)
        if not chunk:
            break
        size += len(chunk)
    full = time.perf_counter() - t0
    conn.close()
    return Timed(res.status, ttfb * 1000, full * 1000, size, res.getheader("server-timing") or "")


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, int(round(p / 100 * (len(ordered) - 1)))))
    return ordered[k]


def _parse_timing(header: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for part in header.split(","):
        bits = [b.strip() for b in part.split(";")]
        if not bits or not bits[0]:
            continue
        name = bits[0]
        for b in bits[1:]:
            if b.startswith("dur="):
                out[f"{name}_ms"] = float(b[4:])
            elif b.startswith("desc="):
                out[f"{name}_desc"] = b[5:].strip('"')
    return out


def measure_api(state: StackState, headers: dict[str, str], *, n: int = 30,
                endpoints: list[str] = API_ENDPOINTS) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for path in endpoints:
        timed_get(state.api_port, path, headers)          # warm
        runs = [timed_get(state.api_port, path, headers) for _ in range(n)]
        if runs[-1].status >= 400:
            report[path] = {"status": runs[-1].status}
            continue
        ms = [r.full_ms for r in runs]
        timing = _parse_timing(runs[-1].server_timing)
        report[path] = {"status": runs[-1].status, "p50_ms": round(statistics.median(ms), 1),
                        "p95_ms": round(pct(ms, 95), 1), "bytes": runs[-1].size, **timing}
    return report


def _log_lines(log: Path, start: int) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    with open(log, "rb") as fh:
        fh.seek(start)
        return [json.loads(line) for line in fh.read().decode("utf-8", "replace").splitlines() if line.strip()]


def measure_page(state: StackState, path: str, headers: dict[str, str], log: Path, *,
                 n: int = 5, cred: str = "session", gap: float = 0.3) -> dict[str, Any]:
    timed_get(state.web_port, path, headers)              # warm (compile, first-hit caches)
    runs: list[Timed] = []
    calls: list[list[dict[str, Any]]] = []
    for _ in range(n):
        time.sleep(gap)                                   # let the previous render's stragglers land
        start = log.stat().st_size if log.exists() else 0
        runs.append(timed_get(state.web_port, path, headers))
        time.sleep(0.3)
        # Only the signed-in person's own calls: the worker polls the same API meanwhile.
        calls.append([c for c in _log_lines(log, start) if c.get("cred") == cred])
    last = calls[-1]
    keys = Counter(f"{c['method']} {c['path']}?{c['query']}" for c in last)
    dup = {k: v for k, v in keys.items() if v > 1}
    api_ms = [c["ms"] for c in last]
    return {
        "status": runs[-1].status,
        "ttfb_p50_ms": round(statistics.median(r.ttfb_ms for r in runs), 1),
        "full_p50_ms": round(statistics.median(r.full_ms for r in runs), 1),
        "full_max_ms": round(max(r.full_ms for r in runs), 1),
        "bytes": runs[-1].size,
        "api_calls": len(last),
        "duplicate_calls": sum(v - 1 for v in dup.values()),
        "duplicates": dup,
        "api_queries": sum(c["queries"] for c in last),
        "api_auth_queries": sum(c["auth_queries"] for c in last),
        "api_auth_ms_sum": round(sum(c["auth_ms"] for c in last), 1),
        "calls": [f"{c['method']} {c['path']} {c['ms']}ms q={c['queries']} auth={c['auth_ms']}ms/{c['auth_queries']}q" for c in last],
        "api_ms_sum": round(sum(api_ms), 1),
        "api_ms_max": round(max(api_ms), 1) if api_ms else 0,
        "slowest": sorted(({"path": c["path"], "ms": c["ms"], "q": c["queries"]} for c in last),
                          key=lambda c: -c["ms"])[:6],
    }


def check_budgets(report: dict[str, Any]) -> list[str]:
    failures = []
    for name, budget in PAGE_BUDGETS.items():
        got = report.get("pages", {}).get(name)
        if not got:
            continue
        for key, limit in budget.items():
            if got.get(key, 0) > limit:
                failures.append(f"{name}: {key} {got.get(key)} > budget {limit}")
    for path, got in report.get("api", {}).items():
        if got.get("p95_ms", 0) > API_P95_MS:
            failures.append(f"{path}: p95 {got['p95_ms']} ms > budget {API_P95_MS} ms")
    return failures


def run(state: StackState, *, label: str, legacy: bool = False, n_api: int = 30,
        n_page: int = 5, pages: Optional[dict[str, str]] = None, gap: float = 0.3) -> dict[str, Any]:
    log = Path(state.run_dir) / "api-requests.jsonl"
    cookie = legacy_console_cookie(state) if legacy else owner_cookie(state)
    api_headers = ({"Authorization": f"Bearer {state.owner_key}"} if legacy else {"Cookie": cookie})
    report: dict[str, Any] = {"label": label, "api": {}, "pages": {}}
    endpoints = [e for e in API_ENDPOINTS if not (legacy and e.startswith("/v1/identity"))]
    report["api"] = measure_api(state, api_headers, n=n_api, endpoints=endpoints)
    for name, path in (pages or PAGES).items():
        if legacy and name == "identity":
            continue
        report["pages"][name] = measure_page(state, path, {"Cookie": cookie}, log, n=n_page,
                                             cred="bearer" if legacy else "session", gap=gap)
    return report


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="perf")
    parser.add_argument("--label", default="HEAD")
    parser.add_argument("--legacy", action="store_true", help="rc5: console password + API key")
    parser.add_argument("--root", default=str(REPO_ROOT), help="checkout to run the API/console from")
    parser.add_argument("--restart-api", action="store_true")
    parser.add_argument("--restart-web", action="store_true")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--no-budget", action="store_true")
    parser.add_argument("--seed", action="store_true",
                        help="write the realistic volume (perf_seed.py) into the stack's database first")
    parser.add_argument("--n-api", type=int, default=30)
    parser.add_argument("--n-page", type=int, default=5)
    parser.add_argument("--out", default="")
    parser.add_argument("--gap", type=float, default=6.0,
                        help="seconds between page loads (6+ defeats a 5 s feed cache: a cold load)")
    args = parser.parse_args(argv)
    state = StackState.load()
    if args.seed:
        from .perf_seed import seed

        print(json.dumps({"seeded": seed(state.database_url)}))
    root = Path(args.root)
    log = Path(state.run_dir) / "api-requests.jsonl"
    if args.restart_api or not log.exists():
        restart_api(state, root=root, log=log)
    if args.restart_web:
        restart_web(state, root=root, build=not args.no_build)
    report = run(state, label=args.label, legacy=args.legacy, n_api=args.n_api, n_page=args.n_page,
                 gap=args.gap)
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
    if args.no_budget:
        return 0
    failures = check_budgets(report)
    for f in failures:
        print(f"BUDGET: {f}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""THE F4 SCAN PROOF on a real compose stack (APP_TEMPLATES_AND_SECURITY_LOOP.md §4; stream S4).

    python3 tests/e2e/security/scan_proof.py [--keep] [--out DIR]

Builds the api/worker/egress images with BOSS_TOOLCHAINS=security under its own compose project
and image tag (`bosss4`; never boss-community-*), boots db + api + worker + egress, seeds, and
registers the deliberately vulnerable Node/Express + SQLite app (tests/e2e/security/vulnapp) as
a build's local workspace in the api container. Then, THROUGH THE API, as the install's owner key:

  1. POST /v1/security/scans {semgrep, osv, zap baseline on the app started in the sandbox};
     the worker leases `security.scan`, the API runs the tools as a sandbox uid (OSV reaches
     api.osv.dev through boss-egress). Expected: Semgrep finds the SQL injection and the hardcoded
     JWT secret, OSV the lodash advisories, ZAP the missing security headers.
  2. The fix (vulnapp-fixed) is committed over it, and the app is scanned again. Expected: those
     findings are gone, and what remains carries the SAME fingerprints (lines moved).
  3. GET /v1/security/reports/<app>: the planted flaws are `fixed`, nothing is `new`.

Only one e2e stack may run at a time on the operator's Mac: it refuses to start if another is up.
`down -v` always runs (unless --keep). Never prints a secret: the env file is 0600 in --out.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
COMPOSE = ROOT / "scripts" / "install" / "community-compose.yml"
PROJECT = "bosss4"
PORTS = {"BOSS_API_PORT": "58400", "BOSS_WEB_PORT": "53400", "BOSS_POSTGRES_PORT": "55840",
         "BOSS_PREVIEW_PORT": "58401"}
OTHER_STACKS = ("bossrc", "idvid", "bosss2", "bosss5", "bosss6", "bossfxd")
APP_KEY = "s4-vulnapp"
RUN_ID = "ctr_s4proof0001"
WS = f"/var/lib/boss/{RUN_ID}-ws"

SQLI = "javascript.express.security.injection.tainted-sql-string"
JWT = "javascript.jsonwebtoken.security.hardcoded-jwt-secret"
HEADERS = {"10038-1", "10020-1", "10021", "10037"}       # CSP, anti-clickjacking, nosniff, X-Powered-By


def sh(argv: list[str], *, timeout: int = 600, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=input)
    if check and done.returncode != 0:
        raise RuntimeError(f"{' '.join(argv[:6])} failed ({done.returncode}): {done.stderr.strip()[-800:]}")
    return done


class Stack:
    def __init__(self, out: Path):
        self.out = out
        self.env_file = out / f"{PROJECT}.env"
        self.key = secrets.token_urlsafe(32)
        self.api = f"http://127.0.0.1:{PORTS['BOSS_API_PORT']}"

    def base(self) -> list[str]:
        return ["docker", "compose", "-p", PROJECT, "--env-file", str(self.env_file), "-f", str(COMPOSE)]

    def compose(self, *args: str, timeout: int = 900, check: bool = True, input: str | None = None):
        return sh([*self.base(), *args], timeout=timeout, check=check, input=input)

    def write_env(self) -> None:
        worker = secrets.token_urlsafe(32)
        values = {
            "BOSS_EDITION": "community", "BOSS_REQUIRE_API_AUTH": "true",
            "BOSS_API_KEY": self.key, "BOSS_WORKER_API_KEY": worker,
            "BOSS_API_KEYS": f"{self.key},{worker}",
            "BOSS_API_KEY_PRINCIPALS": f"{self.key}:ceo,{worker}:system",
            "BOSS_WORKER_PRINCIPAL": "system",
            "BOSS_DB_PASSWORD": secrets.token_hex(16),
            "BOSS_CONSOLE_PASSWORD": secrets.token_hex(24),
            "BOSS_CONSOLE_SESSION_SECRET": secrets.token_hex(32),
            # the bundled Infisical is not started, but compose interpolates its required keys
            "INFISICAL_ENCRYPTION_KEY": secrets.token_hex(16), "INFISICAL_AUTH_SECRET": secrets.token_urlsafe(32),
            "BOSS_COMMUNITY_IMAGE_TAG": PROJECT, "BOSS_TOOLCHAINS": "security",
            "BOSS_SOURCE_SHA": sh(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).stdout.strip(),
            **PORTS,
        }
        self.env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        os.chmod(self.env_file, 0o600)

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.api + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw[:1] in "{[" else raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    def exec_api(self, script: str, *, user: str = "boss") -> str:
        return self.compose("exec", "-T", "-u", user, "api", "sh", "-c", script, timeout=300).stdout


def other_stacks_up() -> list[str]:
    names = sh(["docker", "ps", "--format", "{{.Names}}"]).stdout.split()
    return [n for n in names if n.startswith(OTHER_STACKS) or (n.startswith("bosss") and not n.startswith(PROJECT))]


def wait_health(stack: Stack, timeout: int = 600) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(stack.api + "/health", timeout=5):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(5)
    raise RuntimeError("the api did not become healthy")


def put_app(stack: Stack, which: str, message: str) -> str:
    """Copy the app's files into the build workspace and commit them (as boss): a build's local
    workspace, which is what the scan checks out for a build with no PR."""
    src = HERE / which
    stack.exec_api(f"mkdir -p {WS}", user="root")
    for name in ("package.json", "package-lock.json", "server.js"):
        sh(["docker", "cp", str(src / name), f"{PROJECT}-api-1:{WS}/{name}"])
    stack.exec_api(f"chown -R boss:boss {WS}", user="root")
    stack.exec_api(
        f"cd {WS} && (git rev-parse --git-dir >/dev/null 2>&1 || git init -q -b boss/code/{RUN_ID}) && "
        f"git add -A && git -c user.email=s4@boss.test -c user.name='S4 proof' commit -qm '{message}' && "
        "git rev-parse HEAD")
    return stack.exec_api(f"cd {WS} && git rev-parse HEAD").strip()


def register_app(stack: Stack) -> None:
    sql = ("INSERT INTO code_task_runs (id, project_id, title, status, spec, finalize_ctx) VALUES "
           f"('{RUN_ID}', 'boss-dogfood', 'S4 proof app', 'completed', '{{}}'::jsonb, "
           f"'{{\"app_id\": \"{APP_KEY}\"}}'::jsonb) ON CONFLICT (id) DO NOTHING")
    stack.compose("exec", "-T", "db", "psql", "-U", "boss", "-d", "boss", "-v", "ON_ERROR_STOP=1", "-c", sql)


def scan(stack: Stack, label: str, timeout: int = 2400) -> dict[str, Any]:
    st, body = stack.call("POST", "/v1/security/scans", {"project_id": "boss-dogfood", "app_key": APP_KEY,
                                                         "tools": ["semgrep", "osv", "zap"],
                                                         "zap": {"mode": "baseline", "target": "sandbox"}})
    if st != 202:
        raise RuntimeError(f"{label}: POST /v1/security/scans -> {st} {body}")
    scan_id = body["scan_id"]
    print(f"[{label}] scan {scan_id} queued", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, row = stack.call("GET", f"/v1/security/scans/{scan_id}")
        if st == 200 and row["status"] in ("done", "failed"):
            break
        time.sleep(10)
    else:
        raise RuntimeError(f"{label}: scan {scan_id} did not finish in {timeout}s")
    st, found = stack.call("GET", f"/v1/security/scans/{scan_id}/findings")
    row["findings"] = found["findings"]
    print(f"[{label}] {row['status']} commit={row['commit_sha'][:12]} counts={json.dumps(row['counts']['by_tool'])}", flush=True)
    for t in row["tools"]:
        print(f"[{label}]   {t['name']:8} {t['status']:6} v{t['version']} {t['duration_ms']/1000:.1f}s "
              f"findings={t['findings']} exit={t['exit']}" + (f" error={t.get('error')}" if t.get("error") else ""), flush=True)
    return row


def check(cond: bool, what: str, failures: list[str]) -> None:
    print(("  PASS " if cond else "  FAIL ") + what, flush=True)
    if not cond:
        failures.append(what)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave the stack up (you must `down -v` it)")
    ap.add_argument("--out", default=str(ROOT / "artifacts" / "e2e-security"))
    ap.add_argument("--no-build", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    busy = other_stacks_up()
    if busy:
        print("another e2e stack is up; wait for it: " + ", ".join(busy))
        return 2
    stack = Stack(out)
    stack.write_env()
    failures: list[str] = []
    try:
        if not args.no_build:
            print("building api, worker, egress with BOSS_TOOLCHAINS=security ...", flush=True)
            stack.compose("build", "api", "worker", "egress", timeout=5400)
        size = sh(["docker", "image", "inspect", f"boss-community-api:{PROJECT}", "--format", "{{.Size}}"]).stdout.strip()
        print(f"api image: {int(size) / 1e9:.2f} GB", flush=True)
        stack.compose("up", "-d", "db", "api", "worker", "egress", timeout=900)
        wait_health(stack)
        stack.compose("exec", "-T", "-u", "boss", "api", "python", "-c",
                      "from boss_engine.bootstrap import seed; seed()", timeout=600)
        manifest = stack.exec_api("cat /opt/toolchains/manifest.json")
        print("toolchains:", manifest.strip(), flush=True)
        register_app(stack)
        v1 = put_app(stack, "vulnapp", "the app, as built (vulnerable)")
        before = scan(stack, "before")
        v2 = put_app(stack, "vulnapp-fixed", "fix: parameterised query, key from env, lodash 4.18.1, headers")
        after = scan(stack, "after")
        st, rep = stack.call("GET", f"/v1/security/reports/{APP_KEY}?project_id=boss-dogfood")
        st_md, md = stack.call("GET", f"/v1/security/reports/{APP_KEY}?project_id=boss-dogfood&format=md")
        egress = stack.compose("logs", "--no-color", "egress", check=False).stdout

        def ids(row, tool):
            return {f["rule_id"] for f in row["findings"] if f["tool"] == tool}

        print("\nCHECKS", flush=True)
        check(before["status"] == "done" and all(t["status"] == "done" for t in before["tools"]),
              "before: every tool ran", failures)
        check(before["commit_sha"] == v1 and after["commit_sha"] == v2, "scans record the commits", failures)
        check(SQLI in ids(before, "semgrep"), "Semgrep finds the SQL injection", failures)
        check(JWT in ids(before, "semgrep"), "Semgrep finds the hardcoded JWT secret", failures)
        check({"GHSA-35jh-r3h4-6jhm"} <= ids(before, "osv")
              and all(f["location"]["package"] == "lodash" for f in before["findings"] if f["tool"] == "osv"),
              "OSV finds the vulnerable lodash", failures)
        check(HEADERS <= ids(before, "zap"), "ZAP baseline finds the missing security headers", failures)
        check("s4-proof-hardcoded-signing-key" not in json.dumps(before["findings"]), "the secret is redacted", failures)
        check(after["status"] == "done" and all(t["status"] == "done" for t in after["tools"]),
              "after: every tool ran", failures)
        check(not ({SQLI, JWT} & ids(after, "semgrep")) and not ids(after, "osv") and not (HEADERS & ids(after, "zap")),
              "after the fix the planted findings are gone", failures)
        fb = {f["fingerprint"]: f for f in before["findings"]}
        remaining = after["findings"]
        check(bool(remaining) and all(f["fingerprint"] in fb for f in remaining),
              f"every remaining finding ({len(remaining)}) keeps its fingerprint", failures)
        moved = [f for f in remaining if f["location"].get("line") and
                 f["location"]["line"] != fb[f["fingerprint"]]["location"].get("line")]
        check(bool(moved), f"including {len(moved)} whose line moved", failures)
        check(st == 200 and rep["summary"]["new"] == 0 and rep["summary"]["fixed"] >= 9,
              f"report: {rep.get('summary')}", failures)
        check(st_md == 200 and "## Fixed" in md, "report exports as Markdown", failures)
        sbx = [r["target"].get("sandbox") or {} for r in (before, after)]
        check(all(x.get("isolated") and 61000 <= int(x.get("uid") or 0) < 62000 and x.get("network_fenced")
                  for x in sbx), f"the tools ran as a sandbox uid on the fenced network: {sbx}", failures)
        fenced = stack.compose("exec", "-T", "api", "test", "-f", "/run/boss-shim/.network-fenced", check=False)
        check(fenced.returncode == 0 and "connect refused" not in egress,
              "the sandbox network is fenced (only boss-egress reaches out), and egress refused nothing: "
              "OSV's findings came through it", failures)
        (out / "proof.json").write_text(json.dumps({"before": before, "after": after, "report": rep}, indent=1, default=str))
        (out / "report.md").write_text(md if isinstance(md, str) else "")
        print(f"\nwrote {out / 'proof.json'} and {out / 'report.md'}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"error: {exc}")
        print("ERROR", exc, flush=True)
        try:
            (out / "api.log").write_text(stack.compose("logs", "--no-color", "--tail", "400", "api", check=False).stdout)
            (out / "worker.log").write_text(stack.compose("logs", "--no-color", "--tail", "200", "worker", check=False).stdout)
        except Exception:  # noqa: BLE001
            pass
    finally:
        if not args.keep:
            stack.compose("down", "-v", "--remove-orphans", timeout=600, check=False)
    print("\nRESULT:", "PASS" if not failures else f"FAIL ({len(failures)})")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

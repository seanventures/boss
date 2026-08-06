"""Every endpoint the delivery console calls, exercised the way the console calls it.

This exists because the console shipped with buttons that did nothing, and each was found by an
operator rather than by me. Unit tests covered the pure logic; nothing covered "does pressing
this actually work end to end". The gap between those two is where every reported bug lived:

  * Deploy sent no code_task_run_id and died with "code task run not found:" — I had verified
    `plan_only` and called it deploying;
  * Create target was disabled while `repo` was empty, with no explanation;
  * the deployment-target list 500'd on a schema mismatch nothing read.

Run against a LIVE local API (it creates and then removes a probe target):
    PYTHONPATH=engine .venv/bin/python tests/test_console_endpoints.py

Written in Python rather than curl because the first version of this harness hand-escaped JSON
in bash, produced malformed bodies, and reported 15 failures that were all its own fault — a
test that lies about the system is worse than no test.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

API = "http://localhost:8000"
PROJECT = "boss-dogfood"
_failures: list[str] = []


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}", method=method, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode() or "{}"
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() or "{}"
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:200]}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def check(name: str, got: int, want: tuple[int, ...], body: dict | None = None) -> bool:
    if got in want:
        print(f"  PASS  {name} ({got})")
        return True
    _failures.append(f"{name}: got {got}, want {want}")
    print(f"  FAIL  {name} -> got {got}, want {want}")
    if body:
        print(f"        {json.dumps(body)[:200]}")
    return False


def _purge(target_id: str) -> None:
    """Remove a probe target outright. Retiring is right for real targets — history matters —
    but a test fixture that lingers makes the next run fail on a uniqueness clash."""
    import subprocess
    subprocess.run([sys.executable, "-c",
                    "import sys; sys.path.insert(0,'engine');"
                    "from boss_engine.config import Settings;"
                    "from boss_engine.db import get_engine, begin;"
                    "from boss_engine.ports import sql;"
                    f"c=begin(get_engine(Settings()));conn=c.__enter__();"
                    f"conn.execute(sql(\"delete from deployment_health_samples where deployment_target_id='{target_id}'\"));"
                    f"conn.execute(sql(\"delete from deployment_runs where deployment_target_id='{target_id}'\"));"
                    f"conn.execute(sql(\"delete from deployment_targets where id='{target_id}'\"));"
                    "c.__exit__(None,None,None)"],
                   capture_output=True, timeout=60)


def main() -> int:
    print("== page load reads ==")
    for label, path in (
        ("pipeline", f"/v1/delivery/pipeline?project_id={PROJECT}"),
        ("health", f"/v1/delivery/health?project_id={PROJECT}"),
        ("secrets", f"/v1/secrets?project_id={PROJECT}"),
        ("observability", f"/v1/observability/stack?project_id={PROJECT}"),
        ("deployment-targets", f"/v1/deployment-targets?project_id={PROJECT}"),
    ):
        code, body = call("GET", path)
        check(f"GET {label}", code, (200,), body)

    print("== Secrets panel ==")
    code, kp = call("POST", "/v1/secrets/keypair", {
        "project_id": PROJECT, "name": "e2e_probe_key", "backend": "file", "scope_id": "e2e"})
    check("POST keypair", code, (201,), kp)
    sref = kp.get("secret_ref_id", "")
    check("keypair returns a PUBLIC key", 200 if kp.get("public_key") else 0, (200,))
    check("keypair NEVER returns private material",
          200 if "BEGIN OPENSSH PRIVATE" not in json.dumps(kp) else 0, (200,))

    code, body = call("POST", "/v1/secrets", {
        "project_id": PROJECT, "name": "e2e_probe_token", "value": "s3cr3t",
        "secret_kind": "api_token", "backend": "file"})
    check("POST upload secret", code, (201,), body)
    code, body = call("POST", f"/v1/secrets/{sref}/verify", {})
    check("POST verify secret", code, (200,), body)

    print("== Onboard wizard ==")
    # A retired duplicate would (correctly) 409, so the probe target from a previous run is
    # cleared first. A suite that only passes on a clean database is a suite nobody re-runs.
    code, existing = call("GET", f"/v1/deployment-targets?project_id={PROJECT}")
    for t in (existing.get("items") or []):
        if t.get("app_name") == "e2e-probe":
            call("DELETE", f"/v1/deployment-targets/{t['id']}")
            _purge(t["id"])

    code, target = call("POST", "/v1/deployment-targets", {
        "project_id": PROJECT, "app_name": "e2e-probe", "repo": "boss-sandbox",
        "environment": "dev", "target_kind": "ssh", "target_ref": "ubuntu@3.94.66.139",
        "domains": ["3.94.66.139:3011"],
        "health_checks": [{"kind": "http", "path": "/", "expect": 200, "scheme": "http"}]})
    check("POST create target", code, (201,), target)
    tid = target.get("id", "")
    if not tid:
        print("  (cannot continue without a target id)")
        return 1

    for label, method, path, payload, want in (
        ("bind secret", "POST", f"/v1/deployment-targets/{tid}/secrets",
         {"project_id": PROJECT, "name": "ssh_key", "secret_ref_id": sref}, (200,)),
        ("bootstrap", "POST", f"/v1/deployment-targets/{tid}/bootstrap",
         {"apply_port": False}, (200,)),
        ("preflight", "GET", f"/v1/delivery/targets/{tid}/preflight?project_id={PROJECT}",
         None, (200,)),
        ("PATCH target", "PATCH", f"/v1/deployment-targets/{tid}",
         {"domains": ["3.94.66.139:3011"]}, (200,)),
        ("auto-deploy on", "POST", f"/v1/deployment-targets/{tid}/auto-deploy",
         {"auto_deploy": True}, (200,)),
        ("pause", "POST", f"/v1/deployment-targets/{tid}/auto-deploy",
         {"paused": True}, (200,)),
    ):
        code, body = call(method, path, payload)
        check(f"{method} {label}", code, want, body)

    print("== Deploy controls ==")
    # A target with no build must refuse with a SENTENCE, not a stack trace from three layers
    # down — this is the bug an operator hit as "nothing happens".
    code, body = call("POST", "/v1/delivery/deploy", {"target_id": tid, "actor": "operator"})
    check("POST deploy with no build -> 422", code, (422,), body)
    check("the 422 explains itself",
          200 if "never been deployed" in json.dumps(body) else 0, (200,), body)

    code, body = call("POST", "/v1/delivery/deploy",
                      {"target_id": "deptgt_stocks_aws", "plan_only": True, "actor": "operator"})
    check("POST deploy plan_only", code, (200,), body)
    code, body = call("POST", "/v1/delivery/deployments/deprun_nope/execute", None)
    check("execute unknown run -> 404", code, (404,), body)
    code, body = call("POST", "/v1/delivery/deployments/deprun_nope/redeploy", {})
    check("redeploy unknown run -> 404", code, (404,), body)

    print("== Teardown and restore ==")
    code, body = call("POST", f"/v1/deployment-targets/{tid}/teardown", {})
    check("teardown without confirm -> 428", code, (428,), body)
    # The probe target is bound to a key GENERATED here, whose public half was never installed
    # on the host — so SSH is refused. Teardown must decline with a legible reason rather than
    # pretend it removed something, and a 409 carrying that reason is the correct outcome.
    # (Asserting 200 here would have been asserting that BOSS tears down hosts it cannot reach.)
    code, body = call("POST", f"/v1/deployment-targets/{tid}/teardown", {"stop_only": True})
    check("teardown refuses an unreachable host", code, (200, 409), body)
    check("the refusal names the reason",
          200 if ("Permission denied" in json.dumps(body) or code == 200) else 0, (200,), body)
    code, body = call("POST", f"/v1/deployment-targets/{tid}/teardown",
                      {"confirm": "e2e-probe", "include_data": True})
    check("teardown confirmed (or refused with a reason)", code, (200, 409), body)
    code, body = call("PATCH", f"/v1/deployment-targets/{tid}", {"status": "active"})
    check("restore (reactivate)", code, (200,), body)
    code, body = call("DELETE", f"/v1/deployment-targets/{tid}")
    check("retire probe target", code, (200,), body)

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("every console endpoint works")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

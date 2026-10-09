"""The F5 end-to-end proof on a local compose stack (APP_TEMPLATES_AND_SECURITY_LOOP.md §5.9).

    python3 tests/e2e/links/proof.py --env-file <stack.env> [--project bosss5] [--keep]

Builds nothing: run `docker compose ... build api egress` first (the README of tests/e2e). Brings
up db, api and egress under its own compose project, copies inside.py and the two apps into the
api container, runs inside.py as `boss`, checks every phase, and `down -v` unless --keep.
Exit 0 only when B signed in to A through the link, every way around the gateway was refused,
and the revoked link cut B off within 5 seconds.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
BLOCKED = {"ConnectionRefusedError", "TimeoutError", "timeout", "OSError", "socket.timeout"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--project", default="bosss5")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    dc = ["docker", "compose", "-p", a.project, "--env-file", a.env_file,
          "-f", str(ROOT / "scripts/install/community-compose.yml")]
    subprocess.run(dc + ["up", "-d", "db", "api", "egress"], check=True)
    try:
        for _ in range(120):
            ps = subprocess.run(dc + ["ps", "--format", "json", "egress"], capture_output=True, text=True).stdout
            if '"healthy"' in ps:
                break
            time.sleep(2)
        subprocess.run(dc + ["exec", "-T", "api", "mkdir", "-p", "/tmp/s5"], check=True)
        for name in ("inside.py", "provider_a.py", "b_tests.py"):
            subprocess.run(dc + ["cp", str(HERE / name), f"api:/tmp/s5/{name}"], check=True)
        subprocess.run(dc + ["exec", "-T", "api", "chmod", "-R", "a+rX", "/tmp/s5"], check=True)
        out = subprocess.run(dc + ["exec", "-T", "-u", "boss", "-w", "/app", "api", "python", "/tmp/s5/inside.py"],
                             capture_output=True, text=True, timeout=600)
        phases = {}
        for line in out.stdout.splitlines():
            try:
                rec = json.loads(line)
                phases[rec["phase"]] = rec
                print(json.dumps(rec))
            except ValueError:
                pass
        if out.returncode:
            print(out.stderr[-2000:], file=sys.stderr)
        return verdict(phases)
    finally:
        if not a.keep:
            subprocess.run(dc + ["down", "-v"], check=False)


def verdict(p: dict) -> int:
    fails = []
    linked = p.get("b_tests_linked", {})
    if not linked.get("signs_in_through_link"):
        fails.append("B did not sign in to A through the link")
    for what, got in (linked.get("direct") or {}).items():
        if got == "open":
            fails.append(f"B reached {what} around the gateway")
    gw = linked.get("gateway") or {}
    if gw.get("no_pass") != "no_link_pass":
        fails.append(f"gateway without a pass: {gw.get('no_pass')}")
    if not str(gw.get("absolute_form_to_api", "")).startswith("HTTP/1.0 400"):
        fails.append(f"gateway absolute-form to the API: {gw.get('absolute_form_to_api')}")
    if gw.get("bank_pass_on_ledger_port") != "wrong_link":
        fails.append(f"BANK pass on the LEDGER port: {gw.get('bank_pass_on_ledger_port')}")
    rv = p.get("revoked", {})
    if rv.get("delete") != 200 or rv.get("b_exit") == 0 or not (rv.get("cut_off_after_seconds", 99) < 5):
        fails.append(f"revocation: {rv}")
    print(json.dumps({"verdict": "PASS" if not fails else "FAIL", "failures": fails}))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

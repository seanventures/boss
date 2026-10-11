"""Previews run as a sandbox uid: the end-to-end check on a compose stack (IDENTITY.md §7.6).

    python3 tests/e2e/preview_sandbox/proof.py --project <compose project> --env-file <stack.env>

Runs against an api container that is already up (the combined proof brings the stack up). Copies
inside.py and the two apps into it, runs inside.py as `boss`, and checks: the preview ran as a pool
uid with the fence on; it could not read the identity key files, /proc of the API or another
preview's directory, nor reach the database or another preview's port (loopback or the api
address); it could reach its own port and egress; BOSS still reached it; and teardown left no
process and no directory. Exit 0 only when all hold.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
REFUSED = {"PermissionError", "ConnectionRefusedError", "TimeoutError", "OSError", "timeout"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--compose-file", action="append", default=[])
    a = ap.parse_args()
    files = a.compose_file or [str(ROOT / "scripts/install/community-compose.yml")]
    dc = ["docker", "compose", "-p", a.project, "--env-file", a.env_file]
    for f in files:
        dc += ["-f", f]
    subprocess.run(dc + ["exec", "-T", "api", "mkdir", "-p", "/tmp/psbx"], check=True)
    for name in ("inside.py", "evil_app.py", "victim_app.py"):
        subprocess.run(dc + ["cp", str(HERE / name), f"api:/tmp/psbx/{name}"], check=True)
    subprocess.run(dc + ["exec", "-T", "api", "chmod", "-R", "a+rX", "/tmp/psbx"], check=True)
    out = subprocess.run(dc + ["exec", "-T", "-u", "boss", "-w", "/app", "api", "python", "/tmp/psbx/inside.py"],
                         capture_output=True, text=True, timeout=300)
    phases = {}
    for line in out.stdout.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        phases[rec["phase"]] = rec
        print(json.dumps(rec))
    if out.returncode:
        print(out.stderr[-3000:], file=sys.stderr)
    return verdict(phases)


def verdict(p: dict) -> int:
    fails = []
    st = p.get("status", {})
    if not (st.get("isolating") and st.get("network_fenced")):
        fails.append(f"sandbox not isolating/fenced: {st}")
    ev = p.get("evil", {})
    probe = ev.get("probe") or {}
    if not probe:
        fails.append("the evil preview never reported")
    if not (61000 <= int(probe.get("uid") or 0) < 62000):
        fails.append(f"the preview ran as uid {probe.get('uid')}, not a sandbox uid")
    for key in ("api_environ", "pid1_environ", "api_mem", "victim_dir", "sandboxes_root"):
        if probe.get(key) not in REFUSED:
            fails.append(f"{key}: {probe.get(key)}")
    for name, got in (probe.get("identity") or {}).items():
        if got not in REFUSED and got != "FileNotFoundError":
            fails.append(f"identity {name}: {got}")
    if probe.get("identity", {}).get("<dir>") not in REFUSED:
        fails.append("the identity directory was listable")
    # BOSS_OWNER_TOKENS is owned_process's process-ownership marker (own_<pid>_<uuid>), not a secret.
    secrets = [k for k in probe.get("env_has_secrets") or [] if k != "BOSS_OWNER_TOKENS"]
    if secrets:
        fails.append(f"secret-shaped env: {secrets}")
    for key in ("db", "victim_loopback", "victim_api_addr"):
        if probe.get(key) == "open":
            fails.append(f"reached {key}")
    if probe.get("own_port_loopback") != "open":
        fails.append(f"own port: {probe.get('own_port_loopback')}")
    if probe.get("egress_proxy") != "open":
        fails.append(f"egress: {probe.get('egress_proxy')}")
    if ev.get("port") != ev.get("own_port") or not ev.get("served_to_boss"):
        fails.append(f"preview port {ev.get('port')} (own {ev.get('own_port')}), served {ev.get('served_to_boss')}")
    for k in ("teardown_prev_evil", "teardown_prev_victim"):
        t = p.get(k, {})
        if not t.get("run_dir_gone") or t.get("processes_left"):
            fails.append(f"{k}: {t}")
    print(json.dumps({"verdict": "PASS" if not fails else "FAIL", "failures": fails}))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

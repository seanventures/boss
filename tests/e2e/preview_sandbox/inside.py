"""The preview-sandbox proof, the part that runs in the api container as `boss`, through BOSS's own
preview code paths (preview_service: the sandbox lease, the tree, the port, launch_app, discovery,
teardown). Prints one JSON line per phase. Driven by tests/e2e/preview_sandbox/proof.py.
"""
import json
import os
import shutil
import socket
import sys
import time
import urllib.parse
from pathlib import Path

from boss_engine.services import owned_process, preview_service as ps, sandbox_identity as si

HERE = Path(__file__).resolve().parent


def api_pid() -> int:
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ")
            except OSError:
                continue
            if b"apps.api.serve" in cmd:
                return int(d.name)
    return 1


def start(preview_id: str, app: str, argv: list[str]):
    ident = ps._preview_sandbox(preview_id)
    workdir = ps._preview_workdir(preview_id, ident)
    shutil.copy(HERE / app, workdir / app)
    ps._open_to_sandbox(workdir)
    log = ps.preview_log_path(preview_id)
    proc, hint = ps.launch_app(workdir, [sys.executable, app, *argv], log,
                               owner=owned_process.Owner("preview", preview_id, "proj_sbx"),
                               port=ps._sandboxed_preview_port(ident))
    port = ps._discover_working_port(log, hint, timeout=30, proc=proc, listener_uid=ident.uid)
    return ident, workdir, proc, port, log


def main() -> None:
    print(json.dumps({"phase": "status", **si.status()}), flush=True)
    db = urllib.parse.urlsplit(os.environ["DATABASE_URL"].replace("+psycopg", ""))
    api_addr = socket.gethostbyname(socket.gethostname())
    v_ident, v_dir, v_proc, v_port, _ = start("prev_victim", "victim_app.py", [])
    print(json.dumps({"phase": "victim", "uid": v_ident.uid if v_ident else None, "port": v_port}), flush=True)
    e_ident, e_dir, e_proc, e_port, e_log = start(
        "prev_evil", "evil_app.py",
        [str(api_pid()), f"{db.hostname}:{db.port or 5432}", str(v_port), str(v_dir.parent), api_addr])
    probe = {}
    for _ in range(60):
        for line in Path(e_log).read_text(errors="ignore").splitlines():
            if line.startswith('{"probe"'):
                probe = json.loads(line)["probe"]
        if probe:
            break
        time.sleep(0.5)
    print(json.dumps({"phase": "evil", "uid": e_ident.uid if e_ident else None, "port": e_port,
                      "own_port": si.preview_port_for_uid(e_ident.uid) if e_ident else None,
                      "served_to_boss": ps._http_ok(e_port) if e_port else False, "probe": probe}), flush=True)
    for pid_, proc, ident in (("prev_evil", e_proc, e_ident), ("prev_victim", v_proc, v_ident)):
        run_dir = ident.run_dir if ident else None
        ps._teardown({"id": pid_, "pid": proc.pid, "mode": "process"})
        left = [p for p in Path("/proc").iterdir() if p.name.isdigit() and ident and _uid_of(p) == ident.uid]
        print(json.dumps({"phase": f"teardown_{pid_}", "run_dir_gone": bool(run_dir) and not run_dir.exists(),
                          "processes_left": len(left)}), flush=True)


def _uid_of(p: Path):
    try:
        for line in (p / "status").read_text().splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[1])
    except OSError:
        return None


main()

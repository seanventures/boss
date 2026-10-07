"""A51 harness, run as `boss` inside the container AFTER scripts/sandbox/entrypoint.sh fenced the
network: stands up what an API container holds (a process with secrets in its environment, the
identity key file, another run's directory, mcp.json and shim socket), then runs probe.py as a
sandbox uid through the REAL sandbox code path (sandbox_identity.bind + owned_process) and checks
every secret is unreachable. Exit 0 = A51 holds."""
import json
import os
import socket
import subprocess
import sys

sys.path.insert(0, "/src/engine")
os.environ.setdefault("BOSS_SANDBOX_ISOLATION", "required")

from boss_engine.identity import runs  # noqa: E402
from boss_engine.services import owned_process, sandbox_identity  # noqa: E402

api = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                       env={**os.environ, "DATABASE_URL": "postgresql://boss:TOPSECRET@db/boss",
                            "BOSS_GITHUB_TOKEN": "ghp_TOPSECRET"})
key_path = "/var/lib/boss/identity/master.key"
with open(os.open(key_path, os.O_WRONLY | os.O_CREAT, 0o600), "w") as fh:
    fh.write("TOPSECRETKEY")
listeners = []
for port in (5432, 8000):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(8)
    listeners.append(s)
host_ip = socket.gethostbyname(socket.gethostname())

with sandbox_identity.bind("crun_other") as other:
    with owned_process.owner_scope("code_run", "crun_other"):
        other_shim = runs.Shim(run_id="run_ct:crun_other")
        other_shim.egress_credential = "boss_egr_OTHERRUNSECRET"
        other_sock = runs.serve_shim_socket(other_shim)
        mcp_dir = other.workspace("mcp-")
        os.chmod(mcp_dir, 0o2755)
        mcp = os.path.join(mcp_dir, "mcp.json")
        with open(mcp, "w") as fh:
            fh.write('{"mcpServers": {"x": {"headers": {"Authorization": "Bearer TOPSECRET"}}}}')
        os.chmod(mcp, 0o644)
    with sandbox_identity.bind("crun_mine") as mine:
        assert mine.uid != other.uid
        with owned_process.owner_scope("code_run", "crun_mine"):
            my_shim = runs.Shim(run_id="run_ct:crun_mine")
            my_shim.egress_credential = "boss_egr_MINE"
            my_sock = runs.serve_shim_socket(my_shim)
            targets = {"api_pid": api.pid, "master_key": key_path, "other_run_dir": str(other.run_dir),
                       "other_run_mcp": mcp, "other_socket": other_sock, "own_socket": my_sock,
                       "run_root": str(sandbox_identity.run_root()), "host_ip": host_ip}
            done = owned_process.run([sys.executable, "/src/engine/tests/sandbox_a51/probe.py", json.dumps(targets)],
                                     capture_output=True, text=True, timeout=60)
api.kill()
print(done.stderr, file=sys.stderr)
result = json.loads(done.stdout.strip().splitlines()[-1])
print(json.dumps(result, indent=1))
must_fail = ("api_environ", "api_mem", "master_key", "identity_dir", "other_run_dir", "other_run_mcp",
             "sandbox_root_listing", "other_shim_socket", "db_port", "internet", "write_app")
must_work = ("own_shim_socket", "api_port", "own_home_write")
bad = [k for k in must_fail if result.get(k) == "reachable"] + [k for k in must_work if result.get(k) != "reachable"]
in_pool = sandbox_identity.UID_BASE <= result["uid"] < sandbox_identity.UID_BASE + sandbox_identity.UID_COUNT
if not in_pool or result["uid"] != mine.uid or result["uid"] == other.uid:
    bad.append("wrong_uid")
if any(g != result["gid"] for g in result["groups"]):
    bad.append("supplementary_groups")
print("A51", "HOLDS" if not bad else f"FAILS: {bad}")
sys.exit(0 if not bad else 1)

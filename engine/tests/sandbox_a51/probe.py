"""A51 probe: runs AS A SANDBOX UID (via boss-sandbox-exec) and tries to reach every secret a
code-task agent used to be able to reach. Prints one JSON object: name -> "reachable" | reason."""
import json
import os
import socket
import sys

targets = json.loads(sys.argv[1])
out = {"uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups()}


def attempt(name, fn):
    try:
        fn()
        out[name] = "reachable"
    except Exception as exc:  # noqa: BLE001
        out[name] = type(exc).__name__


def read(path):
    with open(path, "rb") as fh:
        fh.read(1)


attempt("api_environ", lambda: read(f"/proc/{targets['api_pid']}/environ"))
attempt("api_mem", lambda: read(f"/proc/{targets['api_pid']}/mem"))
attempt("master_key", lambda: read(targets["master_key"]))
attempt("identity_dir", lambda: os.listdir(os.path.dirname(targets["master_key"])))
attempt("other_run_dir", lambda: os.listdir(targets["other_run_dir"]))
attempt("other_run_mcp", lambda: read(targets["other_run_mcp"]))
attempt("sandbox_root_listing", lambda: os.listdir(targets["run_root"]))


def other_socket():
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(2)
    s.connect(targets["other_socket"])
    s.sendall(b"get\n")
    data = s.recv(4096)
    if not data:
        raise PermissionError("no answer")


attempt("other_shim_socket", other_socket)


def own_socket():
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(2)
    s.connect(targets["own_socket"])
    s.sendall(b"get\n")
    if b"password=" not in s.recv(4096):
        raise PermissionError("no answer")


attempt("own_shim_socket", own_socket)


def tcp(port):
    def go():
        s = socket.create_connection((targets["host_ip"], port), timeout=3)
        s.close()
    return go


attempt("db_port", tcp(5432))
attempt("api_port", tcp(8000))
attempt("internet", lambda: socket.create_connection(("1.1.1.1", 443), timeout=3).close())
attempt("write_app", lambda: open("/app-probe-written", "w").close())
attempt("own_home_write", lambda: open(os.path.join(os.environ["HOME"], "ok"), "w").close())
print(json.dumps(out))

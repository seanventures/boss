"""The F5 proof, the part that runs in the api container (as `boss`): BOSS's own code paths,
end to end. Driven by tests/e2e/links/proof.py on the host. Prints one JSON line per phase.

  1. two apps in one project: A "Banking" (a tiny API with a login), B "Bank attacker" (tests);
     a third, C "Ledger viewer", is just another preview B must not reach;
  2. link B -> A (BANK, preview, test users) and B -> C (LEDGER) THROUGH THE REAL API;
  3. A's preview starts the way preview_service starts one: a port from BOSS_PREVIEW_PORTS and
     the environment of app_environment + preview_link_environment (BOSS_TEST_USERS, rotated);
  4. B runs as a BUILD: a sandbox uid (boss-sandbox-exec), behind the real iptables fence, with
     the environment of a build (workload_environment): its tests sign in to A as A's test user
     through the link gateway on egress, and every way around the gateway is refused;
  5. the link is revoked THROUGH THE API; B's next runs fail, measured from the 200 of the DELETE.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

HERE = Path(__file__).resolve().parent
HELPER = "/usr/local/libexec/boss/boss-sandbox-exec"
B_UID = "61777"
engine = create_engine(os.environ["DATABASE_URL"])


def operator_key() -> str:
    """An Operator's key (a service account bound to the built-in Operator role): link create and
    revoke go through the real API under enforce as the role §5.1 names. Never printed."""
    from boss_engine.identity import api_keys, service_accounts, users
    from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus

    with engine.begin() as db:
        owner = users.by_email(db, os.environ.get("S5_OWNER_EMAIL", "e2e-owner@example.test"))
        ctx = AuthContext(owner.principal(), AuthMethod.SESSION)
        org = owner.org_id
        pid = service_accounts.create(db, actor=ctx, name=f"s5-proof-{uuid.uuid4().hex[:6]}",
                                      description="F5 proof: an Operator links two apps.",
                                      owners=[owner.principal_id]).principal_id
        users.add_binding(db, org_id=org, subject_id=pid, role_key="operator", granted_by=owner.principal_id,
                          reason="s5 proof")
        sa = Principal(pid, PrincipalKind.SERVICE_ACCOUNT, org, PrincipalStatus.ACTIVE, "s5-proof")
        return api_keys.create(db, creator=ctx, owner=sa, kind="sak", name="s5-proof",
                               scopes=("app_link:manage", "project:read"), expires_in_days=1).key


KEY = operator_key()


def api(method, path, body=None):
    req = urllib.request.Request("http://127.0.0.1:8000" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json",
                                          "authorization": "Bearer " + KEY})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def say(phase, **kw):
    print(json.dumps({"phase": phase, **kw}), flush=True)


with engine.begin() as db:
    project = db.execute(text("SELECT id FROM projects ORDER BY created_at LIMIT 1")).scalar()
    if project is None:                                   # a fresh install: one project, as Setup makes
        ws = db.execute(text("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")).scalar()
        project = "prj_s5proof"
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:i, :w, 'S5 proof', 's5-proof')"),
                   {"i": project, "w": ws})
    runs = {k: f"ctr_s5{k}{uuid.uuid4().hex[:6]}" for k in ("a", "b", "c")}
    for k, title in (("a", "Banking"), ("b", "Bank attacker"), ("c", "Ledger viewer")):
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status) VALUES (:i, :p, :t, :s)"),
                   {"i": runs[k], "p": project, "t": title, "s": "running" if k == "b" else "completed"})
say("apps", project=project)

s1, bank = api("POST", f"/v1/apps/{runs['b']}/links", {"project_id": project, "provider_app_key": "Banking",
                                                        "alias": "BANK", "env": "preview", "credentials": "test_users"})
s2, ledger = api("POST", f"/v1/apps/{runs['b']}/links", {"project_id": project, "provider_app_key": "Ledger viewer",
                                                          "alias": "LEDGER", "env": "preview", "credentials": "none"})
say("linked", bank=s1, ledger=s2, bank_url=bank.get("url"), error=bank.get("error"))
if s1 != 201 or s2 != 201:
    sys.exit(1)

from boss_engine.app_links import service as links                            # noqa: E402
from boss_engine.services import preview_service as ps                        # noqa: E402

procs, ports, previews = [], {}, {}
expires = datetime.now(timezone.utc) + timedelta(minutes=30)
for k in ("a", "c"):
    ports[k] = ps.app_preview_port()
    previews[k] = f"prev_s5{k}{uuid.uuid4().hex[:6]}"
    extra = ps.preview_link_environment(engine, project_id=project, run_id=runs[k], preview_id=previews[k],
                                        expires_at=expires, rotate=True)
    workdir = Path(tempfile.mkdtemp(prefix=f"s5-{k}-"))
    env = ps.app_environment(workdir, ports[k], extra)
    procs.append(subprocess.Popen([sys.executable, str(HERE / "provider_a.py")], env=env, cwd=workdir))
    with engine.begin() as db:
        db.execute(text("INSERT INTO app_previews (id, project_id, run_id, port, url, status, mode, expires_at) "
                        "VALUES (:i, :p, :r, :port, :u, 'running', 'process', :x)"),
                   {"i": previews[k], "p": project, "r": runs[k], "port": ports[k],
                    "u": f"http://127.0.0.1:{ports[k]}", "x": expires})
    if k == "a":
        say("provider_preview", port=ports[k], seeded_test_users="BOSS_TEST_USERS" in extra)
time.sleep(1.0)

with engine.begin() as db:
    b_env = links.workload_environment(db, project_id=project, run_id=runs["b"], env="preview",
                                       consumer_kind="run", consumer_id=runs["b"])
say("build_env", names=sorted(b_env))
gw_port = int(b_env["BOSS_LINK_BANK_URL"].rsplit(":", 1)[1])
for _ in range(50):                                       # the gateway opens the link's port
    with socket.socket() as s:
        if s.connect_ex(("egress", gw_port)) == 0:
            break
    time.sleep(0.2)

script = Path("/tmp/s5-b_tests.py")
script.write_text((HERE / "b_tests.py").read_text())
script.chmod(0o755)
b_base = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "A_PORT": str(ports["a"]),
          "C_PORT": str(ports["c"]), **b_env}


def run_b(mode):
    done = subprocess.run([HELPER, "run", B_UID, "--", sys.executable, str(script), mode], env=b_base,
                          capture_output=True, text=True, timeout=60, cwd="/tmp")
    try:
        return done.returncode, json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return done.returncode, {"stdout": done.stdout[-300:], "stderr": done.stderr[-300:]}


code, full = run_b("full")
say("b_tests_linked", exit=code, uid=B_UID, **full)

s3, revoked = api("DELETE", f"/v1/apps/{runs['b']}/links/{bank['id']}?project_id={project}")
t0 = time.monotonic()
attempts = []
while time.monotonic() - t0 < 10:
    code, res = run_b("auth")
    attempts.append(round(time.monotonic() - t0, 2))
    if code != 0:
        break
say("revoked", delete=s3, b_exit=code, b_status=res.get("status"), reason=res.get("reason"),
    cut_off_after_seconds=round(time.monotonic() - t0, 2), b_runs=len(attempts))
for p in procs:
    p.terminate()

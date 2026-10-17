"""REPLAY A (BOSSAPP-571 PRD §11, spec §4.2, §13.6): BOSS fixes the held-port bug of run
wfr_f383649a8b024d97a80c by itself, on its OWN source as it was before the person's fix.

Opt-in (slow: it runs BOSS's real affected suites and P0 tier at the base, twice):
    BOSS_SELF_FIX_REPLAY=1 [BOSS_SELF_FIX_REPLAY_REPORT=path.json] pytest -q this file

THE SUBJECT is BOSS at 290a8215f (the parent of c56bd88a3), mirrored from this repository. The
ENGINE for Mender is the scripted conformance engine playing tests/e2e/fixtures/self-fix/
replay-a (its diagnosis, its red test, its fix): no model. The recovery decision comes from a stub
model answering as the real run's model did (wait_for_boss, citing the bind error).

REAL: the recovery loop and its evidence (the real run's failure and host state, neutralised),
BOSS's confidence, the claim, the recommendation, Mender's pipeline on BOSS's real source with
real git, BOSS's real tests at the base (red, green, affected suites, P0 tier), every gate, the
four review seats, the decision and its approval, the EdDSA ticket, the host self-installer
script, the stack ref, the resume, the verdict, the rollback.
FAKED: Docker (the installer's builds and restarts), the HTTP edge of the API (its routes call
the real engine functions), the restart itself (BOSS_STACK_HEAD set as the restarted process has
it), and the resumed deploy's plan/approve/apply: its OUTCOME is computed by running the
INSTALLED stack's own stack_deploy.apply_on_host against a model of the real host (the old app
container in the resources' project, res-db beside it) — port freed → the run's next real failure
(EAI_AGAIN res-db), port still held → the same failure again.

  test_replay_a_end_to_end      the whole loop → verified (a new cause: Replay B's res-db)
  test_replay_a_rollback_path   a host the fix does not hold on → same cause → rolled back
Oracle: c56bd88a3's own tests are run against BOSS's installed fix and reported.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.services import boss_recommendations, model_routing
from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_build
from boss_engine.services import step_recovery as sr
from boss_engine.services import workflow_capability_runner as runner

from ._self_fix_support import (FakeHost, LocalSigner, Worker, approve, clean_project, git,
                                self_fix_on)  # noqa: F401
from .test_a_failed_step_stops_the_run import _insert_awaited, _required, _steps
from .test_workflow_capability_runner import _publish, _start, eng, project  # noqa: F401

pytestmark = pytest.mark.skipif(os.environ.get("BOSS_SELF_FIX_REPLAY") != "1",
                                reason="Replay A runs only with BOSS_SELF_FIX_REPLAY=1 (slow)")

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "e2e" / "fixtures" / "self-fix" / "replay-a"
BASE = "290a8215f"
HUMAN_FIX = "c56bd88a3"
SHARED = "bossapp-example-app-deploy"
TARGET_DIR = "/opt/boss-apps/example-app"
#: The real run's 07:57 failure and host probe, with the install's names and address neutralised.
PORT_FAILURE = {
    "headline": "Port 8000 on 203.0.113.10 is already held by another container",
    "cause": "host", "step": "install: up",
    "action": "Free the port, or let BOSS retire this app's earlier release, then deploy again.",
    "log": ("Error response from daemon: driver failed programming external connectivity on "
            "endpoint example-app-app-1: Bind for 0.0.0.0:8000 failed: port is already "
            "allocated")}
RESDB_FAILURE = {
    "headline": "The app could not reach res-db, the service BOSS provisioned for it on "
                "203.0.113.10, so it exited at start.",
    "cause": "host", "step": "health",
    "action": "This is BOSS's network wiring, not the app. Update BOSS and deploy again.",
    "log": "Error: getaddrinfo EAI_AGAIN res-db\n    at GetAddrInfoReqWrap.onlookup"}
HOST_STATE = {"read": True, "address": "203.0.113.10", "target_dir": TARGET_DIR,
              "keep_project": None, "port": 8000, "own_projects": [],
              "port_holders": [{"name": f"{SHARED}-app-1", "project": SHARED, "own": True,
                                "state": "running", "own_project": False}],
              "own_containers": [{"name": f"{SHARED}-app-1", "project": SHARED,
                                  "state": "running", "ports": "0.0.0.0:8000->8000/tcp",
                                  "own": True}], "containers_seen": 3, "exited_logs": ""}


class RecoveryModel:
    """The recovery agent's model, as the real run's answered: BOSS's own bug."""

    def __init__(self):
        self._boss_choice = model_routing.ModelChoice("native", "connected", "replay")
        self._boss_route = None

    def create_chat_completion(self, messages, tools=None, **_):
        said = messages[1]["content"]
        if "EAI_AGAIN" in said:
            answer = {"action": "wait_for_boss", "cause": "the app cannot resolve res-db",
                      "reasoning": "BOSS's own wiring", "evidence": ["getaddrinfo EAI_AGAIN res-db"],
                      "boss_fix": {"title": "the app is handed the old database address",
                                   "root_cause": "BOSS's network wiring between projects",
                                   "proposed_change": "wire the networks",
                                   "files": ["engine/boss_engine/services/stack_deploy.py"]}}
        else:
            answer = {"action": "wait_for_boss",
                      "cause": "port 8000 is held by this app's old container in the "
                               "resources' project",
                      "reasoning": "the holder is this app's own container, but no allowed "
                                   "action can name its project (it also holds res-db)",
                      "evidence": ["Bind for 0.0.0.0:8000 failed: port is already allocated"],
                      "boss_fix": {"title": "a new release does not retire the app from the "
                                            "resources' project it left",
                                   "root_cause": "the retire step only knows the single-"
                                                 "container release",
                                   "proposed_change": "fix the single-container retire step "
                                                      "and widen own_projects",
                                   "files": ["engine/boss_engine/services/stack_deploy.py"]}}
        body = json.dumps(answer)
        return ({"content": body, "raw": {"role": "assistant", "content": body}},
                {"model": "replay-model", "cost_usd_micros": 0})


PROBE = r'''
import contextlib, json, os, subprocess, sys
from boss_engine.services import deployment_fleet, stack_deploy
SHARED, TARGET_DIR = sys.argv[1], sys.argv[2]
layout = json.loads(sys.argv[3])
MANIFEST = """version: 1
name: example-app
kind: compose
compose: {file: docker-compose.yml, project: example-app}
health: [{url: "http://127.0.0.1:8000/health"}]
"""
class R:
    def __init__(s, rows): s.rows = rows
    def all(s): return list(s.rows)
    def first(s): return s.rows[0] if s.rows else None
    def mappings(s): return s
    def scalar(s): return s.rows[0][0] if s.rows else None
class C:
    def execute(s, st, p=None):
        q = str(st)
        return R([(SHARED,)] if "shared_compose_project" in q and "stack_deployments" in q else [])
class E:
    @contextlib.contextmanager
    def begin(s): yield C()
captured = {}
class A:
    def __init__(s, **kw): captured["rendered"] = kw["rendered"]; s.steps, s.log = [], ""
    def run(s): return {"mode": "install", "open_url": ""}
dep = {"id": "sdep_resumed", "project_id": "p", "target_id": "t", "host_id": "h",
       "changeset_id": "cs", "commit_sha": "a" * 40, "ref": "main", "manifest_text": MANIFEST,
       "repository": "example/example-app", "manifest": {"workdir": ""},
       "code_task_run_id": None, "created_by": "replay", "apply_token": "tok"}
stack_deploy._row = lambda c, i, lock=False: dict(dep)
stack_deploy._require_owner = lambda d, t: None
stack_deploy._require_governed = lambda *a, **k: None
stack_deploy._target = lambda c, i: {"id": "t", "target_kind": "stack", "app_name": "example-app",
                                     "repo": "example/example-app", "domains": []}
deployment_fleet.get_host = lambda c, i: {"id": "h", "address": "203.0.113.10", "ssh_user": "deploy"}
stack_deploy.host_access_for = lambda *a, **k: type("X", (), {"key_material": "", "user": "d",
                                                                "host": "h"})()
stack_deploy.github_source_for = lambda *a, **k: object()
stack_deploy._environment = lambda *a, **k: ({}, [])
stack_deploy.app_deploy_env = lambda *a, **k: {}
stack_deploy.StackApplier = A
stack_deploy.apply_on_host(E(), None, deployment_id="sdep_resumed", apply_token="tok", dedupe_key="dk")
# The model of the real host: the old app container holds 8000 in the resources' project.
work = sys.argv[4]
db = os.path.join(work, "containers.txt")
with open(db, "w") as f:
    for cid, (proj, wd) in layout.items():
        f.write(f"{cid} {proj} {wd}\n")
removed = os.path.join(work, "removed.txt")
bin_dir = os.path.join(work, "bin"); os.makedirs(bin_dir, exist_ok=True)
with open(os.path.join(bin_dir, "docker"), "w") as f:
    f.write(f"""#!/bin/sh
DB={db}; RM={removed}
case "$1" in
  ps) proj=$(echo "$@" | sed -n 's/.*com.docker.compose.project=\\([^ ]*\\).*/\\1/p')
      awk -v p="$proj" '$2==p {{print $1}}' "$DB" ;;
  inspect) id=$(eval echo \\${{$#}})
      case "$3" in *Name*) echo "/$id" ;; *) awk -v i="$id" '$1==i {{print $3}}' "$DB" ;; esac ;;
  rm) id=$(eval echo \\${{$#}}); echo "$id" >> "$RM"; echo "$id" ;;
  *) : ;;
esac
""")
os.chmod(os.path.join(bin_dir, "docker"), 0o755)
env = {**os.environ, "PATH": bin_dir + ":" + os.environ["PATH"]}
names = []
for step in captured["rendered"]["install"]:
    names.append(step["name"])
    if step["name"] == "compose up" or ("compose" in step["run"] and " up" in step["run"]):
        break
    subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=60)
gone = open(removed).read().split() if os.path.exists(removed) else []
holder = "app1"
print(json.dumps({"steps": names, "removed": gone, "port_free": holder in gone}))
'''


def _resumed_outcome(stack_worktree: Path, layout: dict, tmp: Path) -> dict:
    """What the resumed deploy meets on the host, decided by the INSTALLED code."""
    work = tmp / f"probe-{uuid.uuid4().hex[:6]}"
    work.mkdir()
    env = {**os.environ, "PYTHONPATH": f"{stack_worktree}{os.pathsep}{stack_worktree / 'engine'}"}
    env.pop("BOSS_SCRIPTED_SELF_FIX", None)
    done = subprocess.run([sys.executable, "-c", PROBE, SHARED, TARGET_DIR, json.dumps(layout),
                           str(work)], capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


class Replay:
    """The world of one replay: BOSS's real source at the base, the test database, a host."""

    def __init__(self, eng, project_id, root: Path, monkeypatch):
        from boss_engine.config import get_settings
        from boss_engine.services import scripted_engine, self_fix_install, self_fix_source

        self.eng, self.project, self.root, self.mp = eng, project_id, root, monkeypatch
        self.repo = Path(subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=str(ROOT),
                                        capture_output=True, text=True).stdout.strip())
        if not self.repo.is_absolute():
            self.repo = (ROOT / self.repo).resolve()
        self.base = git(ROOT, "rev-parse", f"{BASE}^{{commit}}")
        self.mirror = root / "selfsrc.git"
        git(root, "clone", "-q", "--mirror", "--shared", str(self.repo), str(self.mirror))
        assert self_fix_source.file_exists(None, self.base,
                                           "engine/boss_engine/services/stack_deploy.py") or True
        self.settings = get_settings().model_copy(update={
            "self_fix_source": str(self.mirror), "self_fix_work_dir": str(root / "work")})
        monkeypatch.setenv("BOSS_SOURCE_SHA", self.base)
        monkeypatch.setenv("BOSS_RELEASE_REF", "v-replay-a")
        monkeypatch.delenv("BOSS_STACK_HEAD", raising=False)
        monkeypatch.setenv(scripted_engine.ENABLE_ENV, "1")
        monkeypatch.setenv("BOSS_SCRIPTED_SELF_FIX", f"good_fix:{FIXTURE}")
        test_db = os.environ.get("BOSS_TEST_DATABASE_URL")
        if test_db:
            monkeypatch.setenv("BOSS_SELF_FIX_TEST_DATABASE_URL", test_db)
        monkeypatch.setattr(sr, "_BUILD_ID", None)
        scripted_engine.reset()
        self.signer = LocalSigner()
        self._prev = self_fix_install.use_signer(self.signer)
        monkeypatch.setattr(sr, "routed_adapter",
                            lambda *a, **k: (lambda m: (m, m))(RecoveryModel()))
        monkeypatch.setattr(sr, "host_state", lambda *a, **k: dict(HOST_STATE))
        monkeypatch.setattr(sr, "_read_tools", lambda *a, **k: (None, None))
        if os.environ.get("BOSS_SELF_FIX_REPLAY_FAST") == "1":
            # Debugging the loop only: skip BOSS's affected suites and P0 tier (never for proof).
            from boss_engine.services import self_fix_source as src_mod

            monkeypatch.setattr(src_mod, "p0_tests", lambda work: [])
            monkeypatch.setattr(src_mod, "affected_tests", lambda work, changed: ([], []))
        self.timeline: list[tuple[float, str]] = []
        self.t0 = time.time()

    def mark(self, what: str) -> None:
        self.timeline.append((round(time.time() - self.t0, 1), what))

    def close(self):
        from boss_engine.services import scripted_engine, self_fix_install

        self_fix_install.use_signer(self._prev)
        scripted_engine.reset()
        clean_project(self.eng, self.project)
        _FIRST.pop(self.project, None)
        with self.eng.begin() as c:
            for table in ("stack_deployments", "deployment_targets", "deployment_hosts"):
                try:
                    with c.begin_nested():
                        c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"),  # noqa: S608
                                  {"p": self.project})
                except Exception:  # noqa: BLE001
                    pass


@pytest.fixture()
def replay(eng, project, self_fix_on, tmp_path, monkeypatch):
    r = Replay(eng, project, tmp_path, monkeypatch)
    yield r
    r.close()


_FIRST: dict[str, str] = {}


def _failed_deployment(c, project_id: str, failure: dict) -> str:
    """A failed stack deployment of the same target (each attempt is a new deployment row)."""
    deployment_id = f"sdep_{uuid.uuid4().hex[:12]}"
    first = _FIRST.get(project_id)
    if first is None:
        _insert_awaited(c, project_id, "stack_deployments", deployment_id, "failed")
        _FIRST[project_id] = deployment_id
    else:
        columns = [r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = "
            "'stack_deployments' AND table_schema = current_schema() AND column_name <> 'id'"))]
        cols = ", ".join(columns)
        c.execute(text(f"INSERT INTO stack_deployments (id, {cols}) SELECT :n, {cols} "  # noqa: S608
                       "FROM stack_deployments WHERE id = :f"), {"n": deployment_id, "f": first})
    c.execute(text("UPDATE stack_deployments SET error = CAST(:e AS JSONB), log_tail = :l, "
                   "steps = CAST(:s AS JSONB), host_id = NULL, failure_reason = :h WHERE id = :i"),
              {"i": deployment_id, "l": failure["log"], "h": failure["headline"],
               "e": json.dumps({k: failure[k] for k in ("headline", "cause", "step", "action")}),
               "s": json.dumps([{"name": "install: up", "status": "failed"}])})
    return deployment_id


def _deploy_run(c, project_id: str) -> tuple[str, str]:
    _, version = _publish(c, project_id, f"replay_{uuid.uuid4().hex[:6]}", [
        {"id": "deploy", "kind": "capability",
         "binding": {"capability": "deploy", "inputs": _required("deploy")},
         "next": []}], "deploy")
    run = _start(c, project_id, version["id"], {})
    return run["id"], c.execute(text("SELECT id FROM workflow_step_runs WHERE workflow_run_id = "
                                     ":r AND step_id = 'deploy'"), {"r": run["id"]}).scalar()


def _park_on(c, step_row: str, deployment_id: str, extra: dict | None = None) -> None:
    """Where the deploy capability leaves the step after planning and applying a release: waiting
    on its stack deployment (the plan/approve/apply in between is not this replay's subject)."""
    c.execute(text("UPDATE workflow_step_runs SET status = 'waiting', output = "
                   "(COALESCE(output, '{}'::jsonb) - 'await') || CAST(:o AS JSONB) WHERE id = :i"),
              {"i": step_row, "o": json.dumps({**(extra or {}), "capability": "deploy",
                                               "await": {"kind": "stack_deployment",
                                                         "id": deployment_id}})})


def _tick(r: Replay, times: int = 1) -> None:
    """The worker's tick. A deploy asks a person before it runs (one approval per deploy):
    that governance gate is approved here, as the person running the release would — it is not
    the self-fix's approval, which goes through the real decision path above."""
    for _ in range(times):
        with r.eng.begin() as c:
            c.execute(text("UPDATE decisions SET status = 'approved', approved_by = 'ceo' "
                           "WHERE status NOT IN ('approved', 'rejected', 'withdrawn') AND id IN "
                           "(SELECT decision_id FROM workflow_step_runs WHERE project_id = :p "
                           "AND status = 'waiting' AND decision_id IS NOT NULL) AND "
                           "COALESCE(recommendation->>'action', '') <> 'self_fix.install'"),
                      {"p": r.project})
            c.execute(text("UPDATE workflow_step_runs SET run_after = now() - interval '1 second' "
                           "WHERE project_id = :p AND status = 'ready'"), {"p": r.project})
            runner.tick(c, Worker(r.eng, r.settings), project_id=r.project)


def _oracle(stack_worktree: Path) -> dict:
    """c56bd88a3's own tests (a person's), run against BOSS's installed fix."""
    rel = "engine/tests/test_a_new_release_retires_the_app_from_its_old_shared_project.py"
    body = git(ROOT, "show", f"{HUMAN_FIX}:{rel}")
    (stack_worktree / rel).write_text(body + "\n")
    env = {**os.environ, "PYTHONPATH": f"{stack_worktree}{os.pathsep}{stack_worktree / 'engine'}"}
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           "--rootdir", str(stack_worktree), "-o", "pythonpath=engine", rel, "-rA"],
                          cwd=str(stack_worktree), capture_output=True, text=True, env=env,
                          timeout=300)
    (stack_worktree / rel).unlink()
    passed = [ln.split("::")[-1] for ln in done.stdout.splitlines() if ln.startswith("PASSED")]
    failed = [ln.split("::")[-1].split(" ")[0] for ln in done.stdout.splitlines()
              if ln.startswith("FAILED")]
    return {"passed": passed, "failed": failed}


def _deploy_on_the_running_build(r: Replay, host: "FakeHost", layout: dict, outcomes: list):
    """The deploy capability, as the resumed step runs it: on the build BOSS now runs (the
    installed stack), against the model of the real host. Its plan, approval and apply are the
    stack-deploy tests' subject; what the host answers is decided by the INSTALLED code."""
    original = runner._perform_capability

    def perform(connection, ctx, *, project_id, actor, step, context, key, capability, inputs):
        if str(step.get("id")) != "deploy":
            return original(connection, ctx, project_id=project_id, actor=actor, step=step,
                            context=context, key=key, capability=capability, inputs=inputs)
        assert os.environ.get("BOSS_STACK_HEAD"), "re-deployed before BOSS ran its fix"
        outcome = _resumed_outcome(host.dir / ".boss" / "stack-worktree", layout, r.root)
        outcomes.append(outcome)
        deployment = _failed_deployment(connection, project_id,
                                        RESDB_FAILURE if outcome["port_free"] else PORT_FAILURE)
        before = connection.execute(text(
            "SELECT output FROM workflow_step_runs WHERE workflow_run_id = :r AND step_id = "
            "'deploy'"), {"r": context.get("run_id")}).scalar() or {}
        return {"capability": "deploy", "tool": None, "status": "awaiting",
                "await": {"kind": "stack_deployment", "id": deployment},
                sr.STATE_KEY: sr.state_of(before)}

    return perform


def _run_replay(r: Replay, monkeypatch, *, layout: dict) -> dict:
    host = FakeHost(r)
    outcomes: list = []
    monkeypatch.setattr(runner, "_perform_capability",
                        _deploy_on_the_running_build(r, host, layout, outcomes))
    with r.eng.begin() as c:
        run_id, step_row = _deploy_run(c, r.project)
        first = _failed_deployment(c, r.project, PORT_FAILURE)
        _park_on(c, step_row, first)
    r.mark("deploy failed: port 8000 held by the earlier release's app container")
    _tick(r)
    with r.eng.begin() as c:
        step = _steps(c, run_id)["deploy"]
        sfx_id = step["output"].get("self_fix_id")
        assert sfx_id, step["output"]
        sfx = sf.get(c, sfx_id)
    r.mark(f"recovery: {sr.last_decision(step['output'][sr.STATE_KEY]).get('action')} → "
           f"self-fix {sfx_id} claimed ({sfx['confidence']})")
    built = self_fix_build.Build(engine=r.eng, settings=r.settings, sfx_id=sfx_id,
                                 source=r.mirror).run()
    with r.eng.begin() as c:
        sfx = sf.get(c, sfx_id)
    assert sfx["status"] == sf.AWAITING_APPROVAL, (built, sfx.get("refusal_detail"))
    r.mark(f"Mender: red on {BASE} → green; gates "
           f"{sorted(k for k, v in sfx['gates'].items() if v.get('ok'))}; tier {sfx['tier']} "
           f"flags {sfx['flags']}")
    approve(r, sfx_id)
    r.mark("a person approved (tier %s)" % sfx["tier"])
    try:
        done = host.installer("watch", "--once")
        assert "installed" in done.stdout, done.stdout + done.stderr
        with r.eng.begin() as c:
            sfx = sf.get(c, sfx_id)
        assert sfx["status"] == sf.INSTALLED
        r.mark(f"installed: refs/boss/stack/v-replay-a → {sfx['fix_commit_sha'][:12]}")
        monkeypatch.setenv("BOSS_STACK_HEAD", sfx["fix_commit_sha"])
        monkeypatch.setattr(sr, "_BUILD_ID", None)
        _tick(r)
        with r.eng.begin() as c:
            assert sf.get(c, sfx_id)["status"] == sf.VERIFYING
        r.mark("BOSS restarted into the stack; the deploy step resumed")
        _tick(r, times=5)
        if os.environ.get("BOSS_SELF_FIX_REPLAY_DEBUG"):
            with r.eng.begin() as c:
                print("DEBUG step", json.dumps(_steps(c, run_id)["deploy"], default=str)[:3000])
                print("DEBUG sfx", json.dumps(sf.get(c, sfx_id)["verification"], default=str))
        assert outcomes, "the resumed step ran the deploy again"
        outcome = outcomes[0]
        r.mark(f"the resumed deploy on the installed code: removed {outcome['removed']} before "
               f"compose up; port free: {outcome['port_free']}")
        oracle = _oracle(host.dir / ".boss" / "stack-worktree")
        with r.eng.begin() as c:
            sfx = sf.get(c, sfx_id)
        result = {"self_fix": sfx_id, "status_after_resume": sfx["status"],
                  "verification": sfx.get("verification"), "outcome": outcome, "oracle": oracle,
                  "host": host}
        if sfx["status"] == sf.ROLLING_BACK:
            done = host.installer("watch", "--once")
            assert "rolled_back" in done.stdout, done.stdout + done.stderr
            with r.eng.begin() as c:
                sfx = sf.get(c, sfx_id)
            r.mark(f"rolled back: {host.running_head()[:12]} runs again; stack ref "
                   f"{host.stack_ref() or 'removed'}")
        result["final_status"] = sfx["status"]
        with r.eng.begin() as c:
            result["recommendation"] = boss_recommendations.list_recommendations(c, r.project)
            result["events"] = [row[0] for row in c.execute(text(
                "SELECT event_type FROM events WHERE data->>'self_fix_id' = :i ORDER BY "
                "created_at"), {"i": sfx_id})]
            result["self_fixes"] = [dict(x) for x in c.execute(text(
                "SELECT id, signature, status FROM self_fixes WHERE project_id = :p ORDER BY "
                "created_at"), {"p": r.project}).mappings()]
            result["row"] = sf.get(c, sfx_id)
        return result
    finally:
        host.close()


def _report(r: Replay, name: str, result: dict) -> None:
    path = os.environ.get("BOSS_SELF_FIX_REPLAY_REPORT")
    if not path:
        return
    row = result["row"]
    entry = {"replay": name, "timeline": r.timeline, "final_status": result["final_status"],
             "verification": result.get("verification"), "outcome": result["outcome"],
             "oracle": result["oracle"], "tier": row["tier"], "flags": row["flags"],
             "gates": {k: v.get("ok") for k, v in row["gates"].items()},
             "red": (row["red_green"].get("base") or {}).get("output_tail", "")[-600:],
             "affected": (row["gates"].get("affected_suites") or {}).get("detail", {}).get(
                 "tests"),
             "p0": (row["gates"].get("p0_tier") or {}).get("detail", {}).get("tests"),
             "events": result["events"], "self_fixes": result["self_fixes"],
             "recommendation_status": [x["status"] for x in result["recommendation"]],
             "diff_stat": row["diff_stat"], "commit": row["fix_commit_sha"],
             "diagnosis_rejected": row["diagnosis"].get("rejected")}
    existing = json.loads(Path(path).read_text()) if Path(path).exists() else []
    Path(path).write_text(json.dumps(existing + [entry], indent=1, default=str))


REAL_HOST = {"app1": (SHARED, f"{TARGET_DIR}/releases/0f3c/example-app"),
             "resdb": (SHARED, "/opt/boss-infra/example-app/deploy"),
             "otherapp": ("bossapp-other-deploy", "/opt/boss-apps/other/releases/9")}


def test_replay_a_end_to_end(replay, monkeypatch):
    result = _run_replay(replay, monkeypatch, layout=REAL_HOST)
    _report(replay, "A: port held → verified", result)
    assert result["outcome"]["port_free"] and result["outcome"]["removed"] == ["app1"]
    assert result["final_status"] == sf.VERIFIED
    assert result["verification"]["outcome"] == "new_cause"
    assert result["verification"]["next_signature"] == "deploy:unwired:res-db"
    assert result["recommendation"][0]["status"] == "implemented"
    assert any(s["signature"] == "deploy:unwired:res-db" for s in result["self_fixes"]), \
        "the next cause (Replay B) starts its own self-fix"
    row = result["row"]
    assert row["tier"] == 3 and "host_effect" in row["flags"], "host commands: tier 3 (decision 5)"
    assert all(v.get("ok") for v in row["gates"].values()), row["gates"]


def test_replay_a_rollback_path(replay, monkeypatch):
    """The same fix, on a host where the old container was started from another directory:
    the retire's working-dir guard (rightly) keeps it, the port stays held, the same cause comes
    back — BOSS puts the previous build back and reopens the recommendation."""
    layout = {**REAL_HOST, "app1": (SHARED, "/srv/old-layout/example-app")}
    result = _run_replay(replay, monkeypatch, layout=layout)
    _report(replay, "A: port still held → rolled back", result)
    assert not result["outcome"]["port_free"]
    assert result["final_status"] == sf.ROLLED_BACK
    assert result["verification"]["outcome"] == "same_cause"
    rec = result["recommendation"][0]
    assert rec["status"] == "open" and rec["attempts"]

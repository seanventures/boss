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

ON A REAL ENGINE (the live matrix, spec §9): BOSS_SELF_FIX_REPLAY_ENGINE=<harness key> (e.g.
claude_code) makes Mender that engine — no scripted fixture, no scripted engine: the model writes
the diagnosis, the red test and the fix itself, and BOSS decides everything else exactly as on
the scripted engine. BOSS_SELF_FIX_REPLAY_MODEL picks the harness's default model;
BOSS_SELF_FIX_REPLAY_BUILD_MODEL / _REVIEW_MODEL / _SECURITY_MODEL pick Mender's stages', the review
seats' and the security seat's (BOSS_SELF_FIX_REPLAY_REVIEW_ENGINE puts the security seat on a
second engine). A
build that does not reach the card is REPORTED (refusal class, detail, cost, time) before the
test fails: a real engine not producing a fix is a quality result, not something to hide.
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
from sqlalchemy import create_engine
from boss_engine.config import get_settings
from boss_engine.services import stack_deploy
SHARED, TARGET_DIR, PROJECT, TARGET = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
layout = json.loads(sys.argv[5])
work = sys.argv[6]
MANIFEST = """version: 1
name: example-app
kind: compose
compose: {file: docker-compose.yml, project: example-app}
health: [{url: "http://127.0.0.1:8000/health"}]
"""
# THE DATABASE IS REAL: the replay's test database, where this target's EARLIER release is on
# record as having run in the resources' shared project (seeded by the replay). Whatever the
# installed code reads, at plan or at apply, it reads from real rows.
engine = create_engine(os.environ["REPLAY_PROBE_DATABASE_URL"])


class Planned(Exception):
    pass


class Capture:
    """The plan's own INSERT of the deployment is captured and its transaction rolled back: the
    deployment is exactly what the INSTALLED plan records, and nothing is written."""

    def __init__(self, inner):
        self.inner, self.row = inner, None

    @contextlib.contextmanager
    def begin(self):
        with self.inner.begin() as c:
            yield Conn(c, self)

    def connect(self):
        return self.inner.connect()


class Conn:
    def __init__(self, c, cap):
        self.c, self.cap = c, cap

    def execute(self, statement, *a, **k):
        if "INSERT INTO stack_deployments" in str(statement):
            self.cap.row = dict(a[0] if a else k.get("parameters") or {})
            raise Planned()
        return self.c.execute(statement, *a, **k)

    def __getattr__(self, name):
        return getattr(self.c, name)


class GitHub:
    def resolve_commit(self, owner, repo, ref):
        return "a" * 40

    def read_file(self, owner, repo, path, commit):
        return MANIFEST if path.endswith("boss.deploy.yaml") else "services: {}\n"


class Access:
    key_material, user, host = "", "deploy", "h"

    def run(self, *a, **k):
        raise AssertionError("the probe never runs anything on a host")


# THE EDGES OUTSIDE BOSS (GitHub, ssh, the budget, the approval card) and the plan's checks of
# the APP (its secrets, its manifest's fit, the deploy guard), none of them this replay's
# subject. Everything that decides what is retired runs as installed, on real rows.
from boss_engine.services import budget, deploy_policy, governed_host_deploy, resource_plane
from boss_engine.services import run_decisions
from boss_engine.templates import deploy_guard

stack_deploy.github_source_for = lambda *a, **k: GitHub()
stack_deploy.host_access_for = lambda *a, **k: Access()
stack_deploy.preflight_host = lambda *a, **k: []
stack_deploy.manifest_misfit = lambda *a, **k: None
stack_deploy._plan_infrastructure = lambda *a, **k: (None, None)
stack_deploy.create_blocking_decision = lambda *a, **k: "dec_probe"
deploy_guard.deploy_refusal = lambda *a, **k: None
governed_host_deploy.app_secret_problem = lambda *a, **k: (None, [], None)
budget.BudgetGovernor.reserve = lambda self, *a, **k: {"status": "ok",
                                                       "reservation_id": "res_probe"}


class RP:
    def __init__(self, *a, **k):
        pass

    def create_changeset(self, *a, **k):
        return {"id": "cs_probe"}

    def required_approvers_for_changeset(self, *a, **k):
        return []


resource_plane.ResourcePlaneService = RP
run_decisions.open_step_decision = lambda *a, **k: "dec_probe"
deploy_policy.pre_approval = lambda *a, **k: None
settings = get_settings()

# 1. THE RESUMED STEP PLANS AGAIN, on the installed code.
cap = Capture(engine)
try:
    stack_deploy.plan_stack_deploy(cap, settings, project_id=PROJECT, target_id=TARGET,
                                   repository="example/example-app", ref="main", actor="replay")
    raise SystemExit("the plan returned without recording a deployment")
except Planned:
    pass
row = cap.row
dep = {"id": row["i"], "project_id": PROJECT, "target_id": TARGET, "host_id": row["h"],
       "changeset_id": row["cs"], "commit_sha": row["c"], "ref": row["ref"],
       "manifest_text": row["mt"], "repository": row["repo"], "manifest": json.loads(row["m"]),
       "code_task_run_id": None, "created_by": "replay", "apply_token": "tok",
       "deploy_dir": row.get("dir")}

# 2. ITS APPROVED APPLY, on the installed code, against the same real rows.
captured = {}


class Applier:
    def __init__(self, **kw):
        captured["rendered"] = kw["rendered"]
        self.steps, self.log = [], ""

    def run(self):
        return {"mode": "install", "open_url": ""}


stack_deploy._row = lambda c, i, lock=False: dict(dep)
stack_deploy._require_owner = lambda d, t: None
stack_deploy._require_governed = lambda *a, **k: None
stack_deploy._environment = lambda *a, **k: ({}, [])
stack_deploy.app_deploy_env = lambda *a, **k: {}
stack_deploy.StackApplier = Applier
stack_deploy.apply_on_host(engine, settings, deployment_id=dep["id"], apply_token="tok",
                           dedupe_key="dk")

# 3. THE MODEL OF THE REAL HOST: the old app container holds 8000 in the resources' project.
# The Docker CLI is modelled by tests/e2e/fixtures/self-fix/fake_docker.py: real-shaped ids,
# labels, ps filters and inspect templates, so a fix is judged by what it would do on a host,
# whichever (correct) docker commands it chooses.
import hashlib
containers = [{"id": hashlib.sha256(name.encode()).hexdigest(), "name": name,
               "labels": {"com.docker.compose.project": proj,
                          "com.docker.compose.project.working_dir": wd,
                          "com.docker.compose.service": name.rstrip("0123456789")},
               "ports": [8000] if name == "app1" else [], "state": "running"}
              for name, (proj, wd) in layout.items()]
state = os.path.join(work, "docker-state.json")
with open(state, "w") as f:
    json.dump({"containers": containers, "log": []}, f)
bin_dir = os.path.join(work, "bin")
os.makedirs(bin_dir, exist_ok=True)
with open(os.path.join(bin_dir, "docker"), "w") as f:
    f.write(f"#!/bin/sh\nexec {sys.executable} {os.environ['REPLAY_FAKE_DOCKER']} \"$@\"\n")
os.chmod(os.path.join(bin_dir, "docker"), 0o755)
env = {**os.environ, "PATH": bin_dir + ":" + os.environ["PATH"], "FAKE_DOCKER_STATE": state}
names, failed_steps = [], []
for step in captured["rendered"]["install"]:
    names.append(step["name"])
    if step["name"] == "compose up" or ("compose" in step["run"] and " up" in step["run"]):
        break
    ran = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True,
                         timeout=60)
    if ran.returncode != 0:
        failed_steps.append(step["name"])
log = json.load(open(state))["log"]
gone = [e["name"] for e in log if e["op"].startswith(("rm", "compose down"))]
print(json.dumps({"steps": names, "removed": gone, "port_free": "app1" in gone,
                  "volumes": any("-v" in e["op"].split() for e in log), "log": log,
                  "failed_steps": failed_steps,
                  "planned": sorted(k for k in dep["manifest"] if k != "steps")}))
'''


def _seed_host(eng, project_id: str) -> str:
    """The real rows a resumed deploy plans against: a verified host, this app's stack target,
    and the target's EARLIER release, which ran in the resources' shared project."""
    suffix = uuid.uuid4().hex[:8]
    host_id, target_id, earlier = f"host_{suffix}", f"deptgt_{suffix}", f"sdep_{suffix}"
    with eng.begin() as c:
        _insert_awaited(c, project_id, "deployment_hosts", host_id, "verified")
        c.execute(text("UPDATE deployment_hosts SET name = :n, address = '203.0.113.10', "
                       "ssh_user = 'deploy' WHERE id = :i"),
                  {"i": host_id, "n": f"example-host-{suffix}"})
        # Its own environment name: (project, app, environment) is unique, and a replay plans
        # more than once.
        c.execute(text("INSERT INTO deployment_targets (id, status, project_id, app_name, repo, "
                       "environment, target_kind, target_ref, created_by, host_id) VALUES "
                       "(:i, 'active', :p, 'example-app', 'example/example-app', :e, 'stack', "
                       "'example-app', 'replay', :h)"),
                  {"i": target_id, "p": project_id, "h": host_id, "e": f"replay-{suffix}"})
        # A RELEASE THAT REALLY RAN: applied, finished healthy, its steps done (a fix may rightly
        # look only at releases that were applied; attempt 6 did).
        c.execute(text("INSERT INTO stack_deployments (id, project_id, target_id, host_id, "
                       "repository, ref, commit_sha, manifest, status, created_by, deploy_dir, "
                       "steps, open_url, apply_started_at, finished_at, created_at, updated_at) "
                       "VALUES (:i, :p, :t, :h, 'example/example-app', 'main', :c, "
                       "CAST(:m AS JSONB), 'healthy', 'replay', :d, CAST(:s AS JSONB), "
                       "'http://203.0.113.10:8000', now() - interval '1 day', "
                       "now() - interval '1 day' + interval '2 minutes', "
                       "now() - interval '1 day' - interval '1 minute', "
                       "now() - interval '1 day' + interval '2 minutes')"),
                  {"i": earlier, "p": project_id, "t": target_id, "h": host_id, "c": "b" * 40,
                   "d": TARGET_DIR,
                   "m": json.dumps({"shared_compose_project": SHARED, "workdir": ""}),
                   "s": json.dumps([{"name": n, "status": "succeeded"} for n in
                                    ("prepare", "upload", "environment", "switch",
                                     "install or update: up", "health", "finalize")])})
    return target_id


def _resumed_outcome(stack_worktree: Path, layout: dict, tmp: Path, *, eng, project_id: str
                     ) -> dict:
    """What the resumed deploy meets on the host, decided by the INSTALLED code: it plans again
    and applies, as a real resumed step does, against real rows and a model of the real host."""
    work = tmp / f"probe-{uuid.uuid4().hex[:6]}"
    work.mkdir()
    target_id = _seed_host(eng, project_id)
    env = {**os.environ, "PYTHONPATH": f"{stack_worktree}{os.pathsep}{stack_worktree / 'engine'}",
           "REPLAY_PROBE_DATABASE_URL": eng.url.render_as_string(hide_password=False),
           "REPLAY_FAKE_DOCKER": str(ROOT / "tests" / "e2e" / "fixtures" / "self-fix" /
                                     "fake_docker.py")}
    env.pop("BOSS_SCRIPTED_SELF_FIX", None)
    done = subprocess.run([sys.executable, "-c", PROBE, SHARED, TARGET_DIR, project_id,
                           target_id, json.dumps(layout), str(work)], capture_output=True,
                          text=True, env=env, timeout=180)
    assert done.returncode == 0, done.stderr[-3000:]
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
        self.real_engine = (os.environ.get("BOSS_SELF_FIX_REPLAY_ENGINE") or "").strip()
        if self.real_engine:
            # Mender is a real model: nothing scripted anywhere in the build.
            monkeypatch.delenv(scripted_engine.ENABLE_ENV, raising=False)
            monkeypatch.delenv("BOSS_SCRIPTED_SELF_FIX", raising=False)
            monkeypatch.setenv("BOSS_SELF_FIX_ENGINE", self.real_engine)
            monkeypatch.setenv("BOSS_SELF_FIX_REVIEW_ENGINE",
                               os.environ.get("BOSS_SELF_FIX_REPLAY_REVIEW_ENGINE")
                               or self.real_engine)
            # A model per lane (spec §17.1): Mender's stages, the review seats, the security seat.
            for lane, env in (("BUILD_MODEL", "BOSS_SELF_FIX_MODEL"),
                              ("REVIEW_MODEL", "BOSS_SELF_FIX_REVIEW_MODEL"),
                              ("SECURITY_MODEL", "BOSS_SELF_FIX_SECURITY_MODEL")):
                value = (os.environ.get(f"BOSS_SELF_FIX_REPLAY_{lane}") or "").strip()
                if value:
                    monkeypatch.setenv(env, value)
                else:
                    monkeypatch.delenv(env, raising=False)
            update = {}
            model = (os.environ.get("BOSS_SELF_FIX_REPLAY_MODEL") or "").strip()
            if model:
                update.update(claude_cli_model=model, code_agent_model=model)
            # The test database holds no stored login, and the host's own secret store is the
            # live install's: a `claude` child here runs on the host CLI's own login ("none"
            # binds no credential into its environment) unless told otherwise.
            update["claude_credential_kind"] = (
                os.environ.get("BOSS_SELF_FIX_REPLAY_CREDENTIAL") or "none").strip()
            self.settings = self.settings.model_copy(update=update)
        else:
            monkeypatch.setenv(scripted_engine.ENABLE_ENV, "1")
            monkeypatch.setenv("BOSS_SCRIPTED_SELF_FIX", f"good_fix:{FIXTURE}")
        cap = (os.environ.get("BOSS_SELF_FIX_REPLAY_ATTEMPTS_CAP") or "").strip()
        if cap:
            # An operator who raised the per-cause cap (a setting, not a gate) so that a cause
            # with recorded attempts may be tried again.
            self.settings = self.settings.model_copy(
                update={"self_fix_attempts_per_signature": int(cap)})
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


#: THE BEHAVIOUR ORACLE's hosts: what c56bd88a3's own tests pinned, as behaviour on a host
#: rather than as its function names. Each: (layout, the containers that must go).
ORACLE_HOSTS = {
    # The real run: the app's old container holds 8000 in the resources' project, beside res-db.
    "the real host": (
        {"app1": (SHARED, f"{TARGET_DIR}/releases/0f3c/example-app"),
         "resdb": (SHARED, "/opt/boss-infra/example-app/deploy"),
         "otherapp": ("bossapp-other-deploy", "/opt/boss-apps/other/releases/9")}, ["app1"]),
    # A look-alike directory (example-app-two) in the same project, and another app.
    "a look-alike and another app": (
        {"app1": (SHARED, f"{TARGET_DIR}/releases/0f3c/example-app"),
         "resdb": (SHARED, "/opt/boss-infra/example-app/deploy"),
         "lookalike": (SHARED, "/opt/boss-apps/example-app-two/releases/1"),
         "otherapp": ("bossapp-other-deploy", "/opt/boss-apps/other/releases/9")}, ["app1"]),
    # Nothing of the app is left in the shared project: nothing happens, and nothing fails.
    "nothing of the app left": (
        {"resdb": (SHARED, "/opt/boss-infra/example-app/deploy")}, []),
}


def behaviour_oracle(stack_worktree: Path, *, eng, project_id: str, tmp: Path) -> dict:
    """c56bd88a3's tests as BEHAVIOUR, run by the corrected probe (plan again, apply, a model of
    the Docker CLI) against the installed code: at the next release the earlier shared project
    is retired of this app's container BEFORE compose up, so the port is free; only that
    container goes; res-db, a look-alike directory, another app and every volume are
    untouched; with nothing of the app there nothing is removed and no step fails."""
    cases = {}
    for name, (layout, must_go) in ORACLE_HOSTS.items():
        out = _resumed_outcome(stack_worktree, layout, tmp, eng=eng, project_id=project_id)
        checks = {
            "retired before compose up": sorted(out["removed"]) == sorted(must_go),
            "port freed": (out["port_free"] or "app1" not in layout),
            "nothing else touched": not (set(out["removed"]) - set(must_go)),
            "no volume": not out["volumes"],
            "no step failed": not out.get("failed_steps"),
        }
        cases[name] = {"ok": all(checks.values()), "checks": checks,
                       "removed": out["removed"], "steps": out["steps"]}
    return {"ok": all(c["ok"] for c in cases.values()), "cases": cases}


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


def _seed_history(r: "Replay") -> list[str]:
    """EARLIER ATTEMPTS AT THIS CAUSE, from BOSS_SELF_FIX_REPLAY_HISTORY (a JSON list of real,
    recorded attempts: their status, refusal, diagnosis, gates, reviewers' findings, diff when it
    was kept), put on this replay's record as refused self-fixes of the same cause class, as an
    install that had tried before would hold them. Seeded BEFORE the claim, so the attempts cap
    counts them as it would."""
    path = (os.environ.get("BOSS_SELF_FIX_REPLAY_HISTORY") or "").strip()
    if not path:
        return []
    klass = sf.class_signature("deploy:port_held:8000")
    ids = []
    with r.eng.begin() as c:
        for n, rec in enumerate(json.loads(Path(path).read_text())):
            sid = f"sfx_hist{uuid.uuid4().hex[:10]}"
            _insert_awaited(c, r.project, "self_fixes", sid, rec.get("status") or "refused")
            c.execute(text(
                "UPDATE self_fixes SET signature = 'deploy:port_held:8000', class_signature = :k, "
                "step_kind = 'deploy', refusal = :rf, refusal_detail = :rd, "
                "diagnosis = CAST(:d AS JSONB), gates = CAST(:g AS JSONB), "
                "review = CAST(:rv AS JSONB), red_green = CAST(:rg AS JSONB), "
                "created_at = now() - make_interval(hours => :h) WHERE id = :i"),
                {"i": sid, "k": klass, "rf": rec.get("refusal"), "rd": rec.get("refusal_detail"),
                 "d": json.dumps(rec.get("diagnosis") or {}), "g": json.dumps(rec.get("gates") or {}),
                 "rv": json.dumps(rec.get("review") or {}),
                 "rg": json.dumps(rec.get("red_green") or {}), "h": n + 1})
            ids.append(sid)
    return ids


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
        try:
            outcome = _resumed_outcome(host.dir / ".boss" / "stack-worktree", layout, r.root,
                                       eng=r.eng, project_id=project_id)
        except BaseException as error:  # said, or the runner's own handling hides it
            import traceback

            print("PROBE FAILED", "".join(traceback.format_exception(error))[-3000:])
            raise
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
    seeded = _seed_history(r)
    if seeded:
        r.mark(f"history: {len(seeded)} earlier attempt(s) at this cause on the record")
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
        read = c.execute(text(
            "SELECT data->>'sentence' FROM events WHERE event_type = 'self_fix.progress' AND "
            "data->>'self_fix_id' = :i AND data->>'sentence' LIKE '%earlier attempt%'"),
            {"i": sfx_id}).scalar()
    if read:
        r.mark(read)
    r.mark(f"Mender build ended {sfx['status']} (engine {sfx.get('engine')}, "
           f"${int(sfx.get('cost_micros') or 0) / 1e6:.2f}, rounds {sfx.get('repair_rounds')})")
    if sfx["status"] != sf.AWAITING_APPROVAL:
        _report_build(r, sfx, built)
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
        oracle = behaviour_oracle(host.dir / ".boss" / "stack-worktree", eng=r.eng,
                                  project_id=r.project, tmp=r.root)
        r.mark(f"behaviour oracle: {'PASS' if oracle['ok'] else 'FAIL'} "
               f"{ {k: v['ok'] for k, v in oracle['cases'].items()} }")
        # c56bd88a3's own tests, as written (they name its functions), kept for the record.
        oracle["name_oracle"] = _oracle(host.dir / ".boss" / "stack-worktree")
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


def _seats(sfx: dict) -> dict:
    """Who did what (engine and model per stage and seat), the reviewers' verdicts, and whether
    the entry-point gate applied: the real-engine matrix's columns."""
    review = sfx.get("review") or {}
    return {"lanes": review.get("lanes"),
            "verdicts": {k: {"verdict": v.get("verdict"), "open": v.get("open")}
                         for k, v in (review.get("seats") or {}).items()},
            "entry_gate": ((sfx.get("gates") or {}).get("drives_entry_point") or {}).get("detail")}


def _report_build(r: Replay, sfx: dict, built: dict) -> None:
    """A build that stopped before the card, said in full (the real-engine matrix's row)."""
    path = os.environ.get("BOSS_SELF_FIX_REPLAY_REPORT")
    if not path:
        return
    entry = {"replay": "A: build stopped", "engine": sfx.get("engine"),
             "timeline": r.timeline, "final_status": sfx["status"], "refusal": sfx.get("refusal"),
             "refusal_detail": sfx.get("refusal_detail"), "built": built,
             "cost_micros": sfx.get("cost_micros"), "repair_rounds": sfx.get("repair_rounds"),
             "diagnosis": sfx.get("diagnosis"),
             "gates": {k: v.get("ok") for k, v in (sfx.get("gates") or {}).items()},
             "red": (sfx.get("red_green") or {}).get("base"),
             "green": (sfx.get("red_green") or {}).get("head"),
             "test_files": (sfx.get("red_green") or {}).get("test_files"),
             "red_attempts": (sfx.get("red_green") or {}).get("attempts"),
             "attempt_diff": ((sfx.get("review") or {}).get("attempt") or {}).get("diff"),
             "progress": sfx.get("progress"), **_seats(sfx)}
    existing = json.loads(Path(path).read_text()) if Path(path).exists() else []
    Path(path).write_text(json.dumps(existing + [entry], indent=1, default=str))


def _report(r: Replay, name: str, result: dict) -> None:
    path = os.environ.get("BOSS_SELF_FIX_REPLAY_REPORT")
    if not path:
        return
    row = result["row"]
    entry = {"replay": name, "engine": row.get("engine"), "cost_micros": row.get("cost_micros"),
             "repair_rounds": row.get("repair_rounds"), "diff": (row.get("pr") or {}).get("diff"),
             "timeline": r.timeline, "final_status": result["final_status"],
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
             "diagnosis_rejected": row["diagnosis"].get("rejected"), **_seats(row)}
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
    assert result["oracle"]["ok"], result["oracle"]
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


@pytest.mark.parametrize("commit, freed", [(HUMAN_FIX, True), (BASE, False)])
def test_the_probe_judges_a_plan_time_fix_fairly(eng, project, tmp_path, commit, freed):
    """THE HARNESS, not BOSS: the resumed deploy is judged by the installed code planning again
    and applying, as a real resumed step does. The person's fix c56bd88a3 decides at PLAN time
    (retire_app_in on the recorded manifest) and must free the port here; the base must not."""
    tree = tmp_path / "tree"
    git(ROOT, "worktree", "add", "-q", "--detach", str(tree), commit)
    try:
        out = _resumed_outcome(tree, REAL_HOST, tmp_path, eng=eng, project_id=project)
    finally:
        git(ROOT, "worktree", "remove", "--force", str(tree))
        with eng.begin() as c:
            for table in ("stack_deployments", "deployment_targets", "deployment_hosts"):
                c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"),  # noqa: S608
                          {"p": project})
    assert out["port_free"] is freed, out
    assert out["removed"] == (["app1"] if freed else []), \
        "res-db and another app's container are never touched"
    assert not out["volumes"], "never a volume"
    if freed:
        assert "retire_app_in" in out["planned"], "decided at plan time, read back at apply"


@pytest.mark.parametrize("commit, passes", [(HUMAN_FIX, True), (BASE, False)])
def test_the_behaviour_oracle_passes_the_person_s_fix_and_fails_the_base(eng, project,
                                                                         tmp_path, commit,
                                                                         passes):
    tree = tmp_path / "tree"
    git(ROOT, "worktree", "add", "-q", "--detach", str(tree), commit)
    try:
        out = behaviour_oracle(tree, eng=eng, project_id=project, tmp=tmp_path)
    finally:
        git(ROOT, "worktree", "remove", "--force", str(tree))
        with eng.begin() as c:
            for table in ("stack_deployments", "deployment_targets", "deployment_hosts"):
                c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"),  # noqa: S608
                          {"p": project})
    assert out["ok"] is passes, out

"""BOSSAPP-825: project toolchain images, built interactively.

Covers, against a disposable Postgres (`boss_test_825`, created here and dropped
after — BOSS_TEST_PG_URL or BOSS_TEST_DATABASE_URL names the server; without one
the database tests skip):

* the schema block is idempotent (applied twice) and sits after every other
  `-- BOSSAPP-*` block;
* the capture state machine: start -> exec -> capture -> ready | failed,
  discard, one open session per project, local-daemon and registry captures;
* the refusal when BOSS_SANDBOX_COMMAND_RUNTIME is not docker;
* ContainerCommandRunner choosing the project image vs the default (a fake
  docker CLI), with the no-image case byte-identical to before.
"""
from __future__ import annotations

import os
import subprocess
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import container_runner, toolchain_images as tci

ROOT = Path(__file__).resolve().parents[2]
DB_NAME = "boss_test_825"
DIGEST = "sha256:" + "ab" * 32
PUSHED = "sha256:" + "cd" * 32


def _block(start: str, end: str) -> str:
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    return sql[sql.index(start):sql.index(end)]


def _toolchain_block() -> str:
    return _block("-- BOSSAPP-825\n", "-- ================================ end TOOLCHAIN IMAGES (BOSSAPP-825)")


def _environments_block() -> str:
    return _block("-- EXECUTION ENVIRONMENTS (BOSSAPP-824 phase 1)",
                  "-- ================================ end EXECUTION ENVIRONMENTS (BOSSAPP-824)")


def _statements(sql: str) -> list[str]:
    from boss_engine.db import split_statements

    return split_statements(sql)


def _admin_url() -> str:
    url = os.environ.get("BOSS_TEST_PG_URL") or os.environ.get("BOSS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("no BOSS_TEST_PG_URL / BOSS_TEST_DATABASE_URL (a disposable Postgres)")
    url = url.replace("postgresql+psycopg://", "postgresql://")
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))


@pytest.fixture(scope="module")
def pg():
    psycopg = pytest.importorskip("psycopg")
    from sqlalchemy import create_engine

    admin = _admin_url()
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{DB_NAME}"')
    target = admin.rsplit("/", 1)[0] + f"/{DB_NAME}"
    with psycopg.connect(target, autocommit=True) as c:
        c.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
        c.execute("CREATE TABLE code_task_runs (id TEXT PRIMARY KEY, project_id TEXT)")
        for statement in _statements(_environments_block()):
            c.execute(statement)
        for _round in (1, 2):  # idempotency IS the assertion
            for statement in _statements(_toolchain_block()):
                c.execute(statement)
    engine = create_engine(target.replace("postgresql://", "postgresql+psycopg://"))
    try:
        yield SimpleNamespace(engine=engine, url=target, psycopg=psycopg)
    finally:
        engine.dispose()
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')


@pytest.fixture(autouse=True)
def _quiet_events(monkeypatch):
    # The audit sink needs the events tables; the state machine is what is under test.
    monkeypatch.setattr(tci, "emit_event", lambda *a, **k: "evt")


@pytest.fixture()
def project(pg):
    import uuid

    pid = f"prj_{uuid.uuid4().hex[:10]}"
    with pg.engine.begin() as c:
        from sqlalchemy import text

        c.execute(text("INSERT INTO projects (id) VALUES (:p)"), {"p": pid})
    return pid


class FakeDocker:
    """Records every docker CLI call; answers like the real CLI would."""

    def __init__(self, *, commit_rc=0, push_rc=0, exec_rc=0):
        self.calls: list[list[str]] = []
        self.commit_rc, self.push_rc, self.exec_rc = commit_rc, push_rc, exec_rc

    def __call__(self, argv, timeout, stdin=None):
        self.calls.append(list(argv))
        verb = argv[1]
        if verb == "run":
            return subprocess.CompletedProcess(argv, 0, "c0ffee" * 10 + "\n", "")
        if verb == "exec":
            return subprocess.CompletedProcess(argv, self.exec_rc, "installed\n", "")
        if verb == "commit":
            return subprocess.CompletedProcess(argv, self.commit_rc,
                                               DIGEST + "\n" if not self.commit_rc else "",
                                               "boom: no space left" if self.commit_rc else "")
        if verb == "push":
            out = f"latest: digest: {PUSHED} size: 1234\n" if not self.push_rc else ""
            return subprocess.CompletedProcess(argv, self.push_rc, out, "denied" if self.push_rc else "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls]


def _settings(**kw):
    base = dict(sandbox_command_runtime="docker", sandbox_docker_binary="docker",
                sandbox_container_image="python:3.11-slim", sandbox_container_network="none",
                sandbox_container_memory="2g", sandbox_container_cpus="2",
                sandbox_container_pids=512, toolchain_registry="",
                toolchain_setup_network="bridge", toolchain_default_base="")
    base.update(kw)
    return SimpleNamespace(**base)


# ── schema ──────────────────────────────────────────────────────────────────


def test_the_block_applies_twice_and_its_constraints_hold(pg, project):
    psycopg = pg.psycopg
    with psycopg.connect(pg.url, autocommit=True) as c:
        tables = {r[0] for r in c.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        assert "toolchain_images" in tables
        c.execute("INSERT INTO toolchain_images (id, project_id, base) "
                  "VALUES ('tci_s1', %s, 'python:3.11-slim')", (project,))
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO toolchain_images (id, project_id, base, status) "
                      "VALUES ('tci_s2', %s, 'x', 'pending')", (project,))
        # one open (building) session per project
        with pytest.raises(psycopg.errors.UniqueViolation):
            c.execute("INSERT INTO toolchain_images (id, project_id, base) "
                      "VALUES ('tci_s3', %s, 'x')", (project,))
        c.execute("INSERT INTO toolchain_images (id, project_id, base, status) "
                  "VALUES ('tci_s4', %s, 'x', 'ready')", (project,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            c.execute("INSERT INTO toolchain_images (id, project_id, base) "
                      "VALUES ('tci_s5', 'prj_missing', 'x')")


def test_the_block_is_last_and_declared_once():
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    assert sql.count("CREATE TABLE IF NOT EXISTS toolchain_images") == 1
    import re

    markers = [m.start() for m in re.finditer(r"^-- BOSSAPP-\d+", sql, re.M)]
    assert sql.index("-- BOSSAPP-825\n") == markers[-1], "825 must come after every BOSSAPP block"
    for statement in _statements(_toolchain_block()):
        head = " ".join(statement.split())[:80].upper()
        if head.startswith(("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX")):
            assert "IF NOT EXISTS" in statement.upper(), head


# ── refusal when the docker runtime is off ──────────────────────────────────


@pytest.mark.parametrize("runtime", ["subprocess", "", "SUBPROCESS"])
def test_every_step_refuses_without_the_docker_runtime(runtime):
    docker = FakeDocker()
    settings = _settings(sandbox_command_runtime=runtime)
    with pytest.raises(tci.DockerRuntimeOff) as exc:
        tci.start_session(None, settings, project_id="p", actor="op", run=docker)
    assert "BOSS_SANDBOX_COMMAND_RUNTIME" in str(exc.value) and "'docker'" in str(exc.value)
    for call in (lambda: tci.exec_in_session(None, settings, "tci_x", "ls", actor="op", run=docker),
                 lambda: tci.begin_capture(None, settings, "tci_x", actor="op")):
        with pytest.raises(tci.DockerRuntimeOff):
            call()
    assert docker.calls == [], "nothing may start before the refusal"


def test_the_api_says_runtime_off_as_422_and_conflicts_as_409():
    from apps.api.app.routers.toolchain_images import _refusal

    assert _refusal(tci.DockerRuntimeOff("off")).status_code == 422
    assert _refusal(tci.ToolchainImageError("busy")).status_code == 409


# ── the capture state machine ────────────────────────────────────────────────


def test_start_exec_capture_to_a_local_image(pg, project):
    docker, settings = FakeDocker(), _settings()
    with pg.engine.begin() as c:
        row = tci.start_session(c, settings, project_id=project, actor="op", run=docker)
    assert (row["status"], row["phase"]) == ("building", "session")
    run = docker.calls[0]
    assert run[run.index("--network") + 1] == "bridge"          # setup installs, so it has network
    assert "boss.owner=toolchain-setup" in run and run[-3:] == ["python:3.11-slim", "sleep", "infinity"]

    with pg.engine.begin() as c:
        with pytest.raises(tci.ToolchainImageError, match="already has a setup session"):
            tci.start_session(c, settings, project_id=project, actor="op", run=docker)
        out = tci.exec_in_session(c, settings, row["id"], "apt-get install -y openjdk-21-jdk",
                                  actor="op", run=docker)
        assert out["exit_code"] == 0 and "installed" in out["stdout"]
        assert docker.calls[-1][-3:] == ["sh", "-lc", "apt-get install -y openjdk-21-jdk"]
    failing = FakeDocker(exec_rc=127)
    with pg.engine.begin() as c:
        tci.exec_in_session(c, settings, row["id"], "gox version", actor="op", run=failing)
        captured = tci.begin_capture(c, settings, row["id"], actor="op")
    assert (captured["status"], captured["phase"]) == ("building", "capturing")
    assert [e["exit_code"] for e in captured["transcript"]] == [0, 127]
    # only the commands that worked are replayed
    assert "RUN apt-get install -y openjdk-21-jdk" in captured["dockerfile"]
    assert "gox" not in captured["dockerfile"]

    with pg.engine.begin() as c:
        with pytest.raises(tci.ToolchainImageError, match="not open"):
            tci.exec_in_session(c, settings, row["id"], "ls", actor="op", run=docker)
        done = tci.finish_capture(c, settings, row["id"], actor="op", run=docker)
    assert done["status"] == "ready" and done["container_id"] is None
    assert done["digest"] == DIGEST
    assert done["image_ref"] == f"boss-toolchain/{project.lower()}:{row['id'].lower()}"
    assert docker.verbs()[-2:] == ["commit", "rm"] and "push" not in docker.verbs()
    with pg.engine.begin() as c:
        assert tci.ready_image(c, project)["id"] == row["id"]
        with pytest.raises(tci.ToolchainImageError, match="not being captured"):
            tci.finish_capture(c, settings, row["id"], run=docker)


def test_capture_with_a_registry_pins_the_pushed_digest(pg, project):
    docker, settings = FakeDocker(), _settings(toolchain_registry="127.0.0.1:5000/")
    with pg.engine.begin() as c:
        row = tci.start_session(c, settings, project_id=project, actor="op", run=docker,
                                base="eclipse-temurin:21")
        tci.begin_capture(c, settings, row["id"], actor="op")
        done = tci.finish_capture(c, settings, row["id"], run=docker)
    assert done["status"] == "ready" and done["digest"] == PUSHED
    assert done["image_ref"] == f"127.0.0.1:5000/boss-toolchain/{project.lower()}@{PUSHED}"
    assert docker.verbs()[-3:] == ["commit", "push", "rm"]


@pytest.mark.parametrize("fake,why", [(FakeDocker(commit_rc=1), "docker commit failed"),
                                      (FakeDocker(push_rc=1), "failed: denied")])
def test_a_failed_capture_is_failed_and_cleans_up(pg, project, fake, why):
    settings = _settings(toolchain_registry="127.0.0.1:5000")
    with pg.engine.begin() as c:
        row = tci.start_session(c, settings, project_id=project, actor="op", run=fake)
        tci.begin_capture(c, settings, row["id"], actor="op")
        done = tci.finish_capture(c, settings, row["id"], run=fake)
        assert tci.ready_image(c, project) is None
    assert done["status"] == "failed" and why in done["error"]
    assert fake.verbs()[-1] == "rm" and done["container_id"] is None


def test_discard_closes_the_session_and_a_new_one_may_start(pg, project):
    docker, settings = FakeDocker(), _settings()
    with pg.engine.begin() as c:
        row = tci.start_session(c, settings, project_id=project, actor="op", run=docker)
        gone = tci.discard(c, settings, row["id"], actor="op", run=docker)
        assert gone["status"] == "failed" and "discarded" in gone["error"]
        assert tci.start_session(c, settings, project_id=project, actor="op",
                                 run=docker)["status"] == "building"
    assert "rm" in docker.verbs()


# ── builds use it ────────────────────────────────────────────────────────────


@pytest.fixture()
def fake_runner_docker(monkeypatch):
    calls: list[list[str]] = []

    def boss_run(argv, **_kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "feedface\n", "")

    monkeypatch.setattr(container_runner.owned_process, "boss_run", boss_run)
    monkeypatch.setattr(container_runner, "preflight", lambda _s: None)
    monkeypatch.setattr(container_runner.time, "time", lambda: 1_700_000_000)
    return calls


def _started_argv(tmp_path, calls, **kw) -> list[str]:
    runner = container_runner.ContainerCommandRunner(tmp_path, _settings(), **kw)
    runner.start()
    return calls[-1], runner


def _ready_image(pg, project, ref="boss-toolchain/x:tci_1"):
    from sqlalchemy import text

    with pg.engine.begin() as c:
        c.execute(text("INSERT INTO toolchain_images (id, project_id, base, status, image_ref, digest) "
                       "VALUES (:id, :p, 'python:3.11-slim', 'ready', :ref, :d)"),
                  {"id": f"tci_{project[-6:]}", "p": project, "ref": ref, "d": DIGEST})
        c.execute(text("INSERT INTO code_task_runs (id, project_id) VALUES (:r, :p)"),
                  {"r": f"run_{project[-6:]}", "p": project})


def test_a_build_of_a_project_with_a_ready_image_runs_it(pg, project, tmp_path, fake_runner_docker):
    _ready_image(pg, project, ref="127.0.0.1:5000/boss-toolchain/p@" + PUSHED)
    # The governed pipeline passes only the run id; the runner maps it to the project.
    argv, runner = _started_argv(tmp_path, fake_runner_docker,
                                 task_id=f"run_{project[-6:]}", engine=pg.engine)
    assert argv[-3:] == ["127.0.0.1:5000/boss-toolchain/p@" + PUSHED, "sleep", "infinity"]
    assert runner.image_source == "project"
    argv2, _ = _started_argv(tmp_path, fake_runner_docker, project_id=project, engine=pg.engine)
    assert argv2[-3] == "127.0.0.1:5000/boss-toolchain/p@" + PUSHED


def test_no_image_configured_is_byte_identical(pg, project, tmp_path, fake_runner_docker, monkeypatch):
    from sqlalchemy import text

    with pg.engine.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id) VALUES (:r, :p)"),
                  {"r": f"run_{project[-6:]}", "p": project})
    argv, runner = _started_argv(tmp_path, fake_runner_docker,
                                 task_id=f"run_{project[-6:]}", engine=pg.engine)
    assert runner.image_source == "default"
    # The same runner with the lookup removed entirely: the argv before BOSSAPP-825.
    monkeypatch.setattr(tci, "runner_image_for", lambda *a, **k: None)
    before, _ = _started_argv(tmp_path, fake_runner_docker, task_id=f"run_{project[-6:]}")
    assert argv == before and argv[-3] == "python:3.11-slim"


def test_a_lookup_that_cannot_run_falls_back_to_the_default(tmp_path, fake_runner_docker):
    # No engine and settings without a database: the lookup fails quietly.
    argv, runner = _started_argv(tmp_path, fake_runner_docker, task_id="agentcode")
    assert argv[-3] == "python:3.11-slim" and runner.image_source == "default"


def test_an_environment_image_and_an_explicit_image_win(pg, project, tmp_path, fake_runner_docker):
    from sqlalchemy import text

    _ready_image(pg, project)
    with pg.engine.begin() as c:
        c.execute(text("INSERT INTO execution_environments (id, project_id, name, driver, config) "
                       "VALUES ('env_' || :p, :p, 'box', 'local', "
                       "CAST(:cfg AS JSONB))"),
                  {"p": project, "cfg": '{"default": true, "image": "ghcr.io/acme/build:7"}'})
    argv, _ = _started_argv(tmp_path, fake_runner_docker, project_id=project, engine=pg.engine)
    assert argv[-3] == "ghcr.io/acme/build:7"
    argv2, runner = _started_argv(tmp_path, fake_runner_docker, project_id=project,
                                  engine=pg.engine, image="mine:1")
    assert argv2[-3] == "mine:1" and runner.image_source == "explicit"

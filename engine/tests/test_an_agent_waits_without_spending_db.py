"""wait_for against a real database: the exam's own flow (services/wait_for.py).

A repository is registered and its index job queued; while the worker indexes it, one wait_for
call blocks on the real rows and returns the code graph's final state, nodes and all. Each
reader's SQL is run against the real schema, so a column it names that the table lacks fails here
and not on an install. Needs BOSS_KG_TEST_DATABASE_URL (a disposable database) and tree-sitter
(the API image).
"""
from __future__ import annotations

import os
import subprocess
import textwrap
import threading
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.config import get_settings
from boss_engine.db import ensure_schema
from boss_engine.services import wait_for as waiting
from boss_engine.services.jobs import JobLeaseService
from boss_engine.services.knowledge import repo_index

pytest.importorskip("tree_sitter")

JAVA = {
    "src/main/java/org/bank/loan/LoanRepaymentScheduleTransactionProcessor.java": """
        package org.bank.loan;

        public interface LoanRepaymentScheduleTransactionProcessor { String getCode(); }
        """,
    "src/main/java/org/bank/loan/AcmeProcessor.java": """
        package org.bank.loan;

        public class AcmeProcessor implements LoanRepaymentScheduleTransactionProcessor {
            public String getCode() { return "acme"; }
        }
        """,
}


@pytest.fixture(scope="module")
def pg_engine():
    url = os.environ.get("BOSS_KG_TEST_DATABASE_URL") or ""
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(marker in name for marker in ("test", "testing", "ci", "scratch", "tmp")):
        pytest.skip("BOSS_KG_TEST_DATABASE_URL must name a disposable database")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"disposable postgres is not reachable: {exc.__class__.__name__}")
    ensure_schema(engine)
    return engine


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}).stdout.strip()


@pytest.fixture()
def world(pg_engine, tmp_path):
    tag = uuid.uuid4().hex[:8]
    org, ws, pid = f"org_wf{tag}", f"ws_wf{tag}", f"proj_wf{tag}"
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'WF')"), {"o": org})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'WF')"),
                  {"w": ws, "o": org})
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                  {"p": pid, "w": ws})
        c.execute(text("INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
                       "credential_ref, status, created_by) VALUES (:c, :p, 'token', "
                       "'personal_access_token', 'secret:none', 'active', 'test')"),
                  {"c": f"ghconn_{tag}", "p": pid})
        c.execute(text("INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, "
                       "name, full_name, default_branch, created_by) VALUES (:i, :p, :c, 'bank', "
                       "'core', 'bank/core', 'main', 'test')"),
                  {"i": f"ghrepo_{tag}", "p": pid, "c": f"ghconn_{tag}"})
    root = tmp_path / "origin"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for path, body in JAVA.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(textwrap.dedent(body).lstrip())
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "first")
    settings = get_settings().model_copy(update={"kg_index_dir": str(tmp_path / "work"),
                                                 "kg_repo_index": 1})
    return {"project": pid, "settings": settings, "remote": root}


def _index_like_the_worker(engine, world, started: threading.Event) -> None:
    started.wait(10)
    jobs = JobLeaseService(world["settings"])
    with engine.begin() as c:
        job = c.execute(text("SELECT id FROM jobs WHERE project_id = :p AND kind = :k AND "
                             "status = 'queued' LIMIT 1"),
                        {"p": world["project"], "k": repo_index.JOB_KIND}).scalar()
        leased = jobs.lease_owned_job(c, project_id=world["project"], job_id=job,
                                      worker_id="wrk_test", lease_ttl_seconds=600)

    def checkout(*, project_id, repo, branch, dest, timeout):
        return repo_index.fetch_branch(str(world["remote"]), branch, dest, timeout=timeout)

    out = repo_index.execute_job(engine, world["settings"], job_id=job, worker_id="wrk_test",
                                 lease_token=leased["lease_token"], checkout=checkout, inline=True)
    with engine.begin() as c:
        jobs.complete(c, "wrk_test", job, leased["lease_token"], out)


def test_one_wait_returns_the_indexed_graph_while_the_worker_indexes_it(pg_engine, world,
                                                                       monkeypatch):
    repo_index.enqueue_due(pg_engine, world["settings"], project_id=world["project"])
    with pg_engine.begin() as c:
        before = repo_index.status(c, world["project"])["repositories"][0]
    assert before["status"] == "queued"
    job_id = before["job_id"]

    started = threading.Event()
    real_sleep = waiting._sleep

    def sleep(seconds):
        started.set()           # the worker starts once the wait is really waiting
        real_sleep(min(seconds, 0.5))

    monkeypatch.setattr(waiting, "_sleep", sleep)
    worker = threading.Thread(target=_index_like_the_worker, args=(pg_engine, world, started))
    worker.start()
    begin = pg_engine.begin
    out = waiting.wait(begin, world["settings"], project_id=world["project"], what="code_graph",
                       timeout_seconds=120)
    worker.join(30)
    assert out["done"] is True, out
    repo = out["state"]["repositories"][0]
    assert repo["status"] == "indexed" and repo["graph"]["nodes"] > 0
    assert out["reads"] >= 2, "it returned before the worker had run"
    # The job that did it, by its own id: settled too.
    job = waiting.wait(begin, world["settings"], project_id=world["project"], what="job",
                       ref=job_id, timeout_seconds=5)
    assert job["done"] is True and job["outcome"] == "succeeded"


def test_every_reader_names_real_columns(pg_engine, world):
    """Each kind's SQL runs on the real schema: an unknown id is refused in words, never an
    UndefinedColumn."""
    for what, ref in (("job", "job_nope"), ("code_task", "crun_nope"),
                      ("workflow_run", "wfr_nope")):
        with pytest.raises(waiting.WaitRefused):
            waiting.wait(pg_engine.begin, world["settings"], project_id=world["project"],
                         what=what, ref=ref, timeout_seconds=1)

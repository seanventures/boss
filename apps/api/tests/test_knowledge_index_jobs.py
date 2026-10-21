"""Knowledge Graph Phase 1: keeping the code graph current (services/knowledge/index_jobs.py).

A push enqueues one idempotent `knowledge.index` job; the schedule queues an hourly incremental
pass and a nightly full reconcile; a job indexes the default branch from a partial clone, records
the symbols the PR changed, and joins them to the PR through the lifecycle graph. A local bare
repository stands in for GitHub.
"""
from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from apps.api.app.db import begin
from boss_engine.services.knowledge import Scope
from boss_engine.services.knowledge import index_jobs
from boss_engine.services.knowledge.code_queries import KnowledgeQueries


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=ci@example.com", "-c", "user.name=ci", *args], cwd=root,
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def project(engine):
    with begin(engine) as c:
        pid = c.execute(text("SELECT id FROM projects ORDER BY created_at LIMIT 1")).scalar_one()
        new = "proj-kg-jobs-" + uuid.uuid4().hex[:6]
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) SELECT :n, workspace_id, :n, :n "
                       "FROM projects WHERE id = :p"), {"n": new, "p": pid})
    return new


@pytest.fixture
def remote(tmp_path):
    """A bare 'GitHub' repository: main, plus a PR branch that edits one function and deletes one."""
    work, bare = tmp_path / "work", tmp_path / "remote.git"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "shop").mkdir()
    (work / "shop/pricing.py").write_text(
        "def price(n):\n    return n * 2\n\n\ndef discount(n):\n    return n - 1\n\n\ndef legacy(n):\n    return n\n")
    (work / "shop/cart.py").write_text("from shop.pricing import price\n\n\ndef total(xs):\n    return sum(price(x) for x in xs)\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "checkout", "-q", "-b", "boss/code/run1")
    (work / "shop/pricing.py").write_text(
        "def price(n):\n    return n * 3\n\n\ndef discount(n):\n    return n - 1\n")
    _git(work, "commit", "-qam", "change price, drop legacy")
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare


def test_a_push_enqueues_one_job_per_run(engine, project):
    with begin(engine) as c:
        a = index_jobs.enqueue_index(c, project, "acme/shop", reason="push", ref="boss/code/r1", run_id="r1")
        b = index_jobs.enqueue_index(c, project, "acme/shop", reason="push", ref="boss/code/r1", run_id="r1")
        n = c.execute(text("SELECT count(*) FROM jobs WHERE project_id = :p AND kind = 'knowledge.index'"),
                      {"p": project}).scalar_one()
    assert a["id"] == b["id"] and n == 1


def test_the_schedule_queues_a_nightly_full_and_an_hourly_incremental(engine, project):
    now = datetime.now(timezone.utc)
    with begin(engine) as c:
        for repo, full_at, updated in (("acme/stale-full", now - timedelta(days=2), now),
                                       ("acme/stale-inc", now - timedelta(hours=2), now - timedelta(hours=2)),
                                       ("acme/fresh", now - timedelta(hours=1), now)):
            c.execute(text("INSERT INTO kg_code_index_state (project_id, repo, last_commit, last_full_at, updated_at) "
                           "VALUES (:p, :r, 'abc', :f, :u)"), {"p": project, "r": repo, "f": full_at, "u": updated})
        index_jobs.enqueue_due(c)
        rows = c.execute(text("SELECT payload->>'repo' AS repo, payload->>'mode' AS mode FROM jobs "
                              "WHERE project_id = :p AND kind = 'knowledge.index'"), {"p": project}).all()
    assert sorted((r.repo, r.mode) for r in rows) == [("acme/stale-full", "full"), ("acme/stale-inc", "incremental")]


def test_a_job_indexes_the_default_branch_and_links_the_pr_to_the_symbols_it_changed(
        engine, project, remote, tmp_path, monkeypatch):
    from boss_engine.config import get_settings
    from boss_engine.services import github_connections, repo_host

    monkeypatch.setenv("BOSS_REPO_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(github_connections, "github_token_for", lambda *a, **k: "")
    monkeypatch.setattr(repo_host, "git_access", lambda host, owner, repo, token="": repo_host.GitAccess(str(remote)))
    run_id = "run-" + project
    with begin(engine) as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec, state, finalize_ctx, "
                       "pull_request_number, pull_request_url) VALUES (:r, :p, 'Raise prices', 'completed', "
                       "CAST(:s AS JSONB), '{}'::jsonb, '{}'::jsonb, 3, 'https://github.example.com/acme/shop/pull/3')"),
                  {"r": run_id, "p": project, "s": '{"owner": "acme", "repo": "shop", "instructions": "Triple the price."}'})
    out = index_jobs.run_index_job(engine, get_settings(), project, {
        "repo": "acme/shop", "mode": "incremental", "ref": "boss/code/run1", "base": "main", "run_id": run_id,
        "pr_number": 3})
    with begin(engine) as c:
        changed = c.execute(text("SELECT symbol_key FROM kg_change_links WHERE project_id = :p AND repo = 'acme/shop' "
                                 "AND pr_number = 3 ORDER BY 1"), {"p": project}).scalars().all()
        why = KnowledgeQueries().why(c, Scope(project), "acme/shop:shop/pricing.py::price")
        callers = KnowledgeQueries().callers(c, Scope(project), "price")
    assert out["index"]["mode"] == "full"                       # first index of the repo
    assert sorted(changed) == ["acme/shop:shop/pricing.py::legacy", "acme/shop:shop/pricing.py::price"]
    assert [p["key"] for p in why["pull_requests"]] == ["pull_request:acme/shop#3"]
    assert why["specs"][0]["instructions"] == "Triple the price."
    assert [c_["key"] for c_ in callers["callers"]] == ["acme/shop:shop/cart.py::total"]   # main's graph


def test_changed_symbols_names_the_innermost_definition(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "m.py").write_text("class Cart:\n    def add(self, x):\n        return x\n\n    def drop(self, x):\n        return x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "a")
    base = _git(root, "rev-parse", "HEAD")
    (root / "m.py").write_text("class Cart:\n    def add(self, x):\n        return x + 1\n\n    def drop(self, x):\n        return x\n")
    _git(root, "commit", "-qam", "b")
    assert index_jobs.changed_symbols(root, "acme/r", base, "HEAD") == ["acme/r:m.py::Cart.add"]


def test_rewriting_the_run_record_keeps_the_pr_to_symbol_links(engine, project):
    """Derived data lives in its own table: a full rewrite of the versioned run record loses nothing."""
    from boss_engine.services.knowledge.code_index import CodeIndexer
    from boss_engine.services.knowledge.lifecycle import LifecycleGraph

    scope, run_id = Scope(project), "run-rw-" + project
    root = Path(subprocess.run(["mktemp", "-d"], capture_output=True, text=True).stdout.strip())
    _git(root, "init", "-q", "-b", "main")
    (root / "m.py").write_text("def price(n):\n    return n\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "a")
    with begin(engine) as c:
        CodeIndexer(workers=1).index_full(c, scope, "acme/rw", root)
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec, state, finalize_ctx, "
                       "pull_request_number) VALUES (:r, :p, 't', 'completed', CAST(:s AS JSONB), '{}'::jsonb, "
                       "'{}'::jsonb, 9)"), {"r": run_id, "p": project, "s": '{"owner": "acme", "repo": "rw"}'})
        index_jobs.record_changes(c, scope, "acme/rw", 9, ["acme/rw:m.py::price"], run_id=run_id)
        # The run record is rewritten wholesale, as the code-task pipeline does, with its version bumped.
        c.execute(text("UPDATE code_task_runs SET state = CAST(:s AS JSONB), state_version = state_version + 1 "
                       "WHERE id = :r"), {"s": '{"phase": "rewritten"}', "r": run_id})
        LifecycleGraph().sync(c, scope)
        why = KnowledgeQueries().why(c, scope, "acme/rw:m.py::price")
    assert [p["key"] for p in why["pull_requests"]] == ["pull_request:acme/rw#9"]


def test_the_knowledge_package_never_writes_derived_data_onto_a_versioned_record():
    """The class: no knowledge module UPDATEs a table that carries a state_version column."""
    import re

    root = Path(__file__).resolve().parents[3]
    schema = (root / "db/schema.sql").read_text()
    versioned = {m.group(1) for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((?:(?!\n\);).)*?state_version",
                                                 schema, re.S)}
    versioned |= {m.group(1) for m in re.finditer(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS state_version", schema)}
    assert "code_task_runs" in versioned
    offenders = []
    for path in (root / "engine/boss_engine/services/knowledge").glob("*.py"):
        for table in versioned:
            if re.search(rf"UPDATE\s+{table}\b", path.read_text(), re.I):
                offenders.append(f"{path.name} updates {table}")
    assert offenders == []

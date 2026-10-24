"""The code graph of a registered repository (services/knowledge/repo_index.py).

BOSS's own agent found kg_nodes empty in three projects with a registered Fineract repository:
nothing indexed a repository as it was registered. These tests drive the real path against a
real database and a real git repository (a local one stands in for GitHub): registering marks
it due (trigger), the tick queues ONE job, the leased job fetches the branch and indexes it in a
child process (Java and Python), a moved head is indexed incrementally, an unmoved one is not
indexed again, and the bounds refuse in words.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest
from boss_engine.config import get_settings
from boss_engine.db import ensure_schema
from boss_engine.services.jobs import JobLeaseService
from boss_engine.services.knowledge import repo_index
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

JAVA_REPO = {
    "src/main/java/org/bank/loan/LoanService.java": """
        package org.bank.loan;

        import org.bank.account.AccountRepository;

        public class LoanService {
            private final AccountRepository accounts;

            public LoanService(AccountRepository accounts) { this.accounts = accounts; }

            public long disburse(long accountId, long amount) {
                accounts.credit(accountId, amount);
                return amount;
            }
        }
        """,
    "src/main/java/org/bank/account/AccountRepository.java": """
        package org.bank.account;

        public class AccountRepository {
            public void credit(long accountId, long amount) { }
        }
        """,
    "src/main/java/org/bank/report/Old.java": """
        package org.bank.report;

        public class Old { public void run() { } }
        """,
    "tools/report.py": """
        def summarize(rows):
            return len(rows)

        def main():
            return summarize([])
        """,
}


def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
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


def _write(root: Path, files: dict) -> None:
    for path, body in files.items():
        target = root / path
        if body is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(body).lstrip())


@pytest.fixture()
def remote(tmp_path):
    """A git repository standing in for the GitHub one, on branch main."""
    root = tmp_path / "origin"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write(root, JAVA_REPO)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "first")
    return root


def _commit(root: Path, files: dict, message: str) -> str:
    _write(root, files)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture()
def world(pg_engine, tmp_path):
    tag = uuid.uuid4().hex[:8]
    org, ws, pid = f"org_ri{tag}", f"ws_ri{tag}", f"proj_ri{tag}"
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'RI')"), {"o": org})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'RI')"), {"w": ws, "o": org})
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                  {"p": pid, "w": ws})
        c.execute(text("INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
                       "credential_ref, status, created_by) VALUES (:c, :p, 'token', 'personal_access_token', "
                       "'secret:none', 'active', 'test')"), {"c": f"ghconn_{tag}", "p": pid})
    settings = get_settings().model_copy(update={"kg_index_dir": str(tmp_path / "work"),
                                                 "kg_repo_index": 1})
    return {"tag": tag, "project": pid, "connection": f"ghconn_{tag}", "settings": settings}


def _register(engine, world, full_name="bank/core", branch="main") -> str:
    bid = f"ghrepo_{world['tag']}_{full_name.replace('/', '_')}"
    owner, name = full_name.split("/")
    with engine.begin() as c:
        c.execute(text("INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
                       "full_name, default_branch, created_by) VALUES (:i, :p, :c, :o, :n, :f, :b, 'test')"),
                  {"i": bid, "p": world["project"], "c": world["connection"], "o": owner, "n": name,
                   "f": full_name, "b": branch})
    return bid


def _row(engine, world, bid) -> dict:
    with engine.begin() as c:
        return dict(c.execute(text("SELECT * FROM kg_repo_index WHERE project_id = :p AND binding_id = :b"),
                              {"p": world["project"], "b": bid}).mappings().one())


def _jobs(engine, world) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT id, status, priority, payload FROM jobs WHERE project_id = :p AND kind = :k ORDER BY created_at"),
            {"p": world["project"], "k": repo_index.JOB_KIND}).mappings().all()]


def _run_job(engine, world, remote: Path, *, inline: bool) -> dict:
    """Lease the project's queued index job like the worker does, and run it."""
    jobs = JobLeaseService(world["settings"])
    with engine.begin() as c:
        job = c.execute(text("SELECT id FROM jobs WHERE project_id = :p AND kind = :k AND status = 'queued' "
                             "ORDER BY created_at LIMIT 1"), {"p": world["project"], "k": repo_index.JOB_KIND}).scalar()
        assert job, "no queued index job"
        leased = jobs.lease_owned_job(c, project_id=world["project"], job_id=job, worker_id="wrk_test",
                                      lease_ttl_seconds=600)

    def checkout(*, project_id, repo, branch, dest, timeout):
        return repo_index.fetch_branch(str(remote), branch, dest, timeout=timeout)

    out = repo_index.execute_job(engine, world["settings"], job_id=job, worker_id="wrk_test",
                                 lease_token=leased["lease_token"], checkout=checkout, inline=inline)
    with engine.begin() as c:
        jobs.complete(c, "wrk_test", job, leased["lease_token"], out)
    return out


def _symbols(engine, world, lang: str) -> set[str]:
    with engine.begin() as c:
        return set(c.execute(text(
            "SELECT name FROM kg_nodes WHERE project_id = :p AND kind = 'symbol' AND valid_to IS NULL "
            "AND attrs->>'lang' = :l"), {"p": world["project"], "l": lang}).scalars().all())


def test_registering_marks_the_repository_due_and_one_job_is_queued(pg_engine, world):
    bid = _register(pg_engine, world)
    row = _row(pg_engine, world, bid)
    assert row["due"] and row["reason"] == "registered" and row["branch"] == "main"

    first = repo_index.enqueue_due(pg_engine, world["settings"], project_id=world["project"])
    again = repo_index.enqueue_due(pg_engine, world["settings"], project_id=world["project"])
    assert first["queued"] == 1 and again["queued"] == 0
    jobs = _jobs(pg_engine, world)
    assert len(jobs) == 1 and jobs[0]["payload"]["binding_id"] == bid
    assert _row(pg_engine, world, bid)["status"] == "queued"

    # Asked again while one is in flight: reported, never queued twice.
    with pg_engine.begin() as c:
        out = repo_index.request(c, world["settings"], project_id=world["project"], actor="usr_x")
    assert out["queued"] == [] and out["in_flight"] == ["bank/core"]
    assert len(_jobs(pg_engine, world)) == 1


def test_a_repository_registered_before_the_trigger_is_found_by_the_tick(pg_engine, world):
    bid = _register(pg_engine, world)
    with pg_engine.begin() as c:
        c.execute(text("DELETE FROM kg_repo_index WHERE binding_id = :b"), {"b": bid})
    assert repo_index.enqueue_due(pg_engine, world["settings"], project_id=world["project"])["queued"] == 1
    assert _row(pg_engine, world, bid)["reason"] == "registered"


def test_the_job_indexes_java_then_a_moved_head_incrementally_and_an_unmoved_one_not_at_all(
        pg_engine, world, remote):
    bid = _register(pg_engine, world)
    repo_index.enqueue_due(pg_engine, world["settings"], project_id=world["project"])

    # The real path: the index runs in a child process.
    out = _run_job(pg_engine, world, remote, inline=False)
    assert out["status"] == "succeeded", out
    row = _row(pg_engine, world, bid)
    head = _git(remote, "rev-parse", "HEAD")
    assert row["status"] == "indexed" and row["indexed_sha"] == head
    assert row["files"] == 4 and row["nodes"] > 8 and row["edges"] > 4
    assert {"LoanService", "LoanService.disburse", "AccountRepository.credit"} <= _symbols(pg_engine, world, "java")
    assert {"summarize", "main"} <= _symbols(pg_engine, world, "python")
    with pg_engine.begin() as c:
        # The cross-file Java call resolved through the import.
        calls = c.execute(text(
            "SELECT count(*) FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id JOIN kg_nodes d ON d.id = e.dst_id "
            "WHERE e.project_id = :p AND e.kind = 'calls' AND s.name = 'LoanService.disburse' "
            "AND d.name = 'AccountRepository.credit' AND e.valid_to IS NULL"), {"p": world["project"]}).scalar()
        assert calls == 1
        receipt = c.execute(text("SELECT status, result FROM receipts WHERE project_id = :p AND effect_kind = :k"),
                            {"p": world["project"], "k": repo_index.EFFECT_KIND}).mappings().one()
        assert receipt["status"] == "succeeded" and receipt["result"]["nodes"] == row["nodes"]
        assert c.execute(text("SELECT count(*) FROM events WHERE project_id = :p AND event_type = "
                              "'knowledge.repository.indexed'"), {"p": world["project"]}).scalar() == 1
    with pg_engine.connect() as c:
        status = repo_index.status(c, world["project"])["repositories"][0]
    assert status["graph"]["nodes"] == row["nodes"] and "nodes" in status["summary"]

    # The head moves: one file changed, one deleted -> incremental.
    _commit(remote, {
        "src/main/java/org/bank/account/AccountRepository.java": """
            package org.bank.account;

            public class AccountRepository {
                public void credit(long accountId, long amount) { audit(accountId); }
                public void audit(long accountId) { }
            }
            """,
        "src/main/java/org/bank/report/Old.java": None,
    }, "second")
    with pg_engine.begin() as c:
        asked = repo_index.request(c, world["settings"], project_id=world["project"], actor="usr_x",
                                   repository="bank/core")
    assert asked["queued"] == ["bank/core"]
    out = _run_job(pg_engine, world, remote, inline=True)
    assert out["status"] == "succeeded" and out["mode"] == "incremental", out
    java = _symbols(pg_engine, world, "java")
    assert "AccountRepository.audit" in java and "Old" not in java and "Old.run" not in java
    assert _row(pg_engine, world, bid)["files"] == 3

    # Asked again with nothing new on the branch: nothing is parsed.
    with pg_engine.begin() as c:
        repo_index.request(c, world["settings"], project_id=world["project"], actor="usr_x")
    out = _run_job(pg_engine, world, remote, inline=True)
    assert out == {"status": "succeeded", "mode": "unchanged", "head": _git(remote, "rev-parse", "HEAD")}


def test_bounds_refuse_in_words_and_write_nothing(pg_engine, world, remote):
    bid = _register(pg_engine, world)
    world["settings"] = world["settings"].model_copy(update={"kg_index_max_files": 2})
    repo_index.enqueue_due(pg_engine, world["settings"], project_id=world["project"])
    out = _run_job(pg_engine, world, remote, inline=True)
    assert out["status"] == "failed" and out["retry"] is False
    row = _row(pg_engine, world, bid)
    assert row["status"] == "too_large" and "BOSS_KG_INDEX_MAX_FILES" in row["error"]
    assert _symbols(pg_engine, world, "java") == set()
    with pg_engine.connect() as c:
        assert "Not indexed" in repo_index.status(c, world["project"])["repositories"][0]["summary"]


def test_one_file_over_the_size_limit_is_left_out(pg_engine, world, remote):
    _register(pg_engine, world)
    _commit(remote, {"src/main/java/org/bank/Generated.java":
                     "package org.bank;\npublic class Generated {\n" + "  int x;\n" * 4000 + "}\n"}, "big")
    world["settings"] = world["settings"].model_copy(update={"kg_index_max_file_bytes": 20_000})
    repo_index.enqueue_due(pg_engine, world["settings"], project_id=world["project"])
    assert _run_job(pg_engine, world, remote, inline=True)["status"] == "succeeded"
    java = _symbols(pg_engine, world, "java")
    assert "Generated" not in java and "LoanService" in java


def test_the_sweep_marks_a_moved_branch_and_a_stale_look_due(pg_engine, world, remote):
    bid = _register(pg_engine, world)
    repo_index.enqueue_due(pg_engine, world["settings"], project_id=world["project"])
    _run_job(pg_engine, world, remote, inline=True)
    assert not _row(pg_engine, world, bid)["due"]
    with pg_engine.begin() as c:
        c.execute(text("UPDATE kg_repo_index SET last_checked_at = now() - interval '2 hours' "
                       "WHERE binding_id = :b"), {"b": bid})
        assert repo_index.mark_stale(c, recheck_minutes=60, project_id=world["project"]) == 1
    assert _row(pg_engine, world, bid)["reason"] == "recheck"
    with pg_engine.begin() as c:
        c.execute(text("UPDATE kg_repo_index SET due = false WHERE binding_id = :b"), {"b": bid})
        c.execute(text("UPDATE github_repository_bindings SET default_branch = 'release' WHERE id = :b"), {"b": bid})
    row = _row(pg_engine, world, bid)
    assert row["due"] and row["reason"] == "branch_changed" and row["branch"] == "release"


def test_off_install_wide_queues_nothing(pg_engine, world):
    _register(pg_engine, world)
    off = world["settings"].model_copy(update={"kg_repo_index": 0})
    assert repo_index.enqueue_due(pg_engine, off, project_id=world["project"])["queued"] == 0
    assert _jobs(pg_engine, world) == []


def test_a_job_that_ended_without_reporting_is_due_again(pg_engine, world):
    bid = _register(pg_engine, world)
    repo_index.enqueue_due(pg_engine, world["settings"], project_id=world["project"])
    with pg_engine.begin() as c:
        c.execute(text("UPDATE jobs SET status = 'cancelled' WHERE project_id = :p"), {"p": world["project"]})
        assert repo_index.mark_stale(c, recheck_minutes=60, project_id=world["project"]) == 1
    assert _row(pg_engine, world, bid)["reason"] == "retry"
    assert repo_index.enqueue_due(pg_engine, world["settings"], project_id=world["project"])["queued"] == 1



def test_a_graph_from_an_older_indexer_is_indexed_whole_again_on_the_next_tick(pg_engine, world, remote):
    """The live graphs were built before extends/implements existed. After an install with a newer
    INDEXER_VERSION, the next tick (not the 10-minute sweep) queues each such repository, and the
    run re-parses the whole tree although its head did not move."""
    from boss_engine.services.knowledge.code_index import INDEXER_VERSION

    _commit(remote, {"src/main/java/org/bank/loan/EarlyLoanService.java": """
        package org.bank.loan;

        public class EarlyLoanService extends LoanService {
            public EarlyLoanService() { super(null); }
        }
        """}, "subclass")
    bid = _register(pg_engine, world)
    repo_index.enqueue_due(pg_engine, world["settings"], project_id=world["project"])
    assert _run_job(pg_engine, world, remote, inline=True)["status"] == "succeeded"
    row = _row(pg_engine, world, bid)
    assert row["indexer_version"] == INDEXER_VERSION and not row["due"]
    # A current graph is left alone by the tick.
    assert repo_index.enqueue_due(pg_engine, world["settings"], project_id=world["project"])["queued"] == 0

    # As the banking projects were: indexed by version 1 (no version recorded), no inheritance edges.
    with pg_engine.begin() as c:
        c.execute(text("UPDATE kg_repo_index SET indexer_version = NULL WHERE binding_id = :b"), {"b": bid})
        c.execute(text("UPDATE kg_edges SET valid_to = now() WHERE project_id = :p AND kind = 'extends'"),
                  {"p": world["project"]})
    assert repo_index.enqueue_due(pg_engine, world["settings"], project_id=world["project"])["queued"] == 1
    row = _row(pg_engine, world, bid)
    assert row["reason"] == "indexer_upgraded" and row["force"] is True
    out = _run_job(pg_engine, world, remote, inline=True)
    assert out["status"] == "succeeded" and out["mode"] == "full", out
    row = _row(pg_engine, world, bid)
    assert row["indexer_version"] == INDEXER_VERSION and row["force"] is False
    with pg_engine.begin() as c:
        assert c.execute(text(
            "SELECT count(*) FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id JOIN kg_nodes d ON d.id = e.dst_id "
            "WHERE e.project_id = :p AND e.kind = 'extends' AND e.valid_to IS NULL "
            "AND s.name = 'EarlyLoanService' AND d.name = 'LoanService'"), {"p": world["project"]}).scalar() == 1
    assert repo_index.enqueue_due(pg_engine, world["settings"], project_id=world["project"])["queued"] == 0

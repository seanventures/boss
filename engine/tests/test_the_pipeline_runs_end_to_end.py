"""THE PIPELINE, END TO END, IN THE TEST SUITE: the standard place for a pipeline test.

`_run_governed_code_task` is one long function, and most of its tests read its SOURCE (a call is
behind a flag, a name is passed). That is how #168's re-check shipped green and crashed on its
first real run (KeyError: 'implement', fixed by #173): no test ever ran it. This file runs it.

THE HARNESS (`pipeline` fixture, module scope):
  * the bench database (BOSS_TEST_DATABASE_URL, the suite's own) with one project of its own;
  * a LOCAL GitHub on loopback (tests/e2e/gate/remote.py: git smart-HTTP + the REST API, real
    bare repositories), which BOSS's clone and pull request reach through its own two seams;
  * the SCRIPTED conformance engine (services/scripted_engine.py) for every model seat: it
    writes the plain-node fixture's tests and app, and answers every reviewer, the acceptance
    check and the arbiter deterministically — no model, no cost;
  * builds start as the API starts them (code_task_service.start_code_task / retry_code_task /
    start_followup_task) and run as the worker runs them (code_task_service.run_code_task).

ADDING A PIPELINE TEST. Start the work the way its door does, call `pipeline.run(run_id)`, and
assert on what `pipeline.result(run_id)` returns (the run row, its events, its llm_calls, the
stages the scripted engine played, the merge cards) and on `pipeline.branch_heads(repo)` (what
reached the remote). Inject an engine fault with `pipeline.build(faults="failing_test")` (see
scripted_engine's FAULTS). Keep each scenario to one build of the fixture: the whole file must
stay under a minute on the bench database.

    PYTHONPATH=<repo>:<repo>/engine BOSS_TEST_DATABASE_URL=… pytest engine/tests/test_the_pipeline_runs_end_to_end.py
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "tests" / "e2e" / "gate"
FIXTURE = REPO_ROOT / "tests" / "e2e" / "fixtures" / "plain-node" / "plain"
OWNER = "pipe"

pytestmark = pytest.mark.skipif(not os.environ.get("BOSS_TEST_DATABASE_URL"),
                                reason="needs the bench database (BOSS_TEST_DATABASE_URL)")


class Pipeline:
    """One project, a local GitHub and the scripted engine; builds run in-process."""

    def __init__(self, work: Path):
        from sqlalchemy import create_engine, text

        url = os.environ["BOSS_TEST_DATABASE_URL"]
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        self.text = text
        self.engine = create_engine(url, future=True)
        self.project = f"p_pipe{uuid.uuid4().hex[:8]}"
        with self.engine.begin() as c:
            c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_pipe', 'pipe') "
                           "ON CONFLICT DO NOTHING"))
            c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_pipe', "
                           "'org_pipe', 'pipe') ON CONFLICT DO NOTHING"))
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                           "VALUES (:p, 'ws_pipe', 'pipe', :p)"), {"p": self.project})
        from apps.api.app.bootstrap_ports import build_ports
        from boss_engine.config import get_settings
        from boss_engine.ports.composition import configure_engine_ports, get_engine_ports

        self.stack = contextlib.ExitStack()
        # Everything set up here is undone in close(), in reverse: the process's engine ports,
        # the gate directory on sys.path, the local GitHub and BOSS's pointer at it.
        previous_ports = get_engine_ports()
        configure_engine_ports(build_ports(get_settings(), self.engine))
        self.stack.callback(configure_engine_ports, previous_ports)
        if str(GATE) not in sys.path:
            sys.path.insert(0, str(GATE))
            self.stack.callback(lambda: str(GATE) in sys.path and sys.path.remove(str(GATE)))
        from remote import LocalGitHub  # tests/e2e/gate/remote.py

        self.hub = LocalGitHub(work / "github").start()
        self.stack.callback(self.hub.stop)
        self.stack.callback(self.hub.point_boss_at_me())
        self.repos: dict[str, Path] = {}

    # -- the world -----------------------------------------------------------------------------
    def settings(self, repo: str):
        from boss_engine.config import get_settings

        return get_settings().model_copy(update={
            "enable_code_task": True, "governed_coding_pipeline": True,
            "github_token": "pipe-local-token",
            "code_agent_engine": "scripted", "code_review_engine": "scripted",
            "sandbox_ci_timeout_seconds": 1, "sandbox_ci_poll_interval_seconds": 1,
            "target_github_owner": OWNER, "target_github_repo": repo})

    def jobs(self, repo: str):
        from boss_engine.services.jobs import JobLeaseService

        return JobLeaseService(self.settings(repo))

    def branch_heads(self, repo: str) -> dict[str, str]:
        """Every branch on the remote and its head commit: what a build pushed."""
        out = subprocess.run(["git", "--git-dir", str(self.repos[repo]), "for-each-ref",
                              "--format=%(refname:short) %(objectname)", "refs/heads"],
                             capture_output=True, text=True, check=True).stdout
        return dict(line.split(" ", 1) for line in out.splitlines() if line.strip())

    # -- starting and running work -------------------------------------------------------------
    def build(self, *, faults: str = "", tag: str = "build",
              brief_extra: str = "", seed: Optional[dict] = None) -> tuple[str, str]:
        """A small fresh build, started as the API starts one. Returns (run_id, repo)."""
        from boss_engine.services import code_task_service

        self._engine_env(faults)
        repo = f"pipe-{tag}-{uuid.uuid4().hex[:6]}"
        self.repos[repo] = self.hub.seed(OWNER, repo, {"README.md": f"# {repo}\n", **(seed or {})})
        with self.engine.begin() as c:
            run_id = code_task_service.start_code_task(
                c, project_id=self.project, task_id=None, title=f"Slug service ({tag})",
                instructions=("Build a tiny plain Node ESM service at the repository root: "
                              "slug(text) in src/slug.js and an HTTP server in src/server.js "
                              "answering GET /?q=<text> with {\"slug\": ...} on $PORT. Tests "
                              "under node --test.") + brief_extra,
                acceptance_criteria=["slug lowercases and joins words with hyphens",
                                     "GET /?q= answers the slug as JSON"],
                test_commands=["npm test"], owner=OWNER, repo=repo, base_branch="main",
                finalize_ctx={"app_name": "slug"}, jobs=self.jobs(repo),
                model_config={"default": "scripted"}, open_pr=True, allow_duplicate=True,
                # The full review panel without the design chain or a plan gate (a person's
                # decision would park an unattended run): as the engines gate builds.
                profile="balanced", risk_tier=1)
        return run_id, repo

    def run(self, run_id: str, repo: str) -> Optional[str]:
        """Run one build as the worker runs it. Returns the exception's words, if any."""
        from boss_engine.services import code_task_service
        from boss_engine.services.llm_telemetry import clear_llm_context

        try:
            code_task_service.run_code_task(self.engine, self.settings(repo), self.jobs(repo),
                                            run_id)
            return None
        except Exception as exc:  # noqa: BLE001 — the run's own record says what happened
            return f"{type(exc).__name__}: {exc}"
        finally:
            clear_llm_context()

    def result(self, run_id: str) -> dict[str, Any]:
        from boss_engine.services import scripted_engine

        t = self.text
        with self.engine.begin() as c:
            run = dict(c.execute(t("SELECT * FROM code_task_runs WHERE id = :i"),
                                 {"i": run_id}).mappings().one())
            events = [r[0] for r in c.execute(t(
                "SELECT event_type FROM events WHERE entity_id = :i ORDER BY created_at, id"),
                {"i": run_id})]
            calls = [dict(r) for r in c.execute(t(
                "SELECT stage, engine FROM llm_calls WHERE code_task_run_id = :i"),
                {"i": run_id}).mappings()]
            children = [r[0] for r in c.execute(t(
                "SELECT id FROM code_task_runs WHERE project_id = :p AND id <> :i "
                "AND (finalize_ctx->>'parent_run_id' = :i "
                "     OR finalize_ctx->>'deferred_from_run' = :i)"),
                {"p": self.project, "i": run_id})]
        stages = []
        for row in scripted_engine.run_state(run_id).calls:
            if row["stage"] not in stages:
                stages.append(row["stage"])
        return {"run": run, "events": events, "calls": calls, "stages": stages,
                "children": children,
                "writes": [r for r in scripted_engine.run_state(run_id).calls
                           if r["stage"] in ("write_tests", "implement", "solo", "repair",
                                             "quality") and not r["read_only"]]}

    def cards(self, pr_url: str) -> list[dict[str, Any]]:
        """The merge and fix cards about one pull request, oldest first."""
        with self.engine.begin() as c:
            return [dict(r) for r in c.execute(self.text(
                "SELECT id, status, recommendation->>'action' AS action, "
                "recommendation->>'run_id' AS run_id FROM decisions WHERE project_id = :p "
                "AND (recommendation->>'pull_request_url' = :u "
                "     OR id IN (SELECT decision_id FROM code_task_runs "
                "               WHERE pull_request_url = :u AND decision_id IS NOT NULL)) "
                "ORDER BY created_at"), {"p": self.project, "u": pr_url}).mappings()]

    # -- plumbing ------------------------------------------------------------------------------
    @staticmethod
    def _engine_env(faults: str, fixture: Optional[Path] = None) -> None:
        """The scripted engine's script: `faults` (scripted_engine FAULTS) and the fixture
        tree its writing seats write (default: the plain-node slug service)."""
        from boss_engine.services import scripted_engine

        os.environ["BOSS_SCRIPTED_ENGINE"] = "1"
        os.environ["BOSS_SCRIPTED_FAULTS"] = faults
        os.environ["BOSS_SCRIPTED_CAPS"] = ""
        os.environ["BOSS_SCRIPTED_FIXTURE"] = str(fixture or FIXTURE)
        os.environ["BOSS_SCRIPTED_APP_DIR"] = ""
        scripted_engine.reset()

    def close(self) -> None:
        """Undo the world, then delete every row of this project — loudly: a row the purge
        cannot delete is a test failure, never a leftover (queued code.task jobs once were)."""
        try:
            self.stack.close()
            purge_projects(self.engine, [self.project])
        finally:
            self.engine.dispose()


def purge_projects(engine: Any, project_ids: list[str]) -> None:
    """Delete every row of these projects in ONE transaction that raises on any error.

    Every table with a `project_id` column is emptied of the projects' rows — jobs (the queued
    `code.task` ones too), events, quality gates, budget reservations, the findings ledger and
    acceptance rows, decisions, runs — children before the tables they reference (the schema's
    NO ACTION / RESTRICT foreign keys, read from pg_constraint), then the project rows. The
    append-only ledgers allow it the way a project deletion does (`boss.ledger_purge`). Rows
    of other projects are never touched; a key that still blocks the delete fails the test."""
    from sqlalchemy import bindparam, text

    if not project_ids:
        return
    with engine.begin() as c:
        c.execute(text("SET LOCAL boss.ledger_purge = 'on'"))
        tables = {r[0] for r in c.execute(text(
            "SELECT c.table_name FROM information_schema.columns c "
            "JOIN information_schema.tables t ON t.table_schema = c.table_schema "
            "AND t.table_name = c.table_name WHERE c.table_schema = 'public' "
            "AND c.column_name = 'project_id' AND t.table_type = 'BASE TABLE' "
            "AND c.table_name <> 'projects'"))}
        # parent -> the tables that must be emptied before it (their keys neither cascade nor
        # set null). A partition's rows are deleted through its parent table.
        before: dict[str, set[str]] = {t: set() for t in tables}
        for child, parent in c.execute(text(
                "SELECT ch.relname, pa.relname FROM pg_constraint k "
                "JOIN pg_class ch ON ch.oid = k.conrelid JOIN pg_class pa ON pa.oid = k.confrelid "
                "WHERE k.contype = 'f' AND k.confdeltype IN ('a', 'r') "
                "AND ch.relname <> pa.relname")):
            if child in tables and parent in tables:
                before[parent].add(child)
        order: list[str] = []
        left = set(tables)
        while left:
            ready = sorted(t for t in left if not (before[t] & left))
            if not ready:                    # a cycle: let the database say which key blocks
                ready = sorted(left)
            order += ready
            left -= set(ready)
        ids = bindparam("ids", expanding=True)
        for t in order:
            c.execute(text(f'DELETE FROM "{t}" WHERE project_id IN :ids').bindparams(ids),
                      {"ids": list(project_ids)})
        c.execute(text("DELETE FROM projects WHERE id IN :ids").bindparams(ids),
                  {"ids": list(project_ids)})


def leftovers(engine: Any, project_ids: Optional[list[str]] = None) -> dict[str, int]:
    """What remains of the harness's projects (all `p_pipe*`, or these): project rows, jobs
    still queued or running, and code-task runs."""
    from sqlalchemy import text

    where = "id LIKE 'p\\_pipe%'" if project_ids is None else "id = ANY(:ids)"
    pwhere = ("project_id LIKE 'p\\_pipe%'" if project_ids is None
              else "project_id = ANY(:ids)")
    args = {} if project_ids is None else {"ids": list(project_ids)}
    with engine.begin() as c:
        return {
            "projects": int(c.execute(text(f"SELECT count(*) FROM projects WHERE {where}"),
                                      args).scalar()),
            "queued_jobs": int(c.execute(text(
                f"SELECT count(*) FROM jobs WHERE {pwhere} "
                "AND status IN ('queued', 'leased', 'running')"), args).scalar()),
            "runs": int(c.execute(text(f"SELECT count(*) FROM code_task_runs WHERE {pwhere}"),
                                  args).scalar()),
        }


_ENV = ("DATABASE_URL", "BOSS_AUTHZ_MODE", "BOSS_SCRIPTED_ENGINE", "BOSS_SCRIPTED_FAULTS",
        "BOSS_SCRIPTED_CAPS", "BOSS_SCRIPTED_FIXTURE", "BOSS_SCRIPTED_APP_DIR",
        "BOSS_SANDBOX_CI_TIMEOUT_SECONDS", "BOSS_SANDBOX_CI_POLL_INTERVAL_SECONDS")


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    saved = {k: os.environ.get(k) for k in _ENV}
    # The worker's view of the world: the same database, identity in shadow (who may start a
    # build is the API's test, not this file's), as the engines gate runs it.
    os.environ["DATABASE_URL"] = os.environ["BOSS_TEST_DATABASE_URL"]
    os.environ["BOSS_AUTHZ_MODE"] = "shadow"
    # THE HARNESS'S PATIENCE, not the pipeline's behaviour: the local GitHub has no CI, so the
    # pull request's check wait gives up at once instead of after 120 s; and Chromium is an
    # install-level cache (playwright_cache) the harness treats as warm instead of downloading
    # it into every verify's sandbox (~20 s each) for an app that never opens a browser.
    os.environ["BOSS_SANDBOX_CI_TIMEOUT_SECONDS"] = "1"
    os.environ["BOSS_SANDBOX_CI_POLL_INTERVAL_SECONDS"] = "1"
    from boss_engine.config import get_settings
    from boss_engine.services import playwright_cache

    get_settings.cache_clear()
    mp = pytest.MonkeyPatch()
    mp.setattr(playwright_cache, "ensure", lambda *_a, **_k: {
        "ok": True, "cached": True, "seconds": 0.0, "path": None,
        "detail": "harness: the browser cache is warm"})
    p = Pipeline(tmp_path_factory.mktemp("pipeline"))
    try:
        yield p
    finally:
        mp.undo()
        p.close()
        # NOTHING LEFT BEHIND: no project row, no queued job, no run of this module's project.
        from sqlalchemy import create_engine

        check = create_engine(p.engine.url, future=True)
        try:
            left = leftovers(check, [p.project])
            # ...and no `p_pipe*` project of any earlier run of this file either.
            anywhere = leftovers(check)
        finally:
            check.dispose()
        assert left == {"projects": 0, "queued_jobs": 0, "runs": 0}, left
        assert anywhere == {"projects": 0, "queued_jobs": 0, "runs": 0}, anywhere
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


@pytest.fixture(scope="module")
def built(pipeline):
    """One clean build of the fixture: the change the follow-up and the re-check work on."""
    run_id, repo = pipeline.build(tag="base")
    error = pipeline.run(run_id, repo)
    return {"run_id": run_id, "repo": repo, "error": error, **pipeline.result(run_id)}


# ── a normal build ───────────────────────────────────────────────────────────────────────────

def test_a_small_build_writes_is_judged_and_opens_its_pull_request(pipeline, built):
    run = built["run"]
    assert built["error"] is None, built["error"]
    assert run["status"] == "completed", (run["status"], run.get("error"))
    assert run["pull_request_url"], "no pull request"
    for stage in ("write_tests", "implement", "acceptance_check"):
        assert stage in built["stages"], built["stages"]
    assert any(s in built["stages"] for s in ("review_code", "review_panel")), built["stages"]
    heads = pipeline.branch_heads(built["repo"])
    assert len(heads) >= 2, heads             # main and the work branch
    assert any(c["run_id"] == built["run_id"] and c["status"] == "pending"
               for c in pipeline.cards(run["pull_request_url"])), "no merge card"


# ── a follow-up on that change ───────────────────────────────────────────────────────────────

def test_a_follow_up_builds_on_the_same_branch_and_pull_request(pipeline, built, tmp_path):
    import shutil

    from boss_engine.services import code_task_service

    # The change the follow-up delivers: the fixture plus one more module (the scripted
    # engine writes whatever its fixture holds, so an identical fixture would change nothing).
    changed = tmp_path / "plain"
    shutil.copytree(FIXTURE, changed)
    (changed / "src" / "trim.js").write_text(
        "export const trim = (s) => String(s).replace(/^-+|-+$/g, \"\");\n")
    pipeline._engine_env("", fixture=changed)
    before = pipeline.branch_heads(built["repo"])
    with pipeline.engine.begin() as c:
        started = code_task_service.start_followup_task(
            c, parent_run_id=built["run_id"],
            instructions="Also trim leading and trailing hyphens from the slug.",
            jobs=pipeline.jobs(built["repo"]), title="Trim hyphens", actor="operator",
            is_person_request=True)
    follow = str(started["run_id"])
    error = pipeline.run(follow, built["repo"])
    out = pipeline.result(follow)
    run = out["run"]
    assert error is None, error
    assert run["status"] == "completed", (run["status"], run.get("error"))
    # BOSSAPP-1395: while the parent's merge card is pending, a GitHub follow-up isolates onto
    # its own branch cut from the parent's approved head and opens a stacked PR instead of
    # pushing onto the parent's branch/PR.
    assert run["pull_request_url"] != built["run"]["pull_request_url"]
    assert out["writes"], "the follow-up never wrote"
    after = pipeline.branch_heads(built["repo"])
    parent_branch = f"boss/code/{built['run_id']}"
    followup_branch = f"boss/code/{follow}"
    assert after.get(parent_branch) == before.get(parent_branch), "parent branch must stay pinned"
    assert followup_branch in after and after[followup_branch] != after[parent_branch]


# ── a re-check of that change ────────────────────────────────────────────────────────────────

def test_a_recheck_judges_the_branch_head_and_writes_nothing(pipeline, built):
    from boss_engine.services import change_recheck

    pipeline._engine_env("")
    pr = built["run"]["pull_request_url"]
    latest = _latest_with_pr(pipeline, pr)
    before = pipeline.branch_heads(built["repo"])
    asked = change_recheck.request(pipeline.engine, pipeline.settings(built["repo"]),
                                   pipeline.jobs(built["repo"]), latest,
                                   project_id=pipeline.project, actor="operator",
                                   by_person=True)
    rid = asked["run_id"]
    error = pipeline.run(rid, built["repo"])
    out = pipeline.result(rid)
    run = out["run"]
    assert error is None, error
    assert run["finalize_ctx"].get(change_recheck.KEY) is True
    # A clean branch head passes every gate again.
    assert run["status"] == "completed", (run["status"], run.get("error"))
    # NOTHING WRITTEN: no writing seat played, no model call for a writing stage, and the
    # remote's branches are exactly as they were.
    assert out["writes"] == [], out["writes"]
    assert not [c for c in out["calls"] if c["stage"] in change_recheck.WRITING_STAGES]
    assert pipeline.branch_heads(built["repo"]) == before
    # EVERY GATE RAN on the branch head.
    assert "acceptance_check" in out["stages"], out["stages"]
    assert any(s in out["stages"] for s in ("review_code", "review_panel")), out["stages"]
    # THE CARD: the PR's merge card is the re-check's, and the older one is withdrawn.
    cards = pipeline.cards(pr)
    mine = [c for c in cards if c["run_id"] == rid and c["action"] == "review_pull_request"]
    assert mine and mine[-1]["status"] == "pending", cards
    older = [c for c in cards if c["action"] == "review_pull_request" and c["run_id"] != rid]
    assert older and all(c["status"] == "withdrawn" for c in older), cards
    # NO BUILD STARTED: no follow-up, no repair child of the re-check.
    assert out["children"] == [], out["children"]


def _latest_with_pr(pipeline: Pipeline, pr: str) -> str:
    with pipeline.engine.begin() as c:
        return str(c.execute(pipeline.text(
            "SELECT id FROM code_task_runs WHERE project_id = :p AND pull_request_url = :u "
            "AND status = 'completed' ORDER BY created_at DESC LIMIT 1"),
            {"p": pipeline.project, "u": pr}).scalar())


# ── the no-write agent ───────────────────────────────────────────────────────────────────────

def test_the_no_write_agent_has_only_the_knobs_it_declares():
    from boss_engine.services.change_recheck import NoWriteAgent

    agent = NoWriteAgent("implement")
    assert agent.run_role("x", ".")["cost_usd_micros"] == 0
    with pytest.raises(AttributeError):
        agent.set_budget_ceiling(1)            # an unknown method is an error, never None()
    with pytest.raises(AttributeError):
        _ = agent.anything_else

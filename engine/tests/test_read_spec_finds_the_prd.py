"""BOSS can read the document that says what an app was FOR.

It could read its own source, its logs, its hosts and arbitrary SQL, and it could not read a
PRD. Asked "what should we build next for this app", it chained tools for two minutes and
answered that "the PR description, diff, and any related PRD documents are not present in the
searchable data" — while the PRD sat in `meeting_runs.state`, openable in the console, two
clicks away from the question being asked.

The console papered over it by prepending a block of run facts to every message the operator
typed. That was visible in their own chat bubble, and it still did not answer the question,
because a summary is not a spec.

A spec reaches an app by three routes and no single table holds all three, so `read_spec`
checks all three and reports which one answered. These pin each route, because "not found" on
a document that exists via a route nobody checked is exactly the failure being fixed.

Runs against `boss_test` on a schema it creates itself — never the dev database, which a
pytest run is known to wipe.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.tools.builtin import build_default_registry, build_discuss_read_registry

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"
)

# The three tables these routes live in. `boss_test` normally already carries the full schema
# (so these are no-ops with the real foreign keys in force); the definitions are here so the
# file also runs against an empty database.
DDL = """
CREATE TABLE IF NOT EXISTS code_task_runs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT,
  status TEXT NOT NULL DEFAULT 'completed', phase TEXT NOT NULL DEFAULT 'done',
  title TEXT NOT NULL,
  spec JSONB NOT NULL DEFAULT '{}'::jsonb, state JSONB NOT NULL DEFAULT '{}'::jsonb,
  pull_request_url TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS meeting_runs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT,
  directive TEXT NOT NULL DEFAULT '', state JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT,
  title TEXT NOT NULL, content JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
"""


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            for stmt in filter(None, (s.strip() for s in DDL.split(";"))):
                c.execute(text(stmt))
    except Exception as error:                       # no local postgres in this environment
        pytest.skip(f"boss_test unavailable: {error}")
    return engine


@pytest.fixture()
def ctx(eng):
    class Ctx:
        def begin(self):
            return eng.begin()

    return Ctx()


@pytest.fixture()
def project(eng):
    """A real project row, because code_task_runs.project_id is a foreign key.

    Reuses the existing workspace rather than building an org -> workspace -> project chain:
    the point of this file is the three spec routes, not tenancy setup.
    """
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("boss_test has no workspace to attach a project to")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'read_spec test', :s)"),
                  {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        for table in ("artifacts", "meeting_runs", "code_task_runs", "tasks"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


@pytest.fixture()
def other_project(eng):
    """A SECOND, genuinely different project — the only way to prove project pinning holds."""
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("boss_test has no workspace to attach a project to")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'read_spec other', :s)"),
                  {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        c.execute(text("DELETE FROM code_task_runs WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


@pytest.fixture()
def call(ctx):
    tool = {s.name: s for s in build_default_registry().specs()}["read_spec"]

    def run(project_id: str, **args):
        return tool.handler(ctx, project_id=project_id, actor="ceo", args=args)

    return run


def _task(eng, project) -> str:
    """A real task row — artifacts.task_id is a foreign key into `tasks`."""
    tid = f"tsk_{uuid.uuid4().hex[:8]}"
    with eng.begin() as c:
        cols = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='tasks'"))}
        fields = {"id": tid, "project_id": project}
        if "title" in cols:
            fields["title"] = "read_spec test"
        if "status" in cols:
            fields["status"] = "done"
        if "dedupe_key" in cols:                     # NOT NULL with no default
            fields["dedupe_key"] = tid
        names = ", ".join(fields)
        binds = ", ".join(f":{k}" for k in fields)
        c.execute(text(f"INSERT INTO tasks ({names}) VALUES ({binds})"), fields)
    return tid


def _run(eng, project, *, task_id=None, title="UnitFlip", spec=None, state=None, pr=None):
    rid = f"crun_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO code_task_runs (id, project_id, task_id, title, spec, state, "
                 "pull_request_url) VALUES (:i, :p, :t, :ti, CAST(:s AS JSONB), "
                 "CAST(:st AS JSONB), :pr)"),
            {"i": rid, "p": project, "t": task_id, "ti": title,
             "s": json.dumps(spec or {}), "st": json.dumps(state or {}), "pr": pr},
        )
    return rid


def test_read_spec_is_available_to_the_chat_agent():
    """A tool nobody can call fixes nothing. The discuss path is where the question is asked."""
    assert "read_spec" in {s.name for s in build_discuss_read_registry().specs()}


def test_a_direct_code_task_answers_from_its_brief(eng, call, project):
    """No meeting: the brief the task was created with IS the spec."""
    rid = _run(eng, project,
               spec={"instructions": "Build a unit converter.",
                     "acceptance_criteria": ["converts length", "converts mass"]},
               state={"impl_summary": "shipped", "tests_passed": True},
               pr="https://github.com/x/y/pull/1")
    out = call(project, run_id=rid)
    assert out["ok"] is True
    assert out["prd_or_brief"] == "Build a unit converter."
    assert out["acceptance_criteria"] == ["converts length", "converts mass"]
    assert out["pull_request_url"].endswith("/pull/1")
    assert out["source"] == "code_task_runs.spec.instructions"


def test_a_meeting_born_app_answers_with_the_actual_prd(eng, call, project):
    """The route that failed: the PRD lives on the MEETING, joined by task_id.

    The run's own `instructions` is a directive, not the document. Returning the directive and
    calling it the PRD is how "read the PRD" produced a one-line summary and the operator
    concluded BOSS could not read.
    """
    task = f"tsk_{uuid.uuid4().hex[:8]}"
    prd = "# PRD: UnitFlip\n\n## Goals\nConvert units offline.\n\n## Out of scope\nAccounts."
    with eng.begin() as c:
        c.execute(text("INSERT INTO meeting_runs (id, project_id, task_id, directive, state) "
                       "VALUES (:i, :p, :t, :d, CAST(:s AS JSONB))"),
                  {"i": f"mrun_{uuid.uuid4().hex[:10]}", "p": project, "t": task,
                   "d": "Spec and build UnitFlip", "s": json.dumps({"prd_markdown": prd})})
    rid = _run(eng, project, task_id=task, spec={"instructions": "Implement the approved plan"})
    out = call(project, run_id=rid)
    assert out["prd_or_brief"] == prd, "returned the directive instead of the PRD"
    assert "meeting_runs.state.prd_markdown" in out["source"]


def test_a_published_prd_document_is_found_when_the_meeting_state_is_empty(eng, call, project):
    """Third route: finalized PRDs are also published as artifacts."""
    task = _task(eng, project)
    with eng.begin() as c:
        c.execute(text("INSERT INTO artifacts (id, project_id, task_id, artifact_type, title, "
                       "content) VALUES (:i, :p, :t, 'prd', :ti, CAST(:c AS JSONB))"),
                  {"i": f"art_{uuid.uuid4().hex[:8]}", "p": project, "t": task,
                   "ti": "PRD: UnitFlip", "c": json.dumps({"markdown": "# UnitFlip\nScope."})})
    rid = _run(eng, project, task_id=task, spec={"instructions": "Implement the approved plan"})
    out = call(project, run_id=rid)
    assert out["prd_or_brief"].startswith("# UnitFlip")
    assert out["source"].startswith("artifacts")


def test_a_meeting_run_id_can_be_read_directly(eng, call, project):
    """The operator's link is `?area=meetings&meeting=mrun_…`, so mrun_ must resolve."""
    mid = f"mrun_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO meeting_runs (id, project_id, task_id, directive, state) "
                       "VALUES (:i, :p, NULL, :d, CAST(:s AS JSONB))"),
                  {"i": mid, "p": project, "d": "Build it",
                   "s": json.dumps({"prd_markdown": "# The PRD", "synthesis": "agreed"})})
    out = call(project, run_id=mid)
    assert out["ok"] is True
    assert out["prd_markdown"] == "# The PRD"
    assert out["synthesis"] == "agreed"


def test_an_app_can_be_named_instead_of_identified(eng, call, project):
    """Operators say "UnitFlip", not "crun_9c2f…"."""
    _run(eng, project, title="UnitFlip", spec={"instructions": "old build"})
    _run(eng, project, title="UnitFlip", spec={"instructions": "newest build"})
    out = call(project, app="unitflip")
    assert out["prd_or_brief"] == "newest build", "picked a stale rebuild over the current one"


def test_it_cannot_read_another_project(eng, call, project, other_project):
    """Project pinning is the only thing standing between tenants and each other's specs."""
    rid = _run(eng, other_project, spec={"instructions": "secret"})
    out = call(project, run_id=rid)
    assert out["ok"] is False


def test_a_missing_app_says_so_rather_than_returning_someone_elses(call, project):
    out = call(project, app="does-not-exist")
    assert out["ok"] is False and "does-not-exist" in out["error"]


def test_the_gap_between_intent_and_delivery_is_readable_in_one_call(eng, call, project):
    """The question this exists for: "which criteria are not covered by a test?"

    Answering it needs the criteria AND what was built AND what the reviewer said, together.
    Spread across three tools it cost two minutes and produced a shrug.
    """
    rid = _run(eng, project,
               spec={"instructions": "Build it", "test_commands": ["npm test"],
                     "acceptance_criteria": ["a", "b", "c"]},
               state={"impl_summary": "did a and b", "review_summary": "c is untested",
                      "impl_files": ["src/a.ts"], "tests_passed": True})
    out = call(project, run_id=rid)
    assert out["acceptance_criteria"] == ["a", "b", "c"]
    assert out["implementation_summary"] == "did a and b"
    assert out["review_summary"] == "c is untested"
    assert out["files_changed"] == ["src/a.ts"]
    assert out["test_commands"] == ["npm test"]

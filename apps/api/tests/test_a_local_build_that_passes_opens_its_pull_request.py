"""A LOCAL BUILD THAT PASSES OPENS ITS PULL REQUEST, AND A COMPLETED BUILD NEVER ENDS SILENT.

THE CASE (2026-11-18, laptop, main fed039382): Easy Mode › "Census slice" built crun_5b11643d on
the project's local repository. Every gate passed; the run ended `completed/done` with no pull
request, no merge card and no change set, and nothing on the run said why. Easy Mode started its
builds with `open_pr=False` (meant for evaluation runs), and the governed lane's no-PR path only
emitted an event. Local repositories do get their PR elsewhere (Northgate's
http://boss-local/local_northgate-gateway/northgate-gateway/pull/2).

  - Easy Mode's real build start (factory_launch.build_run → start_code_task) records a build
    that OWES its pull request (pull_request_owed.owed is None), on the local repository;
  - that build's delivery step on a local repository opens the change set at a boss-local URL
    and raises the merge card;
  - a build that skips its PR on purpose says why, in words, on the run;
  - the guard: `completed` with no PR and no reason cannot be stored (db trigger).

Real git in temporary bare repositories and the real database; no model.
"""
from __future__ import annotations

import inspect
import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.schemas import FileChange
from boss_engine.services import local_projects, local_repo, pull_request_owed


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    return root


def _local_project(engine):
    pid = f"census-{uuid.uuid4().hex[:8]}"
    with engine.begin() as c:
        ws = c.execute(text("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")).scalar()
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:id, :ws, :n, :id)"), {"id": pid, "ws": ws, "n": f"Census {pid[-4:]}"})
        bound = local_projects.create_for_project(c, get_settings(), pid, name="census-slice",
                                                  actor="t")
    bare = local_repo.bare_path(bound["owner"], bound["repo"])
    local_repo.commit_files(bare, "main", {"README.md": "census\n"}, "base")
    return pid, bound, bare


def _easy_mode_build(engine, pid, bound) -> str:
    from apps.api.app import main as api_main
    from boss_engine.services import factory_launch

    with engine.begin() as c:
        out = factory_launch.build_run(
            c, project_id=pid,
            spec={"id": "census-slice", "title": "Census slice", "prompt": "Build a census slice.",
                  "mode": "template", "local_repo": f"{bound['owner']}/{bound['repo']}"},
            actor="ceo", settings=get_settings(), jobs=api_main.jobs, reservation_id="res_x")
    assert out["run_id"], out
    return out["run_id"]


def _run(engine, run_id):
    with engine.connect() as c:
        row = c.execute(text("SELECT spec, finalize_ctx, status, pull_request_url "
                             "FROM code_task_runs WHERE id = :r"), {"r": run_id}).mappings().one()
    return {k: (json.loads(v) if isinstance(v, str) and k in ("spec", "finalize_ctx") else v)
            for k, v in row.items()}


def test_an_easy_mode_build_owes_its_pull_request_on_its_local_repository(engine, repo_root):
    pid, bound, _ = _local_project(engine)
    run_id = _easy_mode_build(engine, pid, bound)
    run = _run(engine, run_id)
    spec = run["spec"]
    assert spec.get("open_pr") is not False
    assert (spec["owner"], spec["repo"], spec.get("repo_host")) == (
        bound["owner"], bound["repo"], "local")
    assert pull_request_owed.owed(spec, run["finalize_ctx"]) is None


def test_the_delivery_step_opens_a_boss_local_change_set_and_its_merge_card(engine, repo_root):
    """What the delivery step does once the build owes its PR (governed lane, opening_pr):
    software_factory.propose_pull_request on the project's local host."""
    from boss_engine.services.software_factory import SoftwareFactoryService

    pid, bound, bare = _local_project(engine)
    run_id = _easy_mode_build(engine, pid, bound)
    branch = f"boss/code/{run_id}"
    local_repo.ensure_branch(bare, branch)
    local_repo.commit_files(bare, branch, {"src/census.py": "ROWS = 3\n"}, "census slice")

    sf = SoftwareFactoryService(get_settings())
    with engine.begin() as c:
        sf.bind_project_github(c, pid)
        made = sf.budget.reserve(c, pid, category="code_task", amount_usd_micros=1000,
                                 reason="census PR", expires_in_minutes=30)
    assert made.get("status") == "reserved", made
    with engine.begin() as c:
        proposal = sf.propose_pull_request(
            c, pid, None, reservation_id=made["reservation_id"], dedupe_key=f"code:task:{run_id}",
            title="Census slice", branch_name=branch, summary="A census slice.",
            files=[FileChange(path="src/census.py", content="ROWS = 3\n")], run_id=run_id,
            pr_base="main")

    assert proposal["status"] == "created", proposal
    url = str(proposal["pull_request_url"])
    assert url.startswith(f"http://boss-local/{bound['owner']}/{bound['repo']}/pull/"), url
    with engine.connect() as c:
        change = c.execute(text("SELECT state, run_id FROM local_change_sets WHERE repository = :r"),
                           {"r": f"{bound['owner']}/{bound['repo']}"}).mappings().one()
        card = c.execute(text("SELECT status, recommendation FROM decisions WHERE id = :d"),
                         {"d": proposal["decision_id"]}).mappings().one()
    assert change["state"] == "open" and change["run_id"] == run_id
    card_says = card["recommendation"]
    card_says = card_says if isinstance(card_says, dict) else json.loads(card_says)
    assert card["status"] == "pending"
    assert card_says.get("action") == "review_pull_request"
    assert card_says.get("pull_request_url") == url


def test_a_build_that_skips_its_pull_request_on_purpose_says_why():
    assert pull_request_owed.owed({}, {}) is None
    evaluation = pull_request_owed.owed({"open_pr": False}, {}, branch="boss/code/crun_1")
    assert evaluation["reason"] == "open_pr_false"
    assert "boss/code/crun_1" in evaluation["words"] and " " in evaluation["words"]
    sliced = pull_request_owed.owed({}, {"decomposition_index": 2, "decomposition_total": 5})
    assert sliced["reason"] == "decomposition_slice" and "2 of 5" in sliced["words"]
    assert pull_request_owed.owed({}, {"decomposition_index": 5, "decomposition_total": 5}) is None

    # The governed lane asks this one decision at both places, and writes its answer on the run.
    from boss_engine.services import governed_code_task_service as governed

    source = inspect.getsource(governed)
    assert source.count("_pr_owed.owed(spec, fctx") == 2
    assert "_pr_owed.record_clause()" in source
    assert 'spec.get("open_pr", True) is False' not in source


def _bare_run(engine, pid, *, status="running", pr=None, fctx=None) -> str:
    run_id = "crun_guard" + uuid.uuid4().hex[:10]
    with engine.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, "
                       "finalize_ctx, pull_request_url) VALUES (:id, :p, 'Guard', :st, 'x', "
                       "'{}'::jsonb, CAST(:f AS JSONB), :u)"),
                  {"id": run_id, "p": pid, "st": status, "u": pr, "f": json.dumps(fctx or {})})
    return run_id


def _complete(engine, run_id, extra: str = "", params=None):
    with engine.begin() as c:
        c.execute(text(f"UPDATE code_task_runs SET status = 'completed', phase = 'done'{extra} "
                       "WHERE id = :id"), {"id": run_id, **(params or {})})


def test_completed_with_no_pull_request_and_no_reason_cannot_be_stored(engine, repo_root):
    pid, _, _ = _local_project(engine)
    silent = _bare_run(engine, pid)
    _complete(engine, silent)
    said = _run(engine, silent)["finalize_ctx"][pull_request_owed.KEY]
    assert said["reason"] == "unexplained" and " " in said["words"]

    # Inserted completed (a fixture, a replay): the same.
    inserted = _bare_run(engine, pid, status="completed")
    assert _run(engine, inserted)["finalize_ctx"][pull_request_owed.KEY]["reason"] == "unexplained"

    # A reason the build wrote is kept, not replaced.
    why = pull_request_owed.owed({"open_pr": False}, {})
    meant = _bare_run(engine, pid)
    _complete(engine, meant, ", " + pull_request_owed.record_clause(),
              {"no_pr": pull_request_owed.record_value(why)})
    assert _run(engine, meant)["finalize_ctx"][pull_request_owed.KEY] == why

    # With a pull request there is nothing to explain.
    opened = _bare_run(engine, pid)
    _complete(engine, opened, ", pull_request_url = 'http://boss-local/x/y/pull/1'")
    assert pull_request_owed.KEY not in _run(engine, opened)["finalize_ctx"]

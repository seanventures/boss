"""REQUEST CHANGES ON A LOCAL-FIRST PROJECT, WITH THE STANDARD CONSENT (BOSSAPP-1163).

BOSS found this building a project with code "On this machine": Request changes on the merge
decision opens a follow-up build for the Software Engineer on the local repository, and the
person's 30-day grant — minted with no repositories — did not cover it: 409 consent_required for
good, and the console opened no sheet. Through the real routes, the real decision and a real local
repository:

  1. with no grant, Request changes answers the flat 409 whose body names the exact missing
     resource: `local.repos == ["local_<project>/<name>"]`, no GitHub block at all;
  2. the standard consent — the sheet's grant body for that 409, `kind: standing` — is accepted
     (`local` is a resource the API knows) and the delegation lists the repository on this machine;
  3. the same Request changes then succeeds: the decision is sent back and the fix round starts.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.identity import consent
from boss_engine.services import local_projects, local_repo
from boss_engine.services.local_repo_host import LocalGitAdapter

H = {"sec-fetch-site": "same-origin"}


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("lc-owner")
    w.bind(w.owner, "owner")
    # The Software Engineer the follow-up runs as: a Passport whose ceiling holds a build's needs.
    w.builder = w.agent("forge", ceiling=tuple(sorted(consent.CODE_TASK_PERMISSIONS | {"code_task:create"})),
                        sponsor=w.owner)
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    return root


def _owner(world):
    return world.acting_as(world.ctx(world.owner, mfa=True))


APP_V1 = "".join(f"line {i}\n" for i in range(1, 11))


def _local_project_with_change(engine, world):
    """A fresh local-first project with the builder in its roster, a build's branch with two
    commits, its open change set #1 and the pending merge decision."""
    from boss_engine.services.core import create_blocking_decision

    pid = f"lc-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                        {"p": world.project_a}).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:id, :ws, :n, :id)"),
                   {"id": pid, "ws": ws, "n": f"Desk {pid[-4:]}"})
        db.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, agent_id) VALUES "
                        "(:id, :p, 'forge', 'Software Engineer', :a)"),
                   {"id": f"role_{uuid.uuid4().hex[:8]}", "p": pid, "a": world.builder.id})
        bound = local_projects.create_for_project(db, get_settings(), pid, name="desk", actor="t")
    bare = local_repo.bare_path(bound["owner"], bound["repo"])
    local_repo.commit_files(bare, "main", {"src/app.js": APP_V1}, "base files")
    run_id = "crun_lc" + uuid.uuid4().hex[:8]
    branch = f"boss/code/{run_id}"
    local_repo.ensure_branch(bare, branch)
    local_repo.commit_files(bare, branch, {"src/app.js": APP_V1.replace("line 5\n", "line five\n")},
                            "change line 5")
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, finalize_ctx) "
            "VALUES (:id, :p, 'SLA timer', 'completed', 'done', CAST(:s AS JSONB), '{}'::jsonb)"),
            {"id": run_id, "p": pid, "s": json.dumps({"owner": bound["owner"], "repo": bound["repo"]})})
    adapter = LocalGitAdapter(get_settings(), owner=bound["owner"], repo=bound["repo"], project_id=pid)
    pr = adapter.create_pull_request(branch, "Add an SLA timer", "", run_id=run_id)
    with engine.begin() as db:
        decision_id = create_blocking_decision(
            db, pid, None, "Approve code change: Add an SLA timer (change #1)",
            {"action": "review_pull_request", "pull_request_url": pr.url,
             "head_sha": local_repo.branch_sha(bare, branch), "branch_name": branch, "title": "Add an SLA timer"},
            {}, "review")
    return pid, f"{bound['owner']}/{bound['repo']}", run_id, decision_id


def test_request_changes_on_a_local_project_asks_for_the_local_repository_and_the_standard_consent_covers_it(
        client, world, engine, repo_root):
    pid, local_name, run_id, decision_id = _local_project_with_change(engine, world)
    assert local_name == f"local_{pid}/desk"

    # 1. No grant: the flat 409 names exactly what is missing — the repository on this machine.
    with _owner(world):
        asked = client.post(f"/v1/projects/{pid}/local-changes/1/request-changes",
                            json={"note": "Please handle weekends."}, headers=H)
    assert asked.status_code == 409, asked.text
    body = asked.json()
    assert body["error"] == "consent_required"
    assert body["github"] is None, "a repository on this machine is never asked for as GitHub access"
    assert body["local"] == {"repo": local_name, "repos": [local_name], "summary": "on this machine"}
    assert [a["id"] for a in body["agents"]] == [world.builder.id]
    assert body["projects"] == [pid] and set(body["permissions"]) <= consent.CODE_TASK_PERMISSIONS
    assert {d["kind"] for d in body["durations"]} == {"task", "standing"}
    with engine.connect() as db:
        assert db.execute(text("SELECT status FROM decisions WHERE id = :i"), {"i": decision_id}).scalar() == "pending"

    # 2. The standard consent: the sheet's grant body for that 409 (lib/identity/consent.ts
    #    grantBodyFor), "For 30 days".
    standing = next(d for d in body["durations"] if d["kind"] == "standing")
    grant_body = {"agents": [a["id"] for a in body["agents"]], "permissions": body["permissions"],
                  "projects": body["projects"], "kind": "standing", "days": standing["days"],
                  "purpose": body["task"], "local": {"repos": body["local"]["repos"]}}
    with _owner(world):
        made = client.post("/v1/identity/me/delegations", json=grant_body, headers=H)
        assert made.status_code == 201, made.text
        assert made.json()["local"] == {"repos": [local_name]} and made.json()["github"] is None
        listed = client.get("/v1/identity/me/delegations", headers=H).json()["delegations"]
    assert any(d["id"] == made.json()["id"] and d["local"]["repos"] == [local_name] for d in listed), \
        "Account › Agents lists the repository on this machine the grant covers"

    # 3. The same press succeeds: the decision is sent back and the fix round starts on the branch.
    with _owner(world):
        again = client.post(f"/v1/projects/{pid}/local-changes/1/request-changes",
                            json={"note": "Please handle weekends."}, headers=H)
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "revision_requested"
    with engine.connect() as db:
        status = db.execute(text("SELECT status FROM decisions WHERE id = :i"), {"i": decision_id}).scalar()
        fix = db.execute(text("SELECT run_id FROM local_change_sets WHERE project_id = :p"), {"p": pid}).scalar()
        spec = db.execute(text("SELECT spec FROM code_task_runs WHERE id = :r"), {"r": fix}).scalar()
        jobs = db.execute(text("SELECT count(*) FROM jobs WHERE project_id = :p AND kind = 'code.task'"),
                          {"p": pid}).scalar()
    assert status == "revision_requested" and fix and fix != run_id and jobs >= 1
    assert (spec["owner"], spec["repo"]) == tuple(local_name.split("/"))

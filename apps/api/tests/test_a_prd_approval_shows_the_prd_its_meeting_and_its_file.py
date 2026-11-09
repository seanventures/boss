"""A PRD APPROVAL SHOWS THE PRD, ITS MEETING AND ITS FILE (BOSSAPP-929, FB-1).

The operator: "The PRD is never uploaded to the git? in the approve PRD there's no link or any doc /
pdf to look into the PRD before approval, we need to also give the link to the place where the
discussion happened and PRD generated especially the meeting panel".

A panel finishes (meeting_service._finalize_meeting, the real path) and raises its PRD decision.
Every surface reads the same record from that decision (prd_record.links): Approvals (the inbox
and one decision), the project's Needs you, the meeting room and the work drawer. The record
carries the PRD to read, the meeting (agenda, each agent's position, dissent) and the file in the
repository. A build commits the PRD and the minutes to its branch through the repo host its stages
use — GitHub (the plumbing gate's fake GitHub) or a local-first project's own repository — and the
record then links the committed file.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.services import local_repo, transient_retry

_GATE = Path(__file__).resolve().parents[3] / "tests" / "e2e" / "gate"
if str(_GATE) not in sys.path:
    sys.path.insert(0, str(_GATE))
from remote import LocalGitHub  # noqa: E402  (tests/e2e/gate/remote.py)

PRD = ("# PRD: Greeting Service\n\n## 1. Summary\n\nA polite greeting API with metrics.\n\n"
       "## 7. Proposed Solution\n\n" + "Serve GET /greet with a name and count every call. " * 12
       + "\n")
SYNTHESIS = ("## Converged output\n\nThe panel agreed on a small greeting API.\n\n"
             "### Recorded dissent\n\n- QA: metrics should be opt-in for privacy\n\n"
             "## Open questions\n\n- Rate limits?\n")


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("prd-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def hub(tmp_path):
    github = LocalGitHub(tmp_path / "github").start()
    yield github
    github.stop()


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    monkeypatch.setattr(transient_retry, "_sleep", lambda _s: None)
    return root


def _project(world, client) -> str:
    slug = f"greet-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "Greeting", "slug": slug})
    assert made.status_code == 201, made.text
    return slug


def _bind_github(db, pid: str, full_name: str) -> None:
    conn = f"ghc_{uuid.uuid4().hex[:10]}"
    db.execute(text(
        "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
        "credential_ref, status, created_by) VALUES (:c, :p, 'seed', 'personal_access_token', "
        "'secret:none', 'active', 'seed')"), {"c": conn, "p": pid})
    owner, name = full_name.split("/")
    db.execute(text(
        "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
        "full_name, role, default_branch, created_by) VALUES (:i, :p, :c, :o, :n, :r, 'primary', "
        "'main', 'seed')"), {"i": f"ghb_{uuid.uuid4().hex[:10]}", "p": pid, "c": conn, "o": owner,
                             "n": name, "r": full_name})


def _panel(engine, pid: str, monkeypatch) -> tuple[str, str]:
    """A finished panel through the real finalize: (meeting run id, its PRD decision id)."""
    from boss_engine.services import meeting_service

    settings = get_settings()
    monkeypatch.setattr(settings, "meeting_prd_auto_approve", False, raising=False)
    run_id = f"mrun_{uuid.uuid4().hex[:12]}"
    state = {
        "round1": {"product_manager": "## Ship the greeting first\n\nUsers want a hello that "
                                      "knows their name before anything else.",
                   "qa": "## Test every greeting path\n\nNames with accents must round-trip "
                         "through the API unchanged."},
        "round2": {"product_manager": "## I agree with QA on accents\n\nBut metrics stay on by "
                                      "default so we can see adoption.",
                   "qa": "## I disagree on default metrics\n\nCounting calls should be opt-in "
                         "for privacy reasons."},
        "synthesis": SYNTHESIS, "prd_markdown": PRD,
    }
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO meeting_runs (id, project_id, status, phase, directive, title, role_keys, "
            "state) VALUES (:i, :p, 'running', 'finalize', :d, 'Greeting Service', "
            "CAST(:rk AS JSONB), CAST(:s AS JSONB))"),
            {"i": run_id, "p": pid, "d": "Build a greeting API that says hello by name",
             "rk": json.dumps(["product_manager", "qa"]), "s": json.dumps(state)})
        done = meeting_service._finalize_meeting(db, settings, run_id)
    assert done["decision_id"], done
    return run_id, done["decision_id"]


def _get(world, client, path: str) -> dict:
    with world.acting_as(world.ctx(world.owner)):
        r = client.get(path)
    assert r.status_code == 200, r.text
    return r.json()


def _inbox(client, pid: str) -> list[dict]:
    """The inbox as the console's own reads take it (a writing GET; the dev principal, as the
    other inbox tests read it)."""
    r = client.get("/v1/decisions/inbox", params={"project_id": pid})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _inbox_prd(world, client, pid: str, decision_id: str) -> dict:
    items = _inbox(client, pid)
    card = next(i for i in items if i["id"] == decision_id)
    assert card["prd"], card
    return card["prd"]


def test_approvals_show_the_prd_its_meeting_and_where_it_will_live(world, client, engine,
                                                                   monkeypatch):
    pid = _project(world, client)
    with engine.begin() as db:
        _bind_github(db, pid, "acme/greeting")
    run_id, decision_id = _panel(engine, pid, monkeypatch)
    prd = _inbox_prd(world, client, pid, decision_id)
    assert prd["markdown"].startswith("# PRD: Greeting Service")
    assert prd["title"] == "Greeting Service"
    m = prd["meeting"]
    assert m["run_id"] == run_id and f"meeting={run_id}" in m["href"] and "area=meetings" in m["href"]
    assert prd["full_page"] == m["href"]
    assert "greeting API" in m["agenda"]["brief"]
    assert m["agenda"]["participants"][:2] == ["Product Manager", "QA / Test"]
    seats = {p["seat"]: p for p in m["positions"]}
    assert seats["product_manager"]["position"] == "Ship the greeting first"
    assert seats["qa"]["debate"] == "I disagree on default metrics"
    assert [d["position"] for d in m["dissent"]] == ["QA: metrics should be opt-in for privacy"]
    assert prd["repo"] == {"path": "docs/prd/greeting-service.md",
                           "minutes_path": "docs/prd/greeting-service-meeting.md",
                           "repository": "acme/greeting", "committed": False}
    # One decision, every door: the decision by id, the project's Needs you, the meeting room.
    one = _get(world, client, f"/v1/decisions/{decision_id}")
    assert one["prd"]["meeting"]["run_id"] == run_id
    needs = _get(world, client, f"/v1/projects/{pid}/overview")["needs_you"]
    card = next(n for n in needs if n["id"] == decision_id)
    assert card["prd"]["repo"]["path"] == "docs/prd/greeting-service.md"
    assert card["label"].startswith("Approve PRD")
    room = _get(world, client, f"/v1/meeting-rooms/{run_id}?project_id={pid}")
    room = room.get("item", room) if isinstance(room, dict) else room
    assert room["prd_repo"]["path"] == "docs/prd/greeting-service.md"


def test_a_build_commits_the_prd_and_minutes_to_its_github_branch(world, client, engine, hub,
                                                                  monkeypatch):
    from boss_engine.services import prd_record
    from boss_engine.services.github_adapter import GitHubAdapter

    pid = _project(world, client)
    hub.seed("acme", "greeting", {"README.md": "# greeting\n"})
    with engine.begin() as db:
        _bind_github(db, pid, "acme/greeting")
    run_id, decision_id = _panel(engine, pid, monkeypatch)
    adapter = GitHubAdapter(get_settings(), owner="acme", repo="greeting", token="gate-token",
                            api_base_url=hub.api_url)
    branch = "boss/code/crun_prdtest"
    adapter.ensure_branch(branch)
    with engine.begin() as db:
        record = prd_record.commit_to_branch(db, adapter, project_id=pid, decision_id=decision_id,
                                             branch=branch)
    assert record["path"] == "docs/prd/greeting-service.md" and record["branch"] == branch
    assert adapter.read_text_file(branch, "docs/prd/greeting-service.md").strip() == PRD.strip()
    minutes = adapter.read_text_file(branch, "docs/prd/greeting-service-meeting.md")
    assert "## Each agent's position" in minutes and "### QA / Test" in minutes
    assert "metrics should be opt-in" in minutes and "## Synthesis" in minutes
    # Committing again changes nothing (the same PRD is already on the branch).
    with engine.begin() as db:
        again = prd_record.commit_to_branch(db, adapter, project_id=pid, decision_id=decision_id,
                                            branch=branch)
    assert again["commit"] is None
    repo = _inbox_prd(world, client, pid, decision_id)["repo"]
    assert repo["committed"] is True and repo["branch"] == branch
    assert repo["url"] == f"https://github.com/acme/greeting/blob/{branch}/docs/prd/greeting-service.md"


def test_a_local_first_project_commits_the_prd_to_its_own_repository(world, client, engine,
                                                                      repo_root, monkeypatch):
    from boss_engine.services import local_projects, prd_record
    from boss_engine.services.local_repo_host import LocalGitAdapter

    settings = get_settings()
    pid = _project(world, client)
    with engine.begin() as db:
        bound = local_projects.create_for_project(db, settings, pid, name="greeting", actor="t")
    run_id, decision_id = _panel(engine, pid, monkeypatch)
    adapter = LocalGitAdapter(settings, owner=bound["owner"], repo=bound["repo"], project_id=pid,
                              engine=engine)
    branch = "boss/code/crun_localprd"
    adapter.ensure_branch(branch)
    with engine.begin() as db:
        record = prd_record.commit_to_branch(db, adapter, project_id=pid, decision_id=decision_id,
                                             branch=branch)
    assert record["commit"]
    bare = local_repo.bare_path(bound["owner"], bound["repo"])
    assert local_repo.read_file(bare, branch, "docs/prd/greeting-service.md").strip() == PRD.strip()
    assert "Each agent's position" in local_repo.read_file(bare, branch,
                                                           "docs/prd/greeting-service-meeting.md")
    repo = _inbox_prd(world, client, pid, decision_id)["repo"]
    # A local repository has no web page: the file opens in BOSS's own document drawer.
    assert repo["committed"] is True and repo["url"].startswith(f"?project_id={pid}")
    assert "drawer=doc" in repo["url"]


def test_a_decision_that_is_not_a_prd_carries_no_record(world, client, engine):
    pid = _project(world, client)
    did = f"dec_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO decisions (id, project_id, question, recommendation) VALUES "
                        "(:i, :p, 'Merge it?', CAST(:r AS JSONB))"),
                   {"i": did, "p": pid, "r": json.dumps({"action": "review_pull_request"})})
    items = _inbox(client, pid)
    assert next(i for i in items if i["id"] == did)["prd"] is None

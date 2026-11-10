"""A LOCAL PROJECT NEVER SAYS GITHUB (BOSSAPP-946, finding 8).

The local-first HelpDesk's pages said "PR #1", "Merge PR #1", "Pull request", "Idea to PR" and
offered its merge as "Approve and retry", for a project with no GitHub anywhere. A local change set
is "Change #1", merged by "Merge change #1", and its station is the "Change set".

Driven on a real local project: a build's change set and its merge question, made by the
factory's own propose_pull_request; then every word the project page (Overview, Needs you), Work
and Approvals (the decision card, its stored answers) read from the API is scanned.
"""
from __future__ import annotations

import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from boss_engine.config import get_settings
from boss_engine.services import local_projects, local_repo
from boss_engine.services.software_factory import FileChange, SoftwareFactoryService

#: A GitHub word: PR (as a word), pull request, GitHub, or a retry offered for a merge.
GITHUB_WORDS = re.compile(r"\bPRs?\b|pull request|GitHub|Approve and retry", re.I)


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    return root


def _words(value, keys=("label", "name", "question", "detail", "summary", "words", "title",
                        "action_label", "effect", "text")) -> list[str]:
    """Every string a person reads, by the keys the pages draw."""
    out: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, str) and k in keys:
                out.append(v)
            elif isinstance(v, (dict, list)):
                out += _words(v, keys)
    elif isinstance(value, list):
        for v in value:
            out += _words(v, keys)
    return out


def test_a_local_project_says_change_never_pr(client, engine, repo_root):
    settings = get_settings()
    pid = f"lfw-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:id, :ws, 'Desk', :id)"), {"id": pid, "ws": ws})
        local_projects.create_for_project(db, settings, pid, name="Help Desk", actor="t")
    sf = SoftwareFactoryService(settings)
    run_id = f"crun_lfw{uuid.uuid4().hex[:8]}"
    branch = f"boss/code/{run_id}"
    with engine.begin() as db:
        sf.bind_project_github(db, pid)
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase, spec) "
                        "VALUES (:id, :p, 'Help desk', 'running', 'opening_pr', '{}'::jsonb)"),
                   {"id": run_id, "p": pid})
        reservation = sf.budget.reserve(db, pid, category="code_task", amount_usd_micros=1000,
                                        reason="local words", expires_in_minutes=30)
    with engine.begin() as db:
        out = sf.propose_pull_request(
            db, pid, None, reservation_id=reservation["reservation_id"],
            dedupe_key=f"code:task:{run_id}", title="Help desk", branch_name=branch,
            summary="A help desk.", files=[FileChange(path="server.js", content="x\n")],
            run_id=run_id, pr_base="main")
        db.execute(text("UPDATE code_task_runs SET pull_request_url = :u, pull_request_number = 1, "
                        "status = 'completed', phase = 'done' WHERE id = :r"),
                   {"u": out["pull_request_url"], "r": run_id})
    did = out["decision_id"]
    assert did, out

    with engine.connect() as db:
        stored = db.execute(text("SELECT question, options FROM decisions WHERE id = :i"),
                            {"i": did}).mappings().one()
    options = stored["options"] if isinstance(stored["options"], list) else json.loads(stored["options"])
    assert stored["question"].endswith("(change #1)")
    assert [o["label"] for o in options] == ["Approve and merge", "Keep blocked"]

    overview = client.get(f"/v1/projects/{pid}/overview").json()
    work = client.get(f"/v1/projects/{pid}/work").json()
    inbox = client.get(f"/v1/decisions/inbox?project_id={pid}").json()
    card = client.get(f"/v1/decisions/{did}").json()

    needs = overview.get("needs_you") or []
    merge = next((n for n in needs if n.get("kind") == "merge"), None)
    assert merge is not None, needs
    assert merge["label"] == "Merge change #1" and merge["action"]["label"] == "Review change"
    pr_stations = [s for w in work["items"] for s in w.get("stages") or [] if s.get("key") == "pr"]
    assert pr_stations and all(s["name"] == "Change set" for s in pr_stations)

    said = {"overview": _words(overview), "work": _words(work), "approvals": _words(inbox),
            "decision": _words(card) + [o["label"] for o in card.get("options") or []]}
    found = {where: [w for w in words if GITHUB_WORDS.search(w)] for where, words in said.items()}
    assert not any(found.values()), found

    # THE WORK DRAWER'S LINK is "Change #1" into BOSS's change-set view, never "PR #1 ↗".
    item = next(w for w in work["items"] if w.get("title") == "Help desk")
    story = client.get(f"/v1/projects/{pid}/work/{item['id']}/story")
    assert story.status_code == 200, story.text
    links = [v for v in _links(story.json())]
    assert links and all(not GITHUB_WORDS.search(str(link.get("label") or "")) for link in links)
    pr_link = next(link for link in links if str(link.get("label", "")).startswith("Change #"))
    assert pr_link.get("href") == f"/changes/{pid}/1" and not pr_link.get("external_url")

    # AN AGENT'S TITLE ("Open a draft PR bumping …", an ambient run) reads in the project's words.
    with engine.begin() as db:
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase, spec) "
                        "VALUES (:id, :p, 'Open a draft PR bumping esbuild to 0.25', 'running', "
                        "'coding', '{}'::jsonb)"),
                   {"id": f"crun_lfa{uuid.uuid4().hex[:8]}", "p": pid})
    work = client.get(f"/v1/projects/{pid}/work").json()
    words = [w for w in _words(work) if "esbuild" in w]
    assert words and not any(GITHUB_WORDS.search(w) for w in words), words
    assert any("draft change" in w for w in words), words

    # THE DEPLOY STEP WAITING ON THE MERGE says "change #1 … Merge it in BOSS".
    from boss_engine.services.workflow_capability_runner import _merge_note

    note = _merge_note({"number": 1, "state": "merge_pending"}, None, out["pull_request_url"])
    assert "change #1" in note.lower() and "in BOSS" in note and not GITHUB_WORDS.search(note)


def _links(value) -> list[dict]:
    out: list[dict] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "link" and isinstance(v, dict):
                out.append(v)
            elif isinstance(v, (dict, list)):
                out += _links(v)
    elif isinstance(value, list):
        for v in value:
            out += _links(v)
    return out


def test_a_change_set_keeps_its_title_and_its_notes_once(engine, repo_root):
    """A CHANGE SET KEEPS ITS OWN TITLE (HelpDesk's change #1 read "Follow-up: More tests — …"
    after its merge, from the follow-up build that recorded it), follow-up builds on its branch
    add to it, and its review notes are there once."""
    from boss_engine.services.local_repo_host import LocalGitAdapter
    from boss_engine.services.project_experience.deployments import _pr_by_commit
    from boss_engine.services.project_experience.titles import short_title
    from boss_engine.services.repo_links import change_title

    settings = get_settings()
    pid = f"lfw-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:id, :ws, 'Desk', :id)"), {"id": pid, "ws": ws})
        made = local_projects.create_for_project(db, settings, pid, name="Help Desk", actor="t")
    owner, repo = made["owner"], made["repo"]
    adapter = LocalGitAdapter(settings, owner=owner, repo=repo, project_id=pid)
    branch = "boss/code/crun_first"
    adapter.commit_files(branch, {"server.js": "v1\n"}, "build")
    first = adapter.create_pull_request(branch, "Help desk: tickets", "the build")
    # A follow-up build (More tests) on the same branch: more commits, the same change set.
    adapter.commit_files(branch, {"server.test.js": "test\n"}, "more tests")
    again = adapter.create_pull_request(branch, "Follow-up: More tests — Help desk: tickets",
                                        "the follow-up")
    assert again.number == first.number == 1
    assert adapter.change_set(1)["title"] == "Help desk: tickets"

    # The same review posted twice (a retried finalize) is one note.
    comments = [{"path": "server.js", "line": 1, "body": "check input"}]
    adapter.create_review(1, "### BOSS review — Round 1", comments=comments)
    adapter.create_review(1, "### BOSS review — Round 1", comments=comments)
    assert [n["body"] for n in adapter.notes(1)] == ["### BOSS review — Round 1"]

    head = local_repo.branch_sha(adapter.git_dir, branch)
    merged = adapter.merge_pull_request(1, "Merge change #1", merged_by="ceo")
    sha = str(getattr(merged, "sha", "") or local_repo.branch_sha(adapter.git_dir, "main"))
    # A later build on the same branch moves the branch, never the merged change: its commits,
    # and the deploy's `pull/1`, stay what was merged (stack_deploy.LocalSource).
    adapter.commit_files(branch, {"later.js": "later\n"}, "a later build")
    from boss_engine.services.stack_deploy import LocalSource

    assert LocalSource(adapter.git_dir, settings=settings).resolve_commit(
        owner, repo, "pull/1") == head
    assert [f["path"] for f in adapter.pull_request_files(1)] == ["server.js", "server.test.js"]
    with engine.begin() as db:
        # The follow-up run is the one that recorded the merge.
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, "
                        "pull_request_url, pull_request_number, finalize_ctx) VALUES "
                        "(:id, :p, 'Follow-up: More tests — Help desk: tickets', 'completed', "
                        "'done', '{}'::jsonb, :u, 1, CAST(:f AS JSONB))"),
                   {"id": f"crun_lff{uuid.uuid4().hex[:8]}", "p": pid, "u": first.url,
                    "f": json.dumps({"merge": {"sha": sha}})})
        assert change_title(db, first.url) == "Help desk: tickets"
        assert _pr_by_commit(db, pid)[sha]["title"] == short_title("Help desk: tickets")

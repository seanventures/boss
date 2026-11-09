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

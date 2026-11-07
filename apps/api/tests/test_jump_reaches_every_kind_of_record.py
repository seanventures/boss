"""⌘K reaches any run, PR, deploy, agent, doc or project in one keystroke (screen 11, 11-15).

Tester round 2: "PRD", "README" and "run 4" found nothing, and "deploys" listed only the newest
deploy of each environment. Here the HelpDesk story (3 merged PRs, staging deploys of several
commits, two PRDs, a README and an ADR in the dossier) is searched the way a person types.
Real Postgres; no model, no GitHub.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import seed


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("jump-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def project(world, client, engine):
    slug = f"helpdesk-jump-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        seed(db, slug)
    return slug


def _jump(world, client, q, pid):
    with world.acting_as(world.ctx(world.owner)):
        r = client.get("/v1/jump", params={"q": q, "project_id": pid})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _labels(items, kind):
    return [i["label"] for i in items if i["kind"] == kind]


def test_prd_readme_and_docs_find_the_documents_repo_and_knowledge_lists(world, client, project):
    prds = _labels(_jump(world, client, "PRD", project), "doc")
    assert prds and all(x.startswith("PRD · ") for x in prds) and len(prds) == 2
    assert _labels(_jump(world, client, "readme", project), "doc") == ["README"]
    docs = _labels(_jump(world, client, "docs", project), "doc")
    assert "README" in docs and any(x.startswith("ADR-001") for x in docs) and len(docs) == 4
    # A word of a document's name finds it.
    assert any(x.startswith("ADR-001") for x in _labels(_jump(world, client, "postgres", project), "doc"))
    # Each opens where Repo & knowledge opens it.
    readme = next(i for i in _jump(world, client, "readme", project) if i["kind"] == "doc")
    assert "drawer=doc%3Areadme" in readme["href"] or "drawer=doc:readme" in readme["href"]


def test_run_n_finds_that_run_by_its_number_in_the_project(world, client, engine, project):
    with engine.begin() as db:
        n = db.execute(text("SELECT count(*) FROM workflow_runs WHERE project_id = :p"),
                       {"p": project}).scalar()
        rid = db.execute(text(
            "SELECT r.id FROM workflow_runs r JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
            "WHERE r.project_id = :p AND d.namespace = 'product' AND COALESCE(r.mode, 'normal') <> 'test' "
            "ORDER BY r.created_at LIMIT 1"), {"p": project}).scalar()
        number = db.execute(text(
            "SELECT count(*) FROM workflow_runs WHERE project_id = :p AND created_at <= "
            "(SELECT created_at FROM workflow_runs WHERE id = :r)"), {"p": project, "r": rid}).scalar()
    assert n and rid
    for q in (f"run {number}", f"Run #{number}", f"run#{number}"):
        runs = [i for i in _jump(world, client, q, project) if i["kind"] == "run"]
        assert runs and runs[0]["label"].startswith(f"Run #{number} · "), (q, runs)
        assert f"run%3A{rid}" in runs[0]["href"] or f"run:{rid}" in runs[0]["href"]
        # A run number is not a PR number.
        assert not _labels(_jump(world, client, q, project), "pr")
    # Every run row says its number, as the Work drawer does ("Run #43").
    words = [i for i in _jump(world, client, "", project) if i["kind"] == "run"]
    assert words and all(i["label"].startswith("Run #") for i in words)


def test_pr_three_in_every_spelling_finds_pr_three_once(world, client, project):
    for q in ("pr 3", "PR #3", "#3", "pr3"):
        prs = _labels(_jump(world, client, q, project), "pr")
        assert len([x for x in prs if x.startswith("PR #3")]) == 1, (q, prs)
    every = _labels(_jump(world, client, "prs", project), "pr")
    assert len(every) == len(set(every)) >= 3


def test_deploys_lists_every_deploy_not_only_the_newest_per_environment(world, client, engine,
                                                                        project):
    with engine.begin() as db:
        commits = db.execute(text(
            "SELECT count(DISTINCT (s.target_id, COALESCE(s.commit_sha, s.id))) FROM stack_deployments s "
            "WHERE s.project_id = :p"), {"p": project}).scalar()
        envs = db.execute(text(
            "SELECT count(DISTINCT target_id) FROM stack_deployments WHERE project_id = :p"),
            {"p": project}).scalar()
    assert commits > envs >= 1, "the seed has more than one deploy of an environment"
    deploys = _labels(_jump(world, client, "deploys", project), "deploy")
    assert len(deploys) == min(commits, 12), deploys
    assert len(deploys) == len(set(deploys)), "no duplicate rows"
    # The palette's first look (no query) keeps one row per environment: what is live where.
    first = _labels(_jump(world, client, "", project), "deploy")
    assert len(first) == envs

"""An import that is not a template stores nothing; every import refusal is said plainly.

The defects: a repository with no readable manifest was STORED as `invalid-<sha>` 0.0.0 (a row in
the review list that is nobody's template); git's raw error text came through; spec references
("(D3)") leaked into messages.
"""
from __future__ import annotations

import re

from sqlalchemy import text

from apps.api.tests.test_templates_api import as_, client, master_key, people, world  # noqa: F401
from engine.tests.templates._fixtures import git_repo

_SPEC_REF = re.compile(r"\((?:D\d+|§\s?[\d.]+|S\d+\.\d+|F\d+[^)]*)\)")


def _count(engine) -> int:
    with engine.begin() as db:
        return int(db.execute(text("SELECT count(*) FROM app_templates WHERE source = 'git'")).scalar())


def test_a_repository_without_a_valid_manifest_is_refused_and_nothing_is_stored(world, people, client, tmp_path, engine):  # noqa: F811
    repo, _ = git_repo(tmp_path / "src", drop=("boss-template.yaml",))
    before = _count(engine)
    with as_(world, people["operator"]):
        r = client.post("/v1/templates/import", json={"git_url": f"file://{repo}"})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"] == "invalid_template"
    assert "boss-template.yaml" in body["message"] and not _SPEC_REF.search(body["message"])
    assert _count(engine) == before
    with engine.begin() as db:
        assert db.execute(text("SELECT count(*) FROM app_templates WHERE template_key LIKE 'invalid-%'")).scalar() == 0


def test_a_git_failure_is_said_in_words_not_gits_text(world, people, client, tmp_path):  # noqa: F811
    with as_(world, people["operator"]):
        r = client.post("/v1/templates/import", json={"git_url": f"file://{tmp_path}/no-such-repo"})
    assert r.status_code == 422
    msg = r.json()["message"]
    assert msg.startswith("no repository at file://")
    assert "fatal" not in msg and "git fetch failed" not in msg


def test_naming_a_connection_is_answered_plainly(world, people, client):  # noqa: F811
    with as_(world, people["operator"]):
        r = client.post("/v1/templates/import", json={"git_url": "https://github.com/a/b", "connection_id": "ghc_1"})
    assert r.status_code == 422 and r.json()["error"] == "connection_not_needed"

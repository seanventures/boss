"""Rulesets in the repo, edited as a change (SPEC docs/design/validation-review §4).

Real git in a temporary bare repository, the real database and routes: the console lists the
base branch's rulesets and proposes an edit, which opens "Ruleset <id> v<n+1>" as a local change
with its merge decision; lowering a P0 raises it to the two-person tier.
"""
from __future__ import annotations

import uuid

import yaml
from sqlalchemy import text

from apps.api.tests.test_change_review import client, world  # noqa: F401 — pytest fixtures
from apps.api.tests.test_local_change_review import _owner, _viewer, repo_root  # noqa: F401
from boss_engine.config import get_settings
from boss_engine.services import local_projects, local_repo, rulesets

RULESET = """\
ruleset: northgate
version: 3
validator: gwlint
rules:
  no-inlined-secrets: {severity: P0, autofix: never, guide: "Delete the key if the route already authenticates another way."}
  pii-fields-declared: {severity: P2, autofix: agent}
custom:
  - id: lumen-no-refund-without-schema
    severity: P1
    check: agent
    guide: "A POST route under /v1/payments/refunds must declare request_schema."
"""


def _project(engine, world):  # noqa: F811
    pid = f"rs-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                        {"p": world.project_a}).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:id, :ws, :n, :id)"), {"id": pid, "ws": ws, "n": f"Gate {pid[-4:]}"})
        bound = local_projects.create_for_project(db, get_settings(), pid, name="gate", actor="t")
    bare = local_repo.bare_path(bound["owner"], bound["repo"])
    local_repo.commit_files(bare, "main", {".boss/rulesets/northgate.yaml": RULESET,
                                           "README.md": "gate\n"}, "ruleset v3")
    return pid, bare


def _rules(**changes):
    base = [{"id": "no-inlined-secrets", "enabled": True, "severity": "P0", "autofix": "never",
             "guide": "Delete the key if the route already authenticates another way."},
            {"id": "pii-fields-declared", "enabled": True, "severity": "P2", "autofix": "agent",
             "guide": ""},
            {"id": "lumen-no-refund-without-schema", "enabled": True, "severity": "P1",
             "autofix": "agent", "custom": True, "check": "agent",
             "guide": "A POST route under /v1/payments/refunds must declare request_schema."}]
    for r in base:
        r.update(changes.get(r["id"].replace("-", "_"), {}))
    return base


def test_the_rulesets_are_read_from_the_base_branch(client, world, engine, repo_root):  # noqa: F811
    pid, _bare = _project(engine, world)
    with _viewer(world):
        got = client.get(f"/v1/projects/{pid}/rulesets")
    assert got.status_code == 200, got.text
    (rs,) = got.json()["rulesets"]
    assert (rs["id"], rs["version"], rs["sha"]) == ("northgate", 3, rulesets.parse(RULESET)["sha"])
    assert rs["rules"]["no-inlined-secrets"]["severity"] == "P0"
    assert rs["custom"][0]["check"] == "agent"


def test_a_proposal_opens_ruleset_v4_as_a_change(client, world, engine, repo_root):  # noqa: F811
    pid, bare = _project(engine, world)
    sha = rulesets.parse(RULESET)["sha"]
    with _owner(world):
        got = client.post(f"/v1/projects/{pid}/rulesets/northgate/propose", json={
            "rules": _rules(pii_fields_declared={"severity": "P1", "guide": "Declare it."}),
            "base_version": 3, "base_sha": sha})
    assert got.status_code == 200, got.text
    body = got.json()
    assert isinstance(body["change"], int) and body["second_approver"] is False
    new = yaml.safe_load(local_repo.read_file(bare, "boss/ruleset/northgate-v4",
                                              ".boss/rulesets/northgate.yaml"))
    assert new["version"] == 4 and new["rules"]["pii-fields-declared"]["severity"] == "P1"
    assert new["rules"]["pii-fields-declared"]["guide"] == "Declare it."
    assert new["custom"][0]["id"] == "lumen-no-refund-without-schema"
    # main is untouched until the change merges.
    assert "version: 3" in local_repo.read_file(bare, "main", ".boss/rulesets/northgate.yaml")
    with engine.begin() as db:
        d = db.execute(text("SELECT risk_tier, recommendation FROM decisions WHERE id = :i"),
                       {"i": body["decision_id"]}).mappings().first()
    assert d["risk_tier"] == 2 and d["recommendation"]["action"] == "review_pull_request"
    assert d["recommendation"]["ruleset_change"]["to_version"] == 4


def test_lowering_a_p0_needs_a_second_approver(client, world, engine, repo_root):  # noqa: F811
    pid, _bare = _project(engine, world)
    with _owner(world):
        got = client.post(f"/v1/projects/{pid}/rulesets/northgate/propose", json={
            "rules": _rules(no_inlined_secrets={"severity": "P1"}),
            "base_version": 3, "base_sha": rulesets.parse(RULESET)["sha"]})
    assert got.status_code == 200, got.text
    assert got.json()["second_approver"] is True
    assert got.json()["p0_downgrades"] == ["no-inlined-secrets"]
    with engine.begin() as db:
        tier = db.execute(text("SELECT risk_tier FROM decisions WHERE id = :i"),
                          {"i": got.json()["decision_id"]}).scalar()
    assert tier == 3


def test_a_proposal_against_a_moved_base_is_409(client, world, engine, repo_root):  # noqa: F811
    pid, _bare = _project(engine, world)
    with _owner(world):
        got = client.post(f"/v1/projects/{pid}/rulesets/northgate/propose", json={
            "rules": _rules(), "base_version": 2, "base_sha": "0000000"})
    assert got.status_code == 409 and got.json()["error"] == "stale_base"
    assert got.json()["ruleset"]["version"] == 3


def test_a_ruleset_id_that_is_a_path_is_refused_before_anything_is_read(monkeypatch):
    """The id becomes `.boss/rulesets/<id>.yaml`: a `/` or `..` is a 404 before the repository (or
    the database) is touched."""
    import pytest

    from boss_engine.services import ruleset_changes

    def never(*_a, **_k):
        raise AssertionError("read before the id was checked")

    monkeypatch.setattr(ruleset_changes, "_repo", never)
    for bad in ("../secrets", "a/b", "..", ""):
        with pytest.raises(ruleset_changes.RulesetChangeError) as refused:
            ruleset_changes.propose(None, None, "p", bad, rules=[], base_version=None,
                                    base_sha=None, actor="ceo")
        assert refused.value.status == 404 and refused.value.code == "no_ruleset"

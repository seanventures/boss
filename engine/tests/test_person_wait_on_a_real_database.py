"""A build refused for want of a grant pauses on a person, on a REAL database (L2b).

The laptop, 2026-11-07: four always-on CVE builds (crun_de5396b5, crun_a4364670, crun_eba9d348,
crun_13c3ce2b) were refused `no_grant` -- an always-on squad starts work with no person behind
it. The refusal handler called person_wait, whose first statement was a bare SQL string:
`ObjectNotExecutableError` -> 500 -> the worker could not report it (connection reset) -> the
lease expired -> the build was re-leased, every five minutes, attempt 30 of 3, the console
saying "Queued". person_wait's tests mocked the connection, so none of it was ever executed.

Here every step runs on Postgres: pause (one card for every build waiting on the same builder),
resume by itself once the person's grant covers it (the job carries the grant and the build's
authority reads it), resume at once when a grant already covers it, and the refusal path through
`_run_code_task_once` never raises.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import code_task_service, person_wait

from ._ambient_support import eng, make_project, project, purge  # noqa: F401

ORG = "org_amb"
BUILDER = "engineering_captain"


@pytest.fixture()
def world(eng, project):  # noqa: F811
    sfx = uuid4().hex[:8]
    w = {"person": f"usr_pw_{sfx}", "agent": f"agt_pw_{sfx}", "project": project}
    with eng.begin() as c:
        c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                       "VALUES (:i, :o, 'user', 'active', 'Param')"), {"i": w["person"], "o": ORG})
        c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                       "VALUES (:i, :o, 'agent', 'active', 'Helm')"), {"i": w["agent"], "o": ORG})
        c.execute(text("INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, "
                       "role_id, scope_type, scope_id, granted_by) VALUES (:id, :o, 'principal', :s, "
                       "'arl_owner', 'project', :p, 'test')"),
                  {"id": f"bnd_{sfx}", "o": ORG, "s": w["person"], "p": project})
        c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, agent_id) "
                       "VALUES (:id, :p, :r, 'Engineering Captain', :a)"),
                  {"id": f"role_{sfx}", "p": project, "r": BUILDER, "a": w["agent"]})
    yield w
    with eng.begin() as c:
        c.execute(text("DELETE FROM identity_delegation_grant_permissions WHERE grant_id IN "
                       "(SELECT id FROM identity_delegation_grants WHERE user_principal_id = :u)"),
                  {"u": w["person"]})
        c.execute(text("DELETE FROM identity_delegation_grants WHERE user_principal_id = :u"),
                  {"u": w["person"]})
        c.execute(text("DELETE FROM identity_role_bindings WHERE subject_id = :u"), {"u": w["person"]})
        c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": project})


def _build(c, w, title="Bump esbuild") -> str:
    """A squad build as the laptop had it: started by BOSS (sys_legacy_system), no grant."""
    rid = f"crun_pw_{uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, finalize_ctx) "
        "VALUES (:id, :p, 'running', 'queued', :t, CAST(:s AS jsonb), CAST(:f AS jsonb))"),
        {"id": rid, "p": w["project"], "t": title,
         "s": json.dumps({"owner": "acme", "repo": "helpdesk"}),
         "f": json.dumps({"agent": {"role_key": BUILDER}})})
    c.execute(text(
        "INSERT INTO jobs (id, project_id, kind, status, payload, on_behalf_of, chain, "
        "on_behalf_system, attempt, max_attempts) VALUES (:j, :p, 'code.task', 'running', "
        "CAST(:pl AS jsonb), 'sys_legacy_system', '[]'::jsonb, false, 1, 3)"),
        {"j": f"job_pw_{uuid4().hex[:12]}", "p": w["project"], "pl": json.dumps({"run_id": rid})})
    return rid


def _grant(c, w) -> str:
    from boss_engine.identity import catalogue
    from boss_engine.identity import consent

    gid = f"dlg_pw_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO identity_delegation_grants (id, org_id, user_principal_id, agent_ids, "
        "project_ids, resources, status) VALUES (:g, :o, :u, ARRAY[:a], ARRAY[:p], "
        "CAST(:r AS jsonb), 'active')"),
        {"g": gid, "o": ORG, "u": w["person"], "a": w["agent"], "p": w["project"],
         "r": json.dumps({"github": {"repos": ["acme/helpdesk"]}})})
    for k in consent.CODE_TASK_PERMISSIONS:
        if catalogue.is_known(k) and catalogue.meta(k).delegable:
            c.execute(text("INSERT INTO identity_delegation_grant_permissions (grant_id, "
                           "permission_key) VALUES (:g, :k)"), {"g": gid, "k": k})
    return gid


def _budget():
    released = []
    return SimpleNamespace(release=lambda **kw: released.append(kw), released=released)


def _pause(c, rid, w):
    return person_wait.enter_waiting_on_person(
        c, _budget(), run_id=rid, project_id=w["project"], task_id=None, reason="no_grant",
        question="No one's authority covers this build.")


def test_a_refused_build_pauses_and_four_builds_share_one_card(eng, world):
    with eng.begin() as c:
        runs = [_build(c, world, f"Bump dep {i}") for i in range(4)]
        results = [_pause(c, rid, world) for rid in runs]
    assert {r["status"] for r in results} == {person_wait.STATUS_WAITING_ON_PERSON}
    assert len({r["decision_id"] for r in results}) == 1, "one card for one blocker"
    with eng.begin() as c:
        rows = c.execute(text("SELECT status, phase, finalize_ctx->'person_wait'->>'builder' AS b "
                              "FROM code_task_runs WHERE id = ANY(:ids)"), {"ids": runs}).mappings().all()
        card = c.execute(text("SELECT question, recommendation FROM decisions WHERE id = :d"),
                         {"d": results[0]["decision_id"]}).mappings().one()
        threads = c.execute(text("SELECT count(*) FROM agent_threads WHERE project_id = :p "
                                 "AND agent_role = 'person_wait'"), {"p": world["project"]}).scalar()
    assert all(r["status"] == "waiting_on_person" and r["phase"] == "awaiting_grant" for r in rows)
    assert {r["b"] for r in rows} == {"Helm"}
    assert card["recommendation"]["action"] == person_wait.ACTION_GRANT_BUILDER
    assert sorted(card["recommendation"]["run_ids"]) == sorted(runs)
    assert "Helm" in card["question"] and "standing grant" in card["question"]
    assert threads == 1


def test_the_build_resumes_by_itself_once_the_grant_exists_and_runs_under_it(eng, world):
    from boss_engine.identity.runs import original_build_authority

    with eng.begin() as c:
        rid = _build(c, world)
        paused = _pause(c, rid, world)
    with eng.begin() as c:
        assert person_wait.poll_due(c, get_settings()) == 0      # nothing covers it yet
        gid = _grant(c, world)
    with eng.begin() as c:
        assert person_wait.poll_due(c, get_settings()) >= 1
    with eng.begin() as c:
        run = c.execute(text("SELECT status, phase FROM code_task_runs WHERE id = :r"),
                        {"r": rid}).mappings().one()
        jobs = c.execute(text("SELECT grant_id, on_behalf_of, status FROM jobs WHERE "
                              "payload->>'run_id' = :r ORDER BY created_at"), {"r": rid}).mappings().all()
        acting = original_build_authority(c, rid)
        card = c.execute(text("SELECT status FROM decisions WHERE id = :d"),
                         {"d": paused["decision_id"]}).scalar()
    assert (run["status"], run["phase"]) == ("running", "queued")
    assert jobs[-1]["grant_id"] == gid and jobs[-1]["on_behalf_of"] == world["person"]
    # The build's authority is the job that CARRIES the grant, not the first, grantless one.
    assert acting.grant_id == gid and acting.principal_id == world["person"]
    assert card != "pending"
    with eng.begin() as c:   # a second sweep resumes nothing twice
        assert person_wait.verify_and_resume(c, get_settings(), None, run_id=rid) == {"resumed": False}


def test_a_grant_that_already_covers_it_resumes_at_once_with_no_card(eng, world):
    with eng.begin() as c:
        gid = _grant(c, world)
        rid = _build(c, world)
        out = _pause(c, rid, world)
        cards = c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p AND "
                               "recommendation->>'action' = :a"),
                          {"p": world["project"], "a": person_wait.ACTION_GRANT_BUILDER}).scalar()
    assert out["resumed"] is True and out["grant_id"] == gid
    assert cards == 0


def test_the_refusal_path_never_raises_and_pauses_the_build(eng, world, monkeypatch):
    """The laptop's exact crash: AuthorityRefused inside a real attempt, on a real database."""
    with eng.begin() as c:
        rid = _build(c, world)

    def refused(*a, **k):
        raise code_task_service.AuthorityRefused("no_grant")

    monkeypatch.setattr(code_task_service, "_run_code_task", refused)
    out = code_task_service._run_code_task_once(eng, get_settings(), None, rid)
    assert out["status"] == "waiting_on_person" and out["done"] is True
    with eng.begin() as c:
        status = c.execute(text("SELECT status FROM code_task_runs WHERE id = :r"), {"r": rid}).scalar()
    assert status == "waiting_on_person"


def test_secret_shaped_replies_are_never_stored(eng, world):
    import secrets

    fake = "ghp_" + secrets.token_hex(18)
    safe, blocked = person_wait.scan_reply_for_secrets(f"here: {fake}")
    assert blocked and fake not in safe

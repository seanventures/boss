"""Always-on builds start on the project's Captain authority, with no person's grant (2026-11-07).

The laptop: four helpdesk CVE builds an always-on squad decided (crun_de5396…, crun_a43646…,
crun_eba9d3…, crun_13c3ce…) were refused `no_grant` and waited on a card asking a person for a
standing grant. The operator's decision: "by default let the Captain take action; expose the flag
in case a user wants to review those security builds".

On a real database:

* every project has ONE Captain authority, sponsored by its Owner, build/PR permissions only —
  never merge, production deploy, secrets or admin — created by default and backfilled;
* a squad build within its class's level runs on it OBO the sponsor, and records its lineage: the
  agent, "on the project's Captain authority", the sponsor and the ambient decision;
* "Review always-on builds before they start" on (project, class or install) -> the build waits on
  one card; approved -> it starts on the authority; rejected -> it is dropped;
* the live case replayed: builds the earlier release paused on a grant card resume by themselves
  on the next sweep, the card withdraws with a plain note, and the build's own start accepts the
  stamped authority; a class demoted since refuses it.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import captain_authority, catalogue, grants, passports
from boss_engine.identity import runs as identity_runs
from boss_engine.services import person_wait
from boss_engine.services.ambient import policy

from ._ambient_support import eng, make_project, project, purge  # noqa: F401
from ._squad_support import Launchers, squad_item
from .test_a_squad_hands_work_to_a_workflow_and_gets_the_result_back import _decide_a_draft_pr

ORG = "org_amb"
CAPTAIN = "engineering_captain"
REPO = "acme/helpdesk"


def _staff(c, pid: str, sfx: str) -> dict:
    """An Owner person on the project, the Captain with a Passport, and the project's repo."""
    w = {"person": f"usr_ca_{sfx}", "agent": f"agt_ca{sfx}", "project": pid}
    c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                   "VALUES (:i, :o, 'user', 'active', 'Param') ON CONFLICT DO NOTHING"),
              {"i": w["person"], "o": ORG})
    c.execute(text("INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, "
                   "scope_type, scope_id, granted_by) VALUES (:id, :o, 'principal', :s, 'arl_owner', "
                   "'project', :p, 'test')"),
              {"id": f"bnd_ca_{uuid4().hex[:10]}", "o": ORG, "s": w["person"], "p": pid})
    c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, agent_id) VALUES "
                   "(:id, :p, :r, 'Engineering Captain', :a) ON CONFLICT (project_id, role_key) DO UPDATE "
                   "SET agent_id = EXCLUDED.agent_id"),
              {"id": f"role_{uuid4().hex[:10]}", "p": pid, "r": CAPTAIN, "a": w["agent"]})
    passports.ensure_passport(org_id=ORG, agent_id=w["agent"], role_key=CAPTAIN,
                              display_name="Helm", sponsor_principal_id=w["person"], db=c)
    conn = f"ghc_ca_{uuid4().hex[:10]}"
    c.execute(text("INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
                   "credential_ref, created_by) VALUES (:id, :p, 'gh', 'github_app', 'vault:none', 'test')"), {"id": conn, "p": pid})
    c.execute(text("INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
                   "full_name, created_by) VALUES (:id, :p, :c, 'acme', 'helpdesk', :f, 'test')"),
              {"id": f"ghb_ca_{uuid4().hex[:10]}", "p": pid, "c": conn, "f": REPO})
    return w


def _cleanup(eng, w) -> None:  # noqa: F811
    with eng.begin() as c:
        c.execute(text("DELETE FROM identity_delegation_grant_permissions WHERE grant_id IN "
                       "(SELECT id FROM identity_delegation_grants WHERE user_principal_id = :u)"),
                  {"u": w["person"]})
        c.execute(text("DELETE FROM identity_delegation_grants WHERE user_principal_id = :u"),
                  {"u": w["person"]})
        c.execute(text("DELETE FROM identity_role_bindings WHERE subject_id = :u"), {"u": w["person"]})
        c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": w["project"]})
        c.execute(text("DELETE FROM github_repository_bindings WHERE project_id = :p"), {"p": w["project"]})
        c.execute(text("DELETE FROM github_connections WHERE project_id = :p"), {"p": w["project"]})


@pytest.fixture()
def world(eng, project):  # noqa: F811
    with eng.begin() as c:
        w = _staff(c, project, uuid4().hex[:8])
    yield w
    _cleanup(eng, w)


def _squad_build(c, w, *, title: str = "Bump undici to 6.21.1", same_as: dict | None = None) -> dict:
    """What an always-on squad leaves behind: its ledger decision (within the class's level), the
    workflow run it launched (input names the item and the decision), and that run's build as the
    Captain — started by BOSS (sys_legacy_system), no grant."""
    if same_as is not None:                       # another build of the same workflow run
        item, settled, wfr = {"id": same_as["item"]}, {"act_id": same_as["act"]}, same_as["wfr"]
    else:
        launchers = Launchers(run_status="running")
        item = squad_item(c, w["project"], fingerprint=f"fp:cve:{uuid4().hex[:6]}")["item"]
        settled = _decide_a_draft_pr(c, item["id"], launchers)
        wfr = settled["executed"]["ref"]
    rid = f"crun_ca_{uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, finalize_ctx) "
        "VALUES (:id, :p, 'running', 'queued', :t, CAST(:s AS jsonb), CAST(:f AS jsonb))"),
        {"id": rid, "p": w["project"], "t": title, "s": json.dumps({"owner": "acme", "repo": "helpdesk"}),
         "f": json.dumps({"workflow_run_id": wfr, "agent": {"agent_id": w["agent"], "role_key": CAPTAIN},
                          "origin": {"actor": "security_reviewer"}})})
    c.execute(text(
        "INSERT INTO jobs (id, project_id, kind, status, payload, on_behalf_of, chain, on_behalf_system, "
        "attempt, max_attempts) VALUES (:j, :p, 'code.task', 'running', CAST(:pl AS jsonb), "
        "'sys_legacy_system', '[]'::jsonb, false, 1, 3)"),
        {"j": f"job_ca_{uuid4().hex[:12]}", "p": w["project"], "pl": json.dumps({"run_id": rid})})
    return {"run": rid, "item": item["id"], "act": settled["act_id"], "wfr": wfr}


def _budget():
    return SimpleNamespace(release=lambda **kw: None)


def _pause(c, w, rid, reason="no_grant"):
    return person_wait.enter_waiting_on_person(
        c, _budget(), run_id=rid, project_id=w["project"], task_id=None, reason=reason,
        question="No one's authority covers this build.")


# ---- the authority itself ----------------------------------------------------------------------------

def test_every_project_gets_one_captain_authority_sponsored_by_its_owner(eng, world):
    with eng.begin() as c:
        g = captain_authority.ensure_for_project(c, world["project"])
        again = captain_authority.ensure_for_project(c, world["project"])
        created = c.execute(text("SELECT count(*) FROM identity_security_events WHERE grant_id = :g "
                                 "AND event_type = 'delegation.created'"), {"g": g.id}).scalar()
    assert g is not None and again.id == g.id, "one per project"
    assert grants.is_captain_authority(g) and g.authority_kind == "captain"
    assert g.user_principal_id == world["person"], "sponsored by the project's Owner"
    assert world["agent"] in g.agent_ids and list(g.project_ids) == [world["project"]]
    assert g.permissions == captain_authority.PERMISSIONS
    assert g.expires_at is None and g.task_run_id is None and not g.allow_subdelegation
    assert grants.github_resources(g)["repos"] == [REPO]
    assert created == 1, "its creation is in the lineage"


def test_existing_projects_are_backfilled_and_a_revoked_one_stays_off(eng, world):
    other = make_project(eng)
    try:
        with eng.begin() as c:
            w2 = _staff(c, other, uuid4().hex[:8])
            assert captain_authority.get(c, other) is None
            made = captain_authority.backfill(c)
            g2 = captain_authority.get(c, other)
        assert made >= 1 and g2 is not None and g2.user_principal_id == w2["person"]
        with eng.begin() as c:   # a person revoked it: never re-created, the project stays off
            c.execute(text("UPDATE identity_delegation_grants SET status = 'revoked' WHERE id = :g"),
                      {"g": g2.id})
            captain_authority.backfill(c)
            after = captain_authority.ensure_for_project(c, other)
        assert after.id == g2.id and after.status.value == "revoked"
    finally:
        _cleanup(eng, w2)
        purge(eng, other)


def test_the_authority_never_permits_merge_production_deploy_secrets_or_admin(eng, world):
    assert not captain_authority.PERMISSIONS & captain_authority.FORBIDDEN
    assert not captain_authority.PERMISSIONS & catalogue.NEVER_DELEGABLE
    for key in captain_authority.PERMISSIONS:
        assert not any(w in key for w in ("merge", "deploy", "secret", "admin", "approve"))
    with eng.begin() as c:
        g = captain_authority.ensure_for_project(c, world["project"])
        gh = grants.github_resources(g)
        for forbidden in ("decision:approve", "deployment:production", "secret:manage",
                          "access_role:manage", "code_task:override"):
            assert not grants.covers(g, agent_ids=[world["agent"]], permissions=frozenset({forbidden}),
                                     project_id=world["project"], repos=[REPO], run_id=None)
        # GitHub: push a boss/* branch and open a PR; never administration or workflows
        assert gh.get("contents") == "write" and gh.get("pull_requests") == "write"
        assert "administration" not in gh and "workflows" not in gh and not gh.get("as_user")
        # A person's own start never borrows it: consent still asks them
        assert grants.find_covering(world["person"], agent_ids=[world["agent"]],
                                    permissions=captain_authority.PERMISSIONS, project_id=world["project"],
                                    repos=[REPO], run_id=None, db=c) is None
        # It never reaches a repository the project does not have bound
        with pytest.raises(grants.GrantRefused):
            captain_authority.require_repos_bound(c, g, ["evil/elsewhere"])
        captain_authority.require_repos_bound(c, g, [REPO])


# ---- the default: a squad build runs on it -------------------------------------------------------------

def test_a_squad_build_starts_on_the_captain_authority_and_records_its_lineage(eng, world):
    with eng.begin() as c:
        b = _squad_build(c, world)
    acting, verdict = identity_runs._captain_authority(eng, None, task_run_id=b["run"],
                                                       project_id=world["project"])
    assert verdict.covered and not verdict.review_required, verdict.reason
    assert verdict.basis == "autonomy" and verdict.act_id == b["act"] and verdict.work_item_id == b["item"]
    assert acting.principal_id == world["person"] and acting.grant_id == verdict.grant_id
    assert acting.run_id == f"run_ct:{b['run']}"
    with eng.begin() as c:
        assert captain_authority.record_use(c, verdict, run_id=b["run"]) is True
        assert captain_authority.record_use(c, verdict, run_id=b["run"]) is False, "once per build"
        lineage = c.execute(text("SELECT finalize_ctx->'authority' FROM code_task_runs WHERE id = :r"),
                            {"r": b["run"]}).scalar()
        ev = c.execute(text("SELECT actor_principal_id, actor_kind, on_behalf_of, grant_id, details "
                            "FROM identity_security_events WHERE event_type = 'agent.captain_authority' "
                            "AND target_id = :t"), {"t": f"run_ct:{b['run']}"}).mappings().all()
        product = c.execute(text("SELECT count(*) FROM events WHERE event_type = 'code_task.captain_authority' "
                                 "AND entity_id = :r"), {"r": b["run"]}).scalar()
        board_note = c.execute(text("SELECT body->>'summary' FROM work_item_messages WHERE work_item_id = :w "
                                    "AND kind = 'finding' AND sender = 'boss' ORDER BY created_at DESC LIMIT 1"),
                               {"w": b["item"]}).scalar()
    assert lineage["basis"] == "captain_authority" and lineage["label"] == "on the project's Captain authority"
    assert (lineage["agent"], lineage["sponsor"], lineage["decision"]) == (world["agent"], world["person"], b["act"])
    assert lineage["decided_by"].startswith("squad:") and lineage["decision_basis"] == "autonomy"
    assert len(ev) == 1
    assert (ev[0]["actor_principal_id"], ev[0]["actor_kind"], ev[0]["on_behalf_of"]) == (
        world["agent"], "agent", world["person"])
    assert ev[0]["details"]["decision"] == b["act"] and ev[0]["details"]["basis"] == "captain_authority"
    assert product == 1 and "Captain authority" in board_note


def test_a_refused_squad_build_is_requeued_on_the_authority_with_no_card(eng, world):
    with eng.begin() as c:
        b = _squad_build(c, world)
        out = _pause(c, world, b["run"])
        cards = c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p AND "
                               "recommendation->>'action' IN ('grant_builder_authority', 'review_always_on_build')"),
                          {"p": world["project"]}).scalar()
        acting = identity_runs.original_build_authority(c, b["run"])
        is_captain = captain_authority.is_captain_grant(c, acting.grant_id)
    assert out["resumed"] is True and out["basis"] == "captain_authority"
    assert cards == 0
    assert acting.principal_id == world["person"] and is_captain


def test_builds_not_from_an_always_on_squad_are_untouched(eng, world):
    with eng.begin() as c:
        rid = f"crun_ca_{uuid4().hex[:12]}"
        c.execute(text("INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, finalize_ctx) "
                       "VALUES (:id, :p, 'running', 'queued', 'A person''s build', CAST(:s AS jsonb), "
                       "'{\"agent\": {\"role_key\": \"engineering_captain\"}}'::jsonb)"),
                  {"id": rid, "p": world["project"], "s": json.dumps({"owner": "acme", "repo": "helpdesk"})})
        v = captain_authority.evaluate(c, run_id=rid, project_id=world["project"])
    assert not v.ambient and not v.covered and v.reason == "not_always_on"


# ---- the flag: review always-on builds before they start --------------------------------------------------

def test_review_on_makes_squad_builds_wait_on_one_card_until_a_person_approves(eng, world):
    with eng.begin() as c:
        policy.set_review_builds(c, world["project"], actor="ceo", review=True)
        b1, b2 = _squad_build(c, world), _squad_build(c, world, title="Bump vite to 5.4.12")
    acting, verdict = identity_runs._captain_authority(eng, None, task_run_id=b1["run"],
                                                       project_id=world["project"])
    assert acting is None and verdict.covered and verdict.review_pending and verdict.review_source == "project"
    with eng.begin() as c:
        p1, p2 = _pause(c, world, b1["run"], "captain_review_required"), _pause(c, world, b2["run"])
        card = c.execute(text("SELECT id, question, recommendation FROM decisions WHERE id = :d"),
                         {"d": p1["decision_id"]}).mappings().one()
    assert p1["status"] == p2["status"] == person_wait.STATUS_WAITING_ON_PERSON
    assert p1["decision_id"] == p2["decision_id"], "one card"
    assert card["recommendation"]["action"] == person_wait.ACTION_REVIEW_BUILD
    assert sorted(card["recommendation"]["run_ids"]) == sorted([b1["run"], b2["run"]])
    assert "Captain authority" in card["question"] and "merging still needs you" in card["question"]
    with eng.begin() as c:
        assert person_wait.poll_due(c, get_settings()) == 0, "nobody reviewed it yet"
        c.execute(text("UPDATE decisions SET status = 'approved', approved_by = 'ceo', approved_at = now() "
                       "WHERE id = :d"), {"d": card["id"]})
    with eng.begin() as c:
        assert person_wait.poll_due(c, get_settings()) == 2
        jobs = c.execute(text("SELECT payload->>'run_id' AS r, grant_id, on_behalf_of FROM jobs WHERE "
                              "project_id = :p AND grant_id IS NOT NULL"), {"p": world["project"]}).mappings().all()
        lineage = c.execute(text("SELECT finalize_ctx->'authority' FROM code_task_runs WHERE id = :r"),
                            {"r": b1["run"]}).scalar()
    assert {j["r"] for j in jobs} == {b1["run"], b2["run"]}
    assert all(j["on_behalf_of"] == world["person"] and j["grant_id"] == verdict.grant_id for j in jobs)
    assert lineage["reviewed_by"] == "ceo"


def test_a_rejected_review_drops_the_build(eng, world):
    with eng.begin() as c:
        policy.set_review_builds(c, world["project"], actor="ceo", review=True)
        b = _squad_build(c, world)
        paused = _pause(c, world, b["run"], "captain_review_required")
        c.execute(text("UPDATE decisions SET status = 'rejected', approved_by = 'ceo' WHERE id = :d"),
                  {"d": paused["decision_id"]})
    with eng.begin() as c:
        assert person_wait.poll_due(c, get_settings()) == 0
        row = c.execute(text("SELECT status, error FROM code_task_runs WHERE id = :r"),
                        {"r": b["run"]}).mappings().one()
    assert row["status"] == "cancelled" and "declined" in row["error"]


def test_the_flag_follows_class_then_project_then_install(eng, world):
    pid = world["project"]
    with eng.begin() as c:
        assert policy.review_builds_for(c, pid, "security_patch") == (False, "install"), "off by default"
        before = policy.review_builds_install(c)
        try:
            policy.set_review_builds_install(c, review=True, actor="ceo")
            assert policy.review_builds_for(c, pid, "security_patch") == (True, "install")
            policy.set_review_builds(c, pid, actor="ceo", review=False)
            assert policy.review_builds_for(c, pid, "security_patch") == (False, "project")
            policy.set_review_builds(c, pid, actor="ceo", review=True, task_class="flaky_test")
            assert policy.review_builds_for(c, pid, "flaky_test") == (True, "class")
            assert policy.review_builds_for(c, pid, "security_patch") == (False, "project")
            policy.set_review_builds(c, pid, actor="ceo", review=None)
            state = policy.review_builds_state(c, pid)
            assert state["project"] is None and state["classes"] == {"flaky_test": True} and state["effective"]
            assert policy.matrix(c, pid) and next(r for r in policy.matrix(c, pid)
                                                  if r["task_class"] == "flaky_test")["review_builds"] is True
        finally:
            policy.set_review_builds_install(c, review=before, actor="ceo")


# ---- the live case, replayed ----------------------------------------------------------------------------

def test_the_live_case_paused_builds_resume_on_the_next_sweep_and_the_card_withdraws(eng, world, monkeypatch):
    """The laptop: the earlier release paused the four CVE builds on ONE grant card (it had no
    Captain authority). With this release, the next Shepherd sweep resumes each on the authority,
    the card withdraws with a plain note, and each build's own start accepts the stamped authority."""
    with eng.begin() as c:   # two squads, two builds each: four builds, one builder
        first, second = _squad_build(c, world, title="Bump dep 0"), _squad_build(c, world, title="Bump dep 1")
        builds = [first, second, _squad_build(c, world, title="Bump dep 2", same_as=first),
                  _squad_build(c, world, title="Bump dep 3", same_as=second)]
    with monkeypatch.context() as m:              # the earlier release: no Captain authority yet
        m.setattr(person_wait, "_captain_verdict", lambda *a, **k: None)
        m.setattr(person_wait, "_resume_on_captain", lambda *a, **k: None)
        with eng.begin() as c:
            paused = [_pause(c, world, b["run"]) for b in builds]
            assert person_wait.poll_due(c, get_settings()) == 0
    card_id = paused[0]["decision_id"]
    assert {p["decision_id"] for p in paused} == {card_id}
    with eng.begin() as c:
        assert c.execute(text("SELECT recommendation->>'action' FROM decisions WHERE id = :d"),
                         {"d": card_id}).scalar() == person_wait.ACTION_GRANT_BUILDER
        resumed = person_wait.poll_due(c, get_settings())
        card = c.execute(text("SELECT status, resolution_note FROM decisions WHERE id = :d"),
                         {"d": card_id}).mappings().one()
        rows = c.execute(text("SELECT status, phase FROM code_task_runs WHERE id = ANY(:i)"),
                         {"i": [b["run"] for b in builds]}).mappings().all()
        stamped = [identity_runs.original_build_authority(c, b["run"]) for b in builds]
    assert resumed == 4
    assert all((r["status"], r["phase"]) == ("running", "queued") for r in rows)
    assert card["status"] == "withdrawn" and "Captain authority" in card["resolution_note"]
    assert all(a.principal_id == world["person"] and a.grant_id for a in stamped)
    # The resumed attempt's own start: the stamped Captain authority is re-checked and accepted.
    acting, verdict = identity_runs._captain_authority(eng, stamped[0], task_run_id=builds[0]["run"],
                                                       project_id=world["project"])
    assert verdict.covered and acting.grant_id == stamped[0].grant_id
    with eng.begin() as c:   # a second sweep resumes nothing twice
        assert person_wait.poll_due(c, get_settings()) == 0


def test_the_laptops_queued_shape_starts_on_the_authority_directly(eng, world):
    """The builds as the laptop holds them now: running/queued, a sys_legacy_system job, no grant."""
    with eng.begin() as c:
        b = _squad_build(c, world)
        recorded = identity_runs.original_build_authority(c, b["run"])
    assert recorded is not None and not recorded.grant_id
    acting, verdict = identity_runs._captain_authority(eng, recorded, task_run_id=b["run"],
                                                       project_id=world["project"])
    assert verdict.covered and acting.grant_id == verdict.grant_id


def test_a_class_demoted_since_refuses_a_build_stamped_with_the_authority(eng, world):
    with eng.begin() as c:
        b = _squad_build(c, world)
    acting, verdict = identity_runs._captain_authority(eng, None, task_run_id=b["run"],
                                                       project_id=world["project"])
    assert verdict.covered
    with eng.begin() as c:
        policy.set_level(c, world["project"], "security_patch", actor="ceo", level="A1",
                         reason="reverted a patch", kind="demoted")
    again, verdict2 = identity_runs._captain_authority(eng, acting, task_run_id=b["run"],
                                                       project_id=world["project"])
    assert again is None and not verdict2.covered and verdict2.reason == "autonomy:security_patch"

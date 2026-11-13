"""BOSSAPP-890: the org's skill-access rules, as data, checked at every skill seam.

No rule allows (the default is unchanged) -> the first matching rule decides, project rules before
org rules at the same position -> each seam refuses with the rule's name: create/edit (save_skill),
an agent's draft (draft_skill), import (skill_sources, by source), test, remove, hold (a grant), and
invoke (recorded as a denied invocation) -> the simulator answers without counting a hit.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import skill_definitions as sd
from boss_engine.services import skill_policy as sp

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM skill_policy_rules LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'w4o1 890', :s)"), {"i": pid, "w": wid, "s": pid})
        for role in ("forge", "harbor"):
            c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name) "
                           "VALUES (:i, :p, :r, :r)"),
                      {"i": f"role_{uuid.uuid4().hex[:8]}", "p": pid, "r": role})
    yield pid
    with eng.begin() as c:
        org = c.execute(text("SELECT w.org_id FROM projects p JOIN workspaces w "
                             "ON w.id = p.workspace_id WHERE p.id = :p"), {"p": pid}).scalar()
        c.execute(text("DELETE FROM skill_policy_rules WHERE project_id = :p OR "
                       "(project_id IS NULL AND org_id = :o AND created_by = :tag)"),
                  {"p": pid, "o": org, "tag": f"test:{pid}"})
        for table in ("role_skills", "skill_versions", "skill_sources", "change_consents",
                      "skill_definitions", "skill_invocations", "policy_evaluations",
                      "budget_reservations", "decisions", "events", "agent_identities",
                      "seed_exclusions"):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
            except Exception:  # noqa: BLE001 — a table this install lacks
                pass
        c.execute(text("DELETE FROM roles WHERE project_id=:p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


def _save(c, pid, key, *, actor="operator", tier=1, mode="upsert"):
    return sd.save_skill(c, project_id=pid, actor=actor, skill_key=key, label=key,
                         behaviour_kind="builtin", config={"executor_id": "model.generate_text"},
                         risk_tier=tier, mode=mode, origin="person")


def _rule(c, pid, **fields):
    return sp.create_rule(c, pid, fields, actor="operator")


def test_no_rule_allows_everything_as_before(eng, project):
    with eng.begin() as c:
        assert sp.list_rules(c, project) == []
        out = sp.evaluate(c, project, action="invoke", skill_key="anything", role_key="forge")
        assert out["decision"] == "allow" and out["rule_id"] is None
        _save(c, project, "notes")
        sd.assign_skill_to_role(c, project_id=project, actor="operator", role_key="forge",
                                skill_id="notes")


def test_first_match_decides_and_project_rules_read_before_org_rules(eng, project):
    with eng.begin() as c:
        org_deny = sp.create_rule(c, project, {"scope": "org", "effect": "deny", "position": 10,
                                               "actions": ["create"], "skill_pattern": "deploy_*"},
                                  actor=f"test:{project}")
        proj_allow = _rule(c, project, effect="allow", position=10, actions=["create"],
                           skill_pattern="deploy_preview")
        order = [r["id"] for r in sp.list_rules(c, project)]
        assert order.index(proj_allow["id"]) < order.index(org_deny["id"])
        _save(c, project, "deploy_preview")                 # the project's allow wins
        with pytest.raises(sp.SkillPolicyDenied) as refused:
            _save(c, project, "deploy_prod")                # the org's deny
        assert org_deny["id"] in str(refused.value) and refused.value.status == 403
        # Editing is another action: the org rule names create only.
        _save(c, project, "deploy_preview")


def test_hold_is_checked_for_the_role_being_given_the_skill(eng, project):
    with eng.begin() as c:
        _save(c, project, "deploy_prod_x")
        _rule(c, project, effect="deny", actions=["hold", "invoke"], skill_pattern="deploy_*",
              role_pattern="forge", note="only the release agent deploys")
        with pytest.raises(sp.SkillPolicyDenied, match="only the release agent deploys"):
            sd.assign_skill_to_role(c, project_id=project, actor="operator", role_key="forge",
                                    skill_id="deploy_prod_x")
        held = sd.assign_skill_to_role(c, project_id=project, actor="operator",
                                       role_key="harbor", skill_id="deploy_prod_x")
        assert held["assigned"] is True


def test_invoke_denied_is_a_recorded_refusal(eng, project):
    from boss_engine.services.skill_runtime import SkillRuntimeService

    with eng.begin() as c:
        _save(c, project, "summarise", tier=0)
        sd.assign_skill_to_role(c, project_id=project, actor="operator", role_key="forge",
                                skill_id="summarise")
        before = SkillRuntimeService(Settings()).invoke(c, project, "summarise", "forge",
                                                        {"text": "x"}, mock=True, actor="forge")
        assert before["status"] != "denied", before
        rule = _rule(c, project, effect="deny", actions=["invoke"], skill_pattern="summ*",
                     actor_kind="agent")
        after = SkillRuntimeService(Settings()).invoke(c, project, "summarise", "forge",
                                                       {"text": "x"}, mock=True, actor="forge")
        assert after["status"] == "denied" and rule["id"] in (after.get("reason") or "")
        hits = c.execute(text("SELECT hit_count FROM skill_policy_rules WHERE id=:i"),
                         {"i": rule["id"]}).scalar()
        assert hits == 1


def test_an_agents_draft_is_refused_before_a_card_opens(eng, project):
    with eng.begin() as c:
        _rule(c, project, effect="deny", actions=["create", "edit"], actor_kind="agent",
              min_risk_tier=2, note="agents never write tier 2+")
        with pytest.raises(sp.SkillPolicyDenied):
            sd.draft_for_approval(c, project_id=project, actor="agent:forge", skill_key="risky",
                                  label="risky", behaviour_kind="builtin",
                                  config={"executor_id": "model.generate_text"}, risk_tier=2)
        cards = c.execute(text("SELECT count(*) FROM decisions WHERE project_id=:p"),
                          {"p": project}).scalar()
        assert cards == 0
        # Below the floor: a card opens as before.
        out = sd.draft_for_approval(c, project_id=project, actor="agent:forge", skill_key="safe",
                                    label="safe", behaviour_kind="builtin",
                                    config={"executor_id": "model.generate_text"}, risk_tier=1)
        assert out["status"] == "pending"


def test_import_is_checked_with_its_source_and_test_and_remove_too(eng, project):
    from boss_engine.services import skill_sources
    from boss_engine.services.skill_sources import Fetched
    from boss_engine.services.skill_tests import run_cases

    def fetcher(loc, ref):
        return Fetched(sha="a" * 40, text="---\nname: house-tone\ndescription: tone\n---\nBe kind.",
                       files={})

    with eng.begin() as c:
        _rule(c, project, effect="allow", position=1, actions=["import"],
              source_pattern="github:https://github.com/acme/*")
        _rule(c, project, effect="deny", position=2, actions=["import"])
        ok = skill_sources.import_source(
            c, fetcher, project_id=project, actor="operator", kind="github",
            locator="https://github.com/acme/playbooks/tree/main/skills/house-tone",
            ref="main", skill_key="house_tone")
        assert ok["skill"]["skill_key"] == "house_tone"
        with pytest.raises(sp.SkillPolicyDenied):
            skill_sources.import_source(
                c, fetcher, project_id=project, actor="operator", kind="github",
                locator="https://github.com/stranger/skills/tree/main/skills/house-tone",
                ref="main", skill_key="other_tone")
        _rule(c, project, effect="deny", position=3, actions=["test", "remove"],
              skill_pattern="house_*")
        with pytest.raises(sp.SkillPolicyDenied):
            run_cases(c, None, project_id=project, actor="operator", skill_key="house_tone")
        with pytest.raises(sp.SkillPolicyDenied):
            sd.delete_skill(c, project_id=project, actor="operator", skill_key="house_tone")


def test_the_simulator_decides_like_a_check_and_counts_nothing(eng, project):
    with eng.begin() as c:
        rule = _rule(c, project, effect="deny", actions=["install"], source_pattern="bundle:unsigned")
        out = sp.simulate(c, project, {"action": "install", "skill_key": "x",
                                       "source": "bundle:unsigned"})
        assert out["decision"] == "deny" and out["rule_id"] == rule["id"]
        assert sp.simulate(c, project, {"action": "install", "skill_key": "x",
                                        "source": "bundle:acme"})["decision"] == "allow"
        assert c.execute(text("SELECT hit_count FROM skill_policy_rules WHERE id=:i"),
                         {"i": rule["id"]}).scalar() == 0
        sp.update_rule(c, project, rule["id"], {"status": "disabled"}, actor="operator")
        assert sp.simulate(c, project, {"action": "install", "skill_key": "x",
                                        "source": "bundle:unsigned"})["decision"] == "allow"
        revoked = sp.revoke_rule(c, project, rule["id"], actor="operator")
        assert revoked["status"] == "revoked"
        with pytest.raises(sp.SkillPolicyError):
            sp.update_rule(c, project, rule["id"], {"status": "active"}, actor="operator")
        with pytest.raises(sp.SkillPolicyError):
            _rule(c, project, effect="maybe")
        with pytest.raises(sp.SkillPolicyError):
            _rule(c, project, effect="deny", actions=["fly"])

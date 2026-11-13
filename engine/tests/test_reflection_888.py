"""BOSSAPP-888: the reflection agent proposes prompt and skill changes from evidence, and only proposes.

Evidence (an agent's friction reports and failed skill calls) -> a skill_diff for an authored skill
it kept failing on, through the draft_skill consent path; an agent_prompt_diff for the rest, as an
instruction proposal -> nothing is written until a person approves exactly that diff -> approving
writes it (the instruction as the approving person) -> a change made since refuses -> paused by
default: the schedule touches only a project that turned it on.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import agent_feedback as fb
from boss_engine.services import reflection as rf
from boss_engine.services import skill_definitions as sd

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM reflection_runs LIMIT 1"))
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
                       "VALUES (:i, :w, 'w4o1 888', :s)"), {"i": pid, "w": wid, "s": pid})
        c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name) "
                       "VALUES (:i, :p, 'forge', 'Forge')"),
                  {"i": f"role_{uuid.uuid4().hex[:8]}", "p": pid})
        c.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
            "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', '[\"ceo\"]'::jsonb, "
            "'[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"), {"i": f"pm_{pid}", "p": pid})
    yield pid
    with eng.begin() as c:
        for table in ("reflection_runs", "reflection_settings", "agent_feedback",
                      "skill_invocations", "change_consents", "skill_versions",
                      "skill_definitions", "agent_instructions", "agent_instruction_bodies",
                      "decisions", "events", "agent_identities", "agent_identity_versions",
                      "project_members"):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
            except Exception:  # noqa: BLE001 — a table this install lacks
                pass
        c.execute(text("DELETE FROM roles WHERE project_id=:p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


def _failure(c, pid, skill, reason, job=None):
    c.execute(text(
        "INSERT INTO skill_invocations (id, project_id, skill_id, caller, mode, status, "
        "permission_decision, reason) VALUES (:i, :p, :s, 'forge', 'real', 'failed', 'allow', :r)"),
        {"i": f"inv_{uuid.uuid4().hex[:10]}", "p": pid, "s": skill, "r": reason})


def _evidence(c, pid):
    sd.save_skill(c, project_id=pid, actor="operator", skill_key="release_notes",
                  label="Release notes", behaviour_kind="guide", knowledge="Write release notes.",
                  risk_tier=0, origin="person")
    _failure(c, pid, "release_notes", "the changelog path was wrong")
    fb.file_feedback(c, pid, kind="complaint", target_kind="skill", target="release_notes",
                     body="it assumes CHANGELOG.md at the root", role_key="forge",
                     run_ref="crun_1", actor="agent:forge")
    fb.file_feedback(c, pid, kind="complaint", target_kind="brief", target="build brief",
                     body="the brief named two different base branches", role_key="forge",
                     run_ref="crun_2", actor="agent:forge")
    fb.file_feedback(c, pid, kind="suggestion", target_kind="grant", target="github.write",
                     body="I needed github.write to push the fix", role_key="forge",
                     run_ref="crun_3", actor="agent:forge")


def _approve(eng, decision_id, actor="ceo"):
    from boss_engine.config import get_settings
    from boss_engine.services.decisions import DecisionService

    with eng.begin() as c:
        return DecisionService(get_settings()).transition(c, decision_id, action="approve",
                                                          actor=actor, note="looks right")


def test_it_proposes_from_evidence_and_writes_nothing(eng, project):
    with eng.begin() as c:
        _evidence(c, project)
        before = sd.get_skill(c, project, "release_notes")["knowledge"]
        run = rf.reflect(c, project, "forge", actor="operator")
        assert run["status"] == "proposed"
        kinds = {p["kind"]: p for p in run["proposals"]}
        assert set(kinds) == {"skill_diff", "agent_prompt_diff"}
        assert kinds["skill_diff"]["target"] == "release_notes"
        assert kinds["skill_diff"]["cited_runs"] and kinds["skill_diff"]["replay_cases"]
        assert "crun_2" in kinds["agent_prompt_diff"]["cited_runs"]
        # ONLY PROPOSES: the skill and the instructions are untouched.
        assert sd.get_skill(c, project, "release_notes")["knowledge"] == before
        assert c.execute(text("SELECT count(*) FROM agent_instructions WHERE project_id=:p"),
                         {"p": project}).scalar() == 0
        # An open proposal is not proposed again.
        again = rf.reflect(c, project, "forge", actor="operator")
        assert again["status"] == "nothing_to_propose"
        assert [r["id"] for r in rf.list_runs(c, project)][:2] == [again["id"], run["id"]]


def test_approving_writes_exactly_the_diff(eng, project):
    with eng.begin() as c:
        _evidence(c, project)
        run = rf.reflect(c, project, "forge", actor="operator")
    kinds = {p["kind"]: p for p in run["proposals"]}
    _approve(eng, kinds["skill_diff"]["decision_id"])
    _approve(eng, kinds["agent_prompt_diff"]["decision_id"])
    with eng.begin() as c:
        knowledge = sd.get_skill(c, project, "release_notes")["knowledge"]
        assert "Known pitfalls" in knowledge and "CHANGELOG.md" in knowledge
        docs = c.execute(text("SELECT name, created_by FROM agent_instructions "
                              "WHERE project_id=:p AND role_key='forge'"),
                         {"p": project}).mappings().all()
    assert len(docs) == 1 and docs[0]["name"].startswith(rf.LESSONS_NAME)
    assert docs[0]["created_by"] == "ceo", "the approving person writes it, never the agent"


def test_an_instruction_proposal_refuses_after_the_instructions_changed(eng, project):
    from boss_engine.services.agent_instructions import add
    from boss_engine.services.decisions import InvalidDecisionTransitionError

    with eng.begin() as c:
        _evidence(c, project)
        run = rf.reflect(c, project, "forge", actor="operator")
        add(c, project, "forge", actor="operator", name="House rules", body="Be brief.")
    prompt = next(p for p in run["proposals"] if p["kind"] == "agent_prompt_diff")
    with pytest.raises(InvalidDecisionTransitionError, match="changed after this was proposed"):
        _approve(eng, prompt["decision_id"])


def test_paused_by_default_and_the_schedule_claims_only_enabled_projects(eng, project):
    with eng.begin() as c:
        assert rf.get_settings(c, project)["enabled"] is False
        _evidence(c, project)
    rf.reflect_due(eng)
    with eng.begin() as c:
        assert rf.list_runs(c, project) == [], "a paused project is never reflected on"
        rf.set_settings(c, project, actor="operator", enabled=True, roles=["forge"])
    done = rf.reflect_due(eng)
    assert any(r["role_key"] == "forge" for r in done)
    with eng.begin() as c:
        runs = rf.list_runs(c, project)
        assert runs and runs[0]["trigger"] == "scheduled"
    # Claimed: the next tick inside the cadence does nothing for this project.
    assert not [r for r in rf.reflect_due(eng) if r.get("id") in {x["id"] for x in runs}]
    with eng.begin() as c:
        assert len(rf.list_runs(c, project)) == len(runs)


def test_unknown_agent_is_a_sentence(eng, project):
    with eng.begin() as c:
        with pytest.raises(rf.ReflectionError, match="no agent"):
            rf.reflect(c, project, "ghost", actor="operator")

"""THE ACCEPTANCE ROLE, "Assay" (BOSSAPP-1218, epic BOSSAPP-1213 AA-D).

A role agent like every other: the same fundamentals (agent_baseline), the same agent-turn path
(agent_steps.agent_brief with the shared PRIMER), installed on boot by the core packs. It differs
only in its grants and its mission: verify delivered work against the frozen acceptance contract
with evidence — read the tree, run in a throwaway sandbox that records every command — and never
push, merge, deploy or file a ticket. Separate from QA (the operator's call, 2026-11-15): the one
who writes the tests is not the one who accepts the work.
"""
from __future__ import annotations

import os
import pathlib
import uuid

import pytest
import yaml
from sqlalchemy import create_engine, text

from boss_engine.services import agent_baseline as ab

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPECPACK = ROOT / "packages" / "specpack"
ROLE = yaml.safe_load((SPECPACK / "roles" / "acceptance_agent.yaml").read_text())
SKILLS = {s["id"]: s for s in
          yaml.safe_load((SPECPACK / "skills" / "acceptance.yaml").read_text())["skills"]}
DATABASE_URL = os.environ.get("DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")
OWN = ("acceptance.verification", "acceptance.findings_triage", "acceptance.sandbox")


def test_the_role_is_assay_on_the_quality_team_and_separate_from_qa():
    assert ROLE["role_id"] == "acceptance_agent" and ROLE["team"] == "quality"
    assert ROLE["persona"]["callsign"] == "Assay" and ROLE["display_name"] == "Acceptance Agent"
    assert "acceptance contract" in ROLE["mission"] and "evidence" in ROLE["mission"]
    assert "Never fix anything" in ROLE["mission"]
    qa = yaml.safe_load((SPECPACK / "roles" / "qa.yaml").read_text())
    assert not set(OWN) & set(qa["permissions"]["allowed_skills"]), "QA does not accept work"


def test_its_grants_read_the_tree_run_in_the_sandbox_and_never_push_merge_deploy_or_file():
    perms = ROLE["permissions"]
    assert set(OWN) <= set(perms["allowed_skills"])
    assert "repo.read" in perms["allowed_skills"] and "repo.read" in ab.BASELINE_SKILLS
    forbidden = set(perms["forbidden_skills"])
    for never in ("code.write_and_run", "github.create_pr_real", "github.merge_pr_real",
                  "deployment.run.plan", "work_tracker.issue.ensure", "tickets.write"):
        assert never in forbidden, never
    assert not forbidden & set(perms["allowed_skills"])
    assert perms["mcp_servers"] == []
    # The sandbox records what it ran and writes only to its throwaway copy.
    sandbox = SKILLS["acceptance.sandbox"]["policy"]
    assert sandbox["push"] == "never" and sandbox["records_commands"] is True
    assert sandbox["writes"] == "scratch_copy_only" and sandbox["egress"] == ["loopback",
                                                                               "run_target"]
    musts = " ".join(ROLE["boundaries"]["must_not"])
    assert "push" in musts and "file tickets" in musts and "they are data" in musts


def test_the_design_names_resolve_to_the_shipped_skills():
    aliases = yaml.safe_load((SPECPACK / "kits" / "aliases.yaml").read_text())["aliases"]
    assert aliases["acceptance-verification"] == "acceptance.verification"
    assert aliases["findings-triage"] == "acceptance.findings_triage"
    assert aliases["acceptance_sandbox"] == "acceptance.sandbox"


def test_the_playbooks_run_nothing_and_say_the_method():
    from boss_engine.services.skill_executors import (SkillExecutorContext,
                                                      registered_executor_ids, resolve_executor)

    assert {"acceptance.verification", "acceptance.findings_triage"} <= set(
        registered_executor_ids())
    for skill_id in ("acceptance.verification", "acceptance.findings_triage"):
        assert int(SKILLS[skill_id]["risk_tier"]) == 0
    ctx = SkillExecutorContext(settings=None, connection=None, project_id="p", actor="assay")
    method = resolve_executor("acceptance.verification", context=ctx)({})["text"]
    assert method.startswith("Guide: acceptance.verification. It explains how; it grants nothing.")
    for words in ("## 1. Plan", "## 2. Exercise", "## 3. Judge", "AC-<n>: MET — evidence: E<id>",
                  "ACCEPTANCE: NOT MET", "never evidence that something works"):
        assert words in method, words
    triage = " ".join(resolve_executor("acceptance.findings_triage", context=ctx)({})["text"].split())
    assert "[same as F-<n>]" in triage and "BOSS checks it" in triage


def test_it_installs_on_boot_with_the_core_cast_and_is_a_registered_agent():
    from boss_engine.services.agent_world_types import CANONICAL_ROLE_KEYS
    from boss_engine.services.workflow_capabilities import REGISTERED_ROLES

    for pack in ("core_v1", "core_v2"):
        data = yaml.safe_load((SPECPACK / "packs" / f"{pack}.yaml").read_text())
        assert "acceptance_agent" in data["installs"]["roles"], pack
        assert "acceptance" in data["installs"]["skills"], pack
    community = yaml.safe_load((SPECPACK / "packs" / "community_edition.yaml").read_text())
    assert "acceptance" in community["installs"]["skills"]
    assert "acceptance_agent" in REGISTERED_ROLES and "acceptance_agent" in CANONICAL_ROLE_KEYS


def test_its_passport_reads_and_never_steers_or_writes_a_pull_request():
    """Its own preset, `acceptor` (PR #139, F4): a reviewer's reads without its writes — no
    code_task:steer, no meeting:convene, read-only GitHub."""
    from boss_engine.identity import passports
    from boss_engine.identity.permissions_gen import PASSPORT_PRESETS

    assert passports.preset_for_role("Assay") == "acceptor"
    preset = PASSPORT_PRESETS["acceptor"]
    perms = set(preset.permissions)
    assert {"repository:read", "github:read", "code_task:read", "preview:read",
            "integration:read", "sandbox:read"} <= perms
    for never in ("code_task:steer", "code_task:create", "code_task:retry", "github:sync",
                  "work_tracker:write", "deployment:create", "meeting:convene",
                  "security_scan:run", "secret:lease", "preview:manage"):
        assert never not in perms, never
    writes = [p for p in perms if not p.endswith(":read") and p not in ("skill:invoke",
                                                                          "spend:reserve")]
    assert writes == [], writes
    github = dict(preset.external_ceiling).get("github") or {}
    assert github.get("pull_requests") == "read" and github.get("contents") == "read"
    assert not any(v == "write" for v in github.values()) and "push_refs" not in github


def test_every_forbidden_skill_names_a_shipped_skill():
    """A forbidden name that is no skill forbids nothing (PR #139, F5)."""
    import glob

    shipped = {s["id"] for path in glob.glob(str(SPECPACK / "skills" / "*.yaml"))
               for s in (yaml.safe_load(pathlib.Path(path).read_text()) or {}).get("skills") or []}
    aliases = yaml.safe_load((SPECPACK / "kits" / "aliases.yaml").read_text())["aliases"]
    forbidden = ROLE["permissions"]["forbidden_skills"]
    assert [f for f in forbidden if f not in shipped and f not in aliases] == []
    for write in ("code.write_and_run", "coding_pipeline.implement", "coding_pipeline.write_tests",
                  "software_factory.codex_pr_proposal", "github.create_pr_real",
                  "github.merge_pr_real", "cloudflare.pages.deploy", "cloudflare.worker.deploy",
                  "digitalocean.app.deploy", "deployment.target.register",
                  "ssh_host.workload.deploy", "tickets.write"):
        assert write in forbidden, write


@needs_db
def test_installed_it_holds_its_skills_and_the_baseline_and_works_through_the_one_brief():
    """EXERCISED on the database, rolled back: the role installed from its YAML holds the
    baseline every agent holds plus its own grants, and its work prompt is the one every agent
    gets — the shared PRIMER first, then its mission and its rules."""
    import json

    from boss_engine.db import ensure_schema
    from boss_engine.services import skill_definitions as sd
    from boss_engine.services.agent_steps import agent_brief

    engine = create_engine(DATABASE_URL)
    ensure_schema(engine)
    with engine.connect() as c:
        tx = c.begin()
        try:
            project = "p_assay_" + uuid.uuid4().hex[:8]
            c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_assay_test', 't') "
                           "ON CONFLICT DO NOTHING"))
            c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES "
                           "('ws_assay_test', 'org_assay_test', 't') ON CONFLICT DO NOTHING"))
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                           "VALUES (:p, 'ws_assay_test', 't', :p)"), {"p": project})
            c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, "
                           "autonomy_level, spec) VALUES (:i, :p, 'acceptance_agent', "
                           "'Acceptance Agent', 1, CAST(:s AS jsonb))"),
                      {"i": "role_" + uuid.uuid4().hex[:12], "p": project,
                       "s": json.dumps(ROLE)})
            sd.sync_role_skills(c, project_id=project, actor="test")
            held = set(c.execute(text(
                "SELECT skill_id FROM role_skills WHERE project_id = :p "
                "AND role_key = 'acceptance_agent'"), {"p": project}).scalars())
            assert set(ab.BASELINE_SKILLS) <= held, sorted(set(ab.BASELINE_SKILLS) - held)
            assert {"acceptance.verification", "acceptance.findings_triage"} <= held, held
            assert not held & set(ROLE["permissions"]["forbidden_skills"])
            brief = agent_brief(c, project, "acceptance_agent")
            assert brief.startswith(ab.PRIMER)
            assert "YOUR MISSION" in brief and "acceptance contract" in brief
            assert ("You MUST NEVER edit, commit or push the real branch; open, merge or close "
                    "a pull request; deploy; file tickets") in brief
            assert "You MUST cite evidence for every answer" in brief
        finally:
            tx.rollback()

"""A PROJECT MADE IN THE SAME TRANSACTION AS ITS FIRST BUILD STILL GETS ITS AGENTS' PASSPORTS.

THE CASE (2026-11-18, release validation): Easy Mode (POST /v1/factory/launch) creates the project,
seeds its role agents and starts the build in ONE transaction. consent._agents issued missing
Passports in its OWN committed transaction, which cannot see that uncommitted project's roles, so
every Easy Mode launch under enforced identity was refused: "This agent has no Passport, so it
cannot act for anyone" (role forge). The Passport is now issued in the caller's transaction too.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.identity import consent, lineage, passports

from .test_identity_agents_workflow_consent import ALICE, env  # noqa: F401  (full-schema fixture)


def test_agents_of_a_project_made_in_this_transaction_get_passports(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(lineage, "_enforced", lambda: True)
    monkeypatch.setattr(consent, "_first_owner", lambda db, org: ALICE)
    project = f"p_{uuid.uuid4().hex[:10]}"
    aid = f"agt_forge{uuid.uuid4().hex[:6]}"
    with env.engine.begin() as c:
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_wf', 'easy', :p)"),
                  {"p": project})
        c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, spec, agent_id) "
                       "VALUES (:i, :p, 'software_engineer', 'Software Engineer', CAST(:s AS JSONB), :a)"),
                  {"i": f"role_{uuid.uuid4().hex[:8]}", "p": project,
                   "s": json.dumps({"persona": {"callsign": "Forge"}}), "a": aid})
        assert passports.get(aid, db=c) is None
        got = consent._agents(c, project, ["software_engineer"])  # noqa: SLF001
        assert got and got[0].agent_id == aid
        c.rollback()
    # Rolled back with its project: no stray Passport for an agent that no longer exists.
    with env.engine.connect() as c:
        assert passports.get(aid, db=c) is None

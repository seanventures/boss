"""THE REVIEW NAMES ITS AGENT BY NAME, WHATEVER SHAPE THE RUN STORED IT IN (BOSSAPP-954).

On the laptop, change helpdesk-laptop/6 was built by run crun_9c21e6896cea44cab8ec whose
finalize_ctx.agent is the dict {"agent_id": "agt_2m0xahx2jf", "role_key": "engineering_captain"}.
The pane printed str(dict) and CSS title-cased it: "{'Agent Id': 'Agt 2m0xahx2jf', …}".
Every stored shape now reads as the agent's callsign (else the role's name), the role as a
fallback, and never an id.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.test_local_change_review import _project_with_change
from boss_engine.services import local_repo
from boss_engine.services.agent_reference import agent_display, parse_reference


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.viewer = w.person("cra-viewer")
    w.bind(w.viewer, "viewer")
    return w


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    return root


def _seed_role(engine, pid: str, agent_id: str, *, callsign: str | None) -> None:
    spec = {"persona": {"callsign": callsign, "tagline": "Right engineer, right slice."}} \
        if callsign else {"persona": {}}
    with engine.begin() as db:
        db.execute(text("DELETE FROM roles WHERE agent_id = :a OR (project_id = :p AND "
                        "role_key = 'engineering_captain')"), {"p": pid, "a": agent_id})
        db.execute(text(
            "INSERT INTO roles (id, project_id, role_key, display_name, spec, agent_id) VALUES "
            "(:id, :p, 'engineering_captain', 'Engineering Captain', CAST(:s AS JSONB), :a)"),
            {"id": f"role_{pid}_engineering_captain", "p": pid, "s": json.dumps(spec), "a": agent_id})


def _set_agent(engine, run_id: str, value) -> None:
    with engine.begin() as db:
        db.execute(text("UPDATE code_task_runs SET finalize_ctx = jsonb_build_object('agent', "
                        "CAST(:v AS JSONB)) WHERE id = :r"), {"v": json.dumps(value), "r": run_id})


AGENT = "agt_" + uuid.uuid4().hex[:10]
REAL = {"agent_id": AGENT, "role_key": "engineering_captain"}       # change 6's real shape

SHAPES = [
    ("dict (the laptop's change 6)", REAL),
    ("JSON string", json.dumps(REAL)),
    ("Python repr string", str(REAL)),
    ("plain agent id", AGENT),
    ("role key", "engineering_captain"),
    ("agent:role key", "agent:engineering_captain"),
]


@pytest.mark.parametrize("label,stored", SHAPES, ids=[s[0] for s in SHAPES])
def test_every_stored_shape_reads_as_the_agents_name(label, stored, world, engine, repo_root):
    pid, *_rest, run_id = _project_with_change(engine, world, run=True)
    _seed_role(engine, pid, AGENT, callsign="Helm")
    _set_agent(engine, run_id, stored)
    client = TestClient(main.app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(world.viewer)):
        pane = client.get(f"/v1/projects/{pid}/local-changes/1/agent").json()
    assert pane["agent"] == "Helm", (label, pane["agent"])
    assert pane["agent_role"] == "Engineering Captain"
    assert pane["builder"]["builder"] == "Helm" and pane["builder"]["builder_role"] == "Engineering Captain"
    assert "{" not in json.dumps(pane["agent"]) and "agt_" not in pane["agent"]


def test_a_role_without_a_callsign_is_named_by_its_role(world, engine, repo_root):
    pid, *_rest, run_id = _project_with_change(engine, world, run=True)
    _seed_role(engine, pid, AGENT, callsign=None)
    _set_agent(engine, run_id, str(REAL))
    with engine.connect() as db:
        who = agent_display(db, pid, str(REAL))
    # A role with an empty persona of its own reads the shipped one; either way never an id.
    assert who["name"] in ("Engineering Captain", "Helm") and "agt_" not in who["name"]


def test_an_id_with_no_row_is_never_shown(world, engine):
    with engine.connect() as db:
        unknown = agent_display(db, "no-such-project", {"agent_id": "agt_zzzzzzzzzz"})
        by_key = agent_display(db, "no-such-project", "{'agent_id': 'agt_zzzzzzzzzz', "
                                                       "'role_key': 'backend_engineer'}")
        nothing = agent_display(db, "no-such-project", None)
        garbage = agent_display(db, "no-such-project", "{'agent_id': ")
    assert unknown["name"] == "builder"
    assert by_key["name"] == "Backend engineer"           # the role as the fallback, not title-cased ids
    assert nothing["name"] == "builder" and garbage["name"] == "builder"


def test_parse_reference_reads_each_shape():
    assert parse_reference(REAL) == REAL
    assert parse_reference(json.dumps(REAL)) == REAL
    assert parse_reference(str(REAL)) == REAL
    assert parse_reference(AGENT) == {"agent_id": AGENT}
    assert parse_reference("engineering_captain") == {"role_key": "engineering_captain"}
    assert parse_reference("agent:engineering_captain") == {"role_key": "engineering_captain"}
    assert parse_reference("Helm") == {"name": "Helm"}
    assert parse_reference("") == {} and parse_reference(None) == {}

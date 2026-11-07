"""The Project page's drawers read their own story (screens 05b and 09).

* Captain drawer (09): GET …/captain carries the scope ("<Project> only"), how many facts the
  captain knows (the project's knowledge graph), what it and its crew did lately (agent_acts, in
  words, never a raw brief), and suggestion chips grounded in the record — a merge waiting on a
  person names its PR ("Why is PR #3 waiting?").
* Work drawer (05b): GET …/work/{id}/story tells each station in words with its proof link —
  the PRD the panel agreed, who approved, files changed with the Diff, tests and review rounds,
  "Only a person merges" with the PR — plus the run's number, its minutes and the cost split
  Plan · Build · Review from the build's recorded stage costs. Unknown work is a 404; a stranger
  reads nothing.

Real Postgres, the project seeded the way the build path records it; no model, no GitHub.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import seed


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("drawer-owner")
    w.bind(w.owner, "owner")
    w.stranger = w.person("drawer-stranger")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def helpdesk(world, client, engine):
    slug = f"helpdesk-drawers-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        story = seed(db, slug)
    return {"id": slug, **story}


def _get(world, client, who, path):
    with world.acting_as(world.ctx(who)):
        return client.get(path)


def _act(engine, project_id: str, role: str, label: str, status: str = "complete") -> None:
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO agent_acts (act_id, project_id, role_key, kind, status, label, started_at, "
            "root_act_id, handed_by_kind, how, considered, scope, accountable, artifacts, "
            "tokens_in, tokens_out, cost_usd, reconstructed) VALUES (:i, :p, :r, 'stage', :s, :l, "
            ":at, :i, 'person', 'captain', '[]'::jsonb, '{}'::jsonb, '{}'::jsonb, '[]'::jsonb, "
            "0, 0, 0, false)"),
            {"i": f"act_{uuid.uuid4().hex[:20]}", "p": project_id, "r": role, "l": label,
             "s": status, "at": datetime.now(timezone.utc)})


def test_the_captain_drawer_knows_its_scope_facts_recent_acts_and_starters(world, client,
                                                                           helpdesk, engine):
    pid = helpdesk["id"]
    _act(engine, pid, "engineering_captain",
         "Build · Finish the visual refresh the last change promised but did not deliver. The tick",
         status="active")
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO kg_nodes (id, project_id, kind, node_key, name) VALUES "
            "(:a, :p, 'file', 'f1', 'server/sla.mjs'), (:b, :p, 'file', 'f2', 'server/app.mjs')"),
            {"a": f"kgn_{uuid.uuid4().hex[:12]}", "b": f"kgn_{uuid.uuid4().hex[:12]}", "p": pid})
    r = _get(world, client, world.owner, f"/v1/projects/{pid}/captain")
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["name"] and c["role_key"] == "engineering_captain"
    assert c["scope"]["label"] == "HelpDesk AI Demo only"
    assert c["facts"] >= 2
    labels = [x["label"] for x in c["recent"]]
    assert "Build · Finish the visual refresh" in labels, labels
    assert all(len(x.split()) <= 9 for x in labels)
    now = next(x for x in c["recent"] if x["label"].startswith("Build"))
    assert now["state"] == "now" and now["href"].startswith(f"?project_id={pid}&area=agent_ops")
    chips = [s["label"] for s in c["starters"]]
    assert chips[0] == "Plan next week" and "What did this cost?" in chips
    assert "Why is PR #3 waiting?" in chips, chips
    assert all(s["message"] for s in c["starters"])


def test_the_overview_still_reads_the_captain_without_the_drawer_fields(world, client, helpdesk):
    o = _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/overview").json()
    assert o["captain"]["name"] and "starters" not in o["captain"]


def test_the_work_drawer_tells_each_station_in_words_with_its_proof(world, client, helpdesk):
    pid = helpdesk["id"]
    run_id = helpdesk["merge"]["run_id"]
    r = _get(world, client, world.owner, f"/v1/projects/{pid}/work/{run_id}/story")
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["id"] == run_id and st["pr"]["number"] == 3
    assert isinstance(st["number"], int) and st["number"] >= 1
    assert st["minutes"] == 31
    parts = {p["label"]: p["usd"] for p in st["cost_parts"]}
    assert set(parts) == {"Plan", "Build", "Review"} and parts["Build"] > parts["Plan"]
    rows = {s["key"]: s for s in st["stages"]}
    assert rows["plan"]["sub"] == "PRD agreed" and rows["plan"]["link"]["label"] == "PRD"
    assert "area=meetings" in rows["plan"]["link"]["href"]
    assert rows["build"]["sub"].endswith("files changed")
    assert rows["build"]["link"]["external_url"].endswith("/pull/3/files")
    assert rows["pr"]["sub"] == "Only a person merges" and rows["pr"]["person"] is True
    assert rows["pr"]["link"] == {"label": "PR #3",
                                  "external_url": "https://github.com/acme/helpdesk-ai-demo/pull/3"}
    assert all(s["state"] != "skipped" for s in st["stages"])
    # Nothing raw: names and words only.
    assert all(s["name"] in ("Plan", "You approved", "Build", "Review & test", "Merge", "Staging")
               for s in st["stages"])


def test_a_merged_build_reads_merged_and_reviewed(world, client, helpdesk):
    pid = helpdesk["id"]
    run_id = helpdesk["merged"][0]["run_id"]
    st = _get(world, client, world.owner, f"/v1/projects/{pid}/work/{run_id}/story").json()
    rows = {s["key"]: s for s in st["stages"]}
    if "review" in rows:
        assert "review round" in rows["review"]["sub"]
    assert rows["pr"]["sub"] == "Merged"


def test_unknown_work_is_not_found_and_a_stranger_reads_nothing(world, client, helpdesk):
    pid = helpdesk["id"]
    assert _get(world, client, world.owner, f"/v1/projects/{pid}/work/wfr_nope/story"
                ).status_code == 404
    assert _get(world, client, world.stranger,
                f"/v1/projects/{pid}/work/{helpdesk['merge']['run_id']}/story").status_code in (403, 404)
    assert _get(world, client, world.stranger, f"/v1/projects/{pid}/captain").status_code in (403, 404)

"""PUT holders is set-semantic and atomic (skills-mcp §6.3, test 7)."""
from __future__ import annotations

import pytest

PROJECT = "boss-dogfood"


def _put(client, roles, ability="render_docs"):
    return client.put(f"/v1/kit/{ability}/holders",
                      json={"project_id": PROJECT, "actor": "ceo", "roles": roles})


@pytest.fixture(autouse=True)
def _seeded(engine):
    from boss_engine.services.agents import sync_role_mcp

    with engine.begin() as c:
        sync_role_mcp(c, project_id=PROJECT, actor="test-seed")


def test_set_semantics(client):
    first = _put(client, ["qa", "sre"])
    assert first.status_code == 200, first.text
    assert set(first.json()["holders"]) == {"qa", "sre"}
    second = _put(client, ["sre", "cto"])
    assert set(second.json()["holders"]) == {"sre", "cto"}, "qa should have lost it, cto gained"


def test_a_failure_half_way_changes_nothing(client, monkeypatch):
    _put(client, ["qa"])
    from boss_engine.services import agents

    real = agents.grant_mcp
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database went away")
        return real(*args, **kwargs)

    monkeypatch.setattr(agents, "grant_mcp", flaky)
    with pytest.raises(RuntimeError):
        _put(client, ["sre", "cto"])
    monkeypatch.setattr(agents, "grant_mcp", real)
    now = client.get("/v1/kit/render_docs", params={"project_id": PROJECT}).json()
    assert now["holders"] == ["qa"], "a half-done PUT left grants behind"


def test_an_unknown_agent_is_refused(client):
    r = _put(client, ["nobody_here"])
    assert r.status_code == 422 and "nobody_here" in r.json()["detail"]


def test_an_ability_that_needs_setting_up_cannot_be_given(client):
    r = _put(client, ["qa"], ability="email")
    assert r.status_code == 409 and r.json()["detail"] == "Set up Email first"


def test_apply_recommended_is_one_call_and_skips_what_needs_setup(client):
    r = client.post("/v1/kit/recommended", json={"project_id": PROJECT, "actor": "ceo"})
    assert r.status_code == 200, r.text
    skipped = {(s["role_key"], s["ability"]) for s in r.json()["skipped"]}
    # Juno is offered Email (recommended, not granted) and nothing sends mail yet.
    assert ("pm", "email") in skipped
    assert all(s["reason"].startswith("Set up") for s in r.json()["skipped"]
               if s["ability"] in ("email", "team_chat", "open_prs"))

"""The Studio's Check knows what a step needs set up in Skills & MCP (skills-mcp spec §3.3,
test 12): a built-in `build` step on a project with no GitHub repo is warned "Needs Open pull
requests set up" on its step and field `ability`, without blocking Save; with a repo, nothing."""
from __future__ import annotations

from sqlalchemy import text

PROJECT = "boss-dogfood"
SPEC = {"steps": [{"id": "build", "kind": "capability",
                   "binding": {"capability": "build", "inputs": {"task": {"from": "input.task"}}},
                   "next": []}]}


def _check(client):
    r = client.post("/v1/delivery-workflows/check",
                    json={"project_id": PROJECT, "actor": "ceo", "spec": SPEC})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_build_step_without_a_repo_is_warned(client, engine, monkeypatch):
    from boss_engine.services import kit_connections

    monkeypatch.setattr(kit_connections, "github_state",
                        lambda c, s, p: {"connected": False, "repo": None,
                                         "api_base": "https://api.github.com"})
    out = _check(client)
    warn = [p for p in out["problems"] if p.get("field") == "ability"]
    assert warn == [{"step_id": "build", "field": "ability",
                     "message": "Needs Open pull requests set up", "severity": "warning",
                     "ability": "open_prs"}]
    # A warning does not make the spec unsaveable on its own.
    others = [p for p in out["problems"] if p.get("field") != "ability"]
    assert out["ok"] is (not others)


def test_with_a_repo_there_is_nothing_to_say(client, monkeypatch):
    from boss_engine.services import kit_connections

    monkeypatch.setattr(kit_connections, "github_state",
                        lambda c, s, p: {"connected": True, "repo": "acme/app",
                                         "api_base": "https://api.github.com"})
    out = _check(client)
    assert [p for p in out["problems"] if p.get("field") == "ability"] == []

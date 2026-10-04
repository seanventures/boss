"""Test crew: can at least one agent use each Essential right now? (skills-mcp §5.6, test 11)"""
from __future__ import annotations

PROJECT = "boss-dogfood"


def test_rows_report_holders_and_ok_without_a_model(client, engine, monkeypatch):
    from boss_engine.services import kit_connections, model_routing
    from boss_engine.services.agents import sync_role_mcp

    with engine.begin() as c:
        sync_role_mcp(c, project_id=PROJECT, actor="test-seed")
    ran = []

    def passes(begin, settings, project_id, ability, **kw):
        ran.append(ability["key"])
        return kit_connections.result(True, fact="fake", latency_ms=1)

    def no_model(*args, **kwargs):
        raise AssertionError("Test crew without the live check must not call a model")

    monkeypatch.setattr(kit_connections, "run_test", passes)
    monkeypatch.setattr(model_routing, "project_chat_adapter", no_model)
    r = client.post("/v1/kit/test-crew", json={"project_id": PROJECT, "actor": "ceo",
                                               "live": False})
    assert r.status_code == 200, r.text
    rows = {row["ability"]: row for row in r.json()["rows"]}
    assert set(rows) == {"open_prs", "web_research", "file_tickets", "email"}
    assert "live" not in r.json()
    research = rows["web_research"]
    assert research["ok"] is True and "pm" in research["holders"], research
    assert rows["email"]["ok"] is False
    assert "web_research" in ran

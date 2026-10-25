"""GET /v1/entitlements/sources carries the Models row's own pill (BOSSAPP-769, P0-3).

The live bug: Codex's last proof was refused 401. Repo and models' Models row said Login
expired while the Sources list on the same page said Active · Available, and
GET /v1/setup/coding-agents called it connected. Every row of the sources list now carries
`pill` — the same pill the Models row shows, from `setup_view.source_state` — and the
coding-agent list reads the same verdict.
"""
import json

from sqlalchemy import text

from apps.api.tests.setup_helpers import AUTH, PROJECT, as_ceo, fakes, view


def _expire_codex(engine):
    with engine.begin() as c:
        c.execute(text("DELETE FROM entitlement_sources WHERE project_id = :p "
                       "AND source_id = 'openai_codex_chatgpt'"), {"p": PROJECT})
        c.execute(text(
            "INSERT INTO entitlement_sources (id, project_id, provider, source_id, source_kind,"
            " entitlement_kind, owner_type, owner_id, cost_mode, status, auth_method,"
            " connection_status, connection_metadata, observed_capacity, runtime_supported)"
            " VALUES ('es_onestatus_codex', :p, 'openai', 'openai_codex_chatgpt',"
            " 'official_cli', 'subscription', 'workspace_operator', 'workspace_operator',"
            " 'subscription', 'declared', 'official_cli_oauth', 'connected', CAST(:m AS JSONB),"
            " CAST(:c AS JSONB), true)"),
            {"p": PROJECT,
             "m": json.dumps({"last_check": {"ok": False, "state": "login_expired",
                                             "at": "2026-10-24T05:21:30+00:00"}}),
             "c": json.dumps({"credential_present": True, "credential_state": "rejected"})})


def test_the_sources_row_shows_the_models_pill(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    _expire_codex(engine)
    [model] = [m for m in view(client)["models"] if m["source_id"] == "openai_codex_chatgpt"]
    listed = client.get("/v1/entitlements/sources", params={"project_id": PROJECT},
                        headers=AUTH)
    assert listed.status_code == 200, listed.text
    rows = {r["source_id"]: r for r in listed.json()["items"]}
    codex = rows["openai_codex_chatgpt"]
    assert codex["pill"] == {"state": model["pill"]["state"], "label": model["pill"]["label"],
                             "tone": model["pill"]["tone"], "checked_at": model["checked_at"]}
    assert codex["pill"]["state"] == "login_expired" and codex["pill"]["tone"] == "red"
    assert codex["effective_status"] == "login_rejected"
    # A source with no Models row has no pill.
    assert all(r["pill"] is None for sid, r in rows.items()
               if sid not in {m["source_id"] for m in view(client)["models"]})


def test_the_coding_agent_list_does_not_call_a_refused_login_connected(
        client, engine, monkeypatch):
    from boss_engine.services import setup_service

    as_ceo(monkeypatch)
    fakes(monkeypatch)
    _expire_codex(engine)
    monkeypatch.setattr(setup_service, "list_coding_agents", lambda _s: [
        {"name": "codex", "ready": True, "next_step": None,
         "entitlement_source_id": "openai_codex_chatgpt"}])
    answer = client.get("/v1/setup/coding-agents", params={"project_id": PROJECT},
                        headers=AUTH)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["connected"] == [] and body["ready"] == []
    [codex] = body["agents"]
    assert codex["pill"]["state"] == "login_expired"
    assert "boss connect codex" in body["not_ready"][0]["next_step"]

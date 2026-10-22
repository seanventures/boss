"""What a Kit connection stores survives the seed, a disconnect and a re-test (tester round 1).

K1: GitHub Issues passed Save & test, then the tracker seed's upsert wrote owner "" / repo ""
over it, so every later Test failed. K5: disconnecting Library docs deleted Context7 and every
agent's grant, and adding it back landed "Review" with nobody holding it. K10: Acts as showed
the repo, not the account. K20: a GitHub connection whose Setup check failed read "Needs repo".
"""
from __future__ import annotations

from sqlalchemy import text

PROJECT = "boss-dogfood"


def _github(monkeypatch):
    from boss_engine.services import kit_connections

    monkeypatch.setattr(kit_connections, "github_state", lambda c, s, p: {
        "connected": True, "repo": "acme/widgets", "api_base": "https://api.github.com",
        "failing": None, "has_connection": True})
    monkeypatch.setattr(kit_connections, "github_token", lambda c, s, p: "ghp_test_token_0000")


def test_a_connected_tracker_survives_the_seed_and_tests_again(client, engine, monkeypatch):
    from boss_engine.config import get_settings
    from boss_engine.services.work_orchestration import WorkOrchestrationService
    from boss_engine.services.work_tracker_adapters import GitHubIssuesWorkTrackerAdapter

    _github(monkeypatch)
    seen = []

    def passes(self, configuration):
        seen.append((configuration.get("owner"), configuration.get("repo")))
        if not (configuration.get("owner") and configuration.get("repo")):
            return {"ok": False, "message": "GitHub Issues needs owner, repo, and a token."}
        return {"ok": True, "message": "ok", "repository": "acme/widgets", "account": "@boss-bot"}

    monkeypatch.setattr(GitHubIssuesWorkTrackerAdapter, "test_connection", passes)
    r = client.post("/v1/kit/file_tickets/connect", json={
        "project_id": PROJECT, "actor": "ceo", "provider": "github_issues", "fields": {}})
    assert r.status_code == 200, r.text
    assert r.json()["connection"]["acts_as"] == "@boss-bot", "Acts as is the account, not the repo"
    # Every read of the trackers seeds them first (and a boot seeds again).
    with engine.begin() as c:
        WorkOrchestrationService(get_settings()).seed_defaults(c, PROJECT)
    assert client.get("/v1/work-trackers", params={"project_id": PROJECT}).status_code == 200
    with engine.begin() as c:
        config = c.execute(text("SELECT configuration FROM work_tracker_providers "
                                "WHERE project_id = :p AND provider_key = 'github_issues'"),
                           {"p": PROJECT}).scalar()
    assert (config["owner"], config["repo"]) == ("acme", "widgets"), config
    tested = client.post("/v1/kit/file_tickets/test", json={"project_id": PROJECT, "actor": "ceo"})
    assert tested.status_code == 200 and tested.json()["ok"] is True, tested.text
    assert seen[-1] == ("acme", "widgets")


def test_a_failed_setup_check_is_failing_not_needs_repo(client, engine):
    with engine.begin() as c:
        c.execute(text("""INSERT INTO github_connections (id, project_id, connection_label,
                            auth_method, credential_ref, status, last_test_status, last_error,
                            created_by)
                          VALUES ('ghc_kit_failed', :p, 'kit test', 'personal_access_token', 'env:NOPE',
                                  'configured', 'failed',
                                  CAST('{"message": "HTTP 401 Bad credentials"}' AS JSONB), 't')"""),
                  {"p": PROJECT})
    try:
        ability = client.get("/v1/kit/read_repo", params={"project_id": PROJECT}).json()
        assert ability["status"] == "failing", ability["status"]
        assert ability["status_line"] == "Token rejected"
        assert "Bad credentials" in ability["status_tip"]
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM github_connections WHERE id = 'ghc_kit_failed'"))


def test_disconnect_and_reconnect_keeps_the_agents(client, engine, monkeypatch):
    from boss_engine.services import mcp_registry

    monkeypatch.setattr(mcp_registry, "test_entered", lambda *a, **k: {
        "ok": True, "tools": [{"name": "resolve-library-id"}, {"name": "get-library-docs"}],
        "latency_ms": 5})
    added = client.post("/v1/kit/library_docs/connect", json={
        "project_id": PROJECT, "actor": "ceo", "provider": "context7", "fields": {}})
    assert added.status_code == 200, added.text
    first = added.json()
    # A first-party catalogue server is reviewed by being chosen, and comes with its agents.
    assert first["status"] == "ready", (first["status"], first["status_tip"])
    assert first["holders"], "the recommended agents were not given Library docs"
    assert first["last_test"]["fact"] == "2 tools"
    kept = sorted(first["holders"][:2])
    put = client.put("/v1/kit/library_docs/holders", json={
        "project_id": PROJECT, "actor": "ceo", "roles": kept})
    assert put.status_code == 200, put.text
    gone = client.delete("/v1/kit/library_docs/connection",
                         params={"project_id": PROJECT, "actor": "ceo"})
    assert gone.status_code == 200 and gone.json()["status"] == "setup", gone.text
    back = client.post("/v1/kit/library_docs/connect", json={
        "project_id": PROJECT, "actor": "ceo", "provider": "context7", "fields": {}})
    assert back.status_code == 200, back.text
    assert sorted(back.json()["holders"]) == kept, "reconnecting lost the agents' grants"
    assert back.json()["status"] == "ready"


def test_smtp_security_is_chosen_and_the_password_shows_only_set(client, monkeypatch):
    from boss_engine.services.email_adapter import EmailAdapter

    sent = []

    def capture(self, recipient, subject, body):
        sent.append((self.settings.email_smtp_use_tls, self.settings.email_smtp_use_ssl))
        return {"ok": True}

    monkeypatch.setattr(EmailAdapter, "send_message", capture)
    r = client.post("/v1/kit/email/connect", json={
        "project_id": PROJECT, "actor": "ceo", "provider": "smtp",
        "fields": {"host": "relay.internal", "port": "25", "from_address": "boss@acme.dev",
                   "username": "boss", "password": "relay-password-a1f3", "security": "none"}})
    assert r.status_code == 200, r.text
    assert sent == [(False, False)], "a plain relay was sent STARTTLS or SSL"
    fields = r.json()["connection"]["set_fields"]
    # "set", never any part of the key (operator decision 2026-10-22).
    assert fields["password"] == "set" and fields["security"] == "none"
    assert "a1f3" not in r.text
    assert "relay-password" not in r.text

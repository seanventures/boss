"""BOSSAPP-859: a persistent Ed25519 key per agent, published as JWKS, used only inside BOSS.

A third party must be able to check that an output came from a given agent without calling BOSS
for anything but its public keys. The private half is a vault item; the agent signs through the
`sign` tool on its run's gateway server; commits and the PR carry signatures when — and only
when — the build's lead agent holds a key."""
from __future__ import annotations

import base64
import json
import time
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import agent_keys as ak

from ._ambient_support import eng, make_project, project, purge  # noqa: F401

MASTER_KEY = base64.b64encode(b"w" * 32).decode()


@pytest.fixture()
def vaulted(monkeypatch):
    from boss_engine.identity import vault

    monkeypatch.setenv("BOSS_MASTER_KEY", MASTER_KEY)
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    vault.reset_master_key_cache()
    ak.clear_cache_for_tests()
    yield
    vault.reset_master_key_cache()
    ak.clear_cache_for_tests()


def _agent(c, pid, role_key="builder"):
    agent_id = "agt_" + "".join("0123456789abcdefghjkmnpqrstvwxyz"[b % 32]
                                for b in uuid4().bytes[:10])
    c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, agent_id) "
                   "VALUES (:id, :p, :r, :r, :a)"),
              {"id": f"role_{uuid4().hex[:10]}", "p": pid, "r": role_key, "a": agent_id})
    return agent_id


def _cleanup(eng, pid):
    with eng.begin() as c:
        items = [r[0] for r in c.execute(text(
            "SELECT private_vault_item_id FROM agent_keys WHERE project_id = :p"), {"p": pid})]
        c.execute(text("DELETE FROM agent_keys WHERE project_id = :p"), {"p": pid})
        if items:
            c.execute(text("DELETE FROM identity_vault_items WHERE id = ANY(:i)"), {"i": items})


def test_the_signed_message_is_bound_to_its_purpose():
    assert ak.message_for("commit", "x") == b"boss.agent-sig/v1\ncommit\nx"
    with pytest.raises(ak.AgentKeyError):
        ak.message_for("anything", "x")


def test_a_key_is_created_signs_verifies_from_the_jwks_and_rotates(eng, project, vaulted):  # noqa: F811
    try:
        with eng.begin() as c:
            agent_id = _agent(c, project)
            assert ak.jwks_for_agent(c, agent_id) == {"agent_id": agent_id, "keys": []}
            key = ak.create_key(c, project, "builder", actor="op")
            assert "d" not in key["public_jwk"] and "PRIVATE" not in key["public_pem"]
            stored = c.execute(text("SELECT secret_enc FROM identity_vault_items WHERE id = "
                                    "(SELECT private_vault_item_id FROM agent_keys WHERE kid=:k)"),
                               {"k": key["kid"]}).scalar()
            assert b"PRIVATE KEY" not in bytes(stored)
            with pytest.raises(ak.AgentKeyError):
                ak.create_key(c, project, "builder", actor="op")   # one active key
        ak.clear_cache_for_tests()                                # the key comes from the vault
        with eng.begin() as c:
            signed = ak.sign(c, project, "builder", "the report", purpose="output")
            doc = ak.jwks_for_agent(c, agent_id)
        assert "private" not in json.dumps(signed).lower()
        jwk = doc["keys"][0]
        assert jwk["kid"] == signed["kid"] == key["kid"] and jwk["status"] == "active"
        assert ak.verify(jwk, "the report", signed["signature"], purpose="output")
        assert not ak.verify(jwk, "the report!", signed["signature"], purpose="output")
        assert not ak.verify(jwk, "the report", signed["signature"], purpose="commit")

        with eng.begin() as c:
            new = ak.create_key(c, project, "builder", actor="op", rotate=True)
            doc = ak.jwks_for_agent(c, agent_id)
        assert [k["status"] for k in doc["keys"]] == ["active", "retired"]
        assert doc["keys"][0]["kid"] == new["kid"]
        # what the retired key signed still verifies from the published set
        assert ak.verify(doc["keys"][1], "the report", signed["signature"], purpose="output")

        with eng.begin() as c:
            ak.revoke_key(c, project, "builder", key["kid"], actor="op", reason="leaked")
            doc = ak.jwks_for_agent(c, agent_id)
        assert [k["kid"] for k in doc["keys"]] == [new["kid"]]
        with eng.begin() as c:
            assert ak.jwks_for_agent(c, "agt_unknownnn") is None
    finally:
        _cleanup(eng, project)


def test_an_agent_without_a_key_signs_nothing(eng, project, vaulted):  # noqa: F811
    with eng.begin() as c:
        _agent(c, project, "reviewer")
        assert not ak.has_active_key(c, project, "reviewer")
        with pytest.raises(ak.AgentKeyError):
            ak.sign(c, project, "reviewer", "x")


def test_a_commit_carries_a_verifiable_trailer(eng, project, vaulted):  # noqa: F811
    try:
        with eng.begin() as c:
            _agent(c, project)
            ak.create_key(c, project, "builder", actor="op")
            msg = "Add the login page\n\nOn-behalf-of: Alice <a@x.test>"
            signed = ak.sign_commit(c, msg, (project, "builder"))
            doc = ak.jwks_for_agent(c, ak.keys_for(c, project, "builder")[0]["agent_id"])
        assert signed.startswith(msg + "\nBOSS-Agent-Signature: ")   # joins the trailer block
        body, agent_id, kid, sig = ak.split_signed_commit(signed)
        assert body == msg and kid == doc["keys"][0]["kid"]
        assert ak.verify(doc["keys"][0], body, sig, purpose="commit")
    finally:
        _cleanup(eng, project)


def test_commit_messages_are_unchanged_without_a_keyed_lead(monkeypatch):
    from boss_engine.services import agent_acts

    monkeypatch.setattr(agent_acts, "_current_code_run", lambda: None)
    assert agent_acts.sign_commit_message("chore: x") == "chore: x"
    monkeypatch.setattr(agent_acts, "_KEYED", {"crun_x": False})
    monkeypatch.setattr(agent_acts, "_SIGNED", {"crun_x": ""})
    assert agent_acts.sign_commit_message("chore: x", run_id="crun_x") == "chore: x"


def test_the_pr_evidence_is_a_signed_statement(eng, project, vaulted, monkeypatch):  # noqa: F811
    try:
        with eng.begin() as c:
            agent_id = _agent(c, project)
            ak.create_key(c, project, "builder", actor="op")
        monkeypatch.setattr(ak, "signer_for_run", lambda c, r: (project, "builder"))
        with eng.begin() as c:
            lines = ak.pr_evidence(c, "crun_1", agent_id=agent_id, act_id="act_1")
            doc = ak.jwks_for_agent(c, agent_id)
        key_line, ev_line = lines.splitlines()
        assert key_line.startswith(f"BOSS-Agent-Key: {agent_id} ")
        statement_b64, sig = ev_line.split(": ", 1)[1].split(".")
        statement = base64.urlsafe_b64decode(statement_b64 + "=" * (-len(statement_b64) % 4))
        assert json.loads(statement) == {"act": "act_1", "agent": agent_id, "role": "builder",
                                         "run": "crun_1"}
        assert ak.verify(doc["keys"][0], statement, sig, purpose="pr-evidence")
        monkeypatch.setattr(ak, "signer_for_run", lambda c, r: None)
        with eng.begin() as c:
            assert ak.pr_evidence(c, "crun_1") == ""
    finally:
        _cleanup(eng, project)


def test_the_sign_tool_is_offered_only_to_a_keyed_agent(eng, project, vaulted):  # noqa: F811
    from boss_engine.services import mcp_gateway as gw

    try:
        with eng.begin() as c:
            _agent(c, project, "builder")
            _agent(c, project, "qa")
            ak.create_key(c, project, "builder", actor="op")
        gateway = gw.McpGateway(db=lambda: eng)
        keyed = gw.GatewayGrant(token_hash="h1", project_id=project, expires_at=time.time() + 60,
                                role_key="builder", run_id=None, gateway_id="g1")
        plain = gw.GatewayGrant(token_hash="h2", project_id=project, expires_at=time.time() + 60,
                                role_key="qa", run_id=None, gateway_id="g2")
        assert gateway._keyed_tools(keyed) == [gw.SIGN_TOOL]
        assert gateway._keyed_tools(plain) == []
        out = gateway.call_run_tool(keyed, "sign", {"payload": "release notes"})
        assert not out.get("isError"), out
        signed = json.loads(out["content"][0]["text"])
        with eng.begin() as c:
            doc = ak.jwks_for_agent(c, signed["agent_id"])
        assert ak.verify(doc["keys"][0], "release notes", signed["signature"], purpose="output")
        refused = gateway.call_run_tool(plain, "sign", {"payload": "x"})
        assert refused.get("isError")
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": project})
        _cleanup(eng, project)

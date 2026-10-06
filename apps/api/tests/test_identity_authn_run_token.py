"""Stream S1: run tokens authenticate an AGENT acting on behalf of a person — never the person —
and only with their DPoP proof (IDENTITY.md §7.5; S3 verifies, S1 routes the credential).

S3's verifier is faked here by its frozen contract: it raises AuthnError without a valid proof
and returns verified claims otherwise. The test pins what S1 owns: which credential reaches the
verifier, with which htm/htu/audience, and the context it yields.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.tests.identity_s1_world import ORIGIN, fresh_client, make_user, org_id, world  # noqa: F401
from boss_engine.identity import authn, delegation
from boss_engine.identity.principal import (
    ActorChain,
    AgentInstance,
    AuthMethod,
    RunMode,
    RunTokenClaims,
    TaskBinding,
)

TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6ImJvc3MtcnVuK2p3dCJ9.eyJzdWIiOiJ4In0.c2ln"


def _install_verifier(monkeypatch, engine, person: str, agent: str, calls: list):
    with engine.begin() as db:
        org = org_id(db)

    def verify_run_token(token, *, audience, dpop_proof, htm, htu, db):
        calls.append({"token": token, "audience": audience, "dpop": dpop_proof, "htm": htm, "htu": htu})
        if not dpop_proof:
            raise authn.AuthnError("invalid_dpop")
        return RunTokenClaims(
            iss="boss", aud=audience, sub=person, org_id=org, mode=RunMode.OBO,
            chain=ActorChain((AgentInstance(agent, "run_1"),)), grant_id="dlg_1",
            task=TaskBinding(run_id="run_1", project_id=None), scope=frozenset({"code_task:read"}),
            cnf_jkt="jkt", jti="rt_1", parent_jti=None, iat=0, exp=2 ** 31)

    monkeypatch.setattr(delegation, "verify_run_token", verify_run_token)


def _agent(engine) -> str:
    with engine.begin() as db:
        db.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) VALUES "
                        "('agt_forge_s1', :o, 'agent', 'active', 'Forge') ON CONFLICT (id) DO NOTHING"),
                   {"o": org_id(db)})
    return "agt_forge_s1"


def test_a_run_token_is_the_agent_on_behalf_of_the_person(world, engine, monkeypatch):
    person = make_user(engine, "alice@example.test", role="operator")
    agent = _agent(engine)
    calls: list = []
    _install_verifier(monkeypatch, engine, person, agent, calls)
    try:
        with engine.begin() as db:
            ctx = authn.authenticate(authn.RequestCredentials(
                "POST", "/v1/code-tasks", {"authorization": f"DPoP {TOKEN}", "dpop": "proof.jwt.x"}, {}), db=db)
        assert ctx.method == AuthMethod.RUN_TOKEN
        assert ctx.principal.id == agent and ctx.principal.id != person      # never the person directly
        assert ctx.on_behalf_of == person and ctx.chain.agent_ids == (agent,)
        assert ctx.session is None and ctx.credential is None
        assert calls[-1] == {"token": TOKEN, "audience": delegation.AUD_API, "dpop": "proof.jwt.x",
                             "htm": "POST", "htu": f"{ORIGIN}/v1/code-tasks"}
        me = fresh_client().get("/v1/identity/me", headers={"Authorization": f"DPoP {TOKEN}", "DPoP": "p.r.f"})
        assert me.status_code == 200 and me.json()["principal_id"] == agent and me.json()["kind"] == "agent"
    finally:
        with engine.begin() as db:
            db.execute(text("DELETE FROM identity_principals WHERE id = 'agt_forge_s1'"))


def test_a_run_token_without_its_dpop_proof_is_refused(world, engine, monkeypatch):
    person = make_user(engine, "bob@example.test", role="operator")
    agent = _agent(engine)
    calls: list = []
    _install_verifier(monkeypatch, engine, person, agent, calls)
    try:
        res = fresh_client().get("/v1/identity/me", headers={"Authorization": f"Bearer {TOKEN}"})
        assert res.status_code == 401
        assert calls and calls[-1]["dpop"] is None
        # a disabled person stops every run acting for them, at the next call
        with engine.begin() as db:
            db.execute(text("UPDATE identity_principals SET status = 'disabled' WHERE id = :p"), {"p": person})
        res = fresh_client().get("/v1/identity/me", headers={"Authorization": f"DPoP {TOKEN}", "DPoP": "p.r.f"})
        assert res.status_code == 401
    finally:
        with engine.begin() as db:
            db.execute(text("DELETE FROM identity_principals WHERE id = 'agt_forge_s1'"))

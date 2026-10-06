"""S3 · The API the console (S4) builds against: cascade preview by dry-run revoke, extend and narrow,
409 consent_required, the Passport PATCH with external_ceiling, token claims, and connecting GitHub.

Shapes follow apps/web/lib/identity/types.ts and stub/* on id/s4-console. The guard (enforcement
point) is not mounted here: each test drives the handler as a given principal, so what is tested is
the handler's own rules — who may, what changes, what never leaks.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from boss_engine.identity import consent, grants, jwks, lineage, passports, tokens
from boss_engine.identity.scope import ResolvedScope

from ._identity_agents_support import BUILDER, add_person, build_env, install_github, session_ctx, spawn, start, verify


@pytest.fixture()
def env(monkeypatch):
    return build_env(monkeypatch)


@pytest.fixture()
def api(env, monkeypatch):
    from apps.api.app.routers import identity_agents

    monkeypatch.setattr(identity_agents, "_engine", lambda: env.engine)
    contexts = {}

    def ctx_for(request):
        who = request.headers.get("x-test-as", env.alice)
        return contexts.get(who) or session_ctx(who)

    monkeypatch.setattr(identity_agents, "_ctx", ctx_for)
    app = FastAPI()
    app.include_router(identity_agents.build_identity_agents_router())
    identity_agents.install_consent_handler(app)

    @app.post("/test/start-build")
    def start_build():
        with env.db() as c, lineage.acting(session_ctx(env.alice)):
            acting = consent.require_authority(org_id="org_boss", project_id=env.project, role_keys=["forge"],
                                               permissions=consent.CODE_TASK_PERMISSIONS, repos=["acme/web"],
                                               task="Interview Scorecard", run_id="run_ct:build1", db=c)
        return {"grant_id": acting.grant_id}

    client = TestClient(app, follow_redirects=False)
    client.contexts = contexts
    return client


def _seed_roles(env):
    with env.db() as c:
        c.execute(text("INSERT INTO roles (id, role_key, agent_id) VALUES ('r-forge','forge',:a) ON CONFLICT DO NOTHING"),
                  {"a": env.forge})


# ---- cascade preview via dry-run revoke --------------------------------------------------------------

def test_revoke_with_dry_run_answers_the_cascade_preview_and_changes_nothing(env, api):
    g = env.grant()
    shim = start(env, g.id)
    preview = api.post(f"/v1/identity/delegations/{g.id}/revoke", json={"dry_run": True}).json()
    assert preview["halts"] == 1 and preview["items"][0]["kind"] == "runs"
    assert {i["kind"] for i in preview["items"]} >= {"runs", "tokens"} and preview["report"]["dry_run"] is True
    verify(env, shim)                                                       # still alive
    mine = api.post(f"/v1/identity/me/delegations/{g.id}/revoke", json={"dry_run": True})
    assert mine.status_code == 200 and mine.json()["halts"] == 1
    with env.db() as c:
        add_person(c, env.bob)
    assert api.post(f"/v1/identity/me/delegations/{g.id}/revoke", json={"dry_run": True},
                    headers={"x-test-as": env.bob}).status_code == 404     # not Bob's: invisible
    done = api.post(f"/v1/identity/delegations/{g.id}/revoke", json={}).json()
    assert done["report"]["dry_run"] is False
    with pytest.raises(tokens.TokenError):
        verify(env, shim)
    again = api.post(f"/v1/identity/delegations/{g.id}/revoke", json={"dry_run": True})
    assert again.status_code == 409 and again.json()["error"] == "revoked_is_terminal"


def test_delegations_come_back_in_the_consoles_shape(env, api):
    g = env.grant(repos=None)
    d = api.get("/v1/identity/me/delegations").json()["delegations"][0]
    assert d["id"] == g.id and d["kind"] == "standing" and d["status"] == "active"
    assert d["user"]["id"] == env.alice and isinstance(d["user"]["hue"], int)
    assert d["agents"] == [{"id": env.atlas, "name": "Atlas"}] and d["subdelegation"]["allowed"] is True


# ---- extend and narrow -------------------------------------------------------------------------------

def test_extend_is_the_grantors_and_re_checks_their_live_access(env, api):
    g = env.grant()
    task = env.grant(task_run_id="run_ct:x")
    out = api.post(f"/v1/identity/delegations/{g.id}/extend", json={"days": 60})
    assert out.status_code == 200 and out.json()["expires_at"] > g.expires_at.isoformat()
    with env.db() as c:
        add_person(c, env.bob)
    env.world.grant(env.bob, {"delegation:grant"})
    assert api.post(f"/v1/identity/delegations/{g.id}/extend", json={"days": 10},
                    headers={"x-test-as": env.bob}).status_code == 403
    env.world.revoke(env.alice, {"github:sync"}, env.project)              # demoted: no re-authorizing
    stale = api.post(f"/v1/identity/delegations/{g.id}/extend", json={"days": 10})
    assert stale.status_code == 403 and stale.json()["error"] == "exceeds_your_access"
    assert api.post(f"/v1/identity/delegations/{task.id}/extend", json={"days": 10}).status_code == 409


def test_narrow_is_never_wider_and_live_tokens_narrow_at_their_next_call(env, api):
    g = env.grant()
    claims = verify(env, start(env, g.id))
    scope = ResolvedScope.in_project("org_boss", env.project)
    wider = api.post(f"/v1/identity/delegations/{g.id}/narrow", json={"permissions": [*BUILDER, "code_task:retry"]})
    assert wider.status_code == 422 and wider.json() == {"error": "narrowing_only", "permission": "code_task:retry"}
    smaller = [p for p in BUILDER if p != "preview:manage"]
    out = api.post(f"/v1/identity/delegations/{g.id}/narrow", json={"permissions": smaller})
    assert out.status_code == 200 and "preview:manage" not in out.json()["permissions"]
    with env.db() as c:
        d = tokens.decide(claims, "preview:manage", scope, db=c)
    assert not d.allowed and f"grant:{g.id}" in d.narrowed_by
    with env.db() as c:
        add_person(c, env.bob)
    assert api.post(f"/v1/identity/delegations/{g.id}/narrow", json={"permissions": ["code_task:read"]},
                    headers={"x-test-as": env.bob}).status_code == 403
    assert "delegation.narrowed" in env.world.types()


# ---- 409 consent_required ----------------------------------------------------------------------------

def test_starting_work_without_a_covering_grant_answers_409_consent_required(env, api):
    _seed_roles(env)
    r = api.post("/test/start-build")
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "consent_required" and body["task"] == "Interview Scorecard"
    assert body["agents"] == [{"id": env.forge, "name": "Forge", "role_key": "forge"}]
    assert set(body["permissions"]) == set(consent.CODE_TASK_PERMISSIONS)
    assert body["projects"] == [env.project] and body["github"]["repo"] == "acme/web"
    assert [d["kind"] for d in body["durations"]] == ["task", "standing"] and body["max_days"] == 90
    assert body["task_run_id"].startswith("pending:")


def test_a_for_this_build_only_grant_covers_exactly_one_run(env, api, monkeypatch):
    _seed_roles(env)
    gh = install_github(monkeypatch, env)
    asked = api.post("/test/start-build").json()
    made = api.post("/v1/identity/me/delegations", json={
        "agents": [a["id"] for a in asked["agents"]], "permissions": asked["permissions"], "projects": asked["projects"],
        "kind": "task", "days": None, "purpose": asked["task"],
        "github": {"repos": asked["github"]["repos"], **asked["github"]["permissions"]}})
    assert made.status_code == 201, made.text
    assert made.json()["kind"] == "task" and made.json()["task_run_id"].startswith("pending:")
    first = api.post("/test/start-build")
    assert first.status_code == 200 and first.json()["grant_id"] == made.json()["id"]
    with env.db() as c:
        assert grants.load(made.json()["id"], db=c).task_run_id == "run_ct:build1"      # claimed
    # The same build retried still runs under it; a different run would need consent again.
    assert api.post("/test/start-build").json()["grant_id"] == made.json()["id"]
    with env.db() as c, lineage.acting(session_ctx(env.alice)):
        with pytest.raises(consent.ConsentRequired):
            consent.require_authority(org_id="org_boss", project_id=env.project, role_keys=["forge"],
                                      permissions=consent.CODE_TASK_PERMISSIONS, repos=["acme/web"],
                                      task="another", run_id="run_ct:build2", db=c)
    assert gh is not None


def test_system_work_and_agents_are_never_asked_for_consent(env):
    _seed_roles(env)
    with env.db() as c:
        assert consent.require_authority(org_id="org_boss", project_id=env.project, role_keys=["forge"],
                                         permissions=consent.CODE_TASK_PERMISSIONS, task="x", run_id="run_ct:1",
                                         db=c) is None                      # no human session in context


# ---- the Passport PATCH -------------------------------------------------------------------------------

def test_patch_passport_takes_external_ceiling_and_validates_it(env, api):
    env.world.grant(env.alice, {"agent_passport:manage"})
    ok = api.patch(f"/v1/identity/agents/{env.forge}/passport",
                   json={"external_ceiling": {"github": {"contents": "read", "pull_requests": "write",
                                                         "push_refs": "boss/*"}}})
    assert ok.status_code == 200, ok.text
    p = ok.json()
    assert p["external_ceiling"] == {"github": {"contents": "read", "pull_requests": "write", "push_refs": "boss/*"}}
    assert p["sponsor"]["id"] == env.alice and p["id"] == env.forge
    changed = [e for e in env.world.events if e.event_type == "agent.passport.ceiling_changed"][-1]
    assert changed.details["external_before"]["github"]["contents"] == "write"
    for bad in ({"github": {"admin": "write"}}, {"gitlab": {"contents": "read"}}, {"github": {"contents": "owner"}},
                {"github": {"push_refs": "main"}}):
        r = api.patch(f"/v1/identity/agents/{env.forge}/passport", json={"external_ceiling": bad})
        assert r.status_code == 422, bad
    assert api.patch(f"/v1/identity/agents/{env.forge}/passport", json={"status": "active"}).status_code == 422
    # Raising a ceiling above what the editor holds is refused (no escalation by proxy).
    over = api.patch(f"/v1/identity/agents/{env.forge}/passport",
                     json={"ceiling": [*passports_ceiling(env), "workflow:create"]})
    assert over.status_code == 403 and over.json().get("permission") == "workflow:create"
    # An agent's own token can never change a Passport.
    api.contexts["agent"] = _agent_context(env)
    assert api.patch(f"/v1/identity/agents/{env.forge}/passport", json={"external_ceiling": {"github": {}}},
                     headers={"x-test-as": "agent"}).status_code == 403


def passports_ceiling(env):
    with env.db() as c:
        return sorted(passports.get(env.forge, db=c).ceiling)


def _agent_context(env):
    shim = start(env, env.grant().id, run="run_ct:agentctx")
    with env.db() as c:
        return tokens.context_for(verify(env, shim), db=c)


# ---- token claims -----------------------------------------------------------------------------------

def test_token_claims_show_the_chain_and_never_a_token_or_key(env, api):
    g = env.grant()
    atlas = start(env, g.id)
    forge = spawn(env, atlas, env.forge, "forge")
    r = api.get(f"/v1/identity/delegations/{g.id}/claims")
    assert r.status_code == 200
    body = r.json()
    claims = body["claims"]
    assert claims["sub"] == env.alice and claims["boss_grant"] == g.id and claims["typ"] == "boss-run+jwt"
    assert claims["act"]["sub"].startswith(env.forge) and claims["act"]["act"]["sub"].startswith(env.atlas)
    assert claims["cnf"] == {"jkt": "[redacted]"} and body["scope_source"] == "live_intersection"
    dump = json.dumps(body)
    for secret in (atlas.tokens["boss:api"].access_token, forge.tokens["boss:api"].access_token, atlas.key.jkt,
                   forge.key.jkt):
        assert secret not in dump
    for tok in (atlas.tokens["boss:api"].access_token,):
        assert jwks.split(tok).signature.hex() not in dump
    with env.db() as c:
        add_person(c, env.bob)
    assert api.get(f"/v1/identity/delegations/{g.id}/claims", headers={"x-test-as": env.bob}).status_code == 404


# ---- connecting GitHub --------------------------------------------------------------------------------

def test_connecting_github_redirects_and_the_callback_binds_the_same_person_once(env, api, monkeypatch):
    gh = install_github(monkeypatch, env)
    from boss_engine.identity.github_app import Response

    def fake(method, url, headers, body):
        if "login/oauth/access_token" in url:
            gh.calls.append((method, url, dict(headers), body or b""))
            return Response(200, b'{"access_token":"gho_LINKED","refresh_token":"ghr_LINKED","expires_in":28800}')
        if url.endswith("/user"):
            return Response(200, b'{"login":"alice-gh"}')
        return gh(method, url, headers, body)

    from boss_engine.identity import github_app

    monkeypatch.setattr(github_app, "TRANSPORT", fake)
    r = api.post("/v1/identity/me/connections", json={"provider": "github"})
    assert r.status_code == 200
    url = urlsplit(r.json()["redirect"])
    q = parse_qs(url.query)
    assert url.netloc == "github.com" and q["client_id"] == ["Iv1.test"] and q["state"][0]
    assert "client-secret-test" not in r.text
    state = q["state"][0]
    with env.db() as c:
        add_person(c, env.bob)
    stolen = api.get(f"/v1/identity/me/connections/github/callback?code=c1&state={state}", headers={"x-test-as": env.bob})
    assert stolen.status_code == 303 and "connect_error=bad_state" in stolen.headers["location"]
    done = api.get(f"/v1/identity/me/connections/github/callback?code=c1&state={state}")
    assert done.status_code == 303 and done.headers["location"] == "/account/agents?connected=github"
    assert "gho_" not in done.headers["location"]
    replay = api.get(f"/v1/identity/me/connections/github/callback?code=c1&state={state}")
    assert "connect_error=state_replayed" in replay.headers["location"]
    conns = api.get("/v1/identity/me/connections").json()["connections"]
    assert conns[0]["provider"] == "GitHub" and conns[0]["account"] == "alice-gh" and "gho_LINKED" not in json.dumps(conns)
    assert api.post("/v1/identity/me/connections", json={"provider": "myspace"}).status_code == 404

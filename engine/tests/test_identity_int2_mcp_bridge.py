"""Integration round 2 (d): an agent run can use the API-surface tools. The MCP bridge presents a
`boss:api` run token of the SAME run with a fresh DPoP proof (the run's own shim, or a sibling the
bridge mints for a first hop) — never the server's key, and never a re-minted deeper chain."""
from __future__ import annotations

import pytest

from boss_engine.identity import runs, tokens
from boss_engine.identity.delegation import AUD_API

from ._identity_agents_support import BASE, build_env, spawn, start


@pytest.fixture()
def env(monkeypatch):
    e = build_env(monkeypatch)
    from boss_engine.identity import authn_support
    monkeypatch.setattr(authn_support, "public_origin", lambda db, org=None: BASE)
    return e


def _ctx(env, shim):
    url = BASE + "/v1/code-tasks"
    with env.db() as c:
        claims = tokens.verify_run_token(shim.tokens[AUD_API].access_token, audience=AUD_API,
                                         dpop_proof=shim.key.proof("GET", url, access_token=shim.tokens[AUD_API].access_token),
                                         htm="GET", htu=url, db=c)
        return tokens.context_for(claims, db=c)


def _accepted(env, headers, method, path):
    tok = headers["Authorization"].split(" ", 1)[1]
    with env.db() as c:
        return tokens.verify_run_token(tok, audience=AUD_API, dpop_proof=headers["DPoP"],
                                       htm=method, htu=BASE + path, db=c)


def test_the_bridge_presents_the_runs_own_token_or_a_first_hop_sibling(env):
    shim = start(env, env.grant().id, run="run_ct:bridge1")
    ctx = _ctx(env, shim)
    headers = runs.bridge_headers(ctx, "POST", "/v1/tasks", engine=env.engine, request_origin="http://ignored")
    claims = _accepted(env, headers, "POST", "/v1/tasks")
    assert claims.jti == ctx.run_token.jti                          # the run's own token
    # the same run seen from another process (no shim here): a sibling bound to the bridge's key
    with runs._lock:
        runs._SHIMS.pop("run_ct:bridge1", None)
    headers = runs.bridge_headers(ctx, "GET", "/v1/tasks", engine=env.engine, request_origin="http://ignored")
    sibling = _accepted(env, headers, "GET", "/v1/tasks")
    assert sibling.jti != ctx.run_token.jti
    assert (sibling.sub, sibling.grant_id, sibling.task.run_id) == (ctx.run_token.sub, ctx.run_token.grant_id,
                                                                     ctx.run_token.task.run_id)
    assert sibling.scope <= ctx.run_token.scope


def test_a_deeper_chain_is_never_re_minted_as_a_first_hop(env):
    parent = start(env, env.grant().id, run="run_ct:bridge2")
    child = spawn(env, parent, env.forge, "forge-1")
    ctx = _ctx(env, child)
    with runs._lock:
        runs._SHIMS.pop(child.run_id, None)
    assert runs.bridge_headers(ctx, "GET", "/v1/tasks", engine=env.engine, request_origin=BASE) is None


def test_a_person_is_not_bridged(env):
    from ._identity_agents_support import session_ctx
    assert runs.bridge_headers(session_ctx(env.alice), "GET", "/v1/tasks", engine=env.engine,
                               request_origin=BASE) is None

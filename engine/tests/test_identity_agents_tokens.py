"""S3 · Tokens: run tokens, DPoP, the RFC 8693 exchange and the JWT BCP (IDENTITY.md §7.4–§7.7, §15.3).

A39, A41–A49, A13b, the Keycloak regressions (#52691, #53029, #53040) and the token-forging set.
Each test fails if the protection it names is removed. DB-backed: BOSS_IDENTITY_TEST_DATABASE_URL.
"""
from __future__ import annotations

import time
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text

from boss_engine.identity import delegation, dpop, jwks, passports, signing_keys, tokens
from boss_engine.identity.delegation import (
    AUD_API,
    AUD_EGRESS,
    AUD_MCP,
    TOKEN_TYPE_ACTOR,
    TOKEN_TYPE_RUN,
    TokenExchangeRequest,
)

from ._identity_agents_support import BASE, BUILDER, build_env, session_ctx, spawn, start, verify


@pytest.fixture()
def env(monkeypatch):
    return build_env(monkeypatch)


def _reason(excinfo) -> str:
    return getattr(excinfo.value, "reason", "")


# ---- first hop and verification ------------------------------------------------------------------------

def test_a_run_token_carries_the_person_as_sub_and_the_agent_in_act(env):
    g = env.grant()
    shim = start(env, g.id)
    claims = verify(env, shim)
    assert claims.sub == env.alice and claims.mode.value == "obo" and claims.grant_id == g.id
    assert claims.chain.to_boss_chain() == [f"{env.atlas}#run_ct:task1"]
    assert claims.aud == AUD_API and claims.exp - claims.iat <= 300
    assert claims.scope == frozenset(BUILDER)
    raw = jwks.split(shim.tokens[AUD_API].access_token).claims
    assert raw["act"] == {"sub": f"{env.atlas}#run_ct:task1"}
    assert "email" not in raw and "name" not in raw          # no PII in tokens


def test_a41_no_dpop_proof_and_a_proof_by_another_key_are_401(env):
    shim = start(env, env.grant().id)
    tok = shim.tokens[AUD_API].access_token
    with env.db() as c, pytest.raises(tokens.TokenError) as e:
        tokens.verify_run_token(tok, audience=AUD_API, dpop_proof=None, htm="GET", htu=BASE + "/x", db=c)
    assert e.value.status == 401 and _reason(e) == "dpop_missing"
    thief = dpop.DPoPKey.generate()                           # replay without the shim's key
    with pytest.raises(tokens.TokenError) as e:
        verify(env, shim, proof=thief.proof("GET", BASE + "/v1/code-tasks", access_token=tok))
    assert e.value.status == 401 and _reason(e) == "dpop_key_mismatch"


def test_a42_a_replayed_proof_and_a_wrong_htu_or_htm_are_401(env):
    shim = start(env, env.grant().id)
    tok = shim.tokens[AUD_API].access_token
    proof = shim.key.proof("GET", BASE + "/v1/code-tasks", access_token=tok)
    verify(env, shim, proof=proof)
    with pytest.raises(tokens.TokenError) as e:
        verify(env, shim, proof=proof)
    assert _reason(e) == "dpop_replay"
    with pytest.raises(tokens.TokenError) as e:
        verify(env, shim, proof=shim.key.proof("GET", BASE + "/v1/other", access_token=tok))
    assert _reason(e) == "dpop_htu"
    with pytest.raises(tokens.TokenError) as e:
        verify(env, shim, proof=shim.key.proof("POST", BASE + "/v1/code-tasks", access_token=tok))
    assert _reason(e) == "dpop_htm"
    with pytest.raises(tokens.TokenError) as e:                # a proof for another token (ath)
        verify(env, shim, proof=shim.key.proof("GET", BASE + "/v1/code-tasks", access_token="x.y.z"))
    assert _reason(e) == "dpop_ath"
    with pytest.raises(tokens.TokenError) as e:                # a stale proof
        verify(env, shim, proof=shim.key.proof("GET", BASE + "/v1/code-tasks", access_token=tok,
                                               iat=int(time.time()) - 600))
    assert _reason(e) == "dpop_iat"


def test_a43_one_audience_only_and_no_token_family_confusion(env):
    shim = start(env, env.grant(repos=("acme/web",)).id, repos=("acme/web",), audiences=(AUD_API,))
    with pytest.raises(tokens.TokenError) as e:                 # the egress token at the API
        verify(env, shim, audience=AUD_API, token=shim.tokens[AUD_EGRESS].access_token)
    assert _reason(e) == "bad_aud"
    with pytest.raises(tokens.TokenError):                      # the API token at MCP
        verify(env, shim, audience=AUD_MCP, token=shim.tokens[AUD_API].access_token)
    # An actor assertion is never a run token, and a run token never an actor assertion.
    parent = verify(env, shim)
    with env.db() as c:
        assertion = tokens.mint_actor_assertion(parent=parent, child_agent_id=env.forge,
                                                child_run_id="run_ct:task1/forge", child_jkt="x", db=c)
    with pytest.raises(tokens.TokenError) as e:
        verify(env, shim, token=assertion)
    assert _reason(e) == "bad_typ"


# ---- the JWT BCP and forging ---------------------------------------------------------------------------

def _forge(header: dict, claims: dict, key=None, raw_sig: bytes = None) -> str:
    h, c = jwks.b64u(jwks.canonical(header)), jwks.b64u(jwks.canonical(claims))
    if raw_sig is not None:
        return f"{h}.{c}.{jwks.b64u(raw_sig)}"
    sig = (key or Ed25519PrivateKey.generate()).sign(f"{h}.{c}".encode())
    return f"{h}.{c}.{jwks.b64u(sig)}"


def _real_claims(env, shim) -> tuple[dict, dict]:
    parsed = jwks.split(shim.tokens[AUD_API].access_token)
    return dict(parsed.header), dict(parsed.claims)


def test_alg_none_is_rejected(env):
    shim = start(env, env.grant().id)
    header, claims = _real_claims(env, shim)
    forged = _forge({**header, "alg": "none"}, {**claims, "scope": "code_task:create"}, raw_sig=b"")
    with pytest.raises(tokens.TokenError) as e:
        verify(env, shim, token=forged)
    assert _reason(e) == "bad_alg"


def test_hs256_signed_with_the_public_key_is_rejected(env):
    import hashlib
    import hmac

    shim = start(env, env.grant().id)
    header, claims = _real_claims(env, shim)
    with env.db() as c:
        public = signing_keys.jwks_document(db=c)["keys"][0]["x"].encode()
    h = jwks.b64u(jwks.canonical({**header, "alg": "HS256"}))
    b = jwks.b64u(jwks.canonical(claims))
    sig = hmac.new(public, f"{h}.{b}".encode(), hashlib.sha256).digest()
    with pytest.raises(tokens.TokenError) as e:
        verify(env, shim, token=f"{h}.{b}.{jwks.b64u(sig)}")
    assert _reason(e) == "bad_alg"


def test_an_unknown_or_injected_kid_is_refused_without_any_fetch(env, monkeypatch):
    from boss_engine.identity import http

    def no_fetch(*a, **k):
        raise AssertionError("a kid must never cause a fetch")

    monkeypatch.setattr(http, "fetch", no_fetch)
    shim = start(env, env.grant().id)
    header, claims = _real_claims(env, shim)
    evil = Ed25519PrivateKey.generate()
    for kid in ("run-unknown", "../../../../dev/null", "x' OR '1'='1", header["kid"].replace("run-", "actor-")):
        forged = _forge({**header, "kid": kid}, claims, key=evil)
        with pytest.raises(tokens.TokenError) as e:
            verify(env, shim, token=forged)
        assert _reason(e) in ("unknown_kid", "bad_kid")


def test_jku_x5u_and_jwk_headers_are_ignored(env):
    shim = start(env, env.grant().id)
    header, claims = _real_claims(env, shim)
    evil = Ed25519PrivateKey.generate()
    forged = _forge({**header, "jku": "https://evil.example/jwks", "x5u": "https://evil.example/x",
                     "jwk": jwks.public_jwk(evil.public_key())}, {**claims, "scope": "code_task:create"}, key=evil)
    with pytest.raises(tokens.TokenError) as e:
        verify(env, shim, token=forged)
    assert _reason(e) == "bad_signature"         # verified with the LOCAL kid's key, not the header's


@pytest.mark.parametrize("field, value, reason", [("typ", "JWT", "bad_typ"), ("iss", "https://evil", "bad_iss")])
def test_a_wrong_typ_or_iss_is_rejected(env, field, value, reason):
    shim = start(env, env.grant().id)
    header, claims = _real_claims(env, shim)
    with env.db() as c:
        key = signing_keys.active_key("run", db=c)
    if field == "typ":
        header = {**header, "typ": value}
    else:
        claims = {**claims, "iss": value}
    forged = jwks.sign(header, claims, key.private_key)
    with pytest.raises(tokens.TokenError) as e:
        verify(env, shim, token=forged)
    assert _reason(e) == reason


def test_the_previous_key_verifies_during_the_overlap_and_fails_after(env):
    shim = start(env, env.grant().id)
    old_kid = jwks.split(shim.tokens[AUD_API].access_token).header["kid"]
    with env.db() as c:
        new_kid = signing_keys.rotate("run", org_id="org_boss", db=c)
        assert {k["kid"] for k in signing_keys.jwks_document(db=c)["keys"]} >= {old_kid, new_kid}
    verify(env, shim)                                          # inside the overlap
    with env.db() as c:
        c.execute(text("UPDATE identity_signing_keys SET retire_after = now() - interval '1 second' WHERE kid = :k"),
                  {"k": old_kid})
        signing_keys.rotate_if_due(org_id="org_boss", db=c)
        assert old_kid not in {k["kid"] for k in signing_keys.jwks_document(db=c)["keys"]}
    with pytest.raises(tokens.TokenError) as e:
        verify(env, shim)
    assert _reason(e) == "unknown_kid"


def test_no_signing_key_means_refusal_never_an_ephemeral_key(env):
    with env.db() as c:
        c.execute(text("UPDATE identity_signing_keys SET status = 'retired'"))
    signing_keys.clear_cache()
    with env.db() as c, pytest.raises(signing_keys.SigningKeysMissing):
        signing_keys.assert_ready(db=c)
    with pytest.raises(signing_keys.SigningKeysMissing):
        start(env, env.grant().id)


def test_a_tampered_scope_breaks_the_signature(env):
    shim = start(env, env.grant().id)
    tok = shim.tokens[AUD_API].access_token
    h, b, s = tok.split(".")
    claims = jwks.split(tok).claims
    claims["scope"] += " decision:approve"
    forged = f"{h}.{jwks.b64u(jwks.canonical(claims))}.{s}"
    with pytest.raises(tokens.TokenError) as e:
        verify(env, shim, token=forged)
    assert _reason(e) == "bad_signature"


def test_an_expired_token_is_refused(env):
    shim = start(env, env.grant().id)
    with env.db() as c:
        claims = verify(env, shim)
        key = signing_keys.active_key("run", db=c)
    header, raw = _real_claims(env, shim)
    raw.update(iat=int(time.time()) - 400, exp=int(time.time()) - 100)
    with pytest.raises(tokens.TokenError) as e:
        verify(env, shim, token=jwks.sign(header, raw, key.private_key))
    assert _reason(e) == "expired"
    del claims


# ---- the exchange -----------------------------------------------------------------------------------

def _exchange(env, parent, *, child_agent, child_run=None, scope=BUILDER, assertion=None, subject_type=TOKEN_TYPE_RUN,
              audience=AUD_API, proof_key=None, extra_jkt=None):
    tok = parent.tokens[AUD_API].access_token
    htu = BASE + delegation.TOKEN_ENDPOINT
    child_key = dpop.DPoPKey.generate()
    if assertion is None:
        parent_claims = verify(env, parent)
        with env.db() as c:
            assertion = tokens.mint_actor_assertion(parent=parent_claims, child_agent_id=child_agent,
                                                    child_run_id=child_run or f"{parent.run_id}/c",
                                                    child_jkt=extra_jkt or child_key.jkt, db=c)
    req = TokenExchangeRequest(subject_token=tok, subject_token_type=subject_type, actor_token=assertion,
                               actor_token_type=TOKEN_TYPE_ACTOR, audience=audience, scope=frozenset(scope),
                               dpop_proof=(proof_key or parent.key).proof("POST", htu, access_token=tok))
    with env.db() as c:
        return tokens.exchange_token(req, htm="POST", htu=htu, db=c)


def test_keycloak_53040_a_multi_hop_exchange_preserves_the_nested_chain(env):
    g = env.grant()
    atlas = start(env, g.id)
    forge = spawn(env, atlas, env.forge, "forge")
    vera = spawn(env, forge, env.vera, "vera", scope=("code_task:read", "code_task:steer"))
    raw = jwks.split(vera.tokens[AUD_API].access_token).claims
    assert raw["sub"] == env.alice
    assert raw["act"] == {"sub": f"{env.vera}#run_ct:task1/forge/vera",
                          "act": {"sub": f"{env.forge}#run_ct:task1/forge",
                                  "act": {"sub": f"{env.atlas}#run_ct:task1"}}}
    assert raw["boss_chain"] == [f"{env.atlas}#run_ct:task1", f"{env.forge}#run_ct:task1/forge",
                                 f"{env.vera}#run_ct:task1/forge/vera"]
    assert raw["boss_depth"] == 3 and raw["boss_parent_jti"] == forge.tokens[AUD_API].jti


def test_a44_a_client_supplied_act_or_chain_cannot_enter_a_token(env):
    """The exchange has no field for them; the chain comes only from the verified subject token.
    Even a hand-built assertion naming a different parent run is refused."""
    assert "act" not in TokenExchangeRequest.__dataclass_fields__
    assert "boss_chain" not in TokenExchangeRequest.__dataclass_fields__
    atlas = start(env, env.grant().id)
    other = start(env, env.grant().id, run="run_ct:other")
    parent_claims = verify(env, other)
    with env.db() as c:
        foreign = tokens.mint_actor_assertion(parent=parent_claims, child_agent_id=env.forge,
                                              child_run_id="run_ct:other/forge", child_jkt="k", db=c)
    with pytest.raises(tokens.TokenError) as e:
        _exchange(env, atlas, child_agent=env.forge, assertion=foreign)
    assert _reason(e) == "not_a_child_of_subject"


def test_keycloak_52691_an_alternate_subject_token_type_cannot_skip_actor_validation(env):
    atlas = start(env, env.grant().id)
    for bad in ("urn:ietf:params:oauth:token-type:access_token", "urn:ietf:params:oauth:token-type:jwt", ""):
        with pytest.raises(tokens.TokenError) as e:
            _exchange(env, atlas, child_agent=env.forge, subject_type=bad)
        assert e.value.code == "invalid_request"
    with pytest.raises(tokens.TokenError):                      # no actor token at all
        _exchange(env, atlas, child_agent=env.forge, assertion="")


def test_a39_a_child_outside_allowed_children_is_denied(env):
    atlas = start(env, env.grant(children=("agt_forge000001", "agt_vera0000001", "agt_rook0000001")).id)
    with pytest.raises(tokens.TokenError) as e:                 # Atlas may spawn forge, vera; not rook
        _exchange(env, atlas, child_agent=env.rook)
    assert _reason(e) == "child_not_allowed"
    atlas2 = start(env, env.grant(children=("agt_forge000001",)).id, run="run_ct:t2")
    with pytest.raises(tokens.TokenError) as e:                 # the grant's own child allow-list
        _exchange(env, atlas2, child_agent=env.vera)
    assert _reason(e) == "grant_forbids_subdelegation"
    atlas3 = start(env, env.grant(subdelegation=False, children=()).id, run="run_ct:t3")
    with pytest.raises(tokens.TokenError) as e:
        _exchange(env, atlas3, child_agent=env.forge)
    assert _reason(e) == "grant_forbids_subdelegation"


def test_a45_depth_is_the_minimum_along_the_chain_and_a_child_cannot_raise_it(env):
    env.world.policy_overrides["org_max_depth"] = 3
    with env.db() as c:                                          # Vera's own cap is the hard max
        c.execute(text("UPDATE identity_agent_passports SET max_depth = 5 WHERE agent_id = :a"), {"a": env.vera})
    g = env.grant(children=("agt_forge000001", "agt_vera0000001", "agt_rook0000001"), max_depth=3)
    atlas = start(env, g.id)
    forge = spawn(env, atlas, env.forge, "forge")
    vera = spawn(env, forge, env.vera, "vera", scope=("code_task:read",))
    with pytest.raises(tokens.TokenError) as e:                 # depth 4 > org 3, whatever Vera says
        _exchange(env, vera, child_agent=env.rook, scope=("code_task:read",))
    assert _reason(e) == "depth"
    with env.db() as c:                                          # Forge's own cap of 1 binds its children
        c.execute(text("UPDATE identity_agent_passports SET max_depth = 1 WHERE agent_id = :a"), {"a": env.forge})
    with pytest.raises(tokens.TokenError) as e:
        _exchange(env, forge, child_agent=env.vera, scope=("code_task:read",))
    assert _reason(e) in ("depth", "chain_inactive")


def test_a46_the_same_agent_twice_is_a_cycle(env):
    g = env.grant(children=("agt_forge000001", "agt_vera0000001", "agt_atlas000001"))
    atlas = start(env, g.id)
    vera = spawn(env, atlas, env.vera, "vera", scope=("code_task:read",))
    with pytest.raises(tokens.TokenError) as e:                 # Vera may spawn atlas; atlas is already in
        _exchange(env, vera, child_agent=env.atlas, scope=("code_task:read",))
    assert _reason(e) == "cycle"


def test_a47_a_wider_request_is_narrowed_never_widened_at_any_hop(env):
    atlas = start(env, env.grant().id, scope=("code_task:read", "code_task:steer", "github:sync"))
    issued = _exchange(env, atlas, child_agent=env.forge, scope=BUILDER + ("code_task:retry",))
    assert issued.scope == frozenset({"code_task:read", "code_task:steer", "github:sync"})
    forge = spawn(env, atlas, env.forge, "forge2", scope=("code_task:read", "github:sync"))
    vera = spawn(env, forge, env.vera, "vera", scope=BUILDER)    # Vera's ceiling has no github:sync
    assert vera.tokens[AUD_API].scope == frozenset({"code_task:read"})


def test_a48_an_empty_intersection_is_refused_never_the_requested_scope(env):
    atlas = start(env, env.grant().id, scope=("code_task:read",))
    with pytest.raises(tokens.TokenError) as e:
        _exchange(env, atlas, child_agent=env.forge, scope=("preview:manage",))
    assert e.value.code == "invalid_scope" and _reason(e) == "empty_scope"


def test_a49_the_token_service_never_mints_a_non_delegable_permission(env):
    g = env.grant()
    for key in ("decision:approve", "role_binding:assign", "delegation:grant", "deployment:production",
                "host:exec", "secret:manage"):
        with pytest.raises(tokens.TokenError) as e:
            start(env, g.id, scope=("code_task:read", key), run=f"run_ct:{key.replace(':', '-')}")
        assert e.value.code == "invalid_scope" and _reason(e).startswith("not_delegable")
    atlas = start(env, g.id)
    with pytest.raises(tokens.TokenError) as e:
        _exchange(env, atlas, child_agent=env.forge, scope=("code_task:read", "decision:approve"))
    assert _reason(e).startswith("not_delegable")


def test_an_expired_parent_cannot_be_exchanged_and_a_child_never_outlives_it(env):
    atlas = start(env, env.grant().id)
    issued = _exchange(env, atlas, child_agent=env.forge)
    parent_exp = jwks.split(atlas.tokens[AUD_API].access_token).claims["exp"]
    assert jwks.split(issued.access_token).claims["exp"] <= parent_exp
    with env.db() as c:                                     # the parent's credential is gone
        c.execute(text("UPDATE identity_run_credentials SET revoked_at = now() WHERE jti = :j"),
                  {"j": atlas.tokens[AUD_API].jti})
    with pytest.raises(tokens.TokenError) as e:
        _exchange(env, atlas, child_agent=env.forge)
    assert _reason(e) == "jti_revoked"


def test_a_revoked_mid_chain_agent_stops_every_hop_below_it(env):
    atlas = start(env, env.grant().id)
    forge = spawn(env, atlas, env.forge, "forge")
    with env.db() as c:
        c.execute(text("UPDATE identity_principals SET status = 'revoked' WHERE id = :a"), {"a": env.forge})
    with pytest.raises(tokens.TokenError) as e:
        _exchange(env, forge, child_agent=env.vera, scope=("code_task:read",))
    assert _reason(e) in ("chain_inactive",)
    with env.db() as c:
        claims = verify(env, forge)
        decision = tokens.decide(claims, "code_task:read", None, db=c)
    assert not decision.allowed and decision.reason.value == "chain_inactive"


def test_a_reactivated_agent_does_not_revive_its_revoked_tokens(env):
    env.world.grant(env.alice, {"agent_passport:suspend", "agent_passport:manage", "agent_passport:revoke"})
    atlas = start(env, env.grant().id)
    forge = spawn(env, atlas, env.forge, "forge")
    with env.db() as c:
        passports.suspend(session_ctx(env.alice), env.forge, reason="investigate", db=c)
    with env.db() as c:
        passports.resume(session_ctx(env.alice), env.forge, reason="cleared", db=c)
    with pytest.raises(tokens.TokenError) as e:                 # the old token stays dead
        verify(env, forge)
    assert _reason(e) == "jti_revoked"
    with env.db() as c:                                          # and revoked is terminal
        passports.revoke(session_ctx(env.alice), env.forge, reason="gone", db=c)
    with env.db() as c, pytest.raises(passports.PassportError) as pe:
        passports.resume(session_ctx(env.alice), env.forge, reason="undo", db=c)
    assert pe.value.code == "revoked"
    with env.db() as c:
        c.execute(text("UPDATE identity_principals SET status = 'active' WHERE id = :a"), {"a": env.forge})
    # Even a row flipped back by hand gains nothing: its grants were revoked by the cascade.
    with pytest.raises(tokens.TokenError):
        spawn(env, start(env, env.grant(children=()).id, run="run_ct:after"), env.forge, "f")


def test_cross_org_delegation_is_refused(env):
    from boss_engine.identity import grants

    with env.db() as c:
        passports.ensure_passport(org_id="org_other", agent_id="agt_spy00000001", role_key="forge",
                                  display_name="Spy", sponsor_principal_id="usr_mallory", db=c)
    with pytest.raises(grants.GrantRefused) as e:               # a grant naming another org's agent
        env.grant(agents=("agt_spy00000001",))
    assert e.value.code == "unknown_agent"
    with pytest.raises(grants.GrantRefused) as e:               # ...or naming it as a child
        env.grant(children=("agt_spy00000001",))
    assert e.value.code == "unknown_agent"
    atlas = start(env, env.grant(children=("forge",)).id)       # a role-key allow-list the spy also matches
    with env.db() as c:                                          # a child in another org
        c.execute(text("UPDATE identity_agent_passports SET allowed_children = '{forge,agt_spy00000001}' "
                       "WHERE agent_id = :a"), {"a": env.atlas})
    with pytest.raises(tokens.TokenError) as e:
        _exchange(env, atlas, child_agent="agt_spy00000001")
    assert _reason(e) == "cross_org"


def test_a_replayed_actor_assertion_is_refused(env):
    atlas = start(env, env.grant().id)
    parent_claims = verify(env, atlas)
    child = dpop.DPoPKey.generate()
    with env.db() as c:
        assertion = tokens.mint_actor_assertion(parent=parent_claims, child_agent_id=env.forge,
                                                child_run_id="run_ct:task1/forge", child_jkt=child.jkt, db=c)
    _exchange(env, atlas, child_agent=env.forge, assertion=assertion)
    with pytest.raises(tokens.TokenError) as e:
        _exchange(env, atlas, child_agent=env.forge, assertion=assertion)
    assert _reason(e) == "actor_replayed"


def test_the_parent_cannot_use_the_child_token_it_obtained(env):
    """The child token is bound to the CHILD's key (from the assertion), not the exchanging parent's."""
    atlas = start(env, env.grant().id)
    issued = _exchange(env, atlas, child_agent=env.forge)
    url = BASE + "/v1/code-tasks"
    with env.db() as c, pytest.raises(tokens.TokenError) as e:
        tokens.verify_run_token(issued.access_token, audience=AUD_API, htm="GET", htu=url, db=c,
                                dpop_proof=atlas.key.proof("GET", url, access_token=issued.access_token))
    assert _reason(e) == "dpop_key_mismatch"


def test_a13b_an_exchange_flood_is_429(env):
    atlas = start(env, env.grant().id)
    tokens._local_hits.clear()
    for i in range(tokens.EXCHANGE_LIMIT_PER_MINUTE):
        tokens._local_hits.setdefault("run_ct:task1", __import__("collections").deque()).append(time.monotonic())
    with pytest.raises(tokens.TokenError) as e:
        _exchange(env, atlas, child_agent=env.forge)
    assert e.value.status == 429 and e.value.retry_after
    tokens._local_hits.clear()


# ---- the decision for run tokens ------------------------------------------------------------------------

def test_a49_a50_an_agent_token_is_refused_non_delegable_permissions_at_the_pdp(env):
    from boss_engine.identity import AuthContext, AuthMethod
    from boss_engine.identity.scope import ResolvedScope

    atlas = start(env, env.grant().id)
    claims = verify(env, atlas)
    scope = ResolvedScope.in_project("org_boss", env.project)
    for key in ("decision:approve", "role_binding:assign", "delegation:grant", "deployment:production"):
        with env.db() as c:
            d = tokens.decide(claims, key, scope, db=c)
        assert not d.allowed and d.reason.value == "not_delegable"
    # A50: approvals need a person in a human session; a run-token context never is one.
    with env.db() as c:
        ctx = tokens.context_for(claims, db=c)
    assert ctx.method == AuthMethod.RUN_TOKEN and not ctx.is_human_session and ctx.on_behalf_of == env.alice
    assert isinstance(ctx, AuthContext)


def test_effective_permission_is_live_person_intersection(env):
    from boss_engine.identity.scope import ResolvedScope

    atlas = start(env, env.grant().id)
    claims = verify(env, atlas)
    scope = ResolvedScope.in_project("org_boss", env.project)
    with env.db() as c:
        assert tokens.decide(claims, "github:sync", scope, db=c).allowed
    env.world.revoke(env.alice, {"github:sync"}, env.project)     # Alice demoted: her agents too, now
    with env.db() as c:
        d = tokens.decide(claims, "github:sync", scope, db=c)
    assert not d.allowed and "person" in d.narrowed_by
    with env.db() as c:                                          # another project is out of the task
        d = tokens.decide(claims, "code_task:read", ResolvedScope.in_project("org_boss", "prj_other"), db=c)
    assert not d.allowed and d.reason.value == "out_of_scope"

"""BOSSAPP-1291: an unattended run keeps its delegated access for longer than a token lives.

Run tokens live minutes (RUN_TOKEN_TTL_SECONDS); a build runs for hours. The run supervisor's
renewal loop (runs.renew_due -> runs.renew -> tokens.renew_run_token) swaps every token of a live
run before it expires. This test shortens the lifetime to 60 s and drives a wall clock and the
supervisor's monotonic clock by hand, so a run that lasts many lifetimes is proved in a second:

  * after every renewal the run's current tokens verify, its egress credential resolves to the
    new egress token, and the token it replaced is refused;
  * a token that is NOT renewed dies at its expiry (the lifetime really is short);
  * every renewal is audited: a `token.issued` lineage event naming the jti it renewed, so the
    walk from the first token to the current one never breaks; the ledger holds exactly one live
    token per audience;
  * once the run ends, renewal is refused and the supervisor no longer holds the run.
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import text

from boss_engine.identity import cascade, runs, tokens
from boss_engine.identity.delegation import AUD_API, AUD_EGRESS, CascadeTrigger

from ._identity_agents_support import BASE, build_env, install_github, session_ctx, start, verify

TTL = 60          # the shortened token lifetime
RENEW_AFTER = 40  # the supervisor renews at two thirds of it, as in production (240 of 300)
RENEWALS = 9      # 9 x 40 s = 6 lifetimes


@pytest.fixture()
def env(monkeypatch):
    return build_env(monkeypatch)


@pytest.fixture()
def clock(monkeypatch):
    """The issuer's wall clock (tokens._now), moved by hand. DPoP proofs keep real time."""
    now = [int(time.time())]
    monkeypatch.setattr(tokens, "_now", lambda: now[0])
    monkeypatch.setattr(tokens, "RUN_TOKEN_TTL_SECONDS", TTL)
    monkeypatch.setattr(runs, "RENEW_AFTER_SECONDS", RENEW_AFTER)
    monkeypatch.setattr(runs, "_SHIMS", {})          # only this test's runs are supervised
    return now


def _reason(fn) -> str:
    with pytest.raises(tokens.TokenError) as refused:
        fn()
    return refused.value.reason


def test_a_run_outlives_a_short_token_lifetime_through_audited_renewals(env, clock, monkeypatch):
    install_github(monkeypatch, env)
    g = env.grant(agents=(env.forge,), children=(), repos=("acme/web",))
    shim = start(env, g.id, agent=env.forge, run="run_ct:crun_hours", repos=("acme/web",))
    assert set(shim.tokens) == {AUD_API, AUD_EGRESS}
    first_token = shim.tokens[AUD_API].access_token
    started = clock[0]
    mono = shim.renewed_at

    history = {aud: [tok.jti] for aud, tok in shim.tokens.items()}
    for _ in range(RENEWALS):
        replaced = shim.tokens[AUD_API]
        clock[0] += RENEW_AFTER
        mono += RENEW_AFTER
        out = runs.renew_due(env.engine, now=mono)
        assert out == {"run_ct:crun_hours": "renewed"}, out
        for aud, tok in shim.tokens.items():
            assert tok.jti != history[aud][-1]
            history[aud].append(tok.jti)
        # The run keeps working on its new token ...
        assert verify(env, shim).task.run_id == "run_ct:crun_hours"
        assert verify(env, shim, audience=AUD_EGRESS).aud == AUD_EGRESS
        with env.db() as c:
            assert runs.resolve_egress(shim.egress_credential, db=c).jti == shim.tokens[AUD_EGRESS].jti
        # ... and the token it replaced is refused at once (single use, revoked on renewal).
        assert _reason(lambda: verify(env, shim, token=replaced.access_token)) == "jti_revoked"

    assert clock[0] - started >= 6 * TTL, "the run outlived several token lifetimes"
    assert _reason(lambda: verify(env, shim, token=first_token)) in ("expired", "jti_revoked")

    # Every renewal is audited, each naming the token it renewed: an unbroken chain.
    issued = [e for e in env.world.events if e.event_type == "token.issued"
              and e.target_id == "run_ct:crun_hours" and e.details.get("renewed_from")]
    assert len(issued) == RENEWALS * 2
    for aud, chain in history.items():
        by_jti = {e.token_jti: e.details["renewed_from"] for e in issued if e.details.get("aud") == aud}
        assert [by_jti[j] for j in chain[1:]] == chain[:-1], aud
    with env.db() as c:
        rows = c.execute(text("SELECT aud, count(*), count(*) FILTER (WHERE revoked_at IS NULL) "
                              "FROM identity_run_credentials WHERE run_id = 'run_ct:crun_hours' "
                              "GROUP BY aud")).all()
    assert {r[0]: (r[1], r[2]) for r in rows} == {AUD_API: (RENEWALS + 1, 1),
                                                   AUD_EGRESS: (RENEWALS + 1, 1)}

    # The run ends: renewal is refused, and the supervisor lets go of it.
    with env.db() as c:
        runs.end_run(shim.run_id, db=c, org_id="org_boss")
    clock[0] += RENEW_AFTER
    assert runs.renew_due(env.engine, now=mono + RENEW_AFTER) == {}
    with env.db() as c:
        assert _reason(lambda: runs.renew(shim, db=c, base_url=BASE)) == "jti_revoked"
    assert _reason(lambda: verify(env, shim)) == "jti_revoked"


def test_a_token_that_is_not_renewed_dies_at_its_short_lifetime(env, clock):
    shim = start(env, env.grant().id, run="run_ct:crun_idle")
    assert verify(env, shim).task.run_id == "run_ct:crun_idle"
    clock[0] += TTL + tokens.CLOCK_SKEW_SECONDS + 1
    assert _reason(lambda: verify(env, shim)) == "expired"
    # An expired token cannot be renewed either: a run that missed its renewal must be restarted
    # under live authority, never revived from a dead token.
    with env.db() as c:
        assert _reason(lambda: runs.renew(shim, db=c, base_url=BASE)) == "expired"


def test_a_grant_revoked_mid_run_stops_renewal_and_the_run_s_tokens(env, clock, monkeypatch):
    """BOSSAPP-1291 review: the person revokes the grant while the run is between renewals. The
    next renewal is refused, the supervisor lets go of the run, and its current tokens stop
    verifying at once (they do not linger until their expiry)."""
    install_github(monkeypatch, env)
    g = env.grant(agents=(env.forge,), children=(), repos=("acme/web",))
    shim = start(env, g.id, agent=env.forge, run="run_ct:crun_revoked", repos=("acme/web",))
    mono = shim.renewed_at
    clock[0] += RENEW_AFTER
    mono += RENEW_AFTER
    assert runs.renew_due(env.engine, now=mono) == {"run_ct:crun_revoked": "renewed"}
    live = shim.tokens[AUD_API].access_token

    with env.db() as c:
        cascade.revoke_cascade(CascadeTrigger.GRANT_REVOKED, g.id, actor=session_ctx(env.alice),
                               reason="access withdrawn", db=c)
    assert _reason(lambda: verify(env, shim, token=live)) == "jti_revoked"

    clock[0] += RENEW_AFTER
    mono += RENEW_AFTER
    out = runs.renew_due(env.engine, now=mono)
    assert out.get("run_ct:crun_revoked") != "renewed", out
    with env.db() as c:
        assert _reason(lambda: runs.renew(shim, db=c, base_url=BASE)) in (
            "jti_revoked", "grant_revoked", "grant_inactive")


def test_a_grant_marked_revoked_without_its_cascade_still_refuses_renewal(env, clock, monkeypatch):
    """Defence in depth: even if only the grant row says revoked (its cascade has not reached the
    run's tokens yet), renewal re-checks the grant live and refuses to mint a new token."""
    install_github(monkeypatch, env)
    g = env.grant(agents=(env.forge,), children=(), repos=("acme/web",))
    shim = start(env, g.id, agent=env.forge, run="run_ct:crun_row_only", repos=("acme/web",))
    with env.db() as c:
        c.execute(text("UPDATE identity_delegation_grants SET status = 'revoked', revoked_at = now() "
                       "WHERE id = :g"), {"g": g.id})
    clock[0] += RENEW_AFTER
    with env.db() as c:
        reason = _reason(lambda: runs.renew(shim, db=c, base_url=BASE))
    assert reason == "grant_inactive", reason
    with env.db() as c:
        live = c.execute(text("SELECT count(*) FROM identity_run_credentials WHERE "
                              "run_id = 'run_ct:crun_row_only' AND revoked_at IS NULL")).scalar()
    assert live == len(shim.tokens)       # nothing consumed, no NEW token minted

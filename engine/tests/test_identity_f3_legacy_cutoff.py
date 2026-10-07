"""F3 / M3: the BOSS_API_KEY (and every people bridge's) 30-day cutoff is ABSOLUTE.

Finding (scratchpad identity/sec-authn-findings.md M3): the deferral while the console key is
used and nobody has a session refreshed forever; keep_legacy_console_key set retire_after=NULL;
the legacy_env and OIDC-bearer bridges had no expiry check. Now: claim + 30 days, plus at most
ONE audited Owner extension of at most 30 days, for every bridge a person uses.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from boss_engine.identity import legacy, migration, policy
from boss_engine.identity.authn import AuthnError
from boss_engine.identity.legacy import LegacyKind
from engine.tests.identity_s5_support import (  # noqa: F401, F811  (db, s5_engine: fixtures)
    CONSOLE_KEY,
    NOW,
    ORG,
    WORKER_KEY,
    community_install,
    creds,
    db,
    events,
    fake_claim,
    s5_engine,
    settings,
)

HUMAN_KEY = "abcd" + "5" * 60


def _cfg():
    return settings(legacy_console_api_key=CONSOLE_KEY,
                    api_key_principal_map={CONSOLE_KEY: "ceo", WORKER_KEY: "system", HUMAN_KEY: "cto"})


def _console():
    return creds(method="api_key", member="ceo", key=CONSOLE_KEY, ip="127.0.0.1")


def _human_env_key():
    return creds(method="api_key", member="cto", key=HUMAN_KEY, ip="127.0.0.1")


def _worker():
    return creds(method="api_key", member="system", key=WORKER_KEY, ip="127.0.0.1")


def _oidc():
    return creds(method="oidc", member="cto", ip="127.0.0.1")


def _claimed(db):
    cfg = _cfg()
    community_install(db)
    assert legacy.bridge(_console(), db=db, now=NOW, settings=cfg) is not None
    claim = fake_claim(db, name="Owner", email="owner@example.com", via="setup_token", now=NOW, cfg=cfg)
    return cfg, claim


def test_m3_the_console_key_deferral_never_outlives_claim_plus_30_days(db):
    cfg, _claim = _claimed(db)
    day29 = NOW + timedelta(days=29)
    assert legacy.bridge(_console(), db=db, now=day29, settings=cfg) is not None
    # the console still sends the key every minute and nobody has a session: before F3 this
    # deferred retirement forever
    legacy.invalidate_cache()
    with pytest.raises(AuthnError):
        legacy.bridge(_console(), db=db, now=NOW + timedelta(days=45), settings=cfg)


def test_m3_keep_legacy_console_key_is_capped_at_the_absolute_cutoff(db):
    cfg = _cfg()
    community_install(db)
    legacy.bridge(_console(), db=db, now=NOW, settings=cfg)
    policy.set_extra(ORG, "keep_legacy_console_key", True, db=db)
    fake_claim(db, name="Owner", email="owner@example.com", via="setup_token", now=NOW, cfg=cfg)
    m = migration.mapping(db, ORG, LegacyKind.CONSOLE_API_KEY, "BOSS_API_KEY")
    assert m["retire_after"] is not None and m["retire_after"] <= NOW + timedelta(days=30)


def test_m3_a_persons_env_key_and_oidc_bearer_end_at_the_cutoff_the_workers_does_not(db):
    cfg, _claim = _claimed(db)
    day10 = NOW + timedelta(days=10)
    assert legacy.bridge(_human_env_key(), db=db, now=day10, settings=cfg) is not None
    assert legacy.bridge(_oidc(), db=db, now=day10, settings=cfg) is not None
    later = NOW + timedelta(days=31)
    legacy.invalidate_cache()
    with pytest.raises(AuthnError):
        legacy.bridge(_human_env_key(), db=db, now=later, settings=cfg)
    with pytest.raises(AuthnError):
        legacy.bridge(_oidc(), db=db, now=later, settings=cfg)
    # the worker's key is a machine credential, not a people bridge (its end is S3's run tokens)
    assert legacy.bridge(_worker(), db=db, now=later, settings=cfg) is not None


def test_m3_an_env_keys_own_expiry_is_checked_by_the_bridge(db):
    cfg, _claim = _claimed(db)
    assert legacy.bridge(_worker(), db=db, now=NOW, settings=cfg) is not None
    db.execute(text("UPDATE identity_api_credentials SET expires_at = :t WHERE kind = 'legacy_env' "
                    "AND name LIKE '%(system)%'"), {"t": NOW + timedelta(days=1)})
    legacy.invalidate_cache()
    with pytest.raises(AuthnError):
        legacy.bridge(_worker(), db=db, now=NOW + timedelta(days=2), settings=cfg)


def test_m3_one_owner_extension_of_at_most_30_days(db):
    cfg, claim = _claimed(db)
    far = NOW + timedelta(days=200)
    moved = migration.extend_path(ORG, LegacyKind.CONSOLE_API_KEY, far, db=db, actor_ctx=claim.ctx,
                                  now=NOW + timedelta(days=1))
    assert moved == NOW + timedelta(days=60)                    # capped: 30 + one extension of 30
    assert events(db, "policy.changed")[-1]["details"]["legacy_cutoff"]["extension_days"] == 30
    with pytest.raises(ValueError, match="already extended"):
        migration.extend_path(ORG, LegacyKind.SHARED_PASSWORD if migration.mapping(
            db, ORG, LegacyKind.SHARED_PASSWORD, "BOSS_CONSOLE_PASSWORD") else LegacyKind.CONSOLE_API_KEY,
            far, db=db, actor_ctx=claim.ctx, now=NOW + timedelta(days=2))
    legacy.invalidate_cache()
    with pytest.raises(AuthnError):
        legacy.bridge(_console(), db=db, now=NOW + timedelta(days=61), settings=cfg)


def test_m3_only_an_owner_extends(db):
    import dataclasses

    from boss_engine.identity.principal import Principal, PrincipalKind, PrincipalStatus

    _cfg_, claim = _claimed(db)
    stranger = dataclasses.replace(claim.ctx, principal=Principal(
        "usr_not_owner", PrincipalKind.USER, ORG, PrincipalStatus.ACTIVE, "Admin"))
    with pytest.raises(PermissionError):
        migration.extend_path(ORG, LegacyKind.CONSOLE_API_KEY, NOW + timedelta(days=40), db=db,
                              actor_ctx=stranger, now=NOW)

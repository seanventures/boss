"""The apps' `oidc` signing keys rotate on the identity maintenance tick (F6; IDENTITY.md D35).

S6 built `signing_keys.rotate_oidc_family_if_due` and left calling it to the identity owner: until
now nothing called it, so an org's app-token keys never rotated. The tick now rotates every org's
due `oidc` keys (their own table), next to BOSS's own families.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import maintenance
from boss_engine.oidc_provider import keys as oidc_keys


def _active(engine, org: str) -> dict[str, str]:
    with engine.begin() as c:
        return {r[0]: r[1] for r in c.execute(text(
            "SELECT alg, kid FROM oidc_signing_keys WHERE org_id = :o AND status = 'active'"), {"o": org}).all()}


def test_a_due_oidc_key_rotates_on_the_tick_and_a_fresh_one_does_not(engine, monkeypatch, request):
    import base64

    from boss_engine.identity import vault

    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"r" * 32).decode())
    vault.reset_master_key_cache()
    request.addfinalizer(vault.reset_master_key_cache)
    request.addfinalizer(oidc_keys.clear_cache)
    oidc_keys.clear_cache()
    world = IdentityWorld.create(engine)
    with engine.begin() as c:
        oidc_keys.provision(c, org_id=world.org)
    before = _active(engine, world.org)
    assert before, "no oidc keys were provisioned"

    out = maintenance.tick(engine)
    assert out.get("rotated_oidc_keys") == [] or all(k not in before.values() for k in out["rotated_oidc_keys"])
    assert _active(engine, world.org) == before                              # nothing was due

    with engine.begin() as c:                                                # 31 days later
        c.execute(text("UPDATE oidc_signing_keys SET activated_at = now() - interval '31 days' "
                       "WHERE org_id = :o AND status = 'active'"), {"o": world.org})
    out = maintenance.tick(engine, now=datetime.now(timezone.utc) + timedelta(seconds=1))
    assert isinstance(out["rotated_oidc_keys"], list) and out["rotated_oidc_keys"], out
    after = _active(engine, world.org)
    assert set(after) == set(before) and all(after[a] != before[a] for a in before)   # every alg rotated
    with engine.begin() as c:
        retiring = {r[0] for r in c.execute(text(
            "SELECT kid FROM oidc_signing_keys WHERE org_id = :o AND status = 'retiring'"), {"o": world.org}).all()}
    assert set(before.values()) <= retiring                                   # the old ones overlap, then retire


def test_the_tick_names_the_step():
    import inspect

    assert '"rotated_oidc_keys"' in inspect.getsource(maintenance.tick)

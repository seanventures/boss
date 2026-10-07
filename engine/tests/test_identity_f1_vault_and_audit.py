"""F1 · the vault's boot and transactions (#10, #11, #12 and the vault LOW items) and the audit
log's role separation and keyed chain (#13, A74, A75).

Each test fails without the fix it names:
  #10  a wrong master key, or a missing key file for a vault that has data, REFUSES boot; the key
       file is created only at first install (an empty vault), never by a runtime read;
  #11  `needs_reauth` sticks even when the caller's transaction rolls back;
  #12  a rotated refresh token is on disk even when the caller's transaction rolls back;
  L    a person's TOTP seed is never handed out as a "provider token" by naming its item id;
  L    a provider revocation that fails leaves the item revoked-but-not-shredded, retried later;
  L    rotate_master_key is audited and the CLI swaps the key file only after the commit;
  #13  as a NON-SUPERUSER app role: INSERT/SELECT only — UPDATE, DELETE, TRUNCATE are refused, the
       old prune switch opens nothing, the prune function deletes only a checkpointed range;
       TRUNCATE is refused even for the owner; the chain is HMAC-keyed (a recomputed SHA-256 does not
       verify); verify checks the published anchors (a chain cut back is a break); old unkeyed rows
       are re-keyed once.
"""
from __future__ import annotations

import base64
import os
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from boss_engine.identity import audit, cli, vault
from boss_engine.identity.audit import Outcome, SecurityEvent

from ._identity_agents_support import MASTER_KEY, build_env, install_github, link_github, session_ctx
from engine.tests.identity_s5_support import written_days_ago
from engine.tests.identity_s5_support import ORG, community_install, db, s5_engine  # noqa: F401


@pytest.fixture()
def env(monkeypatch):
    return build_env(monkeypatch)


@pytest.fixture(autouse=True)
def _fresh_key_caches():
    vault.reset_master_key_cache()
    yield
    vault.reset_master_key_cache()


# ---- #10 boot ------------------------------------------------------------------------------------------

def test_10_a_wrong_master_key_refuses_boot(env, monkeypatch):
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"w" * 32).decode())
    vault.reset_master_key_cache()
    with env.db() as c, pytest.raises(vault.BootRefused) as e:
        vault.boot(db=c)
    assert e.value.code == "master_key_mismatch"


def test_10_a_missing_key_file_for_a_vault_with_data_refuses_boot_and_is_never_recreated(env, monkeypatch, tmp_path):
    key_file = tmp_path / "master.key"
    monkeypatch.delenv("BOSS_MASTER_KEY", raising=False)
    monkeypatch.setenv("BOSS_MASTER_KEY_FILE", str(key_file))
    vault.reset_master_key_cache()
    with env.db() as c, pytest.raises(vault.BootRefused) as e:
        vault.boot(db=c)
    assert e.value.code == "master_key_file_missing" and not key_file.exists()
    with pytest.raises(vault.MasterKeyMissing):
        vault._read_master_key()                               # a runtime read creates nothing
    assert not key_file.exists()


def test_10_the_key_file_is_created_only_at_first_install(env, monkeypatch, tmp_path):
    with env.db() as c:
        c.execute(text("SET LOCAL session_replication_role = replica"))
        c.execute(text("TRUNCATE identity_vault_items, identity_vault_keks, identity_signing_keys CASCADE"))
    key_file = tmp_path / "id" / "master.key"
    monkeypatch.delenv("BOSS_MASTER_KEY", raising=False)
    monkeypatch.setenv("BOSS_MASTER_KEY_FILE", str(key_file))
    with env.db() as c:
        kek = vault.boot(db=c)
    assert key_file.exists() and kek.startswith("kek_")
    with env.db() as c:
        assert vault.boot(db=c) == kek                          # the same key on the next start
    vault.reset_master_key_cache()


# ---- #11 / #12 refresh transactions ----------------------------------------------------------------------

def _due(env, person):
    from boss_engine.identity import vault as v

    item = link_github(env, person, "gho_OLD", refresh="ghr_OLD", expires_in_s=30)
    return item, v.ConnectionRef(provider="github", owner_principal_id=person, item_id=item)


class _Boom(Exception):
    pass


def test_11_needs_reauth_sticks_when_the_callers_transaction_rolls_back(env, monkeypatch):
    install_github(monkeypatch, env)
    item, ref = _due(env, env.alice)

    def refused(_refresh):
        raise vault.RefreshRejected()
    monkeypatch.setitem(vault.REFRESHERS, "github", refused)
    with pytest.raises(_Boom):
        with env.db() as c:
            with pytest.raises(vault.VaultNeedsReauth):
                vault.get_credential(ref, vault.CredentialPurpose.PROVIDER_CALL, ctx=session_ctx(env.alice), db=c)
            raise _Boom()                                     # the business transaction rolls back
    with env.db() as c:
        assert c.execute(text("SELECT status FROM identity_vault_items WHERE id = :i"), {"i": item}).scalar() == "needs_reauth"


def test_12_a_rotated_refresh_token_survives_the_callers_rollback(env, monkeypatch):
    hub = install_github(monkeypatch, env)
    item, ref = _due(env, env.alice)
    with pytest.raises(_Boom):
        with env.db() as c:
            cred = vault.get_credential(ref, vault.CredentialPurpose.PROVIDER_CALL, ctx=session_ctx(env.alice), db=c)
            assert cred.token == "gho_NEW1"
            raise _Boom()
    with env.db() as c:
        row = vault._load(c, item)
        assert vault._open(c, row, "refresh") == "ghr_NEW1" and vault._open(c, row) == "gho_NEW1"
    with env.db() as c:                                       # fresh now: no second provider call
        vault.get_credential(ref, vault.CredentialPurpose.PROVIDER_CALL, ctx=session_ctx(env.alice), db=c)
    assert hub.refreshes == 1


# ---- vault LOW items -----------------------------------------------------------------------------------

def test_a_totp_seed_is_never_handed_out_as_a_provider_token(env):
    with env.db() as c:
        item = vault.store(org_id="org_boss", owner_principal_id=env.alice, kind=vault.VaultItemKind.TOTP_SECRET,
                           provider="totp", secret="JBSWY3DPEHPK3PXP", db=c)
    with env.db() as c, pytest.raises(vault.VaultForbidden) as e:
        vault.get_credential(vault.ConnectionRef(provider="totp", owner_principal_id=env.alice, item_id=item),
                             vault.CredentialPurpose.PROVIDER_CALL, ctx=session_ctx(env.alice), db=c)
    assert e.value.code == "wrong_kind"


def test_a_failed_provider_revocation_is_not_shredded_and_is_retried(env, monkeypatch):
    install_github(monkeypatch, env)
    item = link_github(env, env.alice, "gho_LIVE", expires_in_s=3600)
    calls = []

    def down(access, refresh):
        calls.append(access)
        raise RuntimeError("github unreachable")
    monkeypatch.setitem(vault.REVOKERS, "github", down)
    with env.db() as c:
        vault.revoke(item, ctx=session_ctx(env.alice), reason="disconnect", db=c)
    with env.db() as c:
        row = vault._load(c, item)
        assert row["status"] == "revoked" and row["provider_revoke_pending"]
        assert vault._open(c, row) == "gho_LIVE"             # still there to revoke with
        with pytest.raises(vault.VaultNeedsReauth):           # and unusable
            vault.get_credential(vault.ConnectionRef("github", env.alice, item),
                                 vault.CredentialPurpose.PROVIDER_CALL, ctx=session_ctx(env.alice), db=c)
    monkeypatch.setitem(vault.REVOKERS, "github", lambda a, r: calls.append(a))
    with env.db() as c:
        assert vault.retry_provider_revocations(db=c) == 1
    with env.db() as c:
        row = vault._load(c, item)
        assert not row["provider_revoke_pending"]
        with pytest.raises(vault.VaultError):
            vault._open(c, row)                               # shredded once the provider confirmed
    assert calls == ["gho_LIVE", "gho_LIVE"]


def test_rotate_master_key_is_audited_and_the_cli_swaps_the_file_after_commit(env, monkeypatch, tmp_path):
    install_github(monkeypatch, env)
    item = link_github(env, env.alice, "gho_ROT", expires_in_s=3600)
    key_file = tmp_path / "master.key"
    key_file.write_text(MASTER_KEY + "\n")
    monkeypatch.delenv("BOSS_MASTER_KEY", raising=False)
    monkeypatch.setenv("BOSS_MASTER_KEY_FILE", str(key_file))
    vault.reset_master_key_cache()
    new_key = base64.b64encode(os.urandom(32)).decode()
    new_file = tmp_path / "new.key"
    new_file.write_text(new_key)
    with env.db() as c:
        message = cli.rotate_master_key(str(new_file), db=c)
        assert key_file.read_text().strip() == MASTER_KEY      # not swapped before the commit
    assert key_file.read_text().strip() == new_key and new_key not in message
    assert "vault.master_key_rotated" in env.world.types()
    with env.db() as c:
        assert vault._open(c, vault._load(c, item)) == "gho_ROT"
        assert vault.boot(db=c).startswith("kek_")


# ---- #13 the audit log ----------------------------------------------------------------------------------

@pytest.fixture()
def app_role(s5_engine):
    """A NON-superuser role, granted what the app's role gets (identity_audit_grant_app)."""
    name = f"boss_f1_app_{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex
    org = f"org_f1app_{uuid.uuid4().hex[:8]}"
    with s5_engine.begin() as c:
        c.execute(text(f"CREATE ROLE {name} LOGIN NOSUPERUSER PASSWORD '{password}'"))
        c.execute(text("SELECT identity_audit_grant_app(:r)"), {"r": name})
        c.execute(text(f"GRANT SELECT ON events, orgs TO {name}"))
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'F1 app')"), {"o": org})
    url = s5_engine.url.set(username=name, password=password)
    engine = create_engine(url)
    engine.org = org
    yield engine
    engine.dispose()
    with s5_engine.begin() as c:
        c.execute(text("SET LOCAL session_replication_role = replica"))
        c.execute(text("DELETE FROM identity_security_events WHERE org_id = :o"), {"o": org})
        c.execute(text(f"DROP OWNED BY {name}"))
        c.execute(text(f"DROP ROLE {name}"))


def _ev(n=0, org=ORG, **kw):
    return SecurityEvent(org_id=org, event_type="user.updated", outcome=Outcome.SUCCESS,
                         actor_principal_id="usr_alice", actor_kind="user", details={"n": n}, **kw)


@pytest.fixture()
def keyed(monkeypatch, s5_engine):
    """A vault master key is configured (so each org's chain key lives in the vault), and each test
    writes to an org of its own (the app-role tests commit rows to the shared org)."""
    monkeypatch.setenv("BOSS_MASTER_KEY", MASTER_KEY)
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    vault.reset_master_key_cache()
    org = f"org_f1_{uuid.uuid4().hex[:8]}"
    with s5_engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'F1')"), {"o": org})
    with s5_engine.begin() as c:
        assert audit.chain_key(org, db=c) is not None
    yield org
    vault.reset_master_key_cache()


def test_13_the_app_role_may_only_insert_and_select(app_role):
    org = app_role.org
    with app_role.begin() as c:
        audit.record(_ev(1, org), db=c)
        assert c.execute(text("SELECT count(*) FROM identity_security_events WHERE org_id = :o"), {"o": org}).scalar() == 1
    for statement in ("UPDATE identity_security_events SET outcome = 'failure'",
                      "DELETE FROM identity_security_events",
                      "TRUNCATE identity_security_events",
                      "ALTER TABLE identity_security_events DISABLE TRIGGER identity_security_events_append_only",
                      "CREATE OR REPLACE FUNCTION identity_security_events_append_only() RETURNS trigger "
                      "LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END $$"):
        with pytest.raises(DBAPIError):
            with app_role.begin() as c:
                c.execute(text(statement))
    with pytest.raises(DBAPIError):                            # the old switch opens nothing
        with app_role.begin() as c:
            c.execute(text("SELECT set_config('boss.identity_audit_prune', 'on', true)"))
            c.execute(text("DELETE FROM identity_security_events"))
    with pytest.raises(DBAPIError, match="no checkpoint"):     # the prune function needs a checkpoint
        with app_role.begin() as c:
            c.execute(text("SELECT identity_audit_prune(:o, 999999, ARRAY['audit.checkpoint','bootstrap.claimed'])"),
                      {"o": org})
    with pytest.raises(DBAPIError):
        with app_role.begin() as c:
            c.execute(text("UPDATE identity_security_events SET hash_alg = 1"))


def test_13_the_audited_prune_still_works_for_the_app_role(app_role):
    org = app_role.org
    with app_role.begin() as c, written_days_ago(45):
        for i in range(3):
            audit.record(_ev(i, org), db=c)
    with app_role.begin() as c:
        result = audit.prune(org, before=datetime.now(timezone.utc) - timedelta(days=30, seconds=1), db=c,
                             actor_principal_id="usr_owner")
        assert result.pruned == 3
        assert audit.verify_chain(org, db=c).ok


def test_n3_the_app_role_cannot_forge_a_checkpoint_and_prune_with_it(app_role):
    """The re-test's N3 repro: INSERT a forged audit.checkpoint, then call the prune function."""
    import json

    org = app_role.org
    with app_role.begin() as c, written_days_ago(45):
        for i in range(5):
            audit.record(_ev(i, org), db=c)
    with pytest.raises(DBAPIError, match="checkpoint is written only"):
        with app_role.begin() as c:
            c.execute(text(
                "INSERT INTO identity_security_events (org_id, seq, stream, event_type, outcome, prev_hash, "
                "hash, details, hash_alg) VALUES (:o, 6, 'security', 'audit.checkpoint', 'info', :p, :h, "
                "CAST(:d AS jsonb), 2)"),
                {"o": org, "p": os.urandom(32), "h": os.urandom(32), "d": json.dumps({"pruned_through_seq": 5})})
    with pytest.raises(DBAPIError, match="no checkpoint"):
        with app_role.begin() as c:
            c.execute(text("SELECT identity_audit_prune(:o, 5, ARRAY['audit.checkpoint','bootstrap.claimed'])"),
                      {"o": org})
    with app_role.begin() as c:
        assert c.execute(text("SELECT count(*) FROM identity_security_events WHERE org_id = :o"),
                         {"o": org}).scalar() == 5


def test_n3_nothing_younger_than_the_floor_is_checkpointed_or_pruned(app_role):
    """Even through the one legitimate path, the database keeps the last 30 days, whoever asks."""
    org = app_role.org
    with app_role.begin() as c:
        for i in range(3):
            audit.record(_ev(i, org), db=c)
    with pytest.raises(DBAPIError, match="retention floor"):
        with app_role.begin() as c:
            audit.prune(org, before=datetime.now(timezone.utc) + timedelta(seconds=1), db=c,
                        actor_principal_id="usr_owner")
    with app_role.begin() as c:
        assert c.execute(text("SELECT count(*) FROM identity_security_events WHERE org_id = :o"),
                         {"o": org}).scalar() == 3


def test_n3_the_sql_floor_is_the_apis_floor(s5_engine):
    from apps.api.app.routers.identity_admin import MIN_PRUNE_DAYS

    with s5_engine.connect() as c:
        floor = c.execute(text("SELECT extract(day FROM identity_audit_min_prune_age())")).scalar()
    assert int(floor) == MIN_PRUNE_DAYS


def test_n4_rekey_rewrites_only_a_whole_unkeyed_chain(app_role):
    """N4: the app role cannot pick legacy rows and overwrite their hashes with arbitrary values."""
    org = app_role.org
    real = audit.chain_key
    try:
        audit.chain_key = lambda *a, **k: None
        with app_role.begin() as c:
            for i in range(3):
                audit.record(_ev(i, org), db=c)              # unkeyed (alg 1) rows
    finally:
        audit.chain_key = real
    for seqs, prevs, hashes in (
            ([2], [os.urandom(32)], [os.urandom(32)]),                        # one row of three
            ([1, 2, 3], [os.urandom(32)] * 3, [os.urandom(32)] * 3)):         # does not link
        with pytest.raises(DBAPIError, match="identity_audit_rekey"):
            with app_role.begin() as c:
                c.execute(text("SELECT identity_audit_rekey(:o, :s, :p, :h)"),
                          {"o": org, "s": seqs, "p": prevs, "h": hashes})


def test_n4_the_rekey_runs_at_boot_not_on_the_maintenance_timer():
    import inspect

    from boss_engine.identity import maintenance

    assert "_rekey" not in inspect.getsource(maintenance.tick)
    assert "rekey_at_boot" in (Path(__file__).resolve().parents[2] / "apps/api/app/main.py").read_text()


def test_13_truncate_is_refused_even_for_the_owner(db):
    community_install(db)
    audit.record(_ev(), db=db)
    with pytest.raises(DBAPIError, match="append-only"):
        with db.begin_nested():
            db.execute(text("TRUNCATE identity_security_events"))


def _disable(db):
    db.execute(text("ALTER TABLE identity_security_events DISABLE TRIGGER identity_security_events_append_only"))


def test_13_the_chain_is_keyed_a_recomputed_sha256_does_not_verify(db, keyed):
    import hashlib

    org = keyed
    for i in range(4):
        audit.record(_ev(i, org), db=db)
    assert audit.verify_chain(org, db=db).ok
    _disable(db)
    db.execute(text("UPDATE identity_security_events SET details = '{\"n\": 99}' WHERE org_id = :o AND seq = 3"),
               {"o": org})
    # an attacker with only the database recomputes the hashes (unkeyed) from the tampered row on
    rows = list(audit._iter_rows(db, org))
    prev = bytes(rows[1]["hash"])
    for row in rows[2:]:
        new = hashlib.sha256(prev + audit.canonical_json(row)).digest()
        for alg in (2, 1):
            db.execute(text("UPDATE identity_security_events SET prev_hash = :p, hash = :h, hash_alg = :a "
                            "WHERE org_id = :o AND seq = :s"),
                       {"p": prev, "h": new, "a": alg, "o": org, "s": row["seq"]})
            result = audit.verify_chain(org, db=db)
            assert not result.ok and result.first_break_seq == 3
        prev = new


def test_13_verify_checks_the_published_anchors(db, keyed):
    community_install(db)
    org = keyed
    for i in range(5):
        audit.record(_ev(i, org), db=db)
    assert audit.daily_anchor(org, db=db, project_id="boss-dogfood") is not None
    assert audit.verify_chain(org, db=db).ok
    _disable(db)
    db.execute(text("DELETE FROM identity_security_events WHERE org_id = :o AND seq >= 4"), {"o": org})
    result = audit.verify_chain(org, db=db)     # what is left still links up — the anchor says otherwise
    assert not result.ok


def test_13_old_unkeyed_rows_are_rekeyed_once(db, monkeypatch, keyed):
    org = keyed
    real = audit.chain_key
    monkeypatch.setattr(audit, "chain_key", lambda *a, **k: None)
    for i in range(3):
        audit.record(_ev(i, org), db=db)                       # written before the keyed chain
    monkeypatch.setattr(audit, "chain_key", real)
    assert not audit.verify_chain(org, db=db).ok               # unkeyed rows are not accepted
    assert audit.rekey(org, db=db) == 3
    assert audit.verify_chain(org, db=db).ok
    assert audit.rekey(org, db=db) == 0

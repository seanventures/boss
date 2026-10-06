"""The owner recovery CLI (IDENTITY.md §4.7, §6.12): host shell only, audited, single-use link."""
from __future__ import annotations

import subprocess
from datetime import timedelta

import pytest
from sqlalchemy import text

from boss_engine.identity import cli, migration, posture
from boss_engine.identity.migration import token_hash
from engine.tests.identity_s5_support import (  # noqa: F401, F811  (db, s5_engine: fixtures)
    NOW,
    REPO,
    community_install,
    db,
    events,
    fake_claim,
    s5_engine,
    settings,
)


def test_recover_owner_restores_a_locked_out_owner_in_one_audited_step(db):
    community_install(db)
    claim = fake_claim(db, name="Param", email="param@example.com", via="setup_token", now=NOW,
                       origin="https://boss.example.com", cfg=settings())
    pid = claim.principal_id
    db.execute(text("INSERT INTO identity_totp (principal_id, vault_item_id) VALUES (:p, 'vlt_x')"),
               {"p": pid})
    db.execute(text("INSERT INTO identity_sessions (id, token_hash, principal_id, auth_methods, "
                    "idle_expires_at, absolute_expires_at) VALUES ('ses_1', 'h1', :p, "
                    "ARRAY['pwd'], :t, :t)"), {"p": pid, "t": NOW + timedelta(hours=1)})
    # a second Owner exists, so the first can be disabled (last-Owner protection allows it)
    second = fake_claim(db, name="Sean", email="sean@example.com", via="invite", now=NOW,
                        assume_member=None, cfg=settings())
    db.execute(text("UPDATE identity_principals SET status = 'disabled' WHERE id = :p"),
               {"p": pid})

    done = cli.recover_owner("PARAM@example.com", db=db, now=NOW)
    assert done.principal_id == pid and done.reactivated
    assert done.link.startswith("https://boss.example.com/auth/recover#token=")
    token = done.link.split("#token=", 1)[1]
    row = db.execute(text("SELECT kind, principal_id, expires_at FROM identity_auth_challenges "
                          "WHERE id_hash = :h"), {"h": token_hash(token)}).first()
    assert tuple(row) == ("password_reset", pid, NOW + timedelta(minutes=15))
    dump = db.execute(text("SELECT string_agg(row_to_json(c)::text, '') FROM "
                           "identity_auth_challenges c")).scalar()
    assert token not in dump
    assert db.execute(text("SELECT status FROM identity_principals WHERE id = :p"),
                      {"p": pid}).scalar() == "active"
    assert db.execute(text("SELECT count(*) FROM identity_totp WHERE principal_id = :p"),
                      {"p": pid}).scalar() == 0
    assert db.execute(text("SELECT revoked_at FROM identity_sessions WHERE id = 'ses_1'")
                      ).scalar() is not None
    event = events(db, "owner.recovered_by_host")[-1]
    assert event["actor_principal_id"] == "sys_host_cli" and event["target_id"] == pid
    assert event["details"]["mfa_reset"] is True
    # every Owner sees the banner for 7 days
    assert any(b["kind"] == "owner_recovered" for b in posture.banners(second.ctx, db=db))


@pytest.mark.parametrize("email", ["nobody@example.com", ""])
def test_recover_owner_refuses_an_unknown_person(db, email):
    community_install(db)
    with pytest.raises(cli.RecoveryError):
        cli.recover_owner(email, db=db, now=NOW)


def test_recover_owner_never_makes_a_bridge_principal_an_owner(db):
    community_install(db)
    migration.seed(db, settings=settings(legacy_console_auth="network"), now=NOW)
    db.execute(text("INSERT INTO identity_users (principal_id, email) VALUES "
                    "('usr_network_operator', 'net@example.com')"))
    with pytest.raises(cli.RecoveryError, match="real person"):
        cli.recover_owner("net@example.com", db=db, now=NOW)


def test_recovery_is_reachable_only_from_the_host_shell():
    pattern = r"recover_owner|from \.cli import|from \. import .*\bcli\b|identity\.cli import|" \
              r"identity import .*\bcli\b|import_module\(.*identity\.cli"
    hits = subprocess.run(["git", "grep", "-l", "-E", pattern, "--",
                           "apps", "engine/boss_engine", "tools", "api"],
                          cwd=REPO, capture_output=True, text=True).stdout.split()
    assert set(hits) <= {"engine/boss_engine/identity/cli.py"}, hits
    with pytest.raises(SystemExit):
        cli.main(["recover-owner"])            # --email is required


def test_claim_status_is_the_one_line_boss_doctor_prints(db):
    community_install(db)
    cfg = settings(legacy_console_auth="network")
    migration.seed(db, settings=cfg, now=NOW)
    assert cli.claim_status("org_boss", db=db) == "unclaimed"
    fake_claim(db, name="Param", email="param@example.com", via="network_bridge", now=NOW,
               cfg=cfg)
    claimed_on = migration.claimed_at(db, "org_boss").date().isoformat()
    assert cli.claim_status("org_boss", db=db) == \
        f"claimed {claimed_on}; shared credentials stop " \
        f"{(NOW + timedelta(days=30)).date().isoformat()}"
    doctor = (REPO / "scripts/install/checkup.sh").read_text()
    assert "boss_engine.identity.cli claim-status" in doctor and "/claim once" in doctor

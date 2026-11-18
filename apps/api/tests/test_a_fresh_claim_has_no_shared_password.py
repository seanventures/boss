"""A FRESHLY CLAIMED INSTALL HAS NO SHARED PASSWORD (stranger test r2, 2026-11-17).

On a fresh install the sign-in page still offered "Use the shared password", which signed in as
Operator WITHOUT MFA for 30 days — beside the Owner who had just enrolled a second factor. The
shared password is a bridge for UPGRADES from shared-password days; a fresh install has nobody to
bridge. "Fresh" is the seat question's own rule (`claim.install_has_history`: no decision, workflow
run, code task run or project beyond the seeded default), and the claim did not itself use the
shared password. A retirement at the claim is audited and an Owner can undo it from the host shell
(`boss identity legacy restore --path shared_password`, review of #204).

Pinned here, through the real app:
  * fresh (setup-link claim, no seat history): `/v1/auth/options` offers no shared password and
    `/v1/auth/login` with it is refused 403 `shared_password_retired`;
  * upgrade (seat history): it is offered with its end date and still signs in, in its window;
  * a claim made WITH the shared password is never "fresh".
"""
from __future__ import annotations

import pytest

from apps.api.tests.identity_s1_world import HEADERS, STRONG, fresh_client  # noqa: F401
from apps.api.tests.identity_s1_world import world  # noqa: F401  (fixture)
from apps.api.tests.test_identity_claim_to_signed_in_owner import (  # noqa: F401  (fixture)
    SHARED,
    _setup_token,
    real_world,
)
from boss_engine.identity import claim


def _claim_with_link(capsys) -> None:
    token = _setup_token(capsys)
    res = fresh_client().post("/v1/auth/setup/claim", headers=HEADERS, json={
        "name": "New Owner", "email": "owner@example.test", "setup_token": token, "password": STRONG})
    assert res.status_code == 200, res.text


def _shared_login():
    return fresh_client().post("/v1/auth/login", headers=HEADERS, json={"email": "", "password": SHARED})


def test_a_fresh_install_neither_offers_nor_accepts_the_shared_password(real_world, monkeypatch, capsys):
    monkeypatch.setattr(claim, "install_has_history", lambda db: False)
    _claim_with_link(capsys)
    options = fresh_client().get("/v1/auth/options").json()
    assert options.get("shared_password") is None
    res = _shared_login()
    assert res.status_code == 403, res.text
    assert res.json()["error"] == "shared_password_retired"


def test_an_upgrade_keeps_the_shared_password_in_its_window(real_world, monkeypatch, capsys):
    monkeypatch.setattr(claim, "install_has_history", lambda db: True)
    _claim_with_link(capsys)
    offer = fresh_client().get("/v1/auth/options").json().get("shared_password")
    assert offer and offer.get("until")
    res = _shared_login()
    assert res.status_code == 200, res.text


@pytest.mark.parametrize("path, history, fresh", [
    ("setup_token", False, True),
    ("network", False, True),
    ("setup_token", True, False),
    ("legacy_password", False, False),
])
def test_the_fresh_rule(path, history, fresh):
    assert claim.is_fresh_install(None, path, history=history) is fresh


def test_a_retirement_at_the_claim_is_audited_and_an_owner_can_undo_it(real_world, engine, monkeypatch, capsys):
    from sqlalchemy import text

    from boss_engine.identity import cli, migration
    from boss_engine.identity.legacy import LegacyKind

    monkeypatch.setattr(claim, "install_has_history", lambda db: False)
    _claim_with_link(capsys)
    with engine.connect() as db:
        org = migration.default_org(db)
        retired = db.execute(text("SELECT count(*) FROM identity_security_events WHERE org_id = :o "
                                  "AND event_type = 'legacy.path_retired'"), {"o": org}).scalar()
    assert retired >= 1
    assert _shared_login().status_code == 403

    # The host shell (`boss identity legacy restore --path shared_password`) brings it back.
    assert cli.main(["legacy", "restore", "--path", "shared_password"]) == 0
    capsys.readouterr()
    offer = fresh_client().get("/v1/auth/options").json().get("shared_password")
    assert offer and offer.get("until")
    assert _shared_login().status_code == 200
    with engine.connect() as db:
        restored = db.execute(text("SELECT count(*) FROM identity_security_events WHERE org_id = :o "
                                   "AND event_type = 'legacy.path_restored'"), {"o": org}).scalar()
        assert restored == 1
        with pytest.raises(ValueError, match="not retired"):
            migration.restore_path(org, LegacyKind.SHARED_PASSWORD, db=db,
                                   actor_principal_id="sys_host_cli", reason="twice")

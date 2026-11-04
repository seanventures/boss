"""A token pasted in Setup › Repo becomes the vault's org GitHub connection, scoped to the repos picked
for it — the only way a build can use it under enforce (boss-egress reads it; the sandbox never does)."""
import base64

import pytest

from sqlalchemy import text

from apps.api.tests.setup_helpers import as_ceo, connect_repository, fakes
from boss_engine.db import begin
from boss_engine.identity import github_setup, vault


@pytest.fixture(autouse=True)
def _forget_the_test_key():
    yield
    vault.reset_master_key_cache()


def test_a_pasted_setup_token_is_the_org_connection_for_the_picked_repos(client, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"k" * 32).decode())   # the identity suites' test key
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    vault.reset_master_key_cache()
    with begin() as db:
        vault.check_master_key(db=db)
        db.execute(text("UPDATE identity_vault_items SET status = 'revoked' WHERE kind = 'legacy_pat' "
                        "AND owner_principal_id IS NULL"))
    connect_repository(client, token="ghp_e2e_full", full_name="e2e/app")
    with begin() as db:
        org = github_setup.org_token(db)
        assert org is not None and org["repos"] == ["e2e/app"]
        assert vault.org_github_token(org["id"], db=db) == "ghp_e2e_full"
        assert github_setup.mode(db)["mode"] in ("token", "app")
    connect_repository(client, token="ghp_e2e_full", full_name="e2e/other")   # same token, another repo
    with begin() as db:
        again = github_setup.org_token(db)
        assert again["id"] == org["id"] and again["repos"] == ["e2e/app", "e2e/other"]


def test_with_the_app_connected_setup_binds_its_repos_without_a_token(client, monkeypatch):
    """Setup › Repo › Use the GitHub App: the connection's credential is the App itself
    (`identity_github:app`), resolved to a fresh installation token at each use; nothing is
    copied into the legacy store or the vault's org token."""
    import json as _json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from apps.api.tests.setup_helpers import AUTH, PROJECT
    from boss_engine.identity import github_app
    from boss_engine.services.secret_backends import resolve_secret_ref

    as_ceo(monkeypatch)
    fakes(monkeypatch)
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    for name in ("BOSS_GITHUB_APP_ID", "BOSS_GITHUB_APP_INSTALLATION_ID"):
        monkeypatch.delenv(name, raising=False)
    vault.reset_master_key_cache()
    minted = []

    def transport(method, url, headers, body):
        if url.endswith("/access_tokens"):
            minted.append(_json.loads(body or b"{}"))
            return github_app.Response(201, _json.dumps({"token": f"ghs_APP{len(minted)}",
                                                         "expires_at": "2099-01-01T00:00:00Z"}).encode())
        if "/installation/repositories" in url:
            return github_app.Response(200, _json.dumps({"repositories": [
                {"full_name": "e2e/app", "private": True, "default_branch": "main",
                 "owner": {"type": "Organization"}}]}).encode())
        return github_app.Response(404, b"{}")

    monkeypatch.setattr(github_app, "TRANSPORT", transport)
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    with begin() as db:
        vault.check_master_key(db=db)
        org = db.execute(text("SELECT id FROM orgs ORDER BY created_at, id LIMIT 1")).scalar()
        db.execute(text("UPDATE identity_vault_items SET status = 'revoked' WHERE kind IN "
                        "('legacy_pat', 'app_private_key') AND owner_principal_id IS NULL"))
        vault.store(org_id=org, owner_principal_id=None, kind=vault.VaultItemKind.APP_PRIVATE_KEY,
                    provider="github", secret=pem, db=db)
        db.execute(text("DELETE FROM identity_github_app"))
        db.execute(text("INSERT INTO identity_github_app (org_id, app_id, installation_id, account_login) "
                        "VALUES (:o, '777', '4242', 'e2e')"), {"o": org})
    try:
        found = client.post("/v1/setup/repository/app", headers=AUTH, json={"project_id": PROJECT})
        assert found.status_code == 200, found.text
        body = found.json()
        assert [r["full_name"] for r in body["repos"]] == ["e2e/app"] and "ghs_" not in found.text
        picked = client.post("/v1/setup/repository/select", headers=AUTH, json={
            "project_id": PROJECT, "connection_id": body["connection_id"], "full_name": "e2e/app"})
        assert picked.status_code == 200, picked.text
        assert "ghs_" not in picked.text
        with begin() as db:
            row = db.execute(text("SELECT credential_ref FROM github_connections WHERE id = :c"),
                             {"c": body["connection_id"]}).scalar()
            assert row == "identity_github:app"
            ref = db.execute(text("SELECT id FROM secret_refs WHERE ref_kind = 'identity_github' "
                                  "AND project_id = :p"), {"p": PROJECT}).scalar()
            assert resolve_secret_ref(db, ref, None).startswith("ghs_APP")
            assert github_setup.org_token(db) is None              # nothing mirrored into the vault
        assert all("administration" not in m["permissions"] and "repositories" not in m for m in minted)
    finally:
        with begin() as db:
            db.execute(text("DELETE FROM identity_github_app"))


def test_a_second_accounts_token_is_kept_beside_the_first(client, monkeypatch):
    """BOSSAPP-808: a token for ANOTHER GitHub account pasted in Setup is a second org connection.
    It used to replace the first one, so the first account's repositories stopped building."""
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    vault.reset_master_key_cache()
    with begin() as db:
        vault.check_master_key(db=db)
        db.execute(text("UPDATE identity_vault_items SET status = 'revoked' WHERE kind = 'legacy_pat' "
                        "AND owner_principal_id IS NULL"))
    connect_repository(client, token="ghp_e2e_full", full_name="e2e/app")
    with begin() as db:
        first = github_setup.org_token(db)
        # The first account is someone else (the stub answers one login for every token).
        db.execute(text("UPDATE identity_vault_items SET provider_subject = 'first-account' WHERE id = :i"),
                   {"i": first["id"]})
    connect_repository(client, token="ghp_e2e_expiring", full_name="e2e/other")
    with begin() as db:
        tokens = github_setup.org_tokens(db)
        assert first["id"] in {t["id"] for t in tokens} and len(tokens) == 2
        assert github_setup.control_plane_token(db, "e2e", "app") == "ghp_e2e_full"
        assert github_setup.control_plane_token(db, "e2e", "other") == "ghp_e2e_expiring"

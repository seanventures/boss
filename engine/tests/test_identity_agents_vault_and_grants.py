"""S3 · The vault, Passports and delegation grants (IDENTITY.md §7.1, §7.3, §7.8, §7.9, §15.3).

A52–A55 (grant time), A57, A59, R9, and the grant and Passport rules. Each test fails if the
protection it names is removed. DB-backed: BOSS_IDENTITY_TEST_DATABASE_URL.
"""
from __future__ import annotations

import ast
import base64
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from boss_engine.identity import delegation, grants, passports, vault
from boss_engine.identity.principal import RunMode

from ._identity_agents_support import (
    BUILDER,
    ROOT,
    add_person,
    build_env,
    install_github,
    link_github,
    session_ctx,
    start,
    verify,
)


@pytest.fixture()
def env(monkeypatch):
    return build_env(monkeypatch)


# ---- the vault -----------------------------------------------------------------------------------------

def test_a_secret_round_trips_and_the_database_holds_no_plaintext(env):
    with env.db() as c:
        item = vault.store(org_id="org_boss", owner_principal_id=None, kind=vault.VaultItemKind.OIDC_CLIENT_SECRET,
                           provider="okta", secret="very-secret-value", db=c)
        row = c.execute(text("SELECT * FROM identity_vault_items WHERE id = :i"), {"i": item}).mappings().one()
        assert b"very-secret-value" not in bytes(row["secret_enc"]) and b"very-secret" not in bytes(row["dek_wrapped"])
        assert vault.read_secret(item, db=c, kind=vault.VaultItemKind.OIDC_CLIENT_SECRET) == "very-secret-value"


def test_a52_a_ciphertext_moved_to_another_row_fails_with_a_critical_event(env):
    with env.db() as c:
        a = vault.store(org_id="org_boss", owner_principal_id=None, kind=vault.VaultItemKind.OIDC_CLIENT_SECRET,
                        provider="okta", secret="secret-a", db=c)
        b = vault.store(org_id="org_boss", owner_principal_id=None, kind=vault.VaultItemKind.OIDC_CLIENT_SECRET,
                        provider="okta", secret="secret-b", db=c)
        c.execute(text("UPDATE identity_vault_items AS t SET secret_enc = s.secret_enc, secret_nonce = s.secret_nonce, "
                       "dek_wrapped = s.dek_wrapped FROM identity_vault_items s WHERE t.id = :b AND s.id = :a"),
                  {"a": a, "b": b})
    with env.db() as c, pytest.raises(vault.VaultDecryptError):
        vault.read_secret(b, db=c, kind=vault.VaultItemKind.OIDC_CLIENT_SECRET)
    failed = [e for e in env.world.events if e.event_type == "vault.decrypt_failed"]
    assert failed and failed[-1].target_id == b and failed[-1].details.get("critical") is True
    with env.db() as c:                               # the refresh token cannot stand in for the secret
        d = vault.store(org_id="org_boss", owner_principal_id="usr_alice", kind=vault.VaultItemKind.OAUTH_USER,
                        provider="github", secret="gho_a", refresh="ghr_a", db=c)
        c.execute(text("UPDATE identity_vault_items SET secret_enc = refresh_enc, secret_nonce = refresh_nonce "
                       "WHERE id = :d"), {"d": d})
    with env.db() as c, pytest.raises(vault.VaultDecryptError):
        vault.get_credential(vault.ConnectionRef("github", "usr_alice", item_id=d), vault.CredentialPurpose.PROVIDER_CALL,
                             ctx=session_ctx("usr_alice"), db=c)


def test_a53_a_wrong_master_key_refuses_to_boot(env, monkeypatch):
    with env.db() as c:
        vault.store(org_id="org_boss", owner_principal_id=None, kind=vault.VaultItemKind.TOTP_SECRET,
                    provider=None, secret="JBSWY3DPEHPK3PXP", db=c)
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"z" * 32).decode())
    vault.reset_master_key_cache()
    with env.db() as c, pytest.raises(vault.MasterKeyMismatch):
        vault.check_master_key(db=c)
    monkeypatch.delenv("BOSS_MASTER_KEY")
    vault.reset_master_key_cache()
    with env.db() as c, pytest.raises(vault.MasterKeyMissing):
        vault.check_master_key(db=c)


def test_rotating_the_master_key_rewraps_every_row_and_the_old_key_stops_working(env, monkeypatch):
    with env.db() as c:
        item = vault.store(org_id="org_boss", owner_principal_id=None, kind=vault.VaultItemKind.TOTP_SECRET,
                           provider=None, secret="JBSWY3DPEHPK3PXP", db=c)
        new = b"n" * 32
        assert vault.rotate_master_key(new, db=c) >= 1
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(new).decode())
    vault.reset_master_key_cache()
    with env.db() as c:
        vault.check_master_key(db=c)
        assert vault.read_secret(item, db=c, kind=vault.VaultItemKind.TOTP_SECRET) == "JBSWY3DPEHPK3PXP"


def test_r9_only_vault_py_decrypts():
    offenders = []
    roots = [ROOT / "engine" / "boss_engine", ROOT / "apps" / "api", ROOT / "apps" / "egress", ROOT / "apps" / "worker"]
    for root in roots:
        for path in root.rglob("*.py"):
            if path.name == "vault.py" and path.parent.name == "identity":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("vault") and \
                        any(a.name == "_decrypt" for a in node.names):
                    offenders.append(str(path))
                if isinstance(node, ast.Attribute) and node.attr == "_decrypt" and \
                        isinstance(node.value, ast.Name) and node.value.id == "vault":
                    offenders.append(str(path))
    assert offenders == []


def test_a54_a_run_for_alice_cannot_use_bobs_connection(env, monkeypatch):
    gh = install_github(monkeypatch, env)
    gh.user_repo_perms[("gho_ALICE", "acme/web")] = {"pull": True, "push": True}
    with env.db() as c:
        add_person(c, env.bob)
    bob_item = link_github(env, env.bob, "gho_BOB")
    link_github(env, env.alice, "gho_ALICE")
    g = env.grant(repos=("acme/web",), gh={"contents": "write", "as_user": True})
    shim = start(env, g.id)
    with env.db() as c:
        from boss_engine.identity import tokens

        ctx = tokens.context_for(verify(env, shim), db=c)
        for ref in (vault.ConnectionRef("github", env.bob), vault.ConnectionRef("github", env.alice, item_id=bob_item)):
            with pytest.raises(vault.VaultForbidden):
                vault.get_credential(ref, vault.CredentialPurpose.GITHUB_AS_USER, ctx=ctx, db=c)
        mine = vault.get_credential(vault.ConnectionRef("github", env.alice), vault.CredentialPurpose.GITHUB_AS_USER,
                                    ctx=ctx, db=c)
    assert mine.token == "gho_ALICE" and "gho_ALICE" not in repr(mine)


def test_as_me_on_github_needs_the_grant_to_say_so(env, monkeypatch):
    install_github(monkeypatch, env)
    link_github(env, env.alice, "gho_ALICE")
    shim = start(env, env.grant(repos=("acme/web",)).id)
    with env.db() as c:
        from boss_engine.identity import tokens

        ctx = tokens.context_for(verify(env, shim), db=c)
        with pytest.raises(vault.VaultForbidden) as e:
            vault.get_credential(vault.ConnectionRef("github", env.alice), vault.CredentialPurpose.GITHUB_AS_USER,
                                 ctx=ctx, db=c)
    assert e.value.code == "grant_not_as_me"


def test_a57_concurrent_refresh_is_single_flight_and_both_callers_get_the_new_token(env, monkeypatch):
    gh = install_github(monkeypatch, env)
    link_github(env, env.alice, "gho_OLD", expires_in_s=60)            # inside the 5-minute window
    results, errors = [], []

    def worker():
        try:
            with env.db() as c:
                cred = vault.get_credential(vault.ConnectionRef("github", env.alice),
                                            vault.CredentialPurpose.PROVIDER_CALL, ctx=session_ctx(env.alice), db=c)
                results.append(cred.token)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and gh.refreshes == 1 and results == ["gho_NEW1", "gho_NEW1"]


def test_a_refused_refresh_marks_the_connection_needs_reauth_and_never_loops(env, monkeypatch):
    install_github(monkeypatch, env)
    item = link_github(env, env.alice, "gho_OLD", expires_in_s=10)

    def rejected(_refresh):
        raise vault.RefreshRejected("bad_refresh_token")             # e.g. a reused refresh token

    monkeypatch.setitem(vault.REFRESHERS, "github", rejected)
    with env.db() as c, pytest.raises(vault.VaultNeedsReauth):
        vault.get_credential(vault.ConnectionRef("github", env.alice), vault.CredentialPurpose.PROVIDER_CALL,
                             ctx=session_ctx(env.alice), db=c)
    with env.db() as c:
        assert c.execute(text("SELECT status FROM identity_vault_items WHERE id = :i"), {"i": item}).scalar() == "needs_reauth"
    assert "vault.connection.needs_reauth" in env.world.types()


def test_a59_a_jit_github_credential_is_impossible_while_the_policy_is_off(env, monkeypatch):
    gh = install_github(monkeypatch, env)
    g = env.grant(repos=("acme/web",))
    shim = start(env, g.id, repos=("acme/web",))
    from boss_engine.identity import tokens

    with env.db() as c:
        claims = verify(env, shim)
        ctx = tokens.context_for(claims, db=c)
        with pytest.raises(vault.VaultForbidden) as e:
            vault.get_credential(vault.ConnectionRef("github", None), vault.CredentialPurpose.GITHUB_JIT,
                                 ctx=ctx, task=claims.task, db=c)
    assert e.value.code == "jit_disabled" and gh.issued == []
    env.world.policy_overrides["github_jit_credentials"] = True
    with env.db() as c:
        cred = vault.get_credential(vault.ConnectionRef("github", None), vault.CredentialPurpose.GITHUB_JIT,
                                    ctx=ctx, task=claims.task, db=c)
    assert cred.token in gh.issued and "credential.handed_out" in env.world.types()


def test_disconnecting_revokes_at_the_provider_and_crypto_shreds_the_row(env, monkeypatch):
    gh = install_github(monkeypatch, env)
    item = link_github(env, env.alice, "gho_ALICE")
    with env.db() as c:
        vault.revoke(item, ctx=session_ctx(env.alice), reason="disconnect", db=c)
    assert any("/applications/Iv1.test/grant" in u for u in gh.urls("DELETE"))
    with env.db() as c:
        assert c.execute(text("SELECT status FROM identity_vault_items WHERE id = :i"), {"i": item}).scalar() == "revoked"
        c.execute(text("UPDATE identity_vault_items SET status = 'active' WHERE id = :i"), {"i": item})
    with env.db() as c, pytest.raises(vault.VaultDecryptError):    # shredded: nothing reopens it
        vault.get_credential(vault.ConnectionRef("github", env.alice), vault.CredentialPurpose.PROVIDER_CALL,
                             ctx=session_ctx(env.alice), db=c)


# ---- Passports -------------------------------------------------------------------------------------------

def test_a_passport_needs_an_active_human_sponsor(env):
    with env.db() as c:
        for sponsor in ("", "agt_forge000001", "usr_nobody"):
            with pytest.raises(passports.PassportError) as e:
                passports.ensure_passport(org_id="org_boss", agent_id="agt_new00000001", role_key="forge",
                                          display_name="New", sponsor_principal_id=sponsor, db=c)
            assert e.value.code == "sponsor_required"


def test_presets_seed_ceilings_and_a_ceiling_holds_delegable_permissions_only(env):
    with env.db() as c:
        forge = passports.get(env.forge, db=c)
        assert forge.ceiling_preset == "builder" and "github:sync" in forge.ceiling
        assert forge.external_ceiling["github"]["contents"] == "write"
    env.world.grant(env.alice, {"agent_passport:manage"})
    with env.db() as c, pytest.raises(passports.PassportError) as e:
        passports.set_ceiling(session_ctx(env.alice), env.forge, frozenset({"code_task:read", "decision:approve"}), db=c)
    assert e.value.code == "not_delegable"
    with env.db() as c, pytest.raises(Exception):                # the DB trigger is the second check
        c.execute(text("INSERT INTO identity_agent_ceilings VALUES (:a, 'host:exec')"), {"a": env.forge})


def test_an_agent_token_can_never_change_a_passport(env):
    from boss_engine.identity import tokens

    env.world.grant(env.alice, {"agent_passport:manage", "agent_passport:suspend"})
    shim = start(env, env.grant().id)
    with env.db() as c:
        ctx = tokens.context_for(verify(env, shim), db=c)
        with pytest.raises(passports.PassportError) as e:
            passports.set_ceiling(ctx, env.forge, frozenset(BUILDER), db=c)
        assert e.value.code == "people_only"
        with pytest.raises(passports.PassportError):
            passports.suspend(ctx, env.forge, reason="x", db=c)


# ---- grants ---------------------------------------------------------------------------------------------

def test_a_grant_never_exceeds_the_persons_live_access_or_any_named_ceiling(env):
    with pytest.raises(grants.GrantRefused) as e:
        env.grant(permissions=BUILDER + ("workflow:create",))           # Alice lacks workflow:create
    assert e.value.code == "exceeds_your_access"
    env.world.grant(env.alice, {"meeting:convene"}, env.project)
    with pytest.raises(grants.GrantRefused) as e:                     # Forge's ceiling lacks it
        env.grant(agents=(env.forge,), permissions=("meeting:convene",))
    assert e.value.code == "exceeds_ceiling"
    with pytest.raises(grants.GrantRefused) as e:
        env.grant(permissions=("code_task:read", "decision:approve"))
    assert e.value.code == "not_delegable"


def test_only_a_person_in_a_human_session_creates_a_grant(env):
    from boss_engine.identity import AuthContext, AuthMethod, tokens

    shim = start(env, env.grant().id)
    req = delegation.GrantRequest(agent_ids=(env.forge,), permissions=frozenset({"code_task:read"}),
                                  project_ids=(env.project,))
    with env.db() as c:
        agent = tokens.context_for(verify(env, shim), db=c)
        with pytest.raises(grants.GrantRefused) as e:
            grants.create_grant(agent, req, db=c)
        assert e.value.code == "people_only"
        pat = AuthContext(session_ctx(env.alice).principal, AuthMethod.API_KEY)
        with pytest.raises(grants.GrantRefused):
            grants.create_grant(pat, req, db=c)


def test_expiry_defaults_to_30_days_and_caps_at_90(env):
    g = env.grant()
    assert timedelta(days=29) < g.expires_at - datetime.now(timezone.utc) <= timedelta(days=30)
    with pytest.raises(grants.GrantRefused) as e:
        env.grant(expires_at=datetime.now(timezone.utc) + timedelta(days=91))
    assert e.value.code == "too_long"


def test_a55_a_grant_naming_a_repo_the_person_cannot_write_is_refused_on_multi_person_installs(env, monkeypatch):
    gh = install_github(monkeypatch, env)
    link_github(env, env.alice, "gho_ALICE")
    gh.user_repo_perms[("gho_ALICE", "acme/web")] = {"pull": True, "push": True}
    gh.user_repo_perms[("gho_ALICE", "acme/infra")] = {"pull": True, "push": False}
    # One person: the guard is off (D28) — nobody else to confused-deputy.
    env.grant(repos=("acme/infra",))
    assert not any("/repos/acme/infra" in u for u in gh.urls("GET"))
    with env.db() as c:
        add_person(c, env.bob)                                   # a second active human: guard on
    grants.clear_guard_cache()
    with pytest.raises(grants.GrantRefused) as e:
        env.grant(repos=("acme/infra",))
    assert e.value.code == "github_access_lacking"
    env.grant(repos=("acme/web",))                               # where she can push, it is fine


def test_a_task_bound_grant_ends_with_its_run(env):
    g = env.grant(task_run_id="run_ct:build7")
    with pytest.raises(Exception):                               # it covers only its own run
        start(env, g.id, run="run_ct:other")
    shim = start(env, g.id, run="run_ct:build7")
    from boss_engine.identity import runs

    with env.db() as c:
        runs.end_run("run_ct:build7", db=c, org_id="org_boss")
    with env.db() as c:
        assert grants.load(g.id, db=c).status.value == "expired"
    with pytest.raises(Exception):
        verify(env, shim)


def test_autonomous_mode_runs_as_the_agent_within_its_own_bindings_and_ceiling(env):
    from boss_engine.identity import runs, tokens
    from boss_engine.identity.delegation import AUD_API
    from ._identity_agents_support import agent_ctx

    with env.db() as c:
        c.execute(text("UPDATE identity_agent_passports SET autonomous = true WHERE agent_id = :a"), {"a": env.forge})
    env.world.grant(env.forge, {"code_task:read", "preview:read", "deployment:create"}, env.project)
    with env.db() as c:
        shim = runs.start_run(agent_ctx(env.forge), runs.RunStart(run_id="run_ag:sweep", agent_id=env.forge,
                                                                  project_id=env.project, scope=frozenset(BUILDER)),
                              grant_id=None, db=c)
    claims = verify(env, shim)
    assert claims.mode == RunMode.AUTONOMOUS and claims.sub == env.forge and claims.chain.depth == 0
    assert claims.scope == frozenset({"code_task:read", "preview:read"})   # bindings ∩ ceiling ∩ requested
    with env.db() as c:
        c.execute(text("UPDATE identity_agent_passports SET autonomous = false WHERE agent_id = :a"), {"a": env.forge})
        assert not tokens.decide(claims, "code_task:read", None, db=c).allowed
    assert AUD_API in shim.tokens

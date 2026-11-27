"""A credential's host comes from WHERE THE SECRET LIVES, never from the connection naming it
(BOSSAPP-1286, fifth review of #330).

S-A: a connection with credential_ref env:BOSS_GITHUB_TOKEN and api_base_url
https://evil.example/api/v3 re-tagged the install's token as evil.example, and git_access sent it
there. Now an env reference is always the install's credential: assert_may_send refuses the
foreign base, a connection may not pair them, and git never builds the header for evil.example.

S-B: a GHES connection with its OWN GitHub App could never mint, because the App JWT was tagged
as the install's. The JWT now carries the host its private key was stored for.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text

from boss_engine.identity import github_urls as G

from ._ops2_support import fresh_engine, make_project

EVIL = "https://evil.example/api/v3"
GHES = "https://ghe.northgate.example/api/v3"


@pytest.fixture(autouse=True)
def _install_is_github_com(monkeypatch):
    monkeypatch.delenv(G.API_BASE_ENV, raising=False)
    monkeypatch.delenv(G.WEB_BASE_ENV, raising=False)
    monkeypatch.setenv("BOSS_GITHUB_TOKEN", "install-token-never-to-evil")


@pytest.fixture(scope="module")
def eng():
    engine, drop = fresh_engine("origin")
    yield engine
    drop()


def test_an_env_reference_is_tagged_as_the_install_whatever_the_connection_says():
    from boss_engine.services.env_refs import CredentialMisdirected
    from boss_engine.services.github_connections import GitHubConnectionService

    from boss_engine.config import get_settings

    tag = GitHubConnectionService(get_settings())._tag_by_origin  # noqa: SLF001
    with pytest.raises(CredentialMisdirected):          # rc6 N-1: before any re-tag
        tag("install-token-never-to-evil", "env:BOSS_GITHUB_TOKEN", EVIL)
    ok = tag("install-token", "env:BOSS_GITHUB_TOKEN", G.PUBLIC_API)
    assert ok.api_base == G.PUBLIC_API
    stored = tag("pasted-for-ghes", {"id": "sref_1", "ref_kind": "vault", "storage_backend": "vault",
                                     "scope_type": "project", "scope_id": "p_1",
                                     "project_id": "p_1", "ref_key": "BOSS_P_1_GITHUB_TOKEN",
                                     "metadata": {}}, GHES, project_id="p_1")
    # no recorded owner can be read without a database: never the connection's host
    assert stored.api_base == G.PUBLIC_API


def test_a_connection_may_not_pair_an_env_reference_with_another_host():
    from boss_engine.services.github_connections import _refuse_env_ref_on_another_host

    with pytest.raises(ValueError):
        _refuse_env_ref_on_another_host("env:BOSS_GITHUB_TOKEN", EVIL)
    _refuse_env_ref_on_another_host("env:BOSS_GITHUB_TOKEN", G.PUBLIC_API)
    _refuse_env_ref_on_another_host("secret:abc", GHES)


def test_an_existing_env_row_on_evil_never_sends_the_install_token_there(eng, monkeypatch):
    """The exact repro, for a row written before the refusal existed."""
    from boss_engine.config import get_settings
    from boss_engine.services import repo_host
    from boss_engine.services.github_connections import GitHubConnectionService

    project = make_project(eng)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
            "credential_ref, status, api_base_url, created_by) VALUES (:i, :p, 'evil', "
            "'personal_access_token', 'env:BOSS_GITHUB_TOKEN', 'active', :b, 'ceo')"),
            {"i": f"ghc_{project}", "p": project, "b": EVIL})
        c.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
            "full_name, role, created_by) VALUES (:b, :p, :c, 'acme', 'web', 'acme/web', "
            "'primary', 'ceo')"), {"b": f"ghb_{project}", "p": project, "c": f"ghc_{project}"})
    service = GitHubConnectionService(get_settings())
    monkeypatch.setattr(service, "_resolve_connection_secret",
                        lambda *a, **k: "install-token-never-to-evil")
    with eng.begin() as c:
        try:
            token = service.token_for_repository(c, project_id=project, owner="acme", repo="web")
        except Exception:  # noqa: BLE001 — refused outright is fine too
            token = None
    if token:
        assert isinstance(token, G.GitHubCredential) and token.api_base == G.PUBLIC_API
        with pytest.raises(G.CrossHostCredential):
            repo_host.git_access("github", "acme", "web", token,
                                 web_base=G.web_base_for(EVIL))


def test_a_ghes_connection_with_its_own_app_can_mint(monkeypatch):
    from boss_engine.services.github_adapter import GitHubAdapter
    from boss_engine.services.github_app_adapter import GitHubAppAdapter

    key = G.tag("-----BEGIN PRIVATE KEY----- ghes-app", GHES)
    settings = SimpleNamespace(github_app_private_key=key, github_app_private_key_path=None,
                               github_app_id="9", github_app_installation_id="1",
                               github_token=None, target_github_owner="", target_github_repo="")
    app = GitHubAppAdapter.__new__(GitHubAppAdapter)
    app.settings, app.api_base_url = settings, GHES
    app._check_jwt_host()                        # noqa: SLF001 — its own host: allowed
    install_key = SimpleNamespace(**{**vars(settings), "github_app_private_key": "install-key"})
    app.settings = install_key
    with pytest.raises(G.CrossHostCredential):   # the install's App never mints on GHES
        app._check_jwt_host()  # noqa: SLF001
    # The adapter for that connection asks its own App, not "no credential for this host".
    minted = []
    monkeypatch.setattr("boss_engine.services.github_app_adapter.repo_token",
                        lambda s, o, r, **k: minted.append(k.get("api_base_url"))
                        or G.tag("ghs_ghes_installation", GHES))
    adapter = GitHubAdapter(settings, owner="platform", repo="gw", api_base_url=GHES)
    assert adapter._auth_token().api_base == GHES and minted == [GHES]  # noqa: SLF001


# ---- the allow-list, as a table: every backend x every scope (sixth review) -------------------

OWNED_BY_P1 = {"owner_scope_type": "project", "owner_scope_id": "p_1"}


def _record(backend, scope_type="project", project="p_1", key=None, **meta):
    return {"id": "sref_1", "ref_kind": backend, "storage_backend": backend,
            "scope_type": scope_type, "scope_id": project if scope_type == "project" else "org_1",
            "project_id": project, "ref_key": key or "BOSS_P_1_GITHUB_TOKEN",
            "metadata": dict(meta)}


@pytest.mark.parametrize("backend", sorted(
    __import__("boss_engine.services.secret_refs", fromlist=["x"]).SecretRefService
    .ALLOWED_STORAGE_BACKENDS | {"some_future_backend", ""}) + [None])
@pytest.mark.parametrize("scope", ["project", "org", "workspace"])
def test_only_a_boss_stored_project_secret_takes_a_connections_host(backend, scope):
    """THE TABLE: a connection's base is taken ONLY by a project-scoped secret in one of BOSS's own
    stores, written for that project. Every other backend (env, local_runner, identity_github,
    anything added later) and every org/workspace scope is the install's."""
    from boss_engine.services.github_connections import OWN_SECRET_STORES, credential_host_for

    got = credential_host_for(_record(backend, scope), GHES, "p_1", OWNED_BY_P1)
    expected = GHES if (backend in OWN_SECRET_STORES and scope == "project") else ""
    assert got == expected, (backend, scope, got)


def test_the_allow_list_also_needs_the_projects_own_key_and_named_host():
    from boss_engine.services.github_connections import credential_host_for

    assert credential_host_for(_record("vault"), GHES, "p_2", OWNED_BY_P1) == ""    # other project
    assert credential_host_for(_record("vault"), GHES, "p_1", None) == ""   # no recorded owner
    assert credential_host_for(_record("vault"), GHES, "p_1",
                               {"owner_scope_type": "project", "owner_scope_id": "p_9"}) == ""
    assert credential_host_for(_record("vault", allowed_hosts=["ghe.other.example"]), GHES,
                               "p_1", OWNED_BY_P1) == ""
    assert credential_host_for(_record("vault", allowed_hosts=["ghe.northgate.example"]), GHES,
                               "p_1", OWNED_BY_P1) == GHES
    assert credential_host_for(None, GHES, "p_1") == ""


def test_an_untagged_copy_of_the_install_token_is_never_tagged_for_another_host(monkeypatch):
    monkeypatch.setenv("BOSS_GITHUB_TOKEN", "install-token-never-to-evil")
    with pytest.raises(G.CrossHostCredential):
        G.tag("install-token-never-to-evil", EVIL)
    G.register_install_credential("dotenv-loaded-install-token")
    with pytest.raises(G.CrossHostCredential):
        G.tag("dotenv-loaded-install-token", GHES)
    assert G.tag("a-ghes-pasted-token", GHES).api_base == GHES


def test_the_install_repository_token_keeps_its_tag():
    from boss_engine.services.setup_view import install_repository

    settings = SimpleNamespace(target_github_owner="acme", target_github_repo="web",
                               github_owner="", github_repo="", github_token=" install-token ")
    _o, _n, token = install_repository(settings)
    assert isinstance(token, G.GitHubCredential) and token.api_base == G.PUBLIC_API


def test_a_host_file_ref_on_another_host_is_refused_at_create_and_repoint(monkeypatch):
    from boss_engine.services.github_connections import GitHubConnectionService

    check = GitHubConnectionService._check_connection_secret  # noqa: SLF001
    host_file = {"provider": "github", "secret_kind": "private_key", "ref_kind": "local_runner",
                 "storage_backend": "local_runner", "scope_type": "project", "scope_id": "p_1",
                 "project_id": "p_1", "ref_key": "/etc/boss/github-app.pem", "metadata": {}}
    with pytest.raises(ValueError):
        check(host_file, "github_app", "9", "1", EVIL, "p_1")
    check(host_file, "github_app", "9", "1", G.PUBLIC_API, "p_1")          # install host: fine
    own = {**_record("vault"), "provider": "github", "secret_kind": "private_key"}
    with pytest.raises(ValueError):                     # no recorded owner: not this project's
        check(own, "github_app", "9", "1", GHES, "p_1")
    from boss_engine.services import secrets_service

    monkeypatch.setattr(secrets_service, "material_owner", lambda c, b, k: OWNED_BY_P1)
    check(own, "github_app", "9", "1", GHES, "p_1", object())     # recorded as p_1's: allowed


def test_the_real_adapter_path_mints_with_its_own_app_and_refuses_the_install_key(monkeypatch):
    """The REAL _make_adapter → GitHubAppAdapter JWT path with a GHES base: a project-stored App
    key mints on its GHES; the install App's key (a host file) on an evil base is refused."""
    from boss_engine.config import get_settings
    from boss_engine.services.github_connections import GitHubConnectionService

    service = GitHubConnectionService(get_settings())
    made = []
    service.adapter_factory = lambda settings, **kw: made.append((settings, kw)) or (settings, kw)
    for record, base, ok in (
            ({**_record("vault"), "provider": "github", "secret_kind": "private_key"}, GHES, True),
            ({"provider": "github", "secret_kind": "private_key", "ref_kind": "local_runner",
              "storage_backend": "local_runner", "scope_type": "project", "scope_id": "p_1",
              "project_id": "p_1", "ref_key": "/etc/boss/github-app.pem", "metadata": {}},
             EVIL, False)):
        monkeypatch.setattr(service, "_credential_ref", lambda *a, **k: "x:y")
        monkeypatch.setattr(service.secret_refs, "require_usable", lambda *a, **k: record)
        monkeypatch.setattr(service, "_resolve_secret", lambda *a, **k: "-----PEM-----")
        import contextlib

        from boss_engine.services import secrets_service

        # The material's RECORDED owner: the project for its own vault secret; none for a file.
        monkeypatch.setattr(secrets_service, "material_owner",
                            lambda c, b, k: OWNED_BY_P1 if b == "vault" else None)
        db = SimpleNamespace(begin_nested=contextlib.nullcontext)
        settings, kw = service._make_adapter(  # noqa: SLF001
            db, project_id="p_1",
            github_connection={"id": "c", "auth_method": "github_app", "app_id": "9",
                               "installation_id": "1", "api_base_url": base},
            binding={"full_name": "platform/gw", "owner": "platform", "name": "gw"})
        from boss_engine.services.github_app_adapter import GitHubAppAdapter

        app = GitHubAppAdapter.__new__(GitHubAppAdapter)
        app.settings, app.api_base_url = settings, base
        if ok:
            app._check_jwt_host()  # noqa: SLF001 — its own App mints on its own GHES
        else:
            with pytest.raises(G.CrossHostCredential):
                app._check_jwt_host()  # noqa: SLF001 — the install's key never goes to evil


def test_a_project_cannot_read_another_scopes_material_by_naming_its_key(eng, monkeypatch):
    """Finding 2: secret_material is keyed globally. A reference into a global-key store can be
    CREATED only for its own scope's keys, and the store's master key never resolves."""
    from boss_engine.config import get_settings
    from boss_engine.services import secret_backends
    from boss_engine.services.secret_refs import SecretRefService

    owner, thief = make_project(eng), make_project(eng)
    owner_key = f"BOSS_{owner.upper()}_GITHUB_TOKEN"
    refs = SecretRefService()
    with eng.begin() as c, pytest.raises(ValueError):
        refs.create_secret_ref(c, project_id=thief, ref_kind="database", ref_key=owner_key,
                               provider="github", status="active", metadata={}, actor="ceo")
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, ref_key, "
            "provider, storage_backend, created_by) VALUES (:i, :p, 'project', :p, 'database', "
            "'__boss_master_key__', 'github', 'database', 'ceo')"), {"i": f"sref_m_{thief}", "p": thief})
    with eng.begin() as c, pytest.raises(secret_backends.SecretUnavailable):
        secret_backends.resolve_secret_ref(c, f"sref_m_{thief}", get_settings(), project_id=thief)


# ---- ownership is RECORDED, never read off a key's name (seventh review) ----------------------

def test_names_that_fold_together_never_share_an_owner(eng):
    """acme vs acme-prod (a prefix), acme-prod vs acme_prod (the same folded key): the owner is
    the scope BOSS recorded when it wrote the material, so neither can read, re-point or overwrite
    the other's — and credential_host_for never gives the other's host."""
    from boss_engine.config import get_settings
    from boss_engine.services import secret_backends, secrets_service
    from boss_engine.services.github_connections import credential_host_for

    acme, acme_prod = make_project(eng), make_project(eng)
    with eng.begin() as c:
        secrets_service.record_material_owner(c, "database", "BOSS_ACME_PROD_GITHUB_TOKEN",
                                              "project", acme_prod)
        owner = secrets_service.material_owner(c, "database", "BOSS_ACME_PROD_GITHUB_TOKEN")
    rec = {"id": "s", "storage_backend": "database", "scope_type": "project", "scope_id": acme,
           "project_id": acme, "ref_key": "BOSS_ACME_PROD_GITHUB_TOKEN", "metadata": {}}
    assert credential_host_for(rec, GHES, acme, owner) == ""          # prefix: not acme's
    with eng.begin() as c, pytest.raises(secrets_service.MaterialOwnedElsewhere):
        secrets_service.record_material_owner(c, "database", "BOSS_ACME_PROD_GITHUB_TOKEN",
                                              "project", f"{acme}_other")   # a folded twin
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, ref_key, "
            "provider, storage_backend, created_by) VALUES (:i, :p, 'project', :p, 'database', "
            "'BOSS_ACME_PROD_GITHUB_TOKEN', 'github', 'database', 'ceo')"), {"i": f"s_{acme}", "p": acme})
    with eng.begin() as c, pytest.raises(secret_backends.SecretUnavailable):   # read time too
        secret_backends.resolve_secret_ref(c, f"s_{acme}", get_settings(), project_id=acme)


def test_material_with_no_recorded_owner_is_nobodys_own(eng):
    from boss_engine.config import get_settings
    from boss_engine.services import secret_backends
    from boss_engine.services.secret_refs import SecretRefService

    project = make_project(eng)
    with eng.begin() as c, pytest.raises(ValueError):
        SecretRefService().create_secret_ref(
            c, project_id=project, ref_kind="database", ref_key=f"BOSS_{project.upper()}_LEGACY",
            provider="github", status="active", metadata={}, actor="ceo")
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, ref_key, "
            "provider, storage_backend, created_by) VALUES (:i, :p, 'project', :p, 'database', "
            "'BOSS_NOBODY_LEGACY', 'github', 'database', 'ceo')"), {"i": f"l_{project}", "p": project})
    with eng.begin() as c, pytest.raises(secret_backends.SecretUnavailable):
        secret_backends.resolve_secret_ref(c, f"l_{project}", get_settings(), project_id=project)


@pytest.mark.parametrize("slug", ["subscription", "llm-gateway", "llm_gateway", "Subscription"])
def test_a_project_may_not_be_named_onto_an_install_secret_scope(slug):
    from boss_engine.bootstrap import project_slug_for

    with pytest.raises(ValueError):
        project_slug_for(slug, slug.lower() if "_" not in slug else None)

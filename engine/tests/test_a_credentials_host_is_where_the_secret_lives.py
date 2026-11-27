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


# ---- THE RULE, as a table: every backend x scope x pre-existing ref (eighth review) ----------

SLOT_KEY = "ghconn-sref_1"


def _record(backend, scope_type="project", project="p_1", key=None, slot=None, **meta):
    md = dict(meta)
    if slot is not None:
        md["connection_slot"] = slot
    return {"id": "sref_1", "ref_kind": backend, "storage_backend": backend,
            "scope_type": scope_type, "scope_id": project if scope_type == "project" else "org_1",
            "project_id": project, "ref_key": key or (SLOT_KEY if slot else "BOSS_P_1_GITHUB_TOKEN"),
            "metadata": md}


BOUND = {"connection_id": "ghc_1", "project_id": "p_1"}
BACKENDS = sorted(__import__("boss_engine.services.secret_refs", fromlist=["x"]).SecretRefService
                  .ALLOWED_STORAGE_BACKENDS | {"some_future_backend", ""}) + [None]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("scope", ["project", "org", "workspace"])
@pytest.mark.parametrize("origin", ["named", "pre_existing", "pending", "other_connection",
                                    "other_project", "bound_named_key", "bound"])
def test_only_a_slot_bound_to_this_connection_takes_its_host(backend, scope, origin):
    """THE RULE: a connection's non-install host ONLY for a slot its own create/repoint/rotate
    flow wrote and bound to (connection_id, project). Every named ref (database, local_vault,
    vault, infisical, platform, env, local_runner…), any scope, anything that existed before the
    rule, a slot still pending, another connection's or another project's slot: the install's."""
    from boss_engine.services.github_connections import credential_host_for

    slot = {"named": None, "pre_existing": None, "pending": {"pending": True, "project_id": "p_1"},
            "other_connection": {"connection_id": "ghc_2", "project_id": "p_1"},
            "other_project": {"connection_id": "ghc_1", "project_id": "p_2"},
            "bound_named_key": BOUND, "bound": BOUND}[origin]
    # A binding on a name-derived key (ref_key_for's BOSS_… form) is not in slot form: install.
    key = {"pre_existing": "db/sref_1", "bound_named_key": "BOSS_ACME_GITHUB_GITHUB_CI"}.get(origin)
    got = credential_host_for(_record(backend, scope, key=key, slot=slot), GHES, "p_1", "ghc_1")
    from boss_engine.services.github_connections import SLOT_STORES

    expected = GHES if (origin == "bound" and scope == "project"
                        and backend in SLOT_STORES) else ""
    assert got == expected, (backend, scope, origin, got)
    # Without the connection asking, nothing takes a non-install host.
    assert credential_host_for(_record(backend, scope, slot=slot), GHES, "p_1", None) == ""


def test_a_person_cannot_forge_the_binding():
    from boss_engine.services.secret_refs import SecretRefService

    for md in ({"connection_slot": BOUND}, {"Connection_Slot ": BOUND}):
        with pytest.raises(ValueError):
            SecretRefService._validate_metadata(md)  # noqa: SLF001


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


@pytest.mark.parametrize("backend", ["database", "local_vault", "vault", "infisical",
                                     "platform_secret_store", "env", "local_runner"])
def test_create_and_repoint_refuse_any_named_ref_on_another_host(backend):
    from boss_engine.services.github_connections import GitHubConnectionService

    check = GitHubConnectionService._check_connection_secret  # noqa: SLF001
    named = {**_record(backend), "provider": "github", "secret_kind": "private_key"}
    with pytest.raises(ValueError) as refused:
        check(named, "github_app", "9", "1", GHES, "p_1")
    assert ("saved through this connection" in str(refused.value)
            or "install's own GitHub credential" in str(refused.value))
    if backend == "env":
        return                       # the install's own credential: never another host at all
    check(named, "github_app", "9", "1", G.PUBLIC_API, "p_1")           # install host: fine
    # Even a binding on a name-derived key is refused: only the slot key form counts.
    with pytest.raises(ValueError):
        check({**named, "metadata": {"connection_slot": BOUND}}, "github_app", "9", "1", GHES,
              "p_1", "ghc_1")
    slot = {**named, "ref_key": SLOT_KEY}
    pending = {**slot, "metadata": {"connection_slot": {"pending": True, "project_id": "p_1"}}}
    if backend == "local_runner":       # a host file is never a connection slot
        with pytest.raises(ValueError):
            check(pending, "github_app", "9", "1", GHES, "p_1", fresh=True)
        return
    check(pending, "github_app", "9", "1", GHES, "p_1", fresh=True)  # its own flow's fresh slot
    with pytest.raises(ValueError):    # a pending slot a failed flow left behind: no one's
        check(pending, "github_app", "9", "1", GHES, "p_1")
    bound = {**slot, "metadata": {"connection_slot": BOUND}}
    check(bound, "github_app", "9", "1", GHES, "p_1", "ghc_1")  # repoint: still its own
    with pytest.raises(ValueError):
        check(bound, "github_app", "9", "1", GHES, "p_1", "ghc_2")  # another connection's


def test_the_real_adapter_path_mints_with_its_own_slot_and_refuses_the_rest(monkeypatch):
    """The REAL _make_adapter → GitHubAppAdapter JWT path with a GHES base: an App key in the
    connection's own bound slot mints on its GHES; a named ref (even a project vault secret) is
    refused with the plain "re-save" message; the install's host file never goes to evil."""
    import contextlib

    from boss_engine.config import get_settings
    from boss_engine.services.github_app_adapter import GitHubAppAdapter
    from boss_engine.services.github_connections import (GitHubConnectionService,
                                                        GitHubConnectionStateError)

    service = GitHubConnectionService(get_settings())
    service.adapter_factory = lambda settings, **kw: (settings, kw)
    cases = (
        ({**_record("vault", slot=BOUND), "provider": "github", "secret_kind": "private_key"},
         GHES, "mint"),
        ({**_record("vault"), "provider": "github", "secret_kind": "private_key"}, GHES, "resave"),
        ({**_record("local_runner", key="/etc/boss/github-app.pem"), "provider": "github",
          "secret_kind": "private_key"}, EVIL, "resave"),
    )
    for record, base, outcome in cases:
        monkeypatch.setattr(service, "_credential_ref", lambda *a, **k: "x:y")
        monkeypatch.setattr(service.secret_refs, "require_usable", lambda *a, r=record, **k: r)
        monkeypatch.setattr(service, "_resolve_secret", lambda *a, **k: "-----PEM-----")
        db = SimpleNamespace(begin_nested=contextlib.nullcontext)
        call = lambda: service._make_adapter(  # noqa: E731, SLF001
            db, project_id="p_1",
            github_connection={"id": "ghc_1", "auth_method": "github_app", "app_id": "9",
                               "installation_id": "1", "api_base_url": base},
            binding={"full_name": "platform/gw", "owner": "platform", "name": "gw"})
        if outcome == "resave":
            with pytest.raises((GitHubConnectionStateError, G.CrossHostCredential)) as refused:
                call()
            if isinstance(refused.value, GitHubConnectionStateError):
                assert "Re-save this connection's token" in str(refused.value)
            continue
        settings, _kw = call()
        app = GitHubAppAdapter.__new__(GitHubAppAdapter)
        app.settings, app.api_base_url = settings, base
        app._check_jwt_host()  # noqa: SLF001 — its own slot mints on its own GHES


def test_a_project_cannot_read_the_master_key_or_another_refs_slot(eng):
    from boss_engine.config import get_settings
    from boss_engine.services import secret_backends

    thief = make_project(eng)
    with eng.begin() as c:
        for i, key in ((f"sref_m_{thief}", "__boss_master_key__"),
                       (f"sref_s_{thief}", "db/sref_someone_else")):
            c.execute(text(
                "INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, "
                "ref_key, provider, storage_backend, created_by) VALUES (:i, :p, 'project', :p, "
                "'database', :k, 'github', 'database', 'ceo')"), {"i": i, "p": thief, "k": key})
    for i in (f"sref_m_{thief}", f"sref_s_{thief}"):
        with eng.begin() as c, pytest.raises(secret_backends.SecretUnavailable):
            secret_backends.resolve_secret_ref(c, i, get_settings(), project_id=thief)


def test_the_connection_flow_binds_the_slot_it_wrote(eng):
    """store_token_credential writes a pending slot; create binds it to (connection, project) at
    write time; a token then resolves tagged with that GHES — and a hand-made ref does not."""
    from boss_engine.config import get_settings
    from boss_engine.services.github_connections import (CONNECTION_SLOT_KEY,
                                                        GitHubConnectionService)

    project = make_project(eng)
    service = GitHubConnectionService(get_settings())
    with eng.begin() as c:
        ref = service.store_token_credential(c, project_id=project, token="ghes-pasted-token",
                                             auth_method="personal_access_token",
                                             label="northgate", actor="ceo")
        kind, _, key = ref.partition(":")
        md = c.execute(text("SELECT metadata FROM secret_refs WHERE project_id=:p AND "
                            "ref_kind=:k AND ref_key=:key"),
                       {"p": project, "k": kind, "key": key}).scalar_one()
        assert md[CONNECTION_SLOT_KEY] == {"pending": True, "project_id": project}
        service._bind_connection_slot(c, project_id=project, credential_ref=ref,  # noqa: SLF001
                                      connection_id="ghc_x")
        md = c.execute(text("SELECT metadata FROM secret_refs WHERE project_id=:p AND "
                            "ref_kind=:k AND ref_key=:key"),
                       {"p": project, "k": kind, "key": key}).scalar_one()
        assert md[CONNECTION_SLOT_KEY] == {"connection_id": "ghc_x", "project_id": project}
        # Another flow (no fresh slot of its own) can neither bind nor re-bind it.
        GitHubConnectionService(get_settings())._bind_connection_slot(  # noqa: SLF001
            c, project_id=project, credential_ref=ref, connection_id="ghc_thief")
        service._bind_connection_slot(c, project_id=project, credential_ref=ref,  # noqa: SLF001
                                      connection_id="ghc_thief")
        md = c.execute(text("SELECT metadata FROM secret_refs WHERE project_id=:p AND "
                            "ref_kind=:k AND ref_key=:key"),
                       {"p": project, "k": kind, "key": key}).scalar_one()
        assert md[CONNECTION_SLOT_KEY]["connection_id"] == "ghc_x"     # never re-bound


# ---- a slot's key is unique: no name, project or label reaches it (ninth review) -------------

def _named_project(eng, pid):
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_ops2', 'ops2') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES "
                       "('ws_ops2', 'org_ops2', 'ops2') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, 'ws_ops2', :i, :i)"), {"i": pid})
    return pid


def _connect(eng, service, project, label, value, base):
    """The connection's own flow: store the slot, write the connection row, bind."""
    with eng.begin() as c:
        ref = service.store_token_credential(c, project_id=project, token=value,
                                             auth_method="personal_access_token", label=label,
                                             actor="ceo")
        cid = f"ghc_{label.replace(' ', '_')}_{project}"
        c.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
            "credential_ref, status, api_base_url, created_by) VALUES (:i, :p, :l, "
            "'personal_access_token', :r, 'active', :b, 'ceo')"),
            {"i": cid, "p": project, "l": label, "r": ref, "b": base})
        service._bind_connection_slot(c, project_id=project, credential_ref=ref,  # noqa: SLF001
                                      connection_id=cid)
    return cid, ref


def _row(eng, project, ref):
    kind, _, key = ref.partition(":")
    with eng.begin() as c:
        return dict(c.execute(text(
            "SELECT id, ref_kind, ref_key, storage_backend, project_id, scope_type, metadata "
            "FROM secret_refs WHERE project_id=:p AND ref_kind=:k AND ref_key=:key"),
            {"p": project, "k": kind, "key": key}).mappings().one())


def _material(ref):
    from boss_engine.config import get_settings
    from boss_engine.services.secret_backends import backend_for

    kind, _, key = ref.partition(":")
    return backend_for(kind, get_settings()).get(key)


def test_a_victims_rotation_never_reaches_an_attackers_bound_slot(eng):
    """The collision: ref_key_for("<u>_acme", "github_github_ci") ==
    ref_key_for("<u>_acme-github", "github_ci"). With name-derived keys the victim's re-save
    overwrote the attacker's bound slot, sending the victim's token to the attacker's GHES."""
    import uuid

    from boss_engine.config import get_settings
    from boss_engine.services.github_connections import (GitHubConnectionService,
                                                        credential_host_for)
    from boss_engine.services.secrets_service import ref_key_for

    u = uuid.uuid4().hex[:8]
    attacker = _named_project(eng, f"x{u}_acme")
    victim = _named_project(eng, f"x{u}_acme-github")
    assert ref_key_for(attacker, "github_github_ci") == ref_key_for(victim, "github_ci")

    service = GitHubConnectionService(get_settings())
    a_cid, a_ref = _connect(eng, service, attacker, "github ci", "attacker-own-value", GHES)
    v_cid, v_ref = _connect(eng, service, victim, "ci", "victim-first-value", G.PUBLIC_API)
    assert a_ref != v_ref
    for ref in (a_ref, v_ref):
        key = ref.partition(":")[2]
        assert key.startswith("ghconn-sref_") and not key.startswith("BOSS_")
    # The victim rotates its token, twice, through its own connection.
    for value in ("victim-second-value", "victim-third-value"):
        with eng.begin() as c:
            again = service.store_token_credential(
                c, project_id=victim, token=value, auth_method="personal_access_token",
                label="ci", actor="ceo", connection_id=v_cid)
            assert again == v_ref                            # re-saved in place
            service._bind_connection_slot(c, project_id=victim,  # noqa: SLF001
                                          credential_ref=again, connection_id=v_cid)
    assert _material(a_ref) == "attacker-own-value"          # never reached
    assert _material(v_ref) == "victim-third-value"
    # Both slots stay bound to their own connection (a re-save keeps the binding).
    assert credential_host_for(_row(eng, attacker, a_ref), GHES, attacker, a_cid) == GHES
    assert credential_host_for(_row(eng, victim, v_ref), GHES, victim, v_cid) == GHES
    assert credential_host_for(_row(eng, victim, v_ref), GHES, victim, a_cid) == ""


def test_a_second_ref_naming_a_slot_key_is_refused(eng):
    from boss_engine.config import get_settings
    from boss_engine.services import secret_backends
    from boss_engine.services.github_connections import GitHubConnectionService
    from boss_engine.services.secret_refs import SecretRefService

    owner, thief = make_project(eng), make_project(eng)
    service = GitHubConnectionService(get_settings())
    _cid, ref = _connect(eng, service, owner, "northgate", "owner-slot-value", GHES)
    kind, _, key = ref.partition(":")
    # Through the checked path: refused outright.
    with eng.begin() as c, pytest.raises(ValueError):
        SecretRefService().create_secret_ref(c, project_id=thief, ref_kind=kind, ref_key=key,
                                              provider="github", status="active", metadata={},
                                              actor="ceo")
    # A row that names it anyway (written before the rule): never reads it, never binds it.
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, ref_key, "
            "provider, storage_backend, created_by) VALUES (:i, :p, 'project', :p, :k, :key, "
            "'github', :k, 'ceo')"), {"i": f"sref_t_{thief}", "p": thief, "k": kind, "key": key})
    with eng.begin() as c, pytest.raises(secret_backends.SecretUnavailable):
        secret_backends.resolve_secret_ref(c, f"sref_t_{thief}", get_settings(), project_id=thief)
    with eng.begin() as c:
        fresh = service.store_token_credential(c, project_id=owner, token="v2",
                                               auth_method="personal_access_token",
                                               label="northgate", actor="ceo")
    service.__dict__["_fresh_slots"].add((str(owner), ref))      # even if the flow claimed it
    with eng.begin() as c, pytest.raises(ValueError):
        service._bind_connection_slot(c, project_id=owner, credential_ref=ref,  # noqa: SLF001
                                      connection_id="ghc_other")
    assert fresh != ref

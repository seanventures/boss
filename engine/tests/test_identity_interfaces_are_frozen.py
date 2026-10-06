"""The identity interfaces the five streams build against behave as frozen (IDENTITY_BUILD.md §2).

Covers what Phase 0 actually implements — the types, the declaration rules, scope resolution, the
enforcement core, the audit chain format, the vault AAD format — and pins that every stream-owned
function is still a stub that says whose it is, so nobody mistakes a stub for a feature.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from boss_engine.identity import (
    ActorChain,
    AgentInstance,
    AuthContext,
    AuthMethod,
    CatalogueError,
    Decision,
    DenyReason,
    P,
    Principal,
    PrincipalKind,
    PrincipalStatus,
    PublicReason,
    ResolvedScope,
    Scope,
    access_of,
    authenticated,
    permit,
    public,
)
from boss_engine.identity import audit, authn, authz, delegation, http, ids, legacy, policy, ratelimit, vault
from boss_engine.identity import scope as scope_module
from boss_engine.identity.enforcement import (
    LEDGER,
    AuthzMode,
    CoveragePolicy,
    RouteEntry,
    UndeclaredRoutesError,
    check_coverage,
    coverage_policy,
    evaluate,
    mode_for,
)
from boss_engine.identity.scope import RequestValues, ScopeNotFound, ScopeUnresolved

ALICE = Principal("usr_alice", PrincipalKind.USER, "org_boss", PrincipalStatus.ACTIVE, "Alice")


# ---- principals and the act chain ------------------------------------------------------------

def test_the_act_claim_nests_the_current_actor_outermost():
    chain = ActorChain((AgentInstance("agt_atlas", "run_8811"), AgentInstance("agt_forge", "run_8812")))
    assert chain.to_act_claim() == {"sub": "agt_forge#run_8812", "act": {"sub": "agt_atlas#run_8811"}}
    assert chain.to_boss_chain() == ["agt_atlas#run_8811", "agt_forge#run_8812"]
    assert ActorChain.from_act_claim(chain.to_act_claim()) == chain
    assert chain.current.agent_id == "agt_forge" and chain.depth == 2


def test_the_same_agent_twice_is_a_cycle_and_an_empty_chain_has_no_act():
    looped = ActorChain((AgentInstance("agt_a", "run_1"), AgentInstance("agt_a", "run_2")))
    assert looped.has_cycle
    assert ActorChain().to_act_claim() is None
    with pytest.raises(ValueError):
        AgentInstance.parse("usr_alice")


def test_only_a_person_with_a_session_counts_as_a_human_session():
    ctx = AuthContext(ALICE, AuthMethod.API_KEY)
    assert not ctx.is_human_session and ctx.on_behalf_of is None


# ---- declarations ------------------------------------------------------------------------------

def test_a_declaration_is_metadata_on_the_endpoint():
    @permit(P.WORKFLOW_RUN_CONTROL, scope=Scope.of("workflow_run", path="run_id"))
    def pause(run_id: str):
        return run_id

    access = access_of(pause)
    assert access.permission == "workflow_run:control" and pause("r") == "r"
    assert access.describe() == "permit workflow_run:control @ workflow_run(path=run_id)"


@pytest.mark.parametrize("make", [
    lambda: permit("workflow:launch", scope=Scope.org()),            # not in the catalogue
    lambda: permit(P.CODE_TASK_READ),                                # project-scoped, no resolver
    lambda: permit(P.USER_INVITE, scope=Scope.project(path="pid")),  # org-only at a project
    lambda: public(reason=PublicReason.HMAC_SIGNED_WEBHOOK),         # public without its verifier
])
def test_a_bad_declaration_fails_at_import_time(make):
    with pytest.raises((CatalogueError, ValueError)):
        make()


def test_an_operation_has_exactly_one_declaration():
    with pytest.raises(CatalogueError, match="exactly one"):
        @permit(P.SETTINGS_READ, scope=Scope.org())
        @authenticated()
        def both():
            return None


def test_public_reasons_are_a_closed_list():
    with pytest.raises(ValueError):
        public(reason="because", verifier=lambda: True)


def test_a_scope_resolver_names_exactly_one_location():
    with pytest.raises(ValueError):
        Scope.project(path="a", body="b")
    with pytest.raises(ValueError):
        Scope.of("code_task")


# ---- scope resolution ----------------------------------------------------------------------------

def test_scopes_resolve_from_path_body_and_query():
    values = RequestValues(path_params={"pid": "p1"}, query={"project_id": "p2"},
                           body={"project_id": "p3"})
    assert scope_module.resolve(Scope.org(), values, org_id="o").label() == "org"
    assert scope_module.resolve(Scope.project(path="pid"), values, org_id="o").project_id == "p1"
    assert scope_module.resolve(Scope.project(query="project_id"), values, org_id="o").project_id == "p2"
    assert scope_module.resolve(Scope.project(body="project_id"), values, org_id="o").project_id == "p3"
    with pytest.raises(ScopeUnresolved):
        scope_module.resolve(Scope.project(body="missing"), values, org_id="o")


def test_an_object_scope_comes_from_the_object_and_another_orgs_object_is_not_found():
    kind = "phase0_fixture_object"
    scope_module.register_object_kind(kind, lambda db, oid: {"x1": ("o", "p9"), "x2": ("other", "p1")}.get(oid))
    values = lambda oid: RequestValues(path_params={"id": oid}, query={"project_id": "p1"})  # noqa: E731
    resolved = scope_module.resolve(Scope.of(kind, path="id"), values("x1"), org_id="o")
    assert (resolved.project_id, resolved.object_id) == ("p9", "x1")   # not the query's p1
    for oid in ("x2", "nope"):
        with pytest.raises(ScopeNotFound):
            scope_module.resolve(Scope.of(kind, path="id"), values(oid), org_id="o")
    with pytest.raises(NotImplementedError):
        scope_module.resolve(Scope.of("never_registered_kind", path="id"), values("x1"), org_id="o")


# ---- the enforcement core --------------------------------------------------------------------------

def _evaluate(access, *, ctx=ALICE, check=None):
    def authenticate():
        if isinstance(ctx, Exception):
            raise ctx
        return None if ctx is None else AuthContext(ctx, AuthMethod.SESSION)
    return evaluate(
        access,
        authenticate=authenticate,
        values=lambda: RequestValues(path_params={"pid": "p1"}, query={}),
        org_id=lambda c: c.principal.org_id,
        check=check or (lambda c, perm, s: Decision.allow(perm, s)),
        resolve=lambda spec, v, org: scope_module.resolve(spec, v, org_id=org),
    )


def test_undeclared_is_denied_by_default_and_public_is_allowed_without_a_principal():
    assert _evaluate(None).reason == DenyReason.UNDECLARED_ROUTE
    hook = public(reason=PublicReason.LIVENESS)(lambda: None)
    assert _evaluate(access_of(hook), ctx=None).allowed


def test_a_stream_that_has_not_landed_is_not_evaluated_never_allowed():
    access = access_of(permit(P.SETTINGS_READ, scope=Scope.org())(lambda: None))
    out = _evaluate(access, ctx=NotImplementedError("S1"))
    assert not out.allowed and out.reason == DenyReason.NOT_EVALUATED and out.status == 503
    out = _evaluate(access, check=lambda *a: (_ for _ in ()).throw(NotImplementedError("S2")))
    assert out.reason == DenyReason.NOT_EVALUATED


def test_no_principal_is_401_an_inactive_one_too_and_a_deny_carries_the_permission():
    access = access_of(permit(P.SETTINGS_UPDATE, scope=Scope.project(path="pid"))(lambda: None))
    assert _evaluate(access, ctx=None).status == 401
    gone = Principal("usr_x", PrincipalKind.USER, "org_boss", PrincipalStatus.DISABLED, "X")
    assert _evaluate(access, ctx=gone).reason == DenyReason.PRINCIPAL_INACTIVE
    out = _evaluate(access, check=lambda c, perm, s: Decision.deny(perm, s, DenyReason.MISSING_PERMISSION))
    assert out.status == 403 and out.error_body() == {"error": "forbidden", "permission": "settings:update"}
    allowed = _evaluate(access)
    assert allowed.allowed and allowed.decision.scope.label() == "project:p1"


def test_modes_identity_routers_always_enforce_and_off_turns_everything_off():
    assert mode_for("main", global_mode="shadow") == AuthzMode.SHADOW
    assert mode_for("setup", global_mode="shadow", router_modes="setup=enforce") == AuthzMode.ENFORCE
    assert mode_for("identity_me", global_mode="shadow") == AuthzMode.ENFORCE
    assert mode_for("identity_me", global_mode="off") == AuthzMode.OFF
    assert coverage_policy(global_mode="shadow") == CoveragePolicy.WARN
    assert coverage_policy(global_mode="enforce") == CoveragePolicy.FAIL
    with pytest.raises(ValueError):
        mode_for("main", global_mode="shadow", router_modes="main=allow")


def test_coverage_warns_in_shadow_and_refuses_to_boot_on_fail():
    entries = [RouteEntry(("GET",), "/v1/x", "http", "main", None),
               RouteEntry(("GET",), "/health", "http", "main",
                          access_of(public(reason=PublicReason.LIVENESS)(lambda: None)))]
    lines: list[str] = []
    report = check_coverage(entries, CoveragePolicy.WARN, echo=lines.append)
    assert report.undeclared == ["GET /v1/x"] and "1 undeclared" in lines[0]
    with pytest.raises(UndeclaredRoutesError, match="GET /v1/x"):
        check_coverage(entries, CoveragePolicy.FAIL, echo=lambda _: None)


def test_the_ledger_counts_without_repeating_itself(capsys):
    LEDGER.clear()
    out = _evaluate(None)
    for _ in range(3):
        LEDGER.record(router_key="main", method="GET", path_template="/v1/x", outcome=out,
                      mode=AuthzMode.SHADOW)
    assert list(LEDGER.snapshot().values()) == [3]
    assert capsys.readouterr().out.count("would deny GET /v1/x") == 1
    LEDGER.clear()


# ---- audit chain and vault formats -----------------------------------------------------------------

def test_the_audit_chain_hash_is_deterministic_and_covers_only_the_row():
    event = audit.SecurityEvent(org_id="org_boss", event_type="auth.login.succeeded",
                                outcome=audit.Outcome.SUCCESS, actor_principal_id="usr_alice")
    ts = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    row = audit.event_row(event, seq=1, ts=ts)
    first = audit.chain_hash(audit.GENESIS_HASH, row)
    assert first == audit.chain_hash(audit.GENESIS_HASH, {**row, "hash": b"x", "id": 99})
    assert first != audit.chain_hash(audit.GENESIS_HASH, {**row, "outcome": "failure"})
    assert len(first) == 32 and "auth.login.succeeded" in audit.EVENT_TYPES


def test_vault_aad_binds_the_row_and_refuses_the_separator():
    assert vault.aad("vlt_1", "usr_alice", "github", "oauth_user") == b"vlt_1\x1fusr_alice\x1fgithub\x1foauth_user"
    assert vault.aad("vlt_1", None, None, "app_private_key") == b"vlt_1\x1f\x1f\x1fapp_private_key"
    with pytest.raises(ValueError):
        vault.aad("vlt\x1f1", None, None, "k")


def test_secrets_do_not_appear_in_reprs():
    assert "s3cret" not in repr(delegation.IssuedToken(access_token="s3cret"))
    assert "s3cret" not in repr(vault.ShortLivedCredential(token="s3cret"))


# ---- the stubs say whose they are ------------------------------------------------------------------

STUBS = [
    (lambda: authn.authenticate(None), "S1"),
    (lambda: authn.parse_api_key("boss_pat_x"), "S1"),
    (lambda: authz.check(None, "settings:read", ResolvedScope.org_level("o")), "S2"),
    (lambda: authz.effective_permissions(ALICE, ResolvedScope.org_level("o")), "S2"),
    (lambda: authz.explain("usr_alice", "settings:read", ResolvedScope.org_level("o")), "S2"),
    (lambda: delegation.exchange_token(None, htm="POST", htu="/", db=None), "S3"),
    (lambda: delegation.revoke_cascade(delegation.CascadeTrigger.GRANT_REVOKED, "dlg_1",
                                       actor=None, reason="", db=None), "S3"),
    (lambda: vault.get_credential(vault.ConnectionRef("github", "usr_alice"),
                                  vault.CredentialPurpose.GITHUB_RUN, ctx=None, db=None), "S3"),
    (lambda: audit.record(None, db=None), "S5"),
    (lambda: legacy.bridge(None), "S5"),
    (lambda: policy.get_policy("org_boss", db=None), "S5"),
    (lambda: http.fetch("GET", "https://example.com", purpose="test"), "S1"),
    (lambda: ratelimit.hit("signin.ip", "1.2.3.4", limit=1, window_seconds=60, db=None), "S1"),
]


def test_ids_carry_their_kind_and_policy_defaults_match_the_decisions():
    assert ids.new_id("usr").startswith("usr_") and len(ids.new_id("ses")) == 30
    with pytest.raises(ValueError):
        ids.new_id("password")
    defaults = policy.IdentityPolicy()
    assert defaults.signup_enabled is False and defaults.legacy_cutoff_days == 30   # D32, D25
    assert defaults.github_require_user_repo_access is None                         # D28: automatic


@pytest.mark.parametrize("call, stream", STUBS)
def test_every_stream_owned_function_is_a_named_stub(call, stream):
    with pytest.raises(NotImplementedError, match=stream):
        call()

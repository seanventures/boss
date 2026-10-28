"""Approval delegations (IDENTITY.md §6.15; identity/approval_delegations.py), against the real app
under enforce:

  * an Owner creates the grant once (step-up), and gets the delegate's key once;
  * the delegate approves a PRD gate in scope: approved_by is the person, via_delegate the delegate,
    and the audit names both;
  * out of scope (kind, project, tier), revoked and expired grants are 403;
  * a main merge, a deploy and a budget card are 403 whatever the grant says;
  * a checks-failing merge into the bound release branch needs `merge_to_release_branch`;
  * the per-day cap; the delegate reaches no other route;
  * a person's own approval, and a key with no delegation, are unchanged.
"""
from __future__ import annotations

import json
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity.principal import AuthContext, AuthMethod, CredentialRef, PrincipalKind

H = {"sec-fetch-site": "same-origin"}
PRD_GATE = {"action": "resume_workflow", "step_id": "approve_prd"}


@pytest.fixture
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.claimed()
    return w


@pytest.fixture
def owner(world):
    person = world.person("Olive Owner")
    world.bind(person, "owner")
    world.person("Second Person")          # two people: no single-operator waiver
    return person


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _decision(world, rec: dict, *, tier: int = 2, project: str | None = None) -> str:
    did = f"dec_{uuid.uuid4().hex[:10]}"
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO decisions (id, project_id, question, risk_tier, status, default_action, "
                        "recommendation) VALUES (:d, :p, 'Approve?', :t, 'pending', 'hold', CAST(:r AS jsonb))"),
                   {"d": did, "p": project or world.project_a, "t": tier, "r": json.dumps(rec)})
    return did


def _row(world, did: str) -> dict:
    with world.engine.connect() as db:
        return dict(db.execute(text("SELECT * FROM decisions WHERE id = :d"), {"d": did}).mappings().first())


def _grant(world, owner, **body):
    payload = {"kinds": ["prd"], "projects": [world.project_a], "days": 30, **body}
    with world.acting_as(world.ctx(owner)):
        out = _client().post("/v1/identity/me/approval-delegations", json=payload, headers=H)
    assert out.status_code == 201, out.text
    data = out.json()
    assert data["shown_once"] is True and data["secret"].startswith("boss_sak_")
    return data


def _delegate_ctx(world, data) -> AuthContext:
    from boss_engine.identity.grants import principal

    with world.engine.connect() as db:
        sva = principal(db, data["approval_delegation"]["delegate"]["id"])
    assert sva is not None and sva.kind == PrincipalKind.SERVICE_ACCOUNT
    cred = CredentialRef(id=data["key"]["id"], kind="sak", principal_id=sva.id,
                         scopes=frozenset({"decision:read", "decision:approve"}),
                         project_ids=frozenset(data["approval_delegation"]["projects"]))
    return AuthContext(sva, AuthMethod.API_KEY, credential=cred)


def _as(world, ctx, verb, did, **body):
    with world.acting_as(ctx):
        return _client().post(f"/v1/decisions/{did}/{verb}", json={"actor": "x", "note": "ok", **body}, headers=H)


def test_the_delegate_approves_a_prd_card_in_scope_and_both_identities_are_recorded(world, owner):
    data = _grant(world, owner)
    delegate = _delegate_ctx(world, data)
    did = _decision(world, PRD_GATE)
    out = _as(world, delegate, "approve", did, note="PRD reads right")
    assert out.status_code == 200, out.text
    card = out.json()
    row = _row(world, did)
    assert row["status"] == "approved"
    assert row["approved_by"] == owner.id                  # the person, not the delegate
    assert row["via_delegate"] == delegate.principal.id
    assert row["via_delegation"] == data["approval_delegation"]["id"]
    assert row["resolution_note"] == "PRD reads right"
    assert card["via_delegate"] == delegate.principal.id and card["approved_by"] == owner.id
    with world.engine.connect() as db:
        audit = db.execute(text(
            "SELECT actor_principal_id, on_behalf_of, details FROM identity_security_events "
            "WHERE org_id = :o AND event_type = 'decision.approved_by_delegate' AND target_id = :d"),
            {"o": world.org, "d": did}).mappings().first()
        event = db.execute(text("SELECT actor, data AS payload FROM events WHERE entity_id = :d "
                                "AND event_type = 'decision.approved'"), {"d": did}).mappings().first()
    assert audit is not None
    assert audit["actor_principal_id"] == delegate.principal.id and audit["on_behalf_of"] == owner.id
    assert audit["details"]["delegation"] == data["approval_delegation"]["id"]
    assert event["actor"] == owner.id and event["payload"]["via_delegate"] == delegate.principal.id


def test_the_delegate_rejects_in_scope_too(world, owner):
    delegate = _delegate_ctx(world, _grant(world, owner))
    did = _decision(world, PRD_GATE)
    out = _as(world, delegate, "reject", did)
    assert out.status_code == 200, out.text
    assert _row(world, did)["status"] == "rejected" and _row(world, did)["via_delegate"] == delegate.principal.id


def test_out_of_scope_is_403(world, owner):
    delegate = _delegate_ctx(world, _grant(world, owner))
    arch = _decision(world, {"action": "review_architecture"})
    out = _as(world, delegate, "approve", arch)
    assert out.status_code == 403 and out.json()["error"] == "delegation_out_of_scope", out.text
    other_project = _decision(world, PRD_GATE, project=world.project_b)
    out = _as(world, delegate, "approve", other_project)
    assert out.status_code == 403, out.text
    tier3 = _decision(world, PRD_GATE, tier=3)
    out = _as(world, delegate, "approve", tier3)
    assert out.status_code == 403 and out.json().get("scope") == "risk_tier", out.text
    for did in (arch, other_project, tier3):
        assert _row(world, did)["status"] == "pending"
    with world.engine.connect() as db:
        refused = db.execute(text("SELECT count(*) FROM identity_security_events WHERE org_id = :o "
                                  "AND event_type = 'decision.delegate_refused'"), {"o": world.org}).scalar()
    assert refused >= 2


def test_revoked_and_expired_delegations_are_403(world, owner):
    data = _grant(world, owner)
    delegate = _delegate_ctx(world, data)
    did = _decision(world, PRD_GATE)
    with world.acting_as(world.ctx(owner)):
        revoked = _client().post(f"/v1/identity/me/approval-delegations/{data['approval_delegation']['id']}/revoke",
                                 json={}, headers=H)
    assert revoked.status_code == 200 and revoked.json()["approval_delegation"]["status"] == "revoked"
    out = _as(world, delegate, "approve", did)
    assert out.status_code == 403 and out.json()["error"] == "delegation_revoked", out.text

    data2 = _grant(world, owner)
    with world.engine.begin() as db:
        db.execute(text("UPDATE identity_approval_delegations SET created_at = now() - interval '2 days', "
                        "expires_at = now() - interval '1 minute' WHERE id = :i"),
                   {"i": data2["approval_delegation"]["id"]})
    out = _as(world, _delegate_ctx(world, data2), "approve", did)
    assert out.status_code == 403 and out.json()["error"] == "delegation_expired", out.text
    assert _row(world, did)["status"] == "pending"


@pytest.mark.parametrize("rec,code", [
    ({"action": "review_pull_request", "pr_base": "main", "pull_request_url": "https://x/pr/1"},
     "main_merge_never_delegated"),
    ({"action": "review_pull_request", "pull_request_url": "https://x/pr/2"}, "main_merge_never_delegated"),
    ({"action": "review_deployment", "environment": "production"}, "deploy_never_delegated"),
    ({"action": "resume_workflow", "step_id": "approve_prd", "capability": "deploy"}, "deploy_never_delegated"),
    ({"action": "approve_initiative_budget"}, "budget_never_delegated"),
    ({"action": "budget_denied"}, "budget_never_delegated"),
    ({"action": "role.autonomy_change"}, "governance_never_delegated"),
])
def test_main_merge_deploy_budget_and_governance_cards_are_403_always(world, owner, rec, code):
    # The widest grant there is: every kind, every tier.
    data = _grant(world, owner, kinds=["prd", "architecture", "workflow_gate", "merge_to_release_branch"],
                  extra_risk_tiers=[3])
    did = _decision(world, rec, tier=1)
    out = _as(world, _delegate_ctx(world, data), "approve", did, force=True)
    assert out.status_code == 403 and out.json()["error"] == code, out.text
    assert _row(world, did)["status"] == "pending"


def _bind_release_branch(world, branch: str) -> None:
    with world.engine.begin() as db:
        cid = f"ghc_{uuid.uuid4().hex[:10]}"
        db.execute(text("INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
                        "credential_ref, created_by) VALUES (:i, :p, 't', 'personal_access_token', 'env:BOSS_TEST_NONE', 'test')"),
                   {"i": cid, "p": world.project_a})
        db.execute(text("INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
                        "full_name, default_branch, created_by) VALUES (:i, :p, :c, 'acme', 'app', 'acme/app', "
                        ":b, 'test')"), {"i": f"ghb_{uuid.uuid4().hex[:10]}", "p": world.project_a, "c": cid,
                                         "b": branch})


def test_a_checks_failing_merge_into_the_release_branch_needs_merge_to_release_branch(world, owner):
    from boss_engine.identity import approval_delegations as ad
    from boss_engine.identity.approvals import ApprovalRefused

    _bind_release_branch(world, "rel/rc1")
    merge = {"action": "review_pull_request", "pr_base": "rel/rc1", "pull_request_url": "https://x/pr/3",
             "checks_failing": {"gates": [{"gate": "code_review_passed"}]}}
    did = _decision(world, merge, tier=1)
    without = _delegate_ctx(world, _grant(world, owner, kinds=["prd", "workflow_gate"]))
    with_merge = _delegate_ctx(world, _grant(world, owner, kinds=["merge_to_release_branch"]))
    with world.engine.begin() as db:
        row = dict(db.execute(text("SELECT * FROM decisions WHERE id = :d"), {"d": did}).mappings().first())
        with pytest.raises(ApprovalRefused) as refused:
            ad.authorize(db, without, row, action="approve")
        assert refused.value.code == "delegation_out_of_scope"
        allowed = ad.authorize(db, with_merge, row, action="approve")
        assert allowed.kind == "merge_to_release_branch" and allowed.person_principal_id == owner.id
        # The same card into another branch is a hard limit, whatever the grant.
        with pytest.raises(ApprovalRefused) as other:
            ad.authorize(db, with_merge, {**row, "recommendation": {**merge, "pr_base": "feature/x"}},
                         action="approve")
        assert other.value.code == "main_merge_never_delegated"
        # ci_gate lets the delegate decide the checks-failing merge (forced, with its note).
        from boss_engine.services import ci_gate

        ci_gate.refuse_approval(db, row, owner.id, identity_ctx=with_merge, force=True, delegated=allowed)
        with pytest.raises(ci_gate.CiGateRefused):
            ci_gate.refuse_approval(db, row, owner.id, identity_ctx=with_merge, force=True, delegated=None)


def test_the_daily_cap(world, owner):
    delegate = _delegate_ctx(world, _grant(world, owner, daily_cap=1))
    first, second = _decision(world, PRD_GATE), _decision(world, PRD_GATE)
    assert _as(world, delegate, "approve", first).status_code == 200
    out = _as(world, delegate, "approve", second)
    assert out.status_code == 403 and out.json()["error"] == "delegation_daily_cap_reached", out.text


def test_the_delegate_reaches_no_other_route(world, owner):
    delegate = _delegate_ctx(world, _grant(world, owner))
    did = _decision(world, PRD_GATE)
    revise = _as(world, delegate, "revise", did)
    assert revise.status_code == 403, revise.text
    with world.acting_as(delegate):
        me = _client().get("/v1/identity/me/approval-delegations", headers=H)
        read = _client().get(f"/v1/decisions/{did}", headers=H)
    assert me.status_code == 403, me.text
    assert read.status_code == 200, read.text
    assert _row(world, did)["status"] == "pending"


def test_the_grant_rules(world, owner):
    operator = world.person("Opal Operator")
    world.bind(operator, "operator", project=world.project_a)
    with world.acting_as(world.ctx(operator)):
        refused = _client().post("/v1/identity/me/approval-delegations",
                                 json={"kinds": ["prd"], "projects": [world.project_a]}, headers=H)
    assert refused.status_code == 403, refused.text                  # Owner/Admin only
    with world.acting_as(world.ctx(owner, mfa=False)):
        stale = _client().post("/v1/identity/me/approval-delegations",
                               json={"kinds": ["prd"], "projects": [world.project_a]}, headers=H)
    assert stale.status_code == 401 and stale.json()["error"] == "step_up_required", stale.text
    with world.acting_as(world.ctx(owner)):
        long = _client().post("/v1/identity/me/approval-delegations",
                              json={"kinds": ["prd"], "projects": [world.project_a], "days": 91}, headers=H)
        bad_kind = _client().post("/v1/identity/me/approval-delegations",
                                  json={"kinds": ["deploy"], "projects": [world.project_a]}, headers=H)
        listed = _client().get("/v1/identity/me/approval-delegations", headers=H)
    assert long.status_code == 422 and long.json()["error"] == "expiry_too_long"
    assert bad_kind.status_code == 422 and bad_kind.json()["error"] == "invalid_kinds"
    assert listed.status_code == 200 and "secret" not in listed.text


def test_a_persons_own_approval_is_unchanged(world, owner):
    did = _decision(world, PRD_GATE)
    out = _as(world, world.ctx(owner), "approve", did)
    assert out.status_code == 200, out.text
    row = _row(world, did)
    assert row["approved_by"] == owner.id and row["via_delegate"] is None and row["via_delegation"] is None


def test_an_api_key_without_a_delegation_still_needs_a_person(world, owner):
    from boss_engine.identity import approvals

    did = _decision(world, PRD_GATE, tier=1)
    key = world.key_ctx(owner, ("decision:approve", "decision:read", "project:read"))
    by_key = _as(world, key, "approve", did)
    assert by_key.status_code in (401, 403), by_key.text
    sva = world.principal("Plain SA", kind=PrincipalKind.SERVICE_ACCOUNT)
    world.bind(sva, "operator", project=world.project_a)
    sa_key = world.key_ctx(sva, ("decision:approve", "decision:read", "project:read"))
    sa_key = AuthContext(sva, AuthMethod.API_KEY, credential=CredentialRef(
        id=sa_key.credential.id, kind="sak", principal_id=sva.id, scopes=sa_key.credential.scopes))
    with world.engine.begin() as db:
        db.execute(text("UPDATE identity_api_credentials SET kind = 'sak' WHERE id = :k"), {"k": sa_key.credential.id})
    by_sa = _as(world, sa_key, "approve", did)
    assert by_sa.status_code == 403 and by_sa.json()["error"] == "human_session_required", by_sa.text
    with world.engine.begin() as db, pytest.raises(approvals.ApprovalRefused) as refused:
        approvals.require_approval(db, sa_key, {"id": did, "risk_tier": 1})
    assert refused.value.code == "human_session_required"
    assert _row(world, did)["status"] == "pending"


def test_the_real_key_works_end_to_end(world, owner):
    """No faked authentication: the key shown once authenticates and approves."""
    from boss_engine.config import get_settings

    data = _grant(world, owner)
    did = _decision(world, PRD_GATE)
    settings = get_settings()
    saved = (settings.authz_mode, settings.authz_router_modes)
    settings.authz_mode, settings.authz_router_modes = "enforce", ""
    try:
        out = _client().post(f"/v1/decisions/{did}/approve", json={"actor": "x", "note": "nightly"},
                             headers={"X-BOSS-API-Key": data["secret"]})
    finally:
        settings.authz_mode, settings.authz_router_modes = saved
    assert out.status_code == 200, out.text
    row = _row(world, did)
    assert row["approved_by"] == owner.id and row["via_delegation"] == data["approval_delegation"]["id"]

"""An Owner holds every permission except an explicit, documented list.

A Seed Owner was answered 403 {"permission": "workflow_run:control"} when cancelling a run, which
read as "the Owner role lacks it". It does not: the request named no `project_id`, the guard could
not resolve the scope, and that malformed request was reported as a missing permission. Two pins:

  * the Owner role's only exclusions are the machine-only permissions below (each with why), so a
    permission added later reaches the Owner unless someone adds it here on purpose;
  * an unresolvable scope is answered 422 `scope_unresolved`, never a 403 naming a permission.
"""
from __future__ import annotations

from boss_engine.identity.declarations import Permit
from boss_engine.identity.enforcement import SCOPE_UNRESOLVED, evaluate
from boss_engine.identity.permissions_gen import PERMISSION_META, ROLE_PERMISSIONS
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus
from boss_engine.identity.scope import RequestValues, Scope, ScopeUnresolved

#: What an Owner does NOT hold, and why. Each is `system_only` in the catalogue: held only by the
#: built-in machine principals (worker, collector, scim). There is no break-glass permission.
OWNER_EXCLUSIONS = {
    "worker:lease": "the worker protocol: poll, heartbeat and complete leased jobs",
    "self_fix:ticket_read": "the host self-installer only: take signed self-fix install tickets",
    "self_fix:ticket_report": "the host self-installer only: report install phases",
    "spend:reserve": "budget reservation is done by the runtime for work, never by a person",
    "secret:lease": "a run's secret lease is issued to the runtime, never to a person",
    "telemetry:ingest": "collector telemetry, machine only",
    "scim:provision": "SCIM provisioning by the identity provider's service principal",
}


def test_the_owner_holds_every_permission_but_the_documented_exclusions():
    missing = set(PERMISSION_META) - set(ROLE_PERMISSIONS["owner"])
    assert missing == set(OWNER_EXCLUSIONS), (
        f"the Owner lacks {sorted(missing - set(OWNER_EXCLUSIONS))}; it holds everything a person "
        f"can. Unexpected exclusions are a catalogue bug (packages/identity/catalogue.yaml).")
    for key in OWNER_EXCLUSIONS:
        assert PERMISSION_META[key].system_only, f"{key} is excluded but a person could hold it"
    assert "workflow_run:control" in ROLE_PERMISSIONS["owner"]


def test_an_unresolvable_scope_is_a_422_not_a_missing_permission():
    owner = Principal("usr_owner", PrincipalKind.USER, "org_boss", PrincipalStatus.ACTIVE, "Owner")
    access = Permit("workflow_run:control", Scope.project(body="project_id"))
    checked: list[str] = []

    def resolve(spec, values, org):
        from boss_engine.identity import scope as scope_module
        return scope_module.resolve(spec, values, org_id=org)

    outcome = evaluate(
        access,
        authenticate=lambda: AuthContext(owner, AuthMethod.SESSION),
        values=lambda: RequestValues(path_params={"run_id": "wfr_1"}, query={}, body={"actor": "x"}),
        org_id=lambda ctx: ctx.principal.org_id,
        check=lambda ctx, permission, scope: checked.append(permission),  # never reached
        resolve=resolve,
    )
    assert not outcome.allowed and not checked
    assert outcome.status == 422
    body = outcome.error_body()
    assert body["error"] == SCOPE_UNRESOLVED and "permission" not in body
    assert "project_id" in body["detail"]
    assert issubclass(ScopeUnresolved, ValueError)

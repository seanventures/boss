"""Approval delegations: a person's named delegate clears approval gates for them, unattended
(IDENTITY.md §6.15; gap brec_f1de521177c6440c8c3b).

§6.13 condition 3 says only a person in a human session approves. An unattended run (an operator
away overnight, a scripted release train) then stops at every gate. An APPROVAL DELEGATION is the
person's own, revocable, bounded answer to that:

  * THE GRANT (`identity_approval_delegations`, ids `apd_…`). An Owner or Admin, in a human session
    with a fresh step-up (the create route declares `service_account:manage`: Owner/Admin, step-up,
    critical), names the decision KINDS (DELEGABLE_KINDS), the PROJECTS (each one they may approve
    in themselves), an EXPIRY (at most 90 days) and an optional per-day CAP. Step-up happens once,
    here, never at approve time.
  * THE DELEGATE. Creating it creates a service account ("Approves for <person>") owned by the person
    and ONE `boss_sak_` key, scopes {decision:read, decision:approve}, confined to the grant's
    projects and expiring with it. The key is returned ONCE in the create response
    (api_keys.issued: `shown_once`) and stored only as a hash; the person puts it where the
    unattended caller reads it (e.g. the install's host-tools env file). BOSS never shows it again.
  * THE DELEGATE'S AUTHORITY COMES ONLY FROM THE GRANT. The service account holds no role binding.
    The PDP (authz.check -> `pdp_decision`) answers for a delegate key: decision:read and
    decision:approve at the grant's projects, on the delegate routes only (DELEGATE_ROUTES), and
    nothing else, anywhere — not MCP, not the autonomy policy, not a security review.
  * AT APPROVE TIME (`authorize`, from decisions.transition, before anything is recorded) the grant
    is re-read: live (not revoked, not expired), the decision's project and kind in scope, the risk
    tier within the configured tier (or listed in the grant), the day's cap not spent, and none of
    the HARD LIMITS (`hard_limit`), which no grant can lift:
      - a merge to main / the default protected branch (a merge to the project's release branch is
        the `merge_to_release_branch` kind, and only when the PR's base IS that bound branch);
      - any deploy (a delegate never deploys to production; BOSS cannot always tell a deploy's
        environment, so every deploy card is refused);
      - any budget or spend increase, refusal or reconciliation;
      - any identity, security or settings change (autonomy, self-improvement, self-fix installs,
        policies, credentials, a CI change, a security review);
      - a release card (it ships to production).
  * THE RECORD. The decision's `approved_by` is the PERSON (as a person's own approval records it);
    `via_delegate` / `via_delegation` name the delegate and the grant; the note is kept. The audit
    records the delegate as the actor and the person as `on_behalf_of` (decision.approved_by_delegate
    / decision.rejected_by_delegate); every refusal is audited too (decision.delegate_refused).
  * REVOKE stops it at once (status, read at every request); expiry ends it.

A person's own approval, and every other API key, are unchanged: a key with no grant is still
refused `human_session_required`.
"""
from __future__ import annotations

import contextvars
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text

from .principal import AuthContext, PrincipalKind

#: The decision kinds a person may delegate. Anything else is out of scope (403).
DELEGABLE_KINDS = ("prd", "architecture", "workflow_gate", "merge_to_release_branch")
KIND_LABELS = {
    "prd": "PRD approvals",
    "architecture": "Architecture approvals",
    "workflow_gate": "Other workflow gates (not deploys)",
    "merge_to_release_branch": "Merges into the project's release branch",
}
MAX_DAYS = 90
DEFAULT_DAYS = 30
MAX_DAILY_CAP = 1000
#: What the delegate key may hold. Nothing else, whatever its scopes say.
DELEGATE_PERMISSIONS = frozenset({"decision:read", "decision:approve"})
#: The only routes a delegate key reaches (method, path template). Reads of a decision, and its
#: approve / reject. Everything else (MCP, revise, delegate, autonomy apply, security review) is
#: refused by the PDP before any handler runs.
DELEGATE_ROUTES = frozenset({
    ("POST", "/v1/decisions/{decision_id}/approve"),
    ("POST", "/v1/decisions/{decision_id}/reject"),
    ("GET", "/v1/decisions/{decision_id}"),
    ("GET", "/v1/decisions/inbox"),
})
#: Set by the API guard for each request: (method, path template). Read by `pdp_decision`.
CURRENT_ROUTE: contextvars.ContextVar[Optional[tuple[str, str]]] = contextvars.ContextVar(
    "boss_identity_current_route", default=None)
#: Branch names that are never a delegate's merge target, whatever the binding says.
PROTECTED_BRANCHES = frozenset({"main", "master", "trunk", "default", "production", "prod", "live",
                                "release", "stable"})
MAX_TIER_ENV = "BOSS_DELEGATED_APPROVAL_MAX_TIER"
DEFAULT_MAX_TIER = 2


def configured_max_tier() -> int:
    """The highest risk tier a delegate approves without the grant listing it (install setting
    BOSS_DELEGATED_APPROVAL_MAX_TIER, default 2; clamped to 0..3)."""
    try:
        return max(0, min(3, int(os.environ.get(MAX_TIER_ENV, DEFAULT_MAX_TIER))))
    except (TypeError, ValueError):
        return DEFAULT_MAX_TIER


class DelegationRefused(Exception):
    def __init__(self, code: str, status: int = 422, message: str = "", **extra: Any) -> None:
        super().__init__(code)
        self.code, self.status, self.message, self.extra = code, status, message, extra

    def body(self) -> dict[str, Any]:
        return {"error": self.code, **({"message": self.message} if self.message else {}), **self.extra}


@dataclass(frozen=True)
class ApprovalDelegation:
    id: str
    org_id: str
    person_principal_id: str
    delegate_principal_id: str
    credential_id: Optional[str]
    decision_kinds: tuple[str, ...]
    project_ids: tuple[str, ...]
    extra_risk_tiers: tuple[int, ...]
    daily_cap: Optional[int]
    status: str
    purpose: str
    created_at: datetime
    expires_at: datetime
    revoked_at: Optional[datetime]
    last_used_at: Optional[datetime]

    def live(self, now: Optional[datetime] = None) -> bool:
        return self.status == "active" and self.expires_at > (now or _now())

    def state(self, now: Optional[datetime] = None) -> str:
        if self.status == "revoked":
            return "revoked"
        return "active" if self.expires_at > (now or _now()) else "expired"


@dataclass(frozen=True)
class DelegatedApproval:
    """A delegate's approve/reject that `authorize` allowed: who it is FOR and who made it."""
    delegation_id: str
    person_principal_id: str
    person_actor: str          # the actor string the decision records (the person's)
    delegate_principal_id: str
    kind: str
    person_name: str = ""
    delegate_name: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _from_row(r: Mapping[str, Any]) -> ApprovalDelegation:
    return ApprovalDelegation(
        id=r["id"], org_id=r["org_id"], person_principal_id=r["person_principal_id"],
        delegate_principal_id=r["delegate_principal_id"], credential_id=r["credential_id"],
        decision_kinds=tuple(r["decision_kinds"] or ()), project_ids=tuple(r["project_ids"] or ()),
        extra_risk_tiers=tuple(int(t) for t in (r["extra_risk_tiers"] or ())), daily_cap=r["daily_cap"],
        status=r["status"], purpose=r["purpose"] or "", created_at=r["created_at"],
        expires_at=r["expires_at"], revoked_at=r["revoked_at"], last_used_at=r["last_used_at"])


def get(db: Any, delegation_id: str) -> Optional[ApprovalDelegation]:
    r = db.execute(text("SELECT * FROM identity_approval_delegations WHERE id = :i"),
                   {"i": delegation_id}).mappings().first()
    return _from_row(r) if r else None


def for_credential(db: Any, credential_id: Optional[str]) -> Optional[ApprovalDelegation]:
    if not credential_id:
        return None
    r = db.execute(text("SELECT * FROM identity_approval_delegations WHERE credential_id = :k"),
                   {"k": credential_id}).mappings().first()
    return _from_row(r) if r else None


def for_person(db: Any, person_id: str) -> list[ApprovalDelegation]:
    rows = db.execute(text("SELECT * FROM identity_approval_delegations WHERE person_principal_id = :p "
                           "ORDER BY created_at DESC"), {"p": person_id}).mappings().all()
    return [_from_row(r) for r in rows]


def is_delegate_context(db: Any, ctx: Optional[AuthContext]) -> bool:
    """A request made with an approval-delegate key (live or not)."""
    if ctx is None or ctx.credential is None or ctx.run_token is not None:
        return False
    if ctx.principal.kind != PrincipalKind.SERVICE_ACCOUNT:
        return False
    return for_credential(db, ctx.credential.id) is not None


def uses_today(db: Any, delegation_id: str) -> int:
    return int(db.execute(text(
        "SELECT count(*) FROM identity_approval_delegation_uses WHERE delegation_id = :d "
        "AND used_at > now() - interval '24 hours'"), {"d": delegation_id}).scalar() or 0)


# ---- what a decision is, for a delegate ---------------------------------------------------------

def _rec(row: Mapping[str, Any]) -> dict[str, Any]:
    rec = row.get("recommendation")
    if isinstance(rec, str):
        import json

        try:
            rec = json.loads(rec)
        except ValueError:
            rec = None
    return rec if isinstance(rec, dict) else {}


#: Recommendation actions a delegate never answers, by family (the hard limits).
_SPEND_ACTIONS = frozenset({
    "approve_initiative_budget", "create_budget_reservation", "reserve_budget_before_deployment",
    "reserve_provider_budget", "review_provider_spend_kill_switch", "review_cost_anomaly",
    "review_model_spend_and_host_effects_before_retry", "review_model_spend_and_receipts_before_retry",
    "typed_decision_reconciliation", "architecture_reconciliation", "conversation_reconciliation",
    "discovery_reconciliation", "reconcile_legacy_model_exposure", "reconcile_legacy_provider_exposure",
    "reconcile_provider_changeset", "verify_provider_outcome_before_retry",
    "verify_provider_outcome_then_settle_or_release", "typed_decision_lease_expired",
})
_DEPLOY_ACTIONS = frozenset({"review_deployment", "deploy_host", "deploy_stack", "infra_operation",
                             "review_infra_provisioning", "configure_or_enable_infra_provisioning",
                             "review_deployment_health", "review_rollback_or_retry"})
_GOVERNANCE_ACTIONS = frozenset({
    "role.autonomy_change", "self_improvement.begin_canary", "self_improvement.rollout",
    "self_fix.install", "self_fix.remove", "update_deployment_environment_policy",
    "upsert_comms_channel_policy", "provide_github_token", "provide_openai_api_key",
    "review_entitlement_sources", "configure_supported_channel",
    "reclassify_or_configure_approved_boundary_adapter", "set_BOSS_INFRA_REAL_PROVISION_ALLOWED",
    "github.community.merge_authorization", "github.community.dispatch_plan",
})
_MAIN_MERGE_ACTIONS = frozenset({"approve_change_set", "github.community.merge_authorization"})


def _word(value: Any) -> str:
    return str(value or "").strip().lower()


def bound_branches(db: Any, project_id: str) -> set[str]:
    """The project's bound branches (github_repository_bindings.default_branch, active)."""
    try:
        rows = db.execute(text(
            "SELECT default_branch FROM github_repository_bindings WHERE project_id = :p "
            "AND status = 'active' AND default_branch IS NOT NULL"), {"p": project_id}).scalars().all()
    except Exception:  # noqa: BLE001 — no binding table: nothing is a release branch
        return set()
    return {str(b).strip() for b in rows if str(b or "").strip()}


_IDENTIFIER = re.compile(r"^[a-z0-9_.:/-]+$")


def _identifier_blob(rec: Mapping[str, Any]) -> str:
    """The card's machine words, which the hard limits read: action, capability, step and a
    reason_code that is a code. NEVER the prose (reason, or a reason_code holding a sentence):
    every PRD gate explains itself as "approved by a person with the ceo seat … when identity
    is enforced", so reading prose refused every PRD gate as an identity change (2026-10-28,
    boss-1, dec_26d1f0b5: governance_never_delegated on a plain PRD approval)."""
    words = [_word(rec.get(k)) for k in ("action", "capability", "step_id")]
    code = _word(rec.get("reason_code"))
    if _IDENTIFIER.match(code):
        words.append(code)
    return " ".join(w for w in words if w)


def hard_limit(db: Any, row: Mapping[str, Any]) -> Optional[tuple[str, str]]:
    """(code, sentence) when NO grant may let a delegate answer this decision, else None.
    Enforced in code; nothing in a grant, a setting or the request lifts it."""
    from ..services import decision_actions

    rec = _rec(row)
    action = _word(rec.get("action"))
    raw_action = str(rec.get("action") or "")
    kind = decision_actions.kind_of({**row, "recommendation": rec})
    capability = _word(rec.get("capability"))
    blob = _identifier_blob(rec)
    if kind == "refusal" or "budget" in blob or "spend" in blob or raw_action in _SPEND_ACTIONS:
        return ("budget_never_delegated",
                "A delegate never answers a budget or spend decision. The person decides it.")
    if (kind == "deploy_gate" or raw_action in _DEPLOY_ACTIONS or "deploy" in capability
            or "deploy" in action or rec.get("environment") or rec.get("deployment_id")):
        return ("deploy_never_delegated",
                "A delegate never approves a deploy (never a production deploy). The person decides it.")
    try:
        from ..services import release_scope

        if release_scope.of_decision(rec) is not None:
            return ("release_never_delegated",
                    "A delegate never ships a release. The person decides it.")
    except Exception:  # noqa: BLE001
        pass
    if (raw_action in _GOVERNANCE_ACTIONS or rec.get("ci_changed") or rec.get("requires_person_approval")
            or any(w in blob for w in ("identity", "security", "secret", "policy", "setting",
                                       "autonomy", "permission", "credential"))
            or (raw_action == "execute_agent_tool"
                and _word((rec.get("args") or {}).get("kind")) == "security_review")):
        return ("governance_never_delegated",
                "A delegate never approves an identity, security or settings change. The person decides it.")
    if raw_action in _MAIN_MERGE_ACTIONS:
        return ("main_merge_never_delegated",
                "A delegate never merges to main or the default branch. The person decides it.")
    if raw_action == "review_pull_request":
        base = str(rec.get("pr_base") or "").strip()
        if not base or base.lower() in PROTECTED_BRANCHES or base not in bound_branches(
                db, str(row.get("project_id") or "")):
            return ("main_merge_never_delegated",
                    "A delegate merges only into the project's own release branch, never main or the "
                    f"default branch (this pull request targets {base or 'the default branch'}). "
                    "The person decides it.")
    return None


def kind_for(row: Mapping[str, Any]) -> Optional[str]:
    """Which of DELEGABLE_KINDS this decision is, or None (never delegable)."""
    from ..services import decision_actions

    rec = _rec(row)
    action = str(rec.get("action") or "")
    if action == "execute_agent_tool" and rec.get("tool") == "publish_document":
        doc_kind = _word((rec.get("args") or {}).get("kind")) or "prd"
        return "prd" if doc_kind == "prd" else None
    kind = decision_actions.kind_of({**row, "recommendation": rec})
    if kind == "architecture_review":
        return "architecture"
    if kind == "pull_request":
        return "merge_to_release_branch"
    if kind == "workflow_gate":
        step = _word(rec.get("step_id")) + " " + _word(rec.get("capability"))
        return "prd" if "prd" in step else "workflow_gate"
    return None


# ---- the PDP's answer for a delegate key (authz.check) --------------------------------------------

def pdp_decision(conn: Any, ctx: AuthContext, permission: str, scope: Any) -> Optional[Any]:
    """None when this is not a delegate key; else the PDP's Decision. The delegate holds only
    DELEGATE_PERMISSIONS, at the grant's projects, on DELEGATE_ROUTES; reads stop the moment the
    grant is not live, and an approve on a dead grant reaches `authorize` to be refused there by
    name (revoked / expired)."""
    from .authz import Decision, DenyReason, Why

    cred = ctx.credential
    if cred is None or ctx.run_token is not None or ctx.principal.kind != PrincipalKind.SERVICE_ACCOUNT:
        return None
    grant = for_credential(conn, cred.id)
    if grant is None:
        return None
    if (grant.org_id != ctx.principal.org_id or grant.delegate_principal_id != ctx.principal.id
            or permission not in DELEGATE_PERMISSIONS or permission not in cred.scopes):
        return Decision.deny(permission, scope, DenyReason.MISSING_PERMISSION, ("approval_delegation",))
    if CURRENT_ROUTE.get() not in DELEGATE_ROUTES:
        return Decision.deny(permission, scope, DenyReason.MISSING_PERMISSION, ("approval_delegation:route",))
    project = getattr(scope, "project_id", None)
    if project not in grant.project_ids:
        return Decision.deny(permission, scope, DenyReason.OUT_OF_SCOPE, ("approval_delegation",))
    if permission == "decision:read" and not grant.live():
        return Decision.deny(permission, scope, DenyReason.GRANT_INACTIVE, ("approval_delegation",))
    return Decision.allow(permission, scope, (Why(role_key="approval_delegation", binding_id=grant.id,
                                                  binding_scope=f"project:{project}" if project else "org"),))


# ---- approve time (decisions.transition) ----------------------------------------------------------

def _refuse(db: Any, ctx: AuthContext, row: Mapping[str, Any], grant: Optional[ApprovalDelegation],
            code: str, message: str, **extra: Any):
    from .approvals import ApprovalRefused
    from .audit import Outcome, SecurityEvent, record

    record(SecurityEvent.from_context(
        ctx, "decision.delegate_refused", Outcome.DENIED, target_type="decision",
        target_id=str(row.get("id") or ""),
        details={"reason": code, "delegation": grant.id if grant else None,
                 "on_behalf_of": grant.person_principal_id if grant else None,
                 "project": row.get("project_id")}), db=db)
    return ApprovalRefused(403, code, message, **extra)


def authorize(db: Any, ctx: AuthContext, row: Mapping[str, Any], *, action: str) -> DelegatedApproval:
    """A delegate key's approve/reject of `row`: the DelegatedApproval, or ApprovalRefused (403).
    Every condition is read now, from the database: revocation and expiry take effect at once.

    The refusal is audited in its own transaction (the caller's rolls back with the refusal)."""
    from .legacy import is_bridge_principal

    grant = for_credential(db, ctx.credential.id if ctx.credential else None)

    def refused(code: str, message: str, **extra: Any):
        try:
            # Its own transaction on the same pool: the caller's rolls back with the refusal.
            with db.engine.begin() as side:
                return _refuse(side, ctx, row, grant, code, message, **extra)
        except Exception:  # noqa: BLE001 — an audit failure never turns a refusal into an allow
            from .approvals import ApprovalRefused

            return ApprovalRefused(403, code, message, **extra)

    if grant is None or grant.delegate_principal_id != ctx.principal.id:
        raise refused("human_session_required",
                      "Only a person signed in to BOSS can approve. API keys and agents cannot.")
    if action not in ("approve", "reject"):
        raise refused("delegate_approves_or_rejects_only",
                      "A delegate only approves or rejects. The person does everything else.")
    if grant.status == "revoked":
        raise refused("delegation_revoked", "This approval delegation was revoked. The person decides it.",
                      delegation=grant.id)
    if grant.expires_at <= _now():
        raise refused("delegation_expired", "This approval delegation has expired. The person decides it.",
                      delegation=grant.id)
    person = db.execute(text("SELECT status, kind FROM identity_principals WHERE id = :p"),
                        {"p": grant.person_principal_id}).first()
    if person is None or person[0] != "active" or person[1] != "user" or is_bridge_principal(grant.person_principal_id):
        raise refused("delegating_person_inactive",
                      "The person this delegate approves for is no longer active.", delegation=grant.id)
    limit = hard_limit(db, row)
    if limit is not None:
        raise refused(limit[0], limit[1], delegation=grant.id)
    project = str(row.get("project_id") or "")
    if project not in grant.project_ids:
        raise refused("delegation_out_of_scope", "This decision's project is not in the delegation.",
                      delegation=grant.id, scope="project")
    kind = kind_for(row)
    if kind is None or kind not in grant.decision_kinds:
        raise refused("delegation_out_of_scope",
                      "This kind of decision is not in the delegation. The person decides it.",
                      delegation=grant.id, scope="kind", kind=kind)
    tier = int(row.get("risk_tier") or 0)
    if tier > configured_max_tier() and tier not in grant.extra_risk_tiers:
        raise refused("delegation_out_of_scope",
                      f"A tier-{tier} decision is above the tier a delegate approves "
                      f"({configured_max_tier()}) and the delegation does not list it.",
                      delegation=grant.id, scope="risk_tier", tier=tier)
    if grant.daily_cap is not None:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"approval_delegation:{grant.id}"})
        if uses_today(db, grant.id) >= grant.daily_cap:
            raise refused("delegation_daily_cap_reached",
                          f"This delegation's cap of {grant.daily_cap} a day is spent.", delegation=grant.id)
    # The actor string the person's own approval records (identity_guard.actor_for): the legacy
    # member they took over at the claim ("ceo"), else their principal id.
    legacy_ids = db.execute(text("SELECT legacy_member_ids FROM identity_principals WHERE id = :p"),
                            {"p": grant.person_principal_id}).scalar()
    actor = str((legacy_ids or [None])[0] or grant.person_principal_id)
    names = dict(db.execute(text("SELECT id, display_name FROM identity_principals WHERE id = ANY(:i)"),
                            {"i": [grant.person_principal_id, ctx.principal.id]}).all())
    return DelegatedApproval(delegation_id=grant.id, person_principal_id=grant.person_principal_id,
                             person_actor=actor, delegate_principal_id=ctx.principal.id, kind=kind,
                             person_name=names.get(grant.person_principal_id) or "",
                             delegate_name=names.get(ctx.principal.id) or "")


def record_use(db: Any, ctx: AuthContext, delegated: DelegatedApproval, row: Mapping[str, Any], *,
               action: str, note: Optional[str]) -> None:
    """After the approve/reject is written, in the same transaction: the use (the cap counts it),
    the decision's via columns, and the audit event naming both identities."""
    from .audit import Outcome, SecurityEvent, record

    decision_id = str(row["id"])
    db.execute(text("INSERT INTO identity_approval_delegation_uses (delegation_id, decision_id, action) "
                    "VALUES (:d, :x, :a)"), {"d": delegated.delegation_id, "x": decision_id, "a": action})
    db.execute(text("UPDATE identity_approval_delegations SET last_used_at = now() WHERE id = :d"),
               {"d": delegated.delegation_id})
    db.execute(text("UPDATE decisions SET via_delegate = :v, via_delegation = :g WHERE id = :x"),
               {"v": delegated.delegate_principal_id, "g": delegated.delegation_id, "x": decision_id})
    event = SecurityEvent.from_context(
        ctx, "decision.approved_by_delegate" if action == "approve" else "decision.rejected_by_delegate",
        Outcome.SUCCESS, target_type="decision", target_id=decision_id,
        details={"delegation": delegated.delegation_id, "for": delegated.person_principal_id,
                 "delegate": delegated.delegate_principal_id, "kind": delegated.kind,
                 "project": row.get("project_id"), "tier": int(row.get("risk_tier") or 0),
                 "note": (note or "")[:500]})
    record(_with_on_behalf_of(event, delegated.person_principal_id), db=db)


def _with_on_behalf_of(event: Any, person: str) -> Any:
    from dataclasses import replace

    return replace(event, on_behalf_of=person)


# ---- the grant ----------------------------------------------------------------------------------

def _validate(db: Any, ctx: AuthContext, *, kinds: Iterable[str], projects: Iterable[str],
              days: Optional[int], daily_cap: Optional[int], extra_risk_tiers: Iterable[int]
              ) -> tuple[list[str], list[str], datetime, Optional[int], list[int]]:
    from . import authz
    from .scope import ResolvedScope

    kinds_l = sorted({str(k) for k in (kinds or ())})
    unknown = [k for k in kinds_l if k not in DELEGABLE_KINDS]
    if not kinds_l or unknown:
        raise DelegationRefused("invalid_kinds", 422, "Choose one or more of the delegable kinds.",
                                kinds=list(DELEGABLE_KINDS), unknown=unknown)
    projects_l = sorted({str(p) for p in (projects or ()) if str(p).strip()})
    if not projects_l:
        raise DelegationRefused("projects_required", 422, "Name the projects the delegate approves in.")
    for project in projects_l:
        found = db.execute(text("SELECT 1 FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                                "WHERE p.id = :p AND w.org_id = :o"),
                           {"p": project, "o": ctx.principal.org_id}).first()
        if found is None:
            raise DelegationRefused("unknown_project", 422, project=project[:80])
        held = authz.check(ctx, "decision:approve", ResolvedScope.in_project(ctx.principal.org_id, project), db=db)
        if not held.allowed:
            raise DelegationRefused("not_yours_to_delegate", 403,
                                    "You can only delegate approvals you may give yourself.", project=project[:80])
    try:
        days_i = DEFAULT_DAYS if days in (None, "") else int(days)
    except (TypeError, ValueError):
        raise DelegationRefused("invalid_days", 422) from None
    if days_i < 1 or days_i > MAX_DAYS:
        raise DelegationRefused("expiry_too_long", 422, f"A delegation lasts 1 to {MAX_DAYS} days.",
                                max_days=MAX_DAYS)
    cap: Optional[int] = None
    if daily_cap not in (None, "", 0):
        try:
            cap = int(daily_cap)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise DelegationRefused("invalid_daily_cap", 422) from None
        if cap < 1 or cap > MAX_DAILY_CAP:
            raise DelegationRefused("invalid_daily_cap", 422, max=MAX_DAILY_CAP)
    tiers = sorted({int(t) for t in (extra_risk_tiers or ())})
    if any(t < 0 or t > 3 for t in tiers):
        raise DelegationRefused("invalid_risk_tiers", 422)
    return kinds_l, projects_l, _now() + timedelta(days=days_i), cap, tiers


def create(db: Any, ctx: AuthContext, *, kinds: Iterable[str], projects: Iterable[str],
           days: Optional[int] = None, daily_cap: Optional[int] = None,
           extra_risk_tiers: Iterable[int] = (), purpose: str = "", name: Optional[str] = None
           ) -> tuple[ApprovalDelegation, Any]:
    """Create the grant, its delegate service account and its ONE key. Returns (grant, CreatedKey);
    the caller returns the key once (api_keys.issued) and never stores or logs it."""
    from . import api_keys, ids, service_accounts
    from .audit import Outcome
    from .authn_support import emit
    from .legacy import is_bridge_principal

    if not ctx.is_human_session or ctx.principal.kind != PrincipalKind.USER or is_bridge_principal(ctx.principal.id):
        raise DelegationRefused("people_only", 403, "Only a person signed in to BOSS delegates approvals.")
    kinds_l, projects_l, expires_at, cap, tiers = _validate(
        db, ctx, kinds=kinds, projects=projects, days=None if days is None else days,
        daily_cap=daily_cap, extra_risk_tiers=extra_risk_tiers)
    who = ctx.principal.display_name or "you"
    account = service_accounts.create(
        db, actor=ctx, name=(name or f"Approves for {who}")[:200],
        description=f"Approval delegate of {who}: {', '.join(kinds_l)} in {', '.join(projects_l)}",
        owners=[ctx.principal.id])
    remaining_days = max(1, int((expires_at - _now()).total_seconds() // 86400) or 1)
    created = api_keys.create(
        db, creator=ctx, owner=account.principal(), kind="sak", name=f"approval delegate · {who}"[:120],
        scopes=sorted(DELEGATE_PERMISSIONS), project_ids=projects_l, expires_in_days=remaining_days,
        check_subset=False)
    did = ids.new_id("apd")
    db.execute(text(
        "INSERT INTO identity_approval_delegations (id, org_id, person_principal_id, delegate_principal_id, "
        "credential_id, decision_kinds, project_ids, extra_risk_tiers, daily_cap, purpose, expires_at) "
        "VALUES (:id, :o, :p, :d, :k, :kinds, :proj, :tiers, :cap, :purpose, :exp)"),
        {"id": did, "o": ctx.principal.org_id, "p": ctx.principal.id, "d": account.principal_id,
         "k": created.id, "kinds": kinds_l, "proj": projects_l, "tiers": tiers, "cap": cap,
         "purpose": (purpose or "")[:500], "exp": expires_at})
    emit(db, org_id=ctx.principal.org_id, event_type="approval_delegation.created", outcome=Outcome.SUCCESS,
         ctx=ctx, target_type="approval_delegation", target_id=did,
         details={"delegate": account.principal_id, "kinds": kinds_l, "projects": projects_l,
                  "expires_at": expires_at.isoformat(), "daily_cap": cap, "extra_risk_tiers": tiers})
    grant = get(db, did)
    assert grant is not None
    return grant, created


def revoke(db: Any, ctx: AuthContext, delegation_id: str) -> ApprovalDelegation:
    """The person (or an Admin with delegation:revoke_any, checked by the route) ends it now. The
    delegate's key is left in place but answers nothing: the PDP reads the grant at every request."""
    from .audit import Outcome
    from .authn_support import emit

    grant = get(db, delegation_id)
    if grant is None or grant.org_id != ctx.principal.org_id:
        raise DelegationRefused("not_found", 404)
    if grant.status == "revoked":
        raise DelegationRefused("revoked_is_terminal", 409)
    db.execute(text("UPDATE identity_approval_delegations SET status = 'revoked', revoked_at = now(), "
                    "revoked_by = :by WHERE id = :id"), {"by": ctx.principal.id, "id": delegation_id})
    emit(db, org_id=grant.org_id, event_type="approval_delegation.revoked", outcome=Outcome.SUCCESS, ctx=ctx,
         target_type="approval_delegation", target_id=delegation_id,
         details={"delegate": grant.delegate_principal_id, "for": grant.person_principal_id})
    out = get(db, delegation_id)
    assert out is not None
    return out


def view(db: Any, grant: ApprovalDelegation) -> dict[str, Any]:
    names = dict(db.execute(text("SELECT id, display_name FROM identity_principals WHERE id = ANY(:i)"),
                            {"i": [grant.person_principal_id, grant.delegate_principal_id]}).all())
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {
        "id": grant.id, "status": grant.state(),
        "person": {"id": grant.person_principal_id, "name": names.get(grant.person_principal_id)},
        "delegate": {"id": grant.delegate_principal_id, "name": names.get(grant.delegate_principal_id)},
        "key_id": grant.credential_id, "kinds": list(grant.decision_kinds),
        "kind_labels": [KIND_LABELS.get(k, k) for k in grant.decision_kinds],
        "projects": list(grant.project_ids), "extra_risk_tiers": list(grant.extra_risk_tiers),
        "max_risk_tier": configured_max_tier(), "daily_cap": grant.daily_cap,
        "used_today": uses_today(db, grant.id), "purpose": grant.purpose,
        "created_at": iso(grant.created_at), "expires_at": iso(grant.expires_at),
        "revoked_at": iso(grant.revoked_at), "last_used_at": iso(grant.last_used_at)}


def on_decision(db: Any, decision_id: str) -> Optional[dict[str, Any]]:
    """For the decision card: {delegate, delegation, person} when a delegate answered it."""
    r = db.execute(text(
        "SELECT d.via_delegate, d.via_delegation, g.person_principal_id, dp.display_name AS delegate_name, "
        "pp.display_name AS person_name FROM decisions d "
        "LEFT JOIN identity_approval_delegations g ON g.id = d.via_delegation "
        "LEFT JOIN identity_principals dp ON dp.id = d.via_delegate "
        "LEFT JOIN identity_principals pp ON pp.id = g.person_principal_id "
        "WHERE d.id = :x AND d.via_delegate IS NOT NULL"), {"x": decision_id}).mappings().first()
    if r is None:
        return None
    return {"delegate": {"id": r["via_delegate"], "name": r["delegate_name"]},
            "delegation_id": r["via_delegation"],
            "person": {"id": r["person_principal_id"], "name": r["person_name"]}}

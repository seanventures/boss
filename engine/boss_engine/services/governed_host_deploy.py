"""Deploy an app to a verified SSH host through BOSS's own approval machinery.

The SSH connector (deployment_connectors.SSHContainerConnector, wrapping deploy_service) is the
code that shipped every September deployment. It was switched off in Community because it ran
outside Resource Plane: no ChangeSet, no budget reservation, no approval, no ResourceReceipt.
The guard said so and named what it was waiting for. This module is that missing piece, and it
adds no new governance of its own — it is the stack deployment's governance (stack_deploy.py)
applied to a connector target:

    plan     ResourcePlaneService.plan_deployment_run, for an ssh/container target, records a
             budget reservation (0 — your own host costs no provider money, and the record still
             proves the path), a ChangeSet with one `ssh_host.app.deploy` operation describing
             the deployment (app, commit, host, port, secret REFERENCES, rollback), a blocking
             decision for a person ("Deploy <app> to <host> · <env>") and a deployment run
             `awaiting_approval`. Nothing touches the host.
    approve  the decisions API. A person approves (an agent never approves a host write); that
             approves the ChangeSet and queues `deployment.host_apply` as a durable worker job.
             Rejecting closes the ChangeSet and hands back the reservation.
    apply    the worker hands its leased job to the API (POST /v1/delivery/deployments/{id}/
             apply), which applies the ChangeSet in Resource Plane's three committed phases:
             begin_phased_apply (ChangeSet `applying`, receipt and resource receipt COMMITTED
             before any host write), the connector's own preflight → build → transfer → health
             check with no transaction open, then complete_phased_apply (the receipt's verdict,
             the reservation settled or released). A failed health check rolls the host back to
             the previous release and the run says `rolled_back`.

The connector and deploy_ssh refuse to write a host unless they are handed proof of exactly this
apply (require_governed_host_write): an approved ChangeSet that is `applying`, its reservation,
its receipt already committed, and the run's own apply token. Every other way in stays refused.

Secrets: the host key and the app's secrets are SecretRefs. A plan, a decision, a ChangeSet and
a receipt carry their NAMES and reference ids only; the key material is resolved at the moment
of use inside the connector and every step log is redacted by value.
"""
from __future__ import annotations

import json
import logging
import secrets as _secrets
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_blocking_decision, emit_event, make_id

log = logging.getLogger(__name__)

OPERATION_SKILL = "ssh_host.app.deploy"
JOB_KIND = "deployment.host_apply"
DECISION_ACTION = "deploy_host"
REASON_CODE = "host_deploy_requires_approval"
HOST_KINDS = frozenset({"ssh", "container"})
#: A run in one of these is still going somewhere: planning the same commit again returns it.
ACTIVE_STATES = ("awaiting_approval", "approved", "running")
#: How long a queued apply waits before asking again whether its target is free.
WAIT_SECONDS = 30

#: Where every refusal of an ungoverned host write points.
GOVERNED_PATH = (
    "Deploy it from Infra › Deploys (+ Deploy, Redeploy or Roll back), approve it there or in "
    "Approvals, and BOSS applies it under the approved ChangeSet."
)


def is_host_kind(target_kind: Any) -> bool:
    return str(target_kind or "").strip().lower().replace("-", "_") in HOST_KINDS


def ungoverned_refusal(what: str = "Direct host deployment") -> str:
    return (f"{what} outside an approved Resource Plane ChangeSet is refused: a host write "
            f"needs a ChangeSet, a budget reservation, a person's approval and a "
            f"ResourceReceipt. {GOVERNED_PATH}")


class GovernedHostDeployError(ValueError):
    """A plan BOSS refuses before anything is recorded, said as headline + action."""

    def __init__(self, headline: str, action: str):
        super().__init__(headline)
        self.headline = headline
        self.action = action


# ── Small helpers ─────────────────────────────────────────────────────────────────────────────

def _row(connection: Connection, run_id: str, *, lock: bool = False):
    return connection.execute(
        text("SELECT * FROM deployment_runs WHERE id = :i" + (" FOR UPDATE" if lock else "")),
        {"i": run_id}).mappings().first()


def _host_for(connection: Connection, target: dict) -> Optional[dict]:
    from .deployment_fleet import get_host

    host_id = str(target.get("host_id") or "")
    return get_host(connection, host_id) if host_id else None


def _secret_ref_names(target: dict, host: Optional[dict]) -> list[dict[str, str]]:
    """The secrets this deployment uses, BY REFERENCE: a name and a ref id, never a value."""
    out: list[dict[str, str]] = []
    for binding in target.get("secret_refs") or []:
        if isinstance(binding, str) and binding.strip():
            out.append({"name": "ssh_key", "ref": binding.strip()})
        elif isinstance(binding, dict) and binding.get("name"):
            ref = binding.get("secret_ref_id") or binding.get("ref") or ""
            out.append({"name": str(binding["name"]), "ref": str(ref)})
    if host and host.get("key_secret_ref") and not any(b["name"] == "ssh_key" for b in out):
        out.append({"name": "ssh_key", "ref": str(host["key_secret_ref"]), "from": "host"})
    return out


def target_for_apply(connection: Connection, target: dict) -> dict:
    """The target as the connector should see it: bound to its host's key when it has none of
    its own. A verified host's key is a SecretRef; the target that sits on it uses that ref."""
    host = _host_for(connection, target)
    bindings = list(target.get("secret_refs") or [])
    has_key = any((isinstance(b, str) and b.strip())
                  or (isinstance(b, dict) and b.get("name") == "ssh_key") for b in bindings)
    if host and host.get("key_secret_ref") and not has_key:
        bindings.append({"name": "ssh_key", "secret_ref_id": host["key_secret_ref"]})
    return {**target, "secret_refs": bindings}


def refuse_plan(connection: Connection, *, project_id: str, target: dict, actor: str,
                headline: str, action: str, reason: str) -> dict[str, Any]:
    """A host deployment BOSS will not plan: the answer, and one plain event — nothing else.

    No decision (there is nothing for a person to approve), no failed deployment run (nothing
    ran, so the rail and the failure cards have nothing to say). Callers answer 409 with
    `failure_reason`, which carries the next step."""
    emit_event(connection, project_id, "deployment.plan_refused", actor, "deployment_target",
               target["id"], {"reason": reason, "headline": headline, "next": action,
                              "environment": target.get("environment")})
    return {"status": "blocked", "refused": True, "id": None, "deployment_run_id": None,
            "deployment_target_id": target["id"], "reason_code": reason,
            "failure_reason": f"{headline} {action}", "next": action}


#: The decision the pre-governance guard and the first governed plans raised for a refusal.
REFUSAL_DECISION_ACTION = "implement_governed_host_deployment"


def repair_refusal_records(connection: Connection) -> dict[str, int]:
    """Withdraw the decisions earlier refusals raised and remove the failed runs they left.

    Before refusals became plain answers, an unverified host or the old Community guard raised a
    pending decision nobody could act on and a `failed` run that never ran — so Approvals, the
    needs-you count, the deploy rail and the failure cards all reported work that never existed.
    Idempotent; only runs that have no ChangeSet (they never reached Resource Plane) go."""
    withdrawn = connection.execute(
        text("UPDATE decisions SET status = 'withdrawn', updated_at = now(), "
             "resolution_note = 'A refused deployment plan: nothing to approve.' "
             "WHERE status IN ('pending', 'delegated', 'expired') "
             "AND recommendation->>'action' = :a RETURNING id"),
        {"a": REFUSAL_DECISION_ACTION}).scalars().all()
    runs = connection.execute(
        text("SELECT d.id FROM deployment_runs d JOIN decisions x ON x.id = d.decision_id "
             "WHERE x.recommendation->>'action' = :a AND d.changeset_id IS NULL "
             "AND d.status IN ('failed', 'blocked')"),
        {"a": REFUSAL_DECISION_ACTION}).scalars().all()
    removed = 0
    for run_id in runs:
        try:
            with connection.begin_nested():
                connection.execute(text("DELETE FROM deployment_runs WHERE id = :i"),
                                   {"i": run_id})
                removed += 1
        except Exception:  # noqa: BLE001 — something references it; leave it, say nothing
            log.info("refused-plan run %s kept: it is referenced", run_id)
    return {"decisions_withdrawn": len(withdrawn), "runs_removed": removed}


def release_guard(connection: Connection, guard: Optional[dict]) -> None:
    """End a surface lease this plan took (same owner and version), e.g. when the plan is
    refused after taking it."""
    if not isinstance(guard, dict) or not guard.get("ownership_id"):
        return
    connection.execute(
        text("UPDATE surface_ownerships SET expires_at = now(), updated_at = now() "
             "WHERE id = :i AND owner_ref = :o AND version = :v AND status = 'active' "
             "AND (expires_at IS NULL OR expires_at > now())"),
        {"i": guard["ownership_id"], "o": guard.get("owner_ref"),
         "v": int(guard.get("version") or 0)})


def release_surface(connection: Connection, row: Any) -> None:
    """A finished host deploy lets go of its target's deployment surface.

    The plan leases the surface for twenty minutes so two deployers cannot write one target at
    once. Held past the end of the deploy, that lease refused the next person's Redeploy or
    Roll back with "owned by another deployer" for a deploy that was already over. Only the
    lease THIS run took is ended (same owner and version), so a newer claim is never touched.
    """
    packet = row.get("release_packet") if hasattr(row, "get") else None
    guard = (packet or {}).get("surface_ownership") if isinstance(packet, dict) else None
    release_guard(connection, guard)


def _redact(value: str, hidden: list[str]) -> str:
    from .stack_deploy import redact

    return redact(value or "", hidden)


def _app_key_for(connection: Connection, project_id: str, target: dict,
                 release_packet: Optional[dict]) -> Optional[str]:
    """The app this deployment ships, the way every app view names it (its build, then its name)."""
    from ..app_links.service import resolve_app_key
    from .agent_assignment import app_key

    for raw in ((release_packet or {}).get("code_task_run_id"), target.get("app_name")):
        if not raw:
            continue
        try:
            with connection.begin_nested():
                found = resolve_app_key(connection, project_id, str(raw))
        except Exception:  # noqa: BLE001 — falls back to the name's key below
            found = None
        if found:
            return found
    name = str(target.get("app_name") or "").strip()
    return app_key(name) if name else None


def app_secret_problem(connection: Connection, project_id: str, target: dict,
                       release_packet: Optional[dict]) -> tuple[Optional[str], list[dict], Optional[str]]:
    """(app_key, the app secrets a deploy passes BY NAME, the refusal in words or None)."""
    from ..templates.secrets import deploy_secrets, ensure_deploy_generated

    key = _app_key_for(connection, project_id, target, release_packet)
    if not key:
        return None, [], None
    # The secrets BOSS makes (a session key) are made for deploy here, before they are counted.
    org_id = connection.execute(text(
        "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p"), {"p": project_id}).scalar()
    if org_id:
        with connection.begin_nested():
            ensure_deploy_generated(connection, org_id=str(org_id), project_id=project_id,
                                    app_key=key)
    passed, missing = deploy_secrets(connection, project_id=project_id, app_key=key)
    if not missing:
        return key, passed, None
    app = target.get("app_name") or key
    return key, passed, (f"{app} cannot deploy: " + "; ".join(missing) + ".")


APP_SECRET_ACTION = ("Set the missing secret for environment deploy in the app's Secrets (a live key "
                     "also needs its decision approved in Approvals), then deploy again.")


# ── Plan ─────────────────────────────────────────────────────────────────────────────────────

def plan_problem(connection: Connection, target: dict) -> Optional[dict[str, str]]:
    """What stops this target being planned, found without touching the host. None = plannable."""
    from .deployment_connectors import _app_port

    host = _host_for(connection, target)
    if not host:
        return {"headline": f"{target.get('app_name')} is not attached to a deployment host.",
                "action": "Attach the app to a verified host in Infra, then plan again.",
                "reason": "host_not_attached"}
    if host.get("status") != "verified":
        return {"headline": f"Host {host.get('name')} is {host.get('status')}, not verified.",
                "action": "In Infra, add the host's public key to the VM and press Verify.",
                "reason": "host_not_verified"}
    if not _app_port(target, default=0):
        return {"headline": f"{target.get('app_name')} has no app port.",
                "action": "Set the app's port on its target, then plan again.",
                "reason": "app_port_missing"}
    if not (target.get("domains") or []):
        return {"headline": f"{target.get('app_name')} has no address to check health on.",
                "action": "Set the target's domain (host:port), then plan again.",
                "reason": "domain_missing"}
    if not (target.get("health_checks") or []):
        return {"headline": f"{target.get('app_name')} declares no health check.",
                "action": "Add a health check to the target, then plan again.",
                "reason": "health_check_missing"}
    return None


def same_release(a: Optional[str], b: Optional[str]) -> bool:
    """Do two requested refs name the same release? An unnamed ref ("ship this app") is the
    open request, whatever it pinned; a short sha names its long form."""
    x, y = str(a or "").strip(), str(b or "").strip()
    return not x or not y or x == y or (len(min(x, y, key=len)) >= 7
                                        and (x.startswith(y) or y.startswith(x)))


def _open_runs(connection: Connection, target_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(
        text("SELECT * FROM deployment_runs WHERE deployment_target_id = :t "
             "AND changeset_id IS NOT NULL AND status = ANY(:s) ORDER BY created_at DESC"),
        {"t": target_id, "s": list(ACTIVE_STATES)}).mappings()]


def live_run(connection: Connection, target_id: str, commit_ref: Optional[str]):
    """The governed run already open for this target and release, if any: a second click, a
    retried step or two operators at once get the same run, never a second deployment.

    Matched on the RELEASE, not on the literal ref string. Equality on `commit_ref` let one
    target collect three open runs — the workflow asked with no ref, the Deploy button with
    the default branch, a redeploy with the sha — and each was a separate approval, so the
    operator approved one and watched another still asking."""
    for r in _open_runs(connection, target_id):
        if same_release(r.get("commit_ref"), commit_ref):
            return r["id"]
    return None


def supersede(connection: Connection, settings: Optional[Settings], row: Any, *,
              reason: str, actor: str) -> bool:
    """Close a request that never ran and never will: its ChangeSet is closed, its reservation
    returned, its surface released, and its decision withdrawn unless another run holds it."""
    from .budget import BudgetGovernor

    done = connection.execute(
        # finished_at stays the request's own time: closing an old orphan at startup must not
        # make it read as something that just happened.
        text("UPDATE deployment_runs SET status = 'superseded', "
             "finished_at = COALESCE(finished_at, created_at), "
             "failure_reason = :r, updated_at = now() "
             "WHERE id = :i AND status = 'awaiting_approval' RETURNING id"),
        {"i": row["id"], "r": reason[:400]}).first()
    if not done:
        return False
    if row.get("changeset_id"):
        connection.execute(
            text("UPDATE changesets SET status = 'rejected', failure_reason = :r, "
                 "updated_at = now() WHERE id = :c AND status IN ('pending_approval', "
                 "'policy_checked', 'budget_reserved')"),
            {"c": row["changeset_id"], "r": reason[:200]})
    if row.get("decision_id"):
        connection.execute(
            text("UPDATE decisions SET status = 'withdrawn', updated_at = now(), "
                 "resolution_note = :r WHERE id = :d AND status = 'pending' "
                 "AND NOT EXISTS (SELECT 1 FROM deployment_runs o WHERE o.decision_id = :d "
                 "                AND o.id <> :i AND o.status <> 'superseded')"),
            {"d": row["decision_id"], "i": row["id"], "r": reason[:200]})
    if row.get("reservation_id") and settings is not None:
        try:
            with connection.begin_nested():
                BudgetGovernor(settings).release(connection, row["project_id"],
                                                 row["reservation_id"], reason[:200])
        except Exception:  # noqa: BLE001 — a reservation already settled is not a failure
            log.info("reservation %s for %s was already settled", row["reservation_id"],
                     row["id"])
    release_surface(connection, row)
    emit_event(connection, row["project_id"], "deployment_run.superseded", actor,
               "deployment_run", row["id"], {"reason": reason[:300]})
    return True


def supersede_replaced(connection: Connection, settings: Settings, target_id: str,
                       commit_ref: Optional[str], *, actor: str) -> list[str]:
    """A request for a DIFFERENT release replaces the ones still waiting for approval on this
    target: approving the old one afterwards would ship what nobody now wants."""
    closed = []
    for r in _open_runs(connection, target_id):
        if r["status"] == "awaiting_approval" and not same_release(r.get("commit_ref"),
                                                                   commit_ref):
            if supersede(connection, settings, r, actor=actor,
                         reason=f"Replaced by a request for {commit_ref or 'the default branch'}."
                                " Nothing ran."):
                closed.append(r["id"])
    return closed


def supersede_orphaned_runs(connection: Connection, settings: Optional[Settings] = None
                            ) -> dict[str, int]:
    """Close requests that can never move: `awaiting_approval` with a decision that is no
    longer open. Two runs once shared one decision (deprun_dddc… and deprun_659e…); the
    approval took one, and the other sat at "Needs you" with nothing to approve. Idempotent."""
    rows = [dict(r) for r in connection.execute(text(
        "SELECT d.* FROM deployment_runs d JOIN decisions x ON x.id = d.decision_id "
        "WHERE d.status = 'awaiting_approval' AND x.status <> 'pending'")).mappings()]
    closed = sum(supersede(connection, settings, r, actor="boss",
                           reason="Its approval was answered for another deploy request. "
                                  "Nothing ran.") for r in rows)
    return {"runs_superseded": closed}


def previous_success(connection: Connection, target_id: str) -> Optional[dict]:
    row = connection.execute(
        text("SELECT id, commit_ref, finished_at FROM deployment_runs "
             "WHERE deployment_target_id = :t AND status = 'succeeded' "
             "ORDER BY finished_at DESC NULLS LAST LIMIT 1"), {"t": target_id}).mappings().first()
    return dict(row) if row else None


def rollback_plan_for(target: dict, previous: Optional[dict]) -> list[str]:
    if previous:
        commit = str(previous.get("commit_ref") or "")[:12] or "its recorded commit"
        first = (f"If the health check fails, BOSS restores the previous release on the host "
                 f"(run {previous['id']}, commit {commit}) and restarts it.")
    else:
        first = ("If the health check fails, BOSS restores whatever the host served before this "
                 "release (there is no earlier successful deployment of this app).")
    return [first, "The rollback and its health result are recorded on this run and its receipt."]


def plan_host_deployment(rp, connection: Connection, *, project_id: str, target: dict,
                         commit_ref: Optional[str], release_packet: dict,
                         release_artifact_id: Optional[str], ownership_guard: dict,
                         actor: str,
                         approval_decision_id: Optional[str] = None) -> dict[str, Any]:
    """Record a deployment of `target` at `commit_ref`, held for a person's approval — or run
    under one it already has (deploy_policy.pre_approval: the workflow gate's decision, passed
    in as `approval_decision_id`, or the environment's policy).

    Called by ResourcePlaneService.plan_deployment_run for ssh/container targets once the
    environment policy and the surface lease have passed. Returns the deployment run.
    """
    from ..templates.deploy_guard import deploy_refusal
    from .budget import BudgetGovernor
    from .deployment_connectors import _app_port, _split_ref

    refused = deploy_refusal(connection, project_id, target=target, commit_ref=commit_ref,
                             release_packet=release_packet)
    if refused:
        raise GovernedHostDeployError(refused, refused.action)
    _key, app_secrets, secret_refusal = app_secret_problem(connection, project_id, target,
                                                           release_packet)
    if secret_refusal:
        raise GovernedHostDeployError(secret_refusal, APP_SECRET_ACTION)
    host = _host_for(connection, target) or {}
    app = str(target.get("app_name") or target.get("repo") or "app")
    environment = str(target.get("environment") or "")
    host_name = str(host.get("name") or _split_ref(target.get("target_ref") or "")[1])
    user, address, ssh_port = _split_ref(target.get("target_ref") or "")
    app_port = _app_port(target, default=0)
    url = (target.get("domains") or [None])[0]
    previous = previous_success(connection, target["id"])
    rollback_plan = rollback_plan_for(target, previous)
    refs = _secret_ref_names(target, host)
    deployment_run_id = make_id("deprun")
    short = commit_ref[:12] if commit_ref else "default branch"
    risk_tier = 3 if environment == "production" else 2

    reservation = BudgetGovernor(rp.settings).reserve(
        connection, project_id, "provider", 0,
        f"Host deployment {deployment_run_id}: {app}@{short} to {host_name}",
        expires_in_minutes=7 * 24 * 60, risk_tier=risk_tier)
    if reservation.get("status") == "denied" or not reservation.get("reservation_id"):
        raise GovernedHostDeployError(
            "The budget refused to record this deployment's reservation.",
            str(reservation.get("message") or "Review the budget decision in Approvals."))
    reservation_id = reservation["reservation_id"]
    desired = {
        "deployment_run_id": deployment_run_id, "deployment_target_id": target["id"],
        "target_kind": target["target_kind"], "environment": environment, "app_name": app,
        "repo": target.get("repo"), "commit_ref": commit_ref,
        "host_id": host.get("id"), "host": host_name, "address": address, "user": user,
        "ssh_port": ssh_port, "app_port": app_port, "url": url,
        "health_checks": target.get("health_checks") or [],
        # References only. The connector resolves them at the moment of use.
        "secret_refs": refs,
        # The app's own secrets (vault kind app_secret, env deploy): NAMES and modes only; the
        # values are read at apply and reach the host as its environment, nowhere else.
        "app_secrets": app_secrets,
        "rollback_to": ({"deployment_run_id": previous["id"],
                         "commit_ref": previous.get("commit_ref")} if previous else None),
        "release_artifact_id": release_artifact_id,
        "surface_ownership": ownership_guard,
    }
    changeset = rp.create_changeset(
        connection, project_id=project_id, provider_account_id=None,
        title=f"Deploy {app} to {host_name} · {environment}",
        summary=(f"Build {target.get('repo')}@{short}, ship it to {user}@{address} as {app} on "
                 f"port {app_port}, and health-check it; roll back on a failed check."),
        operations=[{"skill_id": OPERATION_SKILL, "action": "deploy",
                     "resource_type": "ssh_host.app",
                     "resource_ref": f"{target['id']}:{app}",
                     "desired_state": desired, "risk_tier": 1,
                     "production_affecting": environment == "production"}],
        expected_cost_usd_micros=0, reservation_id=reservation_id,
        rollback_plan=rollback_plan, risk_tier=1, approval_required=False, actor=actor)
    # The ChangeSet waits on THIS decision, the one whose approval queues the apply — not on
    # the generic "approve ChangeSet" card, which the ChangeSet routes refuse for this skill.
    review = {
        "app_name": app, "environment": environment, "host": host_name,
        "address": address, "commit_ref": commit_ref, "repo": target.get("repo"),
        "app_port": app_port, "url": url,
        "secret_refs": [{"name": r["name"], "ref": r["ref"]} for r in refs],
        "app_secrets": [{"name": a["name"], "mode": a["mode"]} for a in app_secrets],
        "steps": ["Preflight the host (read-only)", "Build the app at the commit",
                  "Transfer and start it", "Health check"],
        "rollback": rollback_plan,
        "cost": {"expected_cost_usd_micros": 0, "reservation_id": reservation_id,
                 "note": "Your own host: no provider cost."},
    }
    question = f"Deploy {app} to {host_name} · {environment}"
    recommendation = {
        **review, "action": "deploy_host", "deployment_run_id": deployment_run_id,
        "changeset_id": changeset["id"], "target_id": target["id"],
    }
    cost = {"expected_cost_usd_micros": 0, "reservation_id": reservation_id,
            "cost_confidence": "no_provider_spend"}
    from .deploy_policy import pre_approval

    approval = pre_approval(connection, project_id, environment,
                            gate_decision_id=approval_decision_id)
    if approval and rp.required_approvers_for_changeset(connection, changeset["id"]):
        approval = None      # dual control is two people's answer; a policy cannot give it
    if approval:
        decision_id = approval["decision_id"]
        review["approval"] = {k: approval[k] for k in ("source", "decision_id", "approved_by")}
    else:
        decision_id = create_blocking_decision(
            connection, project_id, None, question, recommendation, cost, REASON_CODE,
            risk_tier=risk_tier, default_action="hold")
    connection.execute(
        text("UPDATE changesets SET status = 'pending_approval', risk_tier = :r, "
             "approval_decision_id = :d, updated_at = now() WHERE id = :c"),
        {"d": decision_id, "c": changeset["id"], "r": risk_tier})
    connection.execute(
        text("UPDATE changeset_operations SET risk_tier = :r WHERE changeset_id = :c"),
        {"c": changeset["id"], "r": risk_tier})
    if not approval:
        connection.execute(text("UPDATE decisions SET external_ref = :c WHERE id = :d"),
                           {"c": changeset["id"], "d": decision_id})
    review = {**review, "decision_id": decision_id, "changeset_id": changeset["id"]}
    packet = {**(release_packet or {}), "surface_ownership": ownership_guard,
              "governed": True, "plan_review": review}
    connection.execute(
        text("""
            INSERT INTO deployment_runs (
                id, project_id, deployment_target_id, changeset_id, decision_id,
                release_artifact_id, app_name, environment, repo, target_kind, target_ref,
                commit_ref, status, release_packet, rollback_plan, release_receipt_ids,
                created_by, started_at, reservation_id
            ) VALUES (
                :id, :p, :t, :cs, :d, :ra, :app, :env, :repo, :kind, :ref, :commit,
                'awaiting_approval', CAST(:packet AS JSONB), CAST(:rb AS JSONB),
                CAST(:receipts AS JSONB), :actor, now(), :res
            )"""),
        {"id": deployment_run_id, "p": project_id, "t": target["id"], "cs": changeset["id"],
         "d": decision_id, "ra": release_artifact_id, "app": app, "env": environment,
         "repo": target.get("repo") or "", "kind": target["target_kind"],
         "ref": target.get("target_ref") or "", "commit": commit_ref,
         "packet": json.dumps(packet), "rb": json.dumps(rollback_plan),
         "receipts": json.dumps(changeset["expected_receipts"]), "actor": actor,
         "res": reservation_id})
    emit_event(connection, project_id, "deployment_run.planned", actor, "deployment_run",
               deployment_run_id,
               {"deployment_target_id": target["id"], "changeset_id": changeset["id"],
                "decision_id": decision_id, "environment": environment, "governed": True})
    if approval:
        from .deploy_policy import record_pre_approval

        record_pre_approval(connection, project_id, approval, entity_type="deployment_run",
                            entity_id=deployment_run_id, actor=actor)
        approve_and_queue(connection, rp.settings, run_id=deployment_run_id,
                          approver=approval["approved_by"], decision_id=decision_id)
    return rp._get_deployment_run(connection, project_id, deployment_run_id)


# ── Approval (DecisionService follow-on) ─────────────────────────────────────────────────────

def on_decision_approved(connection: Connection, settings: Settings, decision: dict,
                         actor: str) -> Optional[dict[str, Any]]:
    """A person's approval approves the ChangeSet and queues the apply as a worker job."""
    from .agent_identity import is_person
    from .decisions import InvalidDecisionTransitionError
    from .jobs import JOB_PRIORITY_INTERACTIVE, enqueue_job
    from .resource_plane import ResourcePlaneService

    rec = decision.get("recommendation") or {}
    run_id = str(rec.get("deployment_run_id") or "")
    row = _row(connection, run_id, lock=True) if run_id else None
    if not row or row["status"] != "awaiting_approval":
        return None
    # A host write is approved by the signed-in person, never by an agent or by BOSS itself —
    # and certainly not by the agent that planned it.
    if not is_person(actor, connection, row["project_id"]):
        raise InvalidDecisionTransitionError(
            f"'{actor}' cannot approve a host deployment: a person approves it in Approvals.")
    rp = ResourcePlaneService(settings)
    changeset = rp.get_changeset(connection, row["project_id"], row["changeset_id"])
    if changeset.get("status") != "approved":
        from ..identity import viewer as _viewer

        approver_roles = None
        if _viewer.enforced():
            # Dual control under identity: the approver roles this person holds through the PDP
            # (never the legacy members table, which names none of them).
            required = rp.required_approvers_for_changeset(connection, row["changeset_id"])
            approver_roles = (_viewer.roles_granting(row["project_id"], "changeset:approve", required)
                              if required else None)
            if required and not approver_roles:
                raise InvalidDecisionTransitionError(
                    "Dual control: approving this host write needs one of the approver roles "
                    + ", ".join(required) + ".")
        changeset = rp.approve_changeset(connection, changeset_id=row["changeset_id"],
                                         actor=actor, phased_owner=True, approver_roles=approver_roles)
    if changeset.get("status") != "approved":
        return None  # dual control: another approver role is still to sign
    return _queue_apply(connection, row, actor=actor, decision_id=decision.get("id"))


def _queue_apply(connection: Connection, row: Any, *, actor: str,
                 decision_id: Optional[str]) -> dict[str, Any]:
    """The approved deployment's apply, queued as a worker job."""
    from .jobs import JOB_PRIORITY_INTERACTIVE, enqueue_job

    run_id = str(row["id"])
    job = enqueue_job(
        connection, row["project_id"], JOB_KIND,
        {"deployment_run_id": run_id, "changeset_id": row["changeset_id"], "actor": actor},
        priority=JOB_PRIORITY_INTERACTIVE, max_attempts=5,
        dedupe_key=f"host-deploy:{run_id}", data_boundary="internal")
    connection.execute(
        text("UPDATE deployment_runs SET status = 'approved', job_id = :j, updated_at = now() "
             "WHERE id = :i"), {"j": job.get("id"), "i": run_id})
    emit_event(connection, row["project_id"], "deployment_run.approved", actor,
               "deployment_run", run_id,
               {"decision_id": decision_id, "changeset_id": row["changeset_id"],
                "job_id": job.get("id")})
    return job


def approve_and_queue(connection: Connection, settings: Settings, *, run_id: str,
                      approver: str, decision_id: Optional[str]) -> Optional[dict[str, Any]]:
    """Run a deployment under an approval it ALREADY HAS (deploy_policy.pre_approval): its
    ChangeSet approved by that approver, its apply queued now — no card of its own."""
    from .resource_plane import ResourcePlaneService

    row = _row(connection, run_id, lock=True)
    if not row or row["status"] != "awaiting_approval":
        return None
    rp = ResourcePlaneService(settings)
    changeset = rp.approve_changeset(connection, changeset_id=row["changeset_id"],
                                     actor=approver, phased_owner=True)
    if changeset.get("status") != "approved":
        return None
    return _queue_apply(connection, row, actor=approver, decision_id=decision_id)


def on_decision_not_approved(connection: Connection, settings: Settings, decision: dict,
                             actor: str, outcome: str) -> None:
    """A refused deployment closes its ChangeSet and hands back its reservation. The host is
    never contacted."""
    from .budget import BudgetGovernor

    rec = decision.get("recommendation") or {}
    run_id = str(rec.get("deployment_run_id") or "")
    row = _row(connection, run_id, lock=True) if run_id else None
    if not row or row["status"] != "awaiting_approval":
        return
    connection.execute(
        text("UPDATE deployment_runs SET status = 'rejected', finished_at = now(), "
             "failure_reason = :r, updated_at = now() WHERE id = :i"),
        {"i": run_id, "r": f"Not approved ({outcome}) by {actor}. Nothing was deployed."})
    connection.execute(
        text("UPDATE changesets SET status = 'rejected', failure_reason = :r, updated_at = now() "
             "WHERE id = :c AND status IN ('pending_approval', 'policy_checked', "
             "'budget_reserved')"),
        {"c": row["changeset_id"], "r": f"decision {outcome} by {actor}"})
    if row["reservation_id"]:
        BudgetGovernor(settings).release(connection, row["project_id"], row["reservation_id"],
                                         "Host deployment was not approved.")
    release_surface(connection, row)
    emit_event(connection, row["project_id"], "deployment_run.rejected", actor,
               "deployment_run", run_id, {"outcome": outcome})


# ── The only door to a host write ────────────────────────────────────────────────────────────

def require_governed_host_write(connection: Connection, governance: Optional[dict]) -> None:
    """Raise PermissionError unless `governance` proves an approved ChangeSet is being applied
    for this very run, with its reservation and receipt already committed."""
    if not governance:
        raise PermissionError(ungoverned_refusal())
    run = _row(connection, str(governance.get("deployment_run_id") or ""))
    if not run or not run["changeset_id"] or run["changeset_id"] != governance.get("changeset_id"):
        raise PermissionError("Host deployment refused: the ChangeSet does not belong to this run.")
    if run["status"] != "running" or not run["apply_token"] \
            or run["apply_token"] != governance.get("apply_token"):
        raise PermissionError("Host deployment refused: this request does not own the run's apply.")
    cs = connection.execute(
        text("SELECT status, approved_by, reservation_id, project_id FROM changesets "
             "WHERE id = :c"), {"c": run["changeset_id"]}).mappings().first()
    if not cs or cs["status"] != "applying" or not cs["approved_by"]:
        raise PermissionError("Host deployment refused: its ChangeSet is not an approved "
                              "ChangeSet being applied.")
    if not cs["reservation_id"] or not connection.execute(
            text("SELECT 1 FROM budget_reservations WHERE id = :r"),
            {"r": cs["reservation_id"]}).first():
        raise PermissionError("Host deployment refused: it has no budget reservation.")
    if not connection.execute(
            text("SELECT 1 FROM receipts WHERE project_id = :p AND dedupe_key = :k"),
            {"p": cs["project_id"], "k": str(governance.get("dedupe_key") or "")}).first():
        raise PermissionError("Host deployment refused: its receipt was not recorded first.")


def check_governed(engine, governance: Optional[dict]) -> None:
    from ..db import begin

    with begin(engine) as connection:
        require_governed_host_write(connection, governance)


# ── Execute (the worker's leased job, handed to the API) ─────────────────────────────────────

def _close_governance(connection: Connection, settings: Settings, row: Any, *, actor: str,
                      reason: str) -> None:
    from .stack_deploy import _close_governance as close

    close(connection, settings, row, actor=actor, reason=reason)


def _fail(connection: Connection, row: Any, *, reason: str, actor: str) -> None:
    connection.execute(
        text("UPDATE deployment_runs SET status = 'failed', finished_at = now(), "
             "failure_reason = :r, updated_at = now() WHERE id = :i"),
        {"i": row["id"], "r": reason[:400]})
    release_surface(connection, row)
    emit_event(connection, row["project_id"], "delivery.failed", actor, "deployment_run",
               row["id"], {"error": reason[:300], "governed": True})


def execute_host_job(engine, settings: Settings, *, deployment_run_id: str, job_id: str,
                     worker_id: str, lease_token: str, connector: Any = None) -> dict[str, Any]:
    """Apply an approved host deployment under the worker's lease. Idempotent across replays."""
    from .jobs import JobLeaseService, LeaseNotFoundError
    from .resource_plane import ResourcePlaneService
    from .stack_deploy import _ApplyLocks, _lock_key, _short

    jobs = JobLeaseService(settings)
    with _ApplyLocks(engine) as locks:
        with engine.begin() as connection:
            _short(connection)
            try:
                jobs.require_active_lease(connection, worker_id, job_id, lease_token)
            except LeaseNotFoundError as exc:
                # Not this job's live lease (forged, reclaimed or unknown): refused, not "failed".
                return {"status": "refused", "retry": False, "http_status": 409,
                        "error": f"lease: {exc}"}
            row = _row(connection, deployment_run_id, lock=True)
            if not row:
                return {"status": "refused", "retry": False, "http_status": 404,
                        "error": "no such deployment run"}
            if row["status"] not in ("approved", "running"):
                if row["status"] in ("failed", "blocked"):
                    # Failed elsewhere (the startup sweep fails a run the API was applying):
                    # the ChangeSet still gets its verdict, so it never sits `applying`.
                    _close_governance(connection, settings, row, actor=worker_id,
                                      reason=f"deployment run is {row['status']}")
                return {"status": row["status"], "retry": False}
            if not locks.try_lock(_lock_key("host-deploy", deployment_run_id)):
                note = "This deployment is still being applied; waiting for its result."
                jobs.defer_leased(connection, worker_id, job_id, lease_token,
                                  delay_seconds=WAIT_SECONDS, reason="host_deploy_in_flight",
                                  message=note)
                return {"status": "running", "retry": False, "job_deferred": True, "error": note}
            if row["apply_started_at"] is not None:
                # A replay after a crash: the host may be half way through. Say so; a person
                # decides whether to deploy again.
                _fail(connection, row, actor=worker_id,
                      reason="BOSS was interrupted while this deployment was running, so the "
                             "host may hold a partial release. Plan a redeploy.")
                _close_governance(connection, settings, row, actor=worker_id,
                                  reason="apply interrupted; not replayed")
                return {"status": "failed", "retry": False}
            if not locks.try_lock(_lock_key("host-target", str(row["deployment_target_id"]))):
                note = "Another deployment of this app is running; waiting for it to finish."
                jobs.defer_leased(connection, worker_id, job_id, lease_token,
                                  delay_seconds=WAIT_SECONDS, reason="host_deploy_target_busy",
                                  message=note)
                return {"status": "waiting", "retry": False, "job_deferred": True, "error": note}
            actor = str(row["created_by"] or worker_id)
            try:
                started = ResourcePlaneService(settings).begin_phased_apply(
                    connection, changeset_id=row["changeset_id"], actor=actor,
                    created_by_job_id=job_id)
            except ValueError as exc:
                _fail(connection, row, actor=worker_id,
                      reason=f"The ChangeSet could not be applied: {exc}")
                _close_governance(connection, settings, row, actor=worker_id, reason=str(exc))
                return {"status": "failed", "retry": False}
            token = _secrets.token_hex(16)
            connection.execute(
                text("UPDATE deployment_runs SET status = 'running', started_at = now(), "
                     "apply_started_at = now(), apply_token = :tok, job_id = :j, "
                     "steps = '[]'::jsonb, updated_at = now() WHERE id = :i"),
                {"tok": token, "j": job_id, "i": deployment_run_id})
            emit_event(connection, row["project_id"], "delivery.started", actor,
                       "deployment_run", deployment_run_id,
                       {"changeset_id": row["changeset_id"], "governed": True})
        # Phase one is committed. From here to the verdict no transaction is open.
        governance = {"deployment_run_id": deployment_run_id,
                      "changeset_id": row["changeset_id"],
                      "dedupe_key": started[0]["dedupe_key"], "apply_token": token}
        operation_id = str(started[0]["operation"]["id"])
        outcome = apply_on_host(engine, settings, row=dict(row), governance=governance,
                                connector=connector)
        with engine.begin() as connection:
            _short(connection)
            return _finalize(connection, settings, row=dict(row), token=token,
                             operation_id=operation_id, outcome=outcome, actor=actor)


def apply_on_host(engine, settings: Settings, *, row: dict, governance: dict,
                  connector: Any = None) -> dict[str, Any]:
    """Phase two, against the host's PINNED key (host_key_pin): every SSH session of this
    apply — preflight, transfer, health gate, rollback — talks only to the verified machine."""
    from ..db import begin
    from .host_key_pin import CHANGED, pinned

    with begin(engine) as c:
        host_id = c.execute(text("SELECT host_id FROM deployment_targets WHERE id = :t"),
                            {"t": row["deployment_target_id"]}).scalar()
    with pinned(engine, host_id) as pin:
        outcome = _apply_on_host(engine, settings, row=row, governance=governance,
                                 connector=connector)
    if pin is not None and pin.changed:
        outcome = {**outcome, "status": "failed" if outcome.get("status") != "rolled_back"
                   else outcome["status"], "reason": CHANGED}
    return outcome


def _apply_on_host(engine, settings: Settings, *, row: dict, governance: dict,
                   connector: Any = None) -> dict[str, Any]:
    """Phase two: the September connector path — preflight, build, transfer, health check —
    run under the approved ChangeSet, with every step recorded on the run."""
    from ..db import begin
    from .deployment_connectors import connector_for, preflight
    from .deployment_steps import StepRecorder
    from .secret_backends import resolve_target_secret

    run_id = row["id"]
    with begin(engine) as c:
        require_governed_host_write(c, governance)
        trow = c.execute(text("SELECT * FROM deployment_targets WHERE id = :t"),
                         {"t": row["deployment_target_id"]}).mappings().first()
        if not trow:
            return {"status": "failed", "reason": "the deployment target no longer exists"}
        # Again at apply: an approval recorded before an insecure template was applied must
        # still not ship it (templates.deploy_guard).
        from ..templates.deploy_guard import deploy_refusal

        refused = deploy_refusal(c, row.get("project_id") or trow.get("project_id"), target=dict(trow),
                                 commit_ref=row.get("commit_ref"),
                                 release_packet=row.get("release_packet") or {})
        if refused:
            return {"status": "blocked", "reason": refused}
        target = target_for_apply(c, dict(trow))
        # Resolved ONLY to redact it from every log line; never stored or returned.
        hidden: list[str] = []
        # The app's deploy secrets, checked again (a live key's approval can be withdrawn after
        # the plan) and read only here, for the host's environment.
        project_id = row.get("project_id") or trow.get("project_id")
        app_key, _names, secret_refusal = app_secret_problem(
            c, project_id, dict(trow), row.get("release_packet") or {})
        if secret_refusal:
            return {"status": "blocked", "reason": secret_refusal}
        app_env: dict[str, str] = {}
        address: dict[str, Any] = {}
        if app_key:
            # THE ONE READER of a deployed app's environment (templates.base_url): its deploy
            # secrets plus APP_BASE_URL, the address this target serves it at, for every
            # template that declares it — never left at the template's localhost default.
            from ..app_links.service import project_org
            from ..templates.base_url import BASE_URL_ENV, deploy_environment

            app_env, _withheld, address = deploy_environment(
                c, org_id=project_org(c, project_id) if _names else None,
                project_id=project_id, app_key=app_key, target=target)
            hidden.extend(v for k, v in app_env.items() if k != BASE_URL_ENV)
        try:
            material = resolve_target_secret(c, target, "ssh_key", settings)
            if material:
                hidden.append(material)
        except Exception:  # noqa: BLE001 — the preflight reports an unusable key in words
            pass
    conn = connector or connector_for(target)
    rec = StepRecorder(engine, run_id)
    code_task_run = (row.get("release_packet") or {}).get("code_task_run_id") or ""

    def say(handle, value: Any) -> None:
        handle.log(_redact(value if isinstance(value, str)
                           else json.dumps(value, default=str), hidden)[:2000])

    try:
        with rec.step("resolve") as h:
            from ..templates.base_url import address_lines

            say(h, f"target={target.get('app_name')} env={target.get('environment')}\n"
                   f"repo={target.get('repo')} ref={row.get('commit_ref') or '(default branch)'}\n"
                   f"host={target.get('target_ref')}\nchangeset={row['changeset_id']}"
                   + (f"\n{address_lines(address)}" if address else ""))
        with rec.step("preflight") as h:
            with begin(engine) as c:
                pre = preflight(target, settings, c)
            say(h, "ready" if pre["ready"] else "\n".join(pre["problems"]))
            if not pre["ready"]:
                h.failed(_redact("; ".join(pre["problems"]), hidden)[:400])
        if not pre["ready"]:
            rec.finish()
            return {"status": "blocked",
                    "reason": _redact("; ".join(pre["problems"]), hidden)[:400]}
        with rec.step("build", label="Plan and build") as h:
            plan = conn.plan(engine, settings, target, code_task_run, ref=row.get("commit_ref"))
            say(h, f"strategy={plan.get('strategy', '')}\nbranch={plan.get('branch', '')}")
        with rec.step("transfer", label="Build, transfer and release") as h:
            applied = conn.apply(engine, settings, target, code_task_run, plan,
                                 ref=row.get("commit_ref"), governance=governance,
                                 **({"app_env": app_env} if app_env else {})) or {}
            say(h, {k: v for k, v in applied.items() if k not in ("plan", "rollback_point")})
            applied_status = str(applied.get("status") or "")
            if applied_status != "deployed":
                reason = _redact("; ".join(applied.get("problems") or [])
                                 or str(applied.get("detail") or applied.get("error")
                                        or f"apply status={applied_status or 'unknown'}"),
                                 hidden)[:400]
                h.failed(reason)
        if applied_status == "rolled_back":
            # deploy_ssh's own health gate failed and it restored the previous release.
            with rec.step("rollback", label="Roll back") as h:
                say(h, "The app did not answer healthily on the host, so BOSS restored the "
                       "previous release.\n" + str(applied.get("detail") or ""))
            rec.finish()
            return {"status": "rolled_back", "applied": applied, "reason":
                    _redact(str(applied.get("detail") or "health check failed; rolled back"),
                            hidden)[:400]}
        if applied_status != "deployed":
            rec.finish()
            return {"status": "blocked" if applied_status == "blocked" else "failed",
                    "applied": applied, "reason": reason}
        with rec.step("observe", label="Health check") as h:
            with begin(engine) as c:
                fresh = c.execute(text("SELECT * FROM deployment_targets WHERE id = :i"),
                                  {"i": target["id"]}).mappings().first()
            if fresh:
                # The port apply() settled on, with the key binding this apply used.
                target = {**dict(fresh), "secret_refs": target["secret_refs"]}
            health = conn.observe(target, settings)
            say(h, health)
            healthy = health.get("healthy")
            if healthy is False:
                checks = health.get("checks") or [{}]
                first = checks[0]
                h.failed(_redact(str(first.get("error") or
                                     f"{first.get('url', 'target')} returned "
                                     f"{first.get('status')}, expected {first.get('expected')}"),
                                 hidden)[:400])
        if healthy is False:
            with rec.step("rollback", label="Roll back") as h:
                back = conn.rollback(engine, settings, target, applied.get("rollback_point") or {},
                                     governance=governance)
                say(h, back)
                if back.get("status") != "rolled_back":
                    h.failed(_redact(str(back.get("error") or back.get("reason")
                                         or "rollback did not complete"), hidden)[:400])
            rec.finish()
            return {"status": "rolled_back", "applied": applied, "health": health,
                    "rollback": back,
                    "reason": "The health check failed after release; BOSS rolled the host "
                              "back to the previous release."
                              + ("" if back.get("status") == "rolled_back"
                                 else " The rollback did not complete: check the host.")}
        rec.finish()
        return {"status": "succeeded", "applied": applied, "health": health,
                "url": applied.get("url") or (target.get("domains") or [None])[0],
                **({"app_address": address} if address else {})}
    except Exception as exc:  # noqa: BLE001 — a sentence, never a stack trace or a secret
        log.warning("governed host deploy %s failed", run_id, exc_info=True)
        rec.finish()
        # In words: the exception's own sentence, never its class name.
        said = str(exc).strip() or "The deploy stopped unexpectedly."
        return {"status": "failed", "reason": _redact(said[:1].upper() + said[1:], hidden)[:400]}


def _finalize(connection: Connection, settings: Settings, *, row: dict, token: str,
              operation_id: str, outcome: dict, actor: str) -> dict[str, Any]:
    """Phase three: the receipt's verdict, the reservation settled or released, the run's."""
    from .resource_plane import ResourcePlaneService

    status = outcome.get("status") or "failed"
    applied = outcome.get("applied") or {}
    if status == "succeeded":
        result = {
            "provider_ref": f"ssh://{applied.get('host') or row.get('target_ref')}/"
                            f"{applied.get('app') or row.get('app_name')}",
            "resource_external_id": f"app:{row['deployment_target_id']}",
            "resource_name": row.get("app_name"),
            "resource_type": "ssh_host.app",
            "state": "active", "health_status": "healthy", "operation": "deployed",
            "deployment_run_id": row["id"], "commit": applied.get("deployed_sha"),
            "url": outcome.get("url"),
        }
        outcomes = {operation_id: {"result": result}}
    else:
        outcomes = {operation_id: {"error": {
            "message": str(outcome.get("reason") or status)[:400],
            "error_type": "HostDeployRolledBack" if status == "rolled_back"
            else "HostDeployFailed",
            "deployment_run_id": row["id"]}}}
    ResourcePlaneService(settings).complete_phased_apply(
        connection, changeset_id=row["changeset_id"], actor=actor, outcomes=outcomes)
    receipt_id = connection.execute(
        text("SELECT id FROM resource_receipts WHERE changeset_id = :c ORDER BY created_at DESC "
             "LIMIT 1"), {"c": row["changeset_id"]}).scalar()
    health = outcome.get("health") or {}
    connection.execute(
        text("UPDATE deployment_runs SET status = :s, finished_at = now(), "
             "failure_reason = :r, health_check_results = CAST(:h AS JSONB), "
             "commit_ref = COALESCE(:sha, commit_ref), "
             "release_receipt_ids = CAST(:rr AS JSONB), "
             "duration_ms = (EXTRACT(EPOCH FROM (now() - apply_started_at)) * 1000)::int, "
             "updated_at = now() WHERE id = :i AND apply_token = :tok AND status = 'running'"),
        {"s": status, "r": None if status == "succeeded" else
         str(outcome.get("reason") or status)[:400],
         "h": json.dumps(health, default=str),
         "sha": applied.get("deployed_sha") if status == "succeeded" else None,
         "rr": json.dumps([receipt_id] if receipt_id else []), "i": row["id"], "tok": token})
    release_surface(connection, row)
    emit_event(connection, row["project_id"], f"delivery.{status}", actor, "deployment_run",
               row["id"], {"changeset_id": row["changeset_id"], "resource_receipt_id": receipt_id,
                           "governed": True, "url": outcome.get("url")})
    final = _row(connection, row["id"])
    return {"status": final["status"], "retry": False, "deployment_run_id": row["id"],
            "resource_receipt_id": receipt_id}


def fail_for_deadlettered_job(connection: Connection, job: dict, error: dict,
                              settings: Optional[Settings] = None) -> None:
    """A deadlettered apply job fails its run in words and closes its ChangeSet."""
    from .stack_deploy import _json, _lock_key

    if job.get("kind") != JOB_KIND:
        return
    payload = _json(job.get("payload")) or {}
    run_id = str(payload.get("deployment_run_id") or "")
    row = _row(connection, run_id, lock=True) if run_id else None
    if not row or row["status"] not in ("approved", "running"):
        return
    if not connection.execute(text("SELECT pg_try_advisory_xact_lock(:k)"),
                              {"k": _lock_key("host-deploy", run_id)}).scalar():
        return
    _fail(connection, row, actor="worker",
          reason=("The worker job applying this deployment stopped before it finished: "
                  f"{str(error.get('message') or '')[:200]}"))
    if settings is not None:
        _close_governance(connection, settings, row, actor="worker",
                          reason="the apply job was deadlettered")

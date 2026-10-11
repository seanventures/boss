"""The infrastructure platform inside BOSS's existing deploy path (stack_deploy).

    plan:   plan_for_deploy -> store_plan; the decision's recommendation carries
            `infra_plan` (the document) and `action_preview` (skill `infra.plan`), so the decision
            preview shows it and DecisionService pins its content hash the way approvals pin
            content.
    apply:  apply_for_deployment, called by the stack apply (a leased `deployment.stack_apply`
            job) BEFORE the env file is written: the approved plan runs, its hash re-checked.
    env:    executor.env_values gives the binding values the env file gets, at write time.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from sqlalchemy import text

from .contract import ResourceRequest
from .executor import ApplyRefused, apply_plan, ledger_for_app
from .planner import build_plan, resolve, summary_words
from .registry import Registry

PREVIEW_SKILL = "infra.plan"


def plan_for_deploy(engine: Any, *, project_id: str, app_key: str, environment: str,
                    resources: list[ResourceRequest], host: Any = None) -> dict[str, Any]:
    """Resolve and plan. Raises ContractError (one line per problem) for an operator to fix."""
    with engine.begin() as c:
        registry = Registry.load(c)
        existing = ledger_for_app(c, project_id, app_key, resources[0].environment if resources else "deploy")
    resolved = resolve(list(resources), registry)
    return build_plan(resolved, app_key=app_key, environment=environment, existing=existing, host=host)


def preview_inputs(doc: dict[str, Any]) -> dict[str, Any]:
    return {"infra_plan": doc}


def preview(doc: dict[str, Any]) -> dict[str, Any]:
    from ..services import action_preview
    return action_preview.build(PREVIEW_SKILL, preview_inputs(doc), step_label="Infrastructure")


def store_plan(connection: Any, *, project_id: str, app_key: str, environment: str, doc: dict[str, Any],
               deployment_id: Optional[str], actor: Optional[str]) -> str:
    from ..services.core import make_id
    plan_id = make_id("iplan")
    connection.execute(text(
        "INSERT INTO infra_plans (id, project_id, app_key, environment, digest, plan, deployment_id, created_by) "
        "VALUES (:i, :p, :a, :e, :d, CAST(:plan AS JSONB), :dep, :by)"),
        {"i": plan_id, "p": project_id, "a": app_key, "e": environment, "d": doc["digest"],
         "plan": json.dumps(doc), "dep": deployment_id, "by": actor})
    return plan_id


def link_decision(connection: Any, plan_id: str, decision_id: str) -> None:
    connection.execute(text("UPDATE infra_plans SET decision_id = :d, updated_at = now() WHERE id = :i"),
                       {"d": decision_id, "i": plan_id})


def plan_for_deployment(connection: Any, deployment_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT * FROM infra_plans WHERE deployment_id = :d ORDER BY created_at DESC LIMIT 1"),
        {"d": deployment_id}).mappings().first()
    return dict(row) if row else None


def _approved_hash(connection: Any, decision_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not decision_id:
        return None, None
    row = connection.execute(text("SELECT status, recommendation FROM decisions WHERE id = :d"),
                             {"d": decision_id}).mappings().first()
    if not row:
        return None, None
    rec = row["recommendation"]
    rec = json.loads(rec) if isinstance(rec, str) else (rec or {})
    return row["status"], rec.get("approved_content_hash") or (rec.get("action_preview") or {}).get("content_hash")


def apply_for_deployment(engine: Any, *, deployment_id: str, project_id: str, resources: list[ResourceRequest],
                         actor_id: Optional[str], job_id: Optional[str] = None,
                         is_cancelled: Callable[[], bool] = lambda: False, host: Any = None) -> list[dict]:
    """Run the deployment's approved infrastructure plan. No plan (no `resources:`) is a no-op."""
    from ..services.action_preview import content_hash
    with engine.begin() as c:
        row = plan_for_deployment(c, deployment_id)
        if row is None:
            return []
        status, approved = _approved_hash(c, row.get("decision_id"))
        registry = Registry.load(c)
    doc = row["plan"] if isinstance(row["plan"], dict) else json.loads(row["plan"])
    if status not in ("approved", "resolved", "executed", None) and row.get("decision_id"):
        raise ApplyRefused("not_approved", "the infrastructure plan's decision is not approved")
    if approved and content_hash(preview_inputs(doc)) != approved:
        raise ApplyRefused("plan_changed", "the infrastructure plan differs from the one approved")
    try:
        results = apply_plan(engine, project_id=project_id, app_key=row["app_key"], doc=doc,
                             requests=resources, registry=registry, actor_id=actor_id,
                             approved_digest=doc.get("digest"), job_id=job_id, is_cancelled=is_cancelled,
                             host=host)
    except ApplyRefused:
        with engine.begin() as c:
            c.execute(text("UPDATE infra_plans SET status = 'failed', updated_at = now() WHERE id = :i"),
                      {"i": row["id"]})
        raise
    with engine.begin() as c:
        c.execute(text("UPDATE infra_plans SET status = 'applied', updated_at = now() WHERE id = :i"),
                  {"i": row["id"]})
    return [{"resourceId": r.resourceId, "hook": r.hook.value, "status": r.status.value,
             "evidence": [e.check for e in r.evidence if e.passed]} for r in results]


class StackHostRunner:
    """ctx.host for a stack deploy: BOSS's governed SSH to the target host. Reads run as reads;
    during an apply every command is a write under the deployment's approved ChangeSet."""

    def __init__(self, access: Any, *, changeset_id: Optional[str] = None) -> None:
        self.access, self.changeset_id = access, changeset_id
        self.address = str(access.host)

    def run(self, command: str, *, timeout: int = 120, stdin_text: Optional[str] = None) -> Any:
        return self.access.run(command, timeout=timeout, stdin_text=stdin_text,
                               write=bool(self.changeset_id), changeset_id=self.changeset_id)


def apply_and_env(engine: Any, *, deployment_id: str, project_id: str, resources: list[ResourceRequest],
                  actor_id: Optional[str], is_cancelled: Callable[[], bool], host: Any = None
                  ) -> tuple[list[dict], dict[str, str]]:
    """The stack apply's one call: run the approved plan, then the binding values its env file
    gets (resolved from the vault now; the caller adds them to what it redacts)."""
    from .executor import env_values
    results = apply_for_deployment(engine, deployment_id=deployment_id, project_id=project_id,
                                   resources=resources, actor_id=actor_id, is_cancelled=is_cancelled,
                                   host=host)
    with engine.begin() as c:
        row = plan_for_deployment(c, deployment_id)
    if row is None:
        return results, {}
    envs = {r.environment for r in resources} or {"deploy"}
    values: dict[str, str] = {}
    for env in envs:
        values.update(env_values(engine, project_id=project_id, app_key=row["app_key"], environment=env))
    return results, values


__all__ = ["apply_for_deployment", "plan_for_deploy", "preview", "store_plan", "summary_words"]

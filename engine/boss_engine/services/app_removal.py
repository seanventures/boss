"""Remove a deployed app from its host, governed exactly like a deploy.

Community refused every host teardown ("Direct teardown is disabled…"), so the only way to
remove an app was a person's ssh session. A removal is now a deployment run of kind `remove`:
the same plan → ChangeSet (effect `deployment_target.teardown`) → approval (or the environment's
policy, deploy_policy.pre_approval) → leased `deployment.host_apply` job → receipt path as a
deploy, with its steps on the run so "Open full log" shows it.

What it touches is computed here and nowhere else, and only ever:
  * the app's compose projects (`docker compose -p <project> down`, `-v` only on delete_data);
  * the app's single container (named after the app, as deploy_service names it);
  * the app's folders, each strictly ONE segment under /opt/boss-apps/.
Anything else is refused before a ChangeSet exists.
"""
from __future__ import annotations

import json
import posixpath
import re
import shlex
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import create_blocking_decision, emit_event, make_id

REMOTE_BASE = "/opt/boss-apps"
_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
KIND = "remove"


class RemovalRefused(ValueError):
    """A removal that would touch something that is not this app's."""


def guard_folder(path: Any) -> str:
    """`path` if it is exactly /opt/boss-apps/<one app segment>, else RemovalRefused."""
    raw = str(path or "").strip()
    norm = posixpath.normpath(raw) if raw else ""
    if not norm.startswith(REMOTE_BASE + "/") or norm != raw.rstrip("/"):
        raise RemovalRefused(f"{raw or '(empty)'} is not a folder under {REMOTE_BASE}/")
    seg = norm[len(REMOTE_BASE) + 1:]
    if "/" in seg or not _SEGMENT.match(seg) or seg in (".", ".."):
        raise RemovalRefused(f"{raw} is not one app folder directly under {REMOTE_BASE}/")
    return norm


def guard_project(name: Any) -> str:
    raw = str(name or "").strip()
    if not _PROJECT.match(raw):
        raise RemovalRefused(f"{raw or '(empty)'} is not a compose project name BOSS deploys")
    return raw


def removal_scope(connection: Connection, target: dict) -> dict[str, list[str]]:
    """The folders, compose projects and container this target's app owns on its host."""
    from .deploy_service import _app_name

    folders: list[str] = []
    projects: list[str] = []
    rows = connection.execute(
        text("SELECT deploy_dir, manifest FROM stack_deployments WHERE target_id = :t"),
        {"t": target["id"]}).mappings().all()
    for r in rows:
        manifest = r["manifest"] if isinstance(r["manifest"], dict) else json.loads(r["manifest"] or "{}")
        if r["deploy_dir"]:
            folders.append(guard_folder(r["deploy_dir"]))
        # A compose app's project is `compose.project`, else its name (app_deploy_manifest);
        # manifests stored before `compose_project` was recorded carry only the name.
        # A compose app with resources ran in the RESOURCES' project (stack_deploy.
        # join_resource_project records it as `shared_compose_project`); removing only its own
        # name stopped nothing while the app and its database kept running (deprun_bdb914b2).
        shared = (manifest or {}).get("shared_compose_project")
        if shared:
            projects.append(guard_project(shared))
        proj = (manifest or {}).get("compose_project") or (
            (manifest or {}).get("name") if (manifest or {}).get("kind") == "compose" else None)
        if proj:
            projects.append(guard_project(proj))
    # NEVER ANOTHER APP'S. A folder or compose project that another ACTIVE target on the same
    # host also deploys to belongs to that app now (a rebuilt app reusing its predecessor's
    # folder): removing the old target must not remove the new one.
    shared = connection.execute(
        text("SELECT s.deploy_dir, s.manifest FROM stack_deployments s "
             "JOIN deployment_targets t ON t.id = s.target_id "
             "WHERE s.target_id <> :t AND t.status = 'active' AND s.host_id = "
             "(SELECT host_id FROM deployment_targets WHERE id = :t)"),
        {"t": target["id"]}).mappings().all()
    taken_dirs = {str(r["deploy_dir"] or "") for r in shared}
    taken_projects = set()
    for r in shared:
        m = r["manifest"] if isinstance(r["manifest"], dict) else json.loads(r["manifest"] or "{}")
        taken_projects.add((m or {}).get("compose_project") or (m or {}).get("name"))
        if (m or {}).get("shared_compose_project"):
            taken_projects.add(m["shared_compose_project"])
    folders = [f for f in folders if f not in taken_dirs]
    projects = [p for p in projects if p not in taken_projects]
    container = _app_name(str(target.get("repo") or ""), target.get("app_name"))
    single = f"{REMOTE_BASE}/{container}"
    has_single = connection.execute(
        text("SELECT 1 FROM deployment_runs WHERE deployment_target_id = :t AND "
             "COALESCE(release_packet->>'kind', 'deploy') <> 'remove' LIMIT 1"),
        {"t": target["id"]}).first() is not None
    containers: list[str] = []
    if has_single:
        folders.append(guard_folder(single))
        containers.append(container)
    folders = [f for f in folders if f not in taken_dirs]
    dedupe = lambda xs: list(dict.fromkeys(xs))  # noqa: E731
    return {"folders": dedupe(folders), "projects": dedupe(projects),
            "containers": dedupe(containers)}


def removal_script(scope: dict[str, list[str]], *, delete_data: bool) -> str:
    """The shell the host runs. Each path and name re-checked; nothing outside them."""
    parts = ["set -u"]
    for p in scope.get("projects") or []:
        q = shlex.quote(guard_project(p))
        flag = " -v" if delete_data else ""
        parts.append(f"(docker compose -p {q} down --remove-orphans{flag} 2>&1 || "
                     f"docker-compose -p {q} down --remove-orphans{flag} 2>&1); "
                     f"echo 'stopped compose project {p}'")
    for c in scope.get("containers") or []:
        q = shlex.quote(guard_project(c))
        parts.append(f"docker rm -f {q} >/dev/null 2>&1; echo 'removed container {c}'")
    for f in scope.get("folders") or []:
        q = shlex.quote(guard_folder(f))
        if delete_data:
            parts.append(f"rm -rf -- {q}; echo 'deleted {f}'")
        else:
            # Kept: the app's data/ (single-container volume); everything else goes.
            parts.append(f"if [ -d {q} ]; then find {q} -mindepth 1 -maxdepth 1 ! -name data "
                         f"-exec rm -rf -- {{}} +; rmdir {q} 2>/dev/null || true; fi; "
                         f"echo 'removed {f} (data kept)'")
    return "\n".join(parts)


def plan_removal(rp, connection: Connection, *, project_id: str, target: dict, actor: str,
                 delete_data: bool = False, reason: str = "") -> dict[str, Any]:
    """Record the removal of `target`'s app, held for approval unless policy pre-approves it."""
    from .budget import BudgetGovernor
    from .deploy_policy import pre_approval, record_pre_approval
    from .deployment_connectors import _split_ref
    from .governed_host_deploy import (OPERATION_SKILL, REASON_CODE, _host_for,
                                       approve_and_queue)

    scope = removal_scope(connection, target)
    if not any(scope.values()):
        raise RemovalRefused(f"Nothing of {target.get('app_name')} is deployed by BOSS to remove.")
    host = _host_for(connection, target) or {}
    app = str(target.get("app_name") or "app")
    environment = str(target.get("environment") or "")
    host_name = str(host.get("name") or _split_ref(target.get("target_ref") or "")[1])
    user, address, _port = _split_ref(target.get("target_ref") or "")
    run_id = make_id("deprun")
    risk_tier = 3 if environment == "production" else 2
    reservation = BudgetGovernor(rp.settings).reserve(
        connection, project_id, "provider", 0, f"Remove {app} from {host_name} ({run_id})",
        expires_in_minutes=24 * 60, risk_tier=risk_tier)
    if reservation.get("status") == "denied" or not reservation.get("reservation_id"):
        raise RemovalRefused("The budget refused to record this removal's reservation.")
    data_words = "and DELETE its data" if delete_data else "keeping its data"
    rollback_plan = ["Redeploy the app from Infra › Deploys (its last release is on the branch)"
                     + ("" if delete_data else "; its data was kept")]
    desired = {"kind": KIND, "deployment_run_id": run_id, "deployment_target_id": target["id"],
               "app_name": app, "host": host_name, "address": address, "user": user,
               "environment": environment, "delete_data": bool(delete_data), **scope}
    changeset = rp.create_changeset(
        connection, project_id=project_id, provider_account_id=None,
        title=f"Remove {app} from {host_name} · {environment}",
        summary=(f"Stop and remove {app} on {user}@{address}, {data_words}: "
                 + "; ".join(scope["projects"] + scope["containers"] + scope["folders"])),
        operations=[{"skill_id": OPERATION_SKILL, "action": "teardown",
                     "resource_type": "ssh_host.app", "resource_ref": f"{target['id']}:{app}",
                     "desired_state": desired, "risk_tier": 1, "destructive": True,
                     "effect_kind": "deployment_target.teardown",
                     "production_affecting": environment == "production"}],
        expected_cost_usd_micros=0, reservation_id=reservation["reservation_id"],
        rollback_plan=rollback_plan, risk_tier=1, approval_required=False, actor=actor)
    review = {"app_name": app, "environment": environment, "host": host_name,
              "address": address, "operation": "remove", "delete_data": bool(delete_data),
              "removes": scope, "reason": reason[:300],
              "steps": ["Resolve what the app owns", "Stop and remove it on the host"],
              "rollback": rollback_plan}
    question = f"Remove {app} from {host_name} · {environment}" + (" (and its data)" if delete_data else "")
    recommendation = {**review, "action": "deploy_host", "deployment_run_id": run_id,
                      "changeset_id": changeset["id"], "target_id": target["id"]}
    approval = pre_approval(connection, project_id, environment)
    if approval and rp.required_approvers_for_changeset(connection, changeset["id"]):
        approval = None
    if approval:
        decision_id = approval["decision_id"]
    else:
        decision_id = create_blocking_decision(
            connection, project_id, None, question, recommendation,
            {"expected_cost_usd_micros": 0, "reservation_id": reservation["reservation_id"]},
            REASON_CODE, risk_tier=risk_tier, default_action="hold")
        connection.execute(text("UPDATE decisions SET external_ref = :c WHERE id = :d"),
                           {"c": changeset["id"], "d": decision_id})
    connection.execute(
        text("UPDATE changesets SET status = 'pending_approval', risk_tier = :r, "
             "approval_decision_id = :d, updated_at = now() WHERE id = :c"),
        {"d": decision_id, "c": changeset["id"], "r": risk_tier})
    packet = {"governed": True, "kind": KIND, "removal": desired,
              "plan_review": {**review, "decision_id": decision_id,
                              "changeset_id": changeset["id"]}}
    connection.execute(
        text("""
            INSERT INTO deployment_runs (
                id, project_id, deployment_target_id, changeset_id, decision_id, app_name,
                environment, repo, target_kind, target_ref, status, release_packet,
                rollback_plan, release_receipt_ids, created_by, started_at, reservation_id,
                trigger
            ) VALUES (:id, :p, :t, :cs, :d, :app, :env, :repo, :kind, :ref, 'awaiting_approval',
                      CAST(:packet AS JSONB), CAST(:rb AS JSONB), CAST(:rr AS JSONB), :actor,
                      now(), :res, 'remove')"""),
        {"id": run_id, "p": project_id, "t": target["id"], "cs": changeset["id"],
         "d": decision_id, "app": app, "env": environment, "repo": target.get("repo") or "",
         "kind": target["target_kind"], "ref": target.get("target_ref") or "",
         "packet": json.dumps(packet), "rb": json.dumps(rollback_plan),
         "rr": json.dumps(changeset["expected_receipts"]), "actor": actor,
         "res": reservation["reservation_id"]})
    emit_event(connection, project_id, "deployment_run.removal_planned", actor,
               "deployment_run", run_id,
               {"deployment_target_id": target["id"], "changeset_id": changeset["id"],
                "decision_id": decision_id, "delete_data": bool(delete_data), **scope})
    if approval:
        record_pre_approval(connection, project_id, approval, entity_type="deployment_run",
                            entity_id=run_id, actor=actor)
        approve_and_queue(connection, rp.settings, run_id=run_id,
                          approver=approval["approved_by"], decision_id=decision_id)
    return rp._get_deployment_run(connection, project_id, run_id)


def is_removal(row: Any) -> bool:
    packet = (row.get("release_packet") if hasattr(row, "get") else None) or {}
    if isinstance(packet, str):
        packet = json.loads(packet or "{}")
    return packet.get("kind") == KIND


def remove_on_host(engine, settings, *, row: dict, governance: dict) -> dict[str, Any]:
    """Phase two of a removal: one SSH session running removal_script, its output on the run."""
    from ..db import begin
    from .deploy_service import _discard_key_file, _materialise_key, _ssh, _ssh_key_file
    from .deployment_connectors import _split_ref
    from .deployment_steps import StepRecorder
    from .governed_host_deploy import require_governed_host_write
    from .secret_backends import resolve_target_secret

    packet = row.get("release_packet") or {}
    if isinstance(packet, str):
        packet = json.loads(packet or "{}")
    removal = packet.get("removal") or {}
    rec = StepRecorder(engine, row["id"])
    try:
        with rec.step("resolve", label="Resolve what the app owns") as h:
            scope = {k: list(removal.get(k) or []) for k in ("projects", "containers", "folders")}
            script = removal_script(scope, delete_data=bool(removal.get("delete_data")))
            h.log(json.dumps(scope))
        with begin(engine) as c:
            require_governed_host_write(c, governance)
            trow = c.execute(text("SELECT * FROM deployment_targets WHERE id = :t"),
                             {"t": row["deployment_target_id"]}).mappings().first()
            target = dict(trow or {})
            material = resolve_target_secret(c, target, "ssh_key", settings) if target else None
        key = _materialise_key(material) if material else _ssh_key_file(settings)
        user, host, port = _split_ref(target.get("target_ref") or row.get("target_ref") or "")
        with rec.step("remove", label="Stop and remove it on the host") as h:
            try:
                res = _ssh(key, host, user, port, script, timeout=300)
            finally:
                if material:
                    _discard_key_file(key)
            h.log((res.stdout or "")[-1800:] + (res.stderr or "")[-400:])
            if res.returncode != 0:
                h.failed(f"the host answered exit {res.returncode}")
        rec.finish()
        if res.returncode != 0:
            return {"status": "failed", "reason": f"Removal stopped on the host (exit {res.returncode})."}
        return {"status": "succeeded", "applied": {"host": host, "app": row.get("app_name"),
                                                   "operation": "removed"}}
    except RemovalRefused as refused:
        rec.finish()
        return {"status": "blocked", "reason": str(refused)[:400]}
    except Exception as exc:  # noqa: BLE001 — a sentence, never a trace
        rec.finish()
        return {"status": "failed", "reason": (str(exc).strip() or "The removal stopped.")[:400]}


def after_removal(connection: Connection, row: dict, status: str, actor: str) -> None:
    """A removed app's target says so; the run's events name what was removed."""
    if status != "succeeded":
        return
    connection.execute(text("UPDATE deployment_targets SET status = 'retired', updated_at = now() "
                            "WHERE id = :t"), {"t": row["deployment_target_id"]})
    emit_event(connection, row["project_id"], "deployment_target.removed", actor,
               "deployment_target", row["deployment_target_id"],
               {"deployment_run_id": row["id"]})

"""Read-only first-workflow agreements. A binding protects the preview, not authority.

No job, model call, reservation or provider effect is created here. Existing start and
per-step identity, consent, budget and policy admission remain authoritative.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any

from sqlalchemy import text

from boss_engine.identity.authn_support import keyed_hash
from . import repo_registry, workflow_custom_steps, workflow_explain, workflows
from .workflow_costs import workflow_costs

ACTION = "delivery_workflow.start"
TTL_SECONDS = 600


class AgreementConflict(ValueError):
    """The displayed agreement no longer binds the requested start."""


def first_input(directive: str) -> dict[str, str]:
    directive = directive.strip()
    if not directive or len(directive) > 20_000:
        raise ValueError("Describe the work in at most 20,000 characters.")
    return {"title": directive[:69] + "…" if len(directive) > 72 else directive,
            "directive": directive}


def intent_key(binding_token: str) -> str:
    """Opaque correlation key; it grants no access and contains no private brief."""
    return "agreement:" + hashlib.sha256(binding_token.encode()).hexdigest()


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str) and item.strip()] \
        if isinstance(value, list) else []


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _workflow(connection, project_id: str, work_class: str, workflow_key: str) -> dict:
    if workflow_key:
        found = workflows.running_version(connection, project_id, workflow_key)
        if not found:
            raise AgreementConflict("The selected workflow is unavailable. Review the work again.")
        return found
    selection = workflows.resolve_mapping(connection, project_id=project_id,
                                          selector_kind="work_class", selector_value=work_class)
    selected = selection.get("selected") or {}
    if not selected.get("workflow_version_id"):
        raise AgreementConflict(selection.get("reason") or "No workflow is selected for this work.")
    row = connection.execute(text(
        "SELECT d.workflow_key,d.name,v.id AS version_id,v.version,v.spec "
        "FROM workflow_versions v JOIN workflow_definitions d ON d.id=v.workflow_definition_id "
        "WHERE v.id=:v AND d.project_id=:p AND v.status='published' AND d.status='active'"),
        {"v": selected["workflow_version_id"], "p": project_id}).mappings().first()
    if not row:
        raise AgreementConflict("The selected workflow is unavailable. Review the work again.")
    return dict(row)


def _repository(connection, settings, project_id: str, run_input: dict) -> dict | None:
    try:
        target = repo_registry.resolve_repository_target(
            connection, settings, project_id, selector=run_input.get("repository"))
    except repo_registry.RepositoryResolutionError:
        return None
    # Safe metadata only. No token, credential reference or provider configuration is exposed.
    return {"full_name": target["full_name"], "base_branch": run_input.get("base_branch")
            or target.get("default_branch"), "base_revision": None,
            "source": target.get("source"), "binding_id": target.get("binding_id"),
            "selection": "supplied" if run_input.get("repository") else "current_project_target"}


def _material(connection, settings, *, project_id: str, principal: str, run_input: dict,
              work_class: str, workflow_key: str, start_anyway: list[str]) -> tuple[dict, dict]:
    selected = _workflow(connection, project_id, work_class, workflow_key)
    repository = _repository(connection, settings, project_id, run_input)
    # A published spec references editable custom definitions by key. Bind those current
    # definitions and resolved approval facts too; their private knowledge stays in the hash.
    custom = {}
    custom_targets = {}
    raw_steps = selected["spec"].get("steps") or []
    for step in raw_steps:
        key = str((step.get("binding") or {}).get("custom_step") or "")
        if key:
            custom[key] = workflow_custom_steps.get(connection, project_id, key)
            definition = custom[key]
            if definition and definition["binding_kind"] == "mcp_tool":
                row = connection.execute(text(
                    "SELECT id,url,transport,auth FROM mcp_servers WHERE id=:s AND project_id=:p"),
                    {"s": definition["mcp_server_id"], "p": project_id}).mappings().first()
                custom_targets[key] = dict(row) if row else None
    explained = workflow_explain.explain_version(connection, project_id=project_id,
                                                workflow_key=selected["workflow_key"],
                                                version=selected["version"])
    steps = []
    templates = {step["id"]: step for step in raw_steps}
    for original in explained.get("steps") or []:
        # Fixed input values can contain private configuration. Do not project them.
        step = {key: original.get(key) for key in
                ("id", "label", "does", "capability", "runs", "agent", "approval", "approval_note", "produces", "next")}
        template = templates[step["id"]]
        key = str((template.get("binding") or {}).get("custom_step") or "")
        if key:
            definition = custom[key]
            if not definition or not definition["enabled"] or not definition["usable"]:
                step["approval"] = "blocked"
                step["approval_note"] = "This custom step is unavailable; execution refuses until its binding is usable."
            else:
                step["approval"] = workflow_custom_steps.approval_for(
                    definition, (template.get("approval") or {}).get("mode"))
                step["approval_note"] = (
                    definition.get("requires_person") or "A person must release this custom step."
                    if step["approval"] == "manual" else "Runs without stopping at a step approval; execution admission still applies.")
        steps.append(step)
    plan = {"steps": steps, "results": explained.get("results") or []}
    material = {"schema": 1, "action": ACTION, "project_id": project_id,
                "principal": principal, "work_class": work_class, "workflow_key": workflow_key,
                "workflow_version_id": selected["version_id"], "spec": selected["spec"],
                "workflow_name": selected["name"], "resolved_workflow_key": selected["workflow_key"],
                "custom_steps": custom, "custom_targets": custom_targets, "plan": plan,
                "input": run_input, "repository": repository,
                "start_anyway": sorted(set(start_anyway))}
    return material, selected


def preview(connection, settings, *, project_id: str, principal: str, directive: str,
            start_anyway: list[str] | None = None, now: int | None = None) -> dict:
    """Describe exactly this initial workflow intent, without executing or approving it."""
    run_input = first_input(directive)
    accepted = _strings(start_anyway or [])
    material, selected = _material(connection, settings, project_id=project_id,
                                  principal=principal, run_input=run_input,
                                  work_class="project.new", workflow_key="", start_anyway=accepted)
    content_hash = hashlib.sha256(_canonical(material).encode()).hexdigest()
    expires = (int(time.time()) if now is None else now) + TTL_SECONDS
    nonce = secrets.token_hex(16)
    signature = keyed_hash("work-agreement.v1", f"{expires}.{content_hash}.{nonce}")
    binding_token = f"{expires}.{content_hash}.{nonce}.{signature}"
    plan = material["plan"]
    steps = plan["steps"]
    history = workflow_costs(connection, project_id).get("costs", {}).get(selected["workflow_key"])
    return {"schema_version": 1, "action": ACTION, "project_id": project_id,
            "content_hash": content_hash, "binding_token": binding_token, "start_intent_key": intent_key(binding_token),
            "expires_at_unix": expires, "workflow_key": selected["workflow_key"],
            "workflow_name": selected["name"], "workflow_version_id": selected["version_id"],
            "workflow_version": selected["version"], "entry_step": selected["spec"].get("entry_step"), "input": run_input,
            "start_anyway": sorted(set(accepted)), "repository": material["repository"],
            # This door starts planning. An approved PRD and routed account do not exist yet.
            "approved_spec": None, "acceptance_criteria": [], "test_commands": [],
            "selected_provider": None, "selected_account": None,
            "steps": steps, "results": plan.get("results") or [],
            "stop_conditions": [step["approval_note"] for step in steps
                                if step.get("approval") == "manual" and step.get("approval_note")],
            "financial": {"estimate_usd_micros": None, "reservation_usd_micros": None,
                          "authorized_limit_usd_micros": None, "history": history,
                          "history_basis": "API-equivalent value of prior completed runs; may include subscription usage."},
            "bounds": ["Starts the displayed planning/delivery workflow for this supplied brief.",
                       "The later specification and applicable decisions still govern the build.",
                       "Existing identity, consent, budget and policy checks apply at execution."],
            "unknowns": ["No approved task specification or checks are recorded yet.",
                         "Provider and account selection happen when each model step is admitted.",
                         "No task estimate, reservation or new financial authority is created by this preview.",
                         "The base commit has not been read from the repository."]}


def validate(connection, settings, *, project_id: str, principal: str, run_input: dict,
             work_class: str, workflow_key: str, start_anyway: list[str],
             content_hash: str, binding_token: str, now: int | None = None) -> str:
    """Re-read the material snapshot before start effects; return the exact bound version.

    The MAC proves this is a server-issued preview. It never replaces current admission.
    Statistical history and timestamps are deliberately absent from the material snapshot.
    """
    try:
        expiry, token_hash, nonce, signature = binding_token.split(".")
        expires = int(expiry)
    except (ValueError, AttributeError) as exc:
        raise AgreementConflict("Review the work again; the agreement binding is invalid.") from exc
    if not all(isinstance(v, str) and re.fullmatch(r"[a-f0-9]{64}", v) for v in (token_hash, signature, content_hash)):
        raise AgreementConflict("Review the work again; the agreement binding is invalid.")
    if not re.fullmatch(r"[a-f0-9]{32}", nonce):
        raise AgreementConflict("Review the work again; the agreement binding is invalid.")
    current_time = int(time.time()) if now is None else now
    if expires <= current_time or expires > current_time + TTL_SECONDS:
        raise AgreementConflict("This agreement expired. Review the work again before starting.")
    expected_signature = keyed_hash("work-agreement.v1", f"{expires}.{token_hash}.{nonce}")
    if not hmac.compare_digest(signature, expected_signature) or not hmac.compare_digest(token_hash, content_hash):
        raise AgreementConflict("Review the work again; the agreement binding is invalid.")
    material, selected = _material(connection, settings, project_id=project_id, principal=principal,
                                  run_input=run_input, work_class=work_class,
                                  workflow_key=workflow_key, start_anyway=start_anyway)
    current_hash = hashlib.sha256(_canonical(material).encode()).hexdigest()
    if not hmac.compare_digest(current_hash, content_hash):
        raise AgreementConflict("The work, project or workflow changed. Review the current agreement before starting.")
    return str(selected["version_id"])

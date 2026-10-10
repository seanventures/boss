"""Which tool arguments name an object, and of which kind (IDENTITY.md §6.3, §6.4; int-b).

A tool is checked at the project the call acts in. An id in its arguments — a `run_id`, a
`target_id`, a `decision_id` — names a row that has a project of its own, and a handler that loads
the row by id alone would let a permission held in project A reach project B's object. Every id
argument of every built-in tool is listed here: CONFINED to the call's project (the registry's
identity gate answers not-found for another project's object), or UNCONFINED with the reason the
id cannot reach another project. `test_identity_authz_tool_object_args` fails on a tool with an
`*_id` argument in neither list.

Deploying tools also say where their TARGET comes from, so a production target needs
deployment:production at the target's project (object_rules.Production), like the routes.
"""
from __future__ import annotations

from typing import Optional

from ..identity.object_rules import Confine, Production

CODE_TASK = "code_task"

#: tool -> {argument: object kind (or kinds)}. A kind is an identity.object_kinds entry.
CONFINED: dict[str, dict[str, str | tuple[str, ...]]] = {
    "add_review_comment": {"run_id": CODE_TASK},
    "cancel_code_task": {"run_id": CODE_TASK},
    "cancel_job": {"job_id": "job"},
    "create_code_task": {"task_id": "task", "workflow_run_id": "workflow_run",
                         "extends_run_id": CODE_TASK, "preview_id": "app_preview"},
    "create_deployment_target": {"host_id": "deployment_host"},
    "decide": {"decision_id": "decision"},
    "deploy_app": {"run_id": CODE_TASK, "target_id": "deployment_target", "host_id": "deployment_host"},
    "describe_deployment_target": {"target_id": "deployment_target"},
    "describe_workflow_run": {"run_id": "workflow_run"},
    "diagnose_run": {"run_id": CODE_TASK},
    "explain_run": {"run_id": CODE_TASK},
    "extend_code_task": {"run_id": CODE_TASK},
    "forget": {"memory_id": "agent_memory"},
    "get_code_task": {"run_id": CODE_TASK},
    "get_investigation": {"investigation_id": "investigation"},
    "get_meeting": {"meeting_id": "meeting"},
    "get_run_activity": {"run_id": CODE_TASK},
    "get_task_preview": {"run_id": CODE_TASK},
    "git_history": {"run_id": CODE_TASK},
    "investigate": {"run_id": CODE_TASK},
    "link_run_origin": {"run_id": CODE_TASK, "meeting_run_id": ("meeting_run", "discovery_run"),
                        "task_id": "task"},
    "list_review_comments": {"run_id": CODE_TASK},
    "list_source": {"run_id": CODE_TASK},
    "preflight_deployment_target": {"run_id": CODE_TASK},
    "provider_audit": {"run_id": CODE_TASK},
    "read_code": {"run_id": CODE_TASK},
    "read_preview_log": {"preview_id": "app_preview"},
    "read_run_turns": {"run_id": CODE_TASK},
    "read_spec": {"run_id": (CODE_TASK, "meeting")},
    "recheck_workflow_run": {"run_id": "workflow_run"},
    "redeploy": {"run_id": CODE_TASK},
    "register_repository": {"connection_id": "github_connection"},
    "request_meeting": {"task_id": "task"},
    "resolve_review_comment": {"run_id": CODE_TASK},
    "retarget_app": {"target_id": "deployment_target", "host_id": "deployment_host"},
    "retry_code_task": {"run_id": CODE_TASK},
    "retry_workflow_step": {"run_id": "workflow_run"},
    "search_code": {"run_id": CODE_TASK},
    "send_feedback": {"run_id": CODE_TASK},
    "set_job_priority": {"job_id": "job"},
    "skip_workflow_step": {"run_id": "workflow_run"},
    "start_preview": {"run_id": CODE_TASK},
    "steer_code_task": {"run_id": CODE_TASK},
    "stop_preview": {"preview_id": "app_preview"},
    "stop_watching": {"wakeup_id": "agent_wakeup"},
    "test_github_access": {"connection_id": "github_connection"},
    "update_ledger": {"artifact_id": "artifact"},
    "waive_gates": {"run_id": CODE_TASK},
    "watch_code_task": {"run_id": CODE_TASK},
}

#: tool -> {argument: why it cannot reach another project}. `project_id` is never listed: the
#: identity gate checks the tool's permission AT the project_id argument when one is given.
UNCONFINED: dict[str, dict[str, str]] = {
    "merge_apps": {"keep_app_id": "an app id is a finalize_ctx label, matched only within the "
                                  "call's project by the handler",
                   "merge_app_ids": "same: matched only within the call's project"},
    "resolve_review_comment": {"comment_id": "looked up under its (confined) run_id"},
    "retry_workflow_step": {"step_id": "a step of the (confined) run_id"},
    "skip_workflow_step": {"step_id": "a step of the (confined) run_id"},
    "system_doctor_act": {"finding_id": "an install-wide health finding; the tool needs "
                                        "system:operate, an install-level permission"},
}

#: Deploying tools: where the target comes from (the same rule the deploy routes declare).
PRODUCTION: dict[str, Production] = {
    "deploy_app": Production(target="args:target_id", environment="args:environment",
                             environment_default="staging"),
    "redeploy": Production(code_task="args:run_id"),
}


def rules_for(tool: str) -> tuple[tuple[Confine, ...], Optional[Production]]:
    confine = tuple(Confine.arg(arg, kind) for arg, kind in CONFINED.get(tool, {}).items())
    return confine, PRODUCTION.get(tool)

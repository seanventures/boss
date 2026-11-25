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
    "apply_template": {"extends_run_id": CODE_TASK},
    "cancel_code_task": {"run_id": CODE_TASK},
    "cancel_job": {"job_id": "job"},
    "collect_diagnostics": {"task_id": "task"},
    "create_code_task": {"task_id": "task", "workflow_run_id": "workflow_run",
                         "extends_run_id": CODE_TASK, "preview_id": "app_preview"},
    "create_deployment_target": {"host_id": "deployment_host"},
    "decide": {"decision_id": "decision"},
    "deploy_app": {"run_id": CODE_TASK, "target_id": "deployment_target", "host_id": "deployment_host",
                   "approval_decision_id": "decision"},
    "deploy_config": {"target_id": "deployment_target", "approval_decision_id": "decision",
                      "code_task_run_id": CODE_TASK},
    "promote_config": {"deployment_run_id": "deployment_run", "deployment_id": "deployment_run",
                       "target_id": "deployment_target",
                       "approval_decision_id": "decision"},
    "rollback_config": {"deployment_run_id": "deployment_run"},
    "create_config_target": {"host_id": "deployment_host"},
    "describe_deployment_target": {"target_id": "deployment_target"},
    "describe_workflow_run": {"run_id": "workflow_run"},
    "precheck_workflow": {"run_id": "workflow_run", "build_run_id": CODE_TASK},
    "diagnose_run": {"run_id": CODE_TASK},
    "explain_run": {"run_id": CODE_TASK},
    "extend_code_task": {"run_id": CODE_TASK},
    "forget": {"memory_id": ("agent_memory", "kg_node")},
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
    "list_security_findings": {"scan_id": "security_scan"},
    "list_source": {"run_id": CODE_TASK},
    "preflight_deployment_target": {"run_id": CODE_TASK},
    "provider_audit": {"run_id": CODE_TASK},
    "read_code": {"run_id": CODE_TASK},
    "read_preview_log": {"preview_id": "app_preview"},
    "read_run_turns": {"run_id": CODE_TASK},
    "read_spec": {"run_id": (CODE_TASK, "meeting")},
    "recheck_workflow_run": {"run_id": "workflow_run"},
    "propose_secret": {"target_id": "deployment_target"},
    "redeploy": {"run_id": CODE_TASK},
    "register_repository": {"connection_id": "github_connection"},
    "request_meeting": {"task_id": "task"},
    "run_security_scan": {"workflow_run_id": "workflow_run"},
    "resolve_review_comment": {"run_id": CODE_TASK},
    "review_pull_request_again": {"run_id": CODE_TASK},
    "recheck_change": {"run_id": CODE_TASK},
    "retarget_app": {"target_id": "deployment_target", "host_id": "deployment_host"},
    "update_deployment_target": {"target_id": "deployment_target", "host_id": "deployment_host"},
    "retry_code_task": {"run_id": CODE_TASK},
    "retry_workflow_step": {"run_id": "workflow_run"},
    "search_code": {"run_id": CODE_TASK},
    "security_review_status": {"run_id": "workflow_run"},
    "security_scan_status": {"scan_id": "security_scan"},
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
    "wait_for": {"id": ("job", CODE_TASK, "workflow_run")},
    # The work-item graph (BOSSAPP-830/832).
    "ask_human": {"work_item_id": "work_item"},
    "complete_work_item": {"item_id": "work_item"},
    "create_work_item": {"parent_id": "work_item"},
    "link_work_items": {"item_id": "work_item", "other_id": "work_item"},
    "list_work_items": {"item_id": "work_item"},
    "offer_options": {"work_item_id": "work_item"},
    "watch_work_item": {"work_item_id": "work_item"},
    # X2 (2026-11-21): ids that reached a handler with no central check. Each handler also
    # filters by project today; confined here so the gate answers first, the same way for all.
    "autonomy_report": {"run_id": "workflow_run"},
    "file_platform_gap": {"run_id": (CODE_TASK, "workflow_run", "meeting_run", "discovery_run")},
    "get_self_fix": {"self_fix_id": "self_fix"},
    "request_self_fix": {"recommendation_id": "boss_recommendation"},
    "request_budget_override": {"run_id": CODE_TASK},
    "list_deployments": {"deployment_id": "stack_deployment"},
    "kg_why": {"node_id": "kg_node"},
    "kg_callers": {"node_id": "kg_node"},
    "kg_blast_radius": {"node_id": "kg_node"},
}

_SUBAGENT = ("a Task id this kernel run started: refused unless it is in the run's own "
             "`subagents` state (kernel/subagents.py), so it never names another run's child")
_CONFLUENCE = ("a page id in the external Confluence, read and written only through the call's "
               "project's own docs connection (docs_connections.client_for(project))")
_TICKETS = ("a ticket connection, org-shared by design: ticket_reach.reachable_row/lookup accept "
            "only the call's project's own connection or one shared with its org; an unshared "
            "one of another project is refused not_shared, another org's is not found")
_STORAGE = ("an install-wide storage incident on a BOSS machine's own disk (its project_id is "
            "only where its events are written); every storage tool is decided org-wide "
            "(org_scope) on system:read / system:operate")

#: tool -> {argument: why it cannot reach another project}. `project_id` is never listed: the
#: identity gate checks the tool's permission AT the project_id argument when one is given.
UNCONFINED: dict[str, dict[str, str]] = {
    # Templates belong to the ORG, not a project: the handler reads them only in the org that
    # holds the call's project, so another org's template id names nothing.
    "apply_template": {"template_id": "an org's template, read only in the call's project's org",
                       "templates": "connectors' ids or keys, each read only in the call's project's org"},
    "describe_template": {"template_id": "an org's template, read only in the call's project's org"},
    "merge_apps": {"keep_app_id": "an app id is a finalize_ctx label, matched only within the "
                                  "call's project by the handler",
                   "merge_app_ids": "same: matched only within the call's project"},
    "resolve_review_comment": {"comment_id": "looked up under its (confined) run_id"},
    "review_pull_request_again": {"pull_request_number": "matched only among the call's "
                                                         "project's builds by the handler"},
    "retry_workflow_step": {"step_id": "a step of the (confined) run_id"},
    "skip_workflow_step": {"step_id": "a step of the (confined) run_id"},
    "system_doctor_act": {"finding_id": "an install-wide health finding; the tool needs "
                                        "system:operate, an install-level permission"},
    "kg_neighbors": {"node_id": "an alias of node: looked up only in the call's project's "
                                "graph (typed_neighbors(project_id=...))"},
    "create_work_item": {"goal_id": "read only WHERE project_id = the call's project "
                                    "(work_items._check_goal): another project's goal is not found"},
    # X2 (2026-11-21): every remaining id argument, each with why it cannot reach another project.
    **{t: {"task_id": _SUBAGENT} for t in ("check_subagent", "update_subagent", "cancel_subagent")},
    "confluence_read_page": {"page_id": _CONFLUENCE},
    "confluence_publish_page": {"page_id": _CONFLUENCE, "parent_id": _CONFLUENCE},
    "set_ontology_pack": {"pack_id": "a key into the install's shipped ontology-pack catalogue "
                                     "(ontology_packs.shipped_packs); added to the call's project"},
    "update_ledger": {"session_id": "a caller-chosen label, keyed as session:<the call's "
                                    "project>:<id> (_ledger_thread): never another project's"},
    **{t: {"connection_id": _TICKETS} for t in ("ticket_browse", "ticket_comment", "ticket_get",
                                                "ticket_import", "ticket_search",
                                                "ticket_transition")},
    "storage_inspect": {"host_id": "a BOSS machine's own host id (BOSS_HOST_ID / storage "
                                   "samples), not a deployment_hosts row; install-wide",
                        "incident_id": _STORAGE},
    **{t: {"incident_id": _STORAGE} for t in ("storage_plan", "storage_run_step",
                                              "storage_propose_rule", "storage_report",
                                              "work_hold_set", "work_hold_release")},
}

#: Deploying tools: where the target comes from (the same rule the deploy routes declare).
PRODUCTION: dict[str, Production] = {
    "deploy_app": Production(target="args:target_id", environment="args:environment",
                             environment_default="staging"),
    "redeploy": Production(code_task="args:run_id"),
    # Changing a target's host, port, health check or env changes what a production deploy does.
    "update_deployment_target": Production(target="args:target_id"),
    "retarget_app": Production(target="args:target_id"),
    # Configuration deploys (config_deploy, BOSSAPP-1140): the target names the environment; a
    # promotion defaults to production, so it needs deployment:production unless told otherwise.
    "deploy_config": Production(target="args:target_id", environment="args:environment",
                                environment_default="staging"),
    "promote_config": Production(target="args:target_id", environment="args:environment",
                                 environment_default="production"),
    # A rollback acts where the deployment it undoes shipped (BOSSAPP-1173).
    "rollback_config": Production(deployment_run="args:deployment_run_id"),
}


def rules_for(tool: str) -> tuple[tuple[Confine, ...], Optional[Production]]:
    confine = tuple(Confine.arg(arg, kind) for arg, kind in CONFINED.get(tool, {}).items())
    return confine, PRODUCTION.get(tool)

"""Object loaders: how `Scope.of(kind, ...)` finds an object's org and project (IDENTITY.md §6.3).

Stream S2. One entry per object kind a route declares. The loader answers (org_id, project_id) for
an id, or None when the object does not exist, which the enforcement point answers as 404. The org
comes from the object's project (projects -> workspaces -> orgs), never from the request.

Each kind is a table, its id column and the column holding the project (or a SQL expression over
the table aliased `t` for an indirect project). The SQL is built only from this module's constants;
the object id is always a bound parameter.

`test_identity_authz_object_kinds` runs every loader against the real schema (a missing table or
column fails there, not in production), and the coverage test fails on a declared kind with no
loader.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text

from . import scope as scope_module
from .epoch import connection


@dataclass(frozen=True)
class ObjectKind:
    kind: str
    table: str
    id_column: str = "id"
    #: a column of `table`, or a SQL expression over alias `t` that yields the project id
    project: str = "t.project_id"

    def sql(self) -> str:
        return (f"SELECT w.org_id, p.id FROM {self.table} t "
                f"JOIN projects p ON p.id = ({self.project}) "
                f"JOIN workspaces w ON w.id = p.workspace_id "
                f"WHERE t.{self.id_column}::text = :id LIMIT 1")

    def load(self, db: Any, object_id: str) -> Optional[tuple[str, str]]:
        with connection(db) as conn:
            row = conn.execute(text(self.sql()), {"id": str(object_id)}).first()
        return (str(row[0]), str(row[1])) if row else None


class UniqueProjectKind(ObjectKind):
    """An object found by a non-unique key (a dedupe key, a sandbox task id): it resolves only when
    every row the key matches is in ONE project; anything else is answered as not found."""

    def __init__(self, kind: str, sql: str) -> None:
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "table", "")
        object.__setattr__(self, "id_column", "")
        object.__setattr__(self, "project", "")
        object.__setattr__(self, "_projects_sql", sql)

    def sql(self) -> str:
        return ("SELECT w.org_id, p.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                f"WHERE p.id IN ({self._projects_sql})")

    def load(self, db: Any, object_id: str) -> Optional[tuple[str, str]]:
        with connection(db) as conn:
            rows = conn.execute(text(self.sql()), {"id": str(object_id)}).all()
        return (str(rows[0][0]), str(rows[0][1])) if len(rows) == 1 else None


class SecurityScanRunKind(ObjectKind):
    """Security-scan runs live in the engine's in-process registry, not a table."""

    def __init__(self) -> None:
        object.__setattr__(self, "kind", "security_scan_run")
        object.__setattr__(self, "table", "")
        object.__setattr__(self, "id_column", "")
        object.__setattr__(self, "project", "")

    def sql(self) -> str:
        return ("SELECT w.org_id, p.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                "WHERE p.id = :id")

    def load(self, db: Any, object_id: str) -> Optional[tuple[str, str]]:
        from ..services import security_scan

        run = security_scan.get_run(str(object_id))
        project_id = (run or {}).get("project_id")
        if not project_id:
            return None
        with connection(db) as conn:
            row = conn.execute(text(self.sql()), {"id": str(project_id)}).first()
        return (str(row[0]), str(row[1])) if row else None


#: Every object kind a route declares with Scope.of(...). Filled from the retrofit review: each
#: entry was checked against the route's handler and db/schema.sql.
KINDS: tuple[ObjectKind, ...] = (
    ObjectKind("app_preview", "app_previews"),
    ObjectKind("artifact", "artifacts"),
    ObjectKind("break_glass_grant", "break_glass_grants"),
    ObjectKind("changeset", "changesets"),
    ObjectKind("code_task", "code_task_runs"),
    ObjectKind("deadletter", "deadletters"),
    ObjectKind("decision", "decisions"),
    ObjectKind("deployment_host", "deployment_hosts"),
    ObjectKind("deployment_promotion", "deployment_promotions"),
    ObjectKind("deployment_run", "deployment_runs"),
    ObjectKind("deployment_target", "deployment_targets"),
    ObjectKind("discovery_run", "discovery_runs"),
    ObjectKind("feedback_thread", "feedback_threads"),
    ObjectKind("improvement_program", "improvement_programs"),
    ObjectKind("improvement_trial", "improvement_trials"),
    ObjectKind("job", "jobs"),
    ObjectKind("secret_ref", "secret_refs"),
    ObjectKind("shepherd_escalation", "shepherd_escalations"),
    ObjectKind("stack_deployment", "stack_deployments"),
    ObjectKind("task", "tasks"),
    ObjectKind("workflow_run", "workflow_runs"),
    # int-b: ids that ride in bodies, queries and tool arguments (object_rules.Confine)
    ObjectKind("agent_definition", "agent_definitions"),
    ObjectKind("agent_memory", "agent_memory"),
    ObjectKind("agent_wakeup", "agent_wakeups"),
    ObjectKind("budget_reservation", "budget_reservations"),
    ObjectKind("github_connection", "github_connections"),
    ObjectKind("investigation", "investigations"),
    ObjectKind("meeting_run", "meeting_runs"),
    ObjectKind("plan", "plans"),
    #: a body's own project_id, confined to the project the scope resolved (Confine.body)
    ObjectKind("project", "projects", project="t.id"),
    ObjectKind("resource_allocation", "resource_allocations"),
    ObjectKind("work_item", "work_items"),
    ObjectKind("work_tracker_binding", "work_tracker_bindings"),
    ObjectKind("workflow_version", "workflow_versions"),
    # F2/M9: every id a route names is confined centrally (ID_PARAM_KINDS below)
    ObjectKind("a2a_channel", "a2a_channels"),
    ObjectKind("a2a_delivery", "a2a_deliveries"),
    ObjectKind("a2a_envelope", "a2a_envelopes"),
    ObjectKind("a2a_subscription", "a2a_subscriptions"),
    ObjectKind("account_connect_auth_session", "account_connect_auth_sessions"),
    ObjectKind("agent_group_run", "agent_group_runs"),
    ObjectKind("agent_group_version", "agent_group_versions"),
    ObjectKind("agent_instruction", "agent_instructions"),
    ObjectKind("agent_key_entry", "agent_key_entries"),
    ObjectKind("agent_process_run", "agent_process_runs"),
    ObjectKind("agent_run", "agent_runs"),
    ObjectKind("agent_studio_run", "agent_studio_runs"),
    ObjectKind("agent_version", "agent_versions"),
    ObjectKind("assurance_case", "assurance_cases"),
    ObjectKind("assurance_claim", "assurance_claims"),
    ObjectKind("assurance_evidence", "assurance_evidence"),
    ObjectKind("budget_allocation", "budget_allocations"),
    ObjectKind("capability", "capabilities"),
    ObjectKind("capability_resolution", "capability_resolutions"),
    ObjectKind("delivery_bot_identity", "delivery_bot_identities"),
    ObjectKind("entitlement_connection_session", "entitlement_connection_sessions"),
    ObjectKind("entitlement_source", "entitlement_sources"),
    ObjectKind("eval_run", "eval_runs"),
    ObjectKind("event", "events"),
    ObjectKind("event_subscription", "event_subscriptions"),
    ObjectKind("experiment", "experiments"),
    ObjectKind("external_resource", "external_resources"),
    ObjectKind("github_branch_policy", "github_branch_policies"),
    ObjectKind("github_branch_snapshot", "github_branch_snapshots"),
    ObjectKind("github_code_sync_run", "github_code_sync_runs"),
    ObjectKind("github_conflict_set", "github_conflict_sets"),
    ObjectKind("github_merge_authorization", "github_merge_authorizations"),
    ObjectKind("github_repository_binding", "github_repository_bindings"),
    ObjectKind("github_resolution_plan", "github_resolution_plans"),
    ObjectKind("ground_truth_metric_definition", "ground_truth_metric_definitions"),
    ObjectKind("hardware_place", "hardware_places"),
    ObjectKind("inbox_message", "inbox_messages"),
    ObjectKind("incident", "incidents"),
    ObjectKind("infra_resource", "infra_resources"),
    ObjectKind("integration_connection", "integration_connections"),
    ObjectKind("integration_definition", "integration_definitions"),
    ObjectKind("kg_edge", "kg_edges"),
    ObjectKind("kg_node", "kg_nodes"),
    ObjectKind("mcp_confirmation", "mcp_confirmations"),
    ObjectKind("mcp_server", "mcp_servers"),
    ObjectKind("model_gateway_invocation", "model_gateway_invocations"),
    ObjectKind("model_gateway_lease", "model_gateway_leases"),
    ObjectKind("oncall_page", "oncall_pages"),
    ObjectKind("oncall_policy", "oncall_policies"),
    ObjectKind("oncall_schedule", "oncall_schedules"),
    ObjectKind("outbox_message", "outbox_messages"),
    ObjectKind("owned_resource", "owned_resources"),
    ObjectKind("panel_definition", "panel_definitions"),
    ObjectKind("panel_version", "panel_versions"),
    ObjectKind("policy_record", "policy_records"),
    ObjectKind("preview_sandbox", "preview_sandboxes"),
    ObjectKind("process_manager_proposal", "process_manager_proposals"),
    ObjectKind("provider_account", "provider_accounts"),
    ObjectKind("provider_slo_definition", "provider_slo_definitions"),
    ObjectKind("qa_benchmark_evaluation", "qa_benchmark_evaluations"),
    ObjectKind("release_board_item", "release_board_items"),
    ObjectKind("release_plan", "release_plans"),
    ObjectKind("resource_policy", "resource_policies"),
    ObjectKind("resource_topology_edge", "resource_topology_edges"),
    ObjectKind("role", "roles"),
    ObjectKind("self_improvement_proposal", "self_improvement_proposals"),
    ObjectKind("settled_conclusion", "settled_conclusions"),
    ObjectKind("skill_definition", "skill_definitions"),
    ObjectKind("sre_runbook", "sre_runbooks"),
    ObjectKind("surface_ownership", "surface_ownerships"),
    ObjectKind("system_finding", "system_findings"),
    ObjectKind("telemetry_source", "telemetry_sources"),
    ObjectKind("toolchain_candidate", "toolchain_candidates"),
    ObjectKind("toolchain_source", "toolchain_sources"),
    ObjectKind("verification_policy", "verification_policies"),
    ObjectKind("webhook_delivery", "webhook_deliveries"),
    ObjectKind("webhook_endpoint", "webhook_endpoints"),
    ObjectKind("worker_instance", "worker_instances"),
    ObjectKind("worker_pool", "worker_pools"),
    ObjectKind("workflow_compensation_run", "workflow_compensation_runs"),
    ObjectKind("workflow_definition", "workflow_definitions"),
    ObjectKind("workflow_mapping", "workflow_mappings"),
    ObjectKind("workflow_migration", "workflow_migrations"),
    ObjectKind("workflow_schedule", "workflow_schedules"),
    ObjectKind("workflow_step_run", "workflow_step_runs"),
    ObjectKind("focused_agent", "focused_agents"),
    ObjectKind("agent_act", "agent_acts", id_column="act_id"),
    ObjectKind("agent_conversation", "agent_conversations", id_column="conversation_id"),
    ObjectKind("agent_conversation_turn", "agent_conversation_turns", id_column="turn_id",
               project="(SELECT c.project_id FROM agent_conversations c "
                       "WHERE c.conversation_id = t.conversation_id)"),
    # a meeting route's id may also name one of its runs (meetings.load_meeting_run)
    UniqueProjectKind("meeting", "SELECT project_id FROM meetings WHERE id = :id "
                                 "UNION SELECT project_id FROM meeting_runs WHERE id = :id"),
    # main.require_agent_thread_reader: the thread, else its ledger rows
    UniqueProjectKind("agent_thread", "SELECT project_id FROM agent_threads WHERE id = :id "
                                      "UNION SELECT project_id FROM agent_session_ledger "
                                      "WHERE thread_id = :id AND NOT EXISTS "
                                      "(SELECT 1 FROM agent_threads WHERE id = :id)"),
    # main.require_sandbox_reader: a host sandbox is known by the build or preview that made it
    UniqueProjectKind("sandbox", "SELECT project_id FROM code_task_runs "
                                 "WHERE finalize_ctx->>'sandbox_task_id' = :id "
                                 "UNION SELECT project_id FROM app_previews WHERE sandbox_task_id = :id"),
    # a bake-off batch is the builds that share finalize_ctx.bakeoff.batch_id
    UniqueProjectKind("bakeoff_batch", "SELECT project_id FROM code_task_runs "
                                       "WHERE finalize_ctx->'bakeoff'->>'batch_id' = :id"),
    # receipts.get_by_id_or_dedupe_key: by id, else by dedupe key (which must be in one project)
    UniqueProjectKind("receipt", "SELECT project_id FROM receipts WHERE id = :id "
                                 "UNION SELECT project_id FROM receipts WHERE dedupe_key = :id "
                                 "AND NOT EXISTS (SELECT 1 FROM receipts WHERE id = :id)"),
    SecurityScanRunKind(),
)

_BY_KIND: dict[str, ObjectKind] = {}


def register_all(kinds: tuple[ObjectKind, ...] = KINDS) -> None:
    for spec in kinds:
        if spec.kind in _BY_KIND:
            continue
        _BY_KIND[spec.kind] = spec
        scope_module.register_object_kind(spec.kind, spec.load)


def get(kind: str) -> ObjectKind:
    return _BY_KIND[kind]


def all_kinds() -> tuple[ObjectKind, ...]:
    return tuple(_BY_KIND.values())


register_all()


# ---- central confinement (F2 / M9): every id a request names, by its NAME -----------------------
#
# A route used to be confined only where its declaration listed `confine=`; the review found 693 id
# parameters nobody had listed, so a permission held in project A reached B's (or another org's) row
# by id. The rule is now central: the enforcement point confines EVERY path, query and body id
# (nested body ids too) whose name is below to the project the permission was checked at (the org,
# for an org-level check), on every @permit route — no per-route declaration to forget.
#
# Every id parameter of every route must be classified here: either the object kinds it can name
# (ID_PARAM_KINDS), or an explicit reason it names no project-owned row (NON_OBJECT_IDS). The
# coverage test (test_identity_f2_api_object_coverage.py) fails on an unclassified name, so a new
# route with a new id cannot ship unconfined by omission. An id that names nothing passes (the
# handler answers its own not-found); an id naming another project's object is answered 404.

_RUNS = ("code_task", "workflow_run", "discovery_run", "meeting_run", "deployment_run",
         "agent_run", "agent_process_run", "agent_group_run", "agent_studio_run")
_RESERVATIONS = ("budget_reservation", "resource_allocation")
_RESOURCES = ("infra_resource", "external_resource", "owned_resource")
_PLANS = ("plan", "github_resolution_plan")

ID_PARAM_KINDS: dict[str, tuple[str, ...]] = {
    "project_id": ("project",), "project_ids": ("project",), "scope_id": ("project",),
    "act_id": ("agent_act",),
    "agent_group_version_id": ("agent_group_version",),
    "allocation_id": ("budget_allocation", "resource_allocation"),
    "parent_allocation_id": ("budget_allocation", "resource_allocation"),
    "approved_decision_id": ("decision",), "decision_id": ("decision",),
    "artifact_id": ("artifact",),
    "assurance_case_id": ("assurance_case",), "case_id": ("assurance_case",),
    "attach_run_id": ("agent_process_run",),
    "authorization_id": ("github_merge_authorization",),
    "binding_id": ("github_repository_binding", "work_tracker_binding"),
    "repository_binding_id": ("github_repository_binding",),
    "bot_identity_id": ("delivery_bot_identity",),
    "budget_reservation_id": ("budget_reservation",),
    "reservation_id": _RESERVATIONS, "staging_reservation_id": _RESERVATIONS,
    "production_reservation_id": _RESERVATIONS,
    "candidate_id": ("toolchain_candidate",),
    "baseline_evaluation_id": ("qa_benchmark_evaluation", "eval_run"),
    "candidate_evaluation_id": ("qa_benchmark_evaluation", "eval_run"),
    "capability_id": ("capability",), "capability_resolution_ids": ("capability_resolution",),
    "reply_to_id": ("a2a_envelope",), "channel_id": ("a2a_channel",),
    "claim_id": ("assurance_claim",),
    "code_sync_run_id": ("github_code_sync_run",), "sync_run_id": ("github_code_sync_run",),
    "code_task_run_id": ("code_task",),
    "compensation_id": ("workflow_compensation_run",),
    "conclusion_id": ("settled_conclusion",),
    "confirm_plan_id": _PLANS, "pending_plan_id": _PLANS, "plan_id": _PLANS,
    "resolution_plan_id": ("github_resolution_plan",),
    "confirmation_id": ("mcp_confirmation",),
    "conflict_set_id": ("github_conflict_set",),
    "connection_id": ("github_connection", "integration_connection"),
    "conversation_id": ("agent_conversation",), "turn_id": ("agent_conversation_turn",),
    "created_by_job_id": ("job",), "job_id": ("job",), "evaluator_job_id": ("job",),
    "credential_secret_ref_id": ("secret_ref",), "secret_ref_id": ("secret_ref",),
    "secret_ref_ids": ("secret_ref",),
    "deadletter_id": ("deadletter",),
    "definition_id": ("workflow_definition", "agent_definition"),
    "update_copy_id": ("workflow_definition",),
    "delivery_id": ("webhook_delivery", "a2a_delivery"),
    "delivery_run_id": ("deployment_run", "workflow_run"),
    "task_id": ("task",), "parent_task_id": ("task",), "dependency_task_ids": ("task",),
    "deployment_id": ("stack_deployment",), "deployment_run_id": ("deployment_run",),
    "discovery_run_id": ("discovery_run",),
    "src_id": ("kg_node", "work_item", "task"), "dst_id": ("kg_node", "work_item", "task"),
    "node_id": ("kg_node",), "edge_id": ("resource_topology_edge", "kg_edge"),
    "endpoint_id": ("webhook_endpoint",), "event_id": ("event",),
    "evidence_id": ("assurance_evidence",), "experiment_id": ("experiment",),
    "finding_id": ("system_finding",),
    "resource_id": _RESOURCES, "from_resource_id": _RESOURCES, "to_resource_id": _RESOURCES,
    "host_id": ("deployment_host",),
    "incident_id": ("incident",), "reliability_incident_id": ("incident",),
    "ins_id": ("agent_instruction",),
    "instance_id": ("worker_instance",), "worker_instance_id": ("worker_instance",),
    "integration_definition_id": ("integration_definition",),
    "investigation_id": ("investigation",),
    "invocation_id": ("model_gateway_invocation",), "lease_id": ("model_gateway_lease",),
    "item_ids": ("release_board_item", "work_item"),
    "key_entry_id": ("agent_key_entry",),
    "mapping_id": ("workflow_mapping",),
    "mcp_server_id": ("mcp_server",), "server_id": ("mcp_server",),
    "meeting_id": ("meeting",), "meeting_run_id": ("meeting_run",),
    "memory_id": ("agent_memory",),
    "message_id": ("outbox_message", "a2a_envelope", "inbox_message"),
    "metric_definition_id": ("ground_truth_metric_definition",),
    "metric_id": ("ground_truth_metric_definition",),
    "migration_id": ("workflow_migration",),
    "ownership_id": ("surface_ownership",),
    "page_id": ("oncall_page",), "panel_id": ("panel_definition",),
    "place_id": ("hardware_place",),
    "policy_id": ("github_branch_policy", "oncall_policy", "policy_record", "resource_policy"),
    "preview_id": ("app_preview", "preview_sandbox"),
    "proposal_id": ("self_improvement_proposal", "process_manager_proposal"),
    "provider_account_id": ("provider_account",),
    "receipt_id": ("receipt",), "release_id": ("release_plan",),
    "role_id": ("role",), "owner_role_id": ("role",),
    "run_id": _RUNS, "parent_run_id": _RUNS,
    "workflow_run_id": ("workflow_run",),
    "runbook_id": ("sre_runbook",),
    "schedule_id": ("workflow_schedule", "oncall_schedule"),
    "skill_id": ("skill_definition",), "skill_ids": ("skill_definition",),
    "slo_id": ("provider_slo_definition",),
    "snapshot_id": ("github_branch_snapshot",),
    "source_id": ("entitlement_source", "telemetry_source", "toolchain_source"),
    "source_ids": ("entitlement_source", "telemetry_source", "toolchain_source"),
    "step_run_id": ("workflow_step_run",),
    "subscription_id": ("event_subscription", "a2a_subscription"),
    "target_id": ("deployment_target",), "staging_target_id": ("deployment_target",),
    "production_target_id": ("deployment_target",), "deployment_target_id": ("deployment_target",),
    "host_target_id": ("deployment_target",),
    "version_id": ("workflow_version", "agent_version", "panel_version"),
    "target_version_id": ("workflow_version",), "workflow_version_id": ("workflow_version",),
    "thread_id": ("agent_thread",),
    "verification_policy_id": ("verification_policy",),
    "work_item_id": ("work_item",), "worker_pool_id": ("worker_pool",),
    "session_id": ("entitlement_connection_session", "account_connect_auth_session"),
    "agent_definition_id": ("agent_definition",),
    "program_id": ("improvement_program",), "trial_id": ("improvement_trial",),
    "changeset_id": ("changeset",), "escalation_id": ("shepherd_escalation",),
    "promotion_id": ("deployment_promotion",),
    "wakeup_id": ("agent_wakeup",),
    "agent_id": ("focused_agent",), "batch_id": ("bakeoff_batch",), "sandbox_id": ("sandbox",),
    "tracker_binding_id": ("work_tracker_binding",), "feedback_thread_id": ("feedback_thread",),
}

_IDENTITY = ("an identity-module object (principal, group, key, grant, provider, invitation): the "
             "identity service looks it up in the caller's org and refuses another org's")
_EXTERNAL = "an identifier in an external system (a provider, GitHub, a tracker), not a BOSS row"
_RUNTIME = "a runtime or install-wide identifier with no project (a worker, a telemetry unit)"
_CATALOGUE = "a key into the install-wide catalogue (packs, modules, templates, tools, models)"
_POLYMORPHIC = ("a typed reference whose kind travels beside it (entity_type, owner_type, sender_kind); "
                "the handler resolves it inside the request's project")
_CHILD = "a child key inside the scope's own object (a step of the run, a comment of the task)"
_PLANNING = "a planning key inside the project's own document, not a row of another project"

#: Id names that name no project-owned row, each with its reason (explicitly declared non-objects).
NON_OBJECT_IDS: dict[str, str] = {
    "org_id": "the org: must equal the caller's org (checked centrally, object_rules.check_org_ids)",
    **{n: _IDENTITY for n in ("grant_id", "group_id", "group_ids", "idp_id", "identity_id",
                              "invitation_id", "key_id", "passkey_id", "principal_id",
                              "owner_principal_ids", "sa_id", "subject_id", "user_id", "member_id",
                              "target_member_id", "item_id", "principal_ids", "request_id",
                              "to_role_id")},
    **{n: _EXTERNAL for n in ("external_id", "external_channel_id", "external_issue_id",
                              "external_project_id", "external_workspace_id", "installation_id",
                              "client_id", "saml_sp_entity_id", "app_id", "provider_id", "model_id",
                              "trace_id", "correlation_id", "causation_id")},
    **{n: _RUNTIME for n in ("worker_id", "evaluator_worker_id", "unit_id", "send_id",
                             "model_route_decision_id", "pool_id")},
    **{n: _CATALOGUE for n in ("benchmark_id", "pack_id", "module_id", "agent_module_id",
                               "template_id", "tool_ids", "skill_or_model_id")},
    **{n: _POLYMORPHIC for n in ("entity_id", "owner_id", "consumer_owner_id", "id", "ids",
                                 "sender_id", "sender_version_id", "subscriber_id", "actor_id",
                                 "row_id")},
    **{n: _CHILD for n in ("step_id", "comment_id", "action_id")},
    **{n: _PLANNING for n in ("cycle_id", "initiative_id")},
}


def kinds_for_id(name: str) -> tuple[str, ...]:
    """The object kinds an id parameter called `name` can name (empty: not an object)."""
    return ID_PARAM_KINDS.get(name, ())


def is_id_name(name: str) -> bool:
    """Does a parameter name look like an id (the coverage test classifies every one)?"""
    return name in ("id", "ids") or name.endswith(("_id", "_ids"))

"""WHAT A TOOL CALL MAY DO TWICE (AP-P2, docs/design/AGENT_PLATFORM.md §4.3 "Idempotency is
explicit").

A crash, a deploy or a lost lease can stop a run between asking a tool to act and recording what
it did. The run resumes from its last checkpoint and the question is what to do with a call whose
outcome nobody wrote down. Every WRITE tool declares one class, and the kernel answers by it:

  idempotent    running it again converges on the same state (set a field, stop a watch,
                resolve a comment): it is simply re-run.
  keyed         it acts once per idempotency key and the key travels with it
                (`current_idempotency_key()`, stable across re-runs: run:step:call). It is re-run
                with the same key, and the tool (or the provider behind it) dedupes on that key.
  irreversible  no undo and no dedupe (a merge, a message sent, money spent, work destroyed). If
                it STARTED and left no result, it may have happened: the run PARKS on an
                unknown-outcome interrupt and a person (or a reconciler) says what happened — it
                never fires twice. One that never started is run normally.

Reads are idempotent by construction. A write that declares nothing is treated as irreversible:
failing closed is the only safe reading of silence. The job plane has the same rule for job kinds
(services/durable_operations.py); this is that rule applied to tools.
"""
from __future__ import annotations

import contextlib
import contextvars
from typing import Any, Iterator, Optional

IDEMPOTENT = "idempotent"
KEYED = "keyed"
IRREVERSIBLE = "irreversible"

CLASSES = frozenset({IDEMPOTENT, KEYED, IRREVERSIBLE})

_KEY: contextvars.ContextVar = contextvars.ContextVar("boss_kernel_idempotency_key",
                                                      default=None)


def current_idempotency_key() -> Optional[str]:
    """The key of the tool call running now (run:step:call), for a `keyed` tool to dedupe on."""
    return _KEY.get()


@contextlib.contextmanager
def keyed(key: Optional[str]) -> Iterator[None]:
    token = _KEY.set(key)
    try:
        yield
    finally:
        _KEY.reset(token)


def class_of(schema: Any) -> str:
    """The class a call is replayed by: its declaration; else idempotent for a read and
    irreversible for a write that declares none."""
    if schema is None:
        return IRREVERSIBLE
    declared = getattr(schema, "idempotency", None)
    if declared in CLASSES:
        return declared  # a declaration wins, whatever the kind (a risky skill offered as read)
    if getattr(schema, "kind", "read") != "write":
        return IDEMPOTENT
    return IRREVERSIBLE


def may_rerun(schema: Any, *, started: bool) -> bool:
    """Whether a call with no recorded result may run again on resume. A call that never started
    always may; one that started may unless it is irreversible."""
    if not started:
        return True
    return class_of(schema) != IRREVERSIBLE


def validate(value: Optional[str], *, tool: str) -> Optional[str]:
    if value is None:
        return None
    if value not in CLASSES:
        raise ValueError(f"tool {tool!r}: idempotency {value!r} is not one of {sorted(CLASSES)}")
    return value


# ── The registry's write tools, declared ──────────────────────────────────────────────────────
#
# BOSS's own tool registries (tools/builtin.py, agent_loop.native_tool_registry) register ~100
# write tools. Each is declared here, by name, beside its siblings, so a reviewer reads the whole
# policy in one place (the job-kind precedent: durable_operations.JOB_KIND_CLASSES). ToolSpec
# picks its class up at construction (`idempotency=` at the registration wins), and
# test_every_write_tool_declares_an_idempotency_class fails any write tool missing from both.
#
# How each was classed:
#   irreversible  its reversibility is irreversible / destroys_work / costs_money, or it sends
#                 something out of BOSS (a message, a merge, a deploy, a push, a host command);
#   idempotent    a set / update / stop / resolve / link that converges when repeated;
#   keyed         it creates a record inside BOSS (a task, a meeting, a memory, a decision): a
#                 second run makes a second record, so it is re-run only under the same key.
TOOL_IDEMPOTENCY: dict = {}


def _declare(cls: str, *names: str) -> None:
    for name in names:
        TOOL_IDEMPOTENCY[name] = cls


_declare(IRREVERSIBLE,
         # costs money, destroys work, or acts outside BOSS: a second run is a second effect
         "apply_template", "cancel_code_task", "cancel_job", "captain_drive",
         "captain_overwatch", "captain_theater", "change_existing_app", "create_code_task",
         "create_discovery", "decide", "delete_project", "delete_workflow", "deploy_app",
         "extend_code_task", "generate_image", "publish_document", "redeploy", "remove_app",
         "request_meeting", "request_self_fix", "retry_code_task", "review_pull_request_again",
         "run_on_host", "start_workflow", "system_doctor_act", "ticket_comment", "waive_gates",
         "write_db")
_declare(IDEMPOTENT,
         # converges when repeated: the second run finds the state the first one left
         "allow_repository", "archive_project", "assign_mcp_to_agent", "assign_skill_to_agent",
         "close_orphaned_runs", "complete_work_item", "connect_coding_agent", "forget",
         "link_run_origin", "link_work_items", "merge_apps", "recheck_workflow_run",
         "register_repository", "remove_project_repository", "resolve_review_comment",
         "restore_project", "retarget_app", "retry_workflow_step", "set_job_priority",
         "set_ontology_pack", "set_project_setting", "set_project_team", "skip_workflow_step",
         "start_preview", "stop_preview", "stop_watching", "test_github_access", "ticket_import",
         "ticket_transition", "update_ledger")
_declare(KEYED,
         # creates a record inside BOSS: re-run only under the same key
         "add_document_from_file", "add_regulation_document", "add_review_comment", "ask_human",
         "captain_set_goal", "captain_watch", "create_deployment_target", "create_followup_task",
         "create_project", "create_work_item", "draft_custom_step", "draft_skill",
         "draft_workflow", "file_platform_gap", "investigate", "kg_propose_fact",
         "kg_propose_rule", "load_aml_sample", "load_dq_platform", "mandate_standard",
         "offer_options", "open_incident", "propose_hire", "propose_secret", "record_learning",
         "register_mcp_server", "remember", "report_friction", "request_budget_override",
         "request_tool_access", "run_security_scan", "schedule_work", "send_feedback",
         "steer_code_task", "watch", "watch_work_item", "wiki_propose")


def declared(name: str) -> Optional[str]:
    return TOOL_IDEMPOTENCY.get(name)


def default_for(name: str, reversibility: str) -> str:
    """The class a write tool takes when neither its registration nor the table names one: by its
    reversibility (irreversible / destroys_work / costs_money → irreversible), else keyed. Used
    only to FILL the table for a new tool's reviewer; `ToolSpec` never guesses at runtime."""
    if name in TOOL_IDEMPOTENCY:
        return TOOL_IDEMPOTENCY[name]
    if reversibility in ("irreversible", "destroys_work", "costs_money"):
        return IRREVERSIBLE
    return KEYED


__all__ = ["CLASSES", "IDEMPOTENT", "IRREVERSIBLE", "KEYED", "TOOL_IDEMPOTENCY", "class_of",
           "current_idempotency_key", "declared", "default_for", "keyed", "may_rerun",
           "validate"]

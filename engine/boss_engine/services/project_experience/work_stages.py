"""THE WORK PANEL'S FOUR STAGES, FROM THE WORKFLOW'S OWN STEPS (Work panel v2, 2026-11-22).

The rc10.32 panel drew six fixed stations (Spec, Build, Review & tests, Merge, Staging,
Production) for every piece of code work, so a workflow with no spec drew an empty Spec and a
step could show twice. v2 groups the steps the workflow actually has into four stages:

  plan   spec / PRD, panel meeting, discovery, research, the rules a run reads first
  build  code, fixes, tests, checkers, review, security passes, and Merge as Build's last step
  ship   preview, staging deploys, contract / smoke tests, the release approval
  live   production deploys, health, monitoring, incidents

Each step's capability says its stage (STAGE_OF_CAPABILITY, which names every capability in the
catalogue; a test keeps it that way). A step that is no capability's — a person's decision, a
sub-workflow, a custom step — or a capability that only says something about the steps around it
(notify, report) joins the stage of the step before it (a gate after the build is Build's), else
the one after it, else Plan. Routing and waiting steps (conditions, joins, timers) are never
drawn.

Pure functions over the rows work.py already holds: no query, no model call.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from .common import href, iso
from .work_outcome import CONTROL_KINDS, _cap, _words, clip, step_name

STAGES = ("plan", "build", "ship", "live")

#: Every capability in the catalogue (workflow_capabilities.CAPABILITIES) and its stage. None:
#: the step says something about its neighbours and takes their stage (see module doc).
STAGE_OF_CAPABILITY: dict[str, Optional[str]] = {
    # plan — what to build and why
    "research": "plan", "meeting": "plan", "spec_review": "plan", "architecture": "plan",
    "scan_business_rules": "plan", "extract_rulebook_rules": "plan", "trace_regulation": "plan",
    "regulatory_trace_review": "plan", "rules_review": "plan", "clause_intake": "plan",
    "compliance_orchestration": "plan", "compliance_assessment": "plan",
    "lane_confirmation": "plan", "aml_network_mapping": "plan", "aml_analysis": "plan",
    "dq_monitor_intake": "plan", "dq_monitoring": "plan", "dq_context_enrichment": "plan",
    "dq_historical_pattern": "plan", "dq_rca": "plan", "dq_orchestration": "plan",
    "dq_lane": "plan",
    # an incident workflow investigates first: finding the cause is its plan
    "investigate_incident": "plan",
    # build — make the change, check it, review it, merge it
    "build": "build", "retry_build": "build", "fix_pr": "build", "test": "build",
    "more_tests": "build", "review": "build", "security_scan": "build",
    "security_review": "build", "security_panel": "build", "security_decide": "build",
    "security_fix": "build", "security_rereview": "build", "security_report": "build",
    "plrr": "build", "cost_check": "build", "document": "build", "github_comment": "build",
    "wait_for_ci": "build", "merge": "build", "video_edit": "build",
    "remediation_package": "build", "sar_narrative": "build", "dq_rule_drafting": "build",
    "dq_remediation_package": "build", "dq_remediation_fix": "build",
    # ship — put it somewhere a person can try it, and check it there
    "preview": "ship", "ux_review": "ship", "release_approval": "ship", "deploy": "ship",
    "redeploy": "ship", "config_deploy": "ship", "promote_config": "ship",
    "smoke_test": "ship", "contract_test": "ship", "teardown_preview": "ship",
    "social_publish": "ship",
    # live — production and its health
    "monitoring_pack": "live", "monitor": "live",
    "verify_recovery": "live", "incident_report": "live", "compliance_closure": "live",
    "dq_incident_closure": "live", "dq_escalation": "live",
    # says something about its neighbours
    "notify": None, "report": None,
}

#: Deploy steps go to Live when they deploy to production (their own input, else their id).
_DEPLOYS = frozenset({"deploy", "redeploy", "config_deploy", "promote_config"})
_PROD_WORDS = ("prod", "production", "live")

_DONE = ("succeeded",)
_LIVE = ("running", "leased", "ready", "waiting")
_FAILED = ("failed", "deadlettered")


def _inputs(spec_step: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("inputs", "with", "params", "config"):
        v = spec_step.get(key)
        if isinstance(v, dict):
            return v
    return {}


def _to_production(spec_step: Mapping[str, Any], got: Optional[Mapping[str, Any]] = None) -> bool:
    env = str(_inputs(spec_step).get("environment") or "").lower()
    if not env and got:
        env = str(((got.get("stack_deploy") or {}) if isinstance(got.get("stack_deploy"), dict)
                   else {}).get("environment") or "").lower()
    if env:
        from ..production import is_production_environment

        return is_production_environment(env)
    sid = str(spec_step.get("id") or "").lower()
    return any(w in sid.replace("-", "_").split("_") for w in _PROD_WORDS)


def stage_of(spec_step: Mapping[str, Any], got: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """The stage a step's own kind says, or None when it takes its neighbours' (a person's
    decision, a sub-workflow, a custom step, notify/report, a capability not in the map)."""
    cap = _cap(spec_step)
    if cap in _DEPLOYS and _to_production(spec_step, got):
        return "live"
    return STAGE_OF_CAPABILITY.get(cap)


def _state(got: Mapping[str, Any]) -> str:
    status = str(got.get("status") or "pending")
    if status in _FAILED:
        return "failed"
    if status == "skipped":
        return "skipped"
    if status in _DONE:
        return "done"
    if got.get("gate_open"):
        return "waiting"
    if status in _LIVE:
        return "now"
    return "todo"


def _refs(project_id: str, cap: str, got: Mapping[str, Any]) -> dict[str, str]:
    """The screen a step's own record opens: its panel meeting, its build, its deploy, its
    preview. Only what the step's output names."""
    out = got.get("output") or {}
    if not isinstance(out, dict):
        return {}
    wait = out.get("await") if isinstance(out.get("await"), dict) else {}
    refs: dict[str, str] = {}
    held = out.get("meeting") if isinstance(out.get("meeting"), dict) else {}
    meeting = out.get("meeting_run_id") or held.get("run_id") or (
        wait.get("id") if wait.get("kind") == "meeting" else None)
    if meeting:
        refs["meeting"] = href(project_id, area="meetings", meeting=str(meeting))
    build = out.get("run_id") if str(out.get("run_id") or "").startswith("crun_") else (
        wait.get("id") if wait.get("kind") == "code_task" else None)
    if build and cap != "merge":
        refs["build"] = href(project_id, area="code_tasks", view="apps", build=str(build))
    sd = got.get("stack_deploy") if isinstance(got.get("stack_deploy"), dict) else {}
    deploy = (sd or {}).get("stack_deployment_id") or (
        wait.get("id") if wait.get("kind") == "stack_deployment" else None)
    if deploy:
        refs["deploy"] = href(project_id, area="infra", view="deploys", deploy=str(deploy))
    return refs


def step_list(spec: Mapping[str, Any], steps: Mapping[str, Mapping[str, Any]], *,
              project_id: str) -> list[dict[str, Any]]:
    """THE WORKFLOW'S STEPS, EACH WITH ITS STAGE, in the workflow's order: id, name, kind,
    capability, stage, state (done | now | waiting | todo | failed | skipped), started_at,
    completed_at, sub (the step's own sentence) and refs (the screens its record opens).
    Routing steps are left out. A step that never started stays (todo), so a finished run's
    later stages read "not reached", never "not in this workflow"."""
    rows: list[dict[str, Any]] = []
    for spec_step in spec.get("steps") or []:
        if not isinstance(spec_step, dict):
            continue
        kind = str(spec_step.get("kind") or "capability")
        if kind in CONTROL_KINDS:
            continue
        got = steps.get(str(spec_step.get("id") or "")) or {"status": "pending", "output": {}}
        cap = _cap(spec_step)
        rows.append({
            "id": str(spec_step.get("id") or ""), "name": step_name(spec_step), "kind": kind,
            "capability": cap or None, "stage": stage_of(spec_step, got), "state": _state(got),
            "started_at": iso(got.get("started_at")), "completed_at": iso(got.get("completed_at")),
            "sub": clip(_words(got.get("output") or {}), 120) or None,
            "refs": _refs(project_id, cap, got),
        })
    # Steps that take their neighbours' stage: the one before, else the one after, else Plan.
    last: Optional[str] = None
    for row in rows:
        if row["stage"] is None and last is not None:
            row["stage"] = last
        last = row["stage"] or last
    following: Optional[str] = None
    for row in reversed(rows):
        if row["stage"] is None:
            row["stage"] = following or "plan"
        following = row["stage"]
    return rows

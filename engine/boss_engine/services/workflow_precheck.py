"""WORKFLOW PRE-CHECK: before a run spends anything, check each step's REAL infrastructure.

WHY THIS EXISTS
---------------
`workflow_readiness` answers "does this project HAVE the things this workflow needs" — a
repository, a model, a deployment host. It does not ask whether those things will WORK for this
run. HelpDesk Delivery (D1) on the laptop (2026-10-13) passed readiness five times and failed five
times, each a few hours in: pass 2 needed a permission the run's consent never granted, the app
needed a variable nobody could bind for a build, the preview served a dist/ nothing built, the
first deploy had no stack target, smoke could not read a stack deployment. Every one of those was
knowable from the spec, the database and the app's tree before the run started.

So each capability declares its PRE-CHECKS (`CAPABILITY_PRECHECKS`; a capability with nothing to
check says so with `NO_CHECKS`, and `test_every_capability_declares_its_prechecks` holds every new
capability to it). Each check is cheap — seconds, no model call — and returns, per step:

    {id, verdict: ready | fixable | will_fail | unknown, evidence, fix: {auto, action}, skip_allowed}

  * ready      — checked, and it will work;
  * fixable    — it will not work as things stand, and the fix is a known action (`fix.action`);
                 `fix.auto` = BOSS makes it itself at run time (a generated key, say);
  * will_fail  — it will not work, and nothing a person does before the run changes that
                 (or the action is a change to BOSS / the workflow);
  * unknown    — could not be checked here (no network, no app tree yet), or a RISK that only
                 becomes a failure under a condition the evidence names.

TWO CHECKPOINTS
---------------
  * PRE_RUN    — infrastructure and authority, before anything is spent. The DB-only subset
                 (`fast`) runs inside every start door (`gate_start`).
  * PRE_DEPLOY — after pass 1 builds, against the real app tree, before preview/deploy.

AT THE DOOR
-----------
`gate_start` runs the fast pre-run checks when a product run starts. A `will_fail` the caller did
not knowingly `accept` (by check id, or `<step>:<check id>`) and whose step it did not
`skip_steps` refuses the start (PrecheckRefused → 409 with the whole pre-check). Accepted checks
and skipped steps are recorded on the run (`workflow.precheck_accepted`, and `_precheck` in the
run's input, which only the server writes) with the actor and the reason. A skip never bypasses a
gate that protects a deploy: a gate step, or a review/test/CI/security step with a deploy after
it, cannot be skipped (`skip_refusal`).

READ-ONLY. `precheck` runs inside a transaction it rolls back (the budget read expires stale
reservations as it reads); network probes are GETs; the dependency dry run works on a copy.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import owned_process
from . import workflow_capabilities as caps

VERDICTS = ("ready", "fixable", "will_fail", "unknown")
CHECKPOINTS = ("pre_run", "pre_deploy")
PRE_RUN, PRE_DEPLOY = CHECKPOINTS
RUN_STEP = "(run)"
#: The run's input key the server writes accepted checks and skipped steps under. A caller's own
#: value is always dropped (workflows.start_run), so a skip cannot be asserted, only recorded.
RECORD_KEY = "_precheck"
MIN_FREE_DISK_GB = 20
#: The start door's mode: `report` (default until the console's readiness card ships: run,
#: record the report on the run and return it, never refuse), `enforce` (an unaccepted
#: will_fail refuses 409), `off`.
GATE_ENV = "BOSS_WORKFLOW_PRECHECK_GATE"
GATE_MODES = ("enforce", "report", "off")


# ── The registry ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CapabilityPrecheck:
    """What one capability is checked for before a run.

    `checks`: check ids (CHECKS) that apply to a step of this capability.
    `invokes_tools`: tools the capability's runner calls ITSELF, beyond `Capability.tool` — the
    consent check needs every permission the step's calls are checked against, not only the
    declared tool's (more_tests/plrr call extend_code_task, which is `code_task:retry`).
    `tool@inputs.<name>`: only when the step binds that input (a build with `extend`).
    `reserves`: the budget reservation the step makes, as (settings attribute, multiplier).
    `app_env`: the app environment the step runs the app in (secrets.ENVS), if any.
    `none_because`: set on a capability with nothing to check, so "none" is a decision.
    """

    checks: tuple[str, ...] = ()
    invokes_tools: tuple[str, ...] = ()
    reserves: Optional[tuple[str, int]] = None
    app_env: Optional[str] = None
    #: What the runner does when the step's own tool call is refused: "fails" the step (and so
    #: the run), or "skips" it in words (a preview the agent may not start:
    #: workflow_capability_runner._preview_not_permitted).
    on_refusal: str = "fails"
    none_because: str = ""


def NO_CHECKS(why: str) -> CapabilityPrecheck:  # noqa: N802 — reads as a declaration
    return CapabilityPrecheck(none_because=why)


_INFRA = ("sandbox.worker",)
#: A step that runs an agent: the model its route reaches, whether that engine can start, and
#: (once per run) whether the run's estimated cost fits the caps.
_AGENT = ("sandbox.model", "sandbox.engine", "budget.run_cost")
_BUILDS = ("authority.consent", "github.access", "github.own_link", "sandbox.worker",
           "sandbox.toolchain",
           "sandbox.build_services", "sandbox.disk") + _AGENT + ("budget.headroom",)

#: EVERY capability has an entry (test_every_capability_declares_its_prechecks).
CAPABILITY_PRECHECKS: dict[str, CapabilityPrecheck] = {
    "research": CapabilityPrecheck(("authority.consent", "sandbox.worker") + _AGENT
                                   + ("budget.headroom",)),
    "meeting": CapabilityPrecheck(("authority.consent", "sandbox.worker") + _AGENT
                                  + ("budget.headroom",), reserves=("plan_budget_usd_micros", 4)),
    "spec_review": NO_CHECKS("a person's (or the panel's) sign-off: nothing runs"),
    "architecture": CapabilityPrecheck(("sandbox.worker",) + _AGENT),
    "build": CapabilityPrecheck(_BUILDS + ("app_env", "app.test_plan", "app.dependencies",
                                           "app.secret_scan", "code.lockfile",
                                           "code.browser_cache"),
                                # An `extend` build (Incremental Build) builds on the branch.
                                invokes_tools=("extend_code_task@inputs.extend",),
                                reserves=("code_task_budget_usd_micros", 1), app_env="build"),
    "review": CapabilityPrecheck(("sandbox.worker",) + _AGENT),
    "test": NO_CHECKS("reads the build's own test results"),
    "report": NO_CHECKS("writes the run's report from what the steps recorded"),
    "preview": CapabilityPrecheck(("authority.consent", "github.access", "sandbox.worker",
                                   "sandbox.toolchain", "sandbox.disk", "app_env",
                                   "preview.serves_build"), app_env="preview",
                                  on_refusal="skips"),
    "ux_review": NO_CHECKS("a person's sign-off"),
    "release_approval": NO_CHECKS("a person's sign-off"),
    "security_review": NO_CHECKS("a person's sign-off"),
    "security_scan": CapabilityPrecheck(("authority.consent", "github.access", "sandbox.worker",
                                         "sandbox.disk")),
    "security_panel": CapabilityPrecheck(("authority.consent",) + _AGENT + ("budget.headroom",),
                                         reserves=("plan_budget_usd_micros", 4)),
    "security_decide": NO_CHECKS("a person's decision"),
    "security_fix": CapabilityPrecheck(_BUILDS, reserves=("code_task_budget_usd_micros", 1)),
    "security_rereview": CapabilityPrecheck(_AGENT),
    "security_report": NO_CHECKS("writes a report from recorded findings"),
    "plrr": CapabilityPrecheck(_BUILDS, invokes_tools=("extend_code_task",),
                               reserves=("code_task_budget_usd_micros", 1)),
    "more_tests": CapabilityPrecheck(_BUILDS + ("app.test_plan",),
                                     invokes_tools=("extend_code_task",),
                                     reserves=("code_task_budget_usd_micros", 1)),
    "deploy": CapabilityPrecheck(("authority.consent", "github.access", "sandbox.worker",
                                  "app_env", "deploy.target", "deploy.manifest",
                                  "deploy.database", "deploy.health", "code.start_command",
                                  "code.prod_refusal"), app_env="deploy"),
    "video_edit": CapabilityPrecheck(("sandbox.worker", "sandbox.disk")),
    "social_publish": NO_CHECKS("readiness checks the channel connection; nothing else to probe"),
    "monitor": CapabilityPrecheck(_INFRA),
    "notify": NO_CHECKS("readiness checks the channel connection"),
    "smoke_test": CapabilityPrecheck(("authority.consent", "smoke.contract")),
    "wait_for_ci": CapabilityPrecheck(("github.access", "github.ci")),
    "redeploy": CapabilityPrecheck(("authority.consent", "github.access", "deploy.target"),
                                   app_env="deploy"),
    "retry_build": CapabilityPrecheck(_BUILDS, reserves=("code_task_budget_usd_micros", 1)),
    "teardown_preview": CapabilityPrecheck(("authority.consent",)),
    "cost_check": CapabilityPrecheck(("authority.consent",)),
    "document": CapabilityPrecheck(_AGENT),
    # Incident response: model-free reads of what is recorded. What the investigation cannot
    # read (a host's logs, the release diff) it says in its evidence and carries on.
    "investigate_incident": NO_CHECKS("reads the incident's recorded evidence, the app's logs "
                                      "and its release diff; what it cannot read it says"),
    "verify_recovery": NO_CHECKS("waits on BOSS's own monitor of the deployed app"),
    "incident_report": NO_CHECKS("writes the report from what the response recorded"),
    # Set up monitoring (monitoring_pack): a model proposes the pack, BOSS commits it to the
    # build's branch. The model and GitHub are what it needs; it runs no build sandbox.
    "monitoring_pack": CapabilityPrecheck(("authority.consent", "github.access", "sandbox.model",
                                           "budget.run_cost")),
    # Scan for business rules (knowledge/rule_scan.py): reads the repository and runs the
    # review seat's engine read-only; spends within its own budget.
    "scan_business_rules": CapabilityPrecheck(("authority.consent", "github.access") + _AGENT,
                                              invokes_tools=("read_code",)),
    "rules_review": NO_CHECKS("a person's review of the proposed rules"),
    # Trace a regulation to code (knowledge/regulatory_trace.py): reads a document's clauses and
    # proposes the rule or code that implements each, on the review seat's engine; no repo read.
    "trace_regulation": CapabilityPrecheck(("authority.consent",) + _AGENT),
    "regulatory_trace_review": NO_CHECKS(
        "a person's review of the proposed implemented_by and gap findings"),
    # KG P5b banking_regulatory_compliance: thin role-agent steps that read and write the
    # Context Graph through the deterministic knowledge path (not a build sandbox). Two call a
    # tool during their turn — the Service Desk mock, and the repo read — declared so the consent check
    # grants it.
    "clause_intake": NO_CHECKS("opens a ComplianceCase per clause from the pasted document; "
                               "writes the Context Graph, runs no sandbox"),
    "compliance_assessment": CapabilityPrecheck(
        none_because="classifies the assembled evidence and cites a graph node; writes the "
                     "Context Graph, runs no sandbox",
        invokes_tools=("service_desk_mock",)),
    "compliance_orchestration": NO_CHECKS("groups the run's findings into remediation families "
                                          "from what the assessment recorded"),
    "lane_confirmation": CapabilityPrecheck(
        none_because="confirms or rules out a family's finding by reading the cited symbol",
        invokes_tools=("read_code",)),
    "remediation_package": NO_CHECKS("writes the change brief, test, owner and approver from "
                                     "the confirmed finding"),
    "compliance_closure": NO_CHECKS("records the resolution as a Precedent with an "
                                    "execution-state marker; deterministic underneath"),
    # Case study 4 (aml_alert_investigation): role-agent judgements over the graph and what the
    # deterministic steps collected; no sandbox, no repository, no connector call of their own.
    "extract_rulebook_rules": NO_CHECKS("reads a confirmed document's clauses and proposes the "
                                        "rules they state for review; runs no sandbox"),
    "aml_network_mapping": NO_CHECKS("names related parties from the graph edges around the "
                                     "alert; writes the Context Graph, runs no sandbox"),
    "aml_analysis": NO_CHECKS("recommends close or escalate from the assembled evidence; "
                              "writes the Context Graph, runs no sandbox"),
    "sar_narrative": NO_CHECKS("drafts a narrative after a person escalated; files nothing"),
    # The DQ RCA and remediation pipeline (dq_incident_rca): graph reads, the DQ sample's
    # connectors (the DQ Monitor, the Profile Service, the Service Desk) and role-agent
    # judgements; no sandbox, no repository.
    **{key: NO_CHECKS(why) for key, why in (
        ("dq_monitoring", "opens or attaches an incident from the DQ Monitor's exception group; writes "
                          "the Context Graph and the ticket, runs no sandbox"),
        ("dq_context_enrichment", "reads the graph and the profile service; writes the Context "
                                  "Graph"),
        ("dq_historical_pattern", "matches closed incidents in the graph; writes the Context "
                                  "Graph"),
        ("dq_rca", "proposes causes from the assembled evidence; writes the Context Graph"),
        ("dq_orchestration", "routes candidate families to swimlanes by policy"),
        ("dq_lane", "confirms or rules out one family on the evidence"),
        ("dq_rule_drafting", "drafts a rule proposal into Rules review; changes no rule"),
        ("dq_remediation_package", "prepares a package; executes nothing"),
        ("dq_remediation_fix", "records what a person carried out; executes nothing"),
        ("dq_incident_closure", "re-runs the check and records the closure"),
        ("dq_escalation", "raises open incidents' escalation on the ticket"),
        ("dq_monitor_intake", "starts an investigation per Error exception group"),
    )},
}

#: Which route a capability's agent takes (engine_startup): the build pipeline's stages, the
#: review panel's stages, or the chat job (meetings, panels, documents, research). Every
#: capability that declares `sandbox.engine` is here (test_every_agent_step_names_its_route).
AGENT_ROUTE = {
    "build": "build", "more_tests": "build", "plrr": "build", "security_fix": "build",
    "retry_build": "build", "review": "review",
    "research": "chat", "meeting": "chat", "architecture": "chat", "security_panel": "chat",
    "security_rereview": "chat", "document": "chat",
    # The scan runs on the review seat's engine, read-only (rule_scan._review_engine).
    "scan_business_rules": "review",
    # The trace reads the document's clauses and proposes, on the same review seat, read-only.
    "trace_regulation": "review",
}

#: Capabilities that VERIFY work. Skipping one with a deploy after it would ship unverified work,
#: so `skip_refusal` refuses it (a skip never bypasses a gate that protects a deploy).
_PROTECTS_A_DEPLOY = frozenset({"review", "test", "wait_for_ci", "security_scan",
                                "security_rereview", "smoke_test", "ux_review",
                                "security_review", "security_decide", "spec_review"})
_DEPLOYS = frozenset({"deploy", "redeploy"})


def precheck_for(capability_key: str) -> CapabilityPrecheck:
    return CAPABILITY_PRECHECKS.get(capability_key) or NO_CHECKS("an unknown capability")


# ── Results ───────────────────────────────────────────────────────────────────────────────────

def result(check_id: str, verdict: str, evidence: str, *, action: str = "", auto: bool = False,
           details: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    if verdict not in VERDICTS:
        raise ValueError(verdict)
    out: dict[str, Any] = {"id": check_id, "verdict": verdict, "evidence": evidence.strip(),
                           "fix": {"auto": bool(auto), "action": action.strip()}}
    if details:
        out["details"] = dict(details)
    return out


@dataclass
class Context:
    """What one pre-check reads. Everything a check computes once is memoised in `memo`."""

    connection: Connection
    settings: Any
    project_id: str
    actor: str
    spec: dict[str, Any]
    run_input: dict[str, Any]
    checkpoint: str = PRE_RUN
    version_id: str = ""
    workflow_key: str = ""
    workflow_name: str = ""
    run_id: str = ""
    app_dir: Optional[Path] = None
    repo_root: Optional[Path] = None
    build_run_id: str = ""
    #: No network, no subprocesses: the start door's subset.
    offline: bool = False
    #: The run-level families read from BOSS's recent behaviour (workflow_precheck_behaviour):
    #: history, logs, data. Empty at the start door (it stays DB-cheap and per-run).
    families: tuple[str, ...] = ()
    #: How far back the history family reads.
    history_days: int = 7
    memo: dict[str, Any] = field(default_factory=dict)

    def once(self, key: str, compute: Callable[[], Any]) -> Any:
        if key not in self.memo:
            self.memo[key] = compute()
        return self.memo[key]

    def savepoint(self, compute: Callable[[], Any], default: Any = None) -> Any:
        """A read that may touch the database, isolated so a failure leaves the rest usable."""
        if self.connection is None:
            return default
        point = self.connection.begin_nested()
        try:
            value = compute()
        except Exception:  # noqa: BLE001 — a check that cannot read says "unknown", it never raises
            point.rollback()
            return default
        point.rollback()      # read-only: whatever the read wrote (stale reservations) is undone
        return value


@dataclass(frozen=True)
class CheckDef:
    id: str
    label: str
    checkpoints: tuple[str, ...]
    #: DB-only and in-process: runs inside the start door.
    fast: bool
    fn: Callable[[Context, dict[str, Any]], Any]
    #: A check whose answer is the same for every step (disk, worker): computed once, shown on
    #: the first step that declares it, the rest listed in `also_needed_by`.
    shared: bool = False


CHECKS: dict[str, CheckDef] = {}


def _check(check_id: str, label: str, checkpoints: Sequence[str] = CHECKPOINTS, *,
           fast: bool = True, shared: bool = False):
    def register(fn: Callable[[Context, dict[str, Any]], Any]):
        CHECKS[check_id] = CheckDef(check_id, label, tuple(checkpoints), fast, fn, shared)
        return fn
    return register


# ── Spec helpers ──────────────────────────────────────────────────────────────────────────────

def _cap_key(step: Mapping[str, Any]) -> str:
    binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
    if step.get("kind", "capability") != "capability":
        return ""
    return str(binding.get("capability") or "")


def _steps(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [s for s in (spec.get("steps") or []) if isinstance(s, dict) and s.get("id")]


def _targets(step: Mapping[str, Any]) -> list[str]:
    """Every step this one can lead to, its error path included: the runtime's own edges."""
    from . import workflows

    return workflows._targets(dict(step))  # noqa: SLF001 — the runtime's own edges


def _reachable(by_id: Mapping[str, dict], start: str, stop: Optional[str] = None) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        sid = stack.pop()
        if sid in seen or sid not in by_id or sid == stop:
            continue
        seen.add(sid)
        stack.extend(_targets(by_id[sid]))
    return seen


def _ordered(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Steps in the order a run reaches them (entry first, breadth-first), then any others."""
    by_id = {str(s["id"]): s for s in _steps(spec)}
    entry = str(spec.get("entry_step") or (next(iter(by_id), "")))
    out: list[str] = []
    queue = [entry]
    while queue:
        sid = queue.pop(0)
        if sid in out or sid not in by_id:
            continue
        out.append(sid)
        queue.extend(_targets(by_id[sid]))
    out += [sid for sid in by_id if sid not in out]
    return [by_id[s] for s in out]


def _downstream_deploys(spec: Mapping[str, Any], step_id: str) -> list[str]:
    by_id = {str(s["id"]): s for s in _steps(spec)}
    after = _reachable(by_id, step_id) - {step_id}
    return [s for s in after if _cap_key(by_id[s]) in _DEPLOYS]


def skip_refusal(spec: Mapping[str, Any], step: Mapping[str, Any]) -> Optional[str]:
    """Why this step may NOT be skipped, or None. A skip never bypasses a gate that protects a
    deploy: a gate (a person's sign-off) cannot be skipped, nor a step that verifies work with a
    deploy after it."""
    key = _cap_key(step)
    cap = caps.CAPABILITIES.get(key)
    approval = step.get("approval") if isinstance(step.get("approval"), dict) else {}
    if (cap is not None and cap.is_gate) or step.get("kind") in ("approval", "gate"):
        return f"{step.get('id')} is a gate; a gate is answered, never skipped"
    if key in _PROTECTS_A_DEPLOY:
        deploys = _downstream_deploys(spec, str(step.get("id")))
        if deploys:
            return (f"{step.get('id')} verifies the work that {', '.join(sorted(deploys))} "
                    "ships; skipping it would deploy unverified work")
    if approval.get("forced_manual") and key not in _DEPLOYS:
        return f"{step.get('id')} needs a person's approval, which a skip cannot stand in for"
    if step.get("kind") in ("parallel", "join", "condition", "bounded_loop", "for_each"):
        return f"{step.get('id')} is control flow, not work"
    return None


def _money(micros: Any) -> str:
    return f"${int(micros or 0) / 1_000_000:,.2f}"


# ── Shared reads ──────────────────────────────────────────────────────────────────────────────

def _tool_permissions() -> dict[str, str]:
    from ..identity import consent
    from ..tools.builtin import build_default_registry

    registry = consent._registry(build_default_registry)  # noqa: SLF001 — the one registry consent reads
    return {s.name: str(getattr(s, "permission", "") or "") for s in registry.specs()}


def _consent_derivation(ctx: Context) -> dict[str, Any]:
    """What the consent for this run would grant: identity/consent.workflow_needs, verbatim."""
    def derive() -> dict[str, Any]:
        from ..identity import catalogue, consent

        roles, perms, repos = consent.workflow_needs(ctx.connection, ctx.project_id, ctx.spec,
                                                     ctx.run_input)
        delegable = sorted(p for p in perms if catalogue.is_known(p) and catalogue.meta(p).delegable)
        return {"roles": list(roles), "permissions": delegable, "repos": list(repos)}
    return ctx.once("consent", lambda: ctx.savepoint(derive, default=None))


def _runs_as(ctx: Context, step: Mapping[str, Any]) -> list[str]:
    """The agent role(s) the step acts as — the same candidates consent names for it."""
    from . import step_agents

    binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
    named = step_agents.named_agent(dict(step)) or str(binding.get("role_key") or "")
    if named and named != "human":
        return [named]
    cap = caps.CAPABILITIES.get(_cap_key(step))
    if cap is not None and cap.captain:
        return [cap.captain]
    return []


def _passport_ceiling(ctx: Context, role_key: str) -> Optional[frozenset[str]]:
    def read() -> Optional[frozenset[str]]:
        from ..identity import passports, runs

        aid = runs.agent_for_role(ctx.connection, ctx.project_id, role_key)
        p = passports.get(aid, db=ctx.connection) if aid else None
        return frozenset(p.ceiling) if p is not None else None
    return ctx.once(f"ceiling:{role_key}", lambda: ctx.savepoint(read, default=None))


def _identity_applies(ctx: Context) -> bool:
    """Do runs here act under delegated authority at all? Only when the project's agents have
    Passports (consent.require_for_workflow returns None otherwise and steps run as the person)."""
    def read() -> bool:
        return bool(ctx.connection.execute(text(
            "SELECT 1 FROM identity_agent_passports LIMIT 1")).first())
    return bool(ctx.once("identity_applies", lambda: ctx.savepoint(read, default=False)))


def _app_key(ctx: Context) -> str:
    data = ctx.run_input or {}
    template = data.get("template") if isinstance(data.get("template"), dict) else {}
    return str(data.get("app_key") or template.get("app_key") or "").strip()


def _app_name(ctx: Context) -> str:
    from .delivery_pipeline import app_name_of_run

    title = str((ctx.run_input or {}).get("title") or "")
    if ctx.build_run_id:
        row = ctx.savepoint(lambda: ctx.connection.execute(text(
            "SELECT title, finalize_ctx FROM code_task_runs WHERE id = :i"),
            {"i": ctx.build_run_id}).mappings().first())
        if row:
            return app_name_of_run(dict(row))
    return app_name_of_run({"title": title}) if title else ""


def _repository(ctx: Context) -> str:
    data = ctx.run_input or {}
    for key in ("repository", "repo", "github_repo"):
        value = str(data.get(key) or "").strip()
        if value.count("/") == 1:
            return value
    derived = _consent_derivation(ctx) or {}
    if derived.get("repos"):
        return str(derived["repos"][0])
    row = ctx.savepoint(lambda: ctx.connection.execute(text(
        "SELECT full_name FROM github_repository_bindings WHERE project_id = :p "
        "AND COALESCE(status,'active') = 'active' ORDER BY (role = 'primary') DESC LIMIT 1"),
        {"p": ctx.project_id}).scalar())
    return str(row or "")


def _app_rel(ctx: Context) -> str:
    """The app's folder inside its repository ("" = root)."""
    if ctx.app_dir is not None and ctx.repo_root is not None:
        try:
            rel = ctx.app_dir.resolve().relative_to(ctx.repo_root.resolve())
            return "" if str(rel) == "." else str(rel)
        except ValueError:
            pass
    if ctx.app_dir is not None:
        return ctx.app_dir.name
    return ""


# ── Authority ─────────────────────────────────────────────────────────────────────────────────

def step_permissions(step: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(tool, permission) for every tool this step's capability calls: its declared tool and
    the tools its runner calls itself (CapabilityPrecheck.invokes_tools)."""
    from ..identity import catalogue

    key = _cap_key(step)
    cap = caps.CAPABILITIES.get(key)
    tools: list[str] = []
    if cap is not None and cap.tool:
        tools.append(cap.tool)
    binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
    for entry in precheck_for(key).invokes_tools:
        tool, _, when = entry.partition("@")
        if when.startswith("inputs.") and not (binding.get("inputs") or {}).get(when[7:]):
            continue
        if tool not in tools:
            tools.append(tool)
    perms = _tool_permissions()
    out: list[tuple[str, str]] = []
    for tool in tools:
        perm = perms.get(tool, "")
        if perm and catalogue.is_known(perm) and catalogue.meta(perm).delegable:
            out.append((tool, perm))
    return out


@_check("authority.consent", "The run's consent covers every call the step makes")
def check_consent(ctx: Context, step: dict[str, Any]) -> Optional[dict[str, Any]]:
    needed = step_permissions(step)
    if not needed:
        return None
    derived = _consent_derivation(ctx)
    if derived is None:
        return result("authority.consent", "unknown",
                      "could not derive this run's consent (identity/consent.workflow_needs "
                      "raised); the step's calls were not compared against it")
    granted = set(derived["permissions"])
    missing = [(t, p) for t, p in needed if p not in granted]
    roles = _runs_as(ctx, step)
    ceiling_gaps = []
    for role in roles:
        ceiling = _passport_ceiling(ctx, role)
        if ceiling is not None:
            ceiling_gaps += [(role, t, p) for t, p in needed if p not in ceiling]
    calls = ", ".join(f"{t} ({p})" for t, p in needed)
    applies = _identity_applies(ctx)
    if not missing and not ceiling_gaps:
        return result("authority.consent", "ready",
                      f"calls {calls}; the consent this run asks for grants "
                      f"{', '.join(sorted({p for _, p in needed}))}"
                      + ("" if applies else " (no agent Passports here: steps run as the person)"))
    parts = []
    if missing:
        parts.append(
            "the consent this run asks for (consent.workflow_needs) grants "
            f"[{', '.join(sorted(granted))}] and not "
            + ", ".join(f"{p} (needed by {t})" for t, p in missing))
    if ceiling_gaps:
        parts.append("the agent's Passport ceiling lacks " + ", ".join(
            f"{p} ({r} calling {t})" for r, t, p in ceiling_gaps))
    skips = precheck_for(_cap_key(step)).on_refusal == "skips"
    if not applies:
        return result("authority.consent", "unknown",
                      f"calls {calls}; " + "; ".join(parts) + ". No agent Passports exist here, "
                      "so steps run as the person and this gap does not bite on this install; "
                      "it does wherever identity applies.",
                      action="Derive the consent from every tool a capability calls "
                             "(CapabilityPrecheck.invokes_tools), not only Capability.tool.")
    grant_fix = (" Grant " + ", ".join(sorted({f"{p} on {r}'s Passport" for r, _, p in
                                                ceiling_gaps})) + " (Agent Studio).")
    if skips or not missing:
        return result(
            "authority.consent", "fixable",
            f"calls {calls}; " + "; ".join(parts) + (
                ". The runner SKIPS this step when its call is refused (no failure, but none of "
                "its work happens)" if skips else
                ". Under the run's delegated token the call is refused and the step fails at "
                "start unless the Passport is widened first"),
            action=(grant_fix.strip() if ceiling_gaps else "")
            + (" BOSS change: ask for " + ", ".join(sorted({p for _, p in missing}))
               + " in the run's consent." if missing else ""),
            details={"missing": [p for _, p in missing], "granted": sorted(granted),
                     "runs_as": roles, "ceiling_gaps": [p for _, _, p in ceiling_gaps]})
    return result(
        "authority.consent", "will_fail",
        f"calls {calls}; " + "; ".join(parts) + ". Under the run's delegated token the call is "
        "refused (identity_gate / runs.authorize_tool) and the step fails at start.",
        action=("BOSS change: consent.workflow_needs must ask for "
                + ", ".join(sorted({p for _, p in missing} | {p for _, _, p in ceiling_gaps}))
                + " for this step (derive it from CapabilityPrecheck.invokes_tools). A person "
                "cannot add a permission to the consent sheet; a standing grant that already "
                "holds it is the only workaround."),
        details={"missing": [p for _, p in missing], "granted": sorted(granted),
                 "runs_as": roles})


# ── GitHub ────────────────────────────────────────────────────────────────────────────────────

def _binding(ctx: Context) -> Optional[dict[str, Any]]:
    repo = _repository(ctx)

    def read() -> Optional[dict[str, Any]]:
        row = ctx.connection.execute(text(
            "SELECT id, full_name, status, checks, checked_at, default_branch, connection_id "
            "FROM github_repository_bindings WHERE project_id = :p "
            "AND (:r = '' OR lower(full_name) = lower(:r)) "
            "ORDER BY (role = 'primary') DESC LIMIT 1"),
            {"p": ctx.project_id, "r": repo}).mappings().first()
        return dict(row) if row else None
    return ctx.once("binding", lambda: ctx.savepoint(read, default=None))


def _github_token(ctx: Context, binding: Mapping[str, Any]) -> str:
    def read() -> str:
        from .setup_actions import _service

        service = _service(ctx.settings)
        ref = service.secret_refs.require_usable(
            ctx.connection, project_id=ctx.project_id,
            credential_ref=service._credential_ref(ctx.connection, ctx.project_id,  # noqa: SLF001
                                                   binding["connection_id"]))
        return str(service._resolve_secret(ref, ctx.connection) or "")  # noqa: SLF001
    token = ctx.once("gh_token", lambda: ctx.savepoint(read, default="")) or ""
    if not token:
        try:
            from .setup_view import install_repository

            owner, name, tok = install_repository(ctx.settings)
            if f"{owner}/{name}".lower() == str(binding.get("full_name") or "").lower():
                token = tok or ""
        except Exception:  # noqa: BLE001
            token = ""
    return token


def _gh_get(ctx: Context, path: str) -> tuple[Optional[int], Any]:
    """One read-only GET against GitHub with the project's token; (status, json) or (None, why)."""
    import urllib.error
    import urllib.request

    binding = _binding(ctx)
    if not binding:
        return None, "no repository binding"
    token = _github_token(ctx, binding)
    if not token:
        return None, "no usable token"
    base = str(getattr(ctx.settings, "github_api_base_url", "") or "https://api.github.com")
    req = urllib.request.Request(base.rstrip("/") + path, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "User-Agent": "boss-precheck"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310 — GitHub API, GET only
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__


@_check("github.access", "Read and push access to the repository and its branch",
        fast=True, shared=True)
def check_github(ctx: Context, step: dict[str, Any]) -> dict[str, Any]:
    binding = _binding(ctx)
    repo = _repository(ctx)
    if not binding:
        # Readiness owns "no repository" (its `repository` need, which start_anyway accepts).
        return result("github.access", "fixable",
                      f"no repository binding{' for ' + repo if repo else ''} in this project",
                      action="Connect the repository in Repo and models.")
    checks = binding.get("checks") or {}
    if isinstance(checks, str):
        checks = json.loads(checks)
    probes = {k: bool((checks.get(k) or {}).get("ok")) for k in ("read", "write", "pull_requests")}
    when = binding.get("checked_at")
    age_h = None
    if when is not None:
        from datetime import datetime, timezone

        age_h = (datetime.now(timezone.utc) - when).total_seconds() / 3600
    recorded = (f"{binding['full_name']}: last access check "
                f"{'%.0fh ago' % age_h if age_h is not None else 'never'} — "
                + ", ".join(f"{k} {'ok' if v else 'FAILED'}" for k, v in probes.items()))
    live = ""
    if not ctx.offline:
        status, body = _gh_get(ctx, f"/repos/{binding['full_name']}")
        if status == 200 and isinstance(body, dict):
            perms = body.get("permissions") or {}
            default = body.get("default_branch") or binding.get("default_branch") or "main"
            b_status, _ = _gh_get(ctx, f"/repos/{binding['full_name']}/branches/{default}")
            live = (f"; live: repository readable, default branch {default} "
                    f"{'exists' if b_status == 200 else 'NOT FOUND (%s)' % b_status}"
                    + (f", push {'allowed' if perms.get('push') else 'NOT allowed'}"
                       if perms else ""))
            if b_status not in (200, None) or (perms and not perms.get("push")):
                return result("github.access", "will_fail", recorded + live,
                              action="Give the GitHub connection push access to the "
                                     "repository (and create its default branch).")
        else:
            live = f"; live read: {status or body}"
            if status in (401, 403, 404):
                return result("github.access", "will_fail", recorded + live,
                              action="Reconnect the repository in Repo and models "
                                     "(test_github_access re-runs the probes).")
    if not all(probes.values()):
        return result("github.access", "will_fail", recorded + live,
                      action="Run test_github_access; fix the access the failed probe names.")
    if age_h is None or age_h > 24 and not live:
        return result("github.access", "unknown", recorded + live + " (stale: not re-probed)",
                      action="Run test_github_access to re-probe read, write and pull requests.")
    return result("github.access", "ready", recorded + live)


def _starting_person(ctx: Context) -> Optional[tuple[str, str]]:
    """(org id, principal id) of the person who presses Start: the signed-in person this
    pre-check runs for, else `actor` when it names an active person (principal id or email)."""
    def read() -> Optional[tuple[str, str]]:
        from ..identity import lineage

        auth = lineage.current_context()
        if auth is not None and auth.is_human_session:
            return auth.principal.org_id, auth.principal.id
        row = ctx.connection.execute(text(
            "SELECT p.org_id, p.id FROM identity_principals p LEFT JOIN identity_users u "
            "ON u.principal_id = p.id WHERE p.kind = 'user' AND p.status = 'active' "
            "AND (p.id = :a OR lower(u.email) = lower(:a)) LIMIT 1"), {"a": ctx.actor}).first()
        return (str(row[0]), str(row[1])) if row else None
    return ctx.once("starting_person", lambda: ctx.savepoint(read, default=None))


@_check("github.own_link", "The person starting the run can push to its repository with their "
        "own GitHub", fast=True, shared=True)
def check_github_own_link(ctx: Context, step: dict[str, Any]) -> Optional[dict[str, Any]]:
    """github.access checks BOSS's repository binding; the consent at Start checks the starting
    person's OWN GitHub link and push rights. Asked with the start door's own rules
    (consent.own_github_refusal → grants.user_repo_access_refusal), so the two cannot disagree."""
    from ..identity.grants import GITHUB_LINK_FIX

    derived = _consent_derivation(ctx) or {}
    repos = list(derived.get("repos") or [])
    if not repos:
        return None                       # no repository in the consent (or authority.consent says why)
    person = _starting_person(ctx)
    if person is None:
        return result("github.own_link", "unknown",
                      f"no signed-in person is starting this run, so their own GitHub push access "
                      f"to {', '.join(repos)} was not checked (the consent at Start checks it)",
                      action="Run the pre-check signed in, or as the person who will press Start "
                             "(--as).")

    def ask() -> dict[str, Any]:
        from ..identity import consent

        try:
            return consent.own_github_refusal(
                ctx.connection, org_id=person[0], person_id=person[1], project_id=ctx.project_id,
                role_keys=derived["roles"], permissions=derived["permissions"], repos=repos,
                probe=not ctx.offline)
        except Exception as exc:  # noqa: BLE001 — GitHub or the vault could not answer
            return {"state": "error", "why": f"{type(exc).__name__}: {exc}"[:200]}
    answer = ctx.once("own_github", lambda: ctx.savepoint(ask, default=None)) \
        or {"state": "error", "why": "the read failed"}
    state, who, repo = answer["state"], person[1], answer.get("repo") or repos[0]
    if state == "not_asked":
        return (result("github.own_link", "ready", f"a grant of {who}'s already covers this run; "
                       "Start asks for no consent") if answer.get("covered") else None)
    if state == "not_checked":
        return result("github.own_link", "ready", "this install does not check people's own "
                      "GitHub access (one person, or the policy)")
    if state == "ok":
        return result("github.own_link", "ready",
                      f"{who}'s own GitHub can {answer['needs']} to {', '.join(repos)}")
    if state == "refused" and answer.get("answer") is False:
        return result("github.own_link", "fixable",
                      f"Your own GitHub account can't {answer['needs']} to {repo}, so you can't let "
                      "an agent do it for you (" + ("the GitHub linked lacks that access"
                                                    if answer.get("linked") else
                                                    f"{who} has no GitHub linked")
                      + "). The consent at Start refuses it (github_access_lacking).",
                      action=f"{GITHUB_LINK_FIX}, then try again.",
                      details={"repo": repo, "needs": answer["needs"],
                               "linked": bool(answer.get("linked"))})
    why = {"unprobed": f"{who} has a GitHub linked; its {answer.get('needs')} access to "
                       f"{', '.join(repos)} was not asked of GitHub (offline)",
           "refused": f"GitHub did not answer whether {who}'s own GitHub can "
                      f"{answer.get('needs')} to {repo}; the consent at Start refuses that too",
           }.get(state, f"could not check {who}'s own GitHub: {answer.get('why')}")
    return result("github.own_link", "unknown", why,
                  action=f"Re-run the pre-check online; if it still fails, {GITHUB_LINK_FIX}.")


def _repo_files(ctx: Context, rel_dir: str) -> Optional[list[str]]:
    """Names in a repository directory: from the local repo root, else GitHub (read-only)."""
    if ctx.repo_root is not None:
        d = ctx.repo_root / rel_dir
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []
    if ctx.offline:
        return None
    binding = _binding(ctx)
    if not binding:
        return None
    status, body = _gh_get(ctx, f"/repos/{binding['full_name']}/contents/{rel_dir}")
    if status == 404:
        return []
    if status == 200 and isinstance(body, list):
        return sorted(str(i.get("name")) for i in body if isinstance(i, dict))
    return None


def _repo_text(ctx: Context, rel_path: str) -> Optional[str]:
    if ctx.repo_root is not None:
        p = ctx.repo_root / rel_path
        return p.read_text(errors="ignore") if p.is_file() else ""
    if ctx.offline:
        return None
    binding = _binding(ctx)
    if not binding:
        return None
    status, body = _gh_get(ctx, f"/repos/{binding['full_name']}/contents/{rel_path}")
    if status == 404:
        return ""
    if status == 200 and isinstance(body, dict) and body.get("content"):
        import base64

        return base64.b64decode(body["content"]).decode("utf-8", errors="ignore")
    return None


@_check("github.ci", "CI exists for the app's path and has the services the app needs",
        fast=False)
def check_ci(ctx: Context, step: dict[str, Any]) -> dict[str, Any]:
    names = _repo_files(ctx, ".github/workflows")
    if names is None:
        return result("github.ci", "unknown", "could not list .github/workflows (offline or no "
                      "token)", action="Re-run the pre-check with network access.")
    flows = [n for n in names if n.endswith((".yml", ".yaml"))]
    if not flows:
        return result("github.ci", "ready",
                      "the repository has no .github/workflows: wait_for_ci records `no_ci` "
                      "after its grace period and does not block (ci_gate); BOSS's own gate is "
                      "the only test run")
    rel = _app_rel(ctx)
    uses_pg = bool(_postgres_signals(ctx))
    covering, pg_services = [], []
    for name in flows:
        body = _repo_text(ctx, f".github/workflows/{name}") or ""
        paths = re.findall(r"^\s*-\s*['\"]?([^'\"\n]+)['\"]?\s*$", body.split("jobs:")[0], re.M)
        if not rel or not paths or any(rel in p or p.startswith("**") for p in paths):
            covering.append(name)
        if re.search(r"image:\s*['\"]?postgres", body):
            pg_services.append(name)
    evidence = (f"workflows: {', '.join(flows)}; covering {rel or 'the root'}: "
                f"{', '.join(covering) or 'none'}")
    if not covering:
        return result("github.ci", "will_fail", evidence + " — no workflow runs for this app's "
                      "path, so wait_for_ci waits on nothing that tests it",
                      action=f"Add a workflow whose paths include {rel}/**.")
    if uses_pg and not any(n in pg_services for n in covering):
        return result("github.ci", "will_fail",
                      evidence + "; the app uses PostgreSQL and no covering workflow starts a "
                      "postgres service", action="Add `services: postgres` to the app's CI job.")
    return result("github.ci", "ready", evidence)


# ── Sandbox / toolchain ───────────────────────────────────────────────────────────────────────

@_check("sandbox.worker", "A worker is alive to run the steps", shared=True)
def check_worker(ctx: Context, step: dict[str, Any]) -> dict[str, Any]:
    def read() -> dict[str, Any]:
        row = ctx.connection.execute(text(
            "SELECT max(heartbeat_at) AS hb, max(updated_at) FILTER (WHERE status IN "
            "('succeeded','completed','failed')) AS done, "
            "count(*) FILTER (WHERE status = 'queued' AND lease_owner IS NULL "
            "  AND run_after < now() - interval '10 minutes') AS stuck "
            "FROM jobs WHERE project_id = :p"), {"p": ctx.project_id}).mappings().first()
        return dict(row or {})
    row = ctx.savepoint(read, default=None)
    if row is None:
        return result("sandbox.worker", "unknown", "could not read the job queue")
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    last = max([t for t in (row.get("hb"), row.get("done")) if t is not None], default=None)
    age = (now - last).total_seconds() / 60 if last else None
    evidence = (f"last worker activity {'%.0f min ago' % age if age is not None else 'never'}; "
                f"{int(row.get('stuck') or 0)} job(s) queued >10 min with no lease")
    if row.get("stuck") and (age is None or age > 15):
        return result("sandbox.worker", "will_fail", evidence,
                      action="Start the worker (docker compose up -d worker) and check its log.")
    if age is None or age > 60:
        return result("sandbox.worker", "unknown", evidence + " (idle or absent: nothing proves "
                      "a worker is polling)", action="Check that the worker container is up.")
    return result("sandbox.worker", "ready", evidence)


def _engines_node(ctx: Context) -> str:
    pkg = _package_json(ctx)
    return str(((pkg or {}).get("engines") or {}).get("node") or "")


def _version_ok(have: str, want: str) -> Optional[bool]:
    m = re.search(r"(\d+)", have or "")
    w = re.search(r">=?\s*(\d+)", want or "") or re.search(r"\^?(\d+)", want or "")
    if not m or not w:
        return None
    return int(m.group(1)) >= int(w.group(1))


def _java_app(ctx: Context) -> Optional[str]:
    """"maven" | "gradle" when the pre-check's app (ctx.app_dir, the app's own directory) is a
    Java build; a Node app beside a pom.xml is still a Node app."""
    if ctx.app_dir is None or (ctx.app_dir / "package.json").is_file():
        return None
    from . import toolchains

    return toolchains.detect_java(ctx.app_dir)


def _check_java_toolchain(ctx: Context, build: str) -> dict[str, Any]:
    from . import toolchains

    tool = "mvn" if build == "maven" else "gradle"
    tools = {t: shutil.which(t) for t in ("git", "java", tool)}
    have = toolchains.installed().get("java") or {}
    evidence = (f"the app is a {build} build ({toolchains.build_file(build)}); in this process "
                f"({os.uname().nodename}): "
                + ", ".join(f"{t} {'ok' if p else 'MISSING'}" for t, p in tools.items())
                + ("; the image's Java toolchain: "
                   + ", ".join(f"{k} {v}" for k, v in sorted(have.items())) if have else
                   "; this install has no Java toolchain"))
    if not have:
        # The build's own gate refuses with these words; say them before the run spends.
        return result("sandbox.toolchain", "will_fail", evidence,
                      action=toolchains.missing_message("java", toolchains.build_file(build)))
    if any(not p for p in tools.values()):
        return result("sandbox.toolchain", "unknown", evidence + " — checked where the pre-check "
                      "runs; the build runs in the worker", action="Run the CLI in the worker "
                      "container to check the build's own toolchain.")
    return result("sandbox.toolchain", "ready", evidence)


@_check("sandbox.toolchain", "Node, git and compilers present in the sandbox", shared=True)
def check_toolchain(ctx: Context, step: dict[str, Any]) -> dict[str, Any]:
    java = _java_app(ctx)
    if java:
        return _check_java_toolchain(ctx, java)
    tools = {t: shutil.which(t) for t in ("git", "node", "npm", "npx", "python3", "cc", "make")}
    missing = [t for t, p in tools.items() if not p]
    node = ""
    if tools.get("node") and not ctx.offline:
        try:
            node = owned_process.run(["node", "--version"], capture_output=True, text=True,
                                     timeout=5).stdout.strip()
        except Exception:  # noqa: BLE001
            node = ""
    want = _engines_node(ctx)
    evidence = (f"in this process ({os.uname().nodename}): "
                + ", ".join(f"{t} {'ok' if p else 'MISSING'}" for t, p in tools.items())
                + (f"; node {node}" if node else "")
                + (f"; the app wants node {want}" if want else ""))
    if want and node and _version_ok(node, want) is False:
        return result("sandbox.toolchain", "will_fail", evidence,
                      action=f"Install node {want} in the worker image.")
    if any(t in missing for t in ("git", "node", "npm")):
        return result("sandbox.toolchain", "unknown", evidence + " — checked where the pre-check "
                      "runs; the build runs in the worker", action="Run the CLI in the worker "
                      "container to check the build's own toolchain.")
    native = _install_scripts(ctx)
    if missing and native:
        return result("sandbox.toolchain", "will_fail" if "cc" in missing else "unknown",
                      evidence + f" — the app's lockfile has packages that run install scripts "
                      f"({', '.join(native[:4])}), which compile natively",
                      action="Add a C toolchain to the image that runs builds (the API image).")
    if missing:
        return result("sandbox.toolchain", "unknown" if ctx.app_dir is None else "ready",
                      evidence + (" — fine unless a dependency compiles natively (no app tree "
                                  "to tell)" if ctx.app_dir is None else
                                  " — no dependency in the lockfile runs an install script"))
    return result("sandbox.toolchain", "ready", evidence)


def _install_scripts(ctx: Context) -> list[str]:
    """Packages in the app's lockfile that build native code on install: an install script AND
    a native-addon helper among their dependencies (a prebuilt-binary picker like esbuild's has
    the script and none of these)."""
    if ctx.app_dir is None or not (ctx.app_dir / "package-lock.json").is_file():
        return []
    try:
        lock = json.loads((ctx.app_dir / "package-lock.json").read_text())
    except (OSError, ValueError):
        return []
    return sorted({k.rsplit("node_modules/", 1)[-1] for k, v in (lock.get("packages") or {}).items()
                   if k and isinstance(v, dict) and v.get("hasInstallScript")
                   and not v.get("optional")
                   and set(v.get("dependencies") or {}) & _NATIVE_HELPERS})


_NATIVE_HELPERS = {"nan", "node-addon-api", "node-gyp", "node-gyp-build", "prebuild-install",
                   "node-pre-gyp", "@mapbox/node-pre-gyp", "bindings"}


@_check("sandbox.build_services", "The build-services Postgres is reachable when the app needs it",
        fast=False, shared=True)
def check_build_services(ctx: Context, step: dict[str, Any]) -> Optional[dict[str, Any]]:
    from . import build_services

    signals = _postgres_signals(ctx)
    setting = ctx.savepoint(lambda: ctx.connection.execute(text(
        "SELECT value FROM project_settings WHERE project_id = :p AND key = :k"),
        {"p": ctx.project_id, "k": build_services.SETTING_KEY}).scalar())
    mode = build_services.mode_of(setting)
    if not signals and mode != build_services.ALWAYS:
        if ctx.app_dir is None:
            return result("sandbox.build_services", "unknown",
                          "no app tree yet: cannot tell whether the build will want a database "
                          f"(project setting: {mode})")
        return result("sandbox.build_services", "ready", "the app does not use PostgreSQL")
    status = build_services.status()
    evidence = (f"app uses PostgreSQL ({'; '.join(signals[:2]) or 'setting always'}); "
                f"build-services: configured={status.get('configured')}, "
                f"reachable={status.get('reachable')}")
    if mode == build_services.NEVER:
        return result("sandbox.build_services", "will_fail", evidence + "; project setting is "
                      "Never, so the build gets no database", action="Set build services to Auto.")
    if not status.get("reachable"):
        return result("sandbox.build_services", "will_fail",
                      evidence + f" — {status.get('problem') or ''}",
                      action="docker compose up -d build-services")
    return result("sandbox.build_services", "ready", evidence)


def _workspace_root(ctx: Context) -> str:
    for attr in ("code_task_workspace_root", "workspace_root", "workspaces_dir"):
        value = getattr(ctx.settings, attr, None)
        if value and Path(str(value)).exists():
            return str(value)
    return "/"


@_check("sandbox.disk", f"At least {MIN_FREE_DISK_GB} GB free where builds run", shared=True)
def check_disk(ctx: Context, step: dict[str, Any]) -> dict[str, Any]:
    where = _workspace_root(ctx)
    try:
        usage = shutil.disk_usage(where)
    except OSError as exc:
        return result("sandbox.disk", "unknown", f"cannot stat {where}: {exc}")
    free = usage.free / 1e9
    evidence = f"{free:.1f} GB free at {where} (need {MIN_FREE_DISK_GB})"
    if free < 5:
        return result("sandbox.disk", "will_fail", evidence,
                      action="Free disk: docker builder prune; remove old previews.")
    if free < MIN_FREE_DISK_GB:
        return result("sandbox.disk", "fixable", evidence,
                      action="Free disk before starting (docker builder prune -f).")
    return result("sandbox.disk", "ready", evidence)


def _task_spec(step: Mapping[str, Any]) -> dict[str, Any]:
    """The routing a build step hands its code task: the literal `model_config` / `profile`
    the step binds (a run-input reference is resolved only at run time)."""
    binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
    inputs = binding.get("inputs") if isinstance(binding.get("inputs"), dict) else {}
    out: dict[str, Any] = {}
    if isinstance(inputs.get("model_config"), dict):
        out["model_config"] = inputs["model_config"]
    if isinstance(inputs.get("profile"), str) and "{{" not in inputs["profile"]:
        out["profile"] = inputs["profile"]
    return out


def _step_routes(ctx: Context, step: Mapping[str, Any]) -> Optional[list]:
    """The routes (engine_startup.EngineRoute) the step's agent will take, resolved by the
    worker's own routing; [] for a step that runs no agent, None when routing could not be
    read."""
    from . import engine_startup

    kind = AGENT_ROUTE.get(_cap_key(step))
    if not kind:
        return []
    task = _task_spec(step) if kind != "chat" else {}

    def resolve() -> list:
        if kind == "chat":
            return [engine_startup.chat_route(ctx.connection, ctx.settings, ctx.project_id)]
        stages = (engine_startup.REVIEW_STAGES if kind == "review"
                  else engine_startup.BUILD_STAGES)
        return engine_startup.build_routes(ctx.connection, ctx.settings, ctx.project_id,
                                           task, stages)
    return ctx.once(f"routes:{kind}:{json.dumps(task, sort_keys=True, default=str)}",
                    lambda: ctx.savepoint(resolve, default=None))


def _route_lines(step_id: str, routes: Sequence[Any]) -> list[str]:
    """`<step>: implement, write_tests → claude_agent_sdk / claude-sonnet-5 via setup-anthropic
    (pinned in Repo and models)` — stages that share a route on one line."""
    grouped: dict[tuple, list[str]] = {}
    for r in routes:
        grouped.setdefault((r.engine, r.model, r.source_id, r.why), []).append(r.stage)
    out = []
    for (engine, model, source, why), stages in grouped.items():
        head = ", ".join(stages)
        out.append(f"{step_id}: " + (f"{head} → no engine ({why})" if not engine else
                   f"{head} → {engine} / {model or 'its default model'} via "
                   f"{source or 'the install environment'} ({why})"))
    return out


@_check("sandbox.model", "The model each step routes to is connected, and a lane is free",
        shared=True)
def check_model(ctx: Context, step: dict[str, Any]) -> dict[str, Any]:
    """WHAT ROUTING WILL USE, not the first connected-looking source. It reported "a model is
    connected: claude_code (subscription)" — the chat job's answer, on a disconnected
    subscription — for a run whose Build job was pinned to claude_agent_sdk on the API key."""
    from . import engine_startup

    per_step = [(str(s["id"]), _step_routes(ctx, s)) for s in _ordered(ctx.spec)
                if "sandbox.model" in precheck_for(_cap_key(s)).checks]
    running = ctx.savepoint(lambda: ctx.connection.execute(text(
        "SELECT count(*) FROM code_task_runs WHERE project_id = :p AND status IN "
        "('running','queued','in_progress')"), {"p": ctx.project_id}).scalar(), default=None)
    lanes = int(getattr(ctx.settings, "code_agent_max_concurrency", 1) or 1)
    tail = f"; code tasks in flight {running}/{lanes} lane(s)"
    if any(routes is None for _sid, routes in per_step):
        return result("sandbox.model", "unknown", "could not resolve which model routing will "
                      "use" + tail, action="Check Repo and models.")
    lines, nothing, dead, seen = [], [], [], {}
    for sid, routes in per_step:
        lines += _route_lines(sid, routes)
        for r in routes:
            if not r.engine:
                nothing.append(f"{sid}:{r.stage}")
                continue
            if r.source_id not in seen:
                seen[r.source_id] = ctx.savepoint(lambda r=r: engine_startup.source_state(
                    ctx.connection, ctx.project_id, r.source_id), default=(None, "unreadable"))
            usable, said = seen[r.source_id]
            if usable is False:
                dead.append((f"{sid}:{r.stage}", r, said))
    states = "; ".join(f"{said}" for _src, (_ok, said) in seen.items())
    evidence = "; ".join(lines) + (f". Sources: {states}" if states else "") + tail
    if nothing:
        return result("sandbox.model", "will_fail", evidence + f" — nothing can answer "
                      f"{', '.join(nothing[:4])}", action="Connect a model in Repo and models.")
    if dead:
        where, r, said = dead[0]
        return result("sandbox.model", "will_fail", evidence + f" — {where} routes to "
                      f"{r.engine} on {said}", action=f"Reconnect {r.source_id} in Repo and "
                      "models, or pick a connected source for that job.")
    if running is not None and int(running) >= lanes:
        return result("sandbox.model", "unknown", evidence + " — this run's builds will queue "
                      "behind work already in the lanes", action="Wait, or raise "
                      "BOSS_CODE_AGENT_MAX_CONCURRENCY.")
    return result("sandbox.model", "ready" if lines else "unknown", evidence)


_ORDER = {"will_fail": 0, "unknown": 1, "fixable": 2, "ready": 3}


@_check("sandbox.engine", "The engine each step will use can start here: its binaries, its "
        "sandbox, its credential")
def check_engine(ctx: Context, step: dict[str, Any]) -> Optional[dict[str, Any]]:
    """THE ENGINE ROUTING WILL USE, STARTED. Deterministic and free: the engine's declared
    runtime binaries (harness_registry.RuntimeBinary) on this process's PATH. Live (not
    --offline, like github.access): one tiny session per distinct engine/source/model, run the
    way a build starts it, that must run one harmless command through its shell tool
    (engine_startup.probe; metered by the engine's own path)."""
    from . import engine_startup
    from .harness_registry import missing_runtime

    routes = _step_routes(ctx, step)
    if routes == []:
        return None
    sid = str(step["id"])
    if routes is None:
        return result("sandbox.engine", "unknown", f"{sid}: could not resolve which engine "
                      "routing will use", action="Check Repo and models.")
    lines = _route_lines(sid, routes)
    if any(not r.engine for r in routes):
        return result("sandbox.engine", "will_fail", "; ".join(lines),
                      action="Connect a model in Repo and models.")
    distinct: dict[tuple, Any] = {}
    for r in routes:
        distinct.setdefault(r.key(), r)
    verdict, notes, actions = "ready", [], []
    for r in distinct.values():
        missing = ctx.once(f"engine_bins:{r.engine}", lambda r=r: missing_runtime(r.engine))
        if missing:
            verdict = "will_fail"
            notes.append(f"{r.engine} needs {', '.join(m.binary for m in missing)} on PATH and "
                         "this BOSS has none (" + ", ".join(
                             f"{m.binary}: {m.why or 'runtime'}" for m in missing) + ")")
            actions.append("Install " + ", ".join(m.install_hint() for m in missing)
                           + " in the image (Dockerfile.community runtime stage) and rebuild.")
            continue
        notes.append(f"{r.engine}: runtime binaries on PATH")
        if ctx.offline:
            continue
        probe = ctx.once(f"engine_probe:{'|'.join(r.key())}", lambda r=r: engine_startup.probe(
            r, ctx.settings, ctx.connection, ctx.project_id))
        notes[-1] = f"{r.engine} / {r.model or 'default'} live: {probe.detail}"
        if _ORDER[probe.verdict] < _ORDER[verdict]:
            verdict = probe.verdict
        if probe.verdict == "will_fail":
            actions.append(f"Fix what {r.engine} reports (its sandbox, its credential, the "
                           "account's credit), or pin the job to another engine in Repo and "
                           "models.")
    return result("sandbox.engine", verdict, "; ".join(lines) + ". " + "; ".join(notes)
                  + (" (offline: not started)" if ctx.offline else ""),
                  action=" ".join(dict.fromkeys(actions)))


# ── Budget ────────────────────────────────────────────────────────────────────────────────────

def step_reservation(settings: Any, step: Mapping[str, Any]) -> int:
    """What the step reserves when it starts (CapabilityPrecheck.reserves), in micros."""
    declared = precheck_for(_cap_key(step)).reserves
    if not declared:
        return 0
    attr, times = declared
    return int(getattr(settings, attr, 0) or 0) * max(1, int(times))


def _parallel_groups(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each `parallel` step: its branches (step ids until the join) and the join's policy."""
    by_id = {str(s["id"]): s for s in _steps(spec)}
    out = []
    for s in by_id.values():
        if s.get("kind") != "parallel":
            continue
        join = str(s.get("join") or "")
        branches = {}
        for b in s.get("branches") or []:
            branches[str(b)] = sorted(_reachable(by_id, str(b), stop=join))
        policy = str((by_id.get(join) or {}).get("success_policy") or "all")
        out.append({"id": str(s["id"]), "join": join, "policy": policy, "branches": branches})
    return out


def _budget_snapshot(ctx: Context) -> Optional[dict[str, Any]]:
    def snap() -> Optional[dict[str, Any]]:
        from .budget import BudgetGovernor

        return BudgetGovernor(ctx.settings).status_for_project(ctx.connection, ctx.project_id)
    return ctx.once("budget", lambda: ctx.savepoint(snap, default=None))


@_check("budget.headroom", "The day's cap holds this step's reservation alongside other work")
def check_budget(ctx: Context, step: dict[str, Any]) -> Optional[dict[str, Any]]:
    amount = step_reservation(ctx.settings, step)
    if not amount:
        return None
    snapshot = _budget_snapshot(ctx)
    if not snapshot:
        return result("budget.headroom", "unknown", "could not read the budget")
    others = ctx.once("active_runs", lambda: ctx.savepoint(lambda: int(ctx.connection.execute(
        text("SELECT count(*) FROM workflow_runs WHERE project_id = :p AND status IN "
             "('running','waiting','pending','paused')"), {"p": ctx.project_id}).scalar() or 0),
        default=0))
    worst: Optional[dict[str, Any]] = None
    lines = []
    for name, period in (snapshot.get("periods") or {"day": snapshot}).items():
        cap = int(period.get("cap_usd_micros") or 0)
        if not cap:
            continue
        actual = int(period.get("actual_usd_micros") or 0)
        reserved = int(period.get("reserved_usd_micros") or 0)
        lines.append(f"{name}: cap {_money(cap)}, spent {_money(actual)}, held by other work "
                     f"{_money(reserved)}")
        if actual + amount > cap:
            worst = {"kind": "cap", "period": name}
        elif actual + reserved + amount > cap and (worst or {}).get("kind") != "cap":
            worst = {"kind": "headroom", "period": name}
        elif amount * 2 > cap - actual and worst is None:
            worst = {"kind": "overlap", "period": name}
    evidence = (f"{step.get('id')} reserves {_money(amount)} when it starts; "
                + "; ".join(lines) + f"; {others} other run(s) of this project in flight")
    if worst is None:
        return result("budget.headroom", "ready", evidence)
    if worst["kind"] == "cap":
        return result("budget.headroom", "will_fail", evidence + " — the money already spent "
                      "leaves no room for this reservation", action="Raise the cap in Govern › Limits or "
                      "wait for the period to reset.")
    if worst["kind"] == "headroom":
        return result("budget.headroom", "unknown", evidence + " — other work's reservations "
                      "fill the cap now; the reservation is refused until they release",
                      action="Start after the overlapping runs finish, or raise the cap.")
    return result("budget.headroom", "unknown", evidence + " — two overlapping reservations of "
                  "this size do not fit what is left of the cap, so a concurrent run (or a "
                  "retry of this step) is refused 'headroom' while this one holds it",
                  action="Do not overlap runs, or raise the cap above twice this reservation.")


def _pace(ctx: Context) -> dict[str, int]:
    """The project's burst cap ($/min, micros) and max concurrent jobs (budget_policies)."""
    def read() -> dict[str, int]:
        row = ctx.connection.execute(text(
            "SELECT burst_cap_usd_micros_per_min AS burst, max_concurrent_jobs AS lanes FROM "
            "budget_policies WHERE scope_type = 'project' AND scope_id = :p ORDER BY "
            "created_at DESC LIMIT 1"), {"p": ctx.project_id}).mappings().first()
        return {"burst": int((row or {}).get("burst") or 0),
                "lanes": int((row or {}).get("lanes") or 0)}
    return ctx.once("budget_pace", lambda: ctx.savepoint(read, default={}))


@_check("budget.run_cost", "The run's estimated cost fits what the caps leave, at a pace the "
        "burst cap allows", (PRE_RUN,), shared=True)
def check_run_cost(ctx: Context, step: dict[str, Any]) -> dict[str, Any]:
    """WHAT THE WHOLE RUN WILL COST, not whether one reservation fits: past runs' recorded
    token usage priced at today's routes, against the day's and month's caps and the burst cap
    (run_cost_estimate)."""
    from . import run_cost_estimate as rc

    found = ctx.once("run_cost_history", lambda: ctx.savepoint(lambda: rc.history(
        ctx.connection, ctx.project_id, ctx.workflow_key), default=None))
    if found is None:
        return result("budget.run_cost", "unknown", "could not read past runs' model usage")
    stages = sorted({str(c.get("stage") or "model_call") for r in found.get("runs") or []
                     for c in r.get("calls") or []})
    routes = ctx.once("run_cost_routes", lambda: ctx.savepoint(lambda: rc.routes_now(
        ctx.connection, ctx.settings, ctx.project_id, stages), default=None)) if stages else {}
    if routes is None:
        return result("budget.run_cost", "unknown", "could not resolve which models the run's "
                      "steps route to today", action="Check Repo and models.")
    estimate = rc.estimate(found, routes)
    pace = _pace(ctx)
    verdict, evidence, action = rc.judge(
        estimate, _budget_snapshot(ctx), int(pace.get("burst") or 0),
        int(pace.get("lanes") or 0), {s: routes[s] for s in stages if s in routes})
    details = {k: estimate[k] for k in ("money", "value", "low", "high", "n", "basis",
                                        "active_min", "span_min", "peak_parallel",
                                        "typical_turn", "by_stage")} if estimate else None
    return result("budget.run_cost", verdict, evidence, action=action, details=details)


# ── App environment ───────────────────────────────────────────────────────────────────────────

_PURPOSE = {"build": "proof", "preview": "preview", "deploy": "deploy"}
_ENV_NAME = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[A-Z]{4,})\b")
_THROW = re.compile(r"(?:throw\s+new\s+\w*Error|raise\s+\w+|panic|die|sys\.exit|console\.error)"
                    r"\s*\(\s*(?:f?[`\"'])(.{8,300}?)[`\"']", re.S)
_REQUIRED_WORDS = re.compile(r"must be set|is required|are required|required\b|missing|not set|"
                             r"must be provided|must be defined", re.I)
_FORBIDDEN_WORDS = re.compile(r"must not be set|must be unset|not allowed", re.I)
_SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".mjs", ".cjs", ".py", ".go", ".rb", ".java")
_SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", "coverage", "__pycache__",
              ".venv", "venv", "target"}   # target: Maven's output (generated sources too)


def _source_files(app_dir: Path, limit: int = 400) -> Iterable[Path]:
    count = 0
    for root, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            p = Path(root) / f
            if p.suffix in _SOURCE_SUFFIXES and p.stat().st_size < 400_000:
                count += 1
                if count > limit:
                    return
                yield p


def code_env_requirements(app_dir: Optional[Path]) -> dict[str, dict[str, Any]]:
    """{NAME: {"required": [where...], "forbidden": [where...], "messages": [...]}} from the
    app's own start-up errors ("DEMO_PASSWORD must be set"). Names only; never a value."""
    out: dict[str, dict[str, Any]] = {}
    if app_dir is None or not app_dir.is_dir():
        return out
    for path in _source_files(app_dir):
        rel = "/" + str(path.relative_to(app_dir))
        if "/test" in rel or "/__tests__/" in rel or ".test." in path.name \
                or ".spec." in path.name:
            continue
        try:
            body = path.read_text(errors="ignore")
        except OSError:
            continue
        for m in _THROW.finditer(body):
            message = m.group(1)
            names = [n for n in _ENV_NAME.findall(message) if n not in ("BOSS", "HTTP", "JSON")]
            if not names:
                continue
            where = f"{path.relative_to(app_dir)}:{body.count(chr(10), 0, m.start()) + 1}"
            kind = ("forbidden" if _FORBIDDEN_WORDS.search(message) else
                    "required" if _REQUIRED_WORDS.search(message) else "")
            if not kind:
                continue
            name = names[0]
            entry = out.setdefault(name, {"required": [], "forbidden": [], "messages": []})
            entry[kind].append(where)
            entry["messages"].append(message)
    return out


def _declared_needs(ctx: Context) -> dict[str, Any]:
    from ..templates import run_env

    def read() -> dict[str, Any]:
        return run_env.app_needs(ctx.connection, project_id=ctx.project_id,
                                 app_key=_app_key(ctx) or None, app_dir=ctx.app_dir)
    return ctx.once("declared_needs", lambda: ctx.savepoint(read, default={}) or {})


def _template_declared(ctx: Context) -> dict[str, dict]:
    from ..templates.secrets import declared_specs

    key = _app_key(ctx)
    if not key:
        return {}
    return ctx.once("template_declared", lambda: ctx.savepoint(
        lambda: declared_specs(ctx.connection, project_id=ctx.project_id, app_key=key),
        default={}) or {})


def binding_refusal(name: str, env: str, template_declared: Mapping[str, Any]) -> Optional[str]:
    """Would PUT /v1/apps/{app}/secrets/{name} accept a value for `env`? None = yes; otherwise
    the API's own refusal (routes_templates.put_app_secret → secrets.deploy_value_spec)."""
    from ..templates.secrets import deploy_value_spec
    from ..templates.service import TemplateError

    if name in template_declared:
        return None
    try:
        deploy_value_spec(name, [env], "test")
    except TemplateError as error:
        return f"{(getattr(error, 'body', None) or {}).get('error', 'refused')}: {error}"
    except Exception as error:  # noqa: BLE001
        return str(error)
    return None


def _has_binding(ctx: Context, name: str, env: str) -> bool:
    from ..templates.secrets import has_binding

    key = _app_key(ctx)
    if not key:
        return False
    return bool(ctx.savepoint(lambda: has_binding(ctx.connection, project_id=ctx.project_id,
                                                  app_key=key, name=name, env=env), default=False))


@_check("app_env", "Every variable the app needs is given in the env this step runs it in")
def check_app_env(ctx: Context, step: dict[str, Any]) -> Optional[dict[str, Any]]:
    from ..templates import run_env

    env = precheck_for(_cap_key(step)).app_env
    if not env:
        return None
    check_id = f"app_env.{env}"
    declared = dict(_declared_needs(ctx))
    code = ctx.once("code_env", lambda: code_env_requirements(ctx.app_dir))
    template = _template_declared(ctx)
    if not declared and not code:
        return result(check_id, "unknown" if ctx.app_dir is None else "ready",
                      "the app declares no variables" + (" yet (no app tree: only its templates "
                                                         "were read)" if ctx.app_dir is None else ""))
    purpose = _PURPOSE[env]
    problems, notes, fixable, autos = [], [], [], []  # noqa: F841
    bound = {n: "x" for n in sorted(set(declared) | set(code)) if _has_binding(ctx, n, env)}
    plan = run_env.resolve(declared, purpose=purpose,
                           bound={n: v for n, v in bound.items() if n in declared})
    for need in plan.needs:
        if env == "deploy" and need.kind in ("generated", "demo"):
            # A deploy mints its own once (secrets.ensure_deploy_generated, which also makes a
            # demo login's password: secrets.ensure_demo_logins), before it plans.
            autos.append(need.name)
            continue
        refused = binding_refusal(need.name, env, template)
        if refused:
            problems.append(f"{need.name} ({need.where()}) has no {env} value and the API "
                            f"refuses one ({refused})")
        else:
            fixable.append(f"{need.name} ({need.where()}) needs a {env} value a person sets")
    # A build and a preview of a Postgres app get their own database's variables from the
    # build-services server (build_services.ENV_NAMES); sandbox.build_services checks that.
    from . import build_services

    given = set(build_services.ENV_NAMES) if env in ("build", "preview") \
        and _postgres_signals(ctx) else set()
    for name, found in sorted(code.items()):
        if not found["required"] or name in bound or name in given:
            continue
        if env == "deploy" and name in declared and declared[name].kind in ("generated", "demo"):
            if name not in autos:
                autos.append(name)
            continue
        verdict = run_env.decide(declared[name], has_value=False, purpose=purpose) \
            if name in declared else "unset"
        if verdict in ("generate", "use"):
            continue
        if env == "deploy" and found["forbidden"]:
            notes.append(f"{name}: the app also refuses it in some environments "
                         f"({', '.join(found['forbidden'][:2])}) — check the deploy's NODE_ENV")
        how = (f"declared by {declared[name].source} as '{declared[name].kind}' (BOSS leaves it "
               "unset)" if name in declared else "declared nowhere BOSS reads")
        refused = binding_refusal(name, env, template)
        line = (f"{name}: the app refuses to start without it "
                f"({', '.join(found['required'][:2])}); {how}")
        if refused:
            problems.append(line + f"; and the API refuses a {env} value ({refused})")
        else:
            fixable.append(line + f"; set it for {env} in the app's Secrets")
    # P11's class: would BOSS's crash explanation name the variable the app named?
    for name, found in code.items():
        for message in found["messages"]:
            blamed = plan.unset_named_in(message)
            if blamed and blamed != name and name in plan.unset:
                notes.append(f"explain_crash would blame {blamed}, not {name}, for "
                             f"\"{message[:80]}\"")
    autos = list(plan.generated) + autos
    evidence_bits = [f"{len(declared)} declared, {len(autos)} generated by BOSS "
                     f"({', '.join(autos[:4])})" if autos else f"{len(declared)} declared",
                     f"{len(plan.unset)} left unset"
                     + (f" ({', '.join(plan.unset[:8])})" if plan.unset else "")]
    evidence = "; ".join(evidence_bits)
    if problems:
        return result(check_id, "will_fail", evidence + ". " + " ".join(problems)
                      + (" " + " ".join(notes) if notes else ""),
                      action=(f"BOSS change: accept a {env} binding for an app-declared name "
                              "(secrets.deploy_value_spec allows deploy only), or have the app "
                              "start without it in a throwaway run."),
                      details={"problems": problems, "fixable": fixable, "notes": notes})
    if fixable:
        return result(check_id, "fixable", evidence + ". " + " ".join(fixable)
                      + (" " + " ".join(notes) if notes else ""),
                      action=f"Set the named variables for env {env} in the app's Secrets.",
                      details={"fixable": fixable, "notes": notes})
    return result(check_id, "ready" if not notes else "unknown",
                  evidence + (". " + " ".join(notes) if notes else ""), auto=bool(autos),
                  action="BOSS generates " + ", ".join(autos) if autos else "")


# ── The app tree (PRE-DEPLOY) ─────────────────────────────────────────────────────────────────

def _package_json(ctx: Context) -> Optional[dict[str, Any]]:
    if ctx.app_dir is None:
        return None
    p = ctx.app_dir / "package.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _postgres_signals(ctx: Context) -> list[str]:
    def read() -> list[str]:
        if ctx.app_dir is None:
            return []
        from . import build_services

        try:
            return build_services.detect_in_code(ctx.app_dir)
        except Exception:  # noqa: BLE001
            return []
    return ctx.once("pg", read)


def _needs_tree(ctx: Context, check_id: str) -> Optional[dict[str, Any]]:
    if ctx.app_dir is None or not ctx.app_dir.is_dir():
        return result(check_id, "unknown", (
            f"build {ctx.build_run_id} saved no workspace to read" if ctx.build_run_id
            else "no app tree to read (pre-deploy checks need the build: --build-run, or its "
                 "workspace: --app-dir)"))
    return None


@_check("app.test_plan", "The gate will run the app's tests, and leaves package.json alone",
        (PRE_DEPLOY,))
def check_test_plan(ctx: Context, step: dict[str, Any]) -> dict[str, Any]:
    missing = _needs_tree(ctx, "app.test_plan")
    if missing:
        return missing
    from . import verification_target as vt

    pkg = _package_json(ctx) or {}
    scripts = pkg.get("scripts") or {}
    tests = {k: v for k, v in scripts.items() if k == "test" or k.startswith("test:")}
    title = str((ctx.run_input or {}).get("title") or "")
    spec: dict[str, Any] = {}
    if ctx.build_run_id:
        row = ctx.savepoint(lambda: ctx.connection.execute(text(
            "SELECT spec, title FROM code_task_runs WHERE id = :i"),
            {"i": ctx.build_run_id}).mappings().first())
        if row:
            raw = row["spec"]
            spec = raw if isinstance(raw, dict) else json.loads(raw or "{}")
            title = str(row["title"] or title)
    try:
        plan = vt.resolve_gate_plan(spec, title, ctx.app_dir, app_id=None, changed=None)
        plan_txt = f"gate plan ({plan.source}): {plan.description[:200]}"
        finding = getattr(plan, "finding", None) or plan.error
    except Exception as exc:  # noqa: BLE001
        plan_txt, finding = f"gate plan could not be resolved: {exc}", None
    # What the gate's own package.json hardening would write into the app (and so the PR).
    hardened: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        src = ctx.app_dir / "package.json"
        if src.is_file():
            shutil.copy(src, Path(tmp) / "package.json")
            try:
                hardened = list(vt.harden_web_package(tmp).get("changed") or [])
            except Exception:  # noqa: BLE001
                hardened = []
    evidence = (f"scripts: {', '.join(sorted(tests)) or 'no test script'}; {plan_txt}"
                + (f"; the gate's hardening would change package.json: {', '.join(hardened)}"
                   if hardened else ""))
    if "test" not in scripts and any(k.startswith("test:") for k in scripts):
        return result("app.test_plan", "will_fail", evidence + " — only test:* scripts: the "
                      "gate runs `npm test`, so the app's tests never run",
                      action="Add a `test` script that runs the app's suite.")
    if finding:
        return result("app.test_plan", "will_fail", evidence + f" — {finding}",
                      action="Add a `test` script to package.json.")
    if hardened:
        return result("app.test_plan", "fixable", evidence + " — the run will not fail, but "
                      "those edits land in the app's PR", action="BOSS change: install the "
                      "gate's own dev dependencies in the sandbox, not into the app's package.json.")
    return result("app.test_plan", "ready", evidence)


@_check("app.dependencies", "The app's dependencies resolve (no ERESOLVE)", (PRE_DEPLOY,),
        fast=False)
def check_dependencies(ctx: Context, step: dict[str, Any]) -> dict[str, Any]:
    missing = _needs_tree(ctx, "app.dependencies")
    if missing:
        return missing
    if not (ctx.app_dir / "package.json").is_file():
        return result("app.dependencies", "ready", "not a Node app")
    if not shutil.which("npm"):
        return result("app.dependencies", "unknown", "npm is not installed where the pre-check ran")
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("package.json", "package-lock.json", ".npmrc"):
            if (ctx.app_dir / name).is_file():
                shutil.copy(ctx.app_dir / name, Path(tmp) / name)
        started = time.monotonic()
        try:
            proc = owned_process.run(
                ["npm", "install", "--dry-run", "--package-lock-only", "--ignore-scripts",
                 "--no-audit", "--no-fund", "--loglevel=error"],
                cwd=tmp, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            return result("app.dependencies", "unknown", "npm dry run took over 90 s")
        took = time.monotonic() - started
    out = (proc.stdout + proc.stderr)[-600:]
    if "ERESOLVE" in out:
        return result("app.dependencies", "will_fail", f"npm cannot resolve the tree: {out}",
                      action="Fix the conflicting peer versions in package.json.")
    if proc.returncode != 0:
        return result("app.dependencies", "unknown", f"npm dry run exited {proc.returncode} "
                      f"in {took:.0f}s: {out.strip()[:300]}")
    return result("app.dependencies", "ready", f"npm resolves the tree ({took:.0f}s dry run)")


@_check("app.secret_scan", "The committed-secrets gate passes on the app's files", (PRE_DEPLOY,))
def check_secret_scan(ctx: Context, step: dict[str, Any]) -> dict[str, Any]:
    missing = _needs_tree(ctx, "app.secret_scan")
    if missing:
        return missing
    from ..templates.secret_scan import scan_files

    files: dict[str, str] = {}
    for root, dirs, names in os.walk(ctx.app_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for n in names:
            p = Path(root) / n
            if p.stat().st_size < 300_000:
                try:
                    files[str(p.relative_to(ctx.app_dir))] = p.read_text(errors="ignore")
                except OSError:
                    continue
    findings = scan_files(files)
    if not findings:
        return result("app.secret_scan", "ready", f"{len(files)} files scanned, nothing flagged")
    where = "; ".join(f"{f.as_dict().get('path')}:{f.as_dict().get('line')} "
                      f"{f.as_dict().get('rule')}" for f in findings[:5])
    applies = bool(_app_key(ctx)) or bool((ctx.run_input or {}).get("template"))
    return result("app.secret_scan", "will_fail" if applies else "unknown",
                  f"{len(findings)} finding(s): {where}"
                  + ("" if applies else " (the gate runs for template apps; none applied here)"),
                  action="Remove the values (placeholders only in .env.example).")


_BUILD_CMD = re.compile(r"\b(?:npm run build|npm run build:\w+|vite build|next build|tsc\b|"
                     r"pnpm build|yarn build|webpack|esbuild|ng build)")


@_check("preview.serves_build", "The preview's command builds what the server serves",
        (PRE_DEPLOY,))
def check_preview_build(ctx: Context, step: dict[str, Any]) -> dict[str, Any]:
    missing = _needs_tree(ctx, "preview.serves_build")
    if missing:
        return missing
    from .preview_service import _declared_command_in

    command = _declared_command_in(ctx.app_dir) or ""
    served: list[tuple[str, str]] = []
    for path in _source_files(ctx.app_dir):
        try:
            body = path.read_text(errors="ignore")
        except OSError:
            continue
        if not re.search(r"express\.static|serveStatic|sendFile|StaticFiles|static_folder", body):
            continue
        for m in re.finditer(r"[\"'`]((?:\./)?(?:dist|build|out)(?:/[\w.-]+)*)/?[\"'`]", body):
            served.append((m.group(1).removeprefix("./"), str(path.relative_to(ctx.app_dir))))
    if not command:
        return result("preview.serves_build", "unknown",
                      "no boss.preview.command (BOSS derives the command); "
                      f"served build dirs: {', '.join(d for d, _ in served) or 'none'}")
    if not served:
        return result("preview.serves_build", "ready", f"command `{command}`; the server serves "
                      "no build output directory")
    absent = [(d, w) for d, w in served if not (ctx.app_dir / d).exists()]
    builds = bool(_BUILD_CMD.search(command))
    evidence = (f"command `{command}` (BOSS prefixes only npm install); the server serves "
                + ", ".join(f"{d}/ ({w})" for d, w in served))
    if absent and not builds:
        return result("preview.serves_build", "will_fail",
                      evidence + f" — {', '.join(d for d, _ in absent)} is not in the tree and "
                      "the command builds nothing, so GET / answers 404",
                      action="Declare `npm run build && " + command + "` in boss.preview.command.")
    return result("preview.serves_build", "ready", evidence)


def _app_manifest(ctx: Context) -> tuple[Optional[Path], Any, str]:
    """(path, parsed manifest or None, parse error) for the app's OWN boss.deploy.yaml."""
    if ctx.app_dir is None:
        return None, None, ""
    path = ctx.app_dir / "boss.deploy.yaml"
    if not path.is_file():
        return None, None, ""
    from .app_deploy_manifest import load_manifest

    try:
        return path, load_manifest(path), ""
    except Exception as exc:  # noqa: BLE001
        return path, None, str(exc)


@_check("deploy.manifest", "The app declares how it is deployed, in ITS folder", (PRE_DEPLOY,))
def check_manifest(ctx: Context, step: dict[str, Any]) -> dict[str, Any]:
    missing = _needs_tree(ctx, "deploy.manifest")
    if missing:
        return missing
    path, manifest, error = _app_manifest(ctx)
    rel = _app_rel(ctx)
    root_text = _repo_text(ctx, "boss.deploy.yaml") if rel else None
    root_note = ""
    if root_text:
        m = re.search(r"^name:\s*([\w-]+)", root_text, re.M)
        root_note = (f"; the repository root has a boss.deploy.yaml for "
                     f"'{m.group(1) if m else 'another app'}' — another app's, not this one's")
    elif root_text is None and rel:
        root_note = "; the repository root could not be read (offline)"
    if error:
        return result("deploy.manifest", "will_fail", f"{rel}/boss.deploy.yaml does not parse: "
                      f"{error}", action="Fix the manifest.")
    if manifest is not None:
        return result("deploy.manifest", "ready", f"{rel or '.'}/boss.deploy.yaml: kind "
                      f"{manifest.kind}, {len(manifest.health)} health check(s), "
                      f"{len(manifest.resources)} resource(s){root_note}")
    server = bool(_server_entry(ctx))
    return result("deploy.manifest", "will_fail" if server else "unknown",
                  f"no boss.deploy.yaml in {rel or 'the app folder'}{root_note}. Without it the "
                  "deploy takes the legacy one-container path, which guesses how the app runs"
                  + (" — this app is a server (it listens on a port), which that path does not "
                     "start with its database and variables" if server else ""),
                  action=f"Have the build write {rel or '.'}/boss.deploy.yaml (kind, env, "
                         "resources, health).")


def _server_entry(ctx: Context) -> list[str]:
    if ctx.app_dir is None:
        return []
    hits = []
    for path in _source_files(ctx.app_dir):
        try:
            body = path.read_text(errors="ignore")
        except OSError:
            continue
        if re.search(r"\.listen\(\s*(?:port|PORT|Number\(process\.env\.PORT)|uvicorn\.run|"
                     r"app\.run\(|"
                     # Java: Spring Boot's main, a Dropwizard Application subclass.
                     r"SpringApplication\.run\(|extends\s+Application<", body):
            hits.append(str(path.relative_to(ctx.app_dir)))
    return hits


@_check("deploy.database", "A Postgres app gets a database where it is deployed", (PRE_DEPLOY,))
def check_database(ctx: Context, step: dict[str, Any]) -> dict[str, Any]:
    missing = _needs_tree(ctx, "deploy.database")
    if missing:
        return missing
    signals = _postgres_signals(ctx)
    if not signals:
        return result("deploy.database", "ready", "the app does not use PostgreSQL")
    _path, manifest, _err = _app_manifest(ctx)
    provided = []
    if manifest is not None:
        for r in manifest.resources:
            capability = str(getattr(r, "capability", "") or (r.get("capability") if isinstance(
                r, dict) else ""))
            if capability.startswith("database"):
                provided.append(f"resource {getattr(r, 'name', '') or capability}")
        if manifest.kind == "compose" and ctx.app_dir is not None:
            compose = ctx.app_dir / (manifest.compose_file or "docker-compose.yml")
            if compose.is_file() and re.search(r"image:\s*['\"]?postgres", compose.read_text()):
                provided.append(f"compose service in {compose.name}")
        if any(e.key == "DATABASE_URL" for e in manifest.env):
            provided.append("DATABASE_URL in env:")
    if _has_binding(ctx, "DATABASE_URL", "deploy"):
        provided.append("a deploy binding for DATABASE_URL")
    evidence = f"the app uses PostgreSQL ({'; '.join(signals[:2])})"
    if provided:
        return result("deploy.database", "ready", evidence + "; provided by " + ", ".join(provided))
    return result("deploy.database", "will_fail", evidence + "; nothing gives it a database at "
                  "deploy (no database resource, no compose postgres, no DATABASE_URL binding)",
                  action="Declare `resources: {db: {capability: database.relational, engine: "
                         "postgres}}` in the app's boss.deploy.yaml.")


def _routes(ctx: Context) -> set[str]:
    found: set[str] = set()
    if ctx.app_dir is None:
        return found
    for path in _source_files(ctx.app_dir):
        try:
            body = path.read_text(errors="ignore")
        except OSError:
            continue
        found.update(re.findall(r"\.(?:get|all|use|route)\(\s*[\"'`](/[\w/.:-]*)[\"'`]", body))
        found.update(re.findall(r"@\w+\.(?:get|route|api_route)\(\s*[\"'](/[\w/.:-]*)[\"']", body))
        if path.suffix == ".java":
            found.update(_java_routes(body))
    if _spring_actuator(ctx.app_dir):
        # Spring Boot Actuator serves its health endpoint with no route in the app's own code.
        found.add("/actuator/health")
    return found


#: Spring MVC mappings (@GetMapping("/x"), @RequestMapping(value = "/x"), path = {"/x"}) and
#: JAX-RS (@Path("/x"), Dropwizard). A class-level prefix is not joined to its methods: the
#: health check asks whether a path is served at all, and a health route is rarely prefixed.
_JAVA_ROUTE = re.compile(
    r'@(?:Get|Post|Put|Delete|Patch|Request)Mapping\(\s*(?:(?:value|path)\s*=\s*)?\{?\s*'
    r'"(/[\w/.:{}-]*)"|@Path\(\s*"(/[\w/.:{}-]*)"')


def _java_routes(body: str) -> set[str]:
    return {a or b for a, b in _JAVA_ROUTE.findall(body)}


def _spring_actuator(app_dir: Optional[Path]) -> bool:
    if app_dir is None:
        return False
    for name in ("pom.xml", "build.gradle", "build.gradle.kts"):
        try:
            if "spring-boot-starter-actuator" in (app_dir / name).read_text(errors="ignore"):
                return True
        except OSError:
            continue
    return False


@_check("deploy.health", "The health path the deploy checks is one the app serves", (PRE_DEPLOY,))
def check_health(ctx: Context, step: dict[str, Any]) -> dict[str, Any]:
    missing = _needs_tree(ctx, "deploy.health")
    if missing:
        return missing
    _path, manifest, _err = _app_manifest(ctx)
    routes = _routes(ctx)
    health_like = sorted(r for r in routes if re.search(r"health|ready|live|status", r))
    if manifest is None or not manifest.health:
        return result("deploy.health", "fixable",
                      "no health declared in a boss.deploy.yaml; the deploy and smoke fall back "
                      "to GET /" + (f"; the app serves {', '.join(health_like)}" if health_like
                                    else "; the app serves no health route"),
                      action="Add a /healthz route and declare it under health: in the app's "
                             "boss.deploy.yaml.")
    from urllib.parse import urlparse

    paths = [urlparse(h.url.replace("{target.public_url}", "http://x")).path or "/"
             for h in manifest.health]
    unserved = [p for p in paths if p != "/" and p not in routes]
    if unserved:
        return result("deploy.health", "will_fail", f"health checks {', '.join(paths)}; the app "
                      f"serves no {', '.join(unserved)} (routes: {', '.join(sorted(routes))[:200]})",
                      action="Serve the declared health path, or declare the one it serves.")
    return result("deploy.health", "ready", f"health checks {', '.join(paths)}, served")


# ── Code-aware checks on the app tree (PRE-DEPLOY) ────────────────────────────────────────────

_BUILD_DIRS = ("dist/", "build/", "out/", ".output/", "lib/")
_RUNS_FILE = re.compile(r"\b(?:node|tsx|ts-node|bun)\s+(?:--?[\w-]+(?:[= ][\w./-]+)?\s+)*"
                        r"((?:\./)?[\w@./-]+\.(?:c?m?js|ts|tsx))\b")
#: What can emit server JavaScript into a build directory. `vite build` alone emits the CLIENT
#: bundle; with `--ssr` (or a server build script) it emits a server.
_EMITS_SERVER = re.compile(r"\b(?:tsc\b|tsup|esbuild|rollup|webpack|ncc|swc|babel\b|"
                           r"vite build[^&;|]*--ssr|nest build|next build|build:server|"
                           r"build:api|build:backend)")


@_check("code.start_command", "The start command runs a file the build produces", (PRE_DEPLOY,))
def check_start_command(ctx: Context, step: dict[str, Any]) -> Optional[dict[str, Any]]:
    missing = _needs_tree(ctx, "code.start_command")
    if missing:
        return missing
    pkg = _package_json(ctx)
    if pkg is None:
        return None
    scripts = pkg.get("scripts") or {}
    start = str(scripts.get("start") or "")
    if not start:
        return result("code.start_command", "unknown", "no `start` script: the deploy needs "
                      "its manifest (or the legacy guess) to know how the app runs",
                      action="Add a `start` script (or declare the command in boss.deploy.yaml).")
    build_chain = " && ".join(str(v) for k, v in scripts.items()
                              if k == "build" or k.startswith("build:"))
    problems, notes = [], []
    for target in _RUNS_FILE.findall(start):
        rel = target.removeprefix("./")
        if (ctx.app_dir / rel).exists():
            notes.append(f"{rel} is in the tree")
            continue
        if rel.startswith(_BUILD_DIRS):
            if _EMITS_SERVER.search(build_chain):
                notes.append(f"{rel} is built by `{build_chain[:80]}`")
            else:
                problems.append(f"`start` runs {rel}, which is not in the tree, and the build "
                                f"(`{build_chain or 'no build script'}`) emits no server code "
                                "into it" + (" — `vite build` alone emits the client bundle"
                                             if "vite build" in build_chain else ""))
        else:
            problems.append(f"`start` runs {rel}, which is not in the tree and is not build "
                            "output")
    evidence = f"start: `{start[:120]}`" + (f"; {'; '.join(notes)}" if notes else "")
    if problems:
        return result("code.start_command", "will_fail", evidence + ". " + " ".join(problems),
                      action="Make `build` emit the server (tsc/esbuild into that path) or point "
                             "`start` at the source with tsx.")
    return result("code.start_command", "ready", evidence)


_PROD_GUARD = re.compile(r"NODE_ENV\s*[!=]==?\s*[\"']production[\"']|[\"']production[\"']\s*"
                         r"[!=]==?\s*\w*NODE_ENV|\bis_?[Pp]rod(?:uction)?\b|\bIS_PROD\b|"
                         r"\bisProduction\b|\bprod(?:uction)?Mode\b", re.S)


def production_refusals(app_dir: Optional[Path]) -> dict[str, dict[str, list[str]]]:
    """{NAME: {"required": [where], "forbidden": [where]}} for start-up errors the app raises
    only under NODE_ENV=production (a production guard within the few lines before the throw)."""
    out: dict[str, dict[str, list[str]]] = {}
    if app_dir is None or not app_dir.is_dir():
        return out
    for path in _source_files(app_dir):
        rel = str(path.relative_to(app_dir))
        if "test" in rel.split("/")[0] or ".test." in path.name or ".spec." in path.name:
            continue
        try:
            body = path.read_text(errors="ignore")
        except OSError:
            continue
        if not _PROD_GUARD.search(body):
            continue
        for m in _THROW.finditer(body):
            line = body.count("\n", 0, m.start()) + 1
            window = "\n".join(body.splitlines()[max(0, line - 7):line])
            if not _PROD_GUARD.search(window):
                continue
            message = m.group(1)
            names = [n for n in _ENV_NAME.findall(message) if n not in ("BOSS", "HTTP", "JSON",
                                                                        "NODE_ENV")]
            if not names:
                continue
            kind = ("forbidden" if _FORBIDDEN_WORDS.search(message) else "required")
            out.setdefault(names[0], {"required": [], "forbidden": []})[kind].append(
                f"{rel}:{line}")
    return out


def _deploy_bindings(ctx: Context) -> dict[str, str]:
    key = _app_key(ctx)
    if not key:
        return {}
    rows = ctx.savepoint(lambda: ctx.connection.execute(text(
        "SELECT name, mode FROM app_secret_bindings WHERE project_id = :p AND app_key = :a AND "
        "env = 'deploy'"), {"p": ctx.project_id, "a": key}).mappings().all(), default=[]) or []
    return {str(r["name"]): str(r["mode"]) for r in rows}


@_check("code.prod_refusal", "The app does not refuse in production what BOSS gives it",
        (PRE_DEPLOY,))
def check_prod_refusal(ctx: Context, step: dict[str, Any]) -> Optional[dict[str, Any]]:
    missing = _needs_tree(ctx, "code.prod_refusal")
    if missing:
        return missing
    found = ctx.once("prod_refusals", lambda: production_refusals(ctx.app_dir))
    if not found:
        return result("code.prod_refusal", "ready", "no start-up error guarded by "
                      "NODE_ENV=production")
    bound = _deploy_bindings(ctx)
    declared = dict(_declared_needs(ctx))
    bites, risks = [], []
    for name, where in sorted(found.items()):
        mode = bound.get(name)
        kind = getattr(declared.get(name), "kind", "") if name in declared else ""
        if where["forbidden"] and mode:
            bites.append(f"{name}: the app refuses it in production ({where['forbidden'][0]}) "
                         f"and BOSS binds it for deploy ({mode})")
        elif where["required"] and mode in ("stub", "test"):
            bites.append(f"{name}: the app demands a real value in production "
                         f"({where['required'][0]}) and BOSS gives deploy a {mode} value")
        elif where["required"] and not mode and kind not in ("generated",):
            risks.append(f"{name}: required in production ({where['required'][0]}), no deploy "
                         "value bound")
    evidence = f"production-only refusals: {', '.join(sorted(found))}"
    if bites:
        return result("code.prod_refusal", "will_fail", evidence + ". " + "; ".join(bites),
                      action="Unbind the forbidden value for deploy (or set the real one), or "
                             "relax the app's production guard.", details={"names": sorted(found)})
    if risks:
        return result("code.prod_refusal", "fixable", evidence + ". " + "; ".join(risks),
                      action="Set the named values for env deploy in the app's Secrets.")
    return result("code.prod_refusal", "ready", evidence + "; BOSS gives none of them a value "
                  "the app refuses")


def lockfile_drift(app_dir: Path) -> Optional[list[str]]:
    """Dependencies package.json declares that package-lock.json does not record (or records at
    another spec). None when there is no lockfile to compare."""
    lock_path = app_dir / "package-lock.json"
    pkg_path = app_dir / "package.json"
    if not lock_path.is_file() or not pkg_path.is_file():
        return None
    try:
        lock = json.loads(lock_path.read_text())
        pkg = json.loads(pkg_path.read_text())
    except (OSError, ValueError):
        return ["package-lock.json does not parse"]
    root = ((lock.get("packages") or {}).get("") or {}) if isinstance(lock, dict) else {}
    drift = []
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        want = pkg.get(section) or {}
        have = root.get(section)
        if have is None and lock.get("lockfileVersion", 1) == 1:
            have = {k: "*" for k in (lock.get("dependencies") or {})}
        have = have or {}
        for name, spec in want.items():
            if name not in have:
                drift.append(f"{name} missing")
            elif have[name] not in ("*", spec):
                drift.append(f"{name} {have[name]} → {spec}")
        for name in have:
            if name not in want and lock.get("lockfileVersion", 1) != 1:
                drift.append(f"{name} no longer declared")
    return drift


def _regenerate_lock_dry_run(app_dir: Path) -> str:
    """npm regenerates the lockfile in a throwaway copy; returns what changed, in words."""
    if not shutil.which("npm"):
        return "npm is not installed where the pre-check ran"
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("package.json", "package-lock.json", ".npmrc"):
            if (app_dir / name).is_file():
                shutil.copy(app_dir / name, Path(tmp) / name)
        try:
            proc = owned_process.run(["npm", "install", "--package-lock-only", "--ignore-scripts",
                                      "--no-audit", "--no-fund", "--loglevel=error"], cwd=tmp,
                                     capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            return "npm took over 90 s"
        if proc.returncode != 0:
            return f"npm exited {proc.returncode}: {(proc.stderr or proc.stdout)[-200:]}"
        after = lockfile_drift(Path(tmp)) or []
        return ("regenerated in a throwaway copy: in sync afterwards" if not after else
                f"regenerated in a throwaway copy; still drifting: {', '.join(after[:4])}")


@_check("code.lockfile", "The lockfile is in sync with package.json", (PRE_DEPLOY,), fast=False)
def check_lockfile(ctx: Context, step: dict[str, Any]) -> Optional[dict[str, Any]]:
    missing = _needs_tree(ctx, "code.lockfile")
    if missing:
        return missing
    if not (ctx.app_dir / "package.json").is_file():
        return None
    drift = lockfile_drift(ctx.app_dir)
    if drift is None:
        return result("code.lockfile", "unknown", "no package-lock.json: every install resolves "
                      "afresh, so two builds of one commit can differ")
    if not drift:
        return result("code.lockfile", "ready", "package-lock.json records every declared "
                      "dependency at its declared spec")
    report = "" if ctx.offline else _regenerate_lock_dry_run(ctx.app_dir)
    return result("code.lockfile", "fixable", f"package-lock.json drifts from package.json: "
                  f"{', '.join(drift[:6])}" + (f" (+{len(drift) - 6})" if len(drift) > 6 else "")
                  + " — `npm ci` refuses it; `npm install` rewrites it into the PR"
                  + (f". {report}" if report else ""),
                  action="fixable: BOSS can do this (dry run only) — regenerate the lockfile in "
                         "a throwaway copy and report; commit `npm install` in the app to fix",
                  auto=True, details={"autofix": {"fix": "lockfile.regenerate",
                                                  "args": {"report": report or None}}})


def playwright_version(app_dir: Optional[Path]) -> Optional[str]:
    """The Playwright the app's tests run: installed packages, else the lockfile."""
    if app_dir is None:
        return None
    from . import playwright_cache

    found = playwright_cache.project_version(app_dir)
    if found:
        return found
    try:
        lock = json.loads((app_dir / "package-lock.json").read_text())
    except (OSError, ValueError):
        return None
    for name in ("node_modules/@playwright/test", "node_modules/playwright"):
        version = ((lock.get("packages") or {}).get(name) or {}).get("version")
        if isinstance(version, str) and re.match(r"^\d+\.\d+\.\d+$", version):
            return version
    return None


@_check("code.browser_cache", "The browser the app's tests need is already cached",
        (PRE_DEPLOY,))
def check_browser_cache(ctx: Context, step: dict[str, Any]) -> Optional[dict[str, Any]]:
    if ctx.app_dir is None:
        return None
    version = playwright_version(ctx.app_dir)
    if not version:
        return None
    from . import playwright_cache

    base = playwright_cache.browsers_dir(create=False)
    if base is None:
        return result("code.browser_cache", "unknown", f"the app's tests run Playwright "
                      f"{version}; this process has no shared browser cache to check")
    if playwright_cache.is_cached(version, base):
        return result("code.browser_cache", "ready", f"Chromium for Playwright {version} is "
                      f"cached in {base}")
    return result("code.browser_cache", "fixable", f"Chromium for Playwright {version} is not "
                  f"cached in {base}: the first test run downloads it inside the build's time",
                  action="fixable: BOSS can do this — warm the browser cache now", auto=True,
                  details={"autofix": {"fix": "cache.playwright", "args": {"version": version}}})


# ── Deploy target, port, smoke ────────────────────────────────────────────────────────────────

def _deploy_inputs(ctx: Context, step: Mapping[str, Any]) -> dict[str, Any]:
    binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
    inputs = {}
    for k, v in (binding.get("inputs") or {}).items():
        if isinstance(v, (str, int)):
            inputs[k] = v
        elif isinstance(v, dict) and "from_input" in v:
            inputs[k] = (ctx.run_input or {}).get(v["from_input"])
    if ctx.build_run_id:
        inputs["run_id"] = ctx.build_run_id
    return {k: v for k, v in inputs.items() if v not in (None, "")}


def _ever_deployed(ctx: Context, target_id: str) -> bool:
    return bool(ctx.savepoint(lambda: ctx.connection.execute(text(
        "SELECT 1 FROM deployment_runs WHERE deployment_target_id = :t AND status IN "
        "('succeeded','healthy','superseded') UNION ALL SELECT 1 FROM stack_deployments "
        "WHERE target_id = :t AND status IN ('succeeded','healthy','live') LIMIT 1"),
        {"t": target_id}).first(), default=False))


@_check("deploy.target", "Where the deploy goes: its target, path and port")
def check_target(ctx: Context, step: dict[str, Any]) -> dict[str, Any]:
    from .delivery_pipeline import pick_deploy_target

    inputs = _deploy_inputs(ctx, step)
    host_id = str(inputs.get("host_id") or "")
    environment = str(inputs.get("environment") or "staging")
    host = ctx.savepoint(lambda: dict(ctx.connection.execute(text(
        "SELECT id, name, address, status, port_min, port_max FROM deployment_hosts "
        "WHERE id = :h"), {"h": host_id}).mappings().first() or {}), default={}) if host_id else {}
    app = _app_name(ctx)
    repo = _repository(ctx).rsplit("/", 1)[-1].lower()
    targets = ctx.savepoint(lambda: [dict(r) for r in ctx.connection.execute(text(
        "SELECT id, app_name, environment, target_kind, repo, host_id, status, domains, "
        "manifest_path FROM deployment_targets WHERE project_id = :p AND status = 'active' "
        "ORDER BY created_at"), {"p": ctx.project_id}).mappings().all()], default=[]) or []
    candidates = [t for t in targets if t.get("environment") == environment and t.get("host_id")
                  and (not host_id or t.get("host_id") == host_id)
                  and str(t.get("repo") or "").rsplit("/", 1)[-1].lower() == repo]
    mine = pick_deploy_target(candidates, app) if app else None
    lines, verdict, actions = [], "ready", []
    if host_id and not host:
        return result("deploy.target", "will_fail", f"the step names host {host_id}, which does "
                      "not exist", action="Pick an existing host on the deploy step.")
    if host:
        lines.append(f"host {host.get('name')} ({host.get('status')}, ports "
                     f"{host.get('port_min')}-{host.get('port_max')})")
        if host.get("status") != "verified":
            verdict = "fixable"
            actions.append("Finish verifying the host in Infra.")
    # Which path the deploy takes (stack_target_for_deploy, the step's own function).
    stack = None
    declared = None
    if ctx.build_run_id:
        from .stack_deploy import declared_manifest_for_run, stack_target_for_deploy

        stack = ctx.savepoint(lambda: stack_target_for_deploy(
            ctx.connection, ctx.project_id, {**inputs, "environment": environment}), default=None)
        declared = ctx.savepoint(lambda: declared_manifest_for_run(ctx.connection,
                                                                   ctx.build_run_id))
    app_manifest = _app_manifest(ctx)[0] is not None
    if mine:
        lines.append(f"this app's target: {mine['id']} ({mine.get('app_name')}, "
                     f"{mine.get('target_kind')})")
    else:
        lines.append(f"no active {environment} target for '{app or '?'}' on this host — a "
                     "FIRST deploy (stack_target_for_deploy finds no stack target)")
    if ctx.build_run_id:
        lines.append(f"the build's lineage declares {declared or 'no boss.deploy.yaml'}; "
                     f"stack_target_for_deploy → "
                     f"{(stack or {}).get('id') or 'None (the legacy one-container path)'}")
    if not mine and not stack:
        if app_manifest or declared:
            verdict = "will_fail"
            lines.append("the app declares a manifest, so the legacy path refuses it "
                         "(deploy_service.declared_refusal) and nothing else plans it")
            actions.append("BOSS change: a first deploy of an app that declares a manifest must "
                           "create its stack target (or create one in Infra before the deploy).")
        elif ctx.checkpoint == PRE_RUN:
            verdict = "unknown" if verdict == "ready" else verdict
            lines.append("if the build declares a boss.deploy.yaml, the legacy path refuses it "
                         "(declared_refusal); if it does not, the legacy path guesses how the "
                         "app runs")
            actions.append("Create a stack target for this app in Infra before the deploy, or "
                           "re-run the pre-check at pre_deploy.")
    # Ports: what the host has promised, and to whom.
    if host.get("address"):
        from .deployment_fleet import _ports_assigned_on

        taken = ctx.savepoint(lambda: _ports_assigned_on(ctx.connection, host["address"]),
                              default={}) or {}
        low, high = int(host.get("port_min") or 3000), int(host.get("port_max") or 65535)
        first = next((p for p in range(low, min(high, low + 500) + 1) if p not in taken), None)
        owners = {}
        for t in targets:
            for d in t.get("domains") or []:
                head, _, tail = str(d).rpartition(":")
                if head == host["address"] and tail.isdigit():
                    owners[int(tail)] = t
        stale = [(p, owners[p]) for p in sorted(taken) if p in owners
                 and not _ever_deployed(ctx, owners[p]["id"])
                 and (not mine or owners[p]["id"] != mine["id"])]
        if mine:
            ports = [int(str(d).rpartition(":")[2]) for d in mine.get("domains") or []
                     if str(d).rpartition(":")[2].isdigit()]
            lines.append(f"its port: {', '.join(map(str, ports)) or 'none recorded'}")
        else:
            lines.append(f"a new target would get port {first} (first unpromised in "
                         f"{low}-{high}; promised: "
                         + ", ".join(f"{p}→{v}" for p, v in sorted(taken.items())) + ")")
        if stale and not mine:
            lines.append("held by targets that never deployed successfully: " + ", ".join(
                f"{p} ({t.get('app_name')}, {t['id']})" for p, t in stale))
            if verdict == "ready":
                verdict = "fixable"
            actions.append("Retire the never-deployed target(s) holding the low ports, or "
                           "accept the shifted port.")
        if first is None:
            verdict = "will_fail"
            actions.append(f"No free port in {low}-{high}; widen the host's range.")
    return result("deploy.target", verdict, "; ".join(lines), action=" ".join(actions),
                  details={"stack_target": (stack or {}).get("id"), "app": app})


def _handle_kinds(capability: str) -> list[tuple[str, dict[str, Any]]]:
    """Every (await kind, step output) a capability can finish with, from the runner's own
    `_awaited_handle` — fed every id field it knows."""
    from . import workflow_capability_runner as runner

    kind, fields = runner._AWAIT_KINDS.get(capability, ("", ()))  # noqa: SLF001
    probes = [{f: "probe"} for f in fields] + [{"stack_deployment_id": "probe"},
                                               {"change_set_id": "probe"}]
    out, seen = [], set()
    for output in probes:
        handle = runner._awaited_handle(capability, output)  # noqa: SLF001
        if handle and handle["kind"] not in seen:
            seen.add(handle["kind"])
            out.append((handle["kind"], {**output, "await": handle}))
    return out


class _OneRowConnection:
    """A stand-in connection for a reader contract probe: step outputs are the probe's; every
    deployment lookup finds a row."""

    def __init__(self, output: dict[str, Any]):
        self.output = output

    def execute(self, statement: Any, params: Any = None):
        sql = str(statement)
        output = self.output

        class _Rows:
            def mappings(self_inner):
                return self_inner

            def all(self_inner):
                return [{"output": output}] if "workflow_step_runs" in sql else []

            def first(self_inner):
                if "deployment_runs" in sql:
                    return {"id": (params or {}).get("d"), "status": "succeeded",
                            "deployment_target_id": "t", "release_packet": {}}
                return None
        return _Rows()


@_check("smoke.contract", "Smoke can read every kind of deployment the deploy step makes")
def check_smoke(ctx: Context, step: dict[str, Any]) -> Optional[dict[str, Any]]:
    from .deployment_smoke import run_deployment

    by_id = {str(s["id"]): s for s in _steps(ctx.spec)}
    deploys = [s for s in by_id.values() if _cap_key(s) in _DEPLOYS
               and str(step["id"]) in _reachable(by_id, str(s["id"]))]
    if not deploys:
        return result("smoke.contract", "ready", "no deploy before this smoke test")
    unread = []
    for d in deploys:
        for kind, output in _handle_kinds(_cap_key(d)):
            if run_deployment(_OneRowConnection(output), "probe") is None:
                unread.append((str(d["id"]), kind))
    if not unread:
        return result("smoke.contract", "ready", "deployment_smoke.run_deployment reads every "
                      "deployment kind the deploy can finish with")
    evidence = ("deployment_smoke.run_deployment finds no deployment when the deploy finishes "
                "as " + ", ".join(f"'{k}' ({s})" for s, k in unread) + "; the smoke step then "
                "falls back to a free URL probe, which Community refuses")
    stack_path = any(c.get("details", {}).get("stack_target") for c in ctx.memo.get(
        "_target_results", []))
    manifest = ctx.app_dir is not None and _app_manifest(ctx)[0] is not None
    if stack_path or manifest:
        return result("smoke.contract", "will_fail", evidence + " — and this app's deploy ships "
                      "from its manifest (a stack deployment)", action="BOSS change: teach "
                      "deployment_smoke.run_deployment the stack_deployment handle.")
    return result("smoke.contract", "unknown", evidence + " — it bites when the deploy takes the "
                  "manifest (stack) path", action="BOSS change: teach deployment_smoke."
                  "run_deployment the stack_deployment handle.")


# ── Run-lifecycle policy (run-level) ──────────────────────────────────────────────────────────

def _fail_step_consults_advisory() -> bool:
    from . import workflows

    # `fail_step` hands every failed attempt to `_fail_attempt` (which a timed-out attempt
    # reaches too), so the advisory rule may live in either.
    try:
        return any("ADVISORY" in inspect.getsource(fn).upper()
                   for fn in (workflows.fail_step,
                              getattr(workflows, "_fail_attempt", workflows.fail_step)))
    except (OSError, TypeError):
        return False


#: Capabilities that commit to the run's branch, and those that ship whatever the branch holds.
_PUSHES = frozenset({"more_tests", "plrr", "security_fix", "retry_build"})
_SHIPS_BRANCH = frozenset({"deploy", "redeploy", "preview", "wait_for_ci"})


def _pushes_to_branch(step: Mapping[str, Any]) -> bool:
    key = _cap_key(step)
    binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
    return key in _PUSHES or (key == "build" and bool((binding.get("inputs") or {}).get("extend")))


def lifecycle_checks(ctx: Context, step_results: Mapping[str, list[dict[str, Any]]]
                     ) -> list[dict[str, Any]]:
    """Static policy checks over the spec: what a failure in one branch does to the others."""
    from ..identity import maintenance
    from ..identity.maintenance import TERMINAL_WORKFLOW_STATUSES
    from . import workflow_capability_runner as runner

    out: list[dict[str, Any]] = []
    groups = _parallel_groups(ctx.spec)
    by_id = {str(s["id"]): s for s in _steps(ctx.spec)}
    failing = {sid for sid, checks in step_results.items()
               if any(c["verdict"] == "will_fail" for c in checks)}
    # BOSSAPP-540: the grant waits for live branches, and an advisory step's failure (at start
    # too) completes it rather than stopping the run. Once both hold, neither is flagged.
    grant_ends = ("failed" in TERMINAL_WORKFLOW_STATUSES
                  and not hasattr(maintenance, "live_branch_sql"))
    tolerated = (frozenset(getattr(runner, "_ADVISORY_CAPABILITIES", frozenset()))
                 if _fail_step_consults_advisory() else frozenset())
    failing = {sid for sid in failing if _cap_key(by_id.get(sid) or {}) not in tolerated}
    for g in groups:
        branch_steps = {b: steps for b, steps in g["branches"].items()}
        if g["policy"] == "any" or len(branch_steps) < 2:
            continue
        doomed = {b: sorted(set(steps) & failing) for b, steps in branch_steps.items()
                  if set(steps) & failing}
        evidence = (f"{g['id']} runs {len(branch_steps)} branches "
                    + " | ".join(f"[{', '.join(s)}]" for s in branch_steps.values())
                    + f" joined by {g['join']} on '{g['policy']}': a terminal failure in one "
                    "branch stops the run (workflows.fail_step → _stop_run_at)"
                    + ("; the run then ends 'failed', and identity maintenance ends its "
                       "for-this-run grant (end_finished_workflow_grants), so the other "
                       "branch's in-flight work loses its authority" if grant_ends else ""))
        if doomed:
            out.append(result("lifecycle.branch_failure", "will_fail", evidence + ". Expected "
                              "here: " + "; ".join(f"{', '.join(s)} will fail" for s in
                                                    doomed.values()),
                              action="Fix the failing branch step first"
                                     + ("; and (BOSS change) do not end a run's grant while any "
                                        "branch is live." if grant_ends else ".")))
        elif grant_ends or not tolerated:
            out.append(result("lifecycle.branch_failure", "unknown", evidence + ". Nothing "
                              "here is known to fail, so this is a risk, not a verdict.",
                              action="BOSS change: end the grant only when no branch is live; "
                                     "let an advisory branch fail without stopping the run."))
    for g in groups:
        branch_steps = g["branches"]
        pushers = {b: [s for s in steps if _pushes_to_branch(by_id.get(s) or {})]
                   for b, steps in branch_steps.items()}
        shippers = {b: [s for s in steps if _cap_key(by_id.get(s) or {}) in _SHIPS_BRANCH]
                    for b, steps in branch_steps.items()}
        pairs = [(p, s) for bp, ps in pushers.items() for bs, ss in shippers.items()
                 if bp != bs for p in ps for s in ss]
        if pairs:
            out.append(result(
                "lifecycle.concurrent_push", "unknown",
                f"{g['id']} runs " + ", ".join(f"{p} (pushes to the branch)" for p in
                                               sorted({p for p, _ in pairs}))
                + " alongside " + ", ".join(f"{s} (ships the branch)" for s in
                                            sorted({s for _, s in pairs}))
                + ": a push landing while the deploy reads the branch ships a commit nobody "
                  "reviewed in this run, or races the deploy's checkout",
                action="BOSS change: the deploy pins the commit pass 1 built; pass 2 pushes to "
                       "its own branch until the join."))
    advisory = sorted(getattr(runner, "_ADVISORY_CAPABILITIES", frozenset()))
    adv_steps = [sid for sid, s in by_id.items() if _cap_key(s) in advisory]
    if adv_steps and not _fail_step_consults_advisory():
        doomed = sorted(set(adv_steps) & failing)
        evidence = (f"{', '.join(adv_steps)} is advisory only once its work is running "
                    "(resume_awaited → _complete_advisory_failure); a failure while STARTING it "
                    "goes through workflows.fail_step, which does not consult advisory-ness, and "
                    "stops the run")
        out.append(result("lifecycle.advisory_start",
                          "will_fail" if doomed else "unknown",
                          evidence + (f". Expected here: {', '.join(doomed)} fails at start"
                                      if doomed else " (a risk: nothing here is known to fail "
                                      "at start)"),
                          action="BOSS change: an advisory step that fails to start completes "
                                 "with the failure said, as it does when its work fails."))
    return out


# ── Orchestration ─────────────────────────────────────────────────────────────────────────────

def run_checks(ctx: Context, *, only_fast: bool = False) -> dict[str, Any]:
    steps_out: list[dict[str, Any]] = []
    shared_first: dict[str, dict[str, Any]] = {}
    per_step: dict[str, list[dict[str, Any]]] = {}
    target_results: list[dict[str, Any]] = []
    ctx.memo["_target_results"] = target_results
    ordered = _ordered(ctx.spec)
    # deploy.target first, so smoke (earlier or later in the spec) sees which path it takes.
    early: dict[str, dict[str, Any]] = {}
    if ctx.checkpoint in CHECKS["deploy.target"].checkpoints:
        for step in ordered:
            if "deploy.target" in precheck_for(_cap_key(step)).checks:
                try:
                    found = CHECKS["deploy.target"].fn(ctx, step)
                except Exception as exc:  # noqa: BLE001
                    found = result("deploy.target", "unknown", f"the check itself failed: "
                                   f"{type(exc).__name__}: {exc}"[:400])
                early[str(step["id"])] = found
                target_results.append(found)
    computed: dict[int, list[dict[str, Any]]] = {}
    for i, step in enumerate(ordered):
        key = _cap_key(step)
        entry = precheck_for(key)
        checks: list[dict[str, Any]] = []
        refusal = skip_refusal(ctx.spec, step)
        for check_id in entry.checks:
            definition = CHECKS[check_id]
            if ctx.checkpoint not in definition.checkpoints:
                continue
            if only_fast and not definition.fast:
                continue
            if definition.shared and check_id in shared_first:
                shared_first[check_id].setdefault("also_needed_by", []).append(str(step["id"]))
                continue
            started = time.monotonic()
            try:
                found = (early[str(step["id"])] if check_id == "deploy.target"
                         and str(step["id"]) in early else definition.fn(ctx, step))
            except Exception as exc:  # noqa: BLE001 — a check that breaks says so; it never stops the rest
                found = result(check_id, "unknown", f"the check itself failed: "
                               f"{type(exc).__name__}: {exc}"[:400])
            if found is None:
                continue
            found["ms"] = int((time.monotonic() - started) * 1000)
            found["skip_allowed"] = refusal is None
            if refusal:
                found["skip_refused"] = refusal
            if definition.shared:
                shared_first[check_id] = found
            checks.append(found)
        computed[i] = checks
        if checks:
            per_step[str(step["id"])] = checks
    for i, step in enumerate(ordered):
        if computed.get(i):
            steps_out.append({"step_id": str(step["id"]), "capability": _cap_key(step) or
                              str(step.get("kind") or ""), "checks": computed[i]})
    run_level = lifecycle_checks(ctx, per_step)
    for c in run_level:
        c["skip_allowed"] = False
    if run_level:
        steps_out.insert(0, {"step_id": RUN_STEP, "capability": "workflow", "checks": run_level})
    if ctx.families and not only_fast:
        from .workflow_precheck_behaviour import run_families

        steps_out += run_families(ctx, ctx.families)
    every = [c for s in steps_out for c in s["checks"]]
    counts = {v: sum(1 for c in every if c["verdict"] == v) for v in VERDICTS}
    blocking = [f"{s['step_id']}:{c['id']}" for s in steps_out for c in s["checks"]
                if c["verdict"] == "will_fail"]
    return {"checkpoint": ctx.checkpoint, "project_id": ctx.project_id,
            "workflow_key": ctx.workflow_key, "workflow_name": ctx.workflow_name,
            "version_id": ctx.version_id, "run_id": ctx.run_id or None,
            "app_dir": str(ctx.app_dir) if ctx.app_dir else None,
            "build_run_id": ctx.build_run_id or None, "steps": steps_out,
            "summary": {**counts, "blocking": blocking, "ok": not blocking,
                        "sentence": _sentence(counts, blocking)}}


def _sentence(counts: Mapping[str, int], blocking: Sequence[str]) -> str:
    if not blocking:
        return (f"Nothing is known to fail: {counts.get('ready', 0)} ready, "
                f"{counts.get('fixable', 0)} fixable, {counts.get('unknown', 0)} unknown.")
    return (f"{len(blocking)} check(s) say this run will fail: {', '.join(blocking[:6])}"
            f"{' …' if len(blocking) > 6 else ''}. Fix them, skip their steps, or start anyway "
            "by accepting them.")


class PrecheckError(ValueError):
    pass


def _resolve_version(connection: Connection, project_id: str, workflow: str) -> dict[str, Any]:
    from . import workflows

    row = connection.execute(text(
        "SELECT d.workflow_key, d.name FROM workflow_definitions d WHERE d.project_id = :p "
        "AND (d.workflow_key = :w OR d.name = :w OR d.id = :w) ORDER BY d.updated_at DESC "
        "LIMIT 1"), {"p": project_id, "w": workflow}).mappings().first()
    if row is None:
        version = connection.execute(text(
            "SELECT v.id, d.workflow_key, d.name FROM workflow_versions v JOIN "
            "workflow_definitions d ON d.id = v.workflow_definition_id WHERE v.id = :w "
            "AND v.project_id = :p"), {"p": project_id, "w": workflow}).mappings().first()
        if version is None:
            raise PrecheckError(f"no workflow '{workflow}' in project {project_id}")
        return {"version_id": version["id"], "workflow_key": version["workflow_key"],
                "name": version["name"]}
    running = workflows.running_version(connection, project_id, row["workflow_key"])
    if not running or not running.get("version_id"):
        raise PrecheckError(f"workflow '{workflow}' has no running version")
    return {"version_id": running["version_id"], "workflow_key": row["workflow_key"],
            "name": row["name"]}


def build_context(connection: Connection, settings: Any, *, project_id: str, actor: str = "ceo",
                  workflow: Optional[str] = None, version_id: Optional[str] = None,
                  run_id: Optional[str] = None, run_input: Optional[Mapping[str, Any]] = None,
                  checkpoint: str = PRE_RUN, app_dir: Optional[str] = None,
                  repo_root: Optional[str] = None, build_run_id: Optional[str] = None,
                  offline: bool = False, families: Iterable[str] = (),
                  history_days: int = 7) -> Context:
    from . import workflows

    if checkpoint not in CHECKPOINTS:
        raise PrecheckError(f"checkpoint must be one of {', '.join(CHECKPOINTS)}")
    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    name = key = ""
    data = dict(run_input or {})
    if run_id:
        row = connection.execute(text(
            "SELECT r.workflow_version_id, r.input, d.workflow_key, d.name FROM workflow_runs r "
            "JOIN workflow_versions v ON v.id = r.workflow_version_id JOIN workflow_definitions "
            "d ON d.id = v.workflow_definition_id WHERE r.id = :r AND r.project_id = :p"),
            {"r": run_id, "p": project_id}).mappings().first()
        if row is None:
            raise PrecheckError(f"no workflow run {run_id} in project {project_id}")
        version_id, key, name = row["workflow_version_id"], row["workflow_key"], row["name"]
        stored = row["input"] if isinstance(row["input"], dict) else json.loads(row["input"] or "{}")
        data = {**stored, **data}
        spec = workflows._run_spec(connection, run_id)  # noqa: SLF001 — the shape the run walks
        if not build_run_id:
            build_run_id = str(connection.execute(text(
                "SELECT output->>'code_task_run_id' FROM workflow_step_runs WHERE "
                "workflow_run_id = :r AND output ? 'code_task_run_id' ORDER BY created_at DESC "
                "LIMIT 1"), {"r": run_id}).scalar() or "")
    else:
        if not version_id:
            if not workflow:
                raise PrecheckError("name a workflow, a version or a run")
            found = _resolve_version(connection, project_id, workflow)
            version_id, key, name = found["version_id"], found["workflow_key"], found["name"]
        raw = connection.execute(text(
            "SELECT v.spec, d.workflow_key, d.name FROM workflow_versions v JOIN "
            "workflow_definitions d ON d.id = v.workflow_definition_id WHERE v.id = :v "
            "AND v.project_id = :p"), {"v": version_id, "p": project_id}).mappings().first()
        if raw is None:
            raise PrecheckError(f"no workflow version {version_id}")
        key, name = key or raw["workflow_key"], name or raw["name"]
        spec = workflows._published_spec(  # noqa: SLF001 — the spec a run started now would walk
            raw["spec"], workflows.project_roster(connection, project_id))
    app_path = Path(app_dir).resolve() if app_dir else None
    root = Path(repo_root).resolve() if repo_root else None
    if app_path is not None and root is None:
        for parent in [app_path, *app_path.parents]:
            if (parent / ".git").exists():
                root = parent
                break
    return Context(connection=connection, settings=settings, project_id=project_id, actor=actor,
                   spec=spec, run_input=data, checkpoint=checkpoint, version_id=str(version_id),
                   workflow_key=str(key), workflow_name=str(name), run_id=str(run_id or ""),
                   app_dir=app_path, repo_root=root, build_run_id=str(build_run_id or ""),
                   offline=offline, families=tuple(families or ()),
                   history_days=max(1, int(history_days or 7)))


def precheck(engine: Any, settings: Any = None, *, project_id: str, actor: str = "ceo",
             workflow: Optional[str] = None, version_id: Optional[str] = None,
             run_id: Optional[str] = None, run_input: Optional[Mapping[str, Any]] = None,
             checkpoint: str = PRE_RUN, app_dir: Optional[str] = None,
             repo_root: Optional[str] = None, build_run_id: Optional[str] = None,
             offline: bool = False, families: Optional[Iterable[str]] = None,
             history_days: int = 7, fix: bool = False, agent: bool = False,
             agent_max_usd: Optional[float] = None,
             agent_max_turns: Optional[int] = None) -> dict[str, Any]:
    """THE PRE-CHECK. Read-only: runs in a transaction that is always rolled back.

    PRE_DEPLOY with a build and no `app_dir` reads the build's own saved workspace (the files its
    lineage wrote, governed_code_task_service.layered_workspace_files) into a temporary
    directory, removed afterwards.

    `families` (default: all of history, logs, data) adds the run-level checks that read BOSS's
    recent behaviour (workflow_precheck_behaviour). The report always lists the auto-fixes BOSS
    could make (`fixes`, dry run) and the shortest path per will_fail (`explain`); `fix=True`
    applies the auto-fixes AFTER the read-only pass, each in its own transaction, recorded as
    `precheck.autofix_applied` by "boss precheck".

    `agent=True` then runs the MODEL PRE-CHECK (workflow_precheck_agent) over this report: its
    predictions land in `report["model"]`, counted in `summary["model"]`, explained after the
    coded items, never in `blocking`. Capped by `agent_max_usd` / `agent_max_turns` (else the
    project's settings)."""
    from . import workflow_precheck_behaviour as behaviour

    if families is None:
        families = behaviour.FAMILIES
    started = time.monotonic()
    scratch: Optional[tempfile.TemporaryDirectory] = None
    with engine.connect() as connection:
        tx = connection.begin()
        try:
            ctx = build_context(connection, settings, project_id=project_id, actor=actor,
                                workflow=workflow, version_id=version_id, run_id=run_id,
                                run_input=run_input, checkpoint=checkpoint, app_dir=app_dir,
                                repo_root=repo_root, build_run_id=build_run_id, offline=offline,
                                families=families, history_days=history_days)
            if ctx.checkpoint == PRE_DEPLOY and ctx.app_dir is None and ctx.build_run_id:
                scratch = tempfile.TemporaryDirectory(prefix="boss-precheck-")
                ctx.app_dir = materialize_build(connection, ctx.build_run_id, Path(scratch.name))
            out = run_checks(ctx)
        finally:
            tx.rollback()
            if scratch is not None:
                scratch.cleanup()
    out["fixes"] = behaviour.apply_fixes(engine, ctx.settings, project_id, out, apply=bool(fix))
    out["explain"] = behaviour.explain(out)
    out["summary"]["fixable_by_boss"] = len(out["fixes"])
    if agent:
        from . import workflow_precheck_agent

        workflow_precheck_agent.precheck_with_model(
            engine, ctx.settings, project_id=project_id, report=out, spec=ctx.spec,
            run_input=ctx.run_input, max_usd=agent_max_usd, max_turns=agent_max_turns)
    out["summary"]["seconds"] = round(time.monotonic() - started, 2)
    return out


def materialize_build(connection: Connection, build_run_id: str, into: Path) -> Optional[Path]:
    """The build's saved workspace written under `into`; returns the app's folder (the top-level
    directory holding most of the files), or None when the build saved nothing."""
    from . import code_task_service as cts
    from .governed_code_task_service import layered_workspace_files, seed_workspace

    run = cts.load_code_task_run(connection, build_run_id)
    files = layered_workspace_files(lambda rid: cts.load_code_task_run(connection, rid), run)
    if not files:
        return None
    seed_workspace(into, files)
    tops: dict[str, int] = {}
    for f in files:
        head = str(f.get("path") or "").split("/", 1)
        if len(head) == 2:
            tops[head[0]] = tops.get(head[0], 0) + 1
    if tops:
        top = max(tops, key=tops.get)
        if tops[top] * 2 > len(files):
            return into / top
    return into


# ── At the start door ─────────────────────────────────────────────────────────────────────────

class PrecheckRefused(RuntimeError):
    """A start with will_fail checks the caller did not accept or skip. Carries the pre-check."""

    def __init__(self, report: dict[str, Any], unaccepted: list[str]):
        super().__init__(report["summary"]["sentence"])
        self.report = report
        self.unaccepted = unaccepted

    def detail(self) -> dict[str, Any]:
        return {"error": "precheck_failed",
                "message": (f"This run is expected to fail: {', '.join(self.unaccepted)}. Fix "
                            "each (its fix says how), skip its step (`skip_steps`), or start "
                            "anyway by accepting it (`accept`)."),
                "unaccepted": self.unaccepted, "precheck": self.report}


class SkipRefused(ValueError):
    pass


def gate_mode() -> str:
    value = str(os.environ.get(GATE_ENV, "report") or "report").strip().lower()
    if value in ("off", "0", "false", "no"):
        return "off"
    if value in ("enforce", "on", "1", "true", "yes"):
        return "enforce"
    return "report"


#: A readiness need the operator chose to start without (start_anyway) also accepts the
#: pre-check that says the same thing, so one choice is never asked for twice.
READINESS_ACCEPTS = {"budget": ("budget.headroom", "budget.run_cost"),
                     "repository": ("github.access",), "deployment_target": ("deploy.target",),
                     "model": ("sandbox.model",)}


def gate_start(connection: Connection, settings: Any, *, project_id: str, version_id: str,
               run_input: Mapping[str, Any], actor: str, accept: Iterable[str] = (),
               skip_steps: Iterable[Any] = (), reason: str = "",
               start_anyway: Iterable[str] = ()) -> Optional[dict[str, Any]]:
    """The fast pre-run checks at a start door. Returns the record to keep on the run (the mode,
    the counts and check ids, the full report, accepted checks, skipped steps, who and why) or
    None when there is nothing to record. In `enforce` mode raises PrecheckRefused for an
    unaccepted will_fail; in `report` mode never refuses. In both, SkipRefused for a skip that
    would bypass a gate.

    Product workflows only, like readiness: BOSS's own system workflows are not checked."""
    mode = gate_mode()
    if mode == "off" or not version_id:
        return None
    namespace = connection.execute(text(
        "SELECT COALESCE(d.namespace, 'system') FROM workflow_versions v JOIN "
        "workflow_definitions d ON d.id = v.workflow_definition_id WHERE v.id = :v"),
        {"v": version_id}).scalar()
    if namespace != "product":
        return None
    point = connection.begin_nested()
    try:
        ctx = build_context(connection, settings, project_id=project_id, actor=actor,
                            version_id=version_id, run_input=dict(run_input or {}), offline=True)
        report = run_checks(ctx, only_fast=True)
    finally:
        point.rollback()
    by_id = {str(s["id"]): s for s in _steps(ctx.spec)}
    skips: list[dict[str, str]] = []
    for item in skip_steps or ():
        sid = str(item.get("step_id") if isinstance(item, dict) else item or "").strip()
        why = str((item.get("reason") if isinstance(item, dict) else "") or reason or "").strip()
        if not sid:
            continue
        if sid not in by_id:
            raise SkipRefused(f"no step '{sid}' in this workflow")
        refused = skip_refusal(ctx.spec, by_id[sid])
        if refused:
            raise SkipRefused(refused)
        skips.append({"step_id": sid, "reason": why[:300]})
    skipped = {s["step_id"] for s in skips}
    accepted = {str(a).strip() for a in (accept or ()) if str(a).strip()}
    accepted |= {c for k in (start_anyway or ()) for c in READINESS_ACCEPTS.get(k, ())}
    unaccepted = [b for b in report["summary"]["blocking"]
                  if b.split(":", 1)[0] not in skipped and b not in accepted
                  and b.split(":", 1)[1] not in accepted]
    if unaccepted and mode == "enforce":
        raise PrecheckRefused(report, unaccepted)
    # THE MODEL PRE-CHECK, only when the project or the workflow asked for it (a model call
    # costs money and time on every start). Advisory: its (model) lines are recorded on the run
    # and never refuse a start. Run after the refusal, so a refused start spends nothing.
    from . import workflow_precheck_agent as agent

    if agent.wanted_at_the_door(connection, settings, project_id, ctx.spec):
        agent.precheck_with_model(getattr(connection, "engine", None), ctx.settings,
                                  project_id=project_id, report=report, spec=ctx.spec,
                                  run_input=ctx.run_input, connection=connection)
    model_found = bool((report.get("model") or {}).get("findings"))
    taken = [b for b in report["summary"]["blocking"] if b not in unaccepted]
    fixable = [f"{st['step_id']}:{c['id']}" for st in report.get("steps") or []
               for c in st["checks"] if c["verdict"] == "fixable"]
    if not skips and not taken and not report["summary"]["blocking"] and not fixable \
            and not model_found:
        return None
    blocking = list(report["summary"]["blocking"])
    model = report["summary"].get("model")
    return {"mode": mode, "checkpoint": PRE_RUN,
            "message": activity_line(blocking, fixable, model),
            "will_fail": blocking, "fixable": fixable, "unaccepted": unaccepted,
            "accepted": sorted(b for b in taken if b.split(":", 1)[0] not in skipped),
            "skip_steps": skips, "by": actor, "reason": reason[:300], "report": report}


def activity_line(will_fail: Sequence[str], fixable: Sequence[str],
                  model: Optional[Mapping[str, int]] = None) -> str:
    """The run's first activity entry: "Pre-check: 2 will_fail, 1 fixable — a:x, b:y; c:z".
    With the model pre-check's counts: "...; model: will_fail 1, risk 2"."""
    head = f"Pre-check: {len(will_fail)} will_fail, {len(fixable)} fixable"
    if model:
        head += f" (model: will_fail {model.get('will_fail', 0)}, risk {model.get('risk', 0)})"
    parts = [", ".join(will_fail), ", ".join(fixable)]
    tail = "; ".join(p for p in parts if p)
    return (head + (f" — {tail}" if tail else ""))[:1000]


def record_on_run(connection: Connection, project_id: str, actor: str, run_id: str,
                  record: Optional[Mapping[str, Any]]) -> None:
    """Put the pre-check on the run's history: `workflow.precheck_reported` (the activity line,
    the counts, the full report) and, when the operator accepted or skipped anything,
    `workflow.precheck_accepted` with who and why."""
    if not record or not run_id:
        return
    from .core import emit_event

    emit_event(connection, project_id, "workflow.precheck_reported", actor, "workflow_run",
               run_id, {k: record.get(k) for k in ("message", "mode", "checkpoint", "will_fail",
                                                   "fixable", "unaccepted", "report")})
    if record.get("accepted") or record.get("skip_steps"):
        emit_event(connection, project_id, "workflow.precheck_accepted", actor, "workflow_run",
                   run_id, {k: record.get(k) for k in ("accepted", "skip_steps", "by", "reason",
                                                       "mode")})


def app_env_on_tree(connection: Connection, settings: Any, *, project_id: str,
                    app_key: Optional[str], app_dir: Path) -> dict[str, Any]:
    """The pre-check's app_env checks on a build's tree (build, preview, deploy), run INSIDE the
    build right after implement and before the app-start proof, so the run learns what the app
    requires and whether anyone may give it before the proof fails on it."""
    ctx = Context(connection=connection, settings=settings, project_id=project_id,
                  actor="boss", spec={"steps": []},
                  run_input={"app_key": app_key} if app_key else {}, checkpoint=PRE_DEPLOY,
                  app_dir=Path(app_dir), offline=True)
    checks = []
    for capability in ("build", "preview", "deploy"):
        found = check_app_env(ctx, {"id": capability, "kind": "capability",
                                    "binding": {"capability": capability}})
        if found:
            checks.append(found)
    will_fail = [c["id"] for c in checks if c["verdict"] == "will_fail"]
    fixable = [c["id"] for c in checks if c["verdict"] == "fixable"]
    return {"checkpoint": "in_build", "app_dir": Path(app_dir).name,
            "message": activity_line(will_fail, fixable), "will_fail": will_fail,
            "fixable": fixable, "checks": checks}


def skipped_by_precheck(context: Mapping[str, Any], step_id: str) -> Optional[dict[str, Any]]:
    """The skip recorded at start for this step, if any (read by the capability runner)."""
    data = (context or {}).get("input") if isinstance(context, Mapping) else None
    record = (data or {}).get(RECORD_KEY) if isinstance(data, Mapping) else None
    for item in (record or {}).get("skip_steps") or []:
        if isinstance(item, Mapping) and str(item.get("step_id")) == str(step_id):
            return {"by": record.get("by"), "reason": item.get("reason") or record.get("reason")}
    return None


# ── What each check means (one line each; test_every_check_id_has_a_doc_line) ─────────────────

CHECK_DOCS: dict[str, str] = {
    "authority.consent": "the run's consent and the agent's Passport cover every tool the step "
                         "calls, including the ones its runner calls itself",
    "github.access": "the repository binding's last probes and a live GET: read, push, the "
                     "default branch",
    "github.ci": "the repository's CI covers the app's path and has the services it needs",
    "github.own_link": "the person starting the run has their own GitHub linked with push "
                       "access to the run's repository, as the consent at Start checks",
    "sandbox.worker": "a worker is alive to lease the step's job",
    "sandbox.toolchain": "node, npm, git and a C toolchain where builds run; the app's engines",
    "sandbox.build_services": "the build-services database is up for a Postgres app's build",
    "sandbox.disk": "free disk where sandboxes are written",
    "sandbox.model": "the engine/model/source each step routes to (job pins, the router), that "
                     "source's connection state, and its lane's queue",
    "sandbox.engine": "the routed engine's declared runtime binaries on PATH; live, one tiny "
                      "sandboxed session per engine that must run one command",
    "budget.headroom": "the period's cap holds the step's reservation beside other work",
    "budget.run_cost": "the whole run's cost, estimated from past runs' recorded tokens priced "
                       "at today's routes (subscriptions $0), against what is left of the day's "
                       "and month's caps, and its spend rate against the burst cap",
    "app_env.build": "every variable the app requires has a build value BOSS gives or may take",
    "app_env.preview": "every variable the app requires has a preview value",
    "app_env.deploy": "every variable the app requires has a deploy value",
    "app.test_plan": "the gate's test plan runs the app's own tests and leaves package.json alone",
    "app.dependencies": "npm resolves the dependency tree (dry run in a copy)",
    "app.secret_scan": "the committed-secrets gate passes on the app's files",
    "preview.serves_build": "the preview command builds the directory the server serves",
    "deploy.manifest": "the app's own boss.deploy.yaml exists and parses",
    "deploy.database": "a Postgres app gets a database where it is deployed",
    "deploy.health": "the declared health path is a route the app serves (else the app's own "
                     "health-like routes)",
    "deploy.target": "the deploy's host, target, path and port; ports held by targets that "
                     "never deployed",
    "smoke.contract": "smoke can read every deployment kind the deploy step finishes with",
    "code.start_command": "the `start` script runs a file in the tree or one the build emits",
    "code.prod_refusal": "no start-up error under NODE_ENV=production refuses a value BOSS binds "
                         "for deploy",
    "code.lockfile": "package-lock.json records what package.json declares (regenerated in a "
                     "throwaway copy to report the diff)",
    "code.browser_cache": "the Playwright browser the app's tests need is in the shared cache",
    "lifecycle.branch_failure": "a failing branch of an all-join stops the run and ends its grant",
    "lifecycle.advisory_start": "an advisory step that fails to START stops the run",
    "lifecycle.concurrent_push": "a branch that pushes to the run's branch runs beside one that "
                                 "ships it (pass 2 vs pass 1's deploy)",
    "history.recurring_failures": "failure fingerprints that recur per step/gate over recent runs "
                                  "of this app, and whether they still occur on the running BOSS",
    "history.review_panel": "review_degraded events, seats near the panel deadline, serial "
                            "panels, re-reviews past the deadline",
    "history.warm_rounds": "warm rounds that left the same gates failing; failed builds that get "
                           "no warm round on the running BOSS",
    "history.step_up": "calls refused step_up_required, approvals among them and how long the "
                       "gate then waited",
    "history.retry_refused": "automatic retries blocked \"No one's authority covers this build\"",
    "history.noise": "per-day counts of shepherd/captain/duplicate events against a budget; "
                     "repeated shepherd retries",
    "history.restarts": "API/worker starts (app_logs) inside this app's failed builds; drains "
                        "that interrupted builds; builds killed by the platform",
    "history.grant_ended_live": "grants that ended while a job under them was still live",
    "history.idle_in_transaction": "connections idle inside a transaction (pg_stat_activity, "
                                   "sampled) and whether anything bounds them",
    "logs.crash_blame": "the app's real start logs through BOSS's crash-to-variable matcher: "
                        "does the explanation name what the app named",
    "logs.app_start": "the last failed start's cause is still unaddressed (the named variable "
                      "still unbound for build)",
    "logs.swallowed_errors": "programming errors (KeyError, AttributeError, ...) a broad except "
                             "caught and logged past, from BOSS's own log",
    "data.split_app_keys": "the app's secrets are under the key the run uses, not an older key "
                           "of the same app",
    "data.generated_secrets": "throwaway keys BOSS generates exist for deploy",
    "data.stale_targets": "targets that never deployed do not hold host ports",
    "data.leaked_leases": "no budget reservation, model-gateway or branch lease is live past "
                          "expiry",
    "data.dead_run_decisions": "no pending decision belongs to a run that is over",
}


__all__ = [
    "CAPABILITY_PRECHECKS", "CHECKS", "CHECKPOINTS", "CapabilityPrecheck", "Context",
    "NO_CHECKS", "PrecheckError", "activity_line", "app_env_on_tree", "gate_mode", "PrecheckRefused", "RECORD_KEY", "SkipRefused",
    "CHECK_DOCS", "binding_refusal", "build_context", "code_env_requirements", "gate_start", "precheck",
    "precheck_for", "record_on_run", "run_checks", "skip_refusal", "skipped_by_precheck",
    "step_permissions", "step_reservation",
]

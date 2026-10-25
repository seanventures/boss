"""What EVERY agent holds and knows about BOSS itself, whatever its role.

WHY THIS EXISTS
---------------
An agent's tools are exactly what it was given (`role_skills`, agent_steps.holdings), and its
role spec names only its specialism: Harbor held `deployment.health.check` and
`deployment.run.plan`, Forge held the coding skills, the panel voices held a text model. Not
one of the 32 agents on the laptop install held a way to read a run's log, explain why a build
failed, look at the schema, list what BOSS can do or propose a fix — so an agent asked "why did
this build fail" answered from its prompt, and its prompt told it "there are no other tools —
no shell, no database". The operator's standing rule: every BOSS agent knows BOSS's
fundamentals and is never blind; role skills come on top.

THE BASELINE IS A SET OF SKILLS, GRANTED LIKE ANY OTHER
-------------------------------------------------------
Giving a skill to an agent is the grant (skill_runtime). The baseline is three skills, written
to `role_skills` for every agent by `sync_role_skills` (every boot, every project, custom agents
included) and at creation (`agents.create`). Nothing widens a skill's `allowed_callers`.

* `boss.fundamentals` — BOSS's own read tools: run logs and activity, diagnosis of a run, infra
  and health, the schema and a project-fenced row read, the catalogue of skills, harnesses and
  workflows, the pre-check, and memory. Every tool is a declared READ and every result is
  redacted (`redact_for_agent`) before the model sees it.
* `repo.read` — the project's repository: files, search, history, pull requests.
* `boss.propose_fix` — `propose_action`: the agent PROPOSES a governed action; a person
  approves it. In chat it is the button the operator presses; in an agent run it is a hold-
  default decision whose approval runs the action under the approver's authority. The agent
  never runs it, and cannot raise its own authority by proposing.

An operator may still take one off an agent (unassign): that removal is recorded in
seed_exclusions and the sync leaves it off, like every other grant.

Each call still goes through the skill runtime (grant, budget, invocation record), and inside a
delegated run each tool is checked against the run token with the tool's OWN permission, so a
Passport that does not hold it refuses it honestly rather than the bundle hiding the reach.
"""
from __future__ import annotations

import re
from typing import Any

#: The skills every agent holds. `test_every_agent_holds_the_baseline` fails on a role without.
#:
#: EVERY SHIPPED AGENT HOLDS THE SAME BASE (operator, 2026-10-23): the same expertise and the same
#: action and tool access through the one agent pipeline. Agents differ only by the skills and MCP
#: servers a person assigns (role_skills / role_mcp_servers) or a customised agent, never by a code
#: branch on a role name. `test_every_shipped_agent_has_the_same_base` enforces it.
BASELINE_SKILLS: tuple[str, ...] = ("boss.fundamentals", "repo.read", "boss.propose_fix",
                                    "knowledge.read", "knowledge.propose")

#: The engine read tools `boss.fundamentals` offers, by name — its spec's policy.tools, and
#: nothing else. Every one must be a `read` tool in the registry (the enforcing test checks).
#: A tool not registered on this build (the pre-check lands with feat/precheck-v2) is skipped
#: when the bundle is offered, never invented.
FUNDAMENTALS_TOOLS: tuple[str, ...] = (
    # runs and logs
    "read_logs", "get_run_activity", "read_preview_log", "list_code_tasks", "get_code_task",
    "describe_workflow_run",
    # diagnose and explain a run
    "diagnose_run", "explain_run", "read_run_turns",
    # infra and health
    "inspect_self", "system_health", "diagnose_setup", "list_running_work",
    "consistency_report", "project_status", "list_projects", "describe_project",
    # the code graph of the project's repositories: is it indexed, and index it now
    "code_graph_status", "index_repository_code",
    # a knowledge-graph project's setup: its steps, team and next action (BOSSAPP-768)
    "knowledge_project_setup",
    # waiting for BOSS's own work (a job, a build, a run, a code index) without a model call per look
    "wait_for",
    "list_org_repositories",
    "list_deployments", "describe_deployment_target",
    "list_deployment_hosts", "describe_deployment_host",
    # the database: its shape, and this project's rows (never raw SQL)
    "describe_schema", "query_project_data",
    # what BOSS can do
    "list_capabilities", "list_skills", "list_harnesses", "describe_workflow", "describe_role",
    "list_decisions",
    # the pre-check
    "precheck_workflow",
    # memory
    "recall", "recall_role_experience",
    # a gap in BOSS itself, diagnosed: written down for BOSS to fix itself (BOSSAPP-571). The one
    # write here: it only queues for a person (FUNDAMENTALS_WRITES).
    "file_platform_gap",
)

#: The writes `boss.fundamentals` may offer, by name: each one a safe write that only queues
#: something for a person (ToolSpec.queues_for_review). Every other fundamental is a read.
FUNDAMENTALS_WRITES: tuple[str, ...] = ("file_platform_gap",)

#: Tools the bundle names that may not be registered on every build. Everything else it names
#: must exist, or the enforcing test fails.
OPTIONAL_TOOLS: frozenset[str] = frozenset({"precheck_workflow"})

#: `boss.propose_fix` offers exactly this.
PROPOSE_TOOLS: tuple[str, ...] = ("propose_action",)

#: `knowledge.read` offers the knowledge graph (BOSSAPP-622): where code is, who depends on it, what
#: a change can break, and the history behind it. Every role holds it; what a stage may read of
#: it is the stage's knowledge scope (services/knowledge/scopes.py).
KNOWLEDGE_TOOLS: tuple[str, ...] = ("kg_symbol", "kg_callers", "kg_blast_radius", "kg_why",
                                     "kg_search", "kg_precedent", "kg_cypher", "kg_schema",
                                     # the approved business rules that govern code (BOSSAPP-768)
                                     "kg_rules")

#: `knowledge.propose` offers `kg_propose_fact`: the one bundle that writes. What it writes is
#: always `inferred` (quarantined, forgettable, never above governed or approved), so it is
#: tier 0. It is in the baseline: it was granted to six named roles (2026-10-21), and a shipped
#: agent is not handicapped by a role list in code (operator, 2026-10-23). The governance on
#: what it writes is the same for every agent.
PROPOSE_FACT_TOOLS: tuple[str, ...] = ("kg_propose_fact",
                                       # a business rule found in code, proposed for a person to
                                       # approve; the agent can never approve it (BOSSAPP-768)
                                       "kg_propose_rule",
                                       # what the agent learned while working, recorded the moment
                                       # it learns it, inferred and forgettable (BOSSAPP-768 §4.1)
                                       "record_learning")

#: Bundles allowed to offer a write tool, and which ones: {bundle: its writes}. Each such tool must
#: declare reversibility "safe"; any other tool a bundle names must be a read.
BUNDLE_WRITES: dict[str, tuple[str, ...]] = {"knowledge.propose": PROPOSE_FACT_TOOLS,
                                             "boss.fundamentals": FUNDAMENTALS_WRITES}
WRITE_BUNDLES: frozenset[str] = frozenset(BUNDLE_WRITES)


def may_write(skill_id: str, tool: str) -> bool:
    """May this bundle offer `tool` as a write?"""
    return tool in BUNDLE_WRITES.get(skill_id, ())

#: The input naming which of a bundle's tools a call is (agent_loop sets it). repo.read predates
#: the bundles and keeps `tool`.
BUNDLE_TOOL_KEY = "bundle_tool"


def bundles() -> dict[str, tuple[str, ...]]:
    """{skill id: the engine tools it offers by name} — every skill that is a bundle of BOSS's own
    tools rather than an executor of its own. agent_loop offers each tool under its own name."""
    from .skill_executors import REPO_READ_TOOLS

    return {"repo.read": REPO_READ_TOOLS, "boss.fundamentals": FUNDAMENTALS_TOOLS,
            "boss.propose_fix": PROPOSE_TOOLS, "knowledge.read": KNOWLEDGE_TOOLS,
            "knowledge.propose": PROPOSE_FACT_TOOLS}


def bundle_tool_spec(skill_id: str, tool: str, *, edition: str | None = None) -> Any:
    """The registered spec a bundle offers as `tool`, or None. Reads come from the chat agent's
    project-pinned registry; a write bundle's tool from the default registry, admitted only when
    it declares itself safe to reverse."""
    from ..tools.builtin import build_default_registry, build_discuss_read_registry

    registry = build_discuss_read_registry(edition=edition)
    if tool in registry.names():
        return registry.get(tool)
    if may_write(skill_id, tool):
        full = build_default_registry()
        if tool in full.names() and getattr(full.get(tool), "reversibility", None) == "safe":
            return full.get(tool)
    return None


# ── THE PRIMER ────────────────────────────────────────────────────────────────────────────────
#
# STATIC TEXT, ONE COPY. It goes at the front of an agent's system context, before anything
# that varies (who the agent is, the task), so every agent and every stage shares it as one
# cached prompt prefix (code_agent.SHARED_CONTEXT_START, 096e73e7). Nothing here may be
# formatted with a run, a role or a date.

PRIMER = (
    "=== HOW BOSS WORKS (every BOSS agent knows this) ===\n"
    "BOSS is a governed AI-labour control plane. Work is a RUN: a workflow run (wfr_...) is a "
    "sequence of STEPS; a build step starts a code task run (crun_...) that goes through stages "
    "(design, implement, tests, reviews, gates) in a sandbox and ends in a pull request, a "
    "preview or a deploy. Every effect an agent has is an ACT recorded against its run.\n"
    "WHERE THINGS LIVE: run state, errors and stage history in Postgres (code_task_runs, "
    "workflow_runs, events, agent_turns, decisions, app_logs); service logs in app_logs; the "
    "app's code in the project's repository; deployments on registered hosts.\n"
    "TO DIAGNOSE, use the BOSS tools you hold before you answer: diagnose_run / explain_run / "
    "get_run_activity / read_run_turns for a run; read_logs and read_preview_log for logs; "
    "read_code / search_code / git_history for the code; describe_schema then "
    "query_project_data for rows; inspect_self / system_health / diagnose_setup / "
    "list_running_work / consistency_report for BOSS's own health; list_capabilities / "
    "list_skills / describe_workflow for what BOSS can do; recall for what this project "
    "already learned. Quote the error line, the file, the value you read.\n"
    "TO WAIT for work that is running (a job, a build, a workflow run, a code index), call "
    "wait_for once: it blocks until the work settles, without a model call per look. Do not "
    "read a status over and over.\n"
    "NOW, NOT THEN: earlier replies in a conversation, yours included, are history, not facts. "
    "A tool that failed or was refused in an earlier turn may have been fixed or granted since; "
    "the tools you are given now are what you hold, so call one instead of repeating an "
    "earlier failure. Read state again (a status, a count, a setting) before you report it, and "
    "check what a tool offers now (a schema or a describe tool) before reusing an earlier query "
    "or approach.\n"
    "LEARN AS YOU WORK, AND USE WHAT OTHERS LEARNED: before you act on code, read what governs it "
    "and what earlier runs learned about it — the Rules in scope and Learned before you are given, "
    "kg_rules for the approved rules of the code you touch, kg_precedent for past causes and fixes "
    "— and when one of them shapes what you do, cite it by its tag ([R:...] for a rule, [L:...] for "
    "a learning) in your answer or summary. An approved rule is trusted; a learning is another "
    "agent's inference, so check it against what you see. The moment you learn something the next "
    "agent would otherwise have to learn again — a hypothesis you ruled out and why, a cause you "
    "found, a failing test and what made it pass, a build or deploy problem and what worked "
    "instead, a choice and its reasons, an approved rule you found wrong or incomplete — record it "
    "with record_learning (or kg_propose_rule for a rule), with its evidence and the code it is "
    "about. One record per thing learned, said plainly; never a secret, and never what you merely "
    "assumed.\n"
    "TO FIX, propose — never improvise: name the cause, then call propose_action with the BOSS "
    "action that fixes it (retry, redeploy, set a setting...) and exact arguments. A person or "
    "policy approves; you do not run it, raise your own authority, or ask anyone to paste a "
    "secret. When the fix is a value only a person holds (a missing secret, an account), say "
    "exactly which value and where in BOSS it is set.\n"
    "=== END HOW BOSS WORKS ===\n"
)

#: For a coding harness session (a build stage): BOSS's model of the work, without tools the
#: harness does not have there. Static, and first in the shared TASK CONTEXT block.
BUILD_PRIMER = (
    "HOW BOSS WORKS: you are one stage of a BOSS code task run (crun_...): design, implement, "
    "tests, reviews and gates run in this sandbox, and the run ends in a pull request, a preview "
    "or a deploy. BOSS records every stage, log and gate result against the run; a gate that "
    "fails names the check. When something outside the repository blocks you — a secret only a "
    "person holds, an account, a host — do not work around it: say exactly what is missing and "
    "where in BOSS it is set, and stop. When the task context quotes rules or what earlier runs "
    "learned and one of them shapes your change, name it by its [R:...] or [L:...] tag in your "
    "summary.\n\n"
)


# ── REDACTION FOR AN AGENT ────────────────────────────────────────────────────────────────────

_WORDS = re.compile(r"[^a-z0-9]+")
#: "code" is a credential word to the log filter (OAuth `?code=`), and a file's contents to an
#: agent reading the repository. Here it is the second.
_CODE_WORDS = frozenset({"code", "codes"})
#: Strings longer than this are cut: a tool result is read by a model, not archived.
_MAX_STRING = 8000


def _sensitive_key(key: Any) -> bool:
    from ..identity.redaction import is_sensitive_key

    if not is_sensitive_key(key):
        return False
    words = [w for w in _WORDS.split(str(key).lower()) if w]
    rest = [w for w in words if w not in _CODE_WORDS]
    if len(rest) == len(words):
        return True
    return bool(rest) and is_sensitive_key("_".join(rest))


#: An environment variable's NAME after a credential word: "needs a secret: DEMO_PASSWORD",
#: "Set API_KEY". Both redactors read `secret: X` as a credential and masked the name — and the
#: name of the missing secret IS the diagnosis (crun_0d82ab62: "the app needs a secret:
#: DEMO_PASSWORD"). Only an UPPER_SNAKE identifier with an underscore is kept; a value of any
#: other shape is still masked.
_ENV_NAME_AFTER_WORD = re.compile(
    r"(?i:secret|password|passwd|token|key|credential)s?(?P<sep>\s*[:=]\s*)"
    r"(?P<name>[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)(?![A-Za-z0-9_])")


def _redact_text(value: str) -> str:
    from ..identity.redaction import redact_text
    from .redaction import redact_secrets

    kept: list[str] = []

    def hold(match: "re.Match[str]") -> str:
        # The whole phrase is held, credential word included: a redactor sees no `secret:`.
        kept.append(match.group(0))
        return f"\x00{len(kept) - 1}\x00"

    held = _ENV_NAME_AFTER_WORD.sub(hold, value)
    out = redact_secrets(redact_text(held))
    out = re.sub("\x00(\\d+)\x00", lambda m: kept[int(m.group(1))], out)
    if "\x00" in out:                        # a placeholder a redactor cut into: keep it masked
        out = out.replace("\x00", "")
    return out if len(out) <= _MAX_STRING else out[:_MAX_STRING] + " …[truncated]"


def redact_for_agent(value: Any, _depth: int = 0) -> Any:
    """A tool result as an agent may read it: credential-named keys dropped, every credential
    shape and every configured secret scrubbed from strings (the one redaction layer plus the
    provider-token patterns), long strings cut. Never raises: on failure nothing leaks."""
    from ..identity.redaction import REDACTED

    if _depth > 20:
        return REDACTED
    try:
        if isinstance(value, str):
            return _redact_text(value)
        if isinstance(value, dict):
            return {k: (REDACTED if _sensitive_key(k) else redact_for_agent(v, _depth + 1))
                    for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [redact_for_agent(v, _depth + 1) for v in value]
        return value
    except Exception:  # noqa: BLE001 — a redaction failure must not leak the original
        return REDACTED


# ── RUNNING A BUNDLED TOOL ────────────────────────────────────────────────────────────────────

def run_bundled_tool(context: Any, skill_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """One call of a baseline bundle skill: the named engine tool, project-pinned (the chat
    agent's registry — the project is the run's, not the model's), its result redacted."""
    from ..context import EngineContext

    offered = bundles().get(skill_id) or ()
    # WHICH TOOL OF THE BUNDLE: `bundle_tool`, never `tool` — propose_action's own `tool` argument
    # is the action being proposed, and one key for both made every proposal unreadable.
    tool = str(inputs.get(BUNDLE_TOOL_KEY) or "").strip()
    if tool not in offered:
        raise ValueError(f"{skill_id} offers {', '.join(offered)}; not {tool!r}.")
    spec = bundle_tool_spec(skill_id, tool, edition=getattr(context.settings, "boss_edition", None))
    if spec is None:
        from .skill_executors import SkillExecutorConfigurationError

        raise SkillExecutorConfigurationError(f"this BOSS has no {tool} tool.")
    if spec.kind != "read" and not may_write(skill_id, tool):
        # A BUNDLE IS READ-ONLY BY CONSTRUCTION, except a WRITE_BUNDLES one. propose_action is
        # declared a read: it records an offer and runs nothing.
        raise ValueError(f"{tool} is not a read tool; {skill_id} offers reads only.")
    # THE PROJECT IS THE RUN'S, never the model's: a write tool's schema still names project_id.
    args = {k: v for k, v in inputs.items()
            if k not in (BUNDLE_TOOL_KEY, "project_id") and not str(k).startswith("_")}
    ctx = EngineContext(settings=context.settings, engine=context.connection.engine)
    out = spec.handler(ctx, project_id=context.project_id, actor=context.actor, args=args)
    return {"tool": tool, "result": redact_for_agent(out if isinstance(out, dict)
                                                     else {"value": out})}


def propose(context: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    """`boss.propose_fix` in an agent run: validate the proposal exactly as chat does
    (propose_action — a real action, available here, no invented argument), then raise a
    HOLD-DEFAULT decision whose approval runs it as the approver (decisions →
    execute_agent_tool). Nothing runs here."""
    checked = run_bundled_tool(context, "boss.propose_fix",
                               {**inputs, BUNDLE_TOOL_KEY: "propose_action"})["result"]
    if not checked.get("ok", True) or not checked.get("proposed"):
        return {"proposed": False, **checked}
    from .core import create_blocking_decision, emit_event
    from ..tools.builtin import build_default_registry

    name = str(checked["tool"])
    caller = str(getattr(context, "caller", None) or context.actor)
    try:
        reversibility = build_default_registry().get(name).reversibility
    except Exception:  # noqa: BLE001 — unknown cost reads as the highest
        reversibility = "irreversible"
    # THE RISK FLOOR. Anything that cannot be undone, destroys work or grants authority is
    # tier 3; anything else an agent proposes is still tier 2 — a person decides either way.
    tier = 3 if reversibility in ("irreversible", "destroys_work", "grants_authority") else 2
    from .offer_label import button_label

    label = button_label(checked.get("label"), name, checked.get("args") or {})
    decision_id = create_blocking_decision(
        context.connection, context.project_id, None,
        f"Agent proposal: {label}",
        {"action": "execute_agent_tool", "tool": name, "args": checked.get("args") or {},
         "label": label, "why": str(checked.get("why") or ""),
         "proposed_by": caller, "source": "boss.propose_fix"},
        {}, "agent_proposed_fix", risk_tier=tier)
    emit_event(context.connection, context.project_id, "agent.fix_proposed",
               caller, "decision", decision_id,
               {"tool": name, "risk_tier": tier})
    return {"proposed": True, "decision_id": decision_id, "tool": name, "label": label,
            "risk_tier": tier,
            "note": ("NOT run. A person approves it on the decision; approving runs it under "
                     "their authority. Say what you proposed and why — not that it is done.")}

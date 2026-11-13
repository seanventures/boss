from __future__ import annotations

import json
import re
import time
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .tool_result_text import clip_for_model
from ..agent.transient import _with_transient_retry
from ..config import Settings
from . import owned_process
from . import decision_actions
from .argument_binding import CHANGED, bind_held_call, binding_holds
from .core import create_artifact, create_blocking_decision, emit_event, make_id
from .github_adapter import GitHubAdapter
from .low_trust import Ledger, blocked, outside_origin, remember, sink_for_skill
from .mcp_client import MCPClient
from .mcp_registry import auth_headers
from .mcp_registry import list_mcp_servers
from .role_governance import RoleGovernanceService
from .model_routing import governed, project_chat_adapter
from .openai_adapter import OpenAIAdapter, _usage_from_response  # noqa: F401
from .pdf_render import pdf_page_count, render_markdown_pdf
from .skill_executors import SkillExecutorContext
from .skill_registry import resolve_for_task
from .skill_runtime import SkillRuntimeService


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", value or "").strip("_").lower()


def _unique_tool_name(
    base: str,
    occupied: set[str],
    identity: str,
) -> str:
    candidate = base[:64]
    if candidate not in occupied:
        occupied.add(candidate)
        return candidate
    suffix = "_" + sha256(identity.encode("utf-8")).hexdigest()[:8]
    candidate = base[: 64 - len(suffix)] + suffix
    counter = 2
    while candidate in occupied:
        numbered = f"_{counter}"
        candidate = base[: 64 - len(suffix) - len(numbered)] + suffix + numbered
        counter += 1
    occupied.add(candidate)
    return candidate


#: How many refused tool calls an agent may make before the step gives up. Three is enough for
#: a model to read the parameters again; more than that is a loop, not a correction.
MAX_REFUSED_TOOL_CALLS = 3


# ============================================================
# A transient budget refusal is never the agent's problem.
# ============================================================
#
# The per-minute burst cap refuses a reservation that would be granted a few seconds later.
# Handed to the agent as a tool error, the agent read it as a broken tool and gave up: five
# "Video edit and publish" runs started together, the 'edit' step's agent called
# media.fetch_link four times into a busy minute, and declared "Edit is blocked:
# skill_media_fetch_link repeatedly denied with transient burst budget cap error" — the step
# stopped on something that clears by itself and that no person had to decide.
#
# So the call WAITS, here, in the one place agent tool calls reach skills and MCP tools, and is
# made again — every attempt goes through the governor (each one reserves; nothing is
# bypassed). Bounded per call and per run; past the bound a bounded run (a workflow's agent
# step) stops with status `budget_wait` and the WORKFLOW step waits and runs again later
# (workflows.wait_step). Only an unbounded run (chat) hands the agent a plain sentence.
# A refusal a person must lift (cap, freeze, allocation) is not transient and reaches the
# agent exactly as before.

#: How long one tool call may wait for the per-minute budget before the bound is reached.
TOOL_BUDGET_WAIT_SECONDS = 120
#: How long one drive of an agent run may spend waiting in total, across all its calls.
RUN_BUDGET_WAIT_SECONDS = 240
#: Waits are slept in slices so a deadline or a stop request is noticed promptly.
BUDGET_WAIT_SLICE_SECONDS = 5
BUDGET_BUSY_MESSAGE = "BOSS's per-minute budget is busy; try again shortly."
#: The status a bounded run ends with when it stopped to wait for the budget.
BUDGET_WAIT = "budget_wait"

# Injected by tests (no real sleeping); production reads the real clock.
_sleep: Callable[[float], None] = time.sleep
_clock: Callable[[], float] = time.monotonic


def transient_budget_refusal(result: Any) -> Optional[dict[str, Any]]:
    """The refusal's `budget` record when `result` is a skill invocation the budget refused
    for a reason that clears by itself (skill_runtime.budget_refusal), else None."""
    if not isinstance(result, dict) or str(result.get("status")) != "denied":
        return None
    budget = result.get("budget")
    if isinstance(budget, dict) and budget.get("transient"):
        return budget
    return None


class BudgetWaiter:
    """Makes a tool call again while the budget refuses it transiently, within bounds.

    `invoke(call)` returns `(result, exhausted)`: `exhausted` is None when the call got a real
    answer (served, or refused for a reason a person must lift); otherwise it is the refusal's
    budget record and `result` is the plain sentence for whoever reads it next."""

    def __init__(self, *, per_call: float = TOOL_BUDGET_WAIT_SECONDS,
                 per_run: float = RUN_BUDGET_WAIT_SECONDS, deadline: Optional[float] = None,
                 should_stop: Optional[Callable[[], bool]] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 clock: Optional[Callable[[], float]] = None):
        self.per_call = float(per_call)
        self.per_run = float(per_run)
        self.deadline = deadline
        self.should_stop = should_stop
        self.sleep = sleep or _sleep
        self.clock = clock or _clock
        self.waited = 0.0
        self.retries = 0

    def _stopping(self) -> bool:
        return bool(self.should_stop and self.should_stop())

    def _room(self, started: float) -> float:
        now = self.clock()
        room = min(self.per_call - (now - started), self.per_run - self.waited)
        if self.deadline is not None:
            room = min(room, self.deadline - now)
        return room

    def _wait(self, seconds: float) -> bool:
        """Sleep `seconds` in slices; False when told to stop part-way."""
        began = self.clock()
        end = began + seconds
        try:
            while True:
                left = end - self.clock()
                if left <= 0:
                    return True
                if self._stopping():
                    return False
                self.sleep(min(BUDGET_WAIT_SLICE_SECONDS, left))
        finally:
            self.waited += max(0.0, self.clock() - began)

    def invoke(self, call: Callable[[], dict[str, Any]]
               ) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
        started = self.clock()
        result = call()
        while True:
            budget = transient_budget_refusal(result)
            if budget is None:
                return result, None
            wait = max(1.0, float(budget.get("retry_after_seconds") or 30))
            if wait > self._room(started) or self._stopping() or not self._wait(wait):
                return {
                    "status": BUDGET_WAIT,
                    "transient": True,
                    "retry_after_seconds": int(budget.get("retry_after_seconds") or 30),
                    "message": BUDGET_BUSY_MESSAGE,
                    "skill_id": result.get("skill_id"),
                }, budget
            self.retries += 1
            result = call()


def _preview_of(result: Any) -> str:
    """The transcript's record of what a tool returned — and, when it FAILED, why.

    240 characters of a skill invocation is its id, project, skill and status, and the reason it
    failed falls off the end. The agent reads the whole thing, so it knew; the transcript is what
    a PERSON reads afterwards, and it showed a failed step with no cause. Diagnosing one read
    that could not parse a document took a separate query against another endpoint.

    A success does not need more than a preview. A failure is the one case where the detail IS
    the record, so it is kept.
    """
    encoded = json.dumps(result, ensure_ascii=False, default=str)
    if isinstance(result, dict) and str(result.get("status") or "") in ("failed", "denied",
                                                                       "error"):
        return encoded[:2000]
    return encoded[:240]


def _the_provider_refused_the_tool_call(error: Exception) -> Optional[str]:
    """Whether this failure is the model writing a bad call, rather than something being wrong.

    The two are worlds apart and arrive identically — as a 400 from the completion request. A
    provider that validates tool calls server-side (Groq does) refuses the malformed one and
    never returns a message, so the exception surfaces where no assistant turn exists yet:

        Tool call validation failed: parameters for tool skill_media_read_document did not
        match schema: errors: [missing properties: 'url']

    Treated as an outage that is a step deadlettered and work thrown away. Recognised for what
    it is, it is one turn of feedback. Anything not matching stays an exception, because a real
    outage must not be retried as though the agent could word its way out of it.
    """
    detail = str(error)
    lowered = detail.lower()
    if "tool_use_failed" in lowered or "tool call validation failed" in lowered:
        return detail[:600]
    return None


def _skill_tool_parameters(input_schema: dict[str, Any]) -> dict[str, Any]:
    if input_schema.get("type") == "object":
        return input_schema
    properties: dict[str, Any] = {}
    required: list[str] = []
    supported_types = {
        "array",
        "boolean",
        "integer",
        "number",
        "object",
        "string",
    }
    for raw_name, declaration in input_schema.items():
        name = str(raw_name).rstrip("?")
        optional = str(raw_name).endswith("?")
        if isinstance(declaration, dict):
            property_schema = dict(declaration)
            optional = optional or bool(property_schema.pop("optional", False))
        else:
            declared_type = str(declaration or "string")
            optional = optional or declared_type.endswith("?")
            declared_type = declared_type.rstrip("?")
            property_schema = {
                "type": declared_type if declared_type in supported_types else "string"
            }
        properties[name] = property_schema
        if not optional:
            required.append(name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _append_skill_tools(
    schemas: list[dict[str, Any]],
    occupied: set[str],
    resolved_skills: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    skill_map: dict[str, dict[str, Any]] = {}
    for skill in sorted(resolved_skills, key=lambda item: str(item["id"])):
        skill_id = str(skill["id"])
        name = _unique_tool_name(
            "skill_" + _slug(skill_id),
            occupied,
            f"skill:{skill_id}",
        )
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": (
                        f"[BOSS Skill · {skill.get('kind', 'unknown')}] "
                        f"{skill.get('description', '')}"
                    ),
                    "parameters": _skill_tool_parameters(
                        skill.get("input_schema") or {}
                    ),
                },
            }
        )
        skill_map[name] = skill
    return skill_map


def _skill_policy(connection: Connection, settings: Settings, project_id: str,
                  skill: dict[str, Any]) -> Optional[str]:
    """Allow / Ask / Off for one skill (kit.skill_policy), or None when nobody set one."""
    from .kit import skill_policy

    try:
        return skill_policy(connection, settings, project_id, str(skill.get("id") or ""),
                            int(skill.get("risk_tier") or 0))
    except Exception:  # noqa: BLE001 — no kit file means no operator rule, not a broken run
        return None


def _append_repo_read_tools(
    schemas: list[dict[str, Any]],
    occupied: set[str],
    spec: Optional[dict[str, Any]],
) -> dict[str, tuple[dict[str, Any], str]]:
    """Offer `repo.read`'s tools under their own names: {offered name: (skill spec, tool)}."""
    return _append_bundle_tools(schemas, occupied, [spec] if spec is not None else [])


#: How each bundle reads in the tool list, so the model knows which grant a tool came from.
_BUNDLE_LABEL = {"repo.read": "Read the repo", "boss.fundamentals": "BOSS fundamentals",
                 "boss.propose_fix": "Propose a fix", "knowledge.read": "Knowledge graph",
                 "knowledge.propose": "Propose a fact"}


def _append_bundle_tools(
    schemas: list[dict[str, Any]],
    occupied: set[str],
    specs: list[dict[str, Any]],
) -> dict[str, tuple[dict[str, Any], str]]:
    """Offer every held BUNDLE skill's tools under their own names: {offered name: (skill spec,
    tool)}. A bundle is a skill that offers BOSS's own engine tools (agent_baseline.bundles():
    repo.read, boss.fundamentals, boss.propose_fix); each call is a call of that one skill, so
    the grant, the budget gate and the invocation record are the ordinary skill runtime's. A
    tool not registered on this build is skipped, never offered and then failed."""
    from .agent_baseline import bundle_tool_spec, bundles

    from .knowledge.scopes import current_policy, offers_tool

    offered_by = bundles()
    stage_scope = current_policy()
    offered: dict[str, tuple[dict[str, Any], str]] = {}
    for spec in specs:
        skill_id = str(spec.get("id") or "")
        for tool in offered_by.get(skill_id, ()):
            if skill_id.startswith("knowledge.") and not offers_tool(stage_scope, tool):
                continue          # outside this stage's knowledge scope: not offered at all
            found = bundle_tool_spec(skill_id, tool, edition="community")
            if found is None:
                continue
            name = _unique_tool_name(tool, occupied, f"skill:{skill_id}:{tool}")
            parameters = dict(found.parameters or {"type": "object", "properties": {}})
            properties = dict(parameters.get("properties") or {})
            properties.pop("project_id", None)   # the run's project, never the model's
            if skill_id == "repo.read" and "repo" in properties:
                properties["repo"] = {"type": "string",
                                      "description": "owner/name; leave out for this project's repo"}
            parameters["properties"] = properties
            description = (f"[BOSS Skill · {_BUNDLE_LABEL.get(skill_id, skill_id)}] "
                           f"{found.description}")
            if tool == "propose_action":
                # WHAT CAN BE PROPOSED, NAMED. A live Harbor turn diagnosed a blocked build
                # correctly and then said it had "no retry_code_task tool to propose" — the
                # actions are not tools it holds, they are what `tool` may name.
                from ..tools.builtin import _DISCUSS_OPERATIONS_TOOLS

                description += (" In an agent run the proposal becomes a decision a person "
                                "approves; it runs as them. `tool` may name: "
                                + ", ".join(sorted(_DISCUSS_OPERATIONS_TOOLS)) + ".")
            schemas.append({"type": "function", "function": {
                "name": name, "description": description, "parameters": parameters}})
            offered[name] = (spec, tool)
    return offered


def _bundle_tool_permission(skill_id: str, tool: str) -> str:
    """The permission a bundled tool call is checked against inside a delegated run: the TOOL's
    own declared permission, so a Passport that does not hold it refuses it honestly. repo.read
    keeps the one permission it has always been checked against."""
    if skill_id == "repo.read":
        return "repository:read"
    from ..tools.builtin import build_discuss_read_registry

    try:
        return str(build_discuss_read_registry(edition="community").get(tool).permission
                   or "skill:invoke")
    except Exception:  # noqa: BLE001 — an unknown tool is refused by the gate as undeclared
        return "skill:invoke"


def _mcp_tool_policy(server: dict[str, Any], tool: dict[str, Any]) -> str:
    """Allow / Ask / Off for one offered tool: mcp_registry.tool_policy, the one statement."""
    from .mcp_registry import tool_policy

    annotations = tool.get("annotations") or {}
    return tool_policy(server, str(tool.get("name") or ""),
                       declared=tool.get("risk_tier", annotations.get("risk_tier")
                                         if isinstance(annotations, dict) else None))


def _mcp_tool_risk(server: dict[str, Any], tool: dict[str, Any]) -> int:
    # mcp_registry.tool_tier is the one statement of this rule. This had its own copy, which let
    # a tool's self-declared tier REPLACE the operator's blanket tier for the server — so the
    # same tool could need an approval in a workflow step and not in a chat.
    from .mcp_registry import tool_tier

    annotations = tool.get("annotations") or {}
    return tool_tier(server, str(tool.get("name") or ""),
                     declared=tool.get("risk_tier", annotations.get("risk_tier")))


def _mcp_skill_spec(
    server: dict[str, Any], tool: dict[str, Any]
) -> dict[str, Any]:
    tool_name = str(tool.get("name") or "tool")
    return {
        "id": f"mcp.{server['id']}.{_slug(tool_name)}",
        "kind": "mcp",
        "system": False,
        "load": "eager",
        "risk_tier": _mcp_tool_risk(server, tool),
        "description": (
            f"{server.get('name', 'MCP')} / {tool_name}: "
            f"{tool.get('description', '')}"
        ),
        "mock_mode": False,
        "input_schema": tool.get("inputSchema")
        or {"type": "object", "properties": {}},
        "output_schema": {"type": "object", "properties": {}},
        "cost": {"estimator": "fixed", "category": "tool"},
        "retry": {"max_attempts": 2, "backoff": "exponential"},
        "permissions": {
            # The customer workflow runner calls MCP tools through this same governed path, so
            # it is named here. This spec is synthesized by BOSS, not supplied by a tenant —
            # widening it does not widen anything a customer controls.
            "allowed_callers": ["agent_loop", "workflow_runner"],
            "required_permissions": ["mcp_call"],
        },
        "data_boundary": server.get("data_boundary") or "internal",
        # WHICH SERVER AND TOOL this is, for the runtime's permission gate: holding the server
        # (role_mcp_servers) is how an agent holds its tools — the one MCP ownership model.
        "policy": {"requires_receipt": True, "mcp_server_id": str(server["id"]),
                   "mcp_tool": tool_name},
        "publisher": server.get("publisher") or {},
        "provenance": server.get("provenance") or {},
    }


def _granted_mcp_servers(
    connection: Connection, project_id: str, step: dict[str, Any],
) -> list[dict[str, Any]]:
    """The MCP servers a bounded agent run may use: what its agent holds, as pinned at start."""
    from .agents import held_mcp_servers

    role = str(step.get("role_key") or "").strip()
    if not role:
        return []
    servers = held_mcp_servers(connection, project_id, role)
    pinned = step.get("mcp_server_ids")
    if isinstance(pinned, list):
        # Pinned at the moment the step started. A grant revoked mid-run stops being offered
        # on resume; one added mid-run is not picked up by a run already under way.
        servers = [s for s in servers if str(s["id"]) in {str(i) for i in pinned}]
    return servers


def _invoke_mcp_skill(
    connection: Connection,
    settings: Settings,
    project_id: str,
    server: dict[str, Any],
    tool: dict[str, Any],
    args: dict[str, Any],
    *,
    run_id: str,
    step: int,
    actor: str,
    caller: str = "agent_loop",
    approved_by: Optional[str] = None,
    approval_about: Optional[dict[str, Any]] = None,
    mcp_reach: Optional[str] = None,
) -> dict[str, Any]:
    tool_name = str(tool.get("name") or "tool")
    skill_spec = _mcp_skill_spec(server, tool)
    if mcp_reach is None and caller == "agent_loop":
        # THE AGENT LOOP CALLS ONLY WHAT IT OFFERED: a bounded run is offered the servers its
        # agent holds (_granted_mcp_servers), an unbounded run is the operator's own. Every other
        # caller — a custom step's agent, the workflow — must hold the server, which the runtime
        # gate checks (agents.SERVERLESS_MCP_GRANTS).
        mcp_reach = "agent_loop_offer"
    context = SkillExecutorContext(
        settings=settings,
        connection=connection,
        project_id=project_id,
        actor=actor,
        approved_by=approved_by,
        approval_about=approval_about,
        mcp_reach=mcp_reach,
    )

    def execute(inputs: dict[str, Any]) -> dict[str, Any]:
        # THE EFFECT is shared with the authored `mcp_tool` skill executor
        # (skill_executors.execute_mcp_tool), so a tool reached directly and one reached through
        # a skill leave the same receipt, are deduplicated the same way, and both tell the
        # registry when the server did not answer.
        from .mcp_registry import call_tool_with_receipt

        args_digest = sha256(
            json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:20]
        return call_tool_with_receipt(
            connection,
            settings,
            project_id,
            server,
            tool_name,
            inputs,
            dedupe_key=f"mcp:{run_id}:{step}:{server['id']}:{tool_name}:{args_digest}",
            tenant_scope=context.tenant_scope(),
            approved_by=approved_by,
            approval_about=approval_about,
        )

    return SkillRuntimeService(settings).invoke(
        connection,
        project_id,
        str(skill_spec["id"]),
        caller,
        args,
        mock=False,
        executor=execute,
        executor_context=context,
        skill_spec=skill_spec,
        actor=actor,
    )

# ============================================================
# Read tools — internal/local data only (no external side effects).
# Each returns a JSON-serializable dict that is fed back to the LLM.
# ============================================================

_SENSITIVE = (".env", ".git", "id_rsa", ".pem", ".key", "secret", "credential")
_SENSITIVE_REPO_NAMES = frozenset({
    ".npmrc", ".pypirc", ".netrc", ".yarnrc", ".yarnrc.yml",
    ".aws", ".ssh", ".gnupg", ".kube", ".docker", ".azure",
    "id_ed25519", "id_ecdsa", "id_dsa", "pip.conf", "auth.json",
})


def _repo_root(settings: Settings) -> Path:
    return Path(settings.agent_repo_root or ".").resolve()


def _sensitive_repo_path(path: Path, root: Path) -> bool:
    """Deny credential-bearing path components before listing or reading a repo file."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(
        part.lower() in _SENSITIVE_REPO_NAMES
        or any(token in part.lower() for token in _SENSITIVE)
        for part in parts
    )


def _safe_path(settings: Settings, rel: str) -> Optional[Path]:
    root = _repo_root(settings)
    try:
        target = (root / rel).resolve()
    except Exception:
        return None
    if root not in target.parents and target != root:
        return None
    if _sensitive_repo_path(target, root):
        return None
    return target


def _tool_budget_and_costs(connection: Connection, project_id: str, settings: Settings, args: dict) -> dict:
    policy = connection.execute(
        text(
            "SELECT daily_cap_usd_micros FROM budget_policies "
            "WHERE scope_type='project' AND scope_id=:p ORDER BY created_at DESC LIMIT 1"
        ),
        {"p": project_id},
    ).mappings().first()
    from .included_spend import INCLUDED_SQL, MONEY_SQL

    spent = connection.execute(
        text(
            f"SELECT COALESCE(SUM({MONEY_SQL}),0) AS s "
            "FROM cost_receipts WHERE project_id=:p"
        ),
        {"p": project_id},
    ).scalar() or 0
    by_model = connection.execute(
        text(
            "SELECT skill_or_model_id AS model, count(*) AS calls, "
            f"COALESCE(SUM({MONEY_SQL}),0) AS micros, "
            f"COALESCE(SUM({INCLUDED_SQL}),0) AS included "
            "FROM cost_receipts WHERE project_id=:p GROUP BY skill_or_model_id ORDER BY micros DESC"
        ),
        {"p": project_id},
    ).mappings().all()
    infra = connection.execute(
        text("SELECT provider, status, count(*) AS n FROM infra_resources WHERE project_id=:p GROUP BY provider, status"),
        {"p": project_id},
    ).mappings().all()
    cap = int(policy["daily_cap_usd_micros"]) if policy else 0
    # The daily cap is measured against TODAY's money (budget.period_spend), not the total.
    from .budget import period_spend

    today = period_spend(connection, project_id, "day")
    return {
        "daily_budget_cap_usd": round(cap / 1e6, 2),
        "spent_today_usd": round(today["actual_usd_micros"] / 1e6, 4),
        "reserved_today_usd": round(today["reserved_usd_micros"] / 1e6, 4),
        "daily_cap_resets_at": today["resets_at_label"],
        "total_spend_usd": round(int(spent) / 1e6, 4),
        "spend_by_model": [
            {"model": r["model"], "calls": int(r["calls"]), "usd": round(int(r["micros"]) / 1e6, 4),
             # What a subscription answered, at list price: included, not money spent.
             **({"included_in_subscription_usd": round(int(r["included"]) / 1e6, 4)}
                if int(r["included"] or 0) else {})}
            for r in by_model
        ],
        "provisioned_infrastructure": [
            {"provider": r["provider"], "status": r["status"], "count": int(r["n"])} for r in infra
        ]
        or "none provisioned",
        "note": "These are BOSS's real recorded costs. No external cloud-provider bill is connected yet.",
    }


# --------------------------------------------------------------------------------------- #
# A shell, in the app's own code.
#
# `read_repo_file` answers "what is in this file" and nothing else; the next question always
# needed the next tool. A shell answers the questions nobody enumerated: grep the tree, diff
# two files, run the app's checks, read its log. The workspace is a disposable shallow clone of
# the run's branch with a tokenless remote. The clone is disposable but does not isolate
# `sh -lc` from the host filesystem or network; Community leaves this legacy tool unavailable.
# --------------------------------------------------------------------------------------- #


def _run_repo_and_branch(
    connection: Connection, run_id: str, project_id: str,
) -> Optional[dict]:
    """Where this run's code lives. The branch rule matches work_branch_for: a retry or
    follow-up shares the ROOT's branch, so the shell lands in the app, not in an empty one."""
    row = connection.execute(
        text("SELECT id, spec, finalize_ctx FROM code_task_runs "
             "WHERE id = :i AND project_id = :p"),
        {"i": run_id, "p": project_id},
    ).mappings().first()
    if not row:
        return None
    spec = row["spec"] or {}
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError:
            spec = {}
    fctx = row["finalize_ctx"] or {}
    if isinstance(fctx, str):
        try:
            fctx = json.loads(fctx)
        except ValueError:
            fctx = {}
    root = fctx.get("root_run_id") or row["id"]
    return {
        "owner": spec.get("owner"),
        "repo": spec.get("repo"),
        "branch": f"boss/code/{root}",
        "run_id": row["id"],
    }


def _open_workspace(
    connection: Connection, settings: Settings, run_id: str, project_id: str, force: bool = False,
):
    from .agent_shell import ensure_workspace

    target = _run_repo_and_branch(connection, run_id, project_id)
    if not target or not target.get("owner") or not target.get("repo"):
        return None, f"no repo recorded for run {run_id}"
    # NOT settings.github_token — see github_connections.github_token_for. It is empty wherever
    # the repository was connected through the wizard, which makes this an anonymous clone of a
    # private repo and fails with git asking for a username on a terminal nobody is at.
    from .github_connections import github_token_for

    project_id = connection.execute(
        text("SELECT project_id FROM code_task_runs WHERE id = :i"), {"i": run_id},
    ).scalar() or ""
    # The agent's shell works in this checkout, so no token may ever be in it (IDENTITY.md
    # §7.10): through egress under the run's delegated principal, else the token in git's
    # environment only.
    from boss_engine.identity import runs as _identity_runs

    with _identity_runs.repo_access(
            connection.engine, host=getattr(settings, "target_repo_host", "github") or "github",
            owner=target["owner"], repo=target["repo"], project_id=project_id,
            token=lambda: github_token_for(
                connection, settings, project_id, target["owner"], target["repo"]) or "") as access:
        return ensure_workspace(
            run_id,
            owner=target["owner"], repo=target["repo"], branch=target["branch"],
            clone_url=access.url, force=force, git_env=access.environ(),
        )


_LEGACY_SHELL_EDITIONS = frozenset({
    "cloud", "enterprise", "enterprise_cloud", "enterprise_self_hosted",
})
_LEGACY_SHELL_DENIAL = (
    "The legacy agent shell is unavailable in Community until it runs inside an OS-isolated "
    "sandbox with controlled filesystem and network access. Use the available read tools."
)


def _legacy_agent_shell_allowed(settings: Settings) -> bool:
    """Unknown editions fail closed; a disposable clone alone is not a sandbox."""
    return str(getattr(settings, "boss_edition", "community")).strip().lower() in (
        _LEGACY_SHELL_EDITIONS
    )


def _tool_run_shell(connection: Connection, project_id: str, settings: Settings, args: dict) -> dict:
    if not _legacy_agent_shell_allowed(settings):
        return {"error": _LEGACY_SHELL_DENIAL}
    from .agent_shell import run_in_workspace

    run_id = str(args.get("run_id") or "").strip()
    command = str(args.get("command") or "").strip()
    if not run_id:
        return {"error": "run_id is required — the shell opens in that app's code"}
    workspace, problem = _open_workspace(connection, settings, run_id, project_id)
    if problem or workspace is None:
        return {"error": problem or "could not open a workspace"}
    result = run_in_workspace(workspace, command, cwd=args.get("cwd"))
    # Every command is recorded. An agent with a shell must leave a trail a person can read.
    emit_event(
        connection, project_id, "agent.shell_command", "agent", "code_task_run", run_id,
        {"command": command[:500], "exit_code": result.get("exit_code"),
         "refused": bool(result.get("error"))},
    )
    return result


# --------------------------------------------------------------------------------------- #
# Ask the database.
#
# The agent had twelve hand-written tools and still could not answer "why did the last build
# fail", because that answer lives in Postgres — in code_task_runs.state, in jobs, in events —
# and every new question needed a new tool. Twelve becomes twenty and still misses the next
# one. Tools do not scale to open-ended questions; access does.
#
# READ ONLY, enforced three ways: one statement, a SELECT/WITH prefix, and a row cap. The point
# is to answer questions, not to let a language model write to the control plane — every action
# BOSS takes still goes through its governed tools and their approval gates.
# --------------------------------------------------------------------------------------- #

_MUTATING_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate", "grant", "revoke",
    "copy", "call", "vacuum", "refresh", "comment", "reindex", "lock", "into",
)
_SECRET_TABLES = (
    "secret_refs", "secret_material", "pg_shadow", "pg_authid", "pg_auth_members",
)
_QUERY_ROW_CAP = 200


def _query_rejection(sql: str) -> Optional[str]:
    """Why this statement must not run, or None. Separate so it is testable on its own."""
    stripped = re.sub(r"--[^\n]*", " ", sql)
    stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.S).strip()
    if not stripped:
        return "empty query"
    # One statement. A trailing semicolon is fine; a second statement is not — chaining is how
    # a read tool quietly becomes a write tool.
    body = stripped.rstrip(";")
    if ";" in body:
        return "one statement at a time"
    first = body.lstrip("( ").split(None, 1)[0].lower() if body.strip() else ""
    if first not in {"select", "with"}:
        return f"read-only: must start with SELECT or WITH, got {first.upper() or '?'}"
    lowered = f" {body.lower()} "
    for word in _MUTATING_KEYWORDS:
        if re.search(rf"\b{word}\b", lowered):
            return f"read-only: {word.upper()} is not allowed"
    for table in _SECRET_TABLES:
        if re.search(rf"\b{table}\b", lowered):
            return f"table {table} is not queryable"
    return None


def _tool_query_db(connection: Connection, project_id: str, settings: Settings, args: dict) -> dict:
    # This loop is driven by a model in a project-scoped task. Its connection can read
    # every project, and neither SQL parsing nor a prompt-supplied WHERE is a tenant fence.
    return {"error": "raw SQL is unavailable to project-scoped agents; use scoped tools"}


def _tool_describe_schema(connection: Connection, project_id: str, settings: Settings, args: dict) -> dict:
    """Schema metadata for diagnosing a validation error without reading project rows."""
    table = str(args.get("table") or "").strip()
    if table:
        columns = connection.execute(
            text("SELECT column_name, data_type FROM information_schema.columns "
                 "WHERE table_name = :t ORDER BY ordinal_position"),
            {"t": table},
        ).mappings().all()
        return {"table": table, "columns": [dict(c) for c in columns]}
    tables = connection.execute(
        text("SELECT table_name FROM information_schema.tables "
             "WHERE table_schema='public' ORDER BY table_name")
    ).scalars().all()
    return {"tables": list(tables)}


def _tool_project_status(connection: Connection, project_id: str, settings: Settings, args: dict) -> dict:
    def scalar(sql: str) -> int:
        return int(connection.execute(text(sql), {"p": project_id}).scalar() or 0)

    recent = connection.execute(
        text(
            "SELECT event_type, count(*) AS n FROM events WHERE project_id=:p "
            "AND created_at > now() - interval '7 days' GROUP BY event_type ORDER BY n DESC LIMIT 8"
        ),
        {"p": project_id},
    ).mappings().all()
    return {
        "open_tasks": scalar("SELECT count(*) FROM tasks WHERE project_id=:p AND status='open'"),
        # Waiting on the operator by the Approvals inbox's rule, not `status='pending'`, which
        # left out delegated decisions and stuck pull requests (review round 3).
        "pending_decisions": decision_actions.count_waiting_on_operator(connection, project_id),
        "deadletters": scalar("SELECT count(*) FROM deadletters WHERE project_id=:p AND status='open'"),
        "jobs_succeeded": scalar("SELECT count(*) FROM jobs WHERE project_id=:p AND status='succeeded'"),
        "recent_activity": [{"event": r["event_type"], "count": int(r["n"])} for r in recent],
    }


def _tool_list_repo_files(connection: Connection, project_id: str, settings: Settings, args: dict) -> dict:
    subdir = str(args.get("subdir") or "").strip().lstrip("/")
    base = _safe_path(settings, subdir or ".")
    if base is None or not base.exists():
        return {"error": f"path not allowed or not found: {subdir or '.'}"}
    root = _repo_root(settings)
    skip = {"node_modules", ".git", ".next", ".venv", "__pycache__", "dist", ".local-logs"}
    files: list[str] = []
    for p in sorted(base.rglob("*")):
        if any(part in skip for part in p.parts):
            continue
        if _sensitive_repo_path(p, root):
            continue
        if p.is_file():
            files.append(str(p.relative_to(root)))
        if len(files) >= 120:
            break
    return {"root": str(root.name), "subdir": subdir or ".", "files": files, "truncated": len(files) >= 120}


def _tool_read_repo_file(connection: Connection, project_id: str, settings: Settings, args: dict) -> dict:
    rel = str(args.get("path") or "").strip().lstrip("/")
    target = _safe_path(settings, rel)
    if target is None or not target.is_file():
        return {"error": f"file not allowed or not found: {rel}"}
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover
        return {"error": str(exc)}
    limit = 8000
    return {
        "path": rel,
        "content": content[:limit],
        "truncated": len(content) > limit,
        "bytes": len(content),
    }


def _tool_list_apps(connection, project_id, settings, args) -> dict:
    """The apps this project has built, so the agent can resolve "the ApproveIt app" to a run.

    Without this the agent could read repository FILES but had no idea what BOSS had built, so
    a request naming an app by name had nothing to attach to.
    """
    from sqlalchemy import text as _sql

    rows = connection.execute(_sql(
        "SELECT id, title, status, phase, spec->>'owner' AS owner, spec->>'repo' AS repo, "
        "       finalize_ctx->>'root_run_id' AS root, pull_request_url, created_at "
        "FROM code_task_runs WHERE project_id = :p "
        "ORDER BY created_at DESC LIMIT :n"),
        {"p": project_id, "n": int(args.get("limit") or 20)}).mappings().all()
    return {"apps": [{
        "run_id": r["id"], "title": r["title"], "status": r["status"], "phase": r["phase"],
        "repo": f"{r['owner']}/{r['repo']}" if r["owner"] else None,
        "branch": f"boss/code/{r['root'] or r['id']}",
        "pull_request": r["pull_request_url"],
        "created_at": str(r["created_at"]),
    } for r in rows]}


def _tool_inspect_app(connection, project_id, settings, args) -> dict:
    """Report what a built app actually IS: does it have a server, does its UI call one.

    An operator asked why an app forgot everything on reload. Answering needed the built
    bundle and the source tree; the agent could reach neither, so it guessed. This returns
    facts -- whether a server exists, whether the frontend makes API calls, what the preview
    would launch -- so the agent explains instead of speculating.
    """
    from pathlib import Path
    from .code_task_service import load_code_task_run, work_branch_for
    from .preview_service import resolve_app_entry

    run = load_code_task_run(connection, str(args.get("run_id") or ""))
    if run is None:
        return {"error": f"no such run: {args.get('run_id')}"}
    spec = run.get("spec") or {}
    owner, repo = spec.get("owner"), spec.get("repo")
    branch = work_branch_for(run)

    import subprocess
    import tempfile

    from .github_app_adapter import repo_token

    workdir = Path(tempfile.mkdtemp(prefix="boss-inspect-"))
    try:
        from boss_engine.identity import runs as _identity_runs

        with _identity_runs.repo_access(connection.engine, host="github", owner=owner, repo=repo,
                                        project_id=project_id,
                                        token=lambda: repo_token(settings, owner, repo)) as access:
            clone = owned_process.run(
                ["git", "clone", "--depth", "1", "--branch", branch, access.url, str(workdir / "r")],
                capture_output=True, text=True, timeout=300, env=access.environ())
        if clone.returncode != 0:
            return {"error": f"could not read {owner}/{repo}@{branch}",
                    "detail": clone.stderr[-300:]}
        root = workdir / "r"
        files = [str(f.relative_to(root)) for f in root.rglob("*")
                 if f.is_file() and ".git/" not in str(f)]
        servers = [f for f in files if f.endswith(("server.js", "server.ts", "index.js"))
                   and ("server" in f or "api" in f)]
        sources = [f for f in files if f.endswith((".tsx", ".ts", ".js", ".jsx"))
                   and not f.startswith(("node_modules", "dist"))]
        calls = 0
        for rel in sources[:400]:
            try:
                body = (root / rel).read_text(errors="ignore")
            except OSError:
                continue
            calls += body.count("fetch(") + body.count("axios.")
        try:
            kind, value = resolve_app_entry(root, files)
        except Exception as error:  # noqa: BLE001
            kind, value = "ambiguous", str(error)[:200]
        return {
            "run_id": run["id"], "title": run.get("title"), "repo": f"{owner}/{repo}",
            "branch": branch,
            "preview_would_run": {"kind": kind, "entry": value},
            "has_server_file": bool(servers), "server_files": servers[:5],
            "frontend_api_calls": calls,
            "has_database": any(f.endswith(".sql") or "migrations" in f for f in files),
            "verdict": (
                "No backend: the UI holds state in memory, so anything entered is lost on "
                "reload." if not servers and calls == 0 else
                "Has a server but the UI never calls it." if servers and calls == 0 else
                "Frontend calls an API but no server was found in this branch."
                if calls and not servers else
                "Full stack: a server exists and the frontend calls it."),
        }
    finally:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)


def _wt_change_existing_app(connection, project_id, settings, args) -> dict:
    """Change an app that already exists, starting from its own code.

    The agent's only write tools created TASK ROWS -- a to-do nothing acts on. Asked to "make
    this save to a database", the best it could do was file a note, which is why asking it to
    fix an app produced nothing useful. This starts real, governed work on the app's branch.
    """
    from .code_task_service import start_followup_task
    from .jobs import JobLeaseService

    run_id = str(args.get("run_id") or "").strip()
    instructions = str(args.get("instructions") or "").strip()
    if not run_id or not instructions:
        return {"error": "run_id and instructions are both required"}
    return start_followup_task(
        connection, parent_run_id=run_id, instructions=instructions,
        jobs=JobLeaseService(settings), title=args.get("title"), actor="boss_agent")


def _tool_diagnose_preview(connection, project_id, settings, args) -> dict:
    """Look at a preview and report what is actually wrong with it.

    Asked why a preview showed "Error: HTTP 404", the agent had nothing to look at. It has no
    way to make an HTTP request and no tool that reports what a preview is running, so it
    answered from imagination: that the preview was "still building", that the operator should
    open a 127.0.0.1 address belonging to a container, and that a tunnel might help. None of
    that was true -- the gateway was returning 200 and the app's own /api calls were 404ing
    because the preview was serving built files with no server behind them.

    A confident wrong answer is worse than "I cannot see that", and the fix is to let it see.
    This fetches the page and the API paths the bundle actually calls, so the difference
    between "not ready", "serving statically with no backend" and "the server crashed" is
    read off the responses rather than guessed.
    """
    import re
    import urllib.error
    import urllib.request

    from sqlalchemy import text as _sql

    from .preview_service import operator_preview_open_url

    preview_id = str(args.get("preview_id") or "").strip()
    if not preview_id:
        return {"error": "preview_id is required (prev_...)"}
    row = connection.execute(_sql(
        "SELECT id, run_id, status, mode, url, failure_kind, error, expires_at "
        "FROM app_previews WHERE id = :id"), {"id": preview_id}).mappings().first()
    if row is None:
        return {"error": f"no such preview: {preview_id}"}

    report: dict = {
        "preview_id": row["id"], "run_id": row["run_id"], "status": row["status"],
        "mode": row["mode"],
        # The address a PERSON can open — /v1/previews/{id}/web/ (absolute when
        # BOSS_PREVIEW_PUBLIC_BASE is set). app_previews.url holds the sandbox-internal
        # one (http://127.0.0.1:43053), reachable from this process and from nowhere a
        # browser runs. Reporting that column verbatim is exactly how an operator was
        # told to open http://127.0.0.1:46181 -- true of the machine, useless to them.
        "open_url": operator_preview_open_url(row["id"]),
        "internal_url": row["url"],
        "failure_kind": row["failure_kind"], "error": (row["error"] or "")[:300],
    }

    # Probed internally because that is what this process can reach; what gets REPORTED is
    # open_url above.
    base = (row["url"] or "").rstrip("/")
    if not base:
        report["verdict"] = "the preview has no URL yet; it has not started serving."
        return report

    def fetch(path: str = "") -> dict:
        target = f"{base}/{path}" if path else base + "/"
        try:
            with urllib.request.urlopen(target, timeout=20) as response:
                body = response.read(200_000)
                return {"status": response.status, "bytes": len(body), "body": body}
        except urllib.error.HTTPError as error:
            return {"status": error.code, "bytes": 0, "body": b""}
        except Exception as error:  # noqa: BLE001
            return {"status": None, "error": str(error)[:200], "bytes": 0, "body": b""}

    page = fetch()
    report["page_http"] = page.get("status")
    report["page_bytes"] = page.get("bytes")

    # Which API paths does the SHIPPED bundle call? Guessing /api/health tells you nothing
    # about an app whose routes are /api/bugs.
    api_paths: list[str] = []
    html = (page.get("body") or b"").decode("utf-8", "ignore")
    for asset in re.findall(r'src="\.?/?(assets/[^"]+\.js)"', html)[:2]:
        bundle = fetch(asset)
        text_body = (bundle.get("body") or b"").decode("utf-8", "ignore")
        api_paths.extend(re.findall(r'"(/api/[a-zA-Z0-9/_-]+)"', text_body))
    api_paths = sorted(set(api_paths))[:6]

    checks = {}
    for path in api_paths or ["/api/health"]:
        checks[path] = fetch(path.lstrip("/")).get("status")
    report["api_calls_in_bundle"] = api_paths
    report["api_responses"] = checks

    live = [code for code in checks.values() if code and 200 <= code < 400]
    if page.get("status") != 200:
        report["verdict"] = (
            f"the page itself returns HTTP {page.get('status')}; the preview is not serving.")
    elif not api_paths:
        report["verdict"] = (
            "the page serves and the bundle makes no API calls at all -- this app keeps its "
            "state in the browser, so nothing it shows will survive a reload.")
    elif live:
        report["verdict"] = "the page and its API both respond; this preview looks healthy."
    else:
        report["verdict"] = (
            "the page serves but every API path the app calls returns "
            f"{sorted({c for c in checks.values()})} -- the frontend is being served without "
            "its server running behind it, so the UI loads and every action fails.")
    return report


def _tool_diagnose_deployment(connection, project_id, settings, args) -> dict:
    """Look at what is actually deployed and whether it works.

    The counterpart to diagnose_preview, and missing for the same reason: the agent could read
    a deployment ROW but never reach the deployed thing, so "is it live?" was answered from a
    status column. A status column says a deploy finished; it does not say the app serves, and
    the health gate behind it is a 200 on `/` -- which a static shell with no backend passes.

    Fetches the app's real address, and the /api paths its own bundle calls, so a deployment
    that is green and useless can be told apart from one that works.
    """
    import re
    import urllib.error
    import urllib.request

    from sqlalchemy import text as _sql

    app_name = str(args.get("app_name") or "").strip()
    where = "d.project_id = :p" + (" AND d.app_name = :a" if app_name else "")
    params = {"p": project_id, **({"a": app_name} if app_name else {})}
    rows = connection.execute(_sql(
        f"""SELECT d.id, d.app_name, d.environment, d.status, d.target_kind, d.target_ref,
                   d.commit_ref, d.failure_reason, d.health_check_results, d.finished_at,
                   t.domains
            FROM deployment_runs d
            LEFT JOIN deployment_targets t ON t.id = d.deployment_target_id
            WHERE {where}
            ORDER BY d.created_at DESC LIMIT :n"""),
        {**params, "n": int(args.get("limit") or 5)}).mappings().all()
    if not rows:
        return {"deployments": [], "verdict":
                f"nothing has been deployed{' for ' + app_name if app_name else ''} in this "
                f"project."}

    def probe(url: str) -> dict:
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                return {"http": response.status, "body": response.read(120_000)}
        except urllib.error.HTTPError as error:
            return {"http": error.code, "body": b""}
        except Exception as error:  # noqa: BLE001
            return {"http": None, "error": str(error)[:160], "body": b""}

    out = []
    for row in rows:
        entry = {
            "deployment_id": row["id"], "app": row["app_name"],
            "environment": row["environment"], "status": row["status"],
            "target": f"{row['target_kind']}:{row['target_ref']}",
            "commit": (row["commit_ref"] or "")[:12],
            "failure_reason": (row["failure_reason"] or "")[:200] or None,
            "finished_at": str(row["finished_at"]) if row["finished_at"] else None,
        }
        domains = row["domains"] if isinstance(row["domains"], list) else []
        url = None
        for candidate in domains:
            if isinstance(candidate, str) and candidate.startswith("http"):
                url = candidate.rstrip("/")
                break
        if url is None and row["target_kind"] == "ssh" and row["target_ref"]:
            url = f"http://{row['target_ref'].split('@')[-1]}"
        if url:
            entry["url"] = url
            page = probe(url + "/")
            entry["page_http"] = page.get("http")
            html = (page.get("body") or b"").decode("utf-8", "ignore")
            api_paths: list[str] = []
            for asset in re.findall(r'src="\.?/?(assets/[^"]+\.js)"', html)[:2]:
                bundle = probe(f"{url}/{asset}")
                api_paths.extend(re.findall(
                    r'"(/api/[a-zA-Z0-9/_-]+)"',
                    (bundle.get("body") or b"").decode("utf-8", "ignore")))
            api_paths = sorted(set(api_paths))[:5]
            entry["api_calls_in_bundle"] = api_paths
            entry["api_responses"] = {q: probe(url + q).get("http") for q in api_paths}
            live = [c for c in entry["api_responses"].values() if c and 200 <= c < 400]
            if entry["page_http"] != 200:
                entry["verdict"] = f"the app does not serve (HTTP {entry['page_http']})."
            elif api_paths and not live:
                entry["verdict"] = (
                    "serves the frontend but every API path it calls fails -- deployed without "
                    "its server running, so it looks live and does nothing.")
            elif not api_paths:
                entry["verdict"] = "serves; the bundle makes no API calls (nothing persists)."
            else:
                entry["verdict"] = "serves, and its API responds."
        else:
            entry["verdict"] = ("no address recorded for this deployment, so it cannot be "
                                "checked from here.")
        out.append(entry)
    return {"deployments": out}


ToolFn = Callable[[Connection, str, Settings, dict], dict]

TOOLS: list[dict[str, Any]] = [
    {
        "fn": _tool_budget_and_costs,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_budget_and_costs",
                "description": "Get this project's REAL recorded budget cap, total spend, spend broken down by model, and provisioned infrastructure. Use this before proposing anything about cost, budget, or cloud spend.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    },
    {
        "fn": _tool_run_shell,
        "schema": {
            "type": "function",
            "function": {
                "name": "run_shell",
                "description": (
                    "Run a shell in an app checkout in enabled editions and return stdout, "
                    "stderr and the exit code. The checkout is disposable, but this legacy "
                    "shell is not isolated from the host filesystem or network. A non-zero "
                    "exit is a result worth reporting. Known publishing commands are refused; "
                    "start real work with change_existing_app or create_followup_task."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {
                            "type": "string",
                            "description": "The run whose app you want to look at (crun_...).",
                        },
                        "command": {"type": "string", "description": "One shell command."},
                        "cwd": {
                            "type": "string",
                            "description": "Optional directory inside the checkout, e.g. 'bugbox'.",
                        },
                    },
                    "required": ["run_id", "command"],
                    "additionalProperties": False,
                },
            },
        },
    },
    {
        "fn": _tool_describe_schema,
        "schema": {
            "type": "function",
            "function": {
                "name": "describe_schema",
                "description": "List BOSS table and column names for schema diagnostics. "
                               "Use project-scoped tools to read data.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "table": {"type": "string", "description": "Optional table name."},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        },
    },
    {
        "fn": _tool_project_status,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_project_status",
                "description": "Get the project's real current state: open tasks, pending decisions, deadletters, jobs completed, and recent activity. Use this to ground plans in what is actually happening.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    },
    {
        "fn": _tool_diagnose_deployment,
        "schema": {
            "type": "function",
            "function": {
                "name": "diagnose_deployment",
                "description": (
                    "Look at what is actually deployed for an app and whether it works: the "
                    "recorded deployment, its address, and what the app's own API paths "
                    "return when called. USE THIS for any question about a live or published "
                    "app. A deployment row saying `succeeded` only means the deploy finished; "
                    "the health gate behind it is a 200 on `/`, which a frontend with no "
                    "backend passes. Do not answer from the status column."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "app_name": {"type": "string", "description": "omit for all apps"},
                        "limit": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
    },
    {
        "fn": _tool_diagnose_preview,
        "schema": {
            "type": "function",
            "function": {
                "name": "diagnose_preview",
                "description": (
                    "Look at a running preview and report what is actually wrong: whether the "
                    "page serves, which /api paths its bundle calls, and what each of those "
                    "returns. USE THIS whenever someone reports anything about a preview -- a "
                    "404, a blank page, an empty dashboard, 'it resets when I reload'. Those "
                    "symptoms have different causes and only the responses tell them apart. "
                    "Never speculate about a preview you have not looked at."),
                "parameters": {
                    "type": "object",
                    "properties": {"preview_id": {"type": "string",
                                                  "description": "prev_... from the URL"}},
                    "required": ["preview_id"],
                },
            },
        },
    },
    {
        "fn": _tool_list_apps,
        "schema": {
            "type": "function",
            "function": {
                "name": "list_apps",
                "description": (
                    "List the apps this project has BUILT, with each one's run id, repo, branch "
                    "and status. Use this FIRST whenever someone names an app ('the ApproveIt "
                    "app', 'the expense tool') so you can attach their request to a real run. "
                    "Reading repository files tells you what code exists; this tells you what "
                    "BOSS built and where it lives."),
                "parameters": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer"}},
                    "required": [],
                },
            },
        },
    },
    {
        "fn": _tool_inspect_app,
        "schema": {
            "type": "function",
            "function": {
                "name": "inspect_app",
                "description": (
                    "Report what a built app actually IS: whether it has a server, whether its "
                    "frontend calls an API, whether it has a database, and what a preview would "
                    "launch for it. Use this to explain behaviour like 'it resets when I close "
                    "the tab' or 'the preview shows an empty dashboard' instead of guessing -- "
                    "those symptoms have different causes and this distinguishes them."),
                "parameters": {
                    "type": "object",
                    "properties": {"run_id": {"type": "string",
                                              "description": "run id from list_apps"}},
                    "required": ["run_id"],
                },
            },
        },
    },
    {
        "fn": _tool_list_repo_files,
        "schema": {
            "type": "function",
            "function": {
                "name": "list_repo_files",
                "description": "List real files in the codebase under an optional subdirectory. Use to discover what code exists before planning a code change.",
                "parameters": {
                    "type": "object",
                    "properties": {"subdir": {"type": "string", "description": "Optional subdirectory, e.g. 'apps/web'"}},
                    "required": [],
                },
            },
        },
    },
    {
        "fn": _tool_read_repo_file,
        "schema": {
            "type": "function",
            "function": {
                "name": "read_repo_file",
                "description": "Read the real contents of a file in the codebase. Use to ground a code plan in the actual implementation.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Repo-relative file path, e.g. 'README.md'"}},
                    "required": ["path"],
                },
            },
        },
    },
]

_TOOL_FNS: dict[str, ToolFn] = {t["schema"]["function"]["name"]: t["fn"] for t in TOOLS}
_TOOL_SCHEMAS: list[dict] = [t["schema"] for t in TOOLS]


def _native_tool_schemas(settings: Settings) -> list[dict]:
    """Offer only tools whose scope can be enforced for this project — and ask_human, the
    typed question that parks the run until a person answers (typed_forms, BOSSAPP-832)."""
    from .typed_forms import ASK_HUMAN_SCHEMA

    return [schema for schema in _TOOL_SCHEMAS
            if schema["function"]["name"] != "query_db"
            and (schema["function"]["name"] != "run_shell"
                 or _legacy_agent_shell_allowed(settings))] + [ASK_HUMAN_SCHEMA]


def _dispatch(connection: Connection, project_id: str, settings: Settings, name: str, args: dict) -> dict:
    if name == "query_db":
        return {"error": "raw SQL is unavailable to project-scoped agents; use scoped tools"}
    fn = _TOOL_FNS.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    # SAVEPOINT so a failing tool query rolls back just itself and cannot poison
    # the outer transaction that owns the whole plan-generation effect.
    nested = connection.begin_nested()
    try:
        result = fn(connection, project_id, settings, args or {})
        nested.commit()
        return result
    except Exception as exc:  # pragma: no cover
        nested.rollback()
        return {"error": f"{type(exc).__name__}: {exc}"}


# ============================================================
# Write tools — real effects, gated by risk tier. Low-risk run inline; high-risk
# are deferred to a Decision that executes them on human approval.
# ============================================================


def _create_task(
    connection: Connection,
    project_id: str,
    title: str,
    description: Optional[str],
    priority: str,
    risk_tier: int,
    parent_task_id: Optional[str] = None,
) -> dict:
    tid = make_id("tsk")
    connection.execute(
        text(
            """
            INSERT INTO tasks (id, project_id, title, description, priority, risk_tier, parent_task_id, dedupe_key)
            VALUES (:id, :p, :title, :desc, :prio, :risk, :parent, :dedupe)
            """
        ),
        {
            "id": tid,
            "p": project_id,
            "title": title[:200],
            "desc": description,
            "prio": priority,
            "risk": int(risk_tier),
            "parent": parent_task_id,
            "dedupe": tid,
        },
    )
    emit_event(
        connection, project_id, "task.created", "agent_loop", "task", tid,
        {"via": "agent_tool", "parent_task_id": parent_task_id},
    )
    return {"task_id": tid, "status": "created", "title": title}


def _wt_create_followup_task(connection, project_id, settings, args) -> dict:
    return _create_task(
        connection, project_id,
        str(args.get("title") or "Follow-up task"),
        args.get("description"),
        str(args.get("priority") or "p2"),
        1,
    )


def _wt_open_incident(connection, project_id, settings, args) -> dict:
    return _create_task(
        connection, project_id,
        "INCIDENT: " + str(args.get("title") or "untitled"),
        args.get("description"),
        "p0",
        2,
    )


def _wt_publish_document(connection, project_id, settings, args) -> dict:
    """Render a written markdown document to PDF and publish it to a GitHub PR
    (commits both the .md and the .pdf), so a human can review/verify the output.
    HIGH RISK — deferred to a human decision and only runs on approval."""
    title = str(args.get("title") or "BOSS Document").strip()
    markdown = str(args.get("markdown") or "").strip()
    subtitle = args.get("subtitle")
    task_id = args.get("task_id")
    if not markdown:
        return {"error": "markdown is required to publish a document"}
    if len(markdown) < 200:
        return {"error": "document markdown is too short to publish (write the full document first)"}

    subdir = str(args.get("repo_subdir") or "docs/prd").strip("/")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()[:50] or "document"
    short = make_id("doc").split("_")[-1][:8]
    branch = f"boss/{slug}-{short}"
    md_path = f"{subdir}/{slug}.md"
    pdf_path = f"{subdir}/{slug}.pdf"

    try:
        pdf_bytes = render_markdown_pdf(markdown, title=title, subtitle=subtitle)
    except Exception as exc:
        return {"error": f"PDF render failed: {type(exc).__name__}: {exc}"}

    try:
        gh = GitHubAdapter(settings)
        gh.ensure_branch(branch)
        gh.upsert_file(branch, md_path, markdown, f"BOSS: add {md_path}")
        gh.upsert_binary_file(branch, pdf_path, pdf_bytes, f"BOSS: add {pdf_path}")
        body = (
            f"{subtitle or ''}\n\n"
            f"This PR was produced by a BOSS agent and approved by a human.\n\n"
            f"- 📄 Markdown: `{md_path}` (renders inline on GitHub)\n"
            f"- 📑 PDF: `{pdf_path}` ({pdf_page_count(pdf_bytes)} pages, {len(pdf_bytes)} bytes)\n\n"
            f"---\n\n{markdown[:8000]}"
        )
        pr = gh.create_pull_request(branch, title, body)
    except Exception as exc:
        return {"error": f"GitHub publish failed: {type(exc).__name__}: {exc}"}

    artifact_id = create_artifact(
        connection, settings, project_id, task_id, "published_document", title,
        {
            "pull_request_url": pr.url,
            "pull_request_number": pr.number,
            "branch": branch,
            "md_path": md_path,
            "pdf_path": pdf_path,
            "pages": pdf_page_count(pdf_bytes),
            "bytes": len(pdf_bytes),
            "subtitle": subtitle,
            "output_text": markdown,
        },
    )
    emit_event(
        connection, project_id, "document.published", "agent_loop", "artifact", artifact_id,
        {"pull_request_url": pr.url, "pull_request_number": pr.number, "task_id": task_id},
    )
    return {
        "status": "published",
        "pull_request_url": pr.url,
        "pull_request_number": pr.number,
        "branch": branch,
        "md_path": md_path,
        "pdf_path": pdf_path,
        "pages": pdf_page_count(pdf_bytes),
        "artifact_id": artifact_id,
    }


WRITE_TOOLS: list[dict[str, Any]] = [
    {
        "fn": _wt_change_existing_app,
        "risk_tier": 1,
        "schema": {
            "type": "function",
            "function": {
                "name": "change_existing_app",
                "description": (
                    "Start real, governed work to CHANGE an app that already exists, building "
                    "on the code it already has. Use this -- not create_followup_task -- "
                    "whenever someone asks for a fix or an addition to an app that has been "
                    "built ('make it save to a database', 'the server is broken', 'add a "
                    "filter'). create_followup_task only files a note that nobody acts on. "
                    "This starts from the app's own branch, so nothing is rebuilt; get the "
                    "run_id from list_apps. Say what should change and why, in the "
                    "instructions -- they are handed to a coding agent."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string",
                                   "description": "the app to change, from list_apps"},
                        "instructions": {"type": "string",
                                         "description": "the change to make, in detail"},
                        "title": {"type": "string"},
                    },
                    "required": ["run_id", "instructions"],
                },
            },
        },
    },
    {
        "fn": _wt_create_followup_task,
        "risk_tier": 1,
        "schema": {
            "type": "function",
            "function": {
                "name": "create_followup_task",
                "description": ("Record a to-do in BOSS for a concrete next step. This only FILES an item -- no code is written and nothing runs. To actually change an app that exists, use change_existing_app instead; to start a new product, use a meeting. Reach for this only when the next step is genuinely for a person to do."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "priority": {"type": "string", "enum": ["p0", "p1", "p2", "p3"]},
                    },
                    "required": ["title"],
                },
            },
        },
    },
    {
        "fn": _wt_open_incident,
        "risk_tier": 2,
        "schema": {
            "type": "function",
            "function": {
                "name": "open_incident",
                "description": "Open a P0 incident. HIGH RISK — this requires human approval and will be deferred to a decision before it runs.",
                "parameters": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
                    "required": ["title"],
                },
            },
        },
    },
    {
        "fn": _wt_publish_document,
        "risk_tier": 2,
        "schema": {
            "type": "function",
            "function": {
                "name": "publish_document",
                "description": (
                    "Publish a finished written document (e.g. a PRD/spec) to a GitHub "
                    "pull request as BOTH markdown and a rendered PDF, so a human can "
                    "review and verify it. Pass the COMPLETE document as `markdown`. "
                    "HIGH RISK — deferred for human approval; the PR is created on approval."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Document title (also the PR title)"},
                        "markdown": {"type": "string", "description": "The COMPLETE document body in markdown"},
                        "subtitle": {"type": "string", "description": "Optional subtitle / source context line"},
                        "repo_subdir": {"type": "string", "description": "Repo folder to commit into (default 'docs/prd')"},
                    },
                    "required": ["title", "markdown"],
                },
            },
        },
    },
]
_WRITE_FNS: dict[str, tuple[Any, int]] = {
    t["schema"]["function"]["name"]: (t["fn"], t["risk_tier"]) for t in WRITE_TOOLS
}
_WRITE_SCHEMAS: list[dict] = [t["schema"] for t in WRITE_TOOLS]


def describe_native_tools() -> list[dict]:
    """Introspect the agent's built-in tool catalog for the Capabilities UI —
    so agents and humans can see exactly what every native tool does and which
    require approval."""
    catalog: list[dict] = []

    def _params(fn_schema: dict) -> list[dict]:
        params = fn_schema.get("parameters") or {}
        props = params.get("properties") or {}
        required = set(params.get("required") or [])
        return [
            {
                "name": k,
                "type": (v or {}).get("type", "any"),
                "required": k in required,
                "description": (v or {}).get("description", ""),
            }
            for k, v in props.items()
        ]

    for t in TOOLS:
        f = t["schema"]["function"]
        if f["name"] == "query_db":
            continue
        catalog.append({
            "name": f["name"], "kind": "read", "risk_tier": 0,
            "description": f.get("description", ""), "parameters": _params(f),
        })
    for t in WRITE_TOOLS:
        f = t["schema"]["function"]
        catalog.append({
            "name": f["name"], "kind": "write", "risk_tier": int(t["risk_tier"]),
            "description": f.get("description", ""), "parameters": _params(f),
        })
    return catalog


def execute_agent_tool(
    connection: Connection,
    project_id: str,
    settings: Settings,
    tool: str,
    args: dict,
    actor: str = "system",
    task_id: Optional[str] = None,
) -> dict:
    """Run a write tool's effect (used inline for low-risk and by decision
    approval for deferred high-risk calls)."""
    entry = _WRITE_FNS.get(tool)
    if entry is None:
        # AN AGENT'S PROPOSED FIX, APPROVED (boss.propose_fix → a hold-default decision). The
        # action is one of the engine's operational tools — the same set a chat proposal may
        # name — and it runs now under the APPROVER's identity (`actor`, and the tool-call
        # identity of the request that approved it), confirmed by that approval. The agent
        # that proposed it never runs it.
        return _execute_proposed_tool(connection, project_id, settings, tool, args, actor)
    fn, _ = entry
    # Make the owning task available to write tools (e.g. so a published document
    # artifact links to the right task) without the model having to supply it.
    if task_id and isinstance(args, dict) and not args.get("task_id"):
        args = {**args, "task_id": task_id}
    nested = connection.begin_nested()
    try:
        result = fn(connection, project_id, settings, args or {})
        nested.commit()
    except Exception as exc:  # pragma: no cover
        nested.rollback()
        return {"error": f"{type(exc).__name__}: {exc}"}
    emit_event(connection, project_id, "agent.tool_executed", actor, "task", project_id, {"tool": tool})
    return result


def _execute_proposed_tool(connection: Connection, project_id: str, settings: Settings,
                           tool: str, args: dict, actor: str) -> dict:
    """Run an approved agent proposal: an operational engine tool, as the approver."""
    from ..context import EngineContext
    from ..tools.builtin import _DISCUSS_OPERATIONS_TOOLS, action_unavailable, build_default_registry

    if tool not in _DISCUSS_OPERATIONS_TOOLS:
        return {"error": f"unknown write tool: {tool}"}
    unavailable = action_unavailable(tool, settings)
    if unavailable:
        return {"error": unavailable}
    ctx = EngineContext(settings=settings, engine=connection.engine)
    result = build_default_registry().invoke(
        tool, ctx, project_id=project_id, actor=actor,
        args={k: v for k, v in dict(args or {}).items() if not str(k).startswith("_")},
        operator_confirmed=True)
    emit_event(connection, project_id, "agent.proposal_executed", actor, "task", project_id,
               {"tool": tool, "ok": bool(result.get("ok", True))})
    return result


def _zero_usage() -> dict:
    return {"model": None, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _save_run(connection, run_id, project_id, task_id, status, messages, transcript, usage, finalize_ctx, *, step=0, pending=None, output_text=None) -> None:
    connection.execute(
        text(
            """
            INSERT INTO agent_runs (id, project_id, task_id, status, messages, transcript, usage, finalize_ctx, step, pending, output_text)
            VALUES (:id, :p, :t, :status, CAST(:messages AS JSONB), CAST(:transcript AS JSONB),
                    CAST(:usage AS JSONB), CAST(:fctx AS JSONB), :step, CAST(:pending AS JSONB), :output)
            ON CONFLICT (id) DO UPDATE SET
              status = EXCLUDED.status, messages = EXCLUDED.messages, transcript = EXCLUDED.transcript,
              usage = EXCLUDED.usage, finalize_ctx = EXCLUDED.finalize_ctx, step = EXCLUDED.step,
              pending = EXCLUDED.pending, output_text = EXCLUDED.output_text, updated_at = now()
            """
        ),
        {
            "id": run_id, "p": project_id, "t": task_id, "status": status,
            "messages": json.dumps(messages), "transcript": json.dumps(transcript),
            "usage": json.dumps(usage), "fctx": json.dumps(finalize_ctx or {}), "step": int(step),
            "pending": json.dumps(pending) if pending is not None else None, "output": output_text,
        },
    )


def load_agent_run(connection, run_id) -> Optional[dict]:
    row = connection.execute(
        text(
            "SELECT id, project_id, task_id, status, messages, transcript, usage, finalize_ctx, step, pending, output_text "
            "FROM agent_runs WHERE id = :id"
        ),
        {"id": run_id},
    ).mappings().first()
    return dict(row) if row else None


def _skill_resolution_for_run(
    connection: Connection,
    settings: Settings,
    run: dict[str, Any],
) -> dict[str, Any]:
    finalize_ctx = run.get("finalize_ctx") or {}

    # AN AGENT STEP RESOLVES FROM THE GRANT.
    #
    # `resolve_for_task` filters by the role spec's `allowed_skills` — prose in a spec pack
    # meaning "may be given these" — and unions in system skills and module defaults besides. For
    # a workflow step that hands an instruction to a named agent, the answer has to be exactly
    # what somebody GAVE that agent: `role_skills`. Anything wider means the bound the operator
    # set by choosing skills is not the bound that runs.
    step = finalize_ctx.get("agent_step")
    if isinstance(step, dict) and step.get("role_key"):
        from .agent_steps import held_skills

        specs = held_skills(connection, settings, str(run["project_id"]),
                            str(step["role_key"]))
        wanted = {str(i) for i in (step.get("skill_ids") or [])}
        if wanted:
            # Pinned at the moment the step started. A skill revoked mid-run stops being
            # offered on resume, which is the correct direction for a revocation to travel.
            specs = [spec for spec in specs if str(spec["id"]) in wanted]
        # EVERY ONE EAGER, NONE ON DEMAND. The agent was given these deliberately; making it
        # discover its own skills through a loader tool spends a turn to learn what the grant
        # already settled.
        return {"eager": specs, "on_demand": [], "role_key": str(step["role_key"])}

    module_descriptor = finalize_ctx.get("module_descriptor") or {}
    return resolve_for_task(
        connection,
        settings,
        str(run["project_id"]),
        {
            "job_kind": finalize_ctx.get("job_kind"),
            "agent_module_id": (
                finalize_ctx.get("module_id") or module_descriptor.get("id")
            ),
            "task_id": run.get("task_id"),
            "skill_caller": finalize_ctx.get("skill_caller"),
        },
    )


def _invoke_agent_skill(
    connection: Connection,
    settings: Settings,
    project_id: str,
    skill_id: str,
    caller: str,
    args: dict[str, Any],
    *,
    actor: str,
    skill_spec: Optional[dict[str, Any]] = None,
    approved_by: Optional[str] = None,
    approval_about: Optional[dict[str, Any]] = None,
    mcp_reach: Optional[str] = None,
) -> dict[str, Any]:
    return SkillRuntimeService(settings).invoke(
        connection,
        project_id,
        skill_id,
        caller,
        args,
        mock=False,
        executor_context=SkillExecutorContext(
            settings=settings,
            connection=connection,
            project_id=project_id,
            actor=actor,
            approved_by=approved_by,
            approval_about=approval_about,
            mcp_reach=mcp_reach,
        ),
        skill_spec=skill_spec,
        actor=actor,
    )


def run_agent_loop(
    connection: Connection,
    settings: Settings,
    project_id: str,
    system_prompt: Optional[str],
    user_prompt: str,
    *,
    task_id: Optional[str] = None,
    max_steps: Optional[int] = None,
    finalize_ctx: Optional[dict] = None,
) -> dict:
    """Start a durable agent run and drive it until it completes or suspends for
    a human approval. State is persisted to agent_runs after every step so the
    run survives a crash and can be resumed."""
    run_id = make_id("arun")
    try:
        from .knowledge.runtime import append_quoted_pack, quoted_pack_for_turn

        system_prompt = append_quoted_pack(
            system_prompt,
            quoted_pack_for_turn(
                connection,
                project_id=project_id,
                query=user_prompt,
                run_id=run_id,
            ),
        ) or None
    except Exception:
        pass
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})
    _save_run(connection, run_id, project_id, task_id, "running", messages, [], _zero_usage(), finalize_ctx or {}, step=0)
    emit_event(connection, project_id, "agent.run_started", "agent_loop", "agent_run", run_id, {"task_id": task_id})
    # IDENTITY (IDENTITY.md §7.2): with a verified person + grant in context, the loop runs as
    # "<agent> for <person>" under a run token (person ∩ ceiling ∩ grant ∩ task), so its tool calls,
    # jobs and egress are decided and attributed through that chain. Otherwise: unchanged.
    from boss_engine.identity import catalogue as _identity_catalogue
    from boss_engine.identity import runs as _identity_runs

    # Run by the worker for a leased job, the loop acts for that JOB's principal and grant (its
    # identity columns), never for the worker that called in.
    with _identity_runs.delegated(connection.engine, kind="ag", entity_id=run_id, project_id=project_id,
                                  role_key=(finalize_ctx or {}).get("role_key") or "atlas",
                                  scope=_identity_catalogue.delegable(),
                                  job_id=(finalize_ctx or {}).get("created_by_job_id")):
        return _drive_run(connection, settings, run_id, max_steps)


#: The catalogue permission each of the loop's own tools needs (IDENTITY.md §6.4). Inside a
#: delegated run every tool call is decided against the run token (person ∩ ceilings ∩ grant ∩
#: task); a tool missing here is refused there (deny by default). Skills and MCP tools need
#: `skill:invoke`; the repository read tools `repository:read`.
AGENT_TOOL_PERMISSIONS: dict[str, str] = {
    "describe_schema": "system:read",
    "diagnose_deployment": "deployment:read",
    "diagnose_preview": "preview:read",
    "get_budget_and_costs": "budget:read",
    "get_project_status": "project:read",
    "inspect_app": "artifact:read",
    "list_apps": "artifact:read",
    "list_repo_files": "repository:read",
    "read_repo_file": "repository:read",
    "run_shell": "sandbox:exec",                 # not delegable: never inside a delegated run
    "change_existing_app": "code_task:create",
    "create_followup_task": "work_item:manage",
    "open_incident": "reliability:respond",
    "publish_document": "github:sync",
    # A typed question to a person (typed_forms, BOSSAPP-832); its answer may create work items.
    "ask_human": "work_item:manage",
}
#: The native typed-question tool (typed_forms.ASK_HUMAN_SCHEMA); offered to unbounded runs.
_ASK_HUMAN = "ask_human"


def _agent_tool_permission(name: str, *, mcp_map: dict, repo_map: dict, skill_map: dict,
                           on_demand_loader: Optional[str]) -> Optional[str]:
    if name in mcp_map or name in skill_map or (on_demand_loader is not None and name == on_demand_loader):
        return "skill:invoke"
    if name in repo_map:
        spec, tool = repo_map[name]
        return _bundle_tool_permission(str(spec.get("id") or ""), tool)
    return AGENT_TOOL_PERMISSIONS.get(name)


def _identity_refusal(connection: Connection, project_id: str, name: str, **maps: Any) -> Optional[dict]:
    """Inside a delegated run, the tool call is decided against the RUN TOKEN before it runs.
    Returns the refusal to hand the model, or None to proceed (outside a run: unchanged)."""
    from boss_engine.identity import lineage as _identity_lineage
    from boss_engine.identity import runs as _identity_runs

    ctx = _identity_lineage.current_context()
    if ctx is None or ctx.run_token is None:
        return None
    permission = _agent_tool_permission(name, **maps)
    try:
        _identity_runs.authorize_tool(permission, project_id, db=connection)
    except _identity_runs.ToolRefused as refused:
        return {"status": "refused", "error": "forbidden", "permission": refused.permission,
                "reason": refused.reason,
                "message": (f"'{name}' needs {refused.permission}, which this run's authority does not "
                            "include. Do not retry it; say what you could not do.")}
    return None


def _replace_deferred_tool_result(messages: list, result: dict) -> None:
    """Write the approved result over the deferral the loop recorded.

    The newest tool message whose payload is still `deferred_for_approval` is the
    call that was just approved. Older turns have already been replaced.
    """
    try:
        encoded = clip_for_model(json.dumps(result), 16000)
    except TypeError:
        encoded = json.dumps({"status": "succeeded", "note": "result was not JSON-safe"})
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        try:
            payload = json.loads(str(message.get("content") or ""))
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("status") == "deferred_for_approval":
            message["content"] = encoded
            return


def resume_agent_run(connection: Connection, settings: Settings, run_id: str) -> dict:
    """Resume a suspended run: execute the approved deferred tool, feed its real
    result back into the conversation, and continue the loop."""
    run = load_agent_run(connection, run_id)
    if run is None:
        return {"status": "not_found", "run_id": run_id}
    if run["status"] != "awaiting_approval":
        return {"status": run["status"], "run_id": run_id}
    project_id = run["project_id"]
    pending = run["pending"] or {}
    from .typed_forms import is_parked_question, resume_with_answer

    if is_parked_question(pending):
        # A TYPED QUESTION, answered or declined (BOSSAPP-832): nothing is executed, so there is
        # no held call to check; the typed answer replaces the parked result and arrives as a
        # structured user message.
        return resume_with_answer(connection, settings, run)
    # Governance invariant: deferred high-risk tools only execute after the linked
    # Decision is approved. The HTTP resume endpoint is worker-facing and historically
    # had no actor check — without this gate, anyone who knew a suspended run_id could
    # POST /v1/agent-runs/{id}/resume and execute the pending tool as if approved.
    decision_id = str(pending.get("decision_id") or "").strip()
    if not decision_id:
        return {
            "status": "denied",
            "run_id": run_id,
            "reason": "missing approval decision for suspended run",
        }
    decision = connection.execute(
        text(
            "SELECT status, recommendation FROM decisions "
            "WHERE id = :id AND project_id = :project_id"
        ),
        {"id": decision_id, "project_id": project_id},
    ).mappings().first()
    if decision is None or decision["status"] != "approved":
        return {
            "status": "denied",
            "run_id": run_id,
            "decision_id": decision_id,
            "reason": (
                "decision not approved"
                if decision is not None
                else "approval decision not found"
            ),
            "decision_status": None if decision is None else decision["status"],
        }
    # Pending args can be replaced after the person approved a different call.
    # No digest on an unstamped decision means an older one; that resume is unchanged. A v2
    # decision needs its digest and signature (argument_binding.binding_holds, BOSSAPP-816).
    recommendation = decision["recommendation"]
    if isinstance(recommendation, str):
        try:
            recommendation = json.loads(recommendation)
        except ValueError:
            recommendation = {}
    if not isinstance(recommendation, dict):
        recommendation = {}
    tool_name = recommendation.get("tool") or pending.get("tool") or ""
    if not isinstance(tool_name, str):
        tool_name = str(tool_name)
    if not binding_holds(tool_name, pending.get("args"), recommendation):
        return {
            "status": "denied",
            "run_id": run_id,
            "decision_id": decision_id,
            "reason": CHANGED,
        }
    if pending.get("kind") == "mcp":
        server = next(
            (
                item
                for item in list_mcp_servers(
                    connection, project_id, enabled_only=True
                )
                if item["id"] == pending.get("server_id")
            ),
            None,
        )
        tool = None
        if server is not None:
            try:
                tool = next(
                    (
                        item
                        for item in MCPClient(settings).list_tools(
                            server["url"], headers=auth_headers(
                                connection, settings, project_id, server))
                        if item.get("name") == pending.get("tool_name")
                    ),
                    None,
                )
            except Exception:
                tool = None
        if server is None or tool is None:
            result = {
                "status": "denied",
                "reason": "MCP server or tool is no longer enabled and available",
            }
        else:
            # The approved call waits out a busy minute like any other (BudgetWaiter); past
            # the bound the agent reads the plain sentence rather than a refusal.
            result, _ = BudgetWaiter().invoke(lambda: _invoke_mcp_skill(
                connection,
                settings,
                project_id,
                server,
                tool,
                pending.get("args") or {},
                run_id=run_id,
                step=int(pending.get("step") or run.get("step") or 0),
                actor="decision_approval",
                # Checked approved above; the MCP effect reads it again (needs_person),
                # and checks it is THIS run's decision (approval_stands).
                approved_by=decision_id,
                approval_about={"run_id": run_id},
            ))
            emit_event(
                connection,
                project_id,
                "agent.mcp_invoked",
                "decision_approval",
                "agent_run",
                run_id,
                {
                    "server": server["name"],
                    "tool": tool["name"],
                    "approved": True,
                },
            )
    elif pending.get("kind") == "skill":
        skill_id = str(pending.get("skill_id") or "")
        resolution = _skill_resolution_for_run(connection, settings, run)
        still_resolved = next(
            (
                skill
                for skill in resolution["eager"] + resolution["on_demand"]
                if skill["id"] == skill_id
            ),
            None,
        )
        if still_resolved is None:
            scope = None
            if resolution.get("role_key"):
                scope = RoleGovernanceService(settings).check_scope(
                    connection,
                    project_id=project_id,
                    role_key=str(resolution["role_key"]),
                    action="skill.invoke",
                    skill_id=skill_id,
                    actor="decision_approval",
                    evidence_ref=run_id,
                )
            result = {
                "status": "denied",
                "reason": (
                    (scope or {}).get("reason")
                    if scope and not scope.get("allowed")
                    else "skill is no longer available for this project/task"
                ),
                "skill_id": skill_id,
            }
        else:
            result, _ = BudgetWaiter().invoke(lambda: _invoke_agent_skill(
                connection,
                settings,
                project_id,
                skill_id,
                str(pending.get("caller") or resolution.get("role_key") or "agent_loop"),
                pending.get("args") or {},
                actor="decision_approval",
                skill_spec=still_resolved,
                approved_by=decision_id,
                approval_about={"run_id": run_id},
            ))
            emit_event(
                connection,
                project_id,
                "agent.skill_invoked",
                "decision_approval",
                "agent_run",
                run_id,
                {"skill_id": skill_id, "approved": True},
            )
    else:
        result = execute_agent_tool(
            connection, project_id, settings, str(pending.get("tool")), pending.get("args") or {},
            "decision_approval", task_id=run.get("task_id"),
        )
    messages = run["messages"]
    # THE DEFERRED TOOL PAYLOAD IS NOT THE RESULT.
    #
    # The loop writes `deferred_for_approval` into the tool message and stops. Resume
    # used to tell the model what happened in a user message and leave that payload as
    # the deferral. A workflow step reads tool payloads to learn what the agent
    # produced, so the skill a person just approved — the publish, the render, the
    # program — never reached the next step.
    if isinstance(messages, list):
        _replace_deferred_tool_result(messages, result)
    messages.append(
        {
            "role": "user",
            "content": (
                f"APPROVED & EXECUTED: a human approved the deferred action "
                f"'{pending.get('tool')}'. Result: {json.dumps(result)[:2000]}. "
                "Continue and finalize your plan, incorporating this outcome."
            ),
        }
    )
    transcript = run["transcript"]
    transcript.append(
        {
            "step": int(run["step"] or 0),
            "tool": pending.get("tool"),
            "args": pending.get("args"),
            "result_preview": _preview_of(result),
            "deferred": False,
            "resumed": True,
        }
    )
    _save_run(
        connection, run_id, project_id, run["task_id"], "running", messages, transcript,
        run["usage"] or _zero_usage(), run["finalize_ctx"] or {}, step=int(run["step"] or 0), pending=None,
    )
    emit_event(connection, project_id, "agent.run_resumed", "decision_approval", "agent_run", run_id, {"tool": pending.get("tool")})
    return _drive_run(connection, settings, run_id, None)


def _sink_for_call(name: str, mcp_map: dict, skill_map: dict) -> Optional[str]:
    """`external_provider` or `write` when this call would send text out, else None."""
    if name in _WRITE_FNS:
        return "write"
    if name in mcp_map:
        return "external_provider"
    skill = skill_map.get(name)
    return sink_for_skill(skill) if isinstance(skill, dict) else None


def _through_receipt_door(result: dict) -> bool:
    """True when this result came back through mcp_registry.call_tool_with_receipt, which has
    already screened it (BOSSAPP-822): it, or the invocation wrapped around it, has a receipt."""
    if "receipt_id" in result or isinstance(result.get("screen"), dict):
        return True
    for key in ("result", "output"):
        inner = result.get(key)
        if isinstance(inner, dict) and ("receipt_id" in inner
                                        or isinstance(inner.get("screen"), dict)):
            return True
    return False


def _origin_for_call(name: str, mcp_map: dict, skill_map: dict) -> Optional[str]:
    """How this call's result arrived from outside, or None for the operator's own data."""
    if name in mcp_map:
        _server, tool = mcp_map[name]
        real = str(tool.get("name") or name)
        return outside_origin(real, kind="mcp", mcp_tool=real)
    skill = skill_map.get(name)
    if isinstance(skill, dict):
        policy = skill.get("policy") or {}
        return outside_origin(
            str(skill.get("id") or name),
            kind=str(skill.get("kind") or ""),
            mcp_tool=str(policy.get("mcp_tool") or ""),
        )
    return outside_origin(name)


def _hold_for_untrusted_content(
    connection, project_id, task_id, run_id, step, name, args, sink, hold,
    mcp_map, skill_map, skill_resolution,
) -> tuple[dict, dict]:
    """Pause the run on a decision. Resume executes the pending call; this is not checked again."""
    origin = str(hold.get("origin") or "tool_result").replace("_", " ")
    question = (
        f"Approve {name}? It carries content that arrived from outside the operator "
        f"({origin}), and it has not been sent."
    )
    recommendation: dict[str, Any] = {
        "action": "resume_agent_run",
        "run_id": run_id,
        "tool": name,
        "args": args,
        "task_id": task_id,
        "sink": sink,
        "untrusted_origin": hold.get("origin"),
        **bind_held_call(name, args),
    }
    if name in mcp_map:
        server, tool = mcp_map[name]
        tool_name = str(tool.get("name") or "tool")
        recommendation.update({
            "kind": "mcp", "server_id": server["id"], "tool_name": tool_name,
        })
        pending: dict[str, Any] = {
            "kind": "mcp", "tool": name, "server_id": server["id"],
            "tool_name": tool_name, "args": args, "step": step,
        }
    elif name in skill_map:
        skill = skill_map[name]
        skill_id = str(skill.get("id") or name)
        caller = str(
            skill.get("caller") or skill.get("role_key")
            or (skill_resolution or {}).get("role_key") or "agent_loop"
        )
        recommendation.update({"kind": "skill", "skill_id": skill_id, "caller": caller})
        pending = {
            "kind": "skill", "tool": name, "skill_id": skill_id,
            "caller": caller, "args": args,
        }
    else:
        pending = {"tool": name, "args": args}
    decision_id = create_blocking_decision(
        connection, project_id, task_id, question, recommendation, {},
        "untrusted_content_requires_approval",
    )
    pending["decision_id"] = decision_id
    emit_event(
        connection, project_id, "agent.tool_deferred", "agent_loop", "decision",
        decision_id, {"tool": name, "sink": sink, "step": step, "untrusted": True},
    )
    return {
        "status": "deferred_for_approval",
        "decision_id": decision_id,
        "untrusted": True,
        "sent": False,
        "message": hold.get("message") or question,
    }, pending


def _drive_run(connection: Connection, settings: Settings, run_id: str, max_steps: Optional[int]) -> dict:
    run = load_agent_run(connection, run_id)
    project_id = run["project_id"]
    task_id = run["task_id"]
    messages = run["messages"]
    transcript = run["transcript"]
    total = run["usage"] or _zero_usage()
    finalize_ctx = run["finalize_ctx"] or {}
    step0 = int(run["step"] or 0)

    # THE MODEL THIS PROJECT CONNECTED. This was `make_chat_adapter(settings)` — the
    # deployment's `llm_engine`, "auto", the OpenAI adapter — so every agent step on an install
    # with a Claude or Codex subscription and no OpenAI key died at its first turn with
    # "OPENAI_API_KEY is not configured" while readiness said a model was connected
    # ("Video edit and publish", 2026-10-03). See services/model_routing.py.
    #
    # AND EVERY TURN IS SPENT THROUGH THE BUDGET. The turns made no reservation and left no
    # receipt, so an agent step on a capped project spent past its cap and the cost was nowhere.
    #
    # AND THE ROUTER PICKS IT, ONCE FOR THE RUN (docs/product/MODEL_ROUTER.md): an agent step
    # is routed on its brief and its role, honours the agent's Agent Studio model policy
    # (Routed / Pinned / Local), and keeps that model for every turn of the session.
    from .model_routing import routing_purpose

    with routing_purpose(_agent_purpose(connection, run_id, project_id, messages,
                                        finalize_ctx)):
        adapter = governed(project_chat_adapter(connection, settings, project_id),
                           connection, settings, project_id,
                           skill_id="model.agent_turn", ref=run_id)
    mcp = MCPClient(settings)

    # A BOUNDED RUN GETS NO NATIVE TOOLS AT ALL.
    #
    # In enabled editions, the ordinary loop offers a shell, the project's Postgres, the
    # repository and deploy tools because it was written for the software factory. An
    # agent step is the opposite case: an operator gave an agent a set of skills and that set is
    # the claim about what it can do. Handing it `run_shell` besides makes the grant decorative —
    # a video editor could read the control plane's database, and "the skills you gave it" would
    # stop being a true description of its reach.
    #
    # MCP SERVERS GO THE SAME WAY: a connector is authority over somebody's account, so being
    # REGISTERED in the project grants nothing. A bounded run is offered exactly the servers its
    # agent HOLDS (role_mcp_servers), pinned at step start, and only the granted tool names when
    # the grant lists any. Before that table existed a bounded run got no MCP tool at all, so an
    # agent could not be given "post to our Mattermost" short of authoring a skill per tool.
    bounded = isinstance(finalize_ctx.get("agent_step"), dict)
    schemas = [] if bounded else _native_tool_schemas(settings) + list(_WRITE_SCHEMAS)
    occupied = {
        str(schema["function"]["name"])
        for schema in schemas
    }
    mcp_map: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    from .mcp_registry import known_unreachable, record_reachability_quietly
    from .mcp_registry import needs_person, unreviewed_sentence

    # ONE OWNERSHIP MODEL (integration of the agents and skills-mcp streams): a bounded run is
    # offered what its agent HOLDS (role_mcp_servers, via agents.held_mcp_servers); the
    # registry's reachability memory and review rule then apply to those servers exactly as
    # they do to an unbounded run's. A step's own `mcp` list (the steps stream's scope) narrows
    # that set — agent_steps.run pins held-and-named into mcp_server_ids — and never widens it.
    for server in (_granted_mcp_servers(connection, project_id, finalize_ctx["agent_step"])
                   if bounded
                   else list_mcp_servers(connection, project_id, enabled_only=True)):
        if known_unreachable(server):
            # A SERVER THAT JUST FAILED IS NOT ASKED AGAIN EVERY TURN. The three servers BOSS
            # seeds point at ports the community compose never starts, and every chat turn paid
            # a connection timeout per server to rediscover that. The registry remembers the
            # failure for a minute; the screens show it with what to do about it.
            continue
        try:
            server_tools = mcp.list_tools(
                server["url"], headers=auth_headers(connection, settings, project_id, server))
        except Exception as exc:
            record_reachability_quietly(connection, project_id, server, reachable=False,
                                        error=f"{type(exc).__name__}: {exc}")
            emit_event(
                connection,
                project_id,
                "agent.mcp_discovery_failed",
                "agent_loop",
                "mcp_server",
                str(server["id"]),
                {"server": server["name"], "error": str(exc)},
            )
            continue
        record_reachability_quietly(connection, project_id, server, reachable=True,
                                    tools=server_tools)
        allow = set(server.get("granted_tools") or [])
        for t in server_tools:
            tool_name = str(t.get("name") or "tool")
            if allow and tool_name not in allow:
                continue
            if _mcp_tool_policy(server, t) == "off":
                # TURNED OFF IN SKILLS & MCP: never offered, so the model cannot even ask.
                continue
            ns = _unique_tool_name(
                "mcp_" + _slug(server["name"]) + "_" + _slug(tool_name),
                occupied,
                f"mcp:{server['id']}:{tool_name}",
            )
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": ns,
                        "description": (
                            f"[MCP · {server['name']}"
                            + (" · UNREVIEWED server: every call waits for a person"
                               if needs_person(server) else "")
                            + f"] {t.get('description', '')}"),
                        "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                    },
                }
            )
            mcp_map[ns] = (server, t)

    skill_resolution = _skill_resolution_for_run(connection, settings, run)
    # OFF IN SKILLS & MCP MEANS NOT OFFERED (kit.skill_policy): the model cannot call what the
    # operator turned off, so it does not spend a turn finding out.
    eager = [spec for spec in skill_resolution["eager"]
             if _skill_policy(connection, settings, project_id, spec) != "off"]
    # READ THE REPO IS FIVE TOOLS, ONE SKILL (kit E11): holding `repo.read` offers the engine's
    # read tools by their own names and schemas; each call is a call of that one skill. The
    # baseline every agent holds (boss.fundamentals, boss.propose_fix) is offered the same way.
    from .agent_baseline import bundles as _bundles

    _bundled = set(_bundles())
    repo_map = _append_bundle_tools(
        schemas, occupied, [s for s in eager if str(s["id"]) in _bundled])
    skill_map = _append_skill_tools(
        schemas,
        occupied,
        [spec for spec in eager if str(spec["id"]) not in _bundled],
    )
    # Spans survive a clipped tool message: the run row keeps them, and a resumed drive
    # rebuilds from that plus any tool message that still carries the mark.
    _untrusted = Ledger.load(finalize_ctx.get("untrusted_spans"))
    _untrusted.absorb_messages(messages)
    from .tool_result_screen import carried_untrusted, next_write_must_ask, screen_result

    # A phrase-screen hit on a tool result holds the next write. A read keeps its policy.
    screen_hold = bool(finalize_ctx.get("tool_screen_untrusted"))
    on_demand_loader: Optional[str] = None
    if skill_resolution["on_demand"]:
        on_demand_loader = _unique_tool_name(
            "load_on_demand_skill", occupied, "skill:on_demand:loader"
        )
        discoverable = [
            {
                "id": str(skill["id"]),
                "description": str(skill.get("description") or ""),
            }
            for skill in skill_resolution["on_demand"]
        ]
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": on_demand_loader,
                    "description": (
                        "Attach one on-demand BOSS skill for the remainder of this run. "
                        f"Available skills: {json.dumps(discoverable)}"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_id": {
                                "type": "string",
                                "enum": [item["id"] for item in discoverable],
                            }
                        },
                        "required": ["skill_id"],
                        "additionalProperties": False,
                    },
                },
            }
        )

    steps_cap = int(max_steps or settings.agent_max_steps)
    threshold = int(settings.agent_autonomous_risk_max)

    def add_usage(u: dict) -> None:
        total["model"] = u.get("model")
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            total[k] = int(total.get(k) or 0) + int(u.get(k) or 0)
        # carry exact per-call cost (e.g. Claude CLI total_cost_usd) when present
        if u.get("cost_usd_micros") is not None:
            total["cost_usd_micros"] = int(total.get("cost_usd_micros") or 0) + int(u["cost_usd_micros"])

    final_text: Optional[str] = None
    last_step = step0
    refused_calls = 0
    # One waiter per drive, so the per-run bound covers every call this drive makes.
    waiter = BudgetWaiter()
    budget_wait: Optional[dict[str, Any]] = None
    for i in range(steps_cap):
        step = step0 + i
        last_step = step
        try:
            # A PROVIDER HICCUP IS RETRIED WHERE IT HAPPENS, NOT BY FAILING THE RUN. The chat
            # path (EngineAgent.chat) and the coding path both retry a 529/503/timeout before
            # giving up; this loop was the one model caller that did not, so a single
            # overloaded_error part-way through an investigation threw away every tool result
            # it had already paid for. A completion is safe to run again — the tool calls it
            # returns have not run yet.
            message, usage = _with_transient_retry(
                lambda: adapter.create_chat_completion(messages, tools=schemas))
        except Exception as exc:  # noqa: BLE001 — sorted by what it is, one line down
            complaint = _the_provider_refused_the_tool_call(exc)
            if complaint is None or refused_calls >= MAX_REFUSED_TOOL_CALLS:
                raise
            # A MALFORMED CALL IS A MISTAKE, NOT THE END OF THE WORK.
            #
            # Some providers validate a tool call against its schema and return 400 instead of
            # the message, so the exception lands HERE — before any assistant turn exists — and
            # killed the whole step. A video edit that had already trimmed and reframed correctly
            # was deadlettered because one later call omitted a property.
            #
            # A person told "that form was filled in wrong" fills it in again. The refusal is
            # handed back as the next thing the agent reads, and the loop carries on with
            # everything it has already done intact. Bounded, so a model that cannot get it
            # right does not spend the run trying.
            refused_calls += 1
            messages.append({
                "role": "user",
                "content": (
                    "YOUR LAST TOOL CALL WAS REFUSED BEFORE IT RAN, so nothing happened and "
                    "nothing changed. The model provider checked it against the tool's "
                    "parameters and rejected it:\n\n"
                    f"{complaint}\n\n"
                    "This is a fault in how the call was written, not in the tool. Read that "
                    "tool's parameters again, include every property it marks required, and "
                    "make the call again. Everything you have already done still stands — do "
                    "not repeat it."
                ),
            })
            emit_event(
                connection, project_id, "agent.tool_call_refused", "agent_loop",
                "agent_run", run_id,
                {"attempt": refused_calls, "detail": complaint[:500]},
            )
            continue
        add_usage(usage)
        messages.append(message["raw"])
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            final_text = message.get("content") or ""
            break

        deferred: Optional[dict] = None
        thought = (message.get("content") or "").strip()
        for tc_idx, tc in enumerate(tool_calls):
            try:
                args = json.loads(tc.get("arguments") or "{}")
            except Exception:
                args = {}
            name = tc["name"]
            is_deferred = False
            # IDENTITY: decided against the run token before anything runs (or is deferred).
            identity_refused = (_identity_refusal(connection, project_id, name, mcp_map=mcp_map, repo_map=repo_map,
                                                  skill_map=skill_map, on_demand_loader=on_demand_loader)
                                if deferred is None and budget_wait is None else None)
            # OUTSIDE TEXT DOES NOT LEAVE. A page, a tool result, or a document remembered
            # earlier in this run waits here instead of being sent. Resume runs the pending
            # call without asking this again, so an approval can release it.
            sink = None
            untrusted_hold = None
            if deferred is None and budget_wait is None and identity_refused is None:
                sink = _sink_for_call(name, mcp_map, skill_map)
                if sink is not None:
                    untrusted_hold = blocked(args, sink=sink, ledger=_untrusted)
            if deferred is not None:
                result = {"status": "skipped", "note": "a prior action this turn awaits approval"}
            elif budget_wait is not None:
                result = {"status": "skipped", "note": BUDGET_BUSY_MESSAGE}
            elif identity_refused is not None:
                result = identity_refused
            elif untrusted_hold is not None:
                result, deferred = _hold_for_untrusted_content(
                    connection, project_id, task_id, run_id, step, name, args, sink,
                    untrusted_hold, mcp_map, skill_map, skill_resolution,
                )
                is_deferred = True
            elif on_demand_loader is not None and name == on_demand_loader:
                requested_id = str(args.get("skill_id") or "")
                current_resolution = _skill_resolution_for_run(
                    connection, settings, run
                )
                skill = next(
                    (
                        item
                        for item in current_resolution["on_demand"]
                        if item["id"] == requested_id
                    ),
                    None,
                )
                if skill is None:
                    scope = None
                    if current_resolution.get("role_key") and requested_id:
                        scope = RoleGovernanceService(settings).check_scope(
                            connection,
                            project_id=project_id,
                            role_key=str(current_resolution["role_key"]),
                            action="skill.invoke",
                            skill_id=requested_id,
                            actor="agent_loop",
                            evidence_ref=run_id,
                        )
                    result = {
                        "status": "denied",
                        "skill_id": requested_id,
                        "reason": (
                            (scope or {}).get("reason")
                            if scope and not scope.get("allowed")
                            else None
                        )
                        or "skill is not available on demand for this task",
                    }
                else:
                    loaded = _append_skill_tools(schemas, occupied, [skill])
                    skill_map.update(loaded)
                    result = {
                        "status": "loaded",
                        "skill_id": requested_id,
                        "tool_name": next(iter(loaded)),
                        "available_for_remainder_of_run": True,
                    }
                    emit_event(
                        connection,
                        project_id,
                        "agent.skill_lazy_loaded",
                        "agent_loop",
                        "agent_run",
                        run_id,
                        {"skill_id": requested_id, "step": step},
                    )
            elif name == _ASK_HUMAN and not bounded and deferred is None:
                # A TYPED QUESTION (BOSSAPP-832): a form decision is raised and the run parks
                # until it is answered; resume_agent_run hands the typed answer back.
                from .typed_forms import FormError, ask_human

                try:
                    with connection.begin_nested():
                        asked = ask_human(
                            connection, project_id=project_id,
                            question=str(args.get("question") or ""), form=args.get("fields"),
                            asker=f"agent_run:{run_id}", run_id=run_id,
                            work_item_id=str(args.get("work_item_id") or "") or None,
                            task_id=task_id, context=str(args.get("context") or ""))
                except (FormError, LookupError) as refused:
                    result = {"status": "refused", "error": str(refused)}
                else:
                    result = {"status": "deferred_for_approval",
                              "decision_id": asked["decision_id"],
                              "message": "Your question is with a person; the run resumes with "
                                         "their typed answer."}
                    is_deferred = True
                    deferred = {"tool": name, "args": args, "decision_id": asked["decision_id"],
                                "kind": "ask_human"}
            elif name in mcp_map:
                server, tool = mcp_map[name]
                tool_name = str(tool.get("name") or "tool")
                risk = _mcp_tool_risk(server, tool)
                # AN UNREVIEWED SERVER'S TOOL ALWAYS WAITS FOR A PERSON, whatever its tier.
                # The tier is the server's own claim; until somebody has reviewed the server
                # (mcp_registry.reviewed) that claim is the only thing standing between a
                # registered URL and an unattended call. Round-1 review: a chat-registered
                # tier-0 server's tools ran in every chat without anyone being asked.
                unreviewed = needs_person(server)
                # ASK IS THE OPERATOR'S RULE, and tier >= 2 always asks (spec E7): a tier-2 tool
                # waits for a person even on an install that raised the autonomy threshold.
                policy = next_write_must_ask(
                    {"untrusted": screen_hold},
                    write=risk >= 2,
                    policy=_mcp_tool_policy(server, tool),
                )
                asks = policy == "ask"
                if risk > threshold or unreviewed or asks:
                    decision_id = create_blocking_decision(
                        connection,
                        project_id,
                        task_id,
                        (f"Approve MCP tool: {server['name']} / {_key_words(tool_name)}"
                         + (" (server not reviewed yet)" if unreviewed else "")),
                        {
                            "action": "resume_agent_run",
                            "run_id": run_id,
                            "tool": name,
                            "kind": "mcp",
                            "server_id": server["id"],
                            "tool_name": tool_name,
                            "args": args,
                            "task_id": task_id,
                            **({"tool_screen": "untrusted"} if screen_hold and risk >= 2 else {}),
                            **bind_held_call(name, args),
                        },
                        {},
                        "agent_mcp_requires_approval",
                        risk_tier=risk,
                    )
                    result = {
                        "status": "deferred_for_approval",
                        "decision_id": decision_id,
                        "message": (
                            f"MCP tool '{tool_name}' (risk tier {risk}) requires "
                            "human approval."
                            + (f" {unreviewed_sentence(server)}" if unreviewed else "")
                        ),
                    }
                    is_deferred = True
                    if screen_hold and risk >= 2:
                        screen_hold = False
                        finalize_ctx["tool_screen_untrusted"] = False
                    deferred = {
                        "kind": "mcp",
                        "tool": name,
                        "server_id": server["id"],
                        "tool_name": tool_name,
                        "args": args,
                        "decision_id": decision_id,
                        "step": step,
                    }
                    emit_event(
                        connection,
                        project_id,
                        "agent.mcp_deferred",
                        "agent_loop",
                        "decision",
                        decision_id,
                        {
                            "server": server["name"],
                            "tool": tool_name,
                            "risk_tier": risk,
                            "step": step,
                        },
                    )
                else:
                    result, exhausted = waiter.invoke(lambda: _invoke_mcp_skill(
                        connection,
                        settings,
                        project_id,
                        server,
                        tool,
                        args,
                        run_id=run_id,
                        step=step,
                        actor="agent_loop",
                    ))
                    if exhausted is not None and bounded:
                        budget_wait = exhausted
                    emit_event(
                        connection,
                        project_id,
                        "agent.mcp_invoked",
                        "agent_loop",
                        "agent_run",
                        run_id,
                        {
                            "server": server["name"],
                            "tool": tool_name,
                            "risk_tier": risk,
                            "step": step,
                        },
                    )
            elif name in repo_map:
                spec, tool = repo_map[name]
                bundle_id = str(spec.get("id") or "repo.read")
                held_now = any(str(item["id"]) == bundle_id for item in
                               _skill_resolution_for_run(connection, settings, run)["eager"])
                if not held_now:
                    result = {"status": "denied", "skill_id": bundle_id,
                              "reason": f"{_BUNDLE_LABEL.get(bundle_id, bundle_id)} is no "
                                        "longer held by this agent"}
                else:
                    caller = str(spec.get("caller") or skill_resolution.get("role_key")
                                 or "agent_loop")
                    from .agent_baseline import BUNDLE_TOOL_KEY

                    picked = ({"tool": tool, **args} if bundle_id == "repo.read"
                              else {**args, BUNDLE_TOOL_KEY: tool})
                    result, exhausted = waiter.invoke(lambda: _invoke_agent_skill(
                        connection, settings, project_id, bundle_id, caller,
                        picked, actor="agent_loop", skill_spec=spec))
                    if exhausted is not None and bounded:
                        budget_wait = exhausted
                    emit_event(connection, project_id, "agent.skill_invoked", "agent_loop",
                               "agent_run", run_id,
                               {"skill_id": bundle_id, "tool": tool, "step": step})
            elif name in skill_map:
                skill = skill_map[name]
                skill_id = str(skill["id"])
                current_resolution = _skill_resolution_for_run(
                    connection, settings, run
                )
                current_skill = next(
                    (
                        item
                        for item in current_resolution["eager"]
                        + current_resolution["on_demand"]
                        if item["id"] == skill_id
                    ),
                    None,
                )
                if current_skill is None:
                    scope = None
                    if current_resolution.get("role_key"):
                        scope = RoleGovernanceService(settings).check_scope(
                            connection,
                            project_id=project_id,
                            role_key=str(current_resolution["role_key"]),
                            action="skill.invoke",
                            skill_id=skill_id,
                            actor="agent_loop",
                            evidence_ref=run_id,
                        )
                    result = {
                        "status": "denied",
                        "skill_id": skill_id,
                        "reason": (
                            (scope or {}).get("reason")
                            if scope and not scope.get("allowed")
                            else None
                        )
                        or "skill is no longer enabled for this task",
                    }
                    current_skill = None
                if current_skill is None:
                    step_obj = {
                        "step": step,
                        "tool": name,
                        "args": args,
                        "result_preview": _preview_of(result),
                        "deferred": False,
                    }
                    if tc_idx == 0 and thought:
                        step_obj["thought"] = thought
                    transcript.append(step_obj)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": clip_for_model(json.dumps(result), 16000),
                        }
                    )
                    continue
                skill = current_skill
                risk = int(skill.get("risk_tier") or 0)
                caller = str(
                    skill.get("caller")
                    or skill.get("role_key")
                    or skill_resolution.get("role_key")
                    or "agent_loop"
                )
                # THE SAME REVIEW RULE AS A DIRECT MCP CALL (mcp_registry.needs_person), read
                # from the catalogue: an authored mcp_tool skill on an unreviewed server carries
                # policy.requires_person. Round-2 review: this branch asked only the tier, so
                # the tool an agent could not call directly without a person ran unattended
                # once it was wrapped in a skill the agent held.
                person_reason = str(
                    (skill.get("policy") or {}).get("requires_person") or "")
                unreviewed_server = bool(person_reason)
                if not person_reason and _skill_policy(
                        connection, settings, project_id, skill) == "ask":
                    # SET TO ASK IN SKILLS & MCP (and tier >= 2 always is): a person decides.
                    person_reason = "It is set to ask a person each time in Skills & MCP."
                # A WRITE-CLASS SKILL after an untrusted tool result waits too (BOSSAPP-822): the
                # same hold the MCP and built-in write branches apply.
                screen_asks = next_write_must_ask(
                    {"untrusted": screen_hold}, write=risk >= 2, policy="allow") == "ask"
                if screen_asks and not person_reason:
                    person_reason = ("An earlier tool result in this run was screened as "
                                     "untrusted, so this write waits for a person.")
                if risk > threshold or person_reason:
                    decision_id = create_blocking_decision(
                        connection,
                        project_id,
                        task_id,
                        f"Approve agent skill: {skill.get('label') or skill.get('name') or _key_words(skill_id)}"
                        + (" (its MCP server is not reviewed yet)" if unreviewed_server else ""),
                        {
                            "action": "resume_agent_run",
                            "run_id": run_id,
                            "tool": name,
                            "kind": "skill",
                            "skill_id": skill_id,
                            "caller": caller,
                            "args": args,
                            "task_id": task_id,
                            **({"tool_screen": "untrusted"} if screen_asks else {}),
                            **bind_held_call(name, args),
                        },
                        {},
                        "agent_skill_requires_approval",
                        risk_tier=risk,
                    )
                    result = {
                        "status": "deferred_for_approval",
                        "decision_id": decision_id,
                        "message": (
                            f"'{skill_id}' (risk tier {risk}) requires human "
                            "approval; the run is paused and resumes on approval."
                            + (f" {person_reason}" if person_reason else "")
                        ),
                    }
                    is_deferred = True
                    if screen_asks:
                        screen_hold = False
                        finalize_ctx["tool_screen_untrusted"] = False
                    deferred = {
                        "kind": "skill",
                        "tool": name,
                        "skill_id": skill_id,
                        "caller": caller,
                        "args": args,
                        "decision_id": decision_id,
                    }
                    emit_event(
                        connection,
                        project_id,
                        "agent.skill_deferred",
                        "agent_loop",
                        "decision",
                        decision_id,
                        {"skill_id": skill_id, "risk_tier": risk, "step": step},
                    )
                else:
                    result, exhausted = waiter.invoke(lambda: _invoke_agent_skill(
                        connection,
                        settings,
                        project_id,
                        skill_id,
                        caller,
                        args,
                        actor="agent_loop",
                        skill_spec=skill,
                    ))
                    if exhausted is not None and bounded:
                        budget_wait = exhausted
                    emit_event(
                        connection,
                        project_id,
                        "agent.skill_invoked",
                        "agent_loop",
                        "agent_run",
                        run_id,
                        {"skill_id": skill_id, "step": step},
                    )
            elif name in _WRITE_FNS:
                _, risk = _WRITE_FNS[name]
                policy = next_write_must_ask(
                    {"untrusted": screen_hold},
                    write=True,
                    policy="ask" if risk > threshold else "allow",
                )
                if policy == "ask":
                    decision_id = create_blocking_decision(
                        connection, project_id, task_id,
                        f"Approve agent action: {name}",
                        {"action": "resume_agent_run", "run_id": run_id, "tool": name,
                         "args": args, "task_id": task_id,
                         **({"tool_screen": "untrusted"} if screen_hold else {}),
                         **bind_held_call(name, args)},
                        {}, "agent_tool_requires_approval", risk_tier=risk,
                    )
                    result = {"status": "deferred_for_approval", "decision_id": decision_id, "message": f"'{name}' (risk tier {risk}) requires human approval; the run is paused and resumes on approval."}
                    is_deferred = True
                    if screen_hold:
                        screen_hold = False
                        finalize_ctx["tool_screen_untrusted"] = False
                    deferred = {"tool": name, "args": args, "decision_id": decision_id}
                    emit_event(connection, project_id, "agent.tool_deferred", "agent_loop", "decision", decision_id, {"tool": name, "risk_tier": risk, "step": step})
                else:
                    result = execute_agent_tool(connection, project_id, settings, name, args, "agent_loop", task_id=task_id)
            else:
                result = _dispatch(connection, project_id, settings, name, args)
                emit_event(connection, project_id, "agent.tool_invoked", "agent_loop", "task", task_id or project_id, {"tool": name, "step": step})
            arrived = _origin_for_call(name, mcp_map, skill_map)
            if (
                arrived
                and name not in mcp_map
                and isinstance(result, dict)
                and not _through_receipt_door(result)
                and result.get("status") not in (
                    "deferred_for_approval", "skipped", "denied", "refused",
                    "awaiting_approval",
                )
            ):
                # A FETCHED PAGE OR DOCUMENT that did not come through call_tool_with_receipt
                # (which screens its own results) is screened here (BOSSAPP-822).
                result, _verdict = screen_result(connection, settings, project_id, result,
                                                 ref=f"{run_id}:{name}")
            if carried_untrusted(result):
                screen_hold = True
                finalize_ctx["tool_screen_untrusted"] = True
            if (
                arrived
                and isinstance(result, dict)
                and result.get("status") not in (
                    "deferred_for_approval", "skipped", "denied", "refused",
                    "awaiting_approval",
                )
            ):
                result = remember(result, origin=arrived, ledger=_untrusted, echoed=args)
                finalize_ctx["untrusted_spans"] = _untrusted.export()
            step_obj = {"step": step, "tool": name, "args": args, "result_preview": _preview_of(result), "deferred": is_deferred}
            if tc_idx == 0 and thought:
                step_obj["thought"] = thought
            transcript.append(step_obj)
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": clip_for_model(json.dumps(result), 16000)})

        if budget_wait is not None and deferred is None:
            # THE AGENT DOES NOT DECIDE THIS ONE. It would read "budget busy" as a broken tool
            # and declare the work blocked; the step instead goes back to wait, and runs again
            # once the minute has room (agent_steps.run -> workflow wait_step).
            retry_after = int(budget_wait.get("retry_after_seconds") or 30)
            _save_run(connection, run_id, project_id, task_id, BUDGET_WAIT, messages, transcript,
                      total, finalize_ctx, step=step + 1,
                      pending={"kind": BUDGET_WAIT, "budget": budget_wait})
            emit_event(connection, project_id, "agent.run_budget_wait", "agent_loop", "agent_run",
                       run_id, {"retry_after_seconds": retry_after,
                                "waited_seconds": round(waiter.waited, 1),
                                "refusal": budget_wait.get("refusal")})
            return {"status": BUDGET_WAIT, "run_id": run_id, "output_text": "",
                    "usage": total, "transcript": transcript, "budget": budget_wait,
                    "retry_after_seconds": retry_after, "message": BUDGET_BUSY_MESSAGE}

        if deferred is not None:
            _save_run(connection, run_id, project_id, task_id, "awaiting_approval", messages, transcript, total, finalize_ctx, step=step + 1, pending=deferred)
            emit_event(connection, project_id, "agent.run_suspended", "agent_loop", "agent_run", run_id, {"decision_id": deferred["decision_id"], "tool": deferred["tool"]})
            return {"status": "suspended", "run_id": run_id, "decision_id": deferred["decision_id"], "output_text": "", "usage": total, "transcript": transcript}

        _save_run(connection, run_id, project_id, task_id, "running", messages, transcript, total, finalize_ctx, step=step + 1)

    if final_text is None:
        # RUNNING OUT OF STEPS IS NOT THE SAME AS HAVING NOTHING TO SAY.
        #
        # The loop already made one last tool-free call here, but with no instruction — so a
        # model that had gathered real evidence over a dozen calls could still return nothing
        # and the operator got "(agent reached its step limit before finishing)". An
        # investigation that stops early has still LEARNED something, and the half it finished
        # is usually the half that matters.
        messages = messages + [{
            "role": "user",
            "content": (
                "You have used your tool budget for this turn. Do not apologise and do not ask "
                "to continue. Write the answer from what you have already established:\n"
                "1. What you FOUND, with the concrete evidence (ids, counts, timings).\n"
                "2. What you could NOT determine, and precisely which check would settle it.\n"
                "3. The single next action you would take.\n"
                "Partial findings are useful; a report of running out of steps is not."
            ),
        }]
        # Same transient-retry policy as the loop's own calls: the wrap-up completion is the
        # cheapest call of the run and the most expensive one to lose.
        message, usage = _with_transient_retry(
            lambda: adapter.create_chat_completion(messages, tools=None))
        add_usage(usage)
        final_text = message.get("content") or (
            "I ran out of tool budget before reaching a conclusion, and could not summarise "
            "what I had. Ask again narrowing the question to one run or one table."
        )

    _save_run(connection, run_id, project_id, task_id, "completed", messages, transcript, total, finalize_ctx, step=last_step + 1, output_text=final_text)
    try:
        from .knowledge.runtime import attribute_shown_facts

        attribute_shown_facts(
            connection, project_id=project_id, run_id=run_id, success=True
        )
    except Exception:
        pass
    emit_event(connection, project_id, "agent.run_completed", "agent_loop", "agent_run", run_id, {"steps": len(transcript)})
    return {"status": "completed", "run_id": run_id, "output_text": final_text, "usage": total, "transcript": transcript, "tool_calls": len(transcript)}


def conversation_from_messages(messages: list[dict]) -> list[dict]:
    """Transform the raw LLM `messages` array (OpenAI chat format) into clean,
    display-ready turns for the UI — the faithful agent<->LLM back-and-forth.

    Each turn is one of:
      {"role":"system","text":...}
      {"role":"user","text":...}
      {"role":"assistant","text":...,"calls":[{"name","args"}]}
      {"role":"tool","name":...,"text":...}
    """
    id_to_name: dict[str, str] = {}
    turns: list[dict] = []
    for m in messages or []:
        role = m.get("role")
        if role == "system":
            turns.append({"role": "system", "text": m.get("content") or ""})
        elif role == "user":
            turns.append({"role": "user", "text": m.get("content") or ""})
        elif role == "assistant":
            calls = []
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                name = fn.get("name")
                if tc.get("id"):
                    id_to_name[tc["id"]] = name
                raw_args = fn.get("arguments")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except Exception:
                    args = {"_raw": raw_args}
                calls.append({"name": name, "args": args})
            turns.append({"role": "assistant", "text": m.get("content") or "", "calls": calls})
        elif role == "tool":
            turns.append(
                {
                    "role": "tool",
                    "name": id_to_name.get(m.get("tool_call_id")) or "tool",
                    "text": m.get("content") or "",
                }
            )
    return turns


def _agent_purpose(connection, run_id: str, project_id: str, messages: Any,
                   finalize_ctx: dict):
    """What the router reads about an agent run: its brief (the first user message), its
    role and that role's Agent Studio model policy. Sticky: a resumed run keeps its model."""
    from .model_router.router import RouteRequest
    from .model_router.store import agent_model_policy

    step = finalize_ctx.get("agent_step") if isinstance(finalize_ctx.get("agent_step"),
                                                         dict) else {}
    role = step.get("role_key") or finalize_ctx.get("role_key") or None
    brief = str(step.get("brief") or "")
    for message in ([] if brief else (messages or [])):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            brief = content if isinstance(content, str) else json.dumps(content)[:4000]
            break
    # Recorded under the workflow run and its step when the agent works for one, so the
    # step's "Why this model" reads it; a stand-alone agent run is its own unit.
    return RouteRequest(kind="agent", step="agent", project_id=project_id,
                        unit_id=step.get("workflow_run_id") or run_id,
                        ref=step.get("step_id") or None,
                        role=role, text=str(brief)[:6000],
                        agent_policy=agent_model_policy(connection, project_id, role),
                        sticky=True)


def _key_words(value):
    from .decision_words import key_words

    return key_words(value)

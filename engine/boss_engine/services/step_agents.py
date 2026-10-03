"""Who runs a workflow step — one answer for every kind of step.

WHY THIS MODULE EXISTS
----------------------
Before it, "who runs this step" had four different answers depending on what the step was, and
none of them was written down in one place:

    built-in capability   the run's actor (e.g. `ceo`), whatever default captain the console showed
    custom skill step     the anonymous `workflow_runner`, added to the skill's allowed_callers
    agent step            a model pick — which never ran, because `choose_agent` called an
                          `adapter.complete()` no adapter has, and a bare `except` reported the
                          AttributeError as "no model was available"
    approval gate         nobody; `human` was accepted on every kind of step

and the agents a step could NAME came from a hard-coded set of 29 role keys, so an agent made on
the Agents page could never be put on a step ("names captain 'docs_writer', which is not a role
this workforce has") while five roles no project has were offered in the picker.

THE ONE RULE, in the order it is applied
-----------------------------------------
  1. The agent the step NAMES, if it names one. It must be one of the PROJECT's agents (the
     `roles` table, not a constant). `human` is accepted only on an approval gate: a person
     cannot run a step, only answer one.
  2. Otherwise the agent that HOLDS what the step runs — the skill a custom step binds, or every
     skill an agent step is scoped to. Exactly one holder is used directly; several are put to
     the model, which picks from their missions and holdings.
  3. Otherwise, where the step's own policy lets anyone call it (`*`), or it is an agent step
     with no scope, the model picks among every agent that could do it.
  4. Otherwise the step is REFUSED with the reason and what to do about it — at publish, at
     start and at run time, never by quietly widening a skill's allowed callers.

TWO DELIBERATE EXCEPTIONS, both recorded with their reason rather than hidden
-----------------------------------------------------------------------------
* A BUILT-IN capability that names nobody runs as whoever started the run. Its tool is one of
  BOSS's own and needs no grant, and all twelve catalogue workflows name nobody — moving them
  to a model-picked agent would change who acts on every Build. `ran_as.how == "run_actor"`.
* A custom step in a version published BEFORE these rules (no `step_agent_rules` stamp), that
  names nobody and binds a skill or an MCP tool no agent may run, runs as the workflow exactly
  as it did when it was published: the builder's default for such a step was "no agent — runs
  as the workflow", and refusing it now would kill runs, and starts, that worked yesterday.
  `ran_as.how == "legacy_workflow_runner"`, and `why` says which agent to give it to.
  Re-publishing the workflow moves it to the rules above, where the operator can still act.

Every answer is returned as a `ran_as` record — agent, how, why, who else was considered — and
the runner writes it onto the step's output, so the run view can say "Ran as video_editor — the
only agent holding video.trim" instead of leaving the operator to guess.

AN MCP SERVER'S REACH IS GRANTED TO AN AGENT, NEVER TO A STEP (independent review of
milestone/integration, blocker). A custom step bound to an MCP tool that nobody held used to run
as `workflow_runner`, in current versions too, because the tool's synthesised spec names the
workflow among its callers. The chat model can author such a step on its own (draft_custom_step
and draft_workflow are `safe`), publish it as a workflow's active version, and a schedule then
runs it unattended — a reviewed server's tool, with its credentials, run with nobody ever
granting that server to anyone. So the one grant rule (`agents.mcp_grant_gap`) applies to these
steps too: the step runs as an agent that holds the server, a step naming nobody is refused at
publish when no agent does, and only a version published under the old rules keeps running as
the workflow (`legacy_workflow_runner`). The same goes for a skill that calls an MCP server.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: The one participant BOSS cannot run. Only an approval gate may name it.
HUMAN = "human"

#: What the runner calls itself when a step's own policy lets the workflow call it directly.
WORKFLOW_RUNNER = "workflow_runner"

#: WHICH RULES A PUBLISHED VERSION WAS CHECKED UNDER. `publish_version` stamps every new version
#: with CURRENT_AGENT_RULES; a version without the stamp was published before this module
#: existed. Kept on the spec rather than inferred from a date, so a run reads the answer off the
#: version it is running and nothing else.
AGENT_RULES_KEY = "step_agent_rules"
CURRENT_AGENT_RULES = 2
LEGACY_AGENT_RULES = 1

_ROLE_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class StepAgentError(RuntimeError):
    """Nobody can run this step. The message says why and what to do."""


def rules_of(spec: dict[str, Any]) -> int:
    """The agent rules a spec (or a step `_run_spec` tagged) was published under."""
    try:
        return int(spec.get(AGENT_RULES_KEY) or LEGACY_AGENT_RULES)
    except (TypeError, ValueError):
        return LEGACY_AGENT_RULES


def under_current_rules(spec: dict[str, Any]) -> bool:
    """For a whole SPEC: an unstamped spec was published before these rules."""
    return rules_of(spec) >= CURRENT_AGENT_RULES


def tag_steps(spec: dict[str, Any]) -> dict[str, Any]:
    """Copy the version's rules onto each step, so the runner — which is handed one step, not
    the version — can read them. Overwrites whatever a step says: only the version's stamp,
    written by `publish_version`, decides."""
    rules = rules_of(spec)
    for step in spec.get("steps") or []:
        if isinstance(step, dict):
            step[AGENT_RULES_KEY] = rules
    return spec


def ran_under_old_rules(step: dict[str, Any]) -> bool:
    """For one STEP: true only when `tag_steps` said so. An untagged step — one built in code, or
    a test's — is under the current rules, so nothing reaches the legacy path by omission."""
    value = step.get(AGENT_RULES_KEY)
    return value is not None and rules_of(step) < CURRENT_AGENT_RULES


# ── Reading the step ─────────────────────────────────────────────────────────────────────────


def named_agent(step: dict[str, Any]) -> str:
    """The agent a step names, normalised the way validation and execution both read it.

    `binding.agent` is read as a fallback on an agent step, for specs written by hand before
    `captain` became the one field for every kind.
    """
    binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
    raw = step.get("captain")
    if not raw and binding.get("agent_work"):
        raw = binding.get("agent")
    return str(raw or "").strip().lower()


def is_gate(step: dict[str, Any]) -> bool:
    """A step whose whole purpose is that somebody answers it."""
    if str(step.get("kind") or "") == "decision":
        return True
    binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
    key = str(binding.get("capability") or "")
    if not key:
        return False
    from .workflow_capabilities import CAPABILITY_KEYS, get

    return key in CAPABILITY_KEYS and bool(get(key).is_gate)


def step_scope(step: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The skills and MCP servers an agent step is limited to, as written on the step.

    `mcp_servers` is accepted beside `mcp` because both spellings appear in the map of what
    operators and the drafting agent write; one is normalised to the other on read.
    """
    binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
    skills = [str(s).strip() for s in (binding.get("skills") or []) if str(s).strip()]
    mcp = [str(s).strip() for s in (binding.get("mcp") or binding.get("mcp_servers") or [])
           if str(s).strip()]
    return list(dict.fromkeys(skills)), list(dict.fromkeys(mcp))


def captain_shape_problems(step: dict[str, Any]) -> list[str]:
    """What is wrong with the agent a step names, that needs no project to see."""
    step_id = step.get("id", "?")
    named = named_agent(step)
    if named == HUMAN and not is_gate(step):
        return [
            f"step '{step_id}' names 'human', but a person cannot run a step — only an "
            "approval gate waits for a person. Name one of the project's agents, or leave it "
            "empty so BOSS picks the agent that holds what the step needs."]
    if named and not _ROLE_KEY.fullmatch(named):
        return [
            f"step '{step_id}' names agent '{named}', which is not an agent key (lower case "
            "letters, digits and underscores). Pick the agent from the list instead of typing it."]
    return []


def shape_problems(step: dict[str, Any]) -> list[str]:
    """What is wrong with a step's agent fields that needs no project to see.

    Called from `validate_workflow_spec`, which has no connection. Whether the agent EXISTS is
    a fact about the project and is checked in `assignment_problems`.
    """
    return captain_shape_problems(step) + [message for _, message in scope_shape_problems(step)]


def scope_shape_problems(step: dict[str, Any]) -> list[tuple[str, str]]:
    """What is wrong with a step's skills/MCP lists, as (field, sentence) pairs.

    Paired with the field so the workflow editor can put the sentence on the control that
    caused it; `shape_problems` is the same list as plain sentences.
    """
    step_id = step.get("id", "?")
    problems: list[tuple[str, str]] = []
    binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
    for field in ("skills", "mcp", "mcp_servers"):
        value = binding.get(field)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            problems.append((field, f"step '{step_id}' {field} must be a list of names."))
        elif not binding.get("agent_work"):
            # A custom step runs exactly ONE skill or tool, and a built-in runs its own. A list
            # here would be stored and ignored, which is the failure this whole change removes.
            problems.append((field,
                f"step '{step_id}' lists {field}, but only an agent step can be given a set "
                "of skills or MCP servers. A custom step runs the one skill or tool it binds."))
    return problems


# ── The project's agents ─────────────────────────────────────────────────────────────────────


def _agent_rows(connection: Connection, project_id: str) -> list[Any]:
    """THE LIST OF AGENTS: every `roles` row but the person. The step picker and the Agents
    roster both read it, so the roster cannot show five agents while the picker offers 34."""
    rows = connection.execute(
        text("SELECT role_key, display_name, spec FROM roles WHERE project_id = :p "
             "ORDER BY role_key"),
        {"p": project_id},
    ).mappings().all()
    return [row for row in rows if str(row["role_key"]) != HUMAN]


def agent_keys(connection: Connection, project_id: str) -> list[str]:
    """The role keys `project_agents` offers, without reading what each holds."""
    return [str(row["role_key"]) for row in _agent_rows(connection, project_id)]


def project_agents(connection: Connection, project_id: str,
                   settings: Any = None) -> list[dict[str, Any]]:
    """Every agent this project has, with what it is for and what it holds.

    THE ROLES TABLE, NOT A CONSTANT. An agent made on the Agents page is a `roles` row; the
    old picker read a frozenset and could never offer it.

    `holds` is what an agent step can actually OFFER the agent — read through
    `agent_steps.holdings`, the same function the runtime uses. It was raw `role_skills` here,
    so a step scoped to an authored skill published cleanly and failed at run time with the
    false "<agent> does not hold X". `granted` is the raw grant, which is what a custom step's
    permission gate checks; `unrunnable` is the difference, kept so a refusal can say so.
    """
    from .agent_steps import holdings as read_holdings

    rows = _agent_rows(connection, project_id)
    held = read_holdings(connection, settings, project_id)
    out: list[dict[str, Any]] = []
    for row in rows:
        role = str(row["role_key"])
        spec = row["spec"] if isinstance(row["spec"], dict) else {}
        out.append({
            "role_key": role,
            "name": row["display_name"] or role.replace("_", " "),
            "mission": str((spec or {}).get("mission") or ""),
            "holds": list((held.get(role) or {}).get("runnable") or []),
            "granted": list((held.get(role) or {}).get("granted") or []),
            "unrunnable": list((held.get(role) or {}).get("unrunnable") or []),
            # The MCP servers it holds (role_mcp_servers), by name and id.
            "mcp": list((held.get(role) or {}).get("mcp") or []),
            "mcp_ids": list((held.get(role) or {}).get("mcp_ids") or []),
        })
    return out


def _listed(agents: Iterable[dict[str, Any]], limit: int = 8) -> str:
    keys = [a["role_key"] for a in agents]
    if not keys:
        return "none yet — create one on the Agents page"
    return ", ".join(keys[:limit]) + (", …" if len(keys) > limit else "")


def _allowed_callers(connection: Connection, project_id: str, skill_id: str) -> set[str]:
    from .workflow_custom_steps import _allowed_callers as read

    return read(connection, project_id, skill_id)


def mcp_skill_id(server_id: str, tool_name: str) -> str:
    """The id an MCP tool is governed under — the same one `agent_loop` synthesises."""
    from .agent_loop import _slug

    return f"mcp.{server_id}.{_slug(tool_name)}"


def who_may_run(
    connection: Connection,
    project_id: str,
    definition: dict[str, Any],
    agents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str, str]:
    """The agents allowed to run one custom step, and how they came to be allowed.

    Returns (pool, basis, fallback):
      basis     "holds" | "listed" | "anyone" | ""   — why the pool is what it is
      fallback  WORKFLOW_RUNNER when the binding's OWN policy names the workflow as a caller
                and it reaches no MCP server, so an empty pool is still runnable without
                widening anything; "" otherwise — always "" for an MCP tool or a skill that
                calls one, because the workflow holds no server.

    The order matches `skill_runtime`'s permission gate exactly: a `role_skills` grant, or the
    skill naming the caller in allowed_callers. Picking an agent the gate would then deny is
    the failure this is here to prevent.
    """
    if definition.get("binding_kind") == "mcp_tool":
        # HOLDING THE SERVER, FOR THIS TOOL (role_mcp_servers through agents.holds_mcp_tool) is
        # the one way an agent holds an MCP tool — the same rule the runtime gate applies. There
        # is NO workflow fallback: the step used to run as `workflow_runner` whenever nobody
        # held the server, which let a step chat authored by itself reach a server nobody was
        # given (see the module docstring). Only a version published under the old rules still
        # runs that way, and `resolve` decides that from the step, not from here.
        holders = [a for a in agents
                   if _holds_mcp_tool(connection, project_id, a["role_key"], definition)]
        return holders, ("holds" if holders else ""), ""
    skill_id = str(definition.get("skill_id") or "")
    holders = [a for a in agents if skill_id in _granted(a)]
    if holders:
        # AN MCP-CALLING SKILL ALSO NEEDS ITS SERVER (agents.mcp_reach_gap — the gate the
        # runtime applies). Picking a holder the gate then denies is the failure this function
        # exists to prevent, so a holder without the server is not in the pool.
        holders = [a for a in holders if not _lacks_server(connection, project_id,
                                                           a["role_key"], skill_id)]
        if holders:
            return holders, "holds", ""
        return [], "", ""
    callers = _allowed_callers(connection, project_id, skill_id)
    # A SKILL THAT CALLS AN MCP SERVER never runs as the workflow, whatever its callers say: the
    # workflow holds no server (the one grant rule), and the runtime gate would deny it.
    reaches = _skill_server(connection, project_id, skill_id)
    fallback = WORKFLOW_RUNNER if WORKFLOW_RUNNER in callers and not reaches else ""
    if "*" in callers:
        pool = list(agents)
        basis = "anyone"
    else:
        pool = [a for a in agents if a["role_key"] in callers]
        basis = "listed"
    if reaches:
        pool = [a for a in pool
                if not _lacks_server(connection, project_id, a["role_key"], skill_id)]
    return pool, (basis if pool else ""), fallback


def _holds_mcp_tool(connection: Connection, project_id: str, role_key: str,
                    definition: dict[str, Any]) -> bool:
    from .agents import holds_mcp_tool

    try:
        return holds_mcp_tool(connection, project_id, role_key,
                              str(definition.get("mcp_server_id") or ""),
                              str(definition.get("mcp_tool_name") or ""))
    except Exception:  # noqa: BLE001 — a missing table is "holds nothing", not a crash
        return False


def _skill_server(connection: Connection, project_id: str, skill_id: str) -> str:
    """The MCP server a skill calls, or "" — read from the catalogue the runtime gate reads."""
    from .skill_catalogue import entry

    try:
        item = entry(connection, project_id, skill_id)
    except Exception:  # noqa: BLE001 — cannot read the catalogue: treat as no server
        return ""
    policy = ((item or {}).get("spec") or {}).get("policy") or {}
    return str(policy.get("mcp_server_id") or "")


def _server_name(connection: Connection, project_id: str, server_id: str) -> str:
    row = connection.execute(
        text("SELECT name FROM mcp_servers WHERE project_id = :p AND id = :s"),
        {"p": project_id, "s": server_id},
    ).first()
    return str(row[0]) if row else server_id


def custom_step_runs(definition: dict[str, Any], connection: Optional[Connection] = None,
                     project_id: str = "") -> str:
    """What a custom step runs, in the words a refusal uses."""
    if definition.get("binding_kind") == "mcp_tool":
        server = str(definition.get("mcp_server_id") or "")
        if connection is not None:
            server = _server_name(connection, project_id, server)
        return f"'{definition.get('mcp_tool_name')}' on MCP server '{server}'"
    return str(definition.get("skill_id") or "")


def nobody_may_run_problem(connection: Connection, project_id: str,
                           definition: dict[str, Any]) -> str:
    """Why no agent may run a custom step that names nobody. Publish, start and run say this."""
    label = definition.get("label")
    if definition.get("binding_kind") == "mcp_tool":
        server = _server_name(connection, project_id, str(definition.get("mcp_server_id") or ""))
        return (f"'{label}' calls '{definition.get('mcp_tool_name')}' on MCP server '{server}', "
                f"and no agent holds MCP server '{server}', so nobody can do this step. Give "
                "it to an agent on the Agents page (Give this agent an MCP server), or name an "
                "agent that holds it on the step.")
    skill_id = str(definition.get("skill_id") or "")
    # A HOLDER WITHOUT THE SERVER is a different repair from "nobody holds it", so it is named:
    # the agent and the server it still needs.
    for agent in project_agents(connection, project_id):
        if skill_id in _granted(agent):
            short = _lacks_server(connection, project_id, agent["role_key"], skill_id)
            if short:
                return f"nobody can do '{label}': {short}"
    return (f"'{label}' runs {skill_id}, and no agent on this project may run it, so nobody can "
            f"do this step. Give {skill_id} to an agent on the Agents page, or name an agent "
            "that holds it on the step.")


def named_agent_problem(connection: Connection, project_id: str, definition: dict[str, Any],
                        named: str, agents: list[dict[str, Any]]) -> Optional[str]:
    """Why the agent a custom step NAMES may not run what the step binds, or None.

    Review of milestone/integration: a named agent was never checked against the binding, so a
    step naming an agent without the server (or the skill) published cleanly and failed at run
    time with the raw gate string "caller 'x' not in allowed_callers [...]". This asks what
    the runtime gate asks — `who_may_run`, the same pool the auto-pick uses — and says it in
    the gate's own words (agents.mcp_grant_gap), which name the Agents page.
    """
    label = definition.get("label")
    if not any(a["role_key"] == named for a in agents):
        return (f"names agent '{named}', which this project does not have, so nothing on it "
                f"holds what '{label}' runs. Its agents are: {_listed(agents)}. Pick one of "
                f"them, or create '{named}' on the Agents page and give it what the step runs.")
    pool, _, _ = who_may_run(connection, project_id, definition, agents)
    if any(a["role_key"] == named for a in pool):
        return None
    if definition.get("binding_kind") == "mcp_tool":
        from .agents import mcp_grant_gap

        tool = str(definition.get("mcp_tool_name") or "")
        server_id = str(definition.get("mcp_server_id") or "")
        gap = mcp_grant_gap(connection, project_id, named, server_id=server_id, tool=tool,
                            skill_key=mcp_skill_id(server_id, tool))
        if gap:
            return gap["message"]
        return None  # the server is gone: `defined_step_problems` says that in its own words
    skill_id = str(definition.get("skill_id") or "")
    short = _lacks_server(connection, project_id, named, skill_id)
    if short:
        return short
    return (f"{named} may not run {skill_id}: it does not hold it and the skill does not list "
            f"it as a caller. Give {skill_id} to {named} on the Agents page, or name an agent "
            "that holds it.")


def _lacks_server(connection: Connection, project_id: str, role_key: str,
                  skill_id: str) -> Optional[str]:
    """Why this holder still may not run the skill (it lacks the MCP server it calls), or None."""
    from .agents import mcp_reach_gap

    try:
        gap = mcp_reach_gap(connection, project_id, role_key, skill_id)
    except Exception:  # noqa: BLE001 — a missing table is "no MCP skills", not a crash
        return None
    return gap["message"] if gap else None


def _granted(agent: dict[str, Any]) -> list[str]:
    """The raw grant — what `skill_runtime`'s permission gate cashes for a custom step."""
    return list(agent.get("granted") if agent.get("granted") is not None
                else agent.get("holds") or [])


def _mcp_servers(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    from .mcp_registry import list_mcp_servers

    try:
        return list_mcp_servers(connection, project_id)
    except Exception:  # noqa: BLE001 — a missing table is "no servers", not a crash
        return []


def resolve_mcp(connection: Connection, project_id: str,
                wanted: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """The MCP servers a step names, by id or by name, and the names that matched nothing."""
    servers = _mcp_servers(connection, project_id)
    found: list[dict[str, Any]] = []
    missing: list[str] = []
    for name in wanted:
        match = next((s for s in servers
                      if str(s.get("id")) == name
                      or str(s.get("name") or "").strip().lower() == name.lower()), None)
        if match is None:
            missing.append(name)
        elif match not in found:
            found.append(match)
    return found, missing


def _skill_known(connection: Connection, project_id: str, skill_id: str) -> bool:
    from .skill_definitions import _skill_exists

    try:
        return _skill_exists(connection, project_id, skill_id) is not None
    except Exception:  # noqa: BLE001 — cannot judge, so do not refuse on it
        return True


def _agent_work_pool(step: dict[str, Any], agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Who could take an agent step that names nobody."""
    binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
    skills, mcp = step_scope(step)
    only = {str(a).strip().lower() for a in (binding.get("agents") or []) if str(a).strip()}
    pool = [a for a in agents if not only or a["role_key"] in only]
    if skills:
        # Every skill the step is scoped to, held by ONE agent. A step that needs trim and
        # publish is not done by an editor that can only trim.
        return [a for a in pool if set(skills) <= set(a["holds"])]
    if mcp:
        # Every server the step names, HELD by the agent: a step narrows an agent's grants and
        # never hands it a server (role_mcp_servers is the grant).
        return [a for a in pool
                if {m.lower() for m in mcp} <= ({str(n).lower() for n in a.get("mcp") or []}
                                                | {str(i) for i in a.get("mcp_ids") or []})]
    return [a for a in pool if a["holds"] or a.get("mcp")]


def _no_pool_problem(skills: list[str], agents: list[dict[str, Any]]) -> str:
    """Why nobody can take an agent step that names nobody. Publish and run say the same."""
    if skills:
        from .agent_steps import scope_problem

        # Somebody HOLDS all of them but cannot use them here: say that, not "nobody holds".
        for agent in agents:
            if set(skills) <= set(_granted(agent)):
                problem = scope_problem(agent["role_key"], skills, {
                    "runnable": agent.get("holds") or [],
                    "unrunnable": agent.get("unrunnable") or []})
                if problem:
                    return problem
        return (f"no agent holds all of {', '.join(skills)}, so nobody can do this step. Give "
                "those skills to one agent on the Agents page, or name the agent on the step.")
    return ("no agent on this project holds a skill it can use or an MCP server, so there is "
            "nobody to give this step to. Open an agent and give it what this work needs.")


# ── Publish- and start-time checks ───────────────────────────────────────────────────────────


def assignment_problems(connection: Connection, project_id: str,
                        spec: dict[str, Any], *, legacy: bool = False) -> list[str]:
    """Every step whose agent cannot be settled, as sentences an operator can act on.

    Run at publish and again at start, because both are moments the operator can still do
    something — and an agent can be deleted, or a grant taken away, between the two.

    `legacy=True` is for STARTING a version published before these rules (see `rules_of`); it
    defaults off, so a draft or a publish can never fall into it by omission. Such a version is
    started under the rules it was published under for the two things those rules allowed: a
    built-in step naming an agent the project does not have (it ran as that name), and a custom
    step nobody may run (it ran as the workflow). Everything an agent step needs is checked
    either way — that failed at run time under the old rules too, only later.
    """
    from .agent_steps import empty_handed_problem, mcp_scope_problem, scope_problem

    from .workflows import project_roster

    agents = project_agents(connection, project_id)
    by_key = {a["role_key"]: a for a in agents}
    # WHO MAY BE NAMED is the project's roster (agents stream): its agents plus the bundled
    # roles it has not deleted, the same list validate_captain checks at publish. A bundled role
    # the project never installed may be named on a built-in step, as before; it holds nothing,
    # so an agent step given to it is refused below as empty-handed.
    roster = project_roster(connection, project_id)
    problems: list[str] = []
    for step in spec.get("steps") or []:
        step_id = step.get("id", "?")
        binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
        named = named_agent(step)
        if legacy and not binding.get("agent_work"):
            continue
        if named and named != HUMAN and named not in roster:
            problems.append(
                f"step '{step_id}' names agent '{named}', which this project does not have. "
                f"Its agents are: {_listed(agents)}. Pick one of them, or create '{named}' on "
                "the Agents page first.")
            continue
        if step.get("kind") != "capability":
            continue
        if binding.get("agent_work"):
            skills, mcp = step_scope(step)
            unknown = [s for s in skills if not _skill_known(connection, project_id, s)]
            if unknown:
                problems.append(
                    f"step '{step_id}' is scoped to {', '.join(unknown)}, which this project "
                    "has no skill called. Check the spelling on the Skills page, or take it off "
                    "the step.")
            _, missing_mcp = resolve_mcp(connection, project_id, mcp)
            if missing_mcp:
                problems.append(
                    f"step '{step_id}' names MCP server {', '.join(missing_mcp)}, which this "
                    "project has not registered. Add it under Skills and MCP, or take it off "
                    "the step.")
            if unknown or missing_mcp:
                continue
            # THE SAME SENTENCES `agent_steps.run` RAISES, from the same holdings, so a step this
            # accepts is one the runtime will run, and one it refuses is refused for the reason
            # the runtime would give.
            if named:
                agent = by_key.get(named) or {"role_key": named, "holds": [], "unrunnable": [],
                                              "mcp": [], "mcp_ids": []}
                held = {"runnable": agent["holds"], "unrunnable": agent.get("unrunnable") or []}
                if skills:
                    problem = scope_problem(named, skills, held)
                    if problem:
                        problems.append(f"step '{step_id}': {problem}")
                if mcp:
                    problem = mcp_scope_problem(connection, project_id, named, mcp)
                    if problem:
                        problems.append(f"step '{step_id}': {problem}")
                elif not skills and not agent["holds"] and not agent.get("mcp"):
                    problems.append(f"step '{step_id}': {empty_handed_problem(named, held)}")
            elif not _agent_work_pool(step, agents):
                if mcp and not skills:
                    problems.append(
                        f"step '{step_id}': no agent holds MCP server {', '.join(mcp)}, so "
                        "nobody can do this step. Give it to an agent on the Agents page, or "
                        "take it off the step.")
                else:
                    problems.append(f"step '{step_id}': {_no_pool_problem(skills, agents)}")
            continue
        if binding.get("custom_step") and named != HUMAN:
            from . import workflow_custom_steps as custom_steps

            definition = custom_steps.get(connection, project_id, str(binding["custom_step"]))
            if definition is None:
                continue  # `defined_step_problems` names a missing step better than this can
            if named:
                # THE NAMED AGENT IS CHECKED AGAINST WHAT THE STEP RUNS, as the runtime gate
                # will check it — so a step that publishes is a step that runs.
                problem = named_agent_problem(connection, project_id, definition, named, agents)
                if problem:
                    problems.append(f"step '{step_id}' {problem}"
                                    if problem.startswith("names ")
                                    else f"step '{step_id}': {problem}")
                continue
            pool, _, fallback = who_may_run(connection, project_id, definition, agents)
            if not pool and not fallback:
                problems.append(f"step '{step_id}': "
                                f"{nobody_may_run_problem(connection, project_id, definition)}")
    return problems


# ── Run time ─────────────────────────────────────────────────────────────────────────────────


#: The last answer `resolve` gave on this thread. A step that fails AFTER it was given to an
#: agent (a model outage, a refused skill) raises before its output is written, and the ledger
#: would otherwise have to sign the failure to nobody. The runner reads this in its failure
#: path and hands it to agent_acts; it clears it before every step so it can never be stale.
_LAST_RESOLVED: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "step_agents_last_resolved", default=None)


def last_resolved() -> Optional[dict[str, Any]]:
    return _LAST_RESOLVED.get()


def forget_resolved() -> None:
    _LAST_RESOLVED.set(None)


def _record(agent: str, how: str, why: str, considered: Iterable[str] = ()) -> dict[str, Any]:
    record = {"agent": agent, "how": how, "why": why, "considered": list(considered)}
    _LAST_RESOLVED.set(dict(record))
    return record


def _choose(connection: Connection, settings: Any, project_id: str,
            pool: list[dict[str, Any]], instructions: str, *, single_why: str,
            single_how: str) -> dict[str, Any]:
    if len(pool) == 1:
        return _record(pool[0]["role_key"], single_how, single_why, [pool[0]["role_key"]])
    from .agent_steps import choose_agent

    picked = choose_agent(connection, settings, project_id,
                          instructions=instructions, pool=pool)
    return _record(picked["role_key"], picked["how"], picked["why"],
                   [a["role_key"] for a in pool])


def resolve(
    connection: Connection,
    settings: Any,
    project_id: str,
    step: dict[str, Any],
    *,
    actor: str,
    instructions: str = "",
) -> dict[str, Any]:
    """Who runs this step, how that was decided and why. Raises StepAgentError when nobody can.

    The returned record is what the run view shows, so `why` is written for the operator: what
    was true of this project that made this the answer.
    """
    binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
    named = named_agent(step)
    if named:
        return _record(named, "named", "named on the step", [named])

    if binding.get("agent_work"):
        agents = project_agents(connection, project_id)
        pool = _agent_work_pool(step, agents)
        skills, mcp = step_scope(step)
        if not pool:
            raise StepAgentError(
                f"no agent holds MCP server {', '.join(mcp)}, so nobody can do this step. Give "
                "it to an agent on the Agents page, or take it off the step."
                if mcp and not skills else _no_pool_problem(skills, agents))
        if skills or mcp:
            return _choose(connection, settings, project_id, pool, instructions,
                           single_how="only_holder",
                           single_why=f"the only agent holding {', '.join(skills + mcp)}")
        return _choose(connection, settings, project_id, pool, instructions,
                       single_how="only_candidate",
                       single_why="the only agent holding any skill")

    if binding.get("custom_step"):
        from . import workflow_custom_steps as custom_steps

        definition = custom_steps.get(connection, project_id, str(binding["custom_step"]))
        if definition is None:
            raise StepAgentError(
                f"this project has no custom step called '{binding['custom_step']}'.")
        agents = project_agents(connection, project_id)
        pool, basis, fallback = who_may_run(connection, project_id, definition, agents)
        runs = custom_step_runs(definition, connection, project_id)
        brief = " ".join(str(x) for x in (definition.get("label"), definition.get("summary"),
                                          runs) if x)
        if pool:
            single_why = {
                "holds": f"the only agent holding {runs}",
                "listed": f"the only agent {runs} lists as a caller",
                "anyone": "the only agent on this project",
            }.get(basis, f"the only agent that may run {runs}")
            return _choose(connection, settings, project_id, pool, instructions or brief,
                           single_how="only_holder", single_why=single_why)
        if fallback:
            return _record(
                WORKFLOW_RUNNER, "workflow_runner",
                f"no agent holds {runs}; its own policy lets the workflow call it, so it ran as "
                "the workflow. Give it to an agent on the Agents page to have that agent run it.")
        if ran_under_old_rules(step):
            # PUBLISHED UNDER THE OLD RULES, where "no agent — runs as the workflow" was the
            # builder's default for this step, and an MCP tool nobody held always ran that way.
            # It keeps running that way (see the module docstring); the reason tells the
            # operator how to move it to an agent.
            return _record(
                WORKFLOW_RUNNER, "legacy_workflow_runner",
                f"no agent holds {runs}. This workflow was published before a step needed an "
                f"agent holding what it runs, so it ran as the workflow, as it always has. Give "
                f"{runs} to an agent on the Agents page and publish the workflow again to have "
                "that agent run it — a new version is refused until one holds it.")
        raise StepAgentError(nobody_may_run_problem(connection, project_id, definition))

    # A built-in capability, a typed decision or a gate: BOSS's own machinery, no grant needed.
    return _record(
        actor, "run_actor",
        f"a built-in step runs BOSS's own tool, which needs no grant; no agent is named, so it "
        f"acts as {actor}, who started the run", [actor])

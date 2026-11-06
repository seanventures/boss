"""Can this workflow run HERE — asked before anything is spent, and answered with the fix.

WHY THIS EXISTS
---------------
A template that needs a repository, somewhere to deploy, a mail server or a configured tracker
started anyway and found out hours later. On boss-1 (2026-10-02) every catalogue template but
one ends in `deploy`, the install had no deployment host and no target, and nothing checked:
a run would pay for research, a panel, a build and a launch-readiness repair, wait for a person
to approve the release, and only then fail at the last step. The examples were the same story
one level down — the newsletter, the sales signal and the trend script all end in an email and
the install had no SMTP; meeting-to-tickets ends in a tracker and every tracker there was a
seeded row with an empty configuration; the video example names two agents that did not exist.

`workflow_capabilities.project_requirements` already knew some of this and was only ever used
to print a chip on a catalogue card. This module is the one place that turns a workflow's spec
into the list of things it needs from THIS project, checks each one, and says what is missing
with the place to fix it. Every door that starts work asks it (`refuse_unless_ready`), and the
console's start form shows the same answer before the operator presses anything.

WHAT A NEED IS
--------------
A key (`repository`, `model`, `deployment_target`, `email`, `mattermost`, `work_tracker`,
`google`, `agent_pool`, `role:<key>`, `channel:<value>`), the steps that need it, whether it is
there, and what to do when it is not. The first step that needs it and whether the run stops
for a person before reaching it are reported too, so the operator can decide to start now and
add the deployment host while the panel runs — a choice they make out loud
(`start_anyway`), never one made for them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import workflow_capabilities as caps


@dataclass(frozen=True)
class Need:
    key: str
    #: What it is, as the thing an operator would go and get.
    label: str
    #: What to do, as an instruction — the sentence printed under "missing".
    fix: str
    #: The console area that fixes it, when there is one. `None` means the fix is outside the
    #: console (an environment variable, an API call) and the sentence says how.
    area: Optional[str] = None
    #: Where in that area: `focus=repository` opens the row, `ability=email` the drawer.
    params: Optional[tuple[tuple[str, str], ...]] = None

    @property
    def href(self) -> Optional[str]:
        if not self.area:
            return None
        from urllib.parse import urlencode

        return "/?" + urlencode([("area", self.area), *(self.params or ())])


#: Every kind of need there is. A requirement derived from a spec that is not in here (other
#: than the per-role and per-channel ones) is a defect `test_every_template_says_what_it_needs`
#: catches.
NEEDS: dict[str, Need] = {
    "repository": Need(
        "repository", "A GitHub repository BOSS can push to",
        "Connect in Repo and models.", "setup", (("focus", "repository"),)),
    "model": Need(
        "model", "A connected model",
        "Connect in Repo and models.", "setup", (("focus", "models"),)),
    "deployment_target": Need(
        "deployment_target",
        "Somewhere to deploy: a verified deployment host or a stack target",
        "Add a host in Infra → Fleet (+ Add → Host), then Place app on it; or create a stack "
        "target for a repository that ships a boss.deploy.yaml. The release step then puts the "
        "app there; until there is one, the deploy step cannot run.",
        # Lands on Infra › Fleet with the App target sheet open (the Host sheet when there is no
        # host yet): the place the target is made, not a page that describes it.
        "infra", (("view", "fleet"), ("add", "target"))),
    # The fix names a console surface, not an API call: the Email (SMTP) connector in Skills & MCP
    # (the Kit; skills-mcp spec §5.1), the one place mail is set up. The checklist links there.
    "email": Need(
        "email", "Outgoing email (SMTP)",
        "Connect Email (SMTP) in Skills & MCP — one URL such as "
        "smtps://user:key@smtp.example.com:465 (or the separate fields) and a From address on "
        "your own domain. Save & test sends a test message.", "skills",
        (("ability", "email"),)),
    "mattermost": Need(
        "mattermost", "A Mattermost bot BOSS can post as",
        "Connect Mattermost in Skills & MCP → Team chat — the server URL, a bot token and the "
        "channel. Save & test posts a test message there.", "skills"),
    "work_tracker": Need(
        "work_tracker", "A connected work tracker",
        "Connect GitHub Issues, Linear, Jira, Plane or Azure DevOps in Skills & MCP → File "
        "tickets. A tracker listed but not connected cannot create tickets.", "skills"),
    "google": Need(
        "google", "A Google connection (Drive and YouTube)",
        "Connect your Google account in Skills & MCP → YouTube.", "skills"),
    # A DRIVE LINK, NOT AN EDIT. Editing keeps its files in BOSS's own artifact storage, so a
    # step that edits a video from a direct link needs no Google at all; one handed a Drive link
    # (or told to write to Drive) does. Its own key, so the checklist says WHY Google is wanted
    # and that a direct link is the way round it.
    "google_drive": Need(
        "google_drive", "Google (Drive link)",
        "This run reads or writes a Google Drive file. Connect your Google account in Skills "
        "and MCP → Channels — or give a direct HTTPS link to the file instead, which needs no "
        "connection.", "skills"),
    # NOT A THING TO CONNECT — A STATE OF THE PROJECT. Every product run's jobs pass the job
    # gate, which holds non-P0 work while the budget is red. On boss-1 Start said nothing, and
    # three research runs then sat queued under "no worker has picked it up" (2026-10-03).
    "budget": Need(
        "budget", "Budget is red — work will be held",
        "Wait for the cap's period to reset (a daily cap at 00:00 UTC) or raise the cap in "
        "Govern › Limits. Calls on a subscription cost no money and do not count against it; only "
        "metered API spend does.", "budget"),
    "agent_pool": Need(
        "agent_pool", "An agent that holds at least one skill",
        "Give an agent a skill in Agent Studio. A step that lets BOSS pick the agent can only "
        "pick from agents that hold something to work with.", "workforce"),
}


def _role_need(role_key: str) -> Need:
    return Need(
        f"role:{role_key}", f"The agent '{role_key}'",
        f"This workflow names the agent '{role_key}', and this project does not have it. "
        f"Install the pack that ships it (POST /v1/packs/install), add it in Agent Studio "
        f"(+ New agent), or re-run the example's setup script, which creates the agents it "
        f"names.",
        "workforce")


def _channel_need(value: str) -> Need:
    return Need(
        f"channel:{value}", f"A channel BOSS can post to ({value!r})",
        "Give the notify step a channel written as email:someone@example.com or "
        "mattermost:<channel id>.", None)


def _provider_need(key: str) -> Need:
    from .model_routing import requirement_for_need

    req = requirement_for_need(key)
    if req is None:
        raise KeyError(key)
    return Need(key, req.label, req.fix, "setup", (("add", req.secret_provider),))


def need_for(key: str) -> Need:
    if key.startswith("provider:"):
        return _provider_need(key)
    if key.startswith("role:"):
        return _role_need(key.split(":", 1)[1])
    if key.startswith("channel:"):
        return _channel_need(key.split(":", 1)[1])
    return NEEDS[key]


# ── What a spec needs ──────────────────────────────────────────────────────────────────────────

#: Skills whose SPEC says what they need is the source of truth (`policy.requires_connection`,
#: `policy.requires_capabilities`, `kind`). These are the facts no policy field states yet, kept
#: beside the derivation so the two cannot be read separately.
_SKILL_PREFIX_NEEDS: tuple[tuple[str, str], ...] = (
    ("email.send", "email"),
    ("mattermost.post_real", "mattermost"),
)

#: Skill kinds that call a model to do their work.
_MODEL_KINDS = frozenset({"model", "analysis"})

#: `requires_connection` values, as the connection that grants them. YouTube and Drive are one
#: Google connection (`media_providers.PROVIDERS['google']`), so both resolve to `google`.
_CONNECTION_NEEDS = {"google": "google", "youtube": "google", "drive": "google"}


@lru_cache(maxsize=4)
def _skill_specs(root: str) -> dict[str, dict[str, Any]]:
    import yaml

    out: dict[str, dict[str, Any]] = {}
    for path in sorted((Path(root) / "skills").glob("*.yaml")):
        try:
            payload = yaml.safe_load(path.read_text()) or {}
        except Exception:  # noqa: BLE001 — one unreadable file must not hide every other need
            continue
        for skill in payload.get("skills") or []:
            if isinstance(skill, dict) and skill.get("id"):
                out[str(skill["id"])] = skill
    return out


def skill_needs(skill_id: str, spec: Optional[Mapping[str, Any]]) -> set[str]:
    """What running this skill needs from a project, read from the skill's own spec."""
    needed: set[str] = set()
    for prefix, need in _SKILL_PREFIX_NEEDS:
        if skill_id.startswith(prefix):
            needed.add(need)
    spec = spec or {}
    policy = spec.get("policy") if isinstance(spec.get("policy"), Mapping) else {}
    connection = str(policy.get("requires_connection") or "").strip().lower()
    if connection in _CONNECTION_NEEDS:
        needed.add(_CONNECTION_NEEDS[connection])
    required_caps = policy.get("requires_capabilities") or []
    if isinstance(required_caps, list) and any(
            str(c).startswith("work_tracker.") for c in required_caps):
        needed.add("work_tracker")
    if str(spec.get("kind") or "") in _MODEL_KINDS or skill_id.startswith("model."):
        needed.add("model")
    needed.update(_provider_needs(skill_id, spec))
    return needed


def _provider_needs(skill_id: str, spec: Optional[Mapping[str, Any]]) -> set[str]:
    """What this skill declared it needs from ONE provider (image generation, speech-to-text),
    beyond the chat model the project's routing already supplies. See model_routing."""
    from . import skill_executors  # noqa: F401 — registering the executors is the declaration
    from .model_routing import declared_requirements

    return {r.need_key for r in declared_requirements(skill_id, dict(spec or {}))}


def tool_provider_needs(tool_name: str) -> set[str]:
    """What a built-in agent tool declared it needs beyond a chat model
    (model_routing.TOOL_REQUIREMENTS), as readiness need keys."""
    from .model_routing import TOOL_REQUIREMENTS, requirement

    return {requirement(k).need_key for k in TOOL_REQUIREMENTS.get(tool_name.strip(), ())}


def _agent_step_skills(step: Mapping[str, Any], connection: Optional[Connection],
                       project_id: str) -> list[str]:
    """The skills an agent step can reach: the ones it is scoped to, else what its agent holds."""
    from .step_agents import step_scope

    scoped, _mcp = step_scope(dict(step))
    if scoped:
        return scoped
    binding = step.get("binding") or {}
    captain = step.get("captain")
    agent = "" if isinstance(captain, dict) else str(captain or binding.get("agent") or "").strip()
    if not agent or connection is None or not project_id:
        return []
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text("SELECT skill_id FROM role_skills WHERE project_id = :p AND role_key = :r"),
                {"p": project_id, "r": agent},
            ).scalars().all()
    except Exception:  # noqa: BLE001 — no grants table means no grants
        return []
    return [str(r) for r in rows]


def _specpack_root(settings: Any) -> str:
    root = getattr(settings, "specpack_root", None)
    if root:
        return str(root)
    return str(Path(__file__).resolve().parents[3] / "packages" / "specpack")


def _resolved_literal(value: Any, input_data: Mapping[str, Any]) -> Optional[str]:
    """What an input will be at run time, when that can be known before the run starts."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping) and "from_input" in value:
        supplied = input_data.get(str(value["from_input"]))
        if isinstance(supplied, str) and supplied.strip():
            return supplied.strip()
        default = value.get("default")
        return str(default).strip() or None if isinstance(default, str) else None
    return None


_URL = re.compile(r"https?://[^\s<>\"'()\[\]]+", re.I)


def _uses_drive(inputs: Mapping[str, Any], input_data: Mapping[str, Any]) -> bool:
    """Whether a step's inputs, as far as they are known before the run, touch Google Drive.

    A Drive link anywhere in a value the step will receive — a `video_url`, an `asset_url`, a
    link pasted into an agent's instruction — or an explicit request to keep the result in
    Drive (`store_provider: google`, an `output_folder_id`). Decided from the link's host alone
    (`media_providers.is_drive_link`), so no input costs a network round trip here.
    """
    from .media_providers import is_drive_link

    for name, value in (inputs or {}).items():
        literal = _resolved_literal(value, input_data)
        if not literal:
            continue
        if name == "store_provider" and literal.strip().lower() == "google":
            return True
        if name == "output_folder_id":
            return True
        if any(is_drive_link(found) for found in _URL.findall(literal)):
            return True
    return False


def _step_order(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Steps in the order a run reaches them: from the entry, following `next`."""
    steps = [s for s in (spec.get("steps") or []) if isinstance(s, dict)]
    by_id = {str(s.get("id")): s for s in steps}
    seen: list[str] = []
    queue = [str(spec.get("entry_step") or (steps[0].get("id") if steps else ""))]
    while queue:
        current = queue.pop(0)
        if current in seen or current not in by_id:
            continue
        seen.append(current)
        queue.extend(str(n) for n in (by_id[current].get("next") or []))
    # Anything unreachable still counts as needing what it needs: the spec says it exists.
    seen.extend(sid for sid in by_id if sid not in seen)
    return [by_id[sid] for sid in seen]


def _stops_for_a_person(step: Mapping[str, Any]) -> bool:
    """Whether a run pauses for a person at this step, before it does the step's work."""
    if step.get("kind") == "decision":
        return True
    binding = step.get("binding") or {}
    key = str(binding.get("capability") or "")
    approval = step.get("approval") or {}
    if key in caps.CAPABILITY_KEYS:
        return caps.stops_for_a_person(key, approval.get("mode"))
    return str(approval.get("mode") or "").lower() == "manual"


def _label(step: Mapping[str, Any], custom: Mapping[str, Mapping[str, Any]]) -> str:
    from .workflows import step_title

    if step_title(step):
        return step_title(step)
    binding = step.get("binding") or {}
    own = custom.get(str(binding.get("custom_step") or "")) or {}
    key = str(binding.get("capability") or "")
    if own.get("label"):
        return str(own["label"])
    if key in caps.CAPABILITY_KEYS:
        return caps.get(key).label
    if binding.get("agent_work"):
        return "Agent step"
    return str(step.get("id"))


def _custom_steps(connection: Optional[Connection], project_id: str) -> dict[str, dict]:
    if connection is None:
        return {}
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text("SELECT step_key, label, binding_kind, skill_id FROM workflow_custom_steps "
                     "WHERE project_id = :p"),
                {"p": project_id},
            ).mappings().all()
    except Exception:  # noqa: BLE001 — an install without the table has no custom steps
        return {}
    return {str(r["step_key"]): dict(r) for r in rows}


def requirements(
    spec: Mapping[str, Any],
    *,
    input_data: Optional[Mapping[str, Any]] = None,
    connection: Optional[Connection] = None,
    project_id: str = "",
    settings: Any = None,
    _depth: int = 0,
) -> list[dict[str, Any]]:
    """Every (need, step) pair this spec implies, in the order a run reaches the steps.

    Pure enough to call without a database (the catalogue does, to print what a template needs
    before it is installed); with a connection it also reads the project's custom steps and the
    subworkflows a step calls.
    """
    input_data = input_data or {}
    custom = _custom_steps(connection, project_id)
    skills = _skill_specs(_specpack_root(settings))
    out: list[dict[str, Any]] = []
    stopped_before = False
    for position, step in enumerate(_step_order(spec)):
        step_id = str(step.get("id"))
        label = _label(step, custom)
        binding = step.get("binding") or {}
        stops_here = _stops_for_a_person(step)
        needed: set[str] = set()

        key = str(binding.get("capability") or "")
        if step.get("kind") == "capability" and key in caps.CAPABILITY_KEYS:
            capability = caps.get(key)
            needed.update(capability.requires_project)
            if capability.needs_model:
                needed.add("model")
            # THE TOOL IT RUNS MAY NEED ONE PROVIDER'S API (generate_image: OpenAI images).
            needed.update(tool_provider_needs(str(getattr(capability, "tool", "") or "")))
            if key == "notify":
                channel = _resolved_literal((binding.get("inputs") or {}).get("channel"),
                                            input_data)
                if channel:
                    needed.add(caps.notify_channel_need(channel))
        if binding.get("custom_step"):
            own = custom.get(str(binding["custom_step"])) or {}
            if own.get("binding_kind") == "skill" and own.get("skill_id"):
                skill_id = str(own["skill_id"])
                needed.update(skill_needs(skill_id, skills.get(skill_id)))
        if binding.get("agent_work"):
            needed.add("model")
            # A SKILL THE AGENT MAY CALL THAT NEEDS ONE PROVIDER'S API. The chat model is
            # routed to whatever the project connected; image generation is not, and a run
            # that reaches it without the key fails after it has spent on everything before.
            for skill_id in _agent_step_skills(step, connection, project_id):
                needed.update(_provider_needs(skill_id, skills.get(skill_id)))
            if not str(step.get("captain") or binding.get("agent") or "").strip():
                needed.add("agent_pool")
        # AN APPROVER IS NEEDED ONLY WHERE SOMEONE APPROVES. The PRD approval (`spec_review`)
        # is answered by the panel's own decision — the owner keeps it automatic — yet its
        # `approver_role: pm` made every delivery template refuse to start on a project with
        # no 'pm', for a step no person or agent ever acts on (review of the readiness change).
        approver = str((step.get("approval") or {}).get("approver_role") or "").strip()
        if key in caps.CAPABILITY_KEYS and caps.get(key).answered_by != "person":
            approver = ""
        from .step_agents import is_default_captain

        # A catalogue default captain is not a need: without it the step runs on Auto.
        # AN OUTSIDE AGENT IS NOT A ROLE THIS PROJECT MUST HAVE. Its captain is
        # {"external_agent": "ext_…"}; read as a role key it refused every workflow with a bank's
        # agent step ("no agent '{'external_agent': …}' on this project", 2026-10-31). Whether it
        # is connected and active is checked when the step runs (external_agent_runner).
        raw_captain = step.get("captain")
        captain = ("" if is_default_captain(dict(step)) or isinstance(raw_captain, dict)
                   else str(raw_captain or binding.get("agent") or "").strip())
        for named in (captain, approver):
            if named and named not in caps.HUMAN_ROLES:
                needed.add(f"role:{named}")
        if _uses_drive(binding.get("inputs") or {}, input_data):
            needed.add("google_drive")
        if step.get("kind") == "subworkflow" and connection is not None and _depth < 3:
            child = _child_spec(connection, project_id, binding)
            if child:
                for item in requirements(child, input_data=input_data, connection=connection,
                                         project_id=project_id, settings=settings,
                                         _depth=_depth + 1):
                    needed.add(item["key"])

        for need in sorted(needed):
            out.append({"key": need, "step_id": step_id, "step_label": label,
                        "position": position + 1,
                        "after_a_person": stopped_before or stops_here})
        stopped_before = stopped_before or stops_here
    return out


def _child_spec(connection: Connection, project_id: str,
                binding: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    version_id = str(binding.get("workflow_version_id") or "").strip()
    if not version_id:
        return None
    row = connection.execute(
        text("SELECT spec FROM workflow_versions WHERE id = :v AND project_id = :p"),
        {"v": version_id, "p": project_id},
    ).scalar()
    if isinstance(row, str):
        try:
            row = json.loads(row)
        except ValueError:
            return None
    return row if isinstance(row, dict) else None


def needs_of(spec: Mapping[str, Any]) -> list[str]:
    """The need keys a spec implies on its own, for display before anything is installed."""
    seen: list[str] = []
    for item in requirements(spec):
        if item["key"] not in seen:
            seen.append(item["key"])
    return seen


# ── Whether the project has it ─────────────────────────────────────────────────────────────────

def _scalar(connection: Connection, sql: str, params: Mapping[str, Any]) -> Any:
    # In a SAVEPOINT: a failed statement aborts a Postgres transaction, and this runs inside
    # the transaction that is about to start the run.
    try:
        with connection.begin_nested():
            return connection.execute(text(sql), dict(params)).scalar()
    except Exception:  # noqa: BLE001 — a missing table means the thing is not there
        return None


def check(connection: Connection, settings: Any, project_id: str, key: str
          ) -> tuple[bool, str]:
    """Whether this project has `key`, and what was found, in a sentence."""
    p = {"p": project_id}
    if key in ("repository", "model"):
        from .setup_service import connected_basics

        basics = connected_basics(connection, settings, project_id)
        if key == "repository":
            if basics["github"]:
                return True, (f"pushes to {basics['repository']}" if basics.get("repository")
                              else "a repository is connected")
            return False, (basics.get("repository_problem")
                           or "no GitHub repository is connected to this project")
        # THE ROUTER'S ANSWER, not a count of rows: the same `model_routing.choose` that picks
        # the adapter a step will run on (connected_basics → model_done).
        if basics["model"]:
            from .model_routing import choose

            return True, f"a model is connected: {choose(connection, settings, project_id).describe()}"
        return False, basics.get("model_problem") or "no model is connected to this project"
    if key == "deployment_target":
        from .deployment_fleet import usable_hosts_sql
        hosts = _scalar(connection, f"SELECT count(*) FROM deployment_hosts WHERE {usable_hosts_sql()} "
                                    "AND status IN ('verified','ready','active')", p) or 0
        targets = _scalar(connection, "SELECT count(*) FROM deployment_targets "
                                      "WHERE project_id=:p AND status='active'", p) or 0
        # A STACK TARGET IS A DESTINATION. A repository that ships a boss.deploy.yaml deploys
        # through its stack target and needs nothing else, so it counts — and is named, so the
        # checklist says WHERE such a run will go rather than only that something exists.
        stacks = _scalar(connection, "SELECT count(*) FROM deployment_targets "
                                     "WHERE project_id=:p AND status='active' "
                                     "AND target_kind='stack'", p) or 0
        if hosts or targets:
            found = f"{hosts} verified host(s), {targets} active target(s)"
            if stacks:
                found += f", {stacks} of them stack target(s) deploying a boss.deploy.yaml"
            return True, found
        return False, ("this project has no verified deployment host, no active target and no "
                       "stack target")
    if key == "email":
        from .mail_transport import route

        if not settings:
            return False, "no SMTP server is configured for this project"
        chosen = route(connection, settings, project_id)
        if chosen.ready:
            return True, f"sends through {chosen.detail}"
        return False, chosen.detail.rstrip(".")
    if key == "mattermost":
        # CONNECTED IN SKILLS & MCP › Team chat (tested when saved), or the environment.
        from .kit_connections import TEAM_CHAT_KEY, _config

        chat = _config(connection, project_id, TEAM_CHAT_KEY) or {}
        if chat.get("provider") == "mattermost" and chat.get("secret_ref_id"):
            return True, f"Mattermost is connected ({chat.get('acts_as') or 'tested'})"
        ok = bool(getattr(settings, "mattermost_real_send_allowed", False)
                  and getattr(settings, "mattermost_base_url", "")
                  and getattr(settings, "mattermost_bot_token", ""))
        return (True, "a Mattermost bot is configured") if ok else (
            False, "no Mattermost bot is configured")
    if key == "work_tracker":
        from .work_orchestration import connected_tracker

        tracker = connected_tracker(connection, project_id)
        if tracker:
            return True, f"{tracker['display_name']} is connected"
        return False, ("no work tracker is connected — the ones listed are not configured, "
                       "so a ticket could not be created")
    if key in ("google", "google_drive"):
        found = _scalar(
            connection,
            "SELECT count(*) FROM integration_connections c "
            "JOIN integration_definitions d ON d.id = c.integration_definition_id "
            "WHERE c.project_id=:p AND d.integration_key='google' AND c.status='active'", p)
        return (True, "Google is connected") if found else (
            False, "no Google account is connected to this project")
    if key == "agent_pool":
        held = _scalar(connection, "SELECT count(DISTINCT role_key) FROM role_skills "
                                   "WHERE project_id=:p", p) or 0
        return (True, f"{held} agent(s) hold skills") if held else (
            False, "no agent in this project holds a skill")
    if key.startswith("role:"):
        role = key.split(":", 1)[1]
        found = _scalar(connection, "SELECT count(*) FROM roles WHERE project_id=:p "
                                    "AND role_key=:k", {**p, "k": role})
        return (True, f"'{role}' is on this project") if found else (
            False, f"there is no agent '{role}' on this project")
    if key == "budget":
        snapshot = budget_state_of(connection, settings, project_id) or {}
        if snapshot.get("budget_state") != "red":
            return True, f"the budget is {snapshot.get('budget_state') or 'unknown'}"
        from .model_failures import budget_hold_sentence, held_headline

        return False, held_headline(budget_hold_sentence(
            "red", snapshot.get("utilization_ratio"), snapshot.get("cap_usd_micros"), snapshot))
    if key.startswith("channel:"):
        return False, f"{key.split(':', 1)[1]!r} is not a channel BOSS can post to"
    if key.startswith("provider:"):
        from .model_routing import provider_connected, requirement_for_need

        req = requirement_for_need(key)
        if req is None:
            return False, f"BOSS does not know the provider requirement {key!r}"
        if provider_connected(connection, settings, project_id, req):
            return True, f"an {req.provider} key is connected for {req.capability}"
        return False, (f"no {req.provider} key is connected, and {req.capability} needs one — "
                       f"a connected subscription does not do it")
    raise KeyError(f"no check for need {key!r}")


def preflight(
    connection: Connection,
    settings: Any,
    project_id: str,
    spec: Mapping[str, Any],
    input_data: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Everything this workflow needs from this project, each checked, and what is missing."""
    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    grouped: dict[str, dict[str, Any]] = {}
    for item in requirements(spec, input_data=input_data, connection=connection,
                             project_id=project_id, settings=settings):
        entry = grouped.get(item["key"])
        if entry is None:
            need = need_for(item["key"])
            entry = grouped[item["key"]] = {
                "key": need.key, "label": need.label, "fix": need.fix, "area": need.area,
                "href": need.href,
                "needed_by": [], "first_step": item["step_id"],
                "first_step_label": item["step_label"],
                # Whether a person gets a chance to fix it before the first step that needs it
                # runs: the run stops at a gate first, or the step itself waits for approval.
                "after_a_person": item["after_a_person"],
            }
        entry["needed_by"].append({"step_id": item["step_id"], "label": item["step_label"]})
    checked: list[dict[str, Any]] = []
    for entry in grouped.values():
        ok, detail = check(connection, settings, project_id, entry["key"])
        checked.append({**entry, "ok": ok, "detail": detail})
    budget, notes = _budget_readiness(connection, settings, project_id, spec)
    if budget is not None:
        checked.append(budget)
    missing = [c for c in checked if not c["ok"]]
    return {"ready": not missing, "checked": checked, "missing": missing,
            "notes": notes, "summary": summary_sentence(missing)}


def budget_state_of(connection: Connection, settings: Any, project_id: str
                    ) -> Optional[dict[str, Any]]:
    """The project's budget snapshot, or None when it cannot be read. In a SAVEPOINT, like
    `_scalar`: this runs inside the transaction that is about to start the run."""
    try:
        from .budget import BudgetGovernor

        with connection.begin_nested():
            return BudgetGovernor(settings).status_for_project(connection, project_id)
    except Exception:  # noqa: BLE001 — an unreadable budget is not a reason to refuse
        return None


def _budget_readiness(connection: Connection, settings: Any, project_id: str,
                      spec: Mapping[str, Any]) -> tuple[Optional[dict[str, Any]], list[dict]]:
    """Red → a need ("Budget is red — work will be held") with its fix; orange or yellow → a
    note. The money counted is money: subscription calls are not in it (included_spend.py)."""
    snapshot = budget_state_of(connection, settings, project_id)
    if not snapshot:
        return None, []
    from .model_failures import budget_hold_sentence, held_headline, resets_sentence

    state = str(snapshot.get("budget_state") or "")
    sentence = budget_hold_sentence(state, snapshot.get("utilization_ratio"),
                                    snapshot.get("cap_usd_micros"), snapshot)
    if state == "red":
        need = NEEDS["budget"]
        steps = _step_order(spec)
        first = steps[0] if steps else {}
        label = _label(first, {}) if first else "the first step"
        fix = (f"{resets_sentence(snapshot)} Calls on a subscription cost no money and do "
               "not count against it; only metered API spend does.")
        return ({"key": need.key, "label": need.label, "fix": fix, "area": need.area,
                 "needed_by": [{"step_id": str(first.get("id") or ""), "label": label}],
                 "first_step": str(first.get("id") or ""), "first_step_label": label,
                 "after_a_person": False, "ok": False,
                 "detail": held_headline(sentence)}, [])
    if state in ("orange", "yellow"):
        used = f"{round(float(snapshot.get('utilization_ratio') or 0) * 100):.0f}%"
        whose = snapshot.get("whose") or "the"
        text_ = (sentence if state == "orange" else
                 f"The budget is yellow ({used} of {whose} cap used). Work runs; at orange "
                 f"only P0 and P1 work runs, and at red only P0. {resets_sentence(snapshot)}")
        return None, [{"key": "budget", "level": state, "text": text_, "area": "budget"}]
    return None, []


def summary_sentence(missing: Sequence[Mapping[str, Any]]) -> str:
    """The refusal, as one paragraph an operator can act on."""
    if not missing:
        return "Everything this workflow needs is in place."
    parts = []
    for m in missing:
        steps = ", ".join(n["label"] for n in m.get("needed_by") or [])
        parts.append(f"{m['label']} — needed by {steps}: {m['detail']}. {m['fix']}")
    return (f"This workflow cannot run here yet; {len(missing)} thing"
            f"{'s are' if len(missing) != 1 else ' is'} missing. " + " ".join(parts))


# ── At the door ────────────────────────────────────────────────────────────────────────────────

class WorkflowNotReadyError(RuntimeError):
    """The project lacks something this workflow needs. Carries the whole preflight."""

    def __init__(self, readiness: dict[str, Any]):
        super().__init__(readiness.get("summary") or "this workflow cannot run here yet")
        self.readiness = readiness


def _version_spec(connection: Connection, project_id: str, version_id: str
                  ) -> tuple[Optional[dict[str, Any]], str]:
    row = connection.execute(
        text("SELECT v.spec, COALESCE(d.namespace, 'system') AS namespace "
             "FROM workflow_versions v JOIN workflow_definitions d "
             "  ON d.id = v.workflow_definition_id "
             "WHERE v.id = :v AND v.project_id = :p"),
        {"v": version_id, "p": project_id},
    ).mappings().first()
    if not row:
        return None, ""
    spec = row["spec"]
    if isinstance(spec, str):
        spec = json.loads(spec)
    return (spec if isinstance(spec, dict) else None), str(row["namespace"])


def readiness_of_version(
    connection: Connection,
    settings: Any,
    project_id: str,
    *,
    version_id: str,
    input_data: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """The preflight for a published version — what Start would say, without starting."""
    spec, namespace = _version_spec(connection, project_id, version_id)
    if spec is None or namespace != "product":
        return {"ready": True, "checked": [], "missing": [], "applies": False,
                "summary": "not a product workflow; nothing to check"}
    return {**preflight(connection, settings, project_id, spec, input_data), "applies": True}


def refuse_unless_ready(
    connection: Connection,
    settings: Any,
    project_id: str,
    *,
    version_id: str,
    input_data: Optional[Mapping[str, Any]] = None,
    start_anyway: Iterable[str] = (),
) -> dict[str, Any]:
    """THE CHECK EVERY START DOOR MAKES. Returns the preflight; raises when something is missing
    that the caller did not knowingly accept.

    `start_anyway` is the operator saying "I know, start it — I will add the deployment host
    before the release". It names the keys they were shown; anything missing that they were not
    shown still refuses, so a stale form cannot wave through a new problem.

    Only product workflows are checked. BOSS's own system workflows are machinery that runs
    under its own governance and were never started by an operator.
    """
    readiness = readiness_of_version(connection, settings, project_id, version_id=version_id,
                                     input_data=input_data)
    if not readiness.get("applies"):
        return readiness
    accepted = {str(k) for k in (start_anyway or [])}
    unaccepted = [m for m in readiness["missing"] if m["key"] not in accepted]
    readiness["started_anyway"] = sorted(m["key"] for m in readiness["missing"]
                                         if m["key"] in accepted)
    if unaccepted:
        readiness["summary"] = summary_sentence(unaccepted)
        raise WorkflowNotReadyError(readiness)
    return readiness


def _missing_record(readiness: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The missing items as they are kept on a record: enough to say what and how to fix it
    without re-running the check, never the whole checklist."""
    return [{"key": m["key"], "label": m["label"], "first_step": m.get("first_step"),
             "fix": m.get("fix"), "area": m.get("area"), "href": m.get("href")}
            for m in readiness.get("missing") or []]


def record_started_anyway(connection: Connection, project_id: str, actor: str, run_id: str,
                          readiness: Mapping[str, Any]) -> None:
    """A run started with something missing says so on its own record, so whoever looks at it
    later sees the operator's choice rather than discovering the gap at the step."""
    if not readiness.get("started_anyway") or not run_id:
        return
    from .core import emit_event

    emit_event(connection, project_id, "workflow.started_unready", actor, "workflow_run",
               run_id, {"missing": _missing_record(readiness),
                        "started_anyway": list(readiness.get("started_anyway") or []),
                        "chosen_by": "operator"})


def accepted_by_run(connection: Connection, project_id: str, run_id: str) -> list[str]:
    """The keys an earlier run was started without, so running it AGAIN repeats that choice.

    Re-run and Stop-and-run-again used to post a plain start: a run the operator had started
    anyway (no deployment host yet) was refused on its re-run, and Stop-and-run-again stopped
    the original first, leaving them with no run at all (review of the readiness change). The
    choice made for that run carries to its re-run; anything newly missing still refuses.
    Only the operator's own choice carries — a run that was merely NOTED as unready (an
    approved build, see `note_unready_start`) accepted nothing.
    """
    if not run_id:
        return []
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text("SELECT data FROM events WHERE project_id = :p AND entity_id = :r "
                     "AND event_type = 'workflow.started_unready'"),
                {"p": project_id, "r": run_id},
            ).scalars().all()
    except Exception:  # noqa: BLE001 — no record means no earlier choice to repeat
        return []
    keys: set[str] = set()
    for data in rows:
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                continue
        if not isinstance(data, Mapping) or data.get("chosen_by", "operator") != "operator":
            continue
        keys.update(str(m.get("key")) for m in data.get("missing") or []
                    if isinstance(m, Mapping) and m.get("key"))
    return sorted(keys)


def refusal_record(readiness: Mapping[str, Any], *, door: str, door_name: str,
                   workflow_key: str) -> dict[str, Any]:
    """What an unattended door keeps when it was refused: the missing items with their fixes,
    and one short sentence. Short on purpose — a schedule's `last_error` used to be the whole
    summary cut at 500 characters, which could cut off the very sentence saying what to do."""
    missing = _missing_record(readiness)
    labels = "; ".join(m["label"] for m in missing) or "something it needs"
    one = len(missing) == 1
    message = (f"Not started — this project is missing {labels}. Fix "
               f"{'it' if one else 'them'} (each item says how), or accept starting without "
               f"{'it' if one else 'them'} on the {door}.")
    return {"door": door, "door_name": door_name, "workflow_key": workflow_key,
            "missing": missing, "message": message}


def record_refused(connection: Connection, project_id: str, actor: str, *, door: str,
                   door_id: str, door_name: str, workflow_key: str,
                   readiness: Mapping[str, Any]) -> dict[str, Any]:
    """An UNATTENDED start that was refused says so where people look.

    A schedule or an event subscription has nobody at the keyboard to read a 409. The event
    door used to refuse and only log, so a subscription that had started runs the day before
    silently stopped starting anything (review of the readiness change). This is the visible
    half: a `workflow.start_refused` event naming the door, what is missing and the fix for
    each. Returns the record, for the door to keep beside itself.
    """
    from .core import emit_event

    record = {**refusal_record(readiness, door=door, door_name=door_name,
                               workflow_key=workflow_key), "door_id": door_id}
    emit_event(connection, project_id, "workflow.start_refused", actor, door, door_id, record)
    return record


def note_unready_start(connection: Connection, settings: Any, project_id: str, actor: str,
                       run_id: str, *, version_id: str,
                       input_data: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """For a door that must not refuse: check anyway, and put what is missing on the run.

    `start_project_run` starts the outer workflow for a build that an approval has ALREADY
    started — refusing it would leave a running code task with no run to watch. It was exempt
    from the preflight outright, so the request_meeting → PRD → build path got no warning at
    all while the console path refused, and its deploy still failed at the end on a project
    with no host (review of the readiness change). Now the run carries the same list from its
    first second, marked as nobody's choice.
    """
    readiness = readiness_of_version(connection, settings, project_id, version_id=version_id,
                                     input_data=input_data)
    if readiness.get("missing") and run_id:
        from .core import emit_event

        emit_event(connection, project_id, "workflow.started_unready", actor, "workflow_run",
                   run_id, {"missing": _missing_record(readiness), "started_anyway": [],
                            "chosen_by": "nobody",
                            "why": "started by an approved build, which cannot be refused"})
    return readiness


__all__ = [
    "NEEDS", "Need", "WorkflowNotReadyError", "accepted_by_run", "check", "need_for",
    "needs_of", "note_unready_start", "preflight", "readiness_of_version", "record_refused",
    "record_started_anyway", "refusal_record", "refuse_unless_ready", "requirements",
    "skill_needs", "summary_sentence",
]

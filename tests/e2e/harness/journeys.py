"""The milestone's operator journeys, driven through the console the way a person drives them.

WHY THESE AND NOT "PRESS EVERY PRIMARY BUTTON". The first walk pressed eight buttons across
thirty-six areas — three of them unlabelled — and passed every one that did nothing, because a
press only had to avoid errors for two seconds. It never opened the agent wizard, never pressed
"Advance now", never created a skill or an MCP server or a workflow. Those are exactly the
screens that failed on boss-1 (the wizard posting as `vp_agents` → 403; "Advance now" sending the
console key to a worker-only route → 403). So each requirement gets a journey that does the
whole job and then LOOKS for the result an operator would look for.

AND EACH JOURNEY DOES THE REQUIREMENT, NOT SOMETHING NEAR IT. Round two labelled a palette smoke
check "R3" and an "Advance now" press "R4": neither bound a skill or an MCP tool to a step, named
an agent on one or left one to be picked, so the report would have said R3 and R4 were proven
whether they worked or not. REQUIRES below names, per requirement, the controls that ARE the
requirement, and the self-test fails when no journey labelled with it uses all of them.

  R1  skill_create      Skills → write one by hand → "Saved <key>" and it is in the list
  R1  mcp_register      Capabilities → register the fakes' MCP server → listed there and under Skills
  R2  agent_with_skill  Create agent with that skill and that MCP server → both grants are read
                        back through the API the runtime uses (role-skills, role-mcp-servers)
  R2  agent_update      Open the agent → give it another skill and an MCP server it does not hold
                        → listed after a reload, and both read back as grants
  R3  step_bindings     Your steps → one step bound to the skill, one to the MCP tool → placed in a
                        workflow, saved, reloaded, RUN → the skill ran and the MCP fake was called
  R4  step_agents       Two agent steps, one naming the R2 agent, one naming nobody → saved,
                        reloaded, RUN → the run records the named agent and the auto-picked one
                        with its reason
  R6  workflow_build    (console smoke) two palette steps → saved → "2 steps" after a reload
  R6  advance_now       (console smoke) Running → Advance now → a sentence that is not an error
  R7  live_gate         A run waiting at a gate → Agents › Live shows its row `Waiting on you` →
                        the amber You hop focuses its card → Approve on the card → the decision is
                        approved, the run moves on, and the pill fell by one and still equals what
                        the other surfaces count (agents-home spec §8.3)

Every selector a journey uses is listed in JOURNEY_SELECTORS, which the self-test checks against
the console source — a renamed testid fails a fast test, not a forty-minute walk. Controls the
milestone needs and the console does not have yet are in PENDING_SELECTORS: the journey fails at
runtime with the sentence saying so, and the self-test fails the day one appears, so it is moved
to JOURNEY_SELECTORS rather than silently left unexercised.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# (journey, file under apps/web, testid) — the console's promise to each journey.
JOURNEY_SELECTORS: tuple[tuple[str, str, str], ...] = (
    ("skill_create", "app/skills-ui.tsx", "skill-form"),
    ("skill_create", "app/skills-ui.tsx", "skill-label"),
    ("skill_create", "app/skills-ui.tsx", "skill-key"),
    ("skill_create", "app/skills-ui.tsx", "skill-summary"),
    ("skill_create", "app/skills-ui.tsx", "skill-behaviour"),
    ("skill_create", "app/skills-ui.tsx", "skill-executor"),
    ("skill_create", "app/skills-ui.tsx", "skill-knowledge"),
    ("skill_create", "app/skills-ui.tsx", "skill-save"),
    ("skill_create", "app/skills-ui.tsx", "skill-note"),
    ("skill_create", "app/skills-ui.tsx", "skill-list"),
    ("skill_create", "app/skills-ui.tsx", "skills-custom"),
    # Capabilities and Skills and MCP mount ONE shared panel (app/mcp-servers-ui.tsx, from the
    # skills-mcp stream); its own form replaced Capabilities' server-action form at integration.
    ("mcp_register", "app/mcp-servers-ui.tsx", "mcp-register-open"),
    ("mcp_register", "app/mcp-servers-ui.tsx", "mcp-name"),
    ("mcp_register", "app/mcp-servers-ui.tsx", "mcp-url"),
    ("mcp_register", "app/mcp-servers-ui.tsx", "mcp-save"),
    ("mcp_register", "app/mcp-servers-ui.tsx", "mcp-note"),
    ("mcp_register", "app/skills-ui.tsx", "mcp-servers"),
    ("mcp_register", "app/mcp-servers-ui.tsx", "mcp-list"),
    ("agent_with_skill", "app/workforce-ui.tsx", "wf-new-agent"),
    ("agent_with_skill", "app/agent-creation-wizard.tsx", "aw-dialog"),
    ("agent_with_skill", "app/agent-creation-wizard.tsx", "aw-name"),
    ("agent_with_skill", "app/agent-creation-wizard.tsx", "aw-next"),
    ("agent_with_skill", "app/agent-creation-wizard.tsx", "aw-publish"),
    ("agent_with_skill", "app/agent-creation-wizard.tsx", "aw-result"),
    ("agent_with_skill", "app/agent-creation-wizard.tsx", "aw-complete"),
    ("agent_with_skill", "app/agent-creation-wizard.tsx", "aw-pattern-${pattern.id}"),
    ("agent_with_skill", "app/agent-creation-wizard.tsx", "aw-skill-${skill.id}"),
    # Agents opens on Live (agents-home spec §3.1): the cards are on Directory, a tab away.
    ("agent_with_skill", "app/workforce-ui.tsx", "agents-view-directory"),
    ("agent_update", "app/workforce-ui.tsx", "agents-view-directory"),
    ("agent_update", "app/agents/roster-zones.tsx", "wf-open-${a.id}"),
    ("agent_update", "app/agent-detail-ui.tsx", "agent-detail"),
    ("agent_update", "app/agent-detail-ui.tsx", "agent-skills"),
    ("agent_update", "app/agent-detail-ui.tsx", "agent-skill-open"),
    ("agent_update", "app/agent-detail-ui.tsx", "agent-skill-select"),
    ("agent_update", "app/agent-detail-ui.tsx", "agent-skill-confirm"),
    ("agent_update", "app/agent-detail-ui.tsx", "agent-skills-held"),
    ("agent_update", "app/agent-detail-ui.tsx", "agent-skills-failed"),
    # ARRIVED at integration: the agents stream's MCP grant controls (R2), moved here from
    # PENDING_SELECTORS as the self-test requires the day they appear.
    ("agent_with_skill", "app/agent-creation-wizard.tsx", "aw-mcp-${server.mcp_server_id}"),
    ("agent_update", "app/agent-detail-ui.tsx", "agent-mcp-open"),
    ("agent_update", "app/agent-detail-ui.tsx", "agent-mcp-select"),
    ("agent_update", "app/agent-detail-ui.tsx", "agent-mcp-confirm"),
    ("agent_update", "app/agent-detail-ui.tsx", "agent-mcp-held"),
    ("agent_update", "app/agent-detail-ui.tsx", "agent-mcp-empty"),
    ("agent_update", "app/agent-detail-ui.tsx", "agent-mcp-failed"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-tab-${t}"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-define-step"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-define-form"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-define-label"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-define-binding"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-define-save"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-define-error"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-define-empty"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-steps-list"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-palette-custom-${c.step_key}"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-tab-new"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-tab-edit"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-new-name"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-new-confirm"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-made"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-save"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-notice"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-target"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-head-meta"),
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-draft-${step.id}"),
    ("step_agents", "app/delivery-workflows-ui.tsx", "dw-palette-agent-work"),
    ("step_agents", "app/delivery-workflows-ui.tsx", "dw-agentwork-mode-${step.id}"),
    ("step_agents", "app/delivery-workflows-ui.tsx", "dw-agentwork-text-${step.id}"),
    ("step_agents", "app/delivery-workflows-ui.tsx", "dw-draft-captain-${step.id}"),
    ("step_agents", "app/delivery-workflows-ui.tsx", "dw-tab-new"),
    ("step_agents", "app/delivery-workflows-ui.tsx", "dw-new-name"),
    ("step_agents", "app/delivery-workflows-ui.tsx", "dw-new-confirm"),
    ("step_agents", "app/delivery-workflows-ui.tsx", "dw-save"),
    ("step_agents", "app/delivery-workflows-ui.tsx", "dw-notice"),
    ("step_agents", "app/delivery-workflows-ui.tsx", "dw-target"),
    ("step_agents", "app/delivery-workflows-ui.tsx", "dw-head-meta"),
    ("workflow_build", "app/delivery-workflows-ui.tsx", "dw-tab-${t}"),
    ("workflow_build", "app/delivery-workflows-ui.tsx", "dw-tab-new"),
    ("workflow_build", "app/delivery-workflows-ui.tsx", "dw-new-name"),
    ("workflow_build", "app/delivery-workflows-ui.tsx", "dw-new-confirm"),
    ("workflow_build", "app/delivery-workflows-ui.tsx", "dw-made"),
    ("workflow_build", "app/delivery-workflows-ui.tsx", "dw-palette-${c.key}"),
    ("workflow_build", "app/delivery-workflows-ui.tsx", "dw-save"),
    ("workflow_build", "app/delivery-workflows-ui.tsx", "dw-notice"),
    ("workflow_build", "app/delivery-workflows-ui.tsx", "dw-target"),
    ("workflow_build", "app/delivery-workflows-ui.tsx", "dw-head-meta"),
    ("advance_now", "app/delivery-workflows-ui.tsx", "dw-advance"),
    ("advance_now", "app/delivery-workflows-ui.tsx", "dw-notice"),
    # R7: Agents › Live (live-board stream) — a gate on a run, decided from its card.
    ("live_gate", "app/workforce-ui.tsx", "agents-view-live"),
    ("live_gate", "app/agents/live-board.tsx", "ag-live"),
    ("live_gate", "app/agents/mission-row.tsx", "ag-mission-${mission.runId}"),
    ("live_gate", "app/agents/mission-row.tsx", "ag-mission-you-${mission.runId}"),
    ("live_gate", "app/agents/attention-card.tsx", "ag-att-${item.id}"),
)

# (journey, file under apps/web, testid, what the console is missing). R2 says an agent is given
# MCP servers on create and on update; no screen offers that yet (map.json: "There is NO table
# linking an agent (role) to an MCP server"). The journeys look for exactly these controls and
# fail with the sentence when they are absent. The self-test fails when one APPEARS in its file,
# so it gets moved to JOURNEY_SELECTORS and is held to existing from then on.
#
# A template id is matched by its FIXED START ("aw-mcp-"), never by the variable after it: round
# three wrote `aw-mcp-${server.id}` here while the agents stream renders
# `aw-mcp-${server.mcp_server_id}`, so the arrival self-test would never have fired. The names
# below are the agents stream's (agent-detail-ui.tsx and agent-creation-wizard.tsx on its
# branch), including the button that OPENS the picker: the picker is not rendered until it is
# pressed, so a journey that looked for the picker first said "no way to give an MCP server".
PENDING_SELECTORS: tuple[tuple[str, str, str, str], ...] = (
    # Empty at integration: every control the R2 journeys waited for exists now.
)

# What each requirement IS, as controls. A journey labelled with a requirement must declare
# every one of these (in JOURNEY_SELECTORS or PENDING_SELECTORS) AND press it in its own code —
# both enforced by the self-test, so a table row alone cannot turn a smoke check into proof.
REQUIRES: dict[str, tuple[str, ...]] = {
    "R1": ("skill-save", "mcp-save"),
    "R2": ("aw-skill-${skill.id}", "aw-mcp-${server.mcp_server_id}", "agent-skill-confirm",
           "agent-mcp-open", "agent-mcp-confirm"),
    "R3": ("dw-define-binding", "dw-palette-custom-${c.step_key}", "dw-save"),
    "R4": ("dw-palette-agent-work", "dw-draft-captain-${step.id}", "dw-save"),
}

WAIT_MS = 60_000
RUN_TIMEOUT_SECONDS = 600
MCP_TOOL = "e2e_echo"          # stubs/fakes.py MCP_ECHO_TOOL
AGENT_STEP_MARKER = "e2e-journey-agent-step"   # stubs/model_script.json agent.e2e-journey-step


@dataclass
class Context:
    """What one journey leaves for the next: the skill made in R1 is the one R2 assigns, the
    agent R2 makes is the one R4 names."""
    stamp: str
    fakes_url: Optional[str]
    target: Any = None           # harness.target.Target: read-backs and runs go through its API
    skill_key: str = ""
    mcp_name: str = ""
    mcp_server_id: str = ""
    agent_name: str = ""
    agent_role: str = ""
    notes: dict[str, list[str]] = field(default_factory=dict)


class JourneyFailed(AssertionError):
    """The sentence an operator would say: what they did, what they saw instead."""


# ----------------------------------------------------------------------------- page helpers
def _visible(page, testid: str, *, timeout: int = WAIT_MS):
    # The first VISIBLE match: several screens render a control twice (a dock and a page).
    locator = page.locator(f'[data-testid="{testid}"] >> visible=true').first
    locator.wait_for(state="visible", timeout=timeout)
    return locator


def _present(page, testid_or_prefix: str) -> bool:
    selector = (f'[data-testid^="{testid_or_prefix[:-1]}"]' if testid_or_prefix.endswith("*")
                else f'[data-testid="{testid_or_prefix}"]')
    return page.locator(f"{selector} >> visible=true").count() > 0


def _text(page, testid: str) -> str:
    try:
        return " ".join((page.locator(f'[data-testid="{testid}"] >> visible=true').first.inner_text(
            timeout=2_000) or "").split())
    except Exception:  # noqa: BLE001
        return ""


def _open_details(page, testid: str) -> None:
    details = page.locator(f'details[data-testid="{testid}"]').first
    details.wait_for(state="attached", timeout=WAIT_MS)
    if details.get_attribute("open") is None:
        details.locator("summary").first.click()


def _wait_for_text(page, testid: str, pattern: str, *, timeout_s: float = 60) -> str:
    deadline = time.time() + timeout_s
    seen = ""
    while time.time() < deadline:
        seen = _text(page, testid)
        if re.search(pattern, seen, re.I):
            return seen
        page.wait_for_timeout(500)
    raise JourneyFailed(f"[data-testid={testid}] never said /{pattern}/; it said "
                        f"{seen[:300]!r}" if seen else
                        f"[data-testid={testid}] never showed /{pattern}/ (nothing was shown)")


def _options(page, testid: str) -> list[dict[str, str]]:
    """Every <option> of a visible <select>: value, text and the label of its <optgroup>."""
    select = _visible(page, testid)
    return select.locator("option").evaluate_all(
        "els => els.map(e => ({value: e.value, text: (e.textContent || '').trim(), "
        "group: (e.parentElement && e.parentElement.label) || ''}))") or []


# ----------------------------------------------------------------------------- API helpers
def _api(ctx: Context):
    from .client import owner_api

    if ctx.target is None:
        raise JourneyFailed("this journey reads back through the API, and the walk was given "
                            "no target to read from")
    return owner_api(ctx.target)


def _role_of(ctx: Context, name: str) -> str:
    """The role key the console registered for an agent it shows by `name`."""
    items = (_api(ctx).get("/v1/roles", project_id=ctx.target.project_id) or {}).get("items") or []
    for item in items:
        if name in (item.get("display_name"), item.get("name")):
            return str(item.get("role_key") or item.get("id") or "")
    return ""


def _grants(ctx: Context, role: str) -> set[str]:
    """The skills `role` HOLDS — role_skills, which is what the runtime checks."""
    body = _api(ctx).get("/v1/role-skills", project_id=ctx.target.project_id) or {}
    return {str(a.get("skill_id")) for a in body.get("assignments") or []
            if str(a.get("role_key")) == role}


NO_MCP_GRANT_API = ("the API has no agent→MCP grant to read (GET /v1/role-mcp-servers is not "
                    "there), so an MCP server 'given' to an agent on screen is nothing a run "
                    "can use")


def _mcp_grants(ctx: Context, role: str) -> Optional[set[str]]:
    """The MCP servers `role` HOLDS — role_mcp_servers through GET /v1/role-mcp-servers, which
    is what the runtime offers that agent's steps; None when this install has no such route.

    Round three took a ticked wizard checkbox as proof the agent held the server and never read
    the grant back, so a wizard that dropped the choice would have passed R2."""
    from .client import ApiError

    try:
        body = _api(ctx).get("/v1/role-mcp-servers", project_id=ctx.target.project_id,
                             role_key=role) or {}
    except ApiError as exc:
        if exc.status in (404, 405):
            return None
        raise JourneyFailed(f"reading which MCP servers {role} holds failed: {exc}") from None
    return {str(a.get("mcp_server_id")) for a in body.get("assignments") or []
            if str(a.get("role_key") or role) == role}


def _second_mcp_server(ctx: Context) -> tuple[str, str]:
    """A server the agent does not hold yet, for the update journey: when create worked, the
    agent already holds the R1 server and the page has nothing left to offer. Registered through
    the API — set-up, not the thing under test (R1 proved registering through the console)."""
    name = f"{ctx.mcp_name or 'e2e-mcp-' + ctx.stamp}-update"
    url = f"{(ctx.fakes_url or 'http://127.0.0.1:9').rstrip('/')}/mcp"
    made = _api(ctx).post("/v1/mcp/servers", {"project_id": ctx.target.project_id,
                                              "actor": ctx.target.owner_principal,
                                              "name": name, "url": url}) or {}
    server_id = str(made.get("id") or "")
    if not server_id:
        items = (_api(ctx).get("/v1/mcp/servers", project_id=ctx.target.project_id)
                 or {}).get("items") or []
        server_id = next((str(i.get("id")) for i in items if i.get("name") == name), "")
    if not server_id:
        raise JourneyFailed(f"registered a second MCP server {name} to give on update, and the "
                            f"API does not list it")
    return server_id, name


def _mcp_calls(ctx: Context) -> list[dict[str, Any]]:
    from .drive import _get_json

    return _get_json(f"{ctx.fakes_url.rstrip('/')}/mcp/_calls" if ctx.fakes_url else None
                     ).get("calls") or []


def _workflow_key(ctx: Context, name: str) -> str:
    items = (_api(ctx).get("/v1/delivery-workflows", project_id=ctx.target.project_id)
             or {}).get("items") or []
    for item in items:
        if item.get("name") == name:
            return str(item.get("workflow_key") or "")
    raise JourneyFailed(f"workflow {name!r} was saved in the console but the API does not list it")


def _saved_steps(ctx: Context, workflow_key: str) -> list[dict[str, Any]]:
    body = _api(ctx).get(f"/v1/delivery-workflows/{workflow_key}/spec",
                         project_id=ctx.target.project_id) or {}
    spec = body.get("spec") if isinstance(body.get("spec"), dict) else body
    return list((spec or {}).get("steps") or [])


def _run(ctx: Context, workflow_key: str, run_input: dict[str, Any]):
    """Start the workflow as the owner and follow it with the same driver the templates use:
    gates answered as an operator would, abandoned runs cancelled. (outcome, steps with output)."""
    from .drive import Driver, Outcome, _run_detail

    driver = Driver(ctx.target, approve_gates=True, install_catalogue=False)
    outcome = Outcome("journey", workflow_key, markers=[f"E2E journey {ctx.stamp}"])
    driver.start(workflow_key, run_input, outcome)
    if not outcome.run_id:
        raise JourneyFailed(f"starting {workflow_key} was refused: "
                            f"{'; '.join(outcome.problems) or outcome.status}")
    driver.follow(outcome, timeout=RUN_TIMEOUT_SECONDS)
    driver.settle_abandoned()
    _, steps = _run_detail(driver.api, outcome.run_id)
    return outcome, steps


def _step_line(step: dict[str, Any]) -> str:
    error = str(step.get("last_error") or "")[:240]
    return f"{step.get('step_id')}={step.get('status')}" + (f" ({error})" if error else "")


# ----------------------------------------------------------------------------- the builder
def _new_workflow(page, goto: Callable[[str], None], name: str) -> None:
    goto("delivery_workflows")
    _visible(page, "dw-tab-design").click()
    _visible(page, "dw-tab-new").click()
    _visible(page, "dw-new-name").fill(name)
    _visible(page, "dw-new-confirm").click()
    _wait_for_text(page, "dw-made", re.escape(name))


def _add_from_palette(page, testid: str) -> None:
    # By testid, and only once the palette is enabled again: pressing by index while the first
    # add was still busy shifted the `:enabled` list and added one step, not two.
    button = page.locator(f'[data-testid="{testid}"]:enabled').first
    button.wait_for(state="visible", timeout=WAIT_MS)
    button.click()
    page.locator('[data-testid^="dw-palette-"]:enabled').first.wait_for(timeout=WAIT_MS)
    page.wait_for_timeout(500)


def _step_rows(page) -> list[str]:
    return page.locator("[data-step-row] >> visible=true").evaluate_all(
        "els => els.map(e => e.getAttribute('data-step-row'))") or []


SAVE_FAILED = re.compile(r"could not|fail|error|refus|denied|invalid|forbidden|\b[45]\d\d\b", re.I)


def _save_and_reopen(page, goto: Callable[[str], None], name: str, steps: int) -> str:
    """Save, demand the notice that says it worked, then load the screen afresh and reopen the
    workflow by name: what the header says now came from the server, not from the canvas.

    Round two accepted any notice without an em dash and read "2 steps" off the same page —
    client state — so an unsaved workflow passed."""
    _visible(page, "dw-save").click()
    said = _wait_for_text(page, "dw-notice", r"\bsave")
    if not re.search(r"\bsave\b\s*—\s*done\b|\bsaved\b", said, re.I) or SAVE_FAILED.search(said):
        raise JourneyFailed(f"saving workflow {name!r} said: {said}")
    goto("delivery_workflows")
    _visible(page, "dw-tab-design").click()
    if page.locator('[data-testid="dw-tab-edit"][aria-selected="false"]').count():
        _visible(page, "dw-tab-edit").click()
    try:
        _visible(page, "dw-target").select_option(label=name, timeout=15_000)
    except Exception:  # noqa: BLE001
        offered = [o["text"] for o in _options(page, "dw-target")]
        raise JourneyFailed(f"after a reload, workflow {name!r} is not among the ones you can "
                            f"edit ({len(offered)} offered)") from None
    want = rf"\b{steps} steps?\b"
    try:
        return _wait_for_text(page, "dw-head-meta", want, timeout_s=30)
    except JourneyFailed:
        raise JourneyFailed(f"saved {name!r} with {steps} steps; reopened after a reload it "
                            f"says {_text(page, 'dw-head-meta')!r}") from None


# ----------------------------------------------------------------------------- R1
def skill_create(page, ctx: Context, goto: Callable[[str], None]) -> str:
    key = f"e2e_skill_{ctx.stamp}"
    goto("skills")
    _open_details(page, "skill-form")
    _visible(page, "skill-label").fill(f"E2E skill {ctx.stamp}")
    _visible(page, "skill-key").fill(key)
    _visible(page, "skill-summary").fill("Answers from the knowledge below; made by the e2e "
                                         "harness's skill journey")
    _visible(page, "skill-behaviour").select_option("builtin")
    _visible(page, "skill-executor").fill("knowledge.only")
    _visible(page, "skill-knowledge").fill("The answer is 42.")
    _visible(page, "skill-save").click()
    said = _wait_for_text(page, "skill-note", r"saved|error|refus|invalid|denied|\d{3}")
    if not re.search(rf"saved {re.escape(key)}", said, re.I):
        raise JourneyFailed(f"saving skill {key} from the Skills form said: {said}")
    _open_details(page, "skills-custom")
    _wait_for_text(page, "skill-list", re.escape(key), timeout_s=30)
    ctx.skill_key = key
    return f"created skill {key}; it is listed under Custom skills"


def mcp_register(page, ctx: Context, goto: Callable[[str], None]) -> str:
    name = f"e2e-mcp-{ctx.stamp}"
    # The fakes' MCP server (stubs/fakes.py `_mcp`): it lists one tool and keeps every call, so
    # R3 can bind that tool to a step and prove the step called it.
    url = f"{(ctx.fakes_url or 'http://127.0.0.1:9').rstrip('/')}/mcp"
    goto("capabilities")
    _visible(page, "mcp-register-open").click()
    _visible(page, "mcp-name").fill(name)
    _visible(page, "mcp-url").fill(url)
    _visible(page, "mcp-save").click()
    try:
        _wait_for_text(page, "mcp-list", re.escape(name), timeout_s=60)
    except Exception as exc:  # noqa: BLE001
        raise JourneyFailed(f"registered MCP server {name} from Capabilities, and it never "
                            f"appeared in the server list ({str(exc)[:200]})") from None
    goto("skills")
    _open_details(page, "mcp-servers")
    try:
        _wait_for_text(page, "mcp-list", re.escape(name), timeout_s=30)
    except Exception as exc:  # noqa: BLE001
        raise JourneyFailed(f"MCP server {name} is listed under Capabilities but not under "
                            f"Skills and MCP → MCP servers ({str(exc)[:200]})") from None
    ctx.mcp_name = name
    if ctx.target is not None:
        items = (_api(ctx).get("/v1/mcp/servers", project_id=ctx.target.project_id)
                 or {}).get("items") or []
        ctx.mcp_server_id = next((str(i.get("id")) for i in items if i.get("name") == name), "")
    return f"registered MCP server {name}; listed under Capabilities and under Skills"


# ----------------------------------------------------------------------------- R2
def agent_with_skill(page, ctx: Context, goto: Callable[[str], None]) -> str:
    """Create an agent and give it, on create, the R1 skill and the R1 MCP server; then check
    the GRANT, not the wizard's screen. The wizard used to finish "is in the Studio" while
    writing only `requested_skills`, which the runtime never reads (map.json R2a)."""
    name = f"E2E Agent {ctx.stamp}"
    problems: list[str] = []
    goto("workforce")
    _visible(page, "agents-view-directory").click()
    _visible(page, "wf-new-agent").click()
    _visible(page, "aw-dialog")
    # A preset fills the prompts and rules the wizard requires; the journey is about
    # creating and assigning, not about typing a system prompt.
    page.locator('[data-testid^="aw-pattern-"]:not([data-testid="aw-pattern-blank"])').first.click()
    _visible(page, "aw-name").fill(name)
    picked, mcp_picked = "", False
    for _ in range(8):
        boxes = page.locator('input[data-testid^="aw-skill-"]')
        if not picked and boxes.count():
            wanted = page.locator(f'input[data-testid="aw-skill-{ctx.skill_key}"]')
            if ctx.skill_key and wanted.count():
                wanted.first.check()
                picked = ctx.skill_key
            else:
                offered = [boxes.nth(i).get_attribute("data-testid") for i in range(
                    min(boxes.count(), 60))]
                if ctx.skill_key:
                    # Said, and the agent is still created with another skill: agent_update
                    # and step_agents need an agent, and one missing control must not hide
                    # what those two would find.
                    problems.append(
                        f"the agent wizard does not offer skill {ctx.skill_key}, created a "
                        f"moment ago on the Skills screen, so it cannot be assigned on create "
                        f"(it offers {len(offered)}: {', '.join(o[9:] for o in offered[:6] if o)}…)")
                boxes.first.check()
                picked = (offered[0] or "")[len("aw-skill-"):]
        mcp_box = page.locator(f'input[data-testid="aw-mcp-{ctx.mcp_server_id}"]')
        if not mcp_picked and ctx.mcp_server_id and mcp_box.count():
            mcp_box.first.check()
            mcp_picked = True
        if page.locator('[data-testid="aw-publish"]').count():
            break
        _visible(page, "aw-next").click()
        page.wait_for_timeout(300)
    if not picked:
        raise JourneyFailed("the agent wizard never offered a skill to assign")
    if not mcp_picked:
        problems.append(f"the agent wizard offers no way to give the agent an MCP server "
                        f"(no [aw-mcp-{ctx.mcp_server_id or '<server id>'}] for {ctx.mcp_name or 'the R1 server'})"
                        f", so R2 'MCP servers assigned on create' cannot be done")
    _visible(page, "aw-publish").click()
    deadline = time.time() + WAIT_MS / 1000
    finished = False
    while time.time() < deadline:
        if page.locator('[data-testid="aw-complete"]').count():
            said = _text(page, "aw-complete")
            if name not in said:
                raise JourneyFailed(f"the wizard finished but does not name {name}: {said[:200]}")
            finished = True
            break
        result = page.locator('[data-testid="aw-result"]')
        if result.count() and "bad" in (result.first.get_attribute("class") or ""):
            raise JourneyFailed(f"publishing agent {name!r} with skill {picked} was refused: "
                                f"{_text(page, 'aw-result')}")
        page.wait_for_timeout(500)
    if not finished:
        raise JourneyFailed(f"publishing agent {name!r} showed neither a result nor the finished "
                            f"screen within {WAIT_MS // 1000}s")
    ctx.agent_name = name
    if ctx.target is not None:
        role = _role_of(ctx, name)
        if not role:
            problems.append(f"the wizard says {name!r} is in the Studio, but no agent of that "
                            f"name is on the roster (GET /v1/roles)")
        else:
            ctx.agent_role = role
            held = _grants(ctx, role)
            if picked not in held:
                problems.append(f"agent {role} was created with skill {picked} ticked, but does "
                                f"not hold it (role_skills has {sorted(held) or 'nothing'}), so "
                                f"no run can use it")
            if mcp_picked:
                # The tick is a request; the grant is what a run gets. Read it back.
                missing = _mcp_not_held(ctx, role, ctx.mcp_server_id, ctx.mcp_name,
                                        "was created with it ticked in the wizard")
                if missing:
                    problems.append(missing)
                    mcp_picked = False
    elif mcp_picked:
        problems.append("ticked an MCP server in the wizard with no API to read the grant back "
                        "from (the walk was given no target)")
    if problems:
        raise JourneyFailed("; ".join(problems))
    return (f"created agent {name!r} ({ctx.agent_role or 'role unknown'}) holding skill {picked}"
            f"{' and MCP server ' + ctx.mcp_name + ' (both read back as grants)' if mcp_picked else ''}")


def _open_agent(page, goto: Callable[[str], None], role: str) -> None:
    goto("workforce")
    # Agents opens on Live; the agent cards are under Directory.
    _visible(page, "agents-view-directory").click()
    _visible(page, f"wf-open-{role}").click()
    _visible(page, "agent-detail")
    _visible(page, "agent-skills")


def agent_update(page, ctx: Context, goto: Callable[[str], None]) -> str:
    """Change an existing agent: give it a second skill and an MCP server from its own page,
    reload, and check it stuck — on screen and in the grant the runtime reads."""
    role = ctx.agent_role
    if not role:
        raise JourneyFailed("there is no agent to update: agent_with_skill did not leave one "
                            "(run it first; it failed or was skipped)")
    problems: list[str] = []
    _open_agent(page, goto, role)
    before = _grants(ctx, role) if ctx.target is not None else set()
    _visible(page, "agent-skill-open").click()
    offered = [o["value"] for o in _options(page, "agent-skill-select") if o["value"]]
    extra = next((s for s in offered if s not in before and s != ctx.skill_key), "")
    if not extra:
        raise JourneyFailed(f"the agent page offers no further skill to give {role} "
                            f"(it offers {offered[:6]})")
    _visible(page, "agent-skill-select").select_option(extra)
    _visible(page, "agent-skill-confirm").click()
    deadline = time.time() + 30
    while time.time() < deadline:
        if extra in _text(page, "agent-skills-held"):
            break
        if _present(page, "agent-skills-failed"):
            raise JourneyFailed(f"giving {role} skill {extra} said: "
                                f"{_text(page, 'agent-skills-failed')}")
        page.wait_for_timeout(500)
    given_id, given_name = _give_mcp_on_page(page, ctx, role, problems)
    # From a fresh load: what is shown now came from the server.
    _open_agent(page, goto, role)
    held_text = _text(page, "agent-skills-held")
    if extra not in held_text:
        problems.append(f"gave {role} skill {extra}; after a reload its page lists only: "
                        f"{held_text[:200] or 'nothing'}")
    if ctx.target is not None and extra not in _grants(ctx, role):
        problems.append(f"gave {role} skill {extra} on its page, but role_skills does not hold it")
    if given_id:
        problems.extend(p for p in (
            _mcp_not_listed_after_reload(page, role, given_name),
            _mcp_not_held(ctx, role, given_id, given_name, "was given it on its page")) if p)
    if problems:
        raise JourneyFailed("; ".join(dict.fromkeys(problems)))
    return (f"gave {role} skill {extra} and MCP server {given_name} from its page; both still "
            f"there after a reload and read back as grants")


def _mcp_not_held(ctx: Context, role: str, server_id: str, name: str, how: str) -> str:
    """'' when `role` holds `server_id` in the grant the runtime reads; else the sentence."""
    held = _mcp_grants(ctx, role)
    if held is None:
        return NO_MCP_GRANT_API
    if server_id not in held:
        return (f"agent {role} {how}, but does not hold MCP server {name} (role-mcp-servers "
                f"has {sorted(held) or 'nothing'}), so no run can use it")
    return ""


def _mcp_not_listed_after_reload(page, role: str, name: str) -> str:
    """'' when the agent's freshly loaded page lists `name` among the servers it holds. Required
    whether or not that list renders: an agent holding nothing shows `agent-mcp-empty` instead,
    and round three skipped the check exactly then, so a failed grant passed silently."""
    try:
        _wait_for_text(page, "agent-mcp-held", re.escape(name), timeout_s=30)
        return ""
    except JourneyFailed:
        shown = _text(page, "agent-mcp-held") or _text(page, "agent-mcp-empty")
        return (f"gave {role} MCP server {name}; after a reload its page lists "
                f"{shown[:200] or 'no MCP server'}")


def _give_mcp_on_page(page, ctx: Context, role: str, problems: list[str]) -> tuple[str, str]:
    """Give `role` an MCP server it does not hold, from its page: open the picker, pick, confirm,
    and wait for the page to list it or say why not. (server id, name), or ('', '') with the
    reason appended to `problems`."""
    if not ctx.mcp_server_id:
        problems.append("there is no MCP server to give: mcp_register did not leave one (run it "
                        "first; it failed or was skipped)")
        return "", ""
    held = _mcp_grants(ctx, role)
    if held is None:
        problems.append(NO_MCP_GRANT_API)
        held = set()
    server_id, name = ctx.mcp_server_id, ctx.mcp_name
    if server_id in held:
        server_id, name = _second_mcp_server(ctx)
    # The picker is behind "Give this agent an MCP server": not rendered until that is pressed.
    deadline = time.time() + 15
    while time.time() < deadline and not (_present(page, "agent-mcp-open")
                                          or _present(page, "agent-mcp-select")):
        page.wait_for_timeout(500)
    if _present(page, "agent-mcp-open"):
        _visible(page, "agent-mcp-open").click()
    try:
        picker = _visible(page, "agent-mcp-select", timeout=10_000)
    except Exception:  # noqa: BLE001
        problems.append(f"the agent page offers no way to give {role} an MCP server (no "
                        f"[agent-mcp-open] opening an [agent-mcp-select]), so R2 'MCP servers "
                        f"assigned on update' cannot be done")
        return "", ""
    try:
        picker.select_option(server_id, timeout=15_000)
    except Exception:  # noqa: BLE001
        offered = [o["text"] for o in _options(page, "agent-mcp-select") if o["value"]]
        problems.append(f"the agent page's MCP picker does not offer {name} (it offers "
                        f"{len(offered)}: {', '.join(offered[:6]) or 'nothing'})")
        return "", ""
    _visible(page, "agent-mcp-confirm").click()
    deadline = time.time() + 30
    while time.time() < deadline:
        if name in _text(page, "agent-mcp-held"):
            return server_id, name
        if _present(page, "agent-mcp-failed"):
            problems.append(f"giving {role} MCP server {name} said: "
                            f"{_text(page, 'agent-mcp-failed')}")
            return "", ""
        page.wait_for_timeout(500)
    problems.append(f"gave {role} MCP server {name}; within 30s the page neither listed it nor "
                    f"said why (it shows {_text(page, 'agent-mcp-held')[:160] or 'no held servers'})")
    return server_id, name


# ----------------------------------------------------------------------------- R3
def _define_step(page, ctx: Context, goto: Callable[[str], None], label: str,
                 choose: Callable[[list[dict[str, str]]], str], what: str) -> str:
    """Define one custom step on Your steps, bound by `choose(options)`; its step key, or ''
    with the reason recorded by the caller (raised as JourneyFailed)."""
    goto("delivery_workflows")
    _visible(page, "dw-tab-steps").click()
    _visible(page, "dw-define-step").click()
    _visible(page, "dw-define-form")
    if _present(page, "dw-define-empty"):
        raise JourneyFailed(f"cannot bind a step to {what}: the step builder says "
                            f"{_text(page, 'dw-define-empty')!r}")
    options = [o for o in _options(page, "dw-define-binding") if o["value"]]
    value = choose(options)
    if not value:
        listed = ", ".join(f"{o['group']}: {o['text']}" for o in options[:10])
        raise JourneyFailed(f"the step builder's 'Runs' list does not offer {what} "
                            f"(it offers {len(options)}: {listed or 'nothing'})")
    _visible(page, "dw-define-label").fill(label)
    _visible(page, "dw-define-binding").select_option(value)
    _visible(page, "dw-define-save").click()
    deadline = time.time() + 60
    while time.time() < deadline:
        if _present(page, "dw-define-error"):
            raise JourneyFailed(f"defining step {label!r} ({what}) said: "
                                f"{_text(page, 'dw-define-error')}")
        if label in _text(page, "dw-steps-list"):
            break
        page.wait_for_timeout(500)
    else:
        raise JourneyFailed(f"defined step {label!r} ({what}); it never appeared under Your steps")
    body = _api(ctx).get("/v1/delivery-workflows/custom-steps",
                         project_id=ctx.target.project_id) or {}
    key = next((str(s.get("step_key")) for s in body.get("steps") or []
                if s.get("label") == label), "")
    if not key:
        raise JourneyFailed(f"step {label!r} is listed on screen but the API has no such step")
    return key


def step_bindings(page, ctx: Context, goto: Callable[[str], None]) -> str:
    """R3: a step bound to the R1 skill and one bound to a tool of the R1 MCP server, placed in
    a workflow, saved, reloaded, and RUN — and the run shows the skill ran and the fake MCP
    server was called. Round two's "R3" journey added two built-in palette steps."""
    if not (ctx.skill_key and ctx.mcp_server_id):
        raise JourneyFailed("needs the skill and the MCP server from the R1 journeys "
                            f"(skill {ctx.skill_key or 'missing'}, MCP server "
                            f"{ctx.mcp_name or 'missing'}); run them first")
    problems: list[str] = []
    keys: dict[str, str] = {}
    skill_label, mcp_label = f"E2E skill step {ctx.stamp}", f"E2E MCP step {ctx.stamp}"
    try:
        keys["skill"] = _define_step(
            page, ctx, goto, skill_label,
            lambda opts: next((o["value"] for o in opts if o["value"] == f"skill:{ctx.skill_key}"), ""),
            f"skill {ctx.skill_key} (made on the Skills screen)")
    except JourneyFailed as exc:
        problems.append(str(exc))
    try:
        keys["mcp"] = _define_step(
            page, ctx, goto, mcp_label,
            lambda opts: next((o["value"] for o in opts
                               if o["value"] == f"mcp:{ctx.mcp_server_id}:{MCP_TOOL}"), ""),
            f"tool {MCP_TOOL} of MCP server {ctx.mcp_name}")
    except JourneyFailed as exc:
        problems.append(str(exc))
    if not keys:
        raise JourneyFailed("; ".join(problems))

    name = f"E2E bound steps {ctx.stamp}"
    _new_workflow(page, goto, name)
    for kind, key in keys.items():
        palette = page.locator(f'[data-testid="dw-palette-custom-{key}"]')
        if not palette.count():
            problems.append(f"the {kind} step {key} is not in the builder's palette")
            continue
        if not palette.first.is_enabled():
            problems.append(f"the {kind} step {key} cannot be placed: "
                            f"{palette.first.get_attribute('title') or 'no reason given'}")
            continue
        _add_from_palette(page, f"dw-palette-custom-{key}")
    placed = len(_step_rows(page))
    if not placed:
        raise JourneyFailed("; ".join(problems) or "no bound step could be placed")
    _save_and_reopen(page, goto, name, placed)
    workflow_key = _workflow_key(ctx, name)
    saved = {str((s.get("binding") or {}).get("custom_step") or "") for s in _saved_steps(ctx, workflow_key)}
    lost = [k for k in keys.values() if k not in saved]
    if lost:
        problems.append(f"saved {name!r} with steps {sorted(keys.values())}; the saved spec binds "
                        f"{sorted(saved - {''}) or 'none of them'}")

    calls_before = len(_mcp_calls(ctx))
    outcome, steps = _run(ctx, workflow_key, {"note": f"E2E journey {ctx.stamp}"})
    by_key = {str((s.get("binding_snapshot") or {}).get("custom_step") or ""): s for s in steps}
    said: list[str] = []
    skill_step = by_key.get(keys.get("skill", "\0"))
    if "skill" in keys:
        ran = ((skill_step or {}).get("output") or {}).get("_step") or {}
        if not skill_step or skill_step.get("status") != "succeeded" \
                or ran.get("skill_id") != ctx.skill_key:
            problems.append(f"the step bound to skill {ctx.skill_key} did not run it: "
                            f"{_step_line(skill_step) if skill_step else 'no such step in the run'}")
        else:
            said.append(f"skill step ran {ctx.skill_key}")
    mcp_step = by_key.get(keys.get("mcp", "\0"))
    if "mcp" in keys:
        new_calls = [c for c in _mcp_calls(ctx)[calls_before:] if c.get("tool") == MCP_TOOL]
        if not mcp_step or mcp_step.get("status") != "succeeded" or not new_calls:
            problems.append(f"the step bound to {ctx.mcp_name} · {MCP_TOOL} did not call it "
                            f"({len(new_calls)} call(s) reached the MCP fake): "
                            f"{_step_line(mcp_step) if mcp_step else 'no such step in the run'}")
        else:
            said.append(f"MCP step called {MCP_TOOL} ({len(new_calls)} call)")
    if outcome.verdict not in ("pass", "at an expected human gate") and not problems:
        problems.append(f"run {outcome.run_id} ended {outcome.status}: "
                        f"{'; '.join(outcome.problems[:3]) or outcome.verdict}")
    if problems:
        raise JourneyFailed("; ".join(problems))
    return f"built, saved and reopened {name!r}; run {outcome.run_id}: {'; '.join(said)}"


# ----------------------------------------------------------------------------- R4
def step_agents(page, ctx: Context, goto: Callable[[str], None]) -> str:
    """R4: two agent steps — one naming the agent R2 made, one naming nobody — saved, reloaded
    and RUN; the run must record the named agent on the first and an auto-picked one, with its
    reason, on the second. Round two's "R4" journey pressed "Advance now"."""
    problems: list[str] = []
    name = f"E2E agent steps {ctx.stamp}"
    _new_workflow(page, goto, name)
    _add_from_palette(page, "dw-palette-agent-work")
    _add_from_palette(page, "dw-palette-agent-work")
    rows = _step_rows(page)
    if len(rows) != 2:
        raise JourneyFailed(f"pressed 'Let an agent work it out' twice; the canvas has "
                            f"{len(rows)} step(s)")
    named_id, auto_id = rows
    marker = f"E2E journey {ctx.stamp}"
    asks = {
        named_id: (f"{AGENT_STEP_MARKER} ({marker}): use the skill {ctx.skill_key or 'you hold'} "
                   f"and say what it returns."),
        auto_id: f"{AGENT_STEP_MARKER} ({marker}): say what you can do with the skills you hold.",
    }
    for step_id, said in asks.items():
        _visible(page, f"dw-agentwork-mode-{step_id}").select_option("fixed")
        _visible(page, f"dw-agentwork-text-{step_id}").fill(said)
    offered = [o["value"] for o in _options(page, f"dw-draft-captain-{named_id}") if o["value"]]
    captain = ctx.agent_role if ctx.agent_role in offered else ""
    if not captain:
        problems.append(
            f"the step's agent picker does not offer {ctx.agent_role or 'the agent R2 made'} "
            f"(it offers {len(offered)}: {', '.join(offered[:8])}), so a step cannot be given an "
            f"agent the operator created")
        # Fall back to an agent that HOLDS a skill, so the run still shows whether naming an
        # agent and auto-picking one work; one that holds nothing is refused at the gate.
        holders = {str(a.get("role_key")) for a in (_api(ctx).get(
            "/v1/role-skills", project_id=ctx.target.project_id) or {}).get("assignments") or []}
        captain = next((o for o in offered if o in holders), offered[0] if offered else "")
    if not captain:
        raise JourneyFailed("; ".join(problems) + "; and it offers no agent at all")
    _visible(page, f"dw-draft-captain-{named_id}").select_option(captain)
    _save_and_reopen(page, goto, name, 2)
    shown = {s: _visible(page, f"dw-draft-captain-{s}").input_value() for s in (named_id, auto_id)}
    if shown != {named_id: captain, auto_id: ""}:
        problems.append(f"named {captain} on the first step and nobody on the second; after a "
                        f"reload the pickers say {shown}")
    workflow_key = _workflow_key(ctx, name)
    saved = {str(s.get("id")): str(s.get("captain") or "") for s in _saved_steps(ctx, workflow_key)}
    if saved.get(named_id) != captain or saved.get(auto_id):
        problems.append(f"the saved spec names agents {saved}, not {named_id}={captain} and "
                        f"{auto_id}=nobody")

    outcome, steps = _run(ctx, workflow_key, {})
    by_id = {str(s.get("step_id")): s for s in steps}
    named, auto = by_id.get(named_id) or {}, by_id.get(auto_id) or {}
    named_out, auto_out = named.get("output") or {}, auto.get("output") or {}
    if named.get("status") != "succeeded" or named_out.get("agent") != captain:
        problems.append(f"the step naming {captain} ran as {named_out.get('agent') or 'nobody'}: "
                        f"{_step_line(named) if named else 'no such step in the run'}")
    elif ctx.skill_key and captain == ctx.agent_role \
            and ctx.skill_key not in (named_out.get("skills_offered") or []):
        problems.append(f"{captain} holds {ctx.skill_key} since R2, but its step was offered only "
                        f"{named_out.get('skills_offered') or 'nothing'}")
    if auto.get("status") != "succeeded" or not auto_out.get("agent") \
            or not str(auto_out.get("chosen_because") or "").strip():
        problems.append(f"the step naming nobody was not given an agent with a reason "
                        f"(agent {auto_out.get('agent') or 'none'}, because "
                        f"{auto_out.get('chosen_because') or 'nothing'}): "
                        f"{_step_line(auto) if auto else 'no such step in the run'}")
    if problems:
        raise JourneyFailed("; ".join(problems))
    return (f"run {outcome.run_id}: named step ran as {captain}; unnamed step auto-picked "
            f"{auto_out.get('agent')} ({auto_out.get('chosen_how')}: "
            f"{str(auto_out.get('chosen_because'))[:100]})")


# ----------------------------------------------------------------------------- R6 smoke checks
def workflow_build(page, ctx: Context, goto: Callable[[str], None]) -> str:
    """A console smoke check of the builder (R6): two built-in palette steps, saved, and still
    two after a reload. It binds nothing and names no agent — R3 and R4 are step_bindings and
    step_agents."""
    name = f"E2E two-step {ctx.stamp}"
    _new_workflow(page, goto, name)
    palette = page.locator('[data-testid^="dw-palette-"]:not([data-testid="dw-palette-agent-work"])'
                           ':not([data-testid^="dw-palette-custom-"]):enabled')
    if palette.count() < 2:
        raise JourneyFailed(f"the step palette offers {palette.count()} built-in steps; a "
                            f"two-step workflow needs two")
    chosen = [palette.nth(i).get_attribute("data-testid") or "" for i in range(2)]
    for testid in chosen:
        _add_from_palette(page, testid)
    meta = _save_and_reopen(page, goto, name, 2)
    return (f"built {name!r} from {', '.join(c[len('dw-palette-'):] for c in chosen if c)}; "
            f"saved, and after a reload the header says {meta[:40]!r}")


def advance_now(page, ctx: Context, goto: Callable[[str], None]) -> str:
    """A console smoke check (R6): "Advance now" answers with a sentence, not a 403 — the
    boss-1 bug where it sent the console key to a worker-only route."""
    goto("delivery_workflows")
    _visible(page, "dw-tab-running").click()
    button = page.locator('[data-testid="dw-advance"]')
    try:
        button.first.wait_for(state="visible", timeout=20_000)
    except Exception:  # noqa: BLE001
        raise JourneyFailed("'Advance now' is not on the Running tab: this install has no run "
                            "to show. Run `workflows` first (as `all` does)") from None
    button.first.click()
    said = _wait_for_text(page, "dw-notice", r"advance|nothing to advance")
    if said.lower().startswith("advance —") or re.search(r"\b40[13]\b|forbidden", said, re.I):
        raise JourneyFailed(f"'Advance now' answered: {said}")
    return f"'Advance now' said: {said[:160]}"


# ----------------------------------------------------------------------------- R7
GATED_EXAMPLE = "weekly-newsletter"   # its send step waits for a person's approval


def _open_gate(ctx: Context) -> tuple[str, str]:
    """(run_id, decision_id) of a run held at a gate waiting on the operator: one already
    waiting, else the gated example started and followed to its gate, which is left open."""
    api = _api(ctx)
    runs = (api.get("/v1/delivery-workflows/runs", project_id=ctx.target.project_id,
                    limit=50) or {}).get("runs") or []
    for run in runs:
        for step in run.get("steps") or []:
            if step.get("decision_id") and step.get("decision_status") == "pending" \
                    and str(step.get("status")) == "waiting":
                return str(run.get("id")), str(step.get("decision_id"))
    from .discover import examples
    from .drive import Driver, _run_detail

    example = next((e for e in examples() if e.directory == GATED_EXAMPLE), None)
    if example is None:
        raise JourneyFailed(f"no run is waiting at a gate, and examples/{GATED_EXAMPLE} is gone, "
                            f"so there is no gate to decide from a card")
    driver = Driver(ctx.target, approve_gates=False, install_catalogue=False)
    outcome = driver.run_example(example)
    if not outcome.run_id:
        raise JourneyFailed(f"starting {example.workflow_key} for a gate failed: "
                            f"{'; '.join(outcome.problems) or outcome.status}")
    _, steps = _run_detail(driver.api, outcome.run_id)
    held = next((s for s in steps if s.get("status") == "waiting" and s.get("decision_id")), None)
    if not held:
        raise JourneyFailed(f"{example.workflow_key} ran without stopping at its approval: "
                            f"{', '.join(_step_line(s) for s in steps)[:400]}")
    return outcome.run_id, str(held["decision_id"])


def _pill_count(ctx: Context) -> int:
    body = _api(ctx).get("/v1/agents/live", project_id=ctx.target.project_id) or {}
    return int(((body.get("attention") or {}).get("count")) or 0)


def _steps_done(ctx: Context, run_id: str) -> int:
    runs = (_api(ctx).get("/v1/delivery-workflows/runs", project_id=ctx.target.project_id,
                          limit=50) or {}).get("runs") or []
    run = next((r for r in runs if str(r.get("id")) == run_id), {})
    return int(run.get("steps_done") or 0)


def live_gate(page, ctx: Context, goto: Callable[[str], None]) -> str:
    """Decide a gate from Agents › Live (R7): the run's row says it waits on you, its amber You
    hop focuses the card, Approve on the card approves THAT decision, the run moves on, and the
    pill falls by one and still counts what every other surface counts."""
    if ctx.target is None:
        raise JourneyFailed("this journey reads the decision and the run back through the API, "
                            "and the walk was given no target")
    run_id, decision_id = _open_gate(ctx)
    api = _api(ctx)
    board = api.get("/v1/agents/live-board", project_id=ctx.target.project_id) or {}
    card = next((a for a in board.get("attention") or [] if str(a.get("id")) == decision_id), None)
    if card is None:
        raise JourneyFailed(f"decision {decision_id} holds run {run_id} but Live's attention "
                            f"list does not have it")
    approve = next((a for a in card.get("actions") or [] if a.get("action") == "approve"), None)
    if not card.get("decide_inline") or not approve:
        raise JourneyFailed(f"the gate on {run_id} cannot be decided from its card "
                            f"(decide_inline={card.get('decide_inline')}, risk "
                            f"{card.get('risk_tier')}); Live offers Review ↗ only")
    pill_before, done_before = _pill_count(ctx), _steps_done(ctx, run_id)

    goto("workforce")
    _visible(page, "agents-view-live").click()
    _visible(page, "ag-live")
    _wait_for_text(page, f"ag-mission-{run_id}", r"Waiting on you", timeout_s=30)
    _visible(page, f"ag-mission-you-{run_id}").click()
    page.wait_for_timeout(300)
    focused = page.evaluate(
        "(id) => { const c = document.querySelector(`[data-testid=\"ag-att-${id}\"]`);"
        " return !!c && (c === document.activeElement || c.contains(document.activeElement)); }",
        decision_id)
    if not focused:
        raise JourneyFailed(f"pressing the You hop on {run_id} did not focus the card for "
                            f"decision {decision_id}")
    _visible(page, f"ag-att-{decision_id}").get_by_role(
        "button", name=str(approve.get("label")), exact=True).first.click()

    deadline = time.time() + 30
    status = ""
    while time.time() < deadline:
        status = str((api.get(f"/v1/decisions/{decision_id}") or {}).get("status") or "")
        if status == "approved":
            break
        page.wait_for_timeout(500)
    problems: list[str] = []
    if status != "approved":
        problems.append(f"pressed {approve.get('label')!r} on the card; decision {decision_id} "
                        f"is {status or 'unreadable'}, not approved")
    deadline = time.time() + 120
    done_after = done_before
    while time.time() < deadline and done_after <= done_before:
        page.wait_for_timeout(1000)
        done_after = _steps_done(ctx, run_id)
    if done_after <= done_before:
        problems.append(f"approved the gate, but run {run_id} did not move on "
                        f"({done_before} steps done before, {done_after} after two minutes)")
    pill_after = _pill_count(ctx)
    if pill_after != pill_before - 1:
        problems.append(f"the pill counted {pill_before} before and {pill_after} after one "
                        f"approval")
    items = (api.get("/v1/decisions/inbox", project_id=ctx.target.project_id) or {}).get("items") or []
    inbox = sum(1 for i in items if i.get("attention") == "needs_action")
    board = api.get("/v1/agents/live-board", project_id=ctx.target.project_id) or {}
    others = sum(1 for a in board.get("attention") or [] if a.get("kind") != "approval")
    if pill_after != inbox + others:
        problems.append(f"the pill says {pill_after} need you; Approvals has {inbox} and Live "
                        f"{others} more that are not decisions")
    if problems:
        raise JourneyFailed("; ".join(problems))
    return (f"run {run_id}: Waiting on you → You hop focused its card → {approve.get('label')} → "
            f"approved; {done_before}→{done_after} steps done; pill {pill_before}→{pill_after}")


# In order: each later journey uses what the earlier ones made (the skill, the server, the agent).
JOURNEYS: tuple[tuple[str, str, Callable[..., str]], ...] = (
    ("R1", "skill_create", skill_create),
    ("R1", "mcp_register", mcp_register),
    ("R2", "agent_with_skill", agent_with_skill),
    ("R2", "agent_update", agent_update),
    ("R3", "step_bindings", step_bindings),
    ("R4", "step_agents", step_agents),
    ("R6", "workflow_build", workflow_build),
    ("R6", "advance_now", advance_now),
    ("R7", "live_gate", live_gate),
)


def run_journey(fn: Callable[..., str], page, ctx: Context,
                goto: Callable[[str], None]) -> tuple[bool, str]:
    try:
        return True, fn(page, ctx, goto)
    except JourneyFailed as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 — a missing control is the journey's failure
        return False, f"{type(exc).__name__}: {' '.join(str(exc).split())[:400]}"


__all__: list[Any] = ["JOURNEYS", "JOURNEY_SELECTORS", "PENDING_SELECTORS", "REQUIRES",
                      "Context", "run_journey"]

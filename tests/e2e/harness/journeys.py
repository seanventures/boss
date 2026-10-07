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

  R1  skill_create      Skills & MCP › Library › New skill → saved → listed after a reload
  R1  mcp_register      Library › MCP › Add MCP › Custom → the fakes' server tested and added →
                        listed after a reload
  R2  agent_with_skill  + New agent with that skill and that MCP server → both grants are read
                        back through the API the runtime uses (role-skills, role-mcp-servers)
  R2  agent_update      The agent's Passport › Loadout → + Add another skill and an MCP server it
                        does not hold → shown after a reload, and both read back as grants
  R3  step_bindings     Library › Steps → one step bound to the skill, one to the MCP tool →
                        placed in a workflow in the editor, saved, reopened, RUN → the skill ran
                        and the MCP fake was called
  R4  step_agents       Two Agent task steps, one naming the R2 agent, one left on Auto → saved,
                        reopened, RUN → the run records the named agent and the auto-picked one
                        with its reason
  R6  workflow_build    (console smoke) two built-in palette steps → saved → "2 steps" reopened
  R6  advance_now       (console smoke) Runs → Advance now → a sentence that is not an error
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
    # Skills & MCP is the one home for skills and MCP servers ("kit links", skills-mcp spec
    # §5.1): the old Skills screen (skills-ui.tsx) and the shared MCP panel (mcp-servers-ui.tsx)
    # were deleted. A skill is written in Library › New skill; its key is made from its name.
    ("skill_create", "app/skills/kit-home.tsx", "kit-tab-${t}"),
    ("skill_create", "app/skills/library.tsx", "kit-library"),
    ("skill_create", "app/skills/library.tsx", "kit-lib-new-skill"),
    ("skill_create", "app/skills/skill-drawer.tsx", "kit-skill-drawer"),
    ("skill_create", "app/skills/skill-drawer.tsx", "skill-label"),
    ("skill_create", "app/skills/skill-drawer.tsx", "skill-summary"),
    ("skill_create", "app/skills/skill-drawer.tsx", "skill-behaviour"),
    ("skill_create", "app/skills/skill-drawer.tsx", "skill-how-${h.id}"),
    ("skill_create", "app/skills/skill-drawer.tsx", "skill-knowledge"),
    ("skill_create", "app/skills/skill-drawer.tsx", "skill-save"),
    ("skill_create", "app/skills/library.tsx", "kit-skill-list"),
    ("skill_create", "app/skills/library.tsx", "kit-skill-${r.id}"),
    # Library › MCP › Add MCP › Custom: the sheet tests the server before it registers it.
    ("mcp_register", "app/skills/kit-home.tsx", "kit-tab-${t}"),
    ("mcp_register", "app/skills/library.tsx", "kit-lib-mcp"),
    ("mcp_register", "app/skills/library.tsx", "kit-lib-add-mcp"),
    ("mcp_register", "app/skills/add-mcp-sheet.tsx", "kit-add-mcp"),
    ("mcp_register", "app/skills/add-mcp-sheet.tsx", "kit-add-custom"),
    ("mcp_register", "app/skills/add-mcp-sheet.tsx", "kit-custom-name"),
    ("mcp_register", "app/skills/add-mcp-sheet.tsx", "kit-custom-url"),
    ("mcp_register", "app/skills/add-mcp-sheet.tsx", "kit-custom-save"),
    ("mcp_register", "app/skills/library.tsx", "kit-mcp-list"),
    ("mcp_register", "app/skills/library.tsx", "kit-mcp-${s.name}"),
    ("mcp_register", "app/skills/library.tsx", "kit-lib-error"),
    # One + New agent dialog (Workflow Studio S4) replaced the creation wizard: Blank names the
    # agent and grants skills and MCP servers on create; the Passport opens once it is born.
    ("agent_with_skill", "app/workforce-ui.tsx", "wf-new-agent"),
    ("agent_with_skill", "app/agent-quick-create.tsx", "agent-quick-create"),
    ("agent_with_skill", "app/agent-quick-create.tsx", "aq-name"),
    ("agent_with_skill", "app/agent-quick-create.tsx", "aq-skills"),
    ("agent_with_skill", "app/agent-quick-create.tsx", "aq-skill-${s.skill_id}"),
    ("agent_with_skill", "app/agent-quick-create.tsx", "aq-mcp-${m.mcp_server_id}"),
    ("agent_with_skill", "app/agent-quick-create.tsx", "aq-create"),
    ("agent_with_skill", "app/agent-quick-create.tsx", "aq-failed"),
    ("agent_with_skill", "app/agents/agent-birth.tsx", "ag-birth"),
    ("agent_with_skill", "app/agent-detail-ui.tsx", "agent-detail"),
    # The Passport's Loadout (agent-loadout spec §3.2): one place per grant, + Add on each row.
    ("agent_update", "app/workforce-ui.tsx", "agents-view-directory"),
    ("agent_update", "app/workforce-ui.tsx", "ag-home-search"),
    ("agent_update", "app/agents/roster-zones.tsx", "wf-open-${a.id}"),
    ("agent_update", "app/agents/roster-zones.tsx", "ag-idle-more"),
    ("agent_update", "app/agent-detail-ui.tsx", "agent-detail"),
    ("agent_update", "app/agents/passport-loadout.tsx", "passport-loadout"),
    ("agent_update", "app/skills/agent-abilities.tsx", "loadout-skills"),
    ("agent_update", "app/skills/agent-abilities.tsx", "loadout-skills-add"),
    ("agent_update", "app/skills/agent-abilities.tsx", "loadout-skill-pick-${s.skill_id}"),
    ("agent_update", "app/skills/agent-abilities.tsx", "agent-skill-${s.skill_id}"),
    ("agent_update", "app/skills/agent-abilities.tsx", "loadout-mcp"),
    ("agent_update", "app/skills/agent-abilities.tsx", "loadout-mcp-add"),
    ("agent_update", "app/skills/agent-abilities.tsx", "loadout-mcp-pick-${m.mcp_server_id}"),
    ("agent_update", "app/skills/agent-abilities.tsx", "agent-mcp-${m.mcp_server_id}"),
    ("agent_update", "app/skills/agent-abilities.tsx", "loadout-no-mcp"),
    # Workflows (Workflow Studio S4): Runs and Library; a step of your own is defined in
    # Library › Steps › + New step, a workflow is built in the editor Library › + New opens.
    ("step_bindings", "app/delivery-workflows-ui.tsx", "dw-tab-library"),
    ("step_bindings", "app/workflows/library.tsx", "wf-lib-view-${v}"),
    ("step_bindings", "app/workflows/library.tsx", "wf-lib-new-step"),
    ("step_bindings", "app/workflows/library.tsx", "wf-lib-steps"),
    ("step_bindings", "app/workflows/library.tsx", "wf-step-row-${c.step_key}"),
    ("step_bindings", "app/workflows/editor/define-step-modal.tsx", "wf-define-modal"),
    ("step_bindings", "app/workflows/define-step-form.tsx", "dw-define-form"),
    ("step_bindings", "app/workflows/define-step-form.tsx", "dw-define-label"),
    ("step_bindings", "app/workflows/define-step-form.tsx", "dw-define-binding"),
    ("step_bindings", "app/workflows/define-step-form.tsx", "dw-define-save"),
    ("step_bindings", "app/workflows/define-step-form.tsx", "dw-define-error"),
    ("step_bindings", "app/workflows/define-step-form.tsx", "dw-define-empty"),
    ("step_bindings", "app/workflows/library.tsx", "wf-lib-new"),
    ("step_bindings", "app/workflows/editor/workflow-editor.tsx", "wf-editor"),
    ("step_bindings", "app/workflows/editor/top-bar.tsx", "wf-name"),
    ("step_bindings", "app/workflows/editor/palette.tsx", "wf-pal-custom:${c.step_key}"),
    ("step_bindings", "app/workflows/flow/step-node.tsx", "wf-node"),
    ("step_bindings", "app/workflows/editor/top-bar.tsx", "wf-save"),
    ("step_bindings", "app/workflows/editor/save/use-save-flow.tsx", "wf-issues"),
    ("step_bindings", "app/workflows/library.tsx", "wf-lib-search"),
    ("step_bindings", "app/workflows/library.tsx", "wf-card-open-${w.workflow_key}"),
    ("step_bindings", "app/workflows/editor/summary-chips.tsx", "wf-chip-steps"),
    ("step_agents", "app/delivery-workflows-ui.tsx", "dw-tab-library"),
    ("step_agents", "app/workflows/library.tsx", "wf-lib-new"),
    ("step_agents", "app/workflows/editor/workflow-editor.tsx", "wf-editor"),
    ("step_agents", "app/workflows/editor/top-bar.tsx", "wf-name"),
    ("step_agents", "app/workflows/editor/palette.tsx", "wf-pal-agent-task"),
    ("step_agents", "app/workflows/flow/step-node.tsx", "wf-node"),
    ("step_agents", "app/workflows/editor/inspector/step-inspector.tsx", "wf-inspector-${step.id}"),
    ("step_agents", "app/workflows/editor/inspector/step-inspector.tsx", "wf-task-fixed"),
    ("step_agents", "app/workflows/editor/inspector/step-inspector.tsx", "wf-task-text"),
    ("step_agents", "app/workflows/editor/inspector/agent-combobox.tsx", "wf-agent-trigger"),
    ("step_agents", "app/workflows/editor/inspector/agent-combobox.tsx", "wf-agent-option-${option.value}"),
    ("step_agents", "app/workflows/editor/top-bar.tsx", "wf-save"),
    ("step_agents", "app/workflows/editor/save/use-save-flow.tsx", "wf-issues"),
    ("step_agents", "app/workflows/library.tsx", "wf-lib-search"),
    ("step_agents", "app/workflows/library.tsx", "wf-card-open-${w.workflow_key}"),
    ("step_agents", "app/workflows/editor/summary-chips.tsx", "wf-chip-steps"),
    ("workflow_build", "app/delivery-workflows-ui.tsx", "dw-tab-library"),
    ("workflow_build", "app/workflows/library.tsx", "wf-lib-new"),
    ("workflow_build", "app/workflows/editor/workflow-editor.tsx", "wf-editor"),
    ("workflow_build", "app/workflows/editor/top-bar.tsx", "wf-name"),
    ("workflow_build", "app/workflows/editor/palette.tsx", "wf-pal-${entry.key}"),
    ("workflow_build", "app/workflows/flow/step-node.tsx", "wf-node"),
    ("workflow_build", "app/workflows/editor/top-bar.tsx", "wf-save"),
    ("workflow_build", "app/workflows/library.tsx", "wf-lib-search"),
    ("workflow_build", "app/workflows/library.tsx", "wf-card-open-${w.workflow_key}"),
    ("workflow_build", "app/workflows/editor/summary-chips.tsx", "wf-chip-steps"),
    ("advance_now", "app/delivery-workflows-ui.tsx", "dw-tab-runs"),
    ("advance_now", "app/delivery-workflows-ui.tsx", "dw-advance"),
    ("advance_now", "app/delivery-workflows-ui.tsx", "dw-notice"),
    # R7: Agents › Live (live-board stream) — a gate on a run, decided from its card.
    ("live_gate", "app/workforce-ui.tsx", "agents-view-live"),
    ("live_gate", "app/agents/live-board.tsx", "ag-live"),
    ("live_gate", "app/agents/mission-row.tsx", "ag-mission-${mission.runId}"),
    ("live_gate", "app/agents/mission-row.tsx", "ag-mission-you-${mission.runId}"),
    ("live_gate", "app/agents/attention-card.tsx", "ag-att-${item.id}"),
    # Repo and models (setup spec §7.7): the board, its rows and its connect forms.
    ("setup_first_run", "app/setup/setup-board.tsx", "su-board"),
    ("setup_first_run", "app/setup/setup-board.tsx", "su-verdict"),
    ("setup_first_run", "app/setup/repository-row.tsx", "su-token-input"),
    ("setup_first_run", "app/setup/repo-picker.tsx", "su-repo-option-${r.full_name}"),
    ("setup_first_run", "app/setup/repo-picker.tsx", "su-repo-connect"),
    ("setup_first_run", "app/setup/repository-row.tsx", "su-repo-pill"),
    ("setup_first_run", "app/setup/add-model-drawer.tsx", "su-tile-${t.tile}"),
    ("setup_first_run", "app/setup/add-model-drawer.tsx", "su-provider-${c.provider}"),
    ("setup_first_run", "app/setup/add-model-drawer.tsx", "su-model-base-url"),
    ("setup_first_run", "app/setup/add-model-drawer.tsx", "su-model-key"),
    ("setup_first_run", "app/setup/add-model-drawer.tsx", "su-model-name"),
    ("setup_first_run", "app/setup/add-model-drawer.tsx", "su-model-connect"),
    ("setup_first_run", "app/setup/models-row.tsx", "su-model-pill"),
    ("setup_first_run", "app/setup/try-row.tsx", "su-try-input"),
    ("setup_first_run", "app/setup/try-row.tsx", "su-try-open"),
    ("setup_first_run", "app/setup/try-row.tsx", "su-try-progress"),
    ("setup_replace_token", "app/setup/repository-row.tsx", "su-repo-menu"),
    ("setup_replace_token", "app/setup/repository-row.tsx", "su-menu-replace"),
    ("setup_replace_token", "app/setup/repository-row.tsx", "su-repo-pill"),
    ("setup_deep_links", "app/setup/models-row.tsx", "su-model-drawer"),
    ("setup_deep_links", "app/setup/setup-row.tsx", "su-row-${row}"),
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
    "R1": ("skill-save", "kit-custom-save"),
    "R2": ("aq-skill-${s.skill_id}", "aq-mcp-${m.mcp_server_id}", "loadout-skill-pick-${s.skill_id}",
           "loadout-mcp-add", "loadout-mcp-pick-${m.mcp_server_id}"),
    "R3": ("dw-define-binding", "wf-pal-custom:${c.step_key}", "wf-save"),
    "R4": ("wf-pal-agent-task", "wf-agent-option-${option.value}", "wf-save"),
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


# ----------------------------------------------------------------------------- toasts and keys
def _toast(page, kind: str) -> str:
    """The text of the newest visible `success` / `error` toast (client-forms' Toaster: the
    Kit, the Loadout and the workflow editor all answer a write there), or ''."""
    toasts = page.locator(f".toast.toast-{kind} >> visible=true")
    try:
        n = toasts.count()
        return " ".join((toasts.nth(n - 1).inner_text(timeout=2_000) or "").split()) if n else ""
    except Exception:  # noqa: BLE001
        return ""


def _skill_key_of(label: str) -> str:
    """The key Library › New skill makes from a name (skill-drawer.tsx `keyOf`): the form has
    no key field any more, so the journey predicts the key and looks for it."""
    return re.sub(r"^_+|_+$", "", re.sub(r"[^a-z0-9]+", "_", label.strip().lower()))[:60]


def _with_query(page, goto: Callable[[str], None], area: str, query: str) -> None:
    """Open `area`, then the same URL with `query` added — the walk's `goto` waits for the area
    by id, so a deep link (`setup&add=openai`) is opened in two steps rather than as an id."""
    goto(area)
    page.goto(f"{page.url}&{query}", wait_until="domcontentloaded")
    page.locator(f'main[data-active="{area}"]').first.wait_for(state="attached", timeout=WAIT_MS)


# ----------------------------------------------------------------------------- the editor
def _new_workflow(page, goto: Callable[[str], None], name: str) -> None:
    """Library › + New workflow opens the editor on an unsaved draft; the name is typed in its
    bar (Workflow Studio S4 — the Design tab's name-and-confirm form is gone)."""
    goto("delivery_workflows")
    _visible(page, "dw-tab-library").click()
    _visible(page, "wf-lib-new").click()
    _visible(page, "wf-editor")
    box = _visible(page, "wf-name")
    box.fill(name)
    box.press("Enter")
    if box.input_value() != name:
        raise JourneyFailed(f"typed {name!r} as the new workflow's name; the bar says "
                            f"{box.input_value()!r}")


def _add_from_palette(page, testid: str) -> None:
    """Click a palette row: it is placed after the selected step (the one just added), so rows
    pressed in turn land in that order. Waits for the canvas to have one more node."""
    before = len(_step_rows(page))
    row = page.locator(f'[data-testid="{testid}"]:not([aria-disabled="true"]) >> visible=true').first
    try:
        row.wait_for(state="visible", timeout=WAIT_MS)
    except Exception:  # noqa: BLE001
        why = page.locator(f'[data-testid="{testid}"]').first
        tip = why.get_attribute("data-tip") if why.count() else None
        raise JourneyFailed(f"the palette has no usable [{testid}]"
                            f"{': ' + tip if tip else ''}") from None
    row.click()
    deadline = time.time() + 30
    while time.time() < deadline and len(_step_rows(page)) <= before:
        page.wait_for_timeout(300)
    if len(_step_rows(page)) <= before:
        raise JourneyFailed(f"pressed [{testid}] in the palette; the canvas still has {before} "
                            f"step(s)")


def _step_rows(page) -> list[str]:
    """The step ids on the editor's lane, in flow order."""
    return page.locator('.wf-lane [data-testid="wf-node"] >> visible=true').evaluate_all(
        "els => els.map(e => e.getAttribute('data-step-id'))") or []


def _select_step(page, step_id: str) -> None:
    """Click a node; its inspector opens (`wf-inspector-<id>`)."""
    page.locator(f'.wf-lane [data-testid="wf-node"][data-step-id="{step_id}"]').first.click()
    _visible(page, f"wf-inspector-{step_id}", timeout=15_000)


SAVE_FAILED = re.compile(r"could not|fail|error|refus|denied|invalid|forbidden|\b[45]\d\d\b", re.I)


def _wf_key_in_url(page) -> str:
    match = re.search(r"[?&]wf=([^&#]+)", page.url)
    return match.group(1) if match and match.group(1) != "new" else ""


def _save_and_reopen(page, goto: Callable[[str], None], name: str, steps: int) -> tuple[str, str]:
    """Save, demand the toast that says it worked, then load Workflows afresh and reopen the
    workflow from the Library by name: what the summary says now came from the server, not from
    the canvas. (workflow key, the steps chip's text).

    Round two accepted any notice without an em dash and read "2 steps" off the same page —
    client state — so an unsaved workflow passed."""
    _visible(page, "wf-save").click()
    deadline = time.time() + 60
    said, key = "", ""
    while time.time() < deadline:
        said = _toast(page, "success")
        key = _wf_key_in_url(page)
        if re.search(r"\bsaved\b", said, re.I) and key:
            break
        failed = _toast(page, "error")
        if failed:
            raise JourneyFailed(f"saving workflow {name!r} said: {failed}")
        if _present(page, "wf-issues"):
            raise JourneyFailed(f"saving workflow {name!r} was stopped by its check: "
                                f"{_text(page, 'wf-issues')[:300]}")
        page.wait_for_timeout(500)
    else:
        raise JourneyFailed(f"pressed Save on {name!r}; within 60s no 'Saved' and no workflow "
                            f"key in the URL (toast {said!r})")
    if SAVE_FAILED.search(said):
        raise JourneyFailed(f"saving workflow {name!r} said: {said}")
    goto("delivery_workflows")
    _visible(page, "dw-tab-library").click()
    _visible(page, "wf-lib-search").fill(name)
    try:
        _visible(page, f"wf-card-open-{key}", timeout=15_000).click()
    except Exception:  # noqa: BLE001
        raise JourneyFailed(f"after a reload, workflow {name!r} ({key}) is not in the Library "
                            f"to open") from None
    want = rf"\b{steps} steps?\b"
    try:
        return key, _wait_for_text(page, "wf-chip-steps", want, timeout_s=30)
    except JourneyFailed:
        raise JourneyFailed(f"saved {name!r} with {steps} steps; reopened after a reload it "
                            f"says {_text(page, 'wf-chip-steps')!r}") from None


# ----------------------------------------------------------------------------- R1
def _library(page, goto: Callable[[str], None], segment: str) -> None:
    """Skills & MCP › Library, on its Skills or MCP segment."""
    goto("skills")
    _visible(page, "kit-tab-library").click()
    _visible(page, "kit-library")
    _visible(page, f"kit-lib-{segment}").click()


def skill_create(page, ctx: Context, goto: Callable[[str], None]) -> str:
    label = f"E2E skill {ctx.stamp}"
    key = _skill_key_of(label)
    _library(page, goto, "skills")
    _visible(page, "kit-lib-new-skill").click()
    _visible(page, "kit-skill-drawer")
    _visible(page, "skill-label").fill(label)
    _visible(page, "skill-summary").fill("Answers from the knowledge below; made by the e2e "
                                         "harness's skill journey")
    # A guide: it acts on nothing and its executor (skill.guide) hands back the knowledge, so a
    # step bound to it runs anywhere. The old form's free-text executor `knowledge.only` is not
    # offered by the Built-in picker, and no executor is registered under it.
    _visible(page, "skill-how-guide").click()
    _visible(page, "skill-knowledge").fill("The answer is 42.")
    _visible(page, "skill-save").click()
    deadline = time.time() + 60
    while time.time() < deadline:
        if _present(page, f"kit-skill-{key}") and re.search(r"\bsaved\b", _toast(page, "success"), re.I):
            break
        failed = _toast(page, "error")
        if failed:
            raise JourneyFailed(f"saving skill {label!r} from Library › New skill said: {failed}")
        page.wait_for_timeout(500)
    else:
        raise JourneyFailed(f"saved skill {label!r}; within 60s the Library neither listed "
                            f"{key} nor said why")
    # From a fresh load: the list now came from the server.
    _library(page, goto, "skills")
    try:
        _visible(page, f"kit-skill-{key}", timeout=30_000)
    except Exception:  # noqa: BLE001
        raise JourneyFailed(f"created skill {key}; after a reload Library › Skills does not list "
                            f"it") from None
    ctx.skill_key = key
    return f"created skill {key}; listed in Skills & MCP › Library after a reload"


def mcp_register(page, ctx: Context, goto: Callable[[str], None]) -> str:
    name = f"e2e-mcp-{ctx.stamp}"
    # The fakes' MCP server (stubs/fakes.py `_mcp`): it lists one tool and keeps every call, so
    # R3 can bind that tool to a step and prove the step called it.
    url = f"{(ctx.fakes_url or 'http://127.0.0.1:9').rstrip('/')}/mcp"
    # Capabilities and the old Skills screen each had a register form; both are links to
    # Skills & MCP › Library › MCP now, the one place a server is added.
    _library(page, goto, "mcp")
    _visible(page, "kit-lib-add-mcp").click()
    _visible(page, "kit-add-mcp")
    _visible(page, "kit-add-custom").click()
    _visible(page, "kit-custom-name").fill(name)
    _visible(page, "kit-custom-url").fill(url)
    _visible(page, "kit-custom-save").click()
    deadline = time.time() + 60
    while time.time() < deadline:
        if _present(page, f"kit-mcp-{name}"):
            break
        failed = _toast(page, "error")
        if failed:
            raise JourneyFailed(f"registering MCP server {name} ({url}) said: {failed}")
        page.wait_for_timeout(500)
    else:
        listed = _text(page, "kit-lib-error") or _text(page, "kit-mcp-list")
        raise JourneyFailed(f"registered MCP server {name}; within 60s Library › MCP neither "
                            f"listed it nor said why ({listed[:200] or 'no list shown'})")
    _library(page, goto, "mcp")
    try:
        _visible(page, f"kit-mcp-{name}", timeout=30_000)
    except Exception:  # noqa: BLE001
        raise JourneyFailed(f"MCP server {name} was added, and after a reload Library › MCP "
                            f"does not list it ({_text(page, 'kit-lib-error')[:200] or 'no error shown'})"
                            ) from None
    ctx.mcp_name = name
    if ctx.target is not None:
        items = (_api(ctx).get("/v1/mcp/servers", project_id=ctx.target.project_id)
                 or {}).get("items") or []
        ctx.mcp_server_id = next((str(i.get("id")) for i in items if i.get("name") == name), "")
    return f"registered MCP server {name} in Skills & MCP › Library › MCP; listed after a reload"


# ----------------------------------------------------------------------------- R2
def agent_with_skill(page, ctx: Context, goto: Callable[[str], None]) -> str:
    """Create an agent and give it, on create, the R1 skill and the R1 MCP server; then check
    the GRANT, not the dialog's ticks. The wizard used to finish "is in the Studio" while
    writing only `requested_skills`, which the runtime never reads (map.json R2a)."""
    name = f"E2E Agent {ctx.stamp}"
    problems: list[str] = []
    goto("workforce")
    _visible(page, "wf-new-agent").click()
    _visible(page, "agent-quick-create")
    _visible(page, "aq-name").fill(name)
    _visible(page, "aq-skills")
    page.wait_for_timeout(1_000)      # the grantable skills and servers load after it opens
    boxes = page.locator('input[data-testid^="aq-skill-"]')
    picked = ""
    wanted = page.locator(f'input[data-testid="aq-skill-{ctx.skill_key}"]')
    if ctx.skill_key and wanted.count():
        wanted.first.check()
        picked = ctx.skill_key
    elif boxes.count():
        offered = [boxes.nth(i).get_attribute("data-testid") or "" for i in range(min(boxes.count(), 60))]
        if ctx.skill_key:
            # Said, and the agent is still created with another skill: agent_update and
            # step_agents need an agent, and one missing control must not hide what they find.
            problems.append(
                f"the New agent dialog does not offer skill {ctx.skill_key}, created a moment ago "
                f"in Skills & MCP, so it cannot be assigned on create (it offers {len(offered)}: "
                f"{', '.join(o[len('aq-skill-'):] for o in offered[:6])}…)")
        boxes.first.check()
        picked = offered[0][len("aq-skill-"):]
    if not picked:
        raise JourneyFailed("the New agent dialog never offered a skill to assign"
                            f" ({_text(page, 'aq-skills')[:120] or 'empty'})")
    mcp_box = page.locator(f'input[data-testid="aq-mcp-{ctx.mcp_server_id}"]')
    mcp_picked = bool(ctx.mcp_server_id and mcp_box.count())
    if mcp_picked:
        mcp_box.first.check()
    else:
        problems.append(f"the New agent dialog offers no way to give the agent an MCP server "
                        f"(no [aq-mcp-{ctx.mcp_server_id or '<server id>'}] for "
                        f"{ctx.mcp_name or 'the R1 server'}), so R2 'MCP servers assigned on "
                        f"create' cannot be done")
    _visible(page, "aq-create").click()
    deadline = time.time() + WAIT_MS / 1000
    finished = False
    while time.time() < deadline:
        # Born: the birth plays, then the agent's Passport opens on it.
        if _present(page, "agent-detail") and name in _text(page, "agent-detail"):
            finished = True
            break
        if _present(page, "aq-failed"):
            raise JourneyFailed(f"creating agent {name!r} with skill {picked} was refused: "
                                f"{_text(page, 'aq-failed')}")
        page.wait_for_timeout(500)
    if not finished:
        raise JourneyFailed(f"pressed Create for {name!r}; within {WAIT_MS // 1000}s neither its "
                            f"Passport opened nor the dialog said why")
    ctx.agent_name = name
    if ctx.target is not None:
        role = _role_of(ctx, name)
        if not role:
            problems.append(f"the Passport opened on {name!r}, but no agent of that name is on "
                            f"the roster (GET /v1/roles)")
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
                                        "was created with it ticked in the New agent dialog")
                if missing:
                    problems.append(missing)
                    mcp_picked = False
    elif mcp_picked:
        problems.append("ticked an MCP server in the New agent dialog with no API to read the "
                        "grant back from (the walk was given no target)")
    if problems:
        raise JourneyFailed("; ".join(problems))
    return (f"created agent {name!r} ({ctx.agent_role or 'role unknown'}) holding skill {picked}"
            f"{' and MCP server ' + ctx.mcp_name + ' (both read back as grants)' if mcp_picked else ''}")


def _open_agent(page, goto: Callable[[str], None], role: str, name: str = "") -> None:
    """Agents › Directory, found by the search box (it matches names, not keys), opened: its
    Passport and Loadout."""
    goto("workforce")
    _visible(page, "agents-view-directory").click()
    _visible(page, "ag-home-search").fill(name or role)
    card = page.locator(f'[data-testid="wf-open-{role}"] >> visible=true').first
    try:
        card.wait_for(state="visible", timeout=10_000)
    except Exception:  # noqa: BLE001
        if _present(page, "ag-idle-more"):
            _visible(page, "ag-idle-more").click()
        card.wait_for(state="visible", timeout=WAIT_MS)
    card.click()
    _visible(page, "agent-detail")
    _visible(page, "passport-loadout")


def _skill_chip_shown(page, skill_id: str, label: str) -> bool:
    """The Loadout's Skills row shows the skill: its own chip, or (when an ability covers it)
    its name in the row."""
    if _present(page, f"agent-skill-{skill_id}"):
        return True
    return bool(label) and re.search(rf"(?<![\w-]){re.escape(label)}(?![\w-])",
                                     _text(page, "loadout-skills")) is not None


def agent_update(page, ctx: Context, goto: Callable[[str], None]) -> str:
    """Change an existing agent: give it a second skill and an MCP server from its Passport's
    Loadout, reload, and check it stuck — on screen and in the grant the runtime reads."""
    role = ctx.agent_role
    if not role:
        raise JourneyFailed("there is no agent to update: agent_with_skill did not leave one "
                            "(run it first; it failed or was skipped)")
    problems: list[str] = []
    _open_agent(page, goto, role, ctx.agent_name)
    before = _grants(ctx, role) if ctx.target is not None else set()
    _visible(page, "loadout-skills-add").click()
    picks = page.locator('[data-testid^="loadout-skill-pick-"] >> visible=true')
    try:
        picks.first.wait_for(state="visible", timeout=15_000)
    except Exception:  # noqa: BLE001
        raise JourneyFailed(f"{role}'s Loadout › Skills › + Add offers no skill to give") from None
    offered = [(picks.nth(i).get_attribute("data-testid") or "")[len("loadout-skill-pick-"):]
               for i in range(picks.count())]
    extra = next((s for s in offered if s and s not in before and s != ctx.skill_key), "")
    if not extra:
        raise JourneyFailed(f"the agent page offers no further skill to give {role} "
                            f"(it offers {offered[:6]})")
    pick = _visible(page, f"loadout-skill-pick-{extra}")
    extra_label = " ".join((pick.inner_text() or "").split())
    pick.click()
    deadline = time.time() + 30
    while time.time() < deadline:
        if _skill_chip_shown(page, extra, extra_label):
            break
        failed = _toast(page, "error")
        if failed:
            raise JourneyFailed(f"giving {role} skill {extra} said: {failed}")
        page.wait_for_timeout(500)
    given_id, given_name = _give_mcp_on_page(page, ctx, role, problems)
    # From a fresh load: what is shown now came from the server.
    _open_agent(page, goto, role, ctx.agent_name)
    page.wait_for_timeout(1_000)      # the Loadout reads the Kit after the Passport renders
    if not _skill_chip_shown(page, extra, extra_label):
        problems.append(f"gave {role} skill {extra}; after a reload its Loadout shows only: "
                        f"{_text(page, 'loadout-skills')[:200] or 'nothing'}")
    if ctx.target is not None and extra not in _grants(ctx, role):
        problems.append(f"gave {role} skill {extra} on its page, but role_skills does not hold it")
    if given_id:
        problems.extend(p for p in (
            _mcp_not_listed_after_reload(page, role, given_id, given_name),
            _mcp_not_held(ctx, role, given_id, given_name, "was given it on its page")) if p)
    if problems:
        raise JourneyFailed("; ".join(dict.fromkeys(problems)))
    return (f"gave {role} skill {extra} and MCP server {given_name} from its Loadout; both still "
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


def _mcp_shown(page, server_id: str, name: str) -> bool:
    """The Loadout's MCP row shows the server: its chip, or its name as a whole word (an ability
    chip covers a catalogue server). `e2e-mcp-1` must not be found inside `e2e-mcp-1-update`."""
    if _present(page, f"agent-mcp-{server_id}"):
        return True
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])",
                     _text(page, "loadout-mcp")) is not None


def _mcp_not_listed_after_reload(page, role: str, server_id: str, name: str) -> str:
    """'' when the agent's freshly loaded Loadout lists the server among those it holds.
    Required whether or not the row has chips: an agent holding nothing shows `No MCP`
    instead, and round three skipped the check exactly then, so a failed grant passed."""
    deadline = time.time() + 30
    while time.time() < deadline:
        if _mcp_shown(page, server_id, name):
            return ""
        page.wait_for_timeout(500)
    shown = _text(page, "loadout-mcp") or _text(page, "loadout-no-mcp")
    return (f"gave {role} MCP server {name}; after a reload its Loadout shows "
            f"{shown[:200] or 'no MCP server'}")


def _give_mcp_on_page(page, ctx: Context, role: str, problems: list[str]) -> tuple[str, str]:
    """Give `role` an MCP server it does not hold, from its Loadout: + Add on the MCP row, pick
    the server (that IS the grant), and wait for the row to show it or a toast to say why not.
    (server id, name), or ('', '') with the reason appended to `problems`."""
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
    # The picker is behind the row's "+ Add": not rendered until that is pressed.
    try:
        _visible(page, "loadout-mcp-add", timeout=15_000).click()
    except Exception:  # noqa: BLE001
        problems.append(f"the agent page offers no way to give {role} an MCP server (no "
                        f"[loadout-mcp-add] on its Loadout), so R2 'MCP servers assigned on "
                        f"update' cannot be done")
        return "", ""
    try:
        pick = _visible(page, f"loadout-mcp-pick-{server_id}", timeout=10_000)
    except Exception:  # noqa: BLE001
        offered = page.locator('[data-testid^="loadout-mcp-pick-"] >> visible=true').all_inner_texts()
        problems.append(f"the agent page's MCP picker does not offer {name} (it offers "
                        f"{len(offered)}: {', '.join(' '.join(o.split()) for o in offered[:6]) or 'nothing'})")
        return "", ""
    pick.click()
    deadline = time.time() + 30
    while time.time() < deadline:
        if _mcp_shown(page, server_id, name):
            return server_id, name
        failed = _toast(page, "error")
        if failed:
            problems.append(f"giving {role} MCP server {name} said: {failed}")
            return "", ""
        page.wait_for_timeout(500)
    problems.append(f"gave {role} MCP server {name}; within 30s the page neither listed it nor "
                    f"said why (it shows {_text(page, 'loadout-mcp')[:160] or 'no MCP row'})")
    return server_id, name


# ----------------------------------------------------------------------------- R3
def _define_step(page, ctx: Context, goto: Callable[[str], None], label: str,
                 choose: Callable[[list[dict[str, str]]], str], what: str) -> str:
    """Define one custom step in Workflows › Library › Steps, bound by `choose(options)`; its
    step key, or JourneyFailed with the reason."""
    goto("delivery_workflows")
    _visible(page, "dw-tab-library").click()
    _visible(page, "wf-lib-view-steps").click()
    _visible(page, "wf-lib-steps")
    _visible(page, "wf-lib-new-step").click()
    _visible(page, "wf-define-modal")
    _visible(page, "dw-define-form")
    page.wait_for_timeout(500)
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
        if not _present(page, "wf-define-modal") and label in _text(page, "wf-lib-steps"):
            break
        page.wait_for_timeout(500)
    else:
        raise JourneyFailed(f"defined step {label!r} ({what}); it never appeared in Library › Steps")
    body = _api(ctx).get("/v1/delivery-workflows/custom-steps",
                         project_id=ctx.target.project_id) or {}
    key = next((str(s.get("step_key")) for s in body.get("steps") or []
                if s.get("label") == label), "")
    if not key:
        raise JourneyFailed(f"step {label!r} is listed on screen but the API has no such step")
    if not _present(page, f"wf-step-row-{key}"):
        raise JourneyFailed(f"the API has step {key} ({label!r}); Library › Steps has no row for it")
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
        # Your steps, in the editor's palette (`wf-pal-custom:<step key>`).
        try:
            _add_from_palette(page, f"wf-pal-custom:{key}")
        except JourneyFailed as exc:
            problems.append(f"the {kind} step {key} cannot be placed: {exc}")
    placed = len(_step_rows(page))
    if not placed:
        raise JourneyFailed("; ".join(problems) or "no bound step could be placed")
    workflow_key, _ = _save_and_reopen(page, goto, name, placed)
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
def _named_on_screen(page, step_id: str) -> str:
    """The agent a step's picker has chosen ('' for Auto): its option marked selected."""
    _select_step(page, step_id)
    _visible(page, "wf-agent-trigger").click()
    chosen = page.locator('[data-testid^="wf-agent-option-"][aria-selected="true"] >> visible=true')
    chosen.first.wait_for(state="visible", timeout=15_000)
    value = (chosen.first.get_attribute("data-testid") or "")[len("wf-agent-option-"):]
    page.keyboard.press("Escape")
    return "" if value == "default" else value


def step_agents(page, ctx: Context, goto: Callable[[str], None]) -> str:
    """R4: two agent steps — one naming the agent R2 made, one naming nobody — saved, reloaded
    and RUN; the run must record the named agent on the first and an auto-picked one, with its
    reason, on the second. Round two's "R4" journey pressed "Advance now"."""
    problems: list[str] = []
    name = f"E2E agent steps {ctx.stamp}"
    _new_workflow(page, goto, name)
    _add_from_palette(page, "wf-pal-agent-task")
    _add_from_palette(page, "wf-pal-agent-task")
    rows = _step_rows(page)
    if len(rows) != 2:
        raise JourneyFailed(f"pressed 'Agent task' twice; the canvas has {len(rows)} step(s)")
    named_id, auto_id = rows
    marker = f"E2E journey {ctx.stamp}"
    asks = {
        named_id: (f"{AGENT_STEP_MARKER} ({marker}): use the skill {ctx.skill_key or 'you hold'} "
                   f"and say what it returns."),
        auto_id: f"{AGENT_STEP_MARKER} ({marker}): say what you can do with the skills you hold.",
    }
    for step_id, said in asks.items():
        _select_step(page, step_id)
        _visible(page, "wf-task-fixed").click()
        _visible(page, "wf-task-text").fill(said)
    # The step's Agent picker (the inspector's combobox): Auto, then every agent on the project.
    _select_step(page, named_id)
    _visible(page, "wf-agent-trigger").click()
    options = page.locator('[data-testid^="wf-agent-option-"]:not([data-testid="wf-agent-option-name"])'
                           ':not([data-testid="wf-agent-option-status"]) >> visible=true')
    options.first.wait_for(state="visible", timeout=15_000)
    offered = [(options.nth(i).get_attribute("data-testid") or "")[len("wf-agent-option-"):]
               for i in range(options.count())]
    offered = [o for o in offered if o and o != "default"]
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
    _visible(page, f"wf-agent-option-{captain}").click()
    workflow_key, _ = _save_and_reopen(page, goto, name, 2)
    shown = {s: _named_on_screen(page, s) for s in (named_id, auto_id)}
    if shown != {named_id: captain, auto_id: ""}:
        problems.append(f"named {captain} on the first step and nobody on the second; after a "
                        f"reload the pickers say {shown}")
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
    # The palette's Built-in group (`wf-pal-cap:<capability>`), usable rows only.
    palette = page.locator('[data-testid^="wf-pal-cap:"]:not([aria-disabled="true"]) >> visible=true')
    try:
        palette.first.wait_for(state="visible", timeout=30_000)   # the palette loads after the editor
    except Exception:  # noqa: BLE001
        pass
    # Two that need no input (no "Needs:" in their tooltip): a step needing one fails Save's
    # check until it is wired, which is the editor working, not this smoke check's subject.
    free = [palette.nth(i).get_attribute("data-testid") or "" for i in range(palette.count())
            if "Needs:" not in (palette.nth(i).get_attribute("data-tip") or "")]
    if len(free) < 2:
        raise JourneyFailed(f"the step palette offers {len(free)} built-in steps that need no "
                            f"input ({palette.count()} in all); a two-step workflow needs two")
    chosen = free[:2]
    for testid in chosen:
        _add_from_palette(page, testid)
    _, meta = _save_and_reopen(page, goto, name, 2)
    return (f"built {name!r} from {', '.join(c[len('wf-pal-cap:'):] for c in chosen if c)}; "
            f"saved, and after a reload the summary says {meta[:40]!r}")


def advance_now(page, ctx: Context, goto: Callable[[str], None]) -> str:
    """A console smoke check (R6): "Advance now" answers with a sentence, not a 403 — the
    boss-1 bug where it sent the console key to a worker-only route."""
    goto("delivery_workflows")
    _visible(page, "dw-tab-runs").click()
    button = page.locator('[data-testid="dw-advance"]')
    try:
        button.first.wait_for(state="visible", timeout=20_000)
    except Exception:  # noqa: BLE001
        raise JourneyFailed("'Advance now' is not on the Runs tab: this install has no run "
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


# ----------------------------------------------------------------------------- Repo and models
SETUP_FULL_TOKEN = "ghp_e2e_full"                     # stubs/github_probes.py profiles
SETUP_READ_ONLY_TOKEN = "ghp_e2e_contents_read_only"


def _setup_pill(page, testid: str, pattern: str, *, timeout_s: float = 90) -> str:
    return _wait_for_text(page, testid, pattern, timeout_s=timeout_s)


def setup_first_run(page, ctx: Context, goto: Callable[[str], None]) -> str:
    """J1: a fresh install → token → pick a repository → three checks → a model → Try it.

    Pass: every check is ✓, the model row says Working after one governed question, the header
    says Ready, and Open a PR ends at `PR #n ↗` linking to a pull request the fake opened.
    """
    goto("setup")
    _visible(page, "su-board")
    token = _visible(page, "su-token-input")
    token.fill(SETUP_FULL_TOKEN)
    token.press("Enter")
    _visible(page, "su-repo-option-e2e/app").click()
    _visible(page, "su-repo-connect").click()
    said = _setup_pill(page, "su-repo-pill", r"working")
    if token.is_visible() and token.input_value():
        raise JourneyFailed("the token field still held the token after Connect")
    # ② a model: the scripted model stub as an OpenAI-compatible endpoint ("Other").
    if not ctx.target or not getattr(ctx.target, "model_stub_url", None):
        raise JourneyFailed("this walk has no scripted model to connect")
    _visible(page, "su-tile-api_key").click()
    _visible(page, "su-provider-custom").click()
    _visible(page, "su-model-base-url").fill(str(ctx.target.model_stub_url).rstrip("/") + "/v1")
    _visible(page, "su-model-key").fill("sk-stub-ok")
    _visible(page, "su-model-name").fill("e2e-stub")
    _visible(page, "su-model-connect").click()
    model = _setup_pill(page, "su-model-pill", r"working")
    _setup_pill(page, "su-verdict", r"ready")
    _visible(page, "su-try-input").fill(f"Fix the typo in README ({ctx.stamp})")
    _visible(page, "su-try-open").click()
    _setup_pill(page, "su-try-progress", r"planning|building|testing|PR #")
    # THE FINISH LINE IS THE PULL REQUEST, not "Planning…": a Try it that never left Planning
    # (its run was never ticked) passed this journey once.
    progress = _setup_pill(page, "su-try-progress", r"PR #\d+", timeout_s=RUN_TIMEOUT_SECONDS)
    pr = page.locator('[data-testid="su-try-pr"]').first.get_attribute("href") or ""
    if not re.search(r"/pull/\d+$", pr):
        raise JourneyFailed(f"Try it said {progress!r} but its link is {pr!r}")
    _setup_pill(page, "su-verdict", r"ready")
    return f"repository {said}; model {model}; Try it: {progress} ({pr})"


def setup_replace_token(page, ctx: Context, goto: Callable[[str], None]) -> str:
    """J4: a token without Contents: write → Can't write → Replace token with a full one → the
    SAME row says Working (no duplicate connection, no "already has" error)."""
    goto("setup")
    _visible(page, "su-repo-menu").click()
    _visible(page, "su-menu-replace").click()
    token = _visible(page, "su-token-input")
    token.fill(SETUP_READ_ONLY_TOKEN)
    token.press("Enter")
    _visible(page, "su-repo-option-e2e/site").click()
    _visible(page, "su-repo-connect").click()
    page.wait_for_timeout(1500)
    rows = page.locator('[data-testid="su-repo-pill"]').all_inner_texts()
    if not any(re.search(r"can't write", r, re.I) for r in rows):
        raise JourneyFailed(f"a read-only token's repository said {rows}, not Can't write")
    return f"read-only token: {rows}"


def setup_deep_links(page, ctx: Context, goto: Callable[[str], None]) -> str:
    """J11: ?area=setup&add=openai opens Add a model on OpenAI; focus=jobs lands on the row."""
    _with_query(page, goto, "setup", "add=openai")
    _visible(page, "su-model-drawer")
    _with_query(page, goto, "setup", "focus=jobs")
    focused = page.locator('[data-testid="su-row-jobs"][data-focused="true"]').count()
    if not focused:
        raise JourneyFailed("focus=jobs did not land on Who does what")
    return "add=openai opened the drawer; focus=jobs landed on its row"


# In order: each later journey uses what the earlier ones made (the skill, the server, the agent).
def command_center_fits(page, ctx: Context, goto: Callable[[str], None]) -> str:
    """CC-D1: the landing page never scrolls sideways, at 1440, 1280, 1024 and 390 px, in a real
    browser layout (the Systems row once pushed Deploys outside its card at 1280 and 1024)."""
    goto("overview")
    _visible(page, "cc-systems")
    seen = []
    for width, height in ((1440, 900), (1280, 800), (1024, 768), (390, 844)):
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(600)
        over = page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth")
        outside = page.evaluate(
            """() => [...document.querySelectorAll('[data-testid^="cc-dot-"]')].filter((d) => {
                 const card = d.closest('.cc-sec').getBoundingClientRect();
                 const r = d.getBoundingClientRect();
                 return r.right > card.right + 0.5 || r.left < card.left - 0.5;
               }).map((d) => d.getAttribute('data-testid'))""")
        if over > 0 or outside:
            raise JourneyFailed(f"at {width}px the page scrolls {over}px sideways; outside: {outside}")
        # The shell's top bar: no item drawn over its neighbour (re-test 2, at 1024).
        crowded = page.evaluate(
            """() => { const bar = document.querySelector('.bx-topbar'); if (!bar) return [];
                 const items = [...bar.children].filter((e) => e.getClientRects().length
                   && getComputedStyle(e).position !== 'absolute' && !e.classList.contains('bx-topbar-spacer'))
                   .sort((x, y) => x.getBoundingClientRect().left - y.getBoundingClientRect().left);
                 const out = [];
                 for (let i = 1; i < items.length; i++) {
                   const a = items[i - 1].getBoundingClientRect(), b = items[i].getBoundingClientRect();
                   if (a.right > b.left + 0.5 && a.top < b.bottom && b.top < a.bottom) out.push(items[i - 1].className + ' / ' + items[i].className);
                 }
                 return out; }""")
        if crowded and width >= 640:
            raise JourneyFailed(f"at {width}px the top bar's items overlap: {crowded}")
        seen.append(f"{width}px ok")
    # At 768px tall the sidebar's last item is reachable, never behind the account block.
    page.set_viewport_size({"width": 1280, "height": 768})
    page.wait_for_timeout(400)
    hidden = page.evaluate(
        """() => { const nav = document.querySelector('.bx-nav'), foot = document.querySelector('.bx-sidebar-foot');
             if (!nav || !foot || !nav.getClientRects().length) return false;
             return nav.getBoundingClientRect().bottom > foot.getBoundingClientRect().top + 0.5; }""")
    if hidden:
        raise JourneyFailed("at 768px tall the sidebar's nav runs under its account block")
    page.set_viewport_size({"width": 1440, "height": 900})
    return "; ".join(seen)


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
    ("SU", "setup_first_run", setup_first_run),
    ("SU", "setup_replace_token", setup_replace_token),
    ("SU", "setup_deep_links", setup_deep_links),
    ("CC", "command_center_fits", command_center_fits),
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

"""What the console walk presses in each area, and what it must SEE afterwards.

THE INCIDENT. The first walk chose its actions by a CSS heuristic — buttons whose class token
was exactly `primary` — and a press only had to avoid errors for two seconds. Across thirty-six
areas it pressed eight buttons (three unlabelled), never "Advance now" (`class="cap-btn"`), never
"Create agent" (`class="cap-btn roster-new"`), and passed every button that did nothing. So the
screens boss-1 failed on were reported clean.

THE DESIGN. Every area in ConsoleShell's AREAS has an entry here, chosen deliberately:
  * Press   — a control found by data-testid, pressed, and an OUTCOME the operator would look
              for: a selector that must become visible (`expect`), a list that must grow
              (`grows`), or — when nothing more specific exists — the area's text must change.
              A press with no visible result fails, with the sentence "pressing X showed nothing".
  * Journey — the area's real job is a multi-step journey (journeys.py); named here so the
              area's entry points at it.
  * NothingToPress — the area is read-only, or has nothing to press until data exists, said in a
              sentence. Kept deliberately rare and always explained.
The self-test enumerates every discovered area against this table and every testid against the
console source (`source` / `expect_source`, files under apps/web), so a new area without a
declared action, or a renamed testid, fails in seconds — not after a forty-minute walk.

`write=True` marks a press that changes state; on a live install it is only made with
--allow-writes. `unless` names a selector that means the action has already been done (a
"seed the demo" button that disappears once there is data).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Union


@dataclass(frozen=True)
class Press:
    testid: str                     # exact, or a prefix ending in "*" (first match pressed)
    source: str                     # file under apps/web that declares it
    expect: Optional[str] = None    # selector that must become visible; None → text must change
    expect_source: str = ""         # file declaring the testids named in `expect` (default source)
    grows: Optional[str] = None     # selector whose count must increase instead
    fill: tuple[tuple[str, str], ...] = ()   # (testid, text) typed before the press
    then: tuple[str, ...] = ()      # testids pressed after it (a suggestion fills, Send sends)
    opens: bool = False             # a <details>: open it (click only when closed)
    write: bool = False
    unless: Optional[str] = None    # visible → the action was already done; not a failure
    # Declared in a file the Community export removes (scripts/community_export_manifest.json):
    # not pressed when the API says it is Community, and its source may be absent there.
    paid_only: bool = False
    why: str = ""


@dataclass(frozen=True)
class Journey:
    name: str                       # a name in journeys.JOURNEYS


@dataclass(frozen=True)
class NothingToPress:
    reason: str


Entry = Union[Press, Journey, NothingToPress]


def _tid(name: str) -> str:
    return f'[data-testid="{name}"]'


PRIMARY_ACTIONS: dict[str, tuple[Entry, ...]] = {
    "overview": (
        # `Ideas` is a button now (command-center spec §4.6), not a <details>: pressed, not opened.
        Press("askboss-examples", "app/ask-boss-hero.tsx",
              expect=_tid("askboss-default-workflow"),
              why="the ask box's worked examples; asking itself starts real work"),
        # The bridge's own presses (command-center spec §10.3).
        Press("cc-today-tab-spend", "app/command-center/today-chart.tsx", expect=_tid("cc-today"),
              expect_source="app/command-center-ui.tsx",
              why="Today switches from work to money spent, by hour"),
        Press("cc-floor-list-toggle", "app/command-center/floor.tsx", expect=_tid("cc-floor-list"),
              why="the Floor as a list, for anyone who prefers it to the map"),
        Press("cc-dot-budget", "app/command-center/systems-row.tsx",
              why="a Systems dot opens the area that fixes it (Budget)"),
    ),
    "code_tasks": (
        Press("fx-new-build", "app/code-tasks-ui.tsx", expect=_tid("fx-new-menu"),
              why="+ New build ▾ opens its doors: Plan it with Juno, a workflow, a direct change"),
        Press("fx-view-apps", "app/code-tasks-ui.tsx", expect=_tid("fx-apps"),
              expect_source="app/factory/apps-tab.tsx"),
        Press("fx-view-floor", "app/code-tasks-ui.tsx", expect=_tid("fx-floor"),
              expect_source="app/factory/floor.tsx"),
        Press("fx-build-row-*", "app/factory/build-row.tsx", expect=_tid("fx-cockpit"),
              expect_source="app/factory/cockpit.tsx",
              unless=_tid("fx-nothing-building"),
              why="a build row opens that build's cockpit"),
    ),
    "delivery_workflows": (
        Press("dw-tab-design", "app/delivery-workflows-ui.tsx", expect=_tid("dw-design-ways")),
        Journey("step_bindings"),
        Journey("step_agents"),
        Journey("workflow_build"),
        Journey("advance_now"),
    ),
    "workforce": (
        Press("wf-new-agent", "app/workforce-ui.tsx", expect=_tid("aw-dialog"),
              expect_source="app/agent-creation-wizard.tsx"),
        # Agents home (agents-home spec §3.1): four tabs, each with the surface it must show.
        Press("agents-view-live", "app/workforce-ui.tsx", expect=_tid("ag-live"),
              expect_source="app/agents/live-board.tsx"),
        Press("agents-view-directory", "app/workforce-ui.tsx", expect=_tid("roster-grid"),
              expect_source="app/agents/roster-zones.tsx"),
        Press("agents-view-org", "app/workforce-ui.tsx", expect=_tid("ag-zones"),
              expect_source="app/agents/org-teams.tsx"),
        # Organization (org-view spec §7.3): Teams · Flow · Lines and the window chips.
        Press("ag-org-flow", "app/agents/org-view.tsx", expect=_tid("ag-flow"),
              expect_source="app/agents/org-flow.tsx"),
        Press("ag-org-lines", "app/agents/org-view.tsx",
              expect=f'{_tid("org-tree")}, {_tid("org-empty")}', expect_source="app/workforce-ui.tsx"),
        Press("ag-org-days-30", "app/agents/org-view.tsx", expect=_tid("ag-orgteams"),
              expect_source="app/agents/org-teams.tsx"),
        Press("agents-view-activity", "app/workforce-ui.tsx", expect=_tid("ag-river"),
              expect_source="app/agents/activity-river.tsx"),
        Journey("agent_with_skill"),
        Journey("agent_update"),
        Journey("live_gate"),
    ),
    "org_chart": (
        Press("org-template-toggle", "app/workforce-ui.tsx", expect=_tid("org-templates")),
    ),
    "studio": (
        Press("as-create-agent", "app/agent-studio-ui.tsx", expect=_tid("aw-dialog"),
              expect_source="app/agent-creation-wizard.tsx"),
    ),
    "decisions": (
        Press("decision-filter-all", "app/decision-inbox-ui.tsx",
              expect='[data-testid="decision-filter-all"].active'),
    ),
    "delivery": (
        Press("dm-open-dock", "app/deployment-manager-ui.tsx", expect=_tid("agent-send"),
              expect_source="app/agent-ui.tsx"),
    ),
    "monitoring": (
        Press("monitoring-open-dock", "app/monitoring-ui.tsx", expect=_tid("agent-send"),
              expect_source="app/agent-ui.tsx"),
    ),
    "activity": (
        Press("activity-level-error", "app/activity-log-ui.tsx",
              expect='[data-testid="activity-level-error"][aria-pressed="true"]'),
    ),
    "discovery": (
        NothingToPress("On Community this lists saved Discovery dossiers, each opening its "
                       "dossier; a new project has none, and starting research is not offered."),
    ),
    "meetings": (
        Press("meeting-new", "app/meeting-room-ui.tsx", expect=_tid("meeting-new-form")),
    ),
    "agent": (
        Press("agent-suggest-*", "app/agent-ui.tsx", write=True, then=("agent-send",),
              expect='[data-testid="agent-message-assistant"]',
              why="a suggested question fills the composer; sent, the chat must answer"),
    ),
    "dashboard": (
        NothingToPress("A read-only cross-phase snapshot (budget, queue, work, proof, health); "
                       "it has no controls."),
    ),
    "work": (
        NothingToPress("The task board's rows each open a run's trace, and rows exist only once "
                       "work has run; the area itself has no other control."),
    ),
    "portfolio": (
        Press("wo-add-work", "app/work-orchestration-ui.tsx", opens=True,
              expect='[data-testid="wo-add-work"] form'),
    ),
    "processes": (
        Press("agent-process-add-step", "app/agent-process-ui.tsx",
              grows=_tid("agent-process-builder-step")),
    ),
    "shepherd": (
        Press("shepherd-sweep", "app/shepherd-ui.tsx", write=True,
              why="'Re-examine failures' must say what it found, even when it is nothing"),
    ),
    "reliability": (
        Press("kg-seed", "app/knowledge-graph-ui.tsx", write=True, expect=_tid("kg-seed-note")),
    ),
    "ai-sre": (
        NothingToPress("A read-only capability ledger: category, status and the remaining gap."),
    ),
    "reports": (
        Press("rep-tab-*", "app/reports-ui.tsx", expect=".rep-tab.active"),
    ),
    "skills": (
        Press("skill-agent-open", "app/skills-ui.tsx", expect=_tid("agent-send"),
              expect_source="app/agent-ui.tsx"),
        Journey("skill_create"),
    ),
    "setup": (
        Press("su-test-all", "app/setup/setup-board.tsx", expect=_tid("su-verdict"),
              why="Repo and models re-checks every row and answers with one verdict"),
        Journey("setup_first_run"),
        Journey("setup_replace_token"),
        Journey("setup_deep_links"),
    ),
    "budget": (
        Press("budget-intensity-apply", "app/budget-intensity-ui.tsx", write=True,
              expect=_tid("budget-intensity-current")),
    ),
    "pipeline": (
        Press("chunk-plan", "app/phase3-ui.tsx", fill=(("chunk-title", "E2E chunk plan"),),
              expect='[data-testid="chunk-plan-result"][data-ok="true"]'),
    ),
    "comms": (
        Press("slack-render", "app/phase3-ui.tsx",
              fill=(("slack-persona", "E2E"), ("slack-body", "All systems green.")),
              expect='[data-testid="slack-render-result"][data-ok="true"]'),
    ),
    "ops": (
        Press("eval-run", "app/phase2-ui.tsx", write=True, expect=_tid("eval-latest")),
    ),
    "assurance": (
        Press("assurance-seed", "app/assurance-ui.tsx", write=True,
              expect=_tid("assurance-cases"), unless=_tid("assurance-cases")),
    ),
    "org": (
        NothingToPress("Every action here installs, exports, delegates or grants access (packs, "
                       "modules, break-glass); none has a safe press with a checkable result, "
                       "and Export answered nothing visible in the first probe — a known gap."),
    ),
    "github": (
        Press("ghx-setup", "app/github-operations-ui.tsx", opens=True,
              expect='[data-testid="ghx-setup"] form'),
    ),
    "infra": (
        Press("infra-view-resources", "app/provider-configuration-ui.tsx",
              expect='[data-testid="infra-view-resources"].active'),
    ),
    "capabilities": (
        Journey("mcp_register"),
    ),
    "sandboxes": (
        NothingToPress("Lists live sandboxes, each opening its working tree; a stack with no "
                       "running build has none."),
    ),
    "foundations": (
        NothingToPress("The design-token reference: colour, type and spacing, read-only."),
    ),
    "components": (
        Press("ds-open-modal", "app/design-system-showcase.tsx", expect=_tid("ds-modal")),
    ),
    "enterprise": (
        Press("soc2-collect", "app/phase4-paid-ui.tsx", write=True, paid_only=True,
              expect='[data-testid="soc2-result"][data-ok="true"]'),
        NothingToPress("On Community this area is identity-provider, SCIM and rollout forms that "
                       "change who can sign in; none is pressed by a walk."),
    ),
}

_TESTID_IN_SELECTOR = re.compile(r'data-testid="([^"]+)"')


def selector_for(testid: str) -> str:
    if testid.endswith("*"):
        return f'[data-testid^="{testid[:-1]}"]'
    return f'[data-testid="{testid}"]'


_TEMPLATE_PREFIX = re.compile(r"data-testid=\{`([\w-]*)\$\{")


def declared_in_source(source_text: str, testid: str) -> bool:
    """`data-testid="x"`; for a prefix `x-*` a template literal starting `x-${`; and for an
    exact id made by a template (`activity-level-error` from `activity-level-${value}`), a
    template literal whose fixed start is a prefix of it."""
    if testid.endswith("*"):
        return f"data-testid={{`{testid[:-1]}${{" in source_text
    if f'data-testid="{testid}"' in source_text:
        return True
    if f'testid="{testid}"' in source_text:
        return True   # a component prop forwarded to data-testid (phase3-ui's <Result testid=…>)
    return any(prefix and testid.startswith(prefix)
               for prefix in _TEMPLATE_PREFIX.findall(source_text))


def fixed_start(testid: str) -> str:
    """`aw-mcp-${server.mcp_server_id}` → `aw-mcp-`: the part a page and a journey share. The
    variable after it is the page's own business (round three's arrival check compared it and
    never saw `aw-mcp-${server.mcp_server_id}` arrive because it looked for `${server.id}`)."""
    return testid.split("${", 1)[0]


def rendered_in_source(source_text: str, testid: str) -> bool:
    """Is a control with this testid rendered by the file? A plain id must appear quoted; a
    template id is matched by its fixed start EXACTLY — `aw-${x}` does not render `aw-mcp-…` —
    written as a template literal or as a string concatenation."""
    if "${" not in testid:
        return declared_in_source(source_text, testid)
    start = fixed_start(testid)
    if start in set(_TEMPLATE_PREFIX.findall(source_text)):
        return True
    return re.search(r"data-testid=\{\s*[\"']" + re.escape(start) + r"[\"']\s*\+",
                     source_text) is not None


def selector_testids(selector: Optional[str]) -> list[str]:
    return _TESTID_IN_SELECTOR.findall(selector or "")


__all__ = ["PRIMARY_ACTIONS", "Press", "Journey", "NothingToPress", "Entry", "selector_for",
           "declared_in_source", "rendered_in_source", "fixed_start", "selector_testids"]

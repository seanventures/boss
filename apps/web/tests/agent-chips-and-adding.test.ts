/**
 * Three reports, one page: how agents are drawn, and what "add" means.
 *
 * DRAWN. The lists rendered as bare text with a tiny uppercase source word after each name —
 * "Adversarial Reviewer BY DEFAULT  Chief Architect BY DEFAULT  QA Lead BY DEFAULT" — a run-on
 * line where the label competed with the name and nothing separated one agent from the next.
 * The meeting room has drawn a panel correctly all along: a coloured disc with initials, the
 * name beside it, one pill per agent. Reused rather than reinvented.
 *
 * ADDED. "when I try to add more agents all other agents are getting cleared." They were not
 * being cleared. An app with nothing recorded SHOWS the effective team — because no row is not
 * no one — and writing the first real row made the fallback stop applying, so five became one.
 * The first write now materialises what the app was already running.
 *
 * FOUND. The wizard's picker is on step 2 and was reported missing twice. A thing you have to
 * click through to is a thing nobody finds, so it is on the Review screen too.
 */
import { describe, expect, it } from "vitest";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { AgentChip, agentMeta } from "../app/agent-chip";
import { avatarSvg, specForRole } from "../lib/agent-avatar";
import { codeOnly } from "./_helpers/source";

const CHIP = codeOnly("app/agent-chip.tsx");
const PANEL = codeOnly("app/agents-on-this.tsx");
const PICKER = codeOnly("app/default-agent-picker.tsx");
// The Runs composer (the "wizard") lives in its own module since Workflows became Runs + Library.
const WIZARD = codeOnly("app/workflows/new-project-composer.tsx");
const CSS = codeOnly("app/globals.css");

describe("an agent has a face", () => {
  it("initials mean something rather than being the first two letters", () => {
    expect(agentMeta("release_manager").initials).toBe("RM");
    expect(agentMeta("software_engineer").initials).toBe("EN");
    expect(agentMeta("qa").initials).toBe("QA");
  });

  it("an unknown agent still gets word initials, not a truncation", () => {
    expect(agentMeta("shift_swap_auditor").initials).toBe("SS");
  });

  it("and its colour is stable, not random", () => {
    // The same agent changing colour between screens is worse than no colour at all.
    expect(agentMeta("shift_swap_auditor").tone).toBe(agentMeta("shift_swap_auditor").tone);
  });

  it("panel seat names resolve too, so one agent is one colour everywhere", () => {
    // The meeting room keys off display names ("UX Designer"); the workforce off role keys
    // (`ux_engineer`). Both arrive here.
    expect(agentMeta("", "UX Designer").initials).toBe("UX");
    expect(agentMeta("ux_engineer").initials).toBe("UX");
  });

  it("it reuses the meeting room's avatar, rather than a second one that can drift", () => {
    expect(CHIP).toContain("mr-avatar tone-");
    expect(CSS).toContain(".agent-chip .mr-avatar");
  });

  it("and every list that shows agents uses it", () => {
    for (const [name, src] of [["panel", PANEL], ["command centre", PICKER],
                               ["wizard", WIZARD]] as const) {
      expect(src, name).toContain("<AgentChip");
    }
  });

  it("the face is the agent's kit character, inside the same wrapper every layout already sizes", () => {
    const html = renderToStaticMarkup(createElement(AgentChip, { roleKey: "release_manager", note: "by default" }));
    expect(html).toContain("mr-avatar tone-green ag-face");
    expect(html).toContain(avatarSvg(specForRole("release_manager"), { size: 24 }));
    expect(html).toContain(">Release Manager<"); // the display name, not the raw key
    expect(html).not.toContain(">RM<"); // no initials disc any more
  });

  it("a chip given a state shows it as a face and a word", () => {
    const html = renderToStaticMarkup(createElement(AgentChip, { roleKey: "qa", state: "needs_you" }));
    expect(html).toContain('data-state="needs_you"');
    expect(html).toContain('class="av-badge"');
    expect(html).toContain('class="ag-state-word" data-state="needs_you">Needs you<');
    expect(renderToStaticMarkup(createElement(AgentChip, { roleKey: "qa" }))).not.toContain("ag-state-word");
  });

  it("two agents whose initials used to collide are told apart", () => {
    // product_owner and providerops_manager were both "PO"; their faces were the same disc.
    expect(agentMeta("providerops_manager").initials).not.toBe(agentMeta("product_owner").initials);
    expect(avatarSvg(specForRole("providerops_manager"), { size: 24 })).not.toBe(avatarSvg(specForRole("product_owner"), { size: 24 }));
  });

  it("the source label is secondary to the name, not shouting beside it", () => {
    const at = CSS.indexOf(".agent-chip-note");
    expect(at).toBeGreaterThan(-1);
    expect(CSS.slice(at, at + 160)).toContain("var(--muted)");
  });
});

describe("adding an agent adds one", () => {
  it("the first write materialises what the app was already running", () => {
    // Not a UI workaround: the server does it, so every caller gets add-semantics rather than
    // each one remembering to send the whole list.
    const ASSIGN = codeOnly("../../engine/boss_engine/services/agent_assignment.py");
    // `establishes_team` excuses the SEEDER, which is stating a run's team rather than adding
    // to one — it was materialising a team the workflow never asked for and putting the real
    // agents on top. Every other caller still gets add-semantics.
    expect(ASSIGN).toContain(
      "if not establishes_team and not assigned(connection, project_id, key):");
    expect(ASSIGN).toContain("ON CONFLICT (project_id, app_key, role_key) DO NOTHING");
  });

  it("including the project's own team, which had the same problem", () => {
    const ASSIGN = codeOnly("../../engine/boss_engine/services/agent_assignment.py");
    expect(ASSIGN).toContain("default_roles() if key == PROJECT_DEFAULT");
  });
});

describe("the wizard's agents are where you confirm things", () => {
  it("they are on the plan, next to the steps they will do", () => {
    // One screen now: the Team fact shows the faces; Edit opens the picker in place.
    expect(WIZARD).toContain('data-testid="np-team"');
    expect(WIZARD).toContain('data-testid="np-team-edit"');
    expect(WIZARD).toContain("{editTeam ? agentPicker : null}");
  });

  it("and are editable there, not merely listed", () => {
    // Add, remove, and "Put it back" — the same picker the wizard had.
    const picker = WIZARD.slice(WIZARD.indexOf("const agentPicker = "));
    expect(picker.slice(0, 1600)).toContain('set("agents"');
  });
});

import { describe, expect, it } from "vitest";

import { areaVisibilityCss } from "../lib/area-visibility";
import { AREA_IDS } from "../lib/area-ids";
import { codeOnly } from "./_helpers/source";

/**
 * Skills as objects an operator authors, not ids only BOSS can implement.
 *
 * Behaviour used to live in a Python executor inside the product, so every capability a
 * customer wanted was a pull request. The thing most worth customising was the thing BOSS knew
 * least about: their tools, their accounts and their house rules.
 */
const UI = codeOnly("app/skills-ui.tsx");
const ROUTE = codeOnly("app/api/skills/route.ts");
const SHELL = codeOnly("app/ConsoleShell.tsx");
const PAGE = codeOnly("app/page.tsx");
const CSS = codeOnly("app/globals.css");
const EDITION = codeOnly("lib/edition.ts");

describe("the area exists in all the places an area has to exist", () => {
  it("the union, the nav, the page, the CSS rule and the edition", () => {
    // Adding an area needs five things and the invisible one is the CSS. A test has caught
    // that twice now.
    expect(AREA_IDS).toContain("skills"); // the id list moved to lib/area-ids.ts
    // The label names both things the area holds: skills you author, and the MCP servers
    // they reach. Asserted on the ID and the label separately, so a copy change does not
    // read as the area having vanished.
    expect(SHELL).toContain('{ id: "skills", label: "Skills and MCP"');
    expect(PAGE).toContain('data-area="skills"');
    // The visibility rule is GENERATED from AREAS now (lib/area-visibility.ts) — the hand list
    // in globals.css drifted and left AI SRE blank — so the check is on the generated rule.
    expect(areaVisibilityCss(["skills"])).toContain('.bx-content[data-active="skills"]');
    expect(SHELL).toContain("areaVisibilityCss(AREAS.map((a) => a.id))");
    expect(CSS).not.toContain('.bx-content[data-active="skills"] .frame > section:is');
    expect(EDITION).toContain('"skills"');
  });
});

describe("the form offers the three behaviours and nothing else", () => {
  it("a command, an MCP tool, or a built-in", () => {
    expect(UI).toContain('id: "sandboxed_command"');
    expect(UI).toContain('id: "mcp_tool"');
    expect(UI).toContain('id: "builtin"');
  });

  it("and says what each is for, because the choice is not obvious", () => {
    expect(UI).toContain("ffmpeg, pandoc, ImageMagick");
    expect(UI).toContain("the MCP server holds the integration");
  });
});

describe("the dangerous choices are explained before they are made", () => {
  it("the image must be pinned by digest, and the field says so", () => {
    expect(UI).toContain("Image (pinned by digest)");
    expect(UI).toContain("ghcr.io/acme/ffmpeg@sha256:");
  });

  it("the quoting guarantee is stated where the command is typed", () => {
    // Not after a save fails: this is structural and not obvious from a command box.
    expect(UI).toContain("shell-quoted");
    expect(UI).toContain("cannot become a second command");
  });

  it("network is off by default and the option says what it means", () => {
    expect(UI).toContain("Off — the command cannot reach anything");
    const at = UI.indexOf('data-testid="skill-network"');
    expect(UI.slice(at - 200, at)).toContain('draft.network ? "yes" : "no"');
  });

  it("and a high tier explains its consequence rather than printing a number", () => {
    expect(UI).toContain('data-testid="skill-risk-note"');
    expect(UI).toContain("cannot be set to auto");
    expect(UI).toContain("marked reviewed before it can be placed");
  });
});

describe("knowledge is treated as the part worth the most time", () => {
  it("it gets the biggest box and says what belongs in it", () => {
    expect(UI).toContain('data-testid="skill-knowledge"');
    const at = UI.indexOf('data-testid="skill-knowledge"');
    expect(UI.slice(at - 200, at)).toContain("rows={8}");
    expect(UI).toContain("hundredth run");
  });
});

describe("the list says what a skill costs you, not what tier it is", () => {
  it("needs a person, and unreviewed", () => {
    expect(UI).toContain("needs a person");
    expect(UI).toContain("unreviewed");
  });
});

describe("the route cannot be used to reach any endpoint", () => {
  it("delete arrives as a named action, not a path taken from the client", () => {
    // The ACTION is named in the body and matched against a fixed set; the path is never
    // taken from the client. Four actions now (save, delete, assign, unassign) and the check
    // is that each is a literal comparison, not a value interpolated into a URL.
    expect(ROUTE).toContain('const action = String(body.action ?? "")');
    expect(ROUTE).toContain('const remove = action === "delete"');
    expect(ROUTE).toContain('action === "assign" || action === "unassign"');
    // And it reaches the DEFINITION endpoints, not the runtime skill catalogue that has owned
    // /v1/skills all along — a second GET on that path is never matched, so it would have
    // answered a different question than the one asked.
    expect(ROUTE).toContain("/v1/skill-definitions");
  });
});

describe("what BOSS already ships is shown beside what you wrote", () => {
  it("the built-in catalogue is fetched and listed", () => {
    // "You have no skills" is false on a fresh project: BOSS ships seventeen, and several are
    // the ones somebody would otherwise author from scratch.
    expect(UI).toContain('data-testid="skills-builtin"');
    expect(UI).toContain('data-testid="builtin-list"');
    expect(UI).toContain("view=builtin");
  });

  it("they are read-only, because they have code behind them", () => {
    const at = UI.indexOf('data-testid="builtin-list"');
    const block = UI.slice(at, at + 900);
    expect(block).not.toContain("skill-delete");
    expect(UI).toContain("Author a new skill above only when nothing here covers the job.");
  });

  it("and the route serves both catalogues without confusing them", () => {
    expect(ROUTE).toContain('const view = url.searchParams.get("view")');
    expect(ROUTE).toContain('view === "builtin"');
    expect(ROUTE).toContain('view === "assignments"');
    expect(ROUTE).toContain("/v1/skills?project_id=");
    expect(ROUTE).toContain("/v1/skill-definitions?project_id=");
  });
});


/**
 * A skill goes to an agent, to a workflow step, or to both.
 *
 * The step end already existed — `workflow_custom_steps.skill_id`. The agent end did not, so
 * the seventeen skills that ship with BOSS could be listed and admired and never given to
 * anybody. Reported as: "I think we need to assign those 17 skills to the Agents out there."
 *
 * The two ends are INDEPENDENT on purpose. A workflow step is "this happens at this point in
 * the path"; an agent holding a skill is "this member of the workforce can do this when its
 * own work calls for it" — which is what an admin-style agent owning a YouTube account needs.
 * Forcing a choice between them would make one of those impossible.
 */
describe("a skill can be given to an agent", () => {
  it("both catalogues offer it, authored and built-in alike", () => {
    // The built-in list existed to be read. Seventeen skills with executors behind them and no
    // way to hand one to an agent is a catalogue, not a capability.
    const custom = UI.slice(UI.indexOf('data-testid="skills-custom"'),
                            UI.indexOf('data-testid="skills-builtin"'));
    const builtin = UI.slice(UI.indexOf('data-testid="skills-builtin"'),
                             UI.indexOf('data-testid="skill-form"'));
    expect(custom).toContain("<AssignRow");
    expect(builtin).toContain("<AssignRow");
  });

  it("only authored skills offer the workflow end", () => {
    // A built-in has an executor and a standard capability already covers it in a workflow;
    // offering "use in a workflow" there would route an operator to build a custom step that
    // duplicates one.
    const custom = UI.slice(UI.indexOf('data-testid="skills-custom"'),
                            UI.indexOf('data-testid="skills-builtin"'));
    const builtin = UI.slice(UI.indexOf('data-testid="skills-builtin"'),
                             UI.indexOf('data-testid="skill-form"'));
    expect(custom).toContain("canPlaceInWorkflow\n");
    expect(builtin).toContain("canPlaceInWorkflow={false}");
  });

  it("placing a step is handed to the workflow agent, not half-built here", () => {
    // Placing a step means deciding where in a path it sits and what feeds it. That is a
    // conversation, and there is already an agent whose remit it is.
    expect(UI).toContain("function askBossToPlaceSkill");
    const fn = UI.slice(UI.indexOf("function askBossToPlaceSkill"));
    expect(fn.slice(0, fn.indexOf("\n}"))).toContain('role: "Program Manager"');
  });

  it("assignment is its own action on the route, gated like authoring", () => {
    expect(ROUTE).toContain("/v1/role-skills");
    expect(ROUTE).toContain('body.role_key');
    expect(ROUTE).toContain('body.skill_id');
  });

  it("the page reads every assignment once, not once per row", () => {
    // This page lists twenty-odd skills. A request per row is how a list becomes a stampede.
    expect(UI).toContain('view=assignments');
    const loader = UI.slice(UI.indexOf("const reloadAssignments"));
    expect(loader.slice(0, 400)).toContain("view=assignments");
  });
});


/**
 * A skill whose callers are all machinery is not offered to an agent.
 *
 * Seven of the seventeen shipped skills name only internal subsystems in their
 * `allowed_callers` — `worker`, `software_factory`, `router`, `control_plane`,
 * `resource_plane_kernel`. Those are not members of the workforce, and handing such a skill to
 * an agent buys nothing: the thing that calls it is not an agent and already has permission.
 *
 * Offering them anyway invites a pairing that is pointless at best, on a page whose whole job
 * is to make the useful pairings obvious.
 */
describe("the page does not invite a pointless assignment", () => {
  it("a skill with no agent among its callers says so instead of offering a picker", () => {
    expect(UI).toContain("function declaredAgentCallers");
    expect(UI).toContain("skill-internal-");
    // The condition is "no declared agent caller AND nobody holds it" — an operator who has
    // already assigned one deliberately keeps seeing the control that lets them undo it.
    expect(UI).toContain("declaredAgentCallers(s, roles).length === 0");
    expect(UI).toContain("holdersOf(s.id).length === 0");
  });

  it("a wildcard caller means every agent, not none", () => {
    // `allowed_callers: ["*"]` is the most permissive spec there is. Reading it as "no declared
    // callers" would hide the picker on precisely the skills anyone may call.
    const fn = UI.slice(UI.indexOf("function declaredAgentCallers"));
    expect(fn.slice(0, fn.indexOf("\n}"))).toContain('callers.includes("*")');
  });

  it("the picker puts the skill's own callers first", () => {
    // Two groups because they mean different things: an agent the spec names can call it
    // today, any other works because the assignment grants it — a decision worth making
    // deliberately rather than by picking the top of an alphabetical list.
    expect(UI).toContain("<optgroup label=\"Already named by this skill\">");
    expect(UI).toContain("const suggested = free.filter((r) => declared.includes(r));");
    expect(UI).toContain("const rest = free.filter((r) => !declared.includes(r));");
  });

  it("a chip says whether the assignment was the skill's idea or the operator's", () => {
    // An assignment that widened permission should not look identical to one the spec already
    // allowed — the second is a decision somebody made, and its tooltip says what it means.
    expect(UI).toContain("declared.includes(r) ? \"tone-muted\" : \"tone-amber\"");
    expect(UI).toContain("Approvals and budget still apply");
  });
});

import { describe, expect, it } from "vitest";

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
    expect(SHELL).toContain('| "skills"');
    expect(SHELL).toContain('{ id: "skills", label: "Skills"');
    expect(PAGE).toContain('data-area="skills"');
    expect(CSS).toContain('.bx-content[data-active="skills"]');
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
    expect(ROUTE).toContain('const remove = body.action === "delete"');
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
    expect(UI).toContain("Author a new one");
  });

  it("and the route serves both catalogues without confusing them", () => {
    expect(ROUTE).toContain('url.searchParams.get("view") === "builtin"');
    expect(ROUTE).toContain("/v1/skills?project_id=");
    expect(ROUTE).toContain("/v1/skill-definitions?project_id=");
  });
});

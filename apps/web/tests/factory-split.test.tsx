import { describe, expect, it } from "vitest";

import { codeOnly, cssOnly } from "./_helpers/source";

/**
 * The Factory app list is master-detail, not an accordion.
 *
 * Expanding an app in place pushed every app below it down the page, so opening the third one
 * moved the second under the cursor — the list lost its shape exactly when it was being
 * navigated. One app is selected; its runs render beside the list.
 */
const UI = codeOnly("app/code-tasks-ui.tsx");
const CSS = cssOnly("app/globals.css");

describe("factory master-detail", () => {
  it("no longer expands apps in place", () => {
    // The accordion state is gone, not merely unused.
    expect(UI).not.toContain("expandedGroups");
  });

  it("the Factory page stays a plain list, with no split in front of it", () => {
    // The split belongs INSIDE an app. Putting it on the Factory page added a navigation layer
    // in front of the list rather than behind it — you had to pick an app to see apps.
    const factory = UI.slice(UI.indexOf("const items = order.map"));
    const listEnd = factory.indexOf("return items;");
    expect(listEnd).toBeGreaterThan(-1);
    expect(factory.slice(0, listEnd)).not.toContain("ct-split-detail");
  });

  it("clicking an app opens it rather than expanding it", () => {
    // Anchored on CODE, not on a comment: codeOnly() strips comments, so a test that slices
    // from a comment slices from nothing and passes or fails for the wrong reason.
    expect(UI).toContain("setSelected(pickBuiltRun(members)?.id");
  });

  it("inside an app, the runs are the left panel and the detail is on the right", () => {
    const inside = UI.slice(UI.indexOf("ct-run-list"));
    expect(inside).toBeTruthy();
    expect(inside.indexOf("ct-run-item-")).toBeLessThan(inside.indexOf("ct-split-detail"));
    expect(inside).toContain("<CodeTaskDetail run={active}");
  });

  it("a run row says what the run WAS, not just that it existed", () => {
    // A column of identical status chips tells you nothing about which attempt to open.
    expect(UI).toContain("ct-run-item-title");
    expect(UI).toContain("attemptKind(m)");
  });

  it("the list docks away", () => {
    expect(UI).toContain("ct-dock-toggle");
    expect(CSS).toContain('.ct-split[data-docked="true"]');
    // Docking must hide the items but keep the toggle reachable.
    const docked = CSS.slice(CSS.indexOf('.ct-split[data-docked="true"]'));
    expect(docked).toContain(".ct-split-items { display: none; }");
  });

  it("the run list offers a way back to all apps", () => {
    // A split with no exit is a trap: the only way out was the browser back button.
    expect(UI).toContain("ct-detail-back");
    expect(UI).toContain("← All apps");
  });

  it("collapses to one column rather than two columns too narrow to use", () => {
    expect(CSS).toMatch(/@media \(max-width: 1100px\)[\s\S]{0,400}grid-template-columns: 1fr/);
  });
});

describe("steps — one table, with who ran what", () => {
  it("no longer renders the same steps twice under two headings", () => {
    // "Step timing" listed phases; "agent activity" listed stages. Same work, two vocabularies,
    // stacked — so "what took 16 minutes and who was doing it" meant joining them by eye.
    expect(UI).not.toContain('<div className="ct-block-title">Step timing</div>');
    expect(UI).toContain("Steps — who ran what");
  });

  it("names the agent responsible for each call, not just the model", () => {
    // The model was on screen all along; the agent was not.
    const table = UI.slice(UI.indexOf("function CodeTaskSteps"));
    expect(table).toContain("ct-call-agent");
    expect(table).toContain("agentLabel(c.role ?? STAGE_AGENT[c.stage])");
  });

  it("names the agent even when the API does not send one", () => {
    // Older deployments do not return a role, and an Agent column full of dashes is worse than
    // no column. The row prefers what the API says and falls back to the stage's owner.
    expect(UI).toContain("const STAGE_AGENT");
    expect(UI).toMatch(/solo:\s*"software_engineer"/);
    expect(UI).toMatch(/review_code:\s*"adversarial_reviewer"/);
  });

  it("joins a call to its phase by STAGE, not by matching the two names", () => {
    // The first merge deduped by comparing a phase name to a stage name — the very two
    // vocabularies that made the old screens look duplicated — so "Engineer implementing
    // 16m 42s" and "solo 16m 42s" both appeared and the total counted the work twice.
    expect(UI).toContain("const PHASE_STAGES");
    expect(UI).toMatch(/implement:\s*\["implement", "solo", "repair", "guest"\]/);
    const table = UI.slice(UI.indexOf("function CodeTaskSteps"));
    expect(table).toContain("stages.includes(c.stage)");
  });

  it("shows a call no phase claimed rather than dropping it", () => {
    // Dropping it would hide spend, and hidden spend is the worst thing this block could do.
    const table = UI.slice(UI.indexOf("function CodeTaskSteps"));
    expect(table).toContain("orphans");
    expect(table).toContain("Other calls");
  });

  it("counts each call once", () => {
    // A call claimed by one phase must not also be claimed by the next.
    const table = UI.slice(UI.indexOf("function CodeTaskSteps"));
    expect(table).toContain("!used.has(i)");
    expect(table).toContain("used.add(i)");
  });
});

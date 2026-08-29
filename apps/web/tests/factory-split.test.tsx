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

  it("selects one app at a time", () => {
    expect(UI).toContain("selectedApp");
    expect(UI).toContain("setSelectedApp");
  });

  it("renders the runs in the detail pane, not inside the list item", () => {
    const detail = UI.slice(UI.indexOf("ct-split-detail"));
    expect(detail).toContain("renderCard");
    // The list item ends before the detail begins, so the runs cannot be inside it.
    const item = UI.slice(UI.indexOf("ct-app-item"), UI.indexOf("ct-split-detail"));
    expect(item).not.toContain("chosen.map");
  });

  it("puts the preview at the top of the detail, above the runs", () => {
    // It sat below the run list, the target picker and the spend line — four scrolls from the
    // name of the app it previews.
    const detail = UI.slice(UI.indexOf("ct-split-detail"));
    const preview = detail.indexOf("ct-detail-preview");
    const runs = detail.indexOf("chosen.map");
    expect(preview).toBeGreaterThan(-1);
    expect(runs).toBeGreaterThan(preview);
  });

  it("the list docks away", () => {
    expect(UI).toContain("ct-dock-toggle");
    expect(CSS).toContain('.ct-split[data-docked="true"]');
    // Docking must hide the items but keep the toggle reachable.
    const docked = CSS.slice(CSS.indexOf('.ct-split[data-docked="true"]'));
    expect(docked).toContain(".ct-split-items { display: none; }");
  });

  it("the detail is never an empty box beside a full list", () => {
    // Defaults to the first app rather than requiring a click to show anything.
    expect(UI).toMatch(/chosenKey\s*=\s*\(selectedApp && groups\.has\(selectedApp\)\)/);
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

  it("names the agent responsible for each step, not just the model", () => {
    // The model was shown all along; the agent was not, and that is what the operator asked for.
    const table = UI.slice(UI.indexOf("function CodeTaskSteps"));
    expect(table).toContain("ct-step-agent");
    expect(table).toContain("agentLabel(c.role)");
  });

  it("keeps phases that no agent ran rather than dropping them", () => {
    // Cloning a repo takes real time and no model call. Dropping those rows would silently
    // mis-attribute the wall clock to the steps that remain.
    const table = UI.slice(UI.indexOf("function CodeTaskSteps"));
    expect(table).toContain("unattributed");
    expect(table).toContain("no agent");
  });

  it("does not attribute a phase to an agent twice", () => {
    // A phase whose stage produced calls is described BY those calls; only phases with no call
    // of their own get their own row, or the total would double-count them.
    const table = UI.slice(UI.indexOf("function CodeTaskSteps"));
    expect(table).toMatch(/callStages\s*=\s*new Set\(calls\.map/);
    expect(table).toMatch(/phaseTimings\.filter\(\(t\) => !callStages\.has\(t\.phase\)\)/);
  });
});

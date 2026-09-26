/**
 * A customer's own workflows sat under the twelve BOSS ships, on a tab called Catalogue — so
 * the thing somebody built was filed beneath the things they did not, and the tab's count was
 * the two added together. Examples make that worse: install three and your own work is below
 * a list you did not write.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const ui = readFileSync(join(__dirname, "../app/delivery-workflows-ui.tsx"), "utf8");
const proxy = readFileSync(join(__dirname, "../app/api/delivery-workflows/route.ts"), "utf8");

describe("your workflows have their own tab", () => {
  it("adds the tab", () => {
    expect(ui).toContain('const TABS = ["running", "yours", "catalogue", "design", "steps"]');
    expect(ui).toContain("function YoursView(");
  });

  it("counts only what BOSS ships on the Catalogue tab", () => {
    // It added the customer's own to the total, so the number answered no question anybody had.
    expect(ui).toContain("`Catalogue (${catalogue.length})`");
    expect(ui).toContain("`Yours (${ownWorkflows(installed).length})`");
  });

  it("no longer renders your workflows inside the catalogue", () => {
    const catalogue = ui.slice(ui.indexOf("function CatalogueView("));
    expect(catalogue).not.toContain('data-testid="dw-mine"');
  });
});

describe("a workflow of your own can be scheduled", () => {
  it("offers Schedule on the card", () => {
    expect(ui).toContain("dw-schedule-");
  });

  it("only once there is a published version to run", () => {
    // The API refuses a schedule pointing at a workflow with no version, and offering the
    // button is how somebody meets that refusal for no reason.
    const yours = ui.slice(ui.indexOf("function YoursView("));
    const button = yours.slice(yours.indexOf("dw-schedule-") - 400, yours.indexOf("dw-schedule-"));
    expect(button).toContain("item.active_version ? (");
  });

  it("asks for the time zone rather than guessing", () => {
    // A report arriving at 3am because the browser's zone was used and nobody said so.
    expect(ui).toContain("in which time zone?");
    expect(ui).toContain("resolvedOptions().timeZone");
  });

  it("shows the schedule in the API's own words", () => {
    // So the console cannot word a schedule differently from the thing that fires it.
    expect(ui).toContain("{s.describes}");
    expect(ui).toContain("dw-sched-toggle-");
  });

  it("surfaces a schedule that has been failing", () => {
    expect(ui).toContain("last run failed");
  });

  it("routes pause, resume and remove through the proxy", () => {
    expect(proxy).toContain("SCHEDULE_ACTIONS");
    expect(proxy).toContain("schedules/${encodeURIComponent(id)}/enabled");
    expect(proxy).toContain("schedule_id");
  });

  it("does not fail the whole screen when schedules cannot be read", () => {
    // An install whose schedule table predates this feature must still show its workflows.
    expect(ui).toContain("SCHEDULES ARE NOT LOAD-BEARING");
  });
});

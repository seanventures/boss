/**
 * THE COMMAND CENTER'S FIRST PAINT SHIPS SLIM APPS (console latency, 2026-10-09).
 *
 * /v1/factory/apps draws every attempt of every app with its whole station line (~470 KB for 35
 * apps on the seeded stack) and the Overview's server render serialised all of it into the HTML
 * (1.16 MB). parseCcApps reads an attempt's run_id and state only, so that is all the first paint
 * carries — and it must parse to exactly the same cards.
 */
import { describe, expect, it } from "vitest";

import { parseCcApps } from "../lib/command-center-apps";
import { slimCcAppsWire } from "../lib/command-center-view-server";

const station = (key: string) => ({ key, label: key, status: "done", face: "none", role_key: "qa",
  act_ids: ["a", "b"], seats: [], word: "Done", tip: "t".repeat(80), links: [] });
const attempt = (run_id: string, state: string) => ({ run_id, state, word: state, attempt: 1,
  stations: ["plan", "build", "tests", "review", "ship"].map(station), verb: "v", tip: "x".repeat(200) });
const wire = {
  project_id: "p", etag: "e1",
  apps: [
    { key: "crun_1", app: "Shop", run_id: "crun_1", builders: ["qa"], state: "pr_open", word: "PR open",
      pr_number: 7, pr_url: "u", pr_state: "open", cost_usd: 1.5, included: false, builds: 2,
      run_ids: ["crun_1", "crun_0"], at: "2026-10-09T10:00:00Z",
      attempts: [attempt("crun_1", "needs_plan"), attempt("crun_0", "failed")] },
    { key: "crun_9", app: "Wiki", run_id: "crun_9", builders: [], state: "building", word: "Building",
      builds: 1, run_ids: ["crun_9"], attempts: [attempt("crun_9", "building")] },
  ],
};

describe("the Command Center's apps on first paint", () => {
  it("parse to exactly the cards the full answer parses to", () => {
    expect(parseCcApps(slimCcAppsWire(wire))).toEqual(parseCcApps(wire));
  });

  it("carry no station lines", () => {
    const slim = JSON.stringify(slimCcAppsWire(wire));
    expect(slim).not.toContain("stations");
    expect(slim.length).toBeLessThan(JSON.stringify(wire).length / 3);
  });

  it("leave anything that is not an apps answer as it is", () => {
    expect(slimCcAppsWire(null)).toBeNull();
    expect(slimCcAppsWire({ error: "x" })).toEqual({ error: "x" });
  });
});

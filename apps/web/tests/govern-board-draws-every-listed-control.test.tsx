// @vitest-environment happy-dom
/**
 * Govern › More › Advanced limits draws every control the API lists (v1.0-rc10.32).
 *
 * The release stranger test looked for "Agents fix failures with your authority" and found it
 * nowhere: the engine listed it and the board drew a fixed list without it. The board now draws
 * the `sections` GET /v1/govern returns, row for row; its fallback (an older engine, or BOSS out
 * of reach) is the engine's BOARD, checked here against the engine source so the two can't drift.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { FALLBACK_SECTIONS } from "../app/govern/control-board";
import { control, fakeGovernApi, governView, newGovern, type FakeGovern } from "./_helpers/govern-fixtures";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { GovernHome } = await import("../app/govern/govern-home");

const FIX_EXPLAIN = "Agents fix failures with the starter's authority — on by default. Never merges, deploys to production, "
  + "approves, or touches secrets. Set per project: this board sets the project chosen at the top. Off: the starter is asked each time.";

const sw = (key: string, name: string, section: string, explain: string) => control(key, {
  name, section, explain, unit: "switch", value_effective: 1, bounds: { min: 0, max: 1, step: 1 },
  write: { method: "PUT", path: `/v1/settings/${key}` },
  enforced: { word: section === "each_build" ? "Each build" : "Each failed step", effect_key: section } });

function viewWithSections() {
  const base = governView() as { controls: Record<string, unknown>[] };
  const controls = [
    ...base.controls,
    sw("agents_fix_failures", "Agents fix failures with your authority", "each_failed_step", FIX_EXPLAIN),
    sw("step_recovery", "Agents recover failed steps", "each_failed_step", "A failed step's agent reads the evidence and retries or fixes it."),
    control("recovery_budget_cents", { name: "Recovery budget (cents)", section: "each_failed_step", value_effective: 300,
                                       explain: "The most one failed step's recovery may spend before it asks you." }),
    sw("open_pr_when_checks_fail", "Open the PR when checks fail", "each_build", "Checks still failing: the PR opens as a draft."),
    control("quality_bar", { name: "Quality bar", section: "each_build", unit: "choice", value_effective: "standard",
                             bounds: { choices: ["prototype", "standard", "production"] }, explain: "How strict every gate is." }),
  ];
  const keysOf = (section: string) => controls.filter((c) => c.section === section).map((c) => c.key as string);
  return {
    ...base, controls,
    sections: [
      { key: "spend", name: "Spend caps", keys: ["daily_cap", "monthly_cap", "burst", "pace"] },
      { key: "agents", name: "How hard agents work", keys: keysOf("agents") },
      { key: "each_failed_step", name: "Each failed step", keys: keysOf("each_failed_step") },
      { key: "each_build", name: "Each build", keys: keysOf("each_build") },
    ],
  };
}

let g: FakeGovern;
beforeEach(() => {
  g = newGovern({ view: viewWithSections() });
  vi.stubGlobal("fetch", fakeGovernApi(g));
  setConsoleLive(true);
  window.history.replaceState({}, "", "/?area=budget&tab=board");
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("the board draws every control the API lists", () => {
  it("every key of every section the API returns is a row, in its section", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    for (const s of (g.view as { sections: { key: string; keys: string[] }[] }).sections) {
      const sec = $(`[data-testid=gv-sec-${s.key}]`);
      expect(sec, s.key).not.toBeNull();
      for (const key of s.keys) expect(sec!.querySelector(`[data-testid=gv-control-${key}]`), key).not.toBeNull();
    }
    expect($("#gv-sec-each_failed_step")!.textContent).toBe("EACH FAILED STEP");
  });

  it("Each failed step › Agents fix failures with your authority: On, a plain line, and a toggle that saves", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    const row = $("[data-testid=gv-sec-each_failed_step] [data-testid=gv-control-agents_fix_failures]")!;
    expect(row.querySelector(".gv-name > span")!.textContent).toBe("Agents fix failures with your authority");
    expect($("[data-testid=gv-value-agents_fix_failures]")!.textContent).toBe("On");
    expect($("[data-testid=gv-explain-agents_fix_failures]")!.textContent).toBe(FIX_EXPLAIN);
    await click($("[data-testid=gv-more-agents_fix_failures]"));
    await click($("[data-testid=gv-edit-agents_fix_failures]"));
    const toggle = $("[data-testid=gv-editor-agents_fix_failures] [role=switch]")!;
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    await click(toggle);
    await click($("[data-testid=gv-save]"));
    await settle();
    const write = g.calls.find((c) => c.op === "setting");
    expect(write, JSON.stringify(g.calls)).toBeTruthy();
    expect(write!.body).toMatchObject({ key: "agents_fix_failures", value: "0", project_id: "p1" });
  });

  it("org policy off: the row reads Off, says why, and can't be edited", async () => {
    const view = viewWithSections();
    view.controls = view.controls.map((c) => c.key !== "agents_fix_failures" ? c : {
      ...c, value_effective: 0, editable_by: "none", write: null,
      note: { kind: "org_off", text: "Off for the whole organisation (Identity › Policy)" } });
    g.view = view;
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    expect($("[data-testid=gv-value-agents_fix_failures]")!.textContent).toBe("Off");
    expect($("[data-testid=gv-note-agents_fix_failures]")!.textContent).toBe("Off for the whole organisation (Identity › Policy)");
    expect($("[data-testid=gv-more-agents_fix_failures]")).toBeNull();
  });

  it("the other switches and the quality bar are drawn too", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    expect($("[data-testid=gv-sec-each_failed_step] [data-testid=gv-value-step_recovery]")!.textContent).toBe("On");
    expect($("[data-testid=gv-sec-each_build] [data-testid=gv-value-open_pr_when_checks_fail]")!.textContent).toBe("On");
    expect($("[data-testid=gv-value-quality_bar]")!.textContent).toBe("Standard");
    await click($("[data-testid=gv-more-quality_bar]"));
    await click($("[data-testid=gv-edit-quality_bar]"));
    expect($$("[data-testid=gv-choice] option").map((o) => o.textContent)).toEqual(["prototype", "standard", "production"]);
  });

  it("the fallback board is the engine's BOARD, row for row", () => {
    const src = readFileSync(resolve(__dirname, "../../../engine/boss_engine/services/govern_view.py"), "utf8");
    const board = src.slice(src.indexOf("BOARD: tuple"), src.indexOf("\n)\n", src.indexOf("BOARD: tuple")));
    const sections = [...board.matchAll(/\("(\w+)", "([^"]+)",\s*\(([^)]*)\)/g)].map((m) => ({
      key: m[1], name: m[2], keys: [...m[3].matchAll(/"(\w+)"/g)].map((k) => k[1]) }));
    expect(sections.length).toBeGreaterThanOrEqual(5);
    expect(FALLBACK_SECTIONS.map((s) => ({ key: s.key, name: s.name, keys: s.rows.map(([k]) => k) }))).toEqual(sections);
  });
});

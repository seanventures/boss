// @vitest-environment happy-dom
/**
 * Govern › Limits tells the truth about each control (spec §4.4.3, §1.4):
 *   - the value shown is what the engine will use, and a stored value that runs as something
 *     else says so (`set 3 · min 6`, `set 0 · reads as 2`);
 *   - every control names what enforces it; token guides are `Advisory` and look it;
 *   - evidence is a recorded count linked into the Log, or `—` when BOSS records nothing;
 *   - Pace is `Not set` on a fresh install, and claims no model tiers;
 *   - the daily cap and burst say Pace sets them.
 * Plus the states: no blur-save, 412 / 403 / 422, unreachable, frozen, saved and reset toasts.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { fakeGovernApi, governView, newGovern, type FakeGovern } from "./_helpers/govern-fixtures";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { GovernHome } = await import("../app/govern/govern-home");

let g: FakeGovern;
let toasts: string[];
const onToast = (e: Event) => toasts.push((e as CustomEvent).detail.message);
beforeEach(() => {
  g = newGovern();
  toasts = [];
  window.addEventListener("boss:toast", onToast);
  vi.stubGlobal("fetch", fakeGovernApi(g));
  setConsoleLive(true);
  window.history.replaceState({}, "", "/?area=budget");
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); window.removeEventListener("boss:toast", onToast); });

const t = (sel: string) => $(sel)?.textContent ?? null;
async function setValue(el: Element | null, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
    el!.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await settle();
}

describe("the board shows the engine's numbers", () => {
  it("effective values, with the stored value beside them when they differ", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    expect(t("[data-testid=gv-value-code_agent_max_concurrency]")).toBe("6");
    expect(t("[data-testid=gv-note-code_agent_max_concurrency]")).toBe("set 3 · min 6");
    expect($("[data-testid=gv-note-code_agent_max_concurrency]")!.getAttribute("title")).toBe("Values under 6 behave like 6 today");
    expect(t("[data-testid=gv-value-code_review_inrun_max_rounds]")).toBe("2");
    expect(t("[data-testid=gv-note-code_review_inrun_max_rounds]")).toBe("set 0 · reads as 2");
    expect($("[data-testid=gv-note-code_review_inrun_max_rounds]")!.getAttribute("title")).toBe("0 is read as 2 today");
    expect(t("[data-testid=gv-value-captain_leads_projects]")).toBe("Off");
    expect(t("[data-testid=gv-value-daily_cap]")).toBe("$10.00");
    expect(t("[data-testid=gv-value-burst]")).toBe("$0.10/min");
    expect(t("[data-testid=gv-value-pace]")).toBe("Not set");
  });

  it("names the enforcer of every row; token guides are Advisory and greyed", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    expect(t("[data-testid=gv-control-daily_cap] .gv-enforced")).toBe("Hard cap");
    expect($("[data-testid=gv-control-daily_cap] .gv-enforced")!.getAttribute("title")).toBe("BOSS refuses the reservation and asks you");
    expect(t("[data-testid=gv-control-burst] .gv-enforced")).toBe("Waits");
    for (const id of ["gv-control-token_default", "gv-control-token_per_step"]) {
      expect($(`[data-testid=${id}]`)!.getAttribute("data-advisory")).toBe("true");
      expect(t(`[data-testid=${id}] .gv-enforced`)).toBe("Advisory");
      expect($(`[data-testid=${id}] .gv-enforced`)!.getAttribute("title")).toBe("Shapes alerts and tips. Doesn't stop model calls.");
    }
    expect(t("[data-testid=gv-value-token_default]")).toBe("5M tok/day · $50.00/day · alert 80%");
  });

  it("evidence is a recorded count that opens the Log, or a dash that says why", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    const daily = $("[data-testid=gv-evidence-daily_cap]")!;
    expect(daily.textContent).toBe("Stopped 2× · 7d");
    const href = new URLSearchParams(daily.getAttribute("href")!.split("?")[1]);
    expect([href.get("area"), href.get("kind"), href.get("range")]).toEqual(["activity", "budget.reservation_denied", "7d"]);
    expect(t("[data-testid=gv-evidence-code_agent_max_turns]")).toBe("Hit 1× · 7d");
    const dash = $("[data-testid=gv-control-captain_leads_projects] .gv-evidence span")!;
    expect([dash.textContent, dash.getAttribute("title")]).toEqual(["—", "BOSS doesn't record when this bites"]);
  });

  it("the daily cap and burst are set by Pace only; Pace previews exactly what it writes", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-more-daily_cap]"));
    const item = $("[data-testid=gv-edit-daily_cap]")! as HTMLButtonElement;
    expect([item.textContent, item.disabled, item.getAttribute("title")]).toEqual(
      ["Set by Pace", true, "Today only Pace sets this. See Pace."]);
    await click($("[data-testid=gv-more-pace]"));
    await click($("[data-testid=gv-edit-pace]"));
    await act(async () => { await new Promise((r) => setTimeout(r, 200)); });
    await settle();
    expect(t("[data-testid=gv-pace-preview]")).toBe("Daily $252 · Monthly $5,040 · Burst $5.25/min · Jobs 11 · Workers 15");
    // The old slider claimed to choose "which model tiers are allowed"; nothing reads that column.
    expect($("[data-testid=gv-control-pace]")!.textContent).not.toMatch(/tier|low-cost only|strong/i);
  });

  it("an edit shows what BOSS will use before saving, and is never saved on blur", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-more-code_agent_max_concurrency]"));
    await click($("[data-testid=gv-edit-code_agent_max_concurrency]"));
    const input = $("[data-testid=gv-input]")!;
    await setValue(input, "4");
    expect(t("[data-testid=gv-will-use]")).toBe("BOSS will use 6");
    await setValue(input, "9");
    expect(t("[data-testid=gv-will-use]")).toBe("BOSS will use 9");
    await act(async () => { input.dispatchEvent(new FocusEvent("blur")); input.dispatchEvent(new FocusEvent("focusout", { bubbles: true })); });
    await settle();
    expect(g.calls.filter((c) => c.method === "POST")).toHaveLength(0);
    await act(async () => { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    await settle();
    expect($("[data-testid=gv-editor-code_agent_max_concurrency]")).toBeNull();
    expect(g.calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("the monthly cap previews its effect on the last 30 days", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-more-monthly_cap]"));
    await click($("[data-testid=gv-edit-monthly_cap]"));
    await setValue($("[data-testid=gv-input]"), "20");
    await act(async () => { await new Promise((r) => setTimeout(r, 350)); });
    await settle();
    expect(t("[data-testid=gv-would-stop]")).toBe("Would have stopped 3 days");
    expect($("[data-testid=gv-would-stop]")!.getAttribute("title")).toBe("Replays the last 30 days of real spend");
    expect(g.calls.filter((c) => c.op === "cap_preview").at(-1)!.body).toMatchObject({ period: "month", cap_usd: 20 });
  });
});

describe("every write outcome says what happened", () => {
  async function editTurns(value = "37") {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-more-code_agent_max_turns]"));
    await click($("[data-testid=gv-edit-code_agent_max_turns]"));
    await setValue($("[data-testid=gv-input]"), value);
    await click($("[data-testid=gv-save]"));
  }

  it("saved: a toast with the control's name, and the board re-read", async () => {
    await editTurns();
    expect(toasts).toContain("Turn budget saved");
    expect(g.calls.filter((c) => c.method === "GET").length).toBe(2);
  });

  it("a stale edit: who changed it, and Reload", async () => {
    g.writeStatus = 412;
    g.writeBody = { error: "Changed by ops_lead just now", refusal: { message: "Changed by ops_lead just now", changed_by: "ops_lead" } };
    await editTurns();
    expect(t("[data-testid=gv-fail]")).toBe("Changed by ops_lead just nowReload");
  });

  it("no rights: You can't change this, naming the right in the tooltip", async () => {
    g.writeStatus = 403;
    g.writeBody = { error: "Member lacks providerops_manage" };
    await editTurns();
    expect(t("[data-testid=gv-fail]")).toBe("You can't change this");
    expect($("[data-testid=gv-fail]")!.getAttribute("title")).toBe("Needs provider rights");
  });

  it("out of bounds: the server's own sentence", async () => {
    g.writeStatus = 422;
    g.writeBody = { error: "Turn budget per agent has to be between 10 and 200." };
    await editTurns("150");
    expect(t("[data-testid=gv-fail]")).toBe("Turn budget per agent has to be between 10 and 200.");
  });

  it("a default reads as the default: \"90 (default)\", never a note about a value no one set", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    expect(t("[data-testid=gv-control-code_agent_max_turns] .gv-value")).toBe("90(default)");
  });

  it("under memory pressure the lanes say what the panel runs now", async () => {
    g.view = governView();
    const lanes = (g.view.controls as Record<string, unknown>[]).find((c) => c.key === "code_agent_max_concurrency")!;
    lanes.running_now = { value: 1, reason: "memory" };
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    const now = $("[data-testid=gv-running-now-code_agent_max_concurrency]")!;
    expect(t("[data-testid=gv-control-code_agent_max_concurrency] .gv-value")).toContain("6");
    expect(now.textContent).toBe("· running 1 now (memory)");
    expect(now.getAttribute("title")!.split(/\s+/).length).toBeLessThanOrEqual(14);
  });

  it("Use default: a toast that says it is back", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-reset-code_review_inrun_max_rounds]"));
    expect(toasts).toContain("Review rounds back to default");
  });
});

describe("states", () => {
  it("unreachable: the meter says so with Retry, the board shows dashes and cannot be edited", async () => {
    g.status = 502;
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    expect(t("[data-testid=gv-meter]")).toBe("Can't reach BOSSRetry");
    expect($$("[data-testid^=gv-value-]").every((v) => v.textContent === "—")).toBe(true);
    expect($$("[data-testid^=gv-more-]").every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
    expect($("[data-testid=gv]")!.textContent).not.toContain("No adjustable settings");
    // D8: rows still read as names, never raw keys.
    expect($$("[data-testid^=gv-control-] .gv-name").map((n) => n.textContent)).toEqual([
      "Daily cap", "Monthly cap", "Burst", "Pace", "Review lanes", "Turn budget", "Review rounds",
      "Captain leads", "Captain self-improves", "Jobs at once", "Workers", "Project default", "Per step"]);
  });

  it("frozen: one line above the board, with the way out", async () => {
    g.view = governView({ meter: { ...(governView().meter as object), state: "red", state_label: "Frozen" } });
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    expect(t("[data-testid=gv-frozen]")).toBe("New work is waiting for budgetRaise monthly capStopped ↗");
    expect(t("[data-testid=gv-meter-state]")).toBe("Frozen");
  });

  it("?area=govern lands on Govern, and focus= rings its row", async () => {
    const { resolveLegacyArea } = await import("../app/legacy-areas");
    const next = resolveLegacyArea(new URLSearchParams("area=govern&focus=monthly_cap&org=o1"))!;
    expect([next.get("area"), next.get("focus"), next.get("org")]).toEqual(["budget", "monthly_cap", "o1"]);
    window.history.replaceState({}, "", "/?area=budget&focus=monthly_cap");
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    expect($("[data-testid=gv-control-monthly_cap]")!.getAttribute("data-focused")).toBe("true");
  });

  it("every Govern motion has a reduced-motion still", () => {
    const css = readFileSync(resolve(__dirname, "../app/govern/govern.css"), "utf8");
    const still = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce)"));
    for (const moving of [".gv-bar-fill", ".gv-row[data-focused]", ".gv-value[data-pulsed]", ".gv-chip[data-passed]"]) {
      expect(still).toContain(moving);
    }
  });
});

describe("the Captain's own improve builds are a Govern switch (first-run walk 2026-10-10)", () => {
  it("reads Off on a fresh install, says in one line what it does, and saves as 0/1", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    expect(t("[data-testid=gv-value-captain_auto_improve]")).toBe("Off");
    const label = $("[data-testid=gv-control-captain_auto_improve] span[title]")!;
    expect(label.textContent).toBe("Captain self-improves");
    expect(label.getAttribute("title")).toMatch(/^Off: the Captain only proposes improve builds\. On: it starts them, spending budget\.$/);
  });
});

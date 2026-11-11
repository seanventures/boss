// @vitest-environment happy-dom
/**
 * Every number a Govern or Log form takes is saved as typed, or refused with a reason on the
 * screen (D1). The class: a native `step`/`min` mismatch made the browser drop the submit with
 * no sign — whole dollars could never be saved as a monthly cap. Forms are `noValidate`, and the
 * field says what is wrong. `click` (live-render) refuses a submit the way a browser would, so
 * these tests fail exactly where the operator was stuck.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { money, readNumber } from "../lib/govern";
import { fakeGovernApi, newGovern, type FakeGovern } from "./_helpers/govern-fixtures";
import { FACETS, fakeLogApi, newLog } from "./_helpers/log-fixtures";
import { $, $$, browserWouldRefuse, click, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { GovernHome } = await import("../app/govern/govern-home");
const { LogExplorer } = await import("../app/activity/log-explorer");

let g: FakeGovern;
beforeEach(() => {
  g = newGovern();
  vi.stubGlobal("fetch", fakeGovernApi(g));
  setConsoleLive(true);
  window.history.replaceState({}, "", "/?area=budget&tab=board");
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

async function type(el: Element | null, value: string) {
  await act(async () => {
    const input = el as HTMLInputElement;
    const proto = input instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event(input instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
  await settle();
}
const sent = (op: string) => g.calls.filter((c) => c.method === "POST" && c.op === op).map((c) => c.body);

async function openMonthly() {
  await mount(<GovernHome projectId="p1" actor="ceo" />);
  await click($("[data-testid=gv-more-monthly_cap]"));
  await click($("[data-testid=gv-edit-monthly_cap]"));
}

describe("the monthly cap", () => {
  it.each([["150", 150], ["150.01", 150.01], ["9.5", 9.5]])("saves %s as typed", async (typed, usd) => {
    await openMonthly();
    await type($("[data-testid=gv-input]"), typed);
    expect($("[data-testid=gv-field-error]")).toBeNull();
    await click($("[data-testid=gv-save]"));
    expect(sent("monthly_cap")).toHaveLength(1);
    expect(sent("monthly_cap")[0]).toMatchObject({ monthly_cap_usd: usd });
  });

  it.each([["0", "$0.01–$1,000,000"], ["150.015", "Cents at most"], ["", "Enter a number"]])(
    "refuses %s with the reason on screen, never silently", async (typed, reason) => {
      await openMonthly();
      await type($("[data-testid=gv-input]"), typed);
      expect($("[data-testid=gv-field-error]")?.textContent).toBe(reason);
      expect(($("[data-testid=gv-save]") as HTMLButtonElement).disabled).toBe(true);
      expect(sent("monthly_cap")).toHaveLength(0);
    });
});

describe("the class", () => {
  it("no Govern or Log form can be refused by the browser without a word", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    const opened: string[] = [];
    for (const key of ["monthly_cap", "code_agent_max_turns", "code_agent_max_concurrency", "pace"]) {
      const more = $(`[data-testid=gv-more-${key}]`);
      if (!more) continue;
      await click(more);
      await click($(`[data-testid=gv-edit-${key}]`));
      opened.push(key);
      for (const form of $$("form")) expect(browserWouldRefuse(form as HTMLFormElement), key).toEqual([]);
      for (const form of $$("form")) expect((form as HTMLFormElement).noValidate, key).toBe(true);
      const cancel = $("[data-testid=gv-cancel]");
      if (cancel) await click(cancel);
    }
    expect(opened.length).toBeGreaterThanOrEqual(3);
    unmount();
    vi.stubGlobal("fetch", fakeLogApi(newLog({ facets: FACETS })));
    window.history.replaceState({}, "", "/?area=activity");
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    for (const form of $$("form")) expect((form as HTMLFormElement).noValidate).toBe(true);
  });

  it("a whole-number control saves any whole number in range and says why otherwise", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-more-code_agent_max_turns]"));
    await click($("[data-testid=gv-edit-code_agent_max_turns]"));
    await type($("[data-testid=gv-input]"), "12.5");
    expect($("[data-testid=gv-field-error]")?.textContent).toMatch(/^Whole numbers/);
    await type($("[data-testid=gv-input]"), "137");
    await click($("[data-testid=gv-save]"));
    expect(sent("setting").at(-1)).toMatchObject({ value: "137" }); // the old knob body: a string
  });

});

describe("money never hides cents that differ", () => {
  it.each([[150_010_000, "$150.01"], [150_000_000, "$150"], [5_040_370_000, "$5,040.37"], [5_040_000_000, "$5,040"],
           [2_000_000, "$2.00"], [4_000, "<$0.01"], [0, "$0.00"]])("%d → %s", (micros, text) => {
    expect(money(micros)).toBe(text);
  });
  it("readNumber agrees with what it says", () => {
    expect(readNumber("150", { min: 0.01, max: 1_000_000, cents: true, money: true })).toEqual({ value: 150, error: null });
  });
});

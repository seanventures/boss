// @vitest-environment happy-dom
/**
 * Govern › Self-fix (docs/product/BOSS_FIXES_ITSELF.md §8.3, §8.4, §8.7, §10.5): the kill switch,
 * the caps, "Ready to fix itself" (ENT-REC-54: where BOSS was installed from, its source mirror,
 * the host installer and the mode, each missing piece with one action), the installed stack with
 * Remove all local fixes (behind a confirm). BOSS_SELF_FIX=off and an install that cannot change
 * itself are said plainly. Short labels; the why in tooltips.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../../lib/console-live";
import { overBudget } from "../_helpers/copy-budget";
import { fakeGovernApi, newGovern } from "../_helpers/govern-fixtures";
import { click, mountAs, tid, type, unmount } from "../_helpers/multirepo-fixtures";
import { fakeSelfFixApi, newState, opsOf, readiness, settings } from "./_fixtures";

const { GovernHome } = await import("../../app/govern/govern-home");

afterEach(() => { unmount(); vi.unstubAllGlobals(); window.history.replaceState({}, "", "/"); });

async function open(state = newState()) {
  const calls = fakeSelfFixApi(state);
  const mine = globalThis.fetch;
  const govern = fakeGovernApi(newGovern());
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) =>
    String(input).startsWith("/api/self-fix") ? mine(input, init) : govern(input, init));
  setConsoleLive(true);
  window.history.replaceState({}, "", "/?area=budget&tab=selffix");
  await mountAs(<GovernHome projectId="p1" actor="ceo" />, "owner");
  return calls;
}

describe("Govern › Self-fix", () => {
  it("is a Govern tab, and draws the switch, caps, auto-install, stack and installer", async () => {
    await open();
    expect(tid("gv-tab-selffix")?.textContent).toBe("Self-fix");
    expect(tid("gv-tab-selffix")?.getAttribute("aria-selected")).toBe("true");
    const root = tid("gv-selffix")!;
    expect(overBudget(root)).toEqual([]);
    expect(tid("gv-selffix-switch")?.getAttribute("aria-checked")).toBe("true");
    expect(tid("gv-selffix-switch")?.getAttribute("title")).toContain("installed fixes stay");
    expect((tid("gv-selffix-self_fix_budget_cents") as HTMLInputElement).value).toBe("10");
    expect((tid("gv-selffix-self_fix_month_cents") as HTMLInputElement).value).toBe("50");
    expect((tid("gv-selffix-self_fix_max_minutes") as HTMLInputElement).value).toBe("60");
    expect((tid("gv-selffix-confidence") as HTMLSelectElement).value).toBe("medium");
    expect(tid("gv-selffix-ready-state")?.textContent).toBe("Ready");
    expect(tid("gv-selffix-state-source")?.textContent).toContain("/home/me/boss @ 1a2b3c4d5e6f");
    expect(tid("gv-selffix-auto")?.textContent).toBe("Asks");
    expect(tid("gv-selffix-auto")?.tagName).not.toBe("BUTTON");
    expect(tid("gv-selffix-do-mode")?.getAttribute("href")).toBe("?area=budget&tab=autonomy");
    expect(tid("gv-selffix-stack")?.textContent).toContain("1 on the stack");
    expect(tid("gv-selffix-installer")?.textContent).toContain("watching, last seen");
    expect(tid("gv-selffix-spent")?.textContent).toBe("$3.12 this month");
  });

  it("the switch turns self-fix off, with who", async () => {
    const calls = await open();
    await click(tid("gv-selffix-switch"));
    expect(opsOf(calls, "settings")).toEqual([expect.objectContaining({ on: false, project_id: "p1", actor: "ceo" })]);
  });

  it("saves a changed cap in the API's unit (cents)", async () => {
    const calls = await open();
    expect((tid("gv-selffix-save") as HTMLButtonElement).disabled).toBe(true);
    await type(tid("gv-selffix-self_fix_budget_cents"), "12.5");
    await click(tid("gv-selffix-save"));
    expect(opsOf(calls, "settings")[0]).toMatchObject({ settings: { self_fix_budget_cents: 1250 } });
  });

  it("Remove all local fixes asks once more, then asks BOSS", async () => {
    const calls = await open();
    await click(tid("gv-selffix-remove-all"));
    expect(opsOf(calls, "remove_all")).toHaveLength(0);
    await click(tid("gv-selffix-remove-all-yes"));
    expect(opsOf(calls, "remove_all")).toHaveLength(1);
  });

  it("names one action for each missing piece: a host command to copy, or the switch", async () => {
    const writes: string[] = [];
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: async (t: string) => { writes.push(t); } } });
    const calls = await open(newState({ settings: settings({ switch: { on: false, why: "nobody has claimed this install yet" },
      readiness: readiness({
        mirror: { ok: false, state: "Not seeded", why: "BOSS's own source is not on this machine yet.",
                  action: { kind: "command", label: "Seed it on the host", command: "boss self-fix seed" } },
        installer: { ok: false, state: "Not enrolled", why: "Nothing on this machine can install an approved fix yet.",
                     action: { kind: "command", label: "Enrol it on the host", command: "boss self-fix enable" } },
        mode: { ok: false, state: "Off", why: "nobody has claimed this install yet", action: { kind: "switch", label: "Turn on" } },
      }) }) }));
    const root = tid("gv-selffix")!;
    expect(overBudget(root)).toEqual([]);
    expect(tid("gv-selffix-ready-state")?.textContent).toBe("3 to do");
    expect(tid("gv-selffix-state-mirror")?.getAttribute("title")).toContain("not on this machine");
    await click(tid("gv-selffix-do-mirror"));
    await click(tid("gv-selffix-do-installer"));
    expect(writes).toEqual(["boss self-fix seed", "boss self-fix enable"]);
    expect(tid("gv-selffix-do-source")).toBeNull();
    await click(tid("gv-selffix-do-mode"));
    expect(opsOf(calls, "settings")).toEqual([expect.objectContaining({ on: true, project_id: "p1" })]);
  });

  it("says BOSS_SELF_FIX=off and that this install cannot change itself", async () => {
    await open(newState({ settings: settings({ env_off: true, can_install: false,
      why_not: "this install was not built from a git checkout, so it cannot change itself" }) }));
    expect(tid("gv-selffix-switch")?.getAttribute("aria-checked")).toBe("false");
    expect((tid("gv-selffix-switch") as HTMLButtonElement).disabled).toBe(true);
    expect(tid("gv-selffix-env-off")?.textContent).toBe("BOSS_SELF_FIX=off");
    expect(tid("gv-selffix-cannot")?.getAttribute("title")).toContain("not built from a git checkout");
  });
});

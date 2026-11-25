// @vitest-environment happy-dom
/**
 * BOSSAPP-1342: the Command Center header says how much subscription is left, so nobody opens the
 * provider's settings mid-work. The chip names the account closest to its limit ("Claude Code 3%
 * left · resets …"), toned by its state, lists every account (pool seats too) in its tooltip, and
 * opens Govern › Subscriptions. No subscription, no chip; an older API without the field, no chip.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { $, mount, unmount } from "./_helpers/live-render";
import { view } from "./_helpers/command-center-view-fixture";
import { parseCommandCenterView, parseHeadroomChip, viewerZone } from "../lib/command-center-view";
import { setConsoleLive } from "../lib/console-live";

vi.mock("../app/command-center/actions", () => ({ cleanUpOldAction: async () => ({ ok: true }) }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(), usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as { React?: typeof React }).React = React;
const { CommandCenter } = await import("../app/command-center-ui");

const acct = (over: Record<string, unknown> = {}) => ({
  key: "claude_code:pool:a", label: "Claude Code · Account 1", left_pct: 3, state: "near_limit",
  state_label: "Near limit", resets_at: "2026-11-27T11:00:00+00:00", login: "ok",
  at_limit: "Moves to the next Claude Code account: Account 2.", taking_new_work: false,
  pool: "Claude Code pool", ...over,
});
const page = (wire: Record<string, unknown>) => (
  <CommandCenter projectId="p1" orgId="org_1" actor="ceo" initial={parseCommandCenterView(wire)} tz={viewerZone()} />
);

beforeEach(() => {
  window.history.replaceState(null, "", "/?area=overview");
  setConsoleLive(true);
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 304, headers: new Headers(), json: async () => ({}) })));
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("the header's headroom chip", () => {
  it("names the account closest to its limit, every account in the tooltip, and opens Govern", async () => {
    await mount(page(view({ subscriptions: { accounts: [
      acct(), acct({ key: "claude_code:pool:b", label: "Claude Code · Account 2", left_pct: 80, state: "plenty",
                     state_label: "Plenty", taking_new_work: true }),
    ] } })));
    const chip = $("[data-testid=cc-headroom]") as HTMLAnchorElement;
    expect($("[data-testid=cc-status]")!.contains(chip)).toBe(true);       // in the header line
    expect($("[data-testid=cc-headroom-text]")!.textContent).toMatch(/^Claude Code · Account 1 3% left · resets /);
    expect($("[data-testid=cc-headroom-more]")!.textContent).toBe("+1");
    expect(chip.querySelector("[data-tone]")!.getAttribute("data-tone")).toBe("amber");
    expect(chip.querySelector("svg")).not.toBeNull();                       // the icon
    expect(chip.title).toMatch(/Account 2 80% left \(Claude Code pool, takes new work\)/);
    expect(chip.title).toMatch(/At the limit: Moves to the next Claude Code account/);
    expect(chip.getAttribute("aria-label")).toMatch(/^Subscription headroom:/);
    const href = new URL(chip.href, "http://x");
    expect([href.searchParams.get("area"), href.searchParams.get("tab"), href.searchParams.get("org")])
      .toEqual(["budget", "subscriptions", "org_1"]);
  });

  it("a limit reached says so with its reset; a malformed row is dropped, never drawn broken", async () => {
    const { headroomWords, headroomTone } = await import("../app/command-center/headroom-chip");
    const chip = parseHeadroomChip({ accounts: [acct({ state: "exhausted", left_pct: 0 }), { label: "no key" }] })!;
    expect(chip.accounts).toHaveLength(1);
    expect(headroomWords(chip.accounts[0])).toMatch(/^Claude Code · Account 1: limit reached · resets /);
    expect(headroomTone(chip.accounts[0])).toBe("red");
    expect(parseHeadroomChip({ accounts: [] })).toBeNull();
  });

  it("an expired sign-in reads as such, toned red", async () => {
    await mount(page(view({ subscriptions: { accounts: [acct({ login: "expired", left_pct: 60, state: "plenty" })] } })));
    expect($("[data-testid=cc-headroom-text]")!.textContent).toBe("Claude Code · Account 1: sign-in expired");
    expect($("[data-testid=cc-headroom] [data-tone]")!.getAttribute("data-tone")).toBe("red");
  });

  it("plenty left is a quiet green chip with no reset", async () => {
    await mount(page(view({ subscriptions: { accounts: [acct({ left_pct: 72, state: "plenty", pool: null })] } })));
    expect($("[data-testid=cc-headroom-text]")!.textContent).toBe("Claude Code · Account 1 72% left");
    expect($("[data-testid=cc-headroom] [data-tone]")!.getAttribute("data-tone")).toBe("green");
  });

  it("no subscription, or an API without the field: no chip", async () => {
    await mount(page(view({ subscriptions: null })));
    expect($("[data-testid=cc-headroom]")).toBeNull();
    unmount();
    await mount(page(view()));
    expect($("[data-testid=cc-headroom]")).toBeNull();
  });
});

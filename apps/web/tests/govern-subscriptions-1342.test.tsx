// @vitest-environment happy-dom
/**
 * BOSSAPP-1342: Govern shows each connected plan, so nobody opens the provider's settings.
 * An account at its limit raises a banner on every Govern tab; the Subscriptions tab shows the
 * windows, % left, the reset in the viewer's time, the jobs routed there, what BOSS does at the
 * limit, and how old a stale reading is. It rides on `/v1/govern` (one read opens Govern).
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { fakeGovernApi, newGovern, type FakeGovern } from "./_helpers/govern-fixtures";
import { $, click, mount, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { GovernHome } = await import("../app/govern/govern-home");

const source = (over: Record<string, unknown> = {}) => ({
  key: "claude_code:install", engine: "claude_code", engine_label: "Claude Code", label: "Claude Code (this machine's login)",
  left_pct: 0, state: "exhausted", state_label: "Limit reached", resets_at: "2026-11-23T14:56:00+00:00", basis: "limit",
  login: { state: "ok", words: "Signed in", tip: "" },
  at_limit: { does: "fails_over", words: "Moves work to Codex." },
  windows: [
    { kind: "five_hour", label: "5-hour", left_pct: 62, state: "plenty", state_label: "Plenty", resets_at: "2026-11-16T22:00:00+00:00", observed_at: null },
    { kind: "seven_day", label: "Weekly", left_pct: 0, state: "exhausted", state_label: "Limit reached", resets_at: "2026-11-23T14:56:00+00:00", observed_at: null },
  ],
  jobs: ["Build", "Fast tier"], freshness: { as_of: null, stale: false }, pool: null, ...over,
});

let g: FakeGovern;
beforeEach(() => {
  g = newGovern();
  vi.stubGlobal("fetch", fakeGovernApi(g));
  setConsoleLive(true);
  window.history.replaceState({}, "", "/?area=budget");
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("Govern › Subscriptions", () => {
  it("a limit reached banners every tab and the tab says what BOSS does", async () => {
    (g.view as Record<string, unknown>).subscriptions = { project_id: "p1", sources: [source()], alerts: [] };
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    expect($("[data-testid=gv-sub-banner]")!.textContent).toMatch(/Claude Code.*limit reached.*Moves work to Codex/);
    expect(g.calls.some((c) => /subscri/.test(c.url))).toBe(false);   // it rides on the one Govern read
    await click($("[data-testid=gv-sub-banner-open]"));
    expect($("[data-testid=gv-sub-state-claude_code]")!.textContent).toBe("Limit reached");
    expect($("[data-testid=gv-sub-window-claude_code-five_hour]")!.textContent).toMatch(/62% left/);
    expect($("[data-testid=gv-sub-window-claude_code-seven_day]")!.textContent).toMatch(/Limit reached.*resets/);
    expect($("[data-testid=gv-sub-jobs-claude_code]")!.textContent).toBe("Used by: Build, Fast tier");
    expect($("[data-testid=gv-sub-atlimit-claude_code]")!.textContent).toMatch(/Moves work to Codex/);
    expect($("[data-testid=gv-sub-banner]")).toBeNull();     // the tab itself needs no banner
  });

  it("a plan with plenty left shows no banner; a stale reading says how old it is", async () => {
    const old = new Date(Date.now() - 5 * 3600_000).toISOString();
    (g.view as Record<string, unknown>).subscriptions = { project_id: "p1", alerts: [], sources: [
      source({ state: "plenty", state_label: "Plenty", left_pct: 90, windows: [], freshness: { as_of: old, stale: true } })] };
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    expect($("[data-testid=gv-sub-banner]")).toBeNull();
    await click($("[data-testid=gv-tab-subscriptions]"));
    expect($("[data-testid=gv-sub-age-claude_code]")!.textContent).toBe("last reported 5 h ago");
  });

  it("a pool's accounts sit under the pool: primary marked, the one taking new work named", async () => {
    const pool = { id: "spool_1", name: "Claude Code pool", size: 2, scope: "org" };
    (g.view as Record<string, unknown>).subscriptions = { project_id: "p1", alerts: [], sources: [
      source({ key: "claude_code:pool:b", label: "Claude Code · Account 2", state: "plenty", state_label: "Plenty", left_pct: 80,
               windows: [], pool: { ...pool, position: 1, primary: false }, taking_new_work: true }),
      source({ key: "claude_code:pool:a", label: "Claude Code · Account 1", pool: { ...pool, position: 0, primary: true },
               taking_new_work: false, at_limit: { does: "next_account", words: "Moves to the next Claude Code account: Account 2." } }),
      source({ key: "codex:install", engine: "codex", label: "Codex", state: "plenty", state_label: "Plenty", left_pct: 50, windows: [] }),
    ] };
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-tab-subscriptions]"));
    const box = $("[data-testid=gv-sub-pool-claude_code]")!;
    expect(box.querySelector("h3")!.textContent).toBe("Claude Code pool");
    expect($("[data-testid=gv-sub-pool-size-claude_code]")!.textContent).toBe("2 accounts · whole org");
    // seat order inside the pool, whatever the state order outside it
    expect([...box.querySelectorAll(".gv-sub .gv-sub-name")].map((h) => h.textContent))
      .toEqual(["Claude Code · Account 1", "Claude Code · Account 2"]);
    expect($("[data-testid=gv-sub-primary-claude_code-0]")!.textContent).toBe("Primary");
    expect($("[data-testid=gv-sub-next-claude_code-1]")!.textContent).toBe("Takes new work");
    expect($("[data-testid=gv-sub-next-claude_code-0]")).toBeNull();
    expect($("[data-testid=gv-sub-atlimit-claude_code-0]")!.textContent).toMatch(/next Claude Code account: Account 2/);
    const manage = $("[data-testid=gv-sub-pool-manage-claude_code]") as HTMLAnchorElement;
    expect(manage.className).toMatch(/\bag-btn\b/);
    expect(manage.querySelector("svg")).not.toBeNull();
    expect(new URL(manage.href, "http://x").searchParams.get("area")).toBe("setup");
    // a single login stays its own card, outside any pool
    expect(box.contains($("[data-testid=gv-sub-codex]"))).toBe(false);
  });

  it("no plan connected says where to connect one", async () => {
    (g.view as Record<string, unknown>).subscriptions = { project_id: "p1", sources: [], alerts: [] };
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-tab-subscriptions]"));
    expect($("[data-testid=gv-sub-empty]")!.textContent).toMatch(/Connect Claude Code or Codex in Setup/);
  });
});

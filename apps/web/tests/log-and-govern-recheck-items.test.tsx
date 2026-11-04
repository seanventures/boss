// @vitest-environment happy-dom
/**
 * The re-check's small items:
 *  - Per step token guides: what was SET shows first and exactly (48,500, never "49k"), and
 *    "+n more" opens the full list — a saved review_code was hidden behind slice(0, 3).
 *  - `kind:` completes by prefix from the server (kind:deploy → deploy.production), past the
 *    facets' top 40 kinds.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { fakeGovernApi, governView, newGovern } from "./_helpers/govern-fixtures";
import { FACETS, fakeLogApi, newLog, rows } from "./_helpers/log-fixtures";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { GovernHome } = await import("../app/govern/govern-home");
const { LogExplorer } = await import("../app/activity/log-explorer");
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const step = (stage: string, cap: number, stored = false) => ({
  stage, max_total_tokens_per_call: cap, max_tokens_per_day: null, max_cost_usd_micros_per_day: null,
  alert_at_pct: 0.8, source: stored ? "stored" : "default", ...(stored ? { updated_at: "2026-10-04T08:00:00+00:00" } : {}) });

describe("Per step token guides", () => {
  it("show what was set first, exactly, and open the rest", async () => {
    const g = newGovern();
    const view = governView() as Record<string, unknown>;
    view.token_guides = { ...(view.token_guides as object), per_step: [
      step("standards", 32_000), step("spec", 48_000), step("plan", 48_000), step("review_plan", 32_000),
      step("write_tests", 64_000), step("implement", 96_000), step("review_code", 48_500, true)] };
    g.view = view;
    vi.stubGlobal("fetch", fakeGovernApi(g));
    setConsoleLive(true);
    window.history.replaceState({}, "", "/?area=budget&tab=board");
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    const shown = () => $$("[data-testid=gv-control-token_per_step] [data-testid^=gv-step-]").map((s) => s.textContent);
    expect(shown()).toEqual(["review code 48,500", "standards 32k", "spec 48k"]);
    expect($("[data-testid=gv-steps-more]")!.textContent).toBe("+4 more");
    await click($("[data-testid=gv-steps-more]"));
    expect(shown()).toHaveLength(7);
    expect($("[data-testid=gv-steps-more]")!.textContent).toBe("Fewer");
  });
});

describe("kind: completion", () => {
  it("asks the server by prefix and offers kinds past the facets' top 40", async () => {
    const log = newLog({ page: { items: rows(2) }, facets: { ...FACETS, kinds: [{ kind: "turn.completion", n: 900 }] } });
    const base = fakeLogApi(log);
    vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
      const url = new URL(String(input), "http://console.test");
      if (url.pathname === "/api/activity/kinds") {
        log.asked.push(`${url.pathname}?${url.searchParams.toString()}`);
        return Response.json({ kinds: [{ kind: "deploy.production", n: 2 }, { kind: "deploy.staging", n: 1 }] });
      }
      return base(input, init);
    }));
    setConsoleLive(true);
    window.history.replaceState({}, "", "/?area=activity");
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    const input = $("[data-testid=lx-query-input]") as HTMLInputElement;
    await act(async () => { input.focus(); input.dispatchEvent(new FocusEvent("focus")); });
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "kind:deploy");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { await new Promise((r) => setTimeout(r, 300)); });
    await settle();
    const asked = log.asked.filter((u) => u.startsWith("/api/activity/kinds?"));
    expect(asked.at(-1)).toContain("prefix=deploy");
    expect($$("[data-testid=lx-suggest] li").map((li) => li.textContent)[0]).toContain("deploy.production");
  });
});

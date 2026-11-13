// @vitest-environment happy-dom
/**
 * Govern › Limits › Budgets by agent or workflow (BOSSAPP-863): the card lists each scoped budget
 * with its spend, and adds one for a picked agent (or workflow) with a cap, a period and a hard stop.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ScopedBudgetsCard, describeScope, type ScopedBudget } from "../app/govern/scoped-budgets";
import { $, mount, settle, unmount } from "./_helpers/live-render";

const POLICY: ScopedBudget = { id: "sbp_1", scope_type: "role", scope_id: "backend_engineer", project_id: "p1",
  period: "month", cap_usd_micros: 50_000_000, warn_pct: 0.8, hard_stop: true, spent_usd_micros: 12_500_000 };

afterEach(() => { unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function set(el: HTMLInputElement | HTMLSelectElement, value: string) {
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
}

describe("budgets by agent or workflow", () => {
  it("names each scope in words", () => {
    expect(describeScope(POLICY, { backend_engineer: "Backend" })).toBe("Agent Backend");
    expect(describeScope({ ...POLICY, scope_type: "org" })).toBe("Whole org");
  });

  it("lists spend against the cap and adds an agent budget", async () => {
    const calls: { url: string; method: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({ url: String(url), method, body: JSON.parse(String(init?.body ?? "{}")) });
      const json = (b: unknown) => new Response(JSON.stringify(b), { status: 200, headers: { "Content-Type": "application/json" } });
      if (String(url).startsWith("/v1/budget-policies/scoped") && method === "GET") return json({ policies: [POLICY] });
      if (String(url).startsWith("/v1/roles")) return json({ items: [{ role_key: "backend_engineer", display_name: "Backend" }] });
      if (String(url).startsWith("/v1/workflows")) return json({ items: [] });
      return json({ id: "sbp_2" });
    }));
    await mount(<ScopedBudgetsCard projectId="p1" actor="ceo" />);
    await settle();
    expect($('[data-testid="gv-scoped-sbp_1"]')?.textContent).toContain("$12.50 of $50.00 / month");
    expect($('[data-testid="gv-scoped-sbp_1"]')?.textContent).toContain("Agent Backend");
    set($('[data-testid="gv-scoped-target"]') as HTMLSelectElement, "backend_engineer");
    set($('[data-testid="gv-scoped-cap"]') as HTMLInputElement, "20");
    await settle();
    ($('[data-testid="gv-scoped-save"]') as HTMLButtonElement).click();
    await settle();
    const sent = calls.find((c) => c.method === "POST" && c.url.includes("/v1/budget-policies/scoped"));
    expect(sent?.body).toMatchObject({ project_id: "p1", scope_type: "role", scope_id: "backend_engineer",
      role_project_id: "p1", cap_usd_micros: 20_000_000, period: "month", hard_stop: true });
  });
});

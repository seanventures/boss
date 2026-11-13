// @vitest-environment happy-dom
/**
 * Govern › Limits › Budgets by agent or workflow (BOSSAPP-863): the card lists each scoped budget
 * with its spend, and adds one for a picked agent (or workflow) with a cap, a period and a hard stop.
 */
import React from "react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
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

  // BOSSAPP-1036: unchecking "Hard stop" still created a hard-stop row. The uncheck itself never
  // reached React — once an error line showed, `.gv-field-error { flex-basis: 100% }` inside the
  // column-flex card claimed the card's HEIGHT, squeezed the add row to its min-height and painted
  // the error span over the checkbox and the Add button, so every real click was swallowed and the
  // box read as "re-checking itself". These two tests pin both halves of the fix.
  it("keeps an unchecked Hard stop through a refused add and sends hard_stop false", async () => {
    const posts: Record<string, unknown>[] = [];
    let refuse = true;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase();
      const json = (b: unknown, status = 200) =>
        new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } });
      if (method === "POST" && String(url).includes("/v1/budget-policies/scoped")) {
        posts.push(JSON.parse(String(init?.body ?? "{}")));
        if (refuse) { refuse = false; return json({ error: "step_up_required" }, 401); }
        return json({ id: "sbp_2" }, 201);
      }
      if (String(url).startsWith("/v1/budget-policies/scoped")) return json({ policies: [] });
      if (String(url).startsWith("/v1/roles")) return json({ items: [{ role_key: "backend_engineer", display_name: "Backend" }] });
      if (String(url).startsWith("/v1/workflows")) return json({ items: [] });
      return json({});
    }));
    await mount(<ScopedBudgetsCard projectId="p1" actor="ceo" />);
    await settle();
    set($('[data-testid="gv-scoped-target"]') as HTMLSelectElement, "backend_engineer");
    set($('[data-testid="gv-scoped-cap"]') as HTMLInputElement, "20");
    ($('[data-testid="gv-scoped-hard"]') as HTMLInputElement).click();
    await settle();
    expect(($('[data-testid="gv-scoped-hard"]') as HTMLInputElement).checked).toBe(false);
    ($('[data-testid="gv-scoped-save"]') as HTMLButtonElement).click();
    await settle();
    // The refusal shows, and the user's uncheck is NOT silently restored to hard-stop.
    expect($('[data-testid="gv-scoped-error"]')).toBeTruthy();
    expect(($('[data-testid="gv-scoped-hard"]') as HTMLInputElement).checked).toBe(false);
    ($('[data-testid="gv-scoped-save"]') as HTMLButtonElement).click();
    await settle();
    expect(posts).toHaveLength(2);
    for (const body of posts) expect(body.hard_stop).toBe(false);
  });

  it("the error line never claims the card's column axis (it covered the checkbox)", () => {
    const css = readFileSync(resolve(__dirname, "../app/govern/govern.css"), "utf8");
    const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
      .map((m) => ({ selector: m[1].trim(), body: m[2] }))
      .filter((r) => r.selector.split(",").some((s) => s.trim().endsWith(".gv-field-error")));
    expect(rules.length).toBeGreaterThan(0);
    for (const rule of rules) {
      if (!/flex-basis\s*:\s*(100%|[1-9])/.test(rule.body)) continue;
      // A full-line basis is only safe where the MAIN axis is a row: the row-wrap edit form.
      for (const s of rule.selector.split(",")) {
        if (!s.trim().endsWith(".gv-field-error")) continue;
        expect(s.trim(), `"${s.trim()}" gives the error line a main-axis basis; inside a column `
          + "flex container that is the card's HEIGHT, and the error overlays the controls").toMatch(
          /^\.gv-edit-form\s+/);
      }
    }
  });
});

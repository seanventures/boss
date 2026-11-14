// @vitest-environment happy-dom
/**
 * IMPORT WORK FROM A TICKET SYSTEM (BOSSAPP-1047 T8): projects (mapped first, a filter) → the
 * epic tree with children and "Not in an epic" → ticks → ONE Import to Work. An epic comes with
 * its children; a ticket already in BOSS cannot be ticked; Open only / type / search reach the
 * API; after the import the page closes and Ready to start has the imported items.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TicketImportPage } from "../app/projects/ticket-import";
import type { PxCtx } from "../app/projects/project-page";
import { parseConnection, type Read, type Connection } from "../lib/tickets";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const CONN = parseConnection({
  id: "wtp_1", project_id: "p1", provider: "jira", label: "Jira", status: "connected", base_url: "https://northpeak.atlassian.net",
  capabilities: { browse: true }, settings: { mappings: [{ external_project: "LEND", boss_project_id: "p1", default: true }] },
});
const conns = (data: Connection[]): Read<Connection[]> => ({ ok: true, data });
const ctx = (refreshWork = vi.fn()) => ({ projectId: "p1", demo: false, refreshWork, overview: { project: { name: "NorthPeak Lending" } } } as unknown as PxCtx);
const tk = (key: string, type: string, extra: Record<string, unknown> = {}) =>
  ({ provider: "jira", key, title: `${key} title`, type, status: "To Do", status_category: "todo", in_boss: null, ...extra });

type Call = { url: string; method: string; body: Record<string, unknown> };
function stub() {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url), method = String(init?.method ?? "GET");
    calls.push({ url: u, method, body: init?.body ? JSON.parse(String(init.body)) : {} });
    if (method === "POST") return json({ imported: [{ key: "LEND-120", work_item_id: "wi_e", parent_work_item_id: null },
                                                   { key: "LEND-142", work_item_id: "wi_c", parent_work_item_id: "wi_e" },
                                                   { key: "LEND-149", work_item_id: "wi_b", parent_work_item_id: null }], already_in_boss: [] });
    if (u.endsWith("/projects")) return json({ projects: [{ key: "CARD", name: "Cards" }, { key: "LEND", name: "Lending", epic_count: 1, open_count: 4 }] });
    if (u.includes("/epics")) return json({ tickets: [tk("LEND-120", "epic", { children_count: 2 })] });
    if (u.includes("/unparented")) return json({ tickets: [tk("LEND-149", "bug"), tk("LEND-150", "story", { status_category: "done" }),
                                                           tk("LEND-138", "story", { in_boss: { work_item_id: "wi_x", run_id: "run_x" } })] });
    if (u.includes("/children")) return json({ tickets: [tk("LEND-142", "story", { parent_key: "LEND-120" })] });
    if (u.includes("/search")) return json({ tickets: [tk("LEND-151", "story")] });
    return json({});
  }));
  return calls;
}
const click = (sel: string) => { ($(sel) as HTMLElement).click(); };
const setValue = (sel: string, v: string) => {
  const el = $(sel) as HTMLInputElement | HTMLSelectElement;
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, v);
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
};

afterEach(() => { unmount(); vi.unstubAllGlobals(); try { window.sessionStorage.clear(); } catch { /* none */ } window.history.replaceState(null, "", "/"); });

describe("Import page", () => {
  it("browses, picks an epic with its children and a bug, and imports them once", async () => {
    window.history.replaceState(null, "", "/?tickets=import");
    const refresh = vi.fn();
    const calls = stub();
    await mount(<TicketImportPage ctx={ctx(refresh)} conns={conns([CONN])} />);
    expect($("h1")?.textContent).toBe("Import work from Jira");
    // Mapped project first, and picked.
    expect($$('[role="option"]').map((b) => b.getAttribute("data-testid"))).toEqual(["tk-imp-proj-LEND", "tk-imp-proj-CARD"]);
    expect($('[data-testid="tk-imp-proj-CARD"]')?.textContent).toContain("not mapped to a BOSS project");
    expect($('[data-testid="tk-imp-proj-LEND"]')?.getAttribute("aria-selected")).toBe("true");
    // Filter projects.
    setValue('[data-testid="tk-imp-filter"]', "card");
    await settle();
    expect($('[data-testid="tk-imp-proj-LEND"]')).toBeNull();
    setValue('[data-testid="tk-imp-filter"]', "");
    await settle();
    // Open only hides the done ticket; already in BOSS is disabled with its link.
    expect($('[data-testid="tk-imp-row-LEND-150"]')).toBeNull();
    click('[data-testid="tk-imp-open-only"]');
    await settle();
    expect($('[data-testid="tk-imp-row-LEND-150"]')).not.toBeNull();
    expect(($('[data-testid="tk-imp-pick-LEND-138"]') as HTMLInputElement).disabled).toBe(true);
    // Type filter.
    setValue('[data-testid="tk-imp-type"]', "bug");
    await settle();
    expect($('[data-testid="tk-imp-row-LEND-150"]')).toBeNull();
    expect($('[data-testid="tk-imp-row-LEND-149"]')).not.toBeNull();
    setValue('[data-testid="tk-imp-type"]', "");
    await settle();
    // Expand the epic: its children are read; picking the epic brings them.
    click('[data-testid="tk-imp-expand-LEND-120"]');
    await settle();
    expect(calls.some((c) => c.url === "/v1/ticket-systems/wtp_1/tickets/LEND-120/children")).toBe(true);
    click('[data-testid="tk-imp-pick-LEND-120"]');
    await settle();
    expect(($('[data-testid="tk-imp-pick-LEND-142"]') as HTMLInputElement).checked).toBe(true);
    expect(($('[data-testid="tk-imp-pick-LEND-142"]') as HTMLInputElement).disabled).toBe(true);
    click('[data-testid="tk-imp-pick-LEND-149"]');
    await settle();
    expect($('[data-testid="tk-imp-summary"] h2')?.textContent).toBe("Import 1 epic + 1 ticket");
    expect($('[data-testid="tk-imp-sum-LEND-120"]')?.textContent).toContain("Major feature");
    expect($('[data-testid="tk-imp-sum-LEND-149"]')?.textContent).toContain("Bug → Bug fix");
    click('[data-testid="tk-imp-go"]');
    await settle();
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0].url).toBe("/v1/projects/p1/tickets/import");
    expect(posts[0].body).toEqual({ connection_id: "wtp_1", items: [{ key: "LEND-120", with_children: true }, { key: "LEND-149", with_children: false }] });
    expect(new URLSearchParams(window.location.search).get("tickets")).toBeNull();
    // Nothing is kept in the browser: the Work tab re-reads the list, which carries the imports.
    expect(Object.keys(window.sessionStorage).filter((k) => k.includes("tickets"))).toEqual([]);
    expect(refresh).toHaveBeenCalled();
  });

  it("searches with the picked project and filters, and goes back", async () => {
    const calls = stub();
    await mount(<TicketImportPage ctx={ctx()} conns={conns([CONN])} />);
    setValue('[data-testid="tk-imp-q"]', "remaining term");
    click('[data-testid="tk-imp-search"]');
    await settle();
    const s = calls.find((c) => c.url.includes("/search"))!;
    expect(s.url).toBe("/v1/ticket-systems/wtp_1/search?q=remaining+term&project=LEND&status=open");
    expect($('[data-testid="tk-imp-row-LEND-151"]')).not.toBeNull();
    click('[data-testid="tk-imp-clear"]');
    await settle();
    expect($('[data-testid="tk-imp-row-LEND-151"]')).toBeNull();
  });

  it("In BOSS opens the work item; the breadcrumb closes the page", async () => {
    stub();
    window.history.replaceState(null, "", "/?tickets=import");
    await mount(<TicketImportPage ctx={ctx()} conns={conns([CONN])} />);
    click('[data-testid="tk-imp-inboss-LEND-138"]');
    const sp = new URLSearchParams(window.location.search);
    expect(sp.get("tickets")).toBeNull();
    expect(sp.get("drawer")).toBe("work:run_x");
    window.history.replaceState(null, "", "/?tickets=import");
    click('[data-testid="tk-imp-back"]');
    expect(new URLSearchParams(window.location.search).get("tickets")).toBeNull();
  });

  it("says an import refusal in words, and offers Setup when nothing is connected", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") return json({ detail: { error: "auth_failed", message: "Jira refused BOSS's token." } }, 424);
      if (String(url).endsWith("/projects")) return json({ projects: [{ key: "LEND", name: "Lending" }] });
      if (String(url).includes("/unparented")) return json({ tickets: [tk("LEND-149", "bug")] });
      return json({ tickets: [] });
    }));
    await mount(<TicketImportPage ctx={ctx()} conns={conns([CONN])} />);
    click('[data-testid="tk-imp-pick-LEND-149"]');
    await settle();
    click('[data-testid="tk-imp-go"]');
    await settle();
    expect($('[data-testid="tk-imp-err"]')?.textContent).toBe("Jira refused BOSS's token.");
    unmount();
    await mount(<TicketImportPage ctx={ctx()} conns={conns([])} />);
    expect($('[data-testid="tk-imp-none-setup"]')?.getAttribute("href")).toBe("?area=setup&focus=ticket_systems");
  });

  it("a denied projects read says so; the connection picker switches systems", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      calls.push(String(url));
      return String(url).includes("wtp_2") ? json({ projects: [] }) : json({ detail: "Needs “View integrations”." }, 403);
    }));
    const plane = { ...CONN, id: "wtp_2", provider: "plane", label: "Plane", base_url: "https://plane.example" } as Connection;
    await mount(<TicketImportPage ctx={ctx()} conns={conns([CONN, plane])} />);
    expect($('[data-testid="tk-imp-projects-denied"]')?.textContent).toContain("View integrations");
    setValue('[data-testid="tk-imp-conn"]', "wtp_2");
    await settle();
    expect(calls).toContain("/v1/ticket-systems/wtp_2/projects");
    expect($("h1")?.textContent).toBe("Import work from Plane");
  });
});

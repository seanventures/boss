// @vitest-environment happy-dom
/**
 * Data page for a built app (BOSSAPP-1266): tables, a filter, a CSV import,
 * and a 30-day trash. Filter and import both go to the server.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppDataScreen } from "../app/app-data/screen";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const PAGE = {
  app_key: "recipe box",
  trash_days: 30,
  tables: [{ name: "Customers", live_count: 2 }],
  table: "Customers",
  columns: ["name", "city"],
  rows: [
    { id: "adr_ada", cells: { name: "Ada Lovelace", city: "London" } },
    { id: "adr_grace", cells: { name: "Grace Hopper", city: "Paris" } },
  ],
  trash: [] as Array<Record<string, unknown>>,
};

type Call = { method: string; path: string; body: unknown };
let calls: Call[] = [];
let page = structuredClone(PAGE);

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

beforeEach(() => {
  calls = [];
  page = structuredClone(PAGE);
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const path = url.replace(/^https?:\/\/[^/]+/, "");
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, path, body });
    if (method === "GET" && path.includes("/data")) return json(page);
    if (method === "POST" && path.endsWith("/import")) {
      page = {
        ...page,
        rows: [...page.rows, { id: "adr_edsger", cells: { name: "Edsger Dijkstra", city: "Amsterdam" } }],
      };
      return json({ imported: 1, table: body.table, rows: [page.rows[page.rows.length - 1]] });
    }
    if (method === "POST" && path.endsWith("/trash")) {
      const id = path.split("/").slice(-2)[0];
      const row = page.rows.find((item) => item.id === id);
      page = {
        ...page,
        rows: page.rows.filter((item) => item.id !== id),
        trash: row ? [...page.trash, { ...row, table: "Customers", days_left: 12, restorable: true }] : page.trash,
      };
      return json({ id, deleted_at: "2026-06-01T12:00:00+00:00" });
    }
    if (method === "POST" && path.endsWith("/restore")) {
      const id = path.split("/").slice(-2)[0];
      const row = page.trash.find((item) => item.id === id) as { id: string; cells: { name: string; city: string } } | undefined;
      page = {
        ...page,
        trash: page.trash.filter((item) => item.id !== id),
        rows: row ? [...page.rows, { id: row.id, cells: row.cells }] : page.rows,
      };
      return json({ id, deleted_at: null });
    }
    return json({ detail: "not stubbed" }, 404);
  }));
});

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 8; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

async function mount() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<AppDataScreen projectId="prj_1" appKey="recipe box" appName="Recipe Box" />);
  });
  await settle();
  return host!;
}

const $ = (sel: string) => host!.querySelector(sel) as HTMLElement;
const text = () => host!.textContent ?? "";

describe("App data", () => {
  it("filters rows through the server", async () => {
    await mount();
    expect(text()).toContain("Ada Lovelace");
    expect(text()).toContain("Grace Hopper");
    const field = $('[data-testid="filter-field"]') as HTMLInputElement;
    const value = $('[data-testid="filter-value"]') as HTMLInputElement;
    const mode = $('[data-testid="filter-mode"]') as HTMLSelectElement;
    await act(async () => {
      field.value = "city";
      field.dispatchEvent(new Event("input", { bubbles: true }));
      value.value = "London";
      value.dispatchEvent(new Event("input", { bubbles: true }));
      mode.value = "is";
      mode.dispatchEvent(new Event("change", { bubbles: true }));
    });
    page = { ...page, rows: [PAGE.rows[0]] };
    await act(async () => { $('[data-testid="apply-filter"]').click(); });
    await settle();
    const sent = calls.filter((call) => call.method === "GET" && call.path.includes("field=city"));
    expect(sent.length).toBeGreaterThan(0);
    expect(sent.at(-1)?.path).toContain("value=London");
    expect(sent.at(-1)?.path).toContain("mode=is");
    expect(text()).toContain("Ada Lovelace");
    expect(text()).not.toContain("Grace Hopper");
  });

  it("imports a CSV and shows the new row", async () => {
    await mount();
    const table = $('[data-testid="import-table"]') as HTMLInputElement;
    const csv = $('[data-testid="import-text"]') as HTMLTextAreaElement;
    await act(async () => {
      table.value = "Customers";
      table.dispatchEvent(new Event("input", { bubbles: true }));
      csv.value = "name,city\nEdsger Dijkstra,Amsterdam\n";
      csv.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { $('[data-testid="import-submit"]').click(); });
    await settle();
    const sent = calls.find((call) => call.method === "POST" && call.path.endsWith("/import"));
    expect(sent?.body).toMatchObject({
      project_id: "prj_1",
      table: "Customers",
      csv: "name,city\nEdsger Dijkstra,Amsterdam\n",
    });
    expect(sent?.path).toContain("/v1/apps/recipe%20box/data/import");
    expect(text()).toContain("Edsger Dijkstra");
  });

  it("moves a row to the trash and restores it inside 30 days", async () => {
    await mount();
    await act(async () => { $('[data-testid="trash-adr_ada"]').click(); });
    await settle();
    const trashed = calls.find((call) => call.method === "POST" && call.path.endsWith("/trash"));
    expect(trashed?.path).toContain("/rows/adr_ada/trash");
    expect(trashed?.body).toMatchObject({ project_id: "prj_1" });
    await act(async () => { $('[data-testid="trash-tab"]').click(); });
    await settle();
    expect(text()).toContain("Ada Lovelace");
    expect(text()).toContain("12 days");
    const rule = [...host!.querySelectorAll("p")].find((node) => node.textContent?.includes("30 days"));
    expect(rule).toBeTruthy();
    await act(async () => { $('[data-testid="restore-adr_ada"]').click(); });
    await settle();
    const restored = calls.find((call) => call.method === "POST" && call.path.endsWith("/restore"));
    expect(restored?.path).toContain("/rows/adr_ada/restore");
    expect(text()).toContain("Ada Lovelace");
  });
});

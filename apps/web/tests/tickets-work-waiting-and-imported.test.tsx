// @vitest-environment happy-dom
/**
 * TICKETS ON THE WORK TAB (BOSSAPP-1047 T5, T9): Import from Jira opens the Import page; Waiting
 * for BOSS lists the labelled tickets with one Start (import, then start on the type's workflow),
 * Plan first for an epic; a row named after a ticket shows its chip with the sync state; after an
 * import, Ready to start offers the epic's Start major feature and each single's picked workflow.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ImportAction, ImportedSection, RowChipsProvider, RowTicket, WaitingSection, connectionForKey, ticketKeyOf, useRowChips, writeImported,
} from "../app/projects/ticket-work";
import type { PxCtx } from "../app/projects/project-page";
import { parseConnection, type Connection } from "../lib/tickets";
import type { WorkItem } from "../lib/project-experience";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const CONN = parseConnection({
  id: "wtp_1", project_id: "p1", provider: "jira", label: "Jira", status: "connected", base_url: "https://northpeak.atlassian.net",
  capabilities: { read: true, browse: true },
  settings: { mappings: [{ external_project: "LEND", boss_project_id: "p1", default: true }], pickup: { label: "boss", mode: "ask" },
              inbound: { mode: "webhook", poll_minutes: 5 }, project_keys: ["LEND"] },
});
const ctx = (refreshWork = vi.fn()) => ({ projectId: "p1", demo: false, refreshWork, overview: { project: { name: "NorthPeak Lending" } } } as unknown as PxCtx);
const tk = (key: string, type: string, extra: Record<string, unknown> = {}) =>
  ({ provider: "jira", connection_id: "wtp_1", key, title: `${key} title`, type, status: "To Do", status_category: "todo", priority: "High", in_boss: null, ...extra });

type Call = { url: string; method: string; body: Record<string, unknown> };
function stub(route: (url: string, method: string) => Response | null) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = String(init?.method ?? "GET");
    calls.push({ url: String(url), method, body: init?.body ? JSON.parse(String(init.body)) : {} });
    return route(String(url), method) ?? json({});
  }));
  return calls;
}
const click = (sel: string) => { ($(sel) as HTMLElement).click(); };

afterEach(() => { unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals(); try { window.sessionStorage.clear(); } catch { /* none */ } window.history.replaceState(null, "", "/"); });

describe("ticket keys on rows", () => {
  it("reads a key from a row label and finds its connection", () => {
    expect(ticketKeyOf("LEND-142 Second reviewer")).toBe("LEND-142");
    expect(ticketKeyOf("Fix the login")).toBe("");
    expect(connectionForKey("LEND-1", [CONN])?.id).toBe("wtp_1");
    expect(connectionForKey("OPS-1", [CONN])).toBeNull();
  });
});

describe("Import from Jira", () => {
  it("opens the Import page through the URL", async () => {
    await mount(<ImportAction ctx={ctx()} conns={[CONN]} />);
    expect($('[data-testid="tk-import-open"]')?.textContent).toContain("Import from Jira");
    click('[data-testid="tk-import-open"]');
    expect(new URLSearchParams(window.location.search).get("tickets")).toBe("import");
  });
  it("is not offered without a connection that can browse", async () => {
    await mount(<ImportAction ctx={ctx()} conns={[{ ...CONN, capabilities: { browse: false } } as Connection]} />);
    expect($('[data-testid="tk-import-open"]')).toBeNull();
  });
});

describe("Waiting for BOSS", () => {
  it("lists labelled tickets; Start imports then starts on the type's workflow; an epic is Plan first", async () => {
    const refresh = vi.fn();
    const calls = stub((url, method) => {
      if (url.endsWith("/tickets/waiting")) return json({ tickets: [tk("LEND-151", "story"), tk("LEND-120", "epic")], errors: [{ connection_id: "wtp_1", error: "rate_limited", message: "" }] });
      if (method === "POST" && url.endsWith("/tickets/import")) return json({ imported: [{ key: "LEND-151", work_item_id: "wi_151", parent_work_item_id: null }], already_in_boss: [] });
      if (method === "POST" && url.includes("/start-from-ticket")) return json({ run_id: "run_1", workflow_key: "minor_feature" });
      return null;
    });
    await mount(<WaitingSection ctx={ctx(refresh)} conns={[CONN]} />);
    expect($('[data-testid="tk-waiting"] h2')?.textContent).toContain("Jira LEND, label “boss” · 2");
    expect($('[data-testid="tk-waiting-err"]')?.textContent).toContain("slow down");
    expect($('[data-testid="tk-waiting-start-LEND-120"]')?.textContent).toBe("Plan first");
    expect($('[data-testid="tk-waiting-rule"]')?.getAttribute("href")).toBe("?area=setup&focus=ticket_systems");
    click('[data-testid="tk-waiting-start-LEND-151"]');
    await settle();
    const imp = calls.find((c) => c.url === "/v1/projects/p1/tickets/import")!;
    expect(imp.body).toEqual({ connection_id: "wtp_1", items: [{ key: "LEND-151", with_children: false }] });
    const st = calls.find((c) => c.url === "/v1/work-items/wi_151/start-from-ticket")!;
    expect(st.body).toEqual({ project_id: "p1", workflow_key: "minor_feature" });
    expect($('[data-testid="tk-waiting-said"]')?.textContent).toContain("Started LEND-151 as minor feature");
    expect(refresh).toHaveBeenCalled();
  });

  it("says a refusal in words", async () => {
    stub((url, method) => {
      if (url.endsWith("/tickets/waiting")) return json({ tickets: [tk("LEND-149", "bug")], errors: [] });
      if (method === "POST") return json({ detail: { error: "auth_failed", message: "Jira refused the token." } }, 424);
      return null;
    });
    await mount(<WaitingSection ctx={ctx()} conns={[CONN]} />);
    click('[data-testid="tk-waiting-start-LEND-149"]');
    await settle();
    expect($('[data-testid="tk-waiting-said"]')?.textContent).toBe("Jira refused the token.");
  });

  it("shows nothing when nothing waits, and a denied read in words", async () => {
    stub(() => json({ tickets: [], errors: [] }));
    await mount(<WaitingSection ctx={ctx()} conns={[CONN]} />);
    expect($('[data-testid="tk-waiting"]')).toBeNull();
    unmount();
    stub(() => json({ detail: "Needs “View integrations”." }, 403));
    await mount(<WaitingSection ctx={ctx()} conns={[CONN]} />);
    expect($('[data-testid="tk-waiting-denied"]')?.textContent).toContain("View integrations");
  });
});

describe("row ticket chips", () => {
  it("reads each ticket row's status and sync state, at most once per row", async () => {
    const calls = stub((url) => (url.includes("/tickets/LEND-142")
      ? json({ ticket: tk("LEND-142", "story", { status: "In Progress", url: "https://j/LEND-142", in_boss: { work_item_id: "wi", sync_status: "synced" } }) }) : null));
    const items = [{ id: "run_1", label: "LEND-142 Second reviewer", state: "running" }, { id: "run_2", label: "Plain work", state: "running" }] as WorkItem[];
    function Rows() {
      const chips = useRowChips(items, [CONN]);
      return <RowChipsProvider chips={chips}>{items.map((w) => <RowTicket key={w.id} w={w} />)}</RowChipsProvider>;
    }
    await mount(<Rows />);
    expect($('[data-testid="tk-row-chip-run_1"]')?.textContent).toContain("Jira: In Progress ✓");
    expect($('[data-testid="tk-row-chip-run_2"]')).toBeNull();
    expect(calls.filter((c) => c.url.includes("/tickets/"))).toHaveLength(1);
  });
});

describe("Ready to start (after an import)", () => {
  it("starts the epic as a major feature and a single on the workflow picked", async () => {
    writeImported("p1", { at: 1, connection_id: "wtp_1", label: "Jira", projects: ["LEND"], items: [
      { key: "LEND-120", work_item_id: "wi_e", parent_work_item_id: null },
      { key: "LEND-142", work_item_id: "wi_c", parent_work_item_id: "wi_e" },
      { key: "LEND-149", work_item_id: "wi_b", parent_work_item_id: null },
    ] });
    const calls = stub((url, method) => {
      if (method === "GET") {
        const key = /tickets\/([A-Z]+-\d+)/.exec(url)?.[1] ?? "";
        return json({ ticket: tk(key, key === "LEND-120" ? "epic" : key === "LEND-149" ? "bug" : "story") });
      }
      return json({ run_id: "r" });
    });
    await mount(<ImportedSection ctx={ctx()} />);
    expect($('[data-testid="tk-imported-banner"]')?.textContent).toContain("Imported 1 epic and 1 ticket from Jira LEND");
    expect($('[data-testid="tk-imported-LEND-120"]')?.textContent).toContain("LEND-142");
    expect($('[data-testid="tk-imported-LEND-142"]')).toBeNull();
    expect(($('[data-testid="tk-imported-wf-LEND-149"]') as HTMLSelectElement).value).toBe("fast_fix");

    click('[data-testid="tk-imported-start-LEND-120"]');
    await settle();
    expect(calls.find((c) => c.url === "/v1/work-items/wi_e/start-from-ticket")?.body).toEqual({ project_id: "p1", workflow_key: "major_feature" });
    expect($('[data-testid="tk-imported-LEND-120"]')).toBeNull();

    const pick = $('[data-testid="tk-imported-wf-LEND-149"]') as HTMLSelectElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!;
    setter.call(pick, "minor_feature");
    pick.dispatchEvent(new Event("change", { bubbles: true }));
    await settle();
    click('[data-testid="tk-imported-start-LEND-149"]');
    await settle();
    expect(calls.find((c) => c.url === "/v1/work-items/wi_b/start-from-ticket")?.body).toEqual({ project_id: "p1", workflow_key: "minor_feature" });
    expect($('[data-testid="tk-imported"]')).toBeNull();
  });

  it("Dismiss clears the cards", async () => {
    writeImported("p1", { at: 1, connection_id: "wtp_1", label: "Jira", projects: [], items: [{ key: "LEND-9", work_item_id: "w9", parent_work_item_id: null }] });
    stub(() => json({ ticket: tk("LEND-9", "task") }));
    await mount(<ImportedSection ctx={ctx()} />);
    expect($$('[data-testid^="tk-imported-start-"]')).toHaveLength(1);
    click('[data-testid="tk-imported-dismiss"]');
    await settle();
    expect($('[data-testid="tk-imported"]')).toBeNull();
  });
});

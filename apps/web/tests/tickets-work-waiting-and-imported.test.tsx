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
  ImportAction, ImportedSection, RowChipsProvider, RowTicket, WaitingSection, connectionForKey, ticketKeyOf, useRowChips,
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
  it("come from the work list's own ticket field: no ticket read per row", async () => {
    const calls = stub(() => null);
    const items = [
      { id: "run_1", label: "LEND-142 Second reviewer", state: "running",
        ticket: { provider: "jira", key: "LEND-142", url: "https://j/LEND-142", status: "In Progress", status_category: "doing", sync_status: "synced", connection_id: "wtp_1" } },
      { id: "run_3", label: "LEND-150 Broken write", state: "running",
        ticket: { provider: "jira", key: "LEND-150", url: "", status: "To Do", status_category: "todo", sync_status: "failed", connection_id: "wtp_1" } },
      { id: "run_2", label: "Plain work", state: "running", ticket: null },
    ] as WorkItem[];
    function Rows() {
      const chips = useRowChips(items, [CONN]);
      return <RowChipsProvider chips={chips}>{items.map((w) => <RowTicket key={w.id} w={w} />)}</RowChipsProvider>;
    }
    await mount(<Rows />);
    expect($('[data-testid="tk-row-chip-run_1"]')?.textContent).toContain("Jira: In Progress ✓");
    expect($('[data-testid="tk-row-chip-run_1"]')?.getAttribute("href")).toBe("https://j/LEND-142");
    expect($('[data-testid="tk-row-chip-run_3"]')?.textContent).toContain("write-back failed");
    expect($('[data-testid="tk-row-chip-run_2"]')).toBeNull();
    expect(calls).toHaveLength(0);
  });
});

const ready = (id: string, key: string, type: string, parent: string | null = null) => ({
  id, work_item_id: id, parent_work_item_id: parent, origin: "ticket_import", ticket_type: type, state: "planned",
  label: `${key} Title of ${key}`,
  ticket: { provider: "jira", key, url: `https://j/${key}`, status: "To Do", status_category: "todo", sync_status: "synced", connection_id: "wtp_1" },
}) as unknown as WorkItem;
const withWork = (items: WorkItem[], refreshWork = vi.fn()) =>
  ({ ...ctx(refreshWork), work: { items } } as unknown as PxCtx);

describe("Ready to start (from the work list, origin ticket_import)", () => {
  it("draws the epic with its tickets and the singles, and starts each on its workflow", async () => {
    const calls = stub(() => json({ run_id: "r" }));
    const refresh = vi.fn();
    await mount(<ImportedSection ctx={withWork([ready("wi_e", "LEND-120", "epic"), ready("wi_c", "LEND-142", "story", "wi_e"), ready("wi_b", "LEND-149", "bug")], refresh)} />);
    expect($('[data-testid="tk-imported-banner"]')?.textContent).toContain("1 epic and 1 ticket imported");
    expect($('[data-testid="tk-imported-LEND-120"]')?.textContent).toContain("LEND-142");
    expect($('[data-testid="tk-imported-LEND-120"]')?.textContent).toContain("Title of LEND-142");
    expect($('[data-testid="tk-imported-LEND-142"]')).toBeNull();
    expect(($('[data-testid="tk-imported-wf-LEND-149"]') as HTMLSelectElement).value).toBe("fast_fix");

    click('[data-testid="tk-imported-start-LEND-120"]');
    await settle();
    expect(calls.find((c) => c.url === "/v1/work-items/wi_e/start-from-ticket")?.body).toEqual({ project_id: "p1", workflow_key: "major_feature" });
    expect(refresh).toHaveBeenCalled();

    const pick = $('[data-testid="tk-imported-wf-LEND-149"]') as HTMLSelectElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!;
    setter.call(pick, "minor_feature");
    pick.dispatchEvent(new Event("change", { bubbles: true }));
    await settle();
    click('[data-testid="tk-imported-start-LEND-149"]');
    await settle();
    expect(calls.find((c) => c.url === "/v1/work-items/wi_b/start-from-ticket")?.body).toEqual({ project_id: "p1", workflow_key: "minor_feature" });
    // Never browser storage: the cards are the API's rows.
    expect(Object.keys(window.sessionStorage).filter((k) => k.includes("tickets"))).toEqual([]);
  });

  it("shows nothing without imported rows, and a refused start says why", async () => {
    stub(() => null);
    await mount(<ImportedSection ctx={withWork([])} />);
    expect($('[data-testid="tk-imported"]')).toBeNull();
    unmount();
    stub(() => json({ detail: { error: "conflict", message: "This work item did not come from a ticket." } }, 409));
    await mount(<ImportedSection ctx={withWork([ready("wi_9", "LEND-9", "task")])} />);
    expect($$('[data-testid^="tk-imported-start-"]')).toHaveLength(1);
    click('[data-testid="tk-imported-start-LEND-9"]');
    await settle();
    expect($('[data-testid="tk-imported-said"]')?.textContent).toBe("This work item did not come from a ticket.");
  });
});

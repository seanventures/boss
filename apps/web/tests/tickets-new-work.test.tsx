// @vitest-environment happy-dom
/**
 * T3 — NEW WORK FROM A TICKET (BOSSAPP-1047 Phase C): a key in the words is resolved, its brief
 * card shows with the workflow its type picks, Start imports it and starts it from the ticket
 * (one press), a ticket BOSS already has starts on its work item, a refusal is said in words, and
 * "Browse tickets" opens the import view on the Work tab.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { ProjectPage } from "../app/projects/project-page";
import { hasTicketRef } from "../app/projects/new-work-ticket";
import {
  FX_DEPLOYMENTS, FX_HEALTH, FX_KNOWLEDGE, FX_ONBOARDING, FX_OVERVIEW, FX_SETTINGS, FX_WORK, FX_WORK_PLAN,
} from "../lib/project-experience-fixtures";
import { choose, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "helpdesk-ai-demo";
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const type = async (el: Element | null, value: string) => {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(el, value);
    (el as HTMLElement).dispatchEvent(new Event("input", { bubbles: true }));
  });
};
const pause = async (ms = 700) => { await act(async () => { await new Promise((r) => setTimeout(r, ms)); }); await settle(); };

const TICKET = {
  provider: "jira", connection_id: "tc_1", key: "LEND-142", id: "10042", url: "https://np.atlassian.net/browse/LEND-142",
  title: "Second reviewer when debt-to-income is over 45%", type: "story", status: "To Do", status_category: "todo", priority: "High",
  labels: [], parent_key: "LEND-120", screen: { verdict: "clean", reasons: [] }, untrusted: false, in_boss: null,
  body: "Route DTI > 45% to a second reviewer.", acceptance_criteria: ["A", "B", "C"], comments: [], attachments: [],
};

function engine(resolve: () => { status?: number; body: unknown }) {
  return stubFetch((c: Call) => {
    if (c.path === "/v1/tickets/resolve") return resolve();
    if (c.path === `/v1/projects/${P}/tickets/import`) return { body: { imported: [{ key: "LEND-142", work_item_id: "wi_9" }], already_in_boss: [] } };
    if (/^\/v1\/work-items\/[^/]+\/start-from-ticket$/.test(c.path)) return { body: { workflow_key: "x", run_id: "wfr_9" } };
    if (c.method === "POST" && c.path === `/v1/projects/${P}/work/understand`) return { body: { status: "empty" } };
    if (c.method === "GET" && c.path === `/v1/projects/${P}/work/plan`) return { body: FX_WORK_PLAN };
    if (c.method !== "GET") return { body: { ok: true, run_id: "wfr_new" } };
    const m = c.path.match(/^\/v1\/projects\/([^/]+)\/(overview|work|knowledge|deployments|onboarding|settings|health|captain)$/);
    if (m) {
      const table: Record<string, unknown> = { overview: FX_OVERVIEW, work: FX_WORK, knowledge: FX_KNOWLEDGE, deployments: FX_DEPLOYMENTS,
                                                onboarding: { ...FX_ONBOARDING, state: "ready" }, settings: FX_SETTINGS, health: FX_HEALTH };
      return { body: table[m[2]] };
    }
    return undefined;
  });
}

async function openModal() {
  window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project&tab=work`);
  await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, "owner");
  await settle(10);
  await press(tid("px-head-new-work"));
}
const writes = (calls: Call[]) => calls.filter((c) => c.method !== "GET" && !c.path.endsWith("/work/understand"));

beforeEach(() => { try { window.localStorage.clear(); } catch { /* none */ } });
afterEach(() => { unmount(); });

describe("T3 · New work from a ticket", () => {
  it("recognises keys, links and #numbers, not plain words", () => {
    expect(hasTicketRef("LEND-142 keep the email")).toBe(true);
    expect(hasTicketRef("see https://np.atlassian.net/browse/LEND-142")).toBe(true);
    expect(hasTicketRef("fix #311")).toBe(true);
    expect(hasTicketRef("Add a CSV export")).toBe(false);
  });

  it("shows the ticket's card with the workflow its type picks; Start imports and starts it, once", async () => {
    const calls = engine(() => ({ body: { tickets: [TICKET], errors: [], refs: [{}] } }));
    await openModal();
    await type(tid("px-compose-text"), "LEND-142 — keep the existing approval email unchanged.");
    await pause();
    expect(tid("px-nw-ticket")!.textContent).toContain("Second reviewer");
    expect(tid("px-nw-ticket-updates")!.textContent).toContain("Updates LEND-142");
    expect((tid("px-nw-ticket-workflow") as HTMLSelectElement).value).toBe("minor_feature");
    await choose(tid("px-nw-ticket-workflow"), "major_feature");
    await press(tid("px-compose-start"));
    expect(writes(calls).map((c) => [c.path, c.body])).toEqual([
      [`/v1/projects/${P}/tickets/import`, { connection_id: "tc_1", items: [{ key: "LEND-142", with_children: false }] }],
      ["/v1/work-items/wi_9/start-from-ticket", { project_id: P, workflow_key: "major_feature" }],
    ]);
    expect(tid("px-composer")).toBeNull();
  });

  it("a ticket BOSS already has starts on its work item, with no import", async () => {
    const calls = engine(() => ({ body: { tickets: [{ ...TICKET, type: "bug", in_boss: { work_item_id: "wi_old", project_id: P } }], errors: [] } }));
    await openModal();
    await type(tid("px-compose-text"), "Fix LEND-142 please");
    await pause();
    expect(tid("px-nw-ticket-in-boss")!.textContent).toBe("Already in BOSS");
    await press(tid("px-compose-start"));
    expect(writes(calls).map((c) => [c.path, c.body])).toEqual([["/v1/work-items/wi_old/start-from-ticket", { project_id: P, workflow_key: "fast_fix" }]]);
  });

  it("a refusal is said in words, and a key no system owns leaves New work as it was", async () => {
    engine(() => ({ status: 424, body: { detail: { error: "auth_failed", message: "Jira refused BOSS's token." } } }));
    await openModal();
    await type(tid("px-compose-text"), "LEND-142 — keep the email.");
    await pause();
    expect(tid("px-nw-ticket-error")!.textContent).toContain("Jira refused BOSS's token.");
    unmount();
    const calls = engine(() => ({ body: { tickets: [], errors: [] } }));
    await openModal();
    await type(tid("px-compose-text"), "UTF-8 handling in the export is broken");
    await pause();
    expect(tid("px-nw-ticket")).toBeNull();
    await press(tid("px-compose-start"));
    expect(writes(calls).some((c) => c.path.includes("tickets/import"))).toBe(false);
  });

  it("Ticket or link says what BOSS recognises; Browse tickets opens the import view on Work", async () => {
    engine(() => ({ body: { tickets: [], errors: [] } }));
    await openModal();
    await press(tid("px-compose-ticket"));
    expect(tid("px-nw-recognises")!.textContent).toContain("LEND-142");
    await press(tid("px-nw-browse"));
    expect(tid("px-composer")).toBeNull();
    const q = new URLSearchParams(window.location.search);
    expect(q.get("tickets")).toBe("import");
    expect(q.get("tab")).toBe("work");
  });
});

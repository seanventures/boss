// @vitest-environment happy-dom
/**
 * NEW WORK FROM A TICKET HAS THE RIGHT DEFAULTS (BOSSAPP-1334, the FXQuote run, Jira KAN-11):
 *   - a Story is picked by type AND size (the engine's `suggested_workflow`): a Medium story is a
 *     Major feature, and the card says what it picked from ("Story · Medium");
 *   - the ticket card's pick and the "Picked for you" Workflow chip are ONE choice: switching
 *     either moves the other, and Start sends it;
 *   - the ticket's acceptance list is on the card;
 *   - "Maps to <project>" only for a real project-key mapping, never the connection's home;
 *   - New work has the Profile choice Change it has, opening on the project's default.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { ProjectPage } from "../app/projects/project-page";
import {
  FX_DEPLOYMENTS, FX_HEALTH, FX_KNOWLEDGE, FX_ONBOARDING, FX_OVERVIEW, FX_SETTINGS, FX_WORK, FX_WORK_PLAN,
} from "../lib/project-experience-fixtures";
import { parseTicket, ticketWorkflow } from "../lib/tickets";
import { mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

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
const choose = async (el: Element | null, value: string) => {
  await act(async () => { (el as HTMLSelectElement).value = value; el!.dispatchEvent(new Event("change", { bubbles: true })); });
  await settle();
};

const KAN_11 = {
  provider: "jira", connection_id: "tc_fx", key: "KAN-11", id: "10011", url: "https://fx.atlassian.net/browse/KAN-11",
  title: "FXQuote: quote a currency pair", type: "story", status: "To Do", status_category: "todo", priority: "Medium",
  labels: [], parent_key: "", screen: { verdict: "clean", reasons: [] }, untrusted: false, in_boss: null,
  body: "Traders need a firm quote.\n\nSize: Medium", comments: [], attachments: [],
  acceptance_criteria: ["GET /quote?pair=EURUSD returns bid and ask", "A quote expires after 30 seconds", "An unknown pair answers 404"],
  size: "medium", suggested_workflow: "major_feature",
  // The connection was added from another project and has no mapping for KAN: its "home".
  maps_to: { project_id: "boss-dogfood", name: "BOSS dogfood bootstrap", by: "home" },
};

function engine(ticket: Record<string, unknown> = KAN_11, profiles: unknown = { default: "lean", items: [] }) {
  return stubFetch((c: Call) => {
    if (c.path === "/v1/tickets/resolve") return { body: { refs: [], tickets: [ticket], errors: [], connected: 1 } };
    if (c.path === "/v1/pipeline-profiles") return { body: profiles };
    if (c.path === `/v1/projects/${P}/tickets/import`) return { body: { imported: [{ key: "KAN-11", work_item_id: "wi_11" }], already_in_boss: [] } };
    if (/^\/v1\/work-items\/[^/]+\/start-from-ticket$/.test(c.path)) return { body: { workflow_key: "major_feature", run_id: "wfr_11" } };
    if (c.method === "POST" && c.path === `/v1/projects/${P}/work/understand`) return { body: { status: "empty" } };
    if (c.method === "GET" && c.path === `/v1/projects/${P}/work/plan`) {
      const wf = new URLSearchParams(c.search).get("workflow_key") || "minor_feature";
      const name = wf === "major_feature" ? "Major Feature" : "Minor Feature";
      return { body: { ...FX_WORK_PLAN, workflow: { ...FX_WORK_PLAN.workflow, key: wf, name, chosen: wf === "minor_feature" ? "default" : "picked" } } };
    }
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

async function openWithTicket() {
  window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project&tab=work`);
  await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, "owner");
  await settle(10);
  await press(tid("px-head-new-work"));
  await type(tid("px-compose-text"), "Build KAN-11");
  await pause();
  await settle(10);
}
const chipWorkflow = () => tid("px-compose-workflow")?.textContent ?? "";
const cardPick = () => tid("px-nw-ticket-workflow") as HTMLSelectElement | null;

beforeEach(() => { try { window.localStorage.clear(); } catch { /* none */ } });
afterEach(() => { unmount(); });

describe("a ticket's workflow, from its type and size", () => {
  it("a Medium story starts as a Major feature, on the card and on the Picked-for-you chip", async () => {
    engine();
    await openWithTicket();
    expect(cardPick()?.value).toBe("major_feature");
    expect(tid("px-nw-ticket-picked-from")?.textContent).toContain("Story · Medium");
    expect(chipWorkflow()).toContain("Major Feature");
    expect(tid("px-compose-change")?.textContent).toContain("TICKET");
  });

  it("switching the card's pick moves the chip, and Start sends it", async () => {
    const calls = engine();
    await openWithTicket();
    await choose(cardPick(), "minor_feature");
    expect(chipWorkflow()).toContain("Minor Feature");
    await choose(cardPick(), "major_feature");
    expect(chipWorkflow()).toContain("Major Feature");
    await press(tid("px-compose-start"));
    const started = calls.find((c) => /start-from-ticket$/.test(c.path));
    expect((started?.body as { workflow_key?: string })?.workflow_key).toBe("major_feature");
    // The profile rides on a start from a ticket too (the project's default here: lean).
    expect((started?.body as { profile?: string })?.profile).toBe("lean");
  });

  it("a small story stays a Minor feature", async () => {
    engine({ ...KAN_11, size: "small", suggested_workflow: "minor_feature", body: "Size: S" });
    await openWithTicket();
    expect(cardPick()?.value).toBe("minor_feature");
    expect(chipWorkflow()).toContain("Minor Feature");
  });

  it("an older engine without suggested_workflow still picks by type", () => {
    const t = parseTicket({ ...KAN_11, suggested_workflow: undefined, size: undefined });
    expect(ticketWorkflow(t)).toBe("minor_feature");
    expect(t.size).toBeNull();
  });
});

describe("the ticket card", () => {
  it("shows the ticket's acceptance list", async () => {
    engine();
    await openWithTicket();
    const ac = tid("px-nw-ticket-ac")?.textContent ?? "";
    expect(ac).toContain("Acceptance criteria · 3");
    expect(ac).toContain("A quote expires after 30 seconds");
  });

  it("never says it maps to the project the connection was merely added from", async () => {
    engine();
    await openWithTicket();
    expect(tid("px-nw-ticket-maps")).toBeNull();
  });

  it("names a real mapping's project", async () => {
    engine({ ...KAN_11, maps_to: { project_id: "fxquote", name: "FXQuote", by: "mapping" } });
    await openWithTicket();
    expect(tid("px-nw-ticket-maps")?.textContent).toBe("Maps to FXQuote");
  });
});

describe("New work's profile", () => {
  it("opens on the project's default and sends the one picked", async () => {
    const calls = engine({ ...KAN_11, key: "", url: "" } as never);
    window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project&tab=work`);
    await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, "owner");
    await settle(10);
    await press(tid("px-head-new-work"));
    await settle(6);
    expect(tid("px-nw-profile")?.textContent).toBe("Lean");
    await press(tid("px-nw-chip-profile"));
    await press(tid("px-nw-profile-strict"));
    expect(tid("px-nw-profile")?.textContent).toBe("Strict");
    await type(tid("px-compose-text"), "Add a health route that reports the pool");
    await press(tid("px-compose-start"));
    const sent = calls.find((c) => c.method === "POST" && c.path === `/v1/projects/${P}/work`);
    expect((sent?.body as { profile?: string })?.profile).toBe("strict");
  });

  it("sends the project's default when the person did not pick", async () => {
    const calls = engine(KAN_11, { default: "balanced", items: [] });
    window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project&tab=work`);
    await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, "owner");
    await settle(10);
    await press(tid("px-head-new-work"));
    await settle(6);
    await type(tid("px-compose-text"), "Add a health route that reports the pool");
    await press(tid("px-compose-start"));
    const sent = calls.find((c) => c.method === "POST" && c.path === `/v1/projects/${P}/work`);
    expect((sent?.body as { profile?: string })?.profile).toBe("balanced");
  });
});

// @vitest-environment happy-dom
/**
 * HELM'S READ NEVER HANGS (recipe-box walk 2026-11-18, item 11): "Reading your words and the code
 * graph…" sat unchanged for 15 s and more, also after Start. Helm's read is a real model turn
 * (15-40 s on a subscription model), so:
 *   - the card counts how long it has been reading, and past READ_PATIENCE_S says Start works now;
 *   - the read is a background write, not a press: a second read of changed words while the
 *     first is still running is SENT (it was refused as "Ask Helm is still in progress", and the
 *     older answer was then dropped as stale);
 *   - a read that throws ends in the fallback card, never in "reading";
 *   - after Start the card says it started with the words as written.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { ProjectPage } from "../app/projects/project-page";
import { READ_PATIENCE_S, readingWords } from "../lib/new-work";
import {
  FX_DEPLOYMENTS, FX_HEALTH, FX_KNOWLEDGE, FX_ONBOARDING, FX_OVERVIEW, FX_SETTINGS, FX_WORK, FX_WORK_PLAN,
} from "../lib/project-experience-fixtures";
import { mountAs, tid, unmount } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "helpdesk-ai-demo";
const UNDERSTAND = `/v1/projects/${P}/work/understand`;
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const type = async (el: Element | null, value: string) => {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(el, value);
    (el as HTMLElement).dispatchEvent(new Event("input", { bubbles: true }));
  });
};
const wait = async (ms: number) => { await act(async () => { await new Promise((r) => setTimeout(r, ms)); }); await settle(); };

const BRIEF = {
  title: "Double the servings from the page", what_changes: "The test clicks More servings.",
  area: [], done_when: ["The test clicks the + control"], risk: { level: "low", why: "" }, added: [], open_questions: [],
};
const UNDERSTOOD = { status: "understood", brief: BRIEF, reply: "", ask: null, applied: [], settings: {},
                     agent: { name: "Helm", role_key: "engineering_captain" }, graph: { indexed: true, files: 2 } };

type Pending = { body: { text: string }; resolve: (v: unknown) => void; reject: (e: unknown) => void };

/** The engine, with Helm's read held open until the test answers it (or throws). */
function engine(mode: "hold" | "throw" = "hold") {
  const reads: Pending[] = [];
  const json = (b: unknown) => new Response(JSON.stringify(b), { status: 200, headers: { "Content-Type": "application/json" } });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input instanceof Request ? input.url : input), "http://console.test");
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "POST" && url.pathname === UNDERSTAND) {
      if (mode === "throw") throw new TypeError("Failed to fetch");
      return new Promise<Response>((resolve, reject) => {
        reads.push({ body: JSON.parse(String(init?.body)), resolve: (b) => resolve(json(b)), reject });
      });
    }
    if (method === "GET" && url.pathname === `/v1/projects/${P}/work/plan`) return json({ ...FX_WORK_PLAN });
    const m = url.pathname.match(/^\/v1\/projects\/([^/]+)\/(overview|work|knowledge|deployments|onboarding|settings|health)$/);
    if (method === "GET" && m) {
      const table: Record<string, unknown> = { overview: FX_OVERVIEW, work: FX_WORK, knowledge: FX_KNOWLEDGE, deployments: FX_DEPLOYMENTS,
                                                onboarding: { ...FX_ONBOARDING, state: "ready" }, settings: FX_SETTINGS, health: FX_HEALTH };
      return json(table[m[2]]);
    }
    return new Response(JSON.stringify({ error: "not_found" }), { status: 404 });
  }));
  return reads;
}

async function openComposer() {
  window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project&tab=work`);
  await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, "owner");
  await settle(10);
  await press(tid("px-nw-bar-input"));
}

beforeEach(() => { window.history.replaceState(null, "", "/"); try { window.localStorage.clear(); } catch { /* none */ } });
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("the reading line moves", () => {
  it("says how long it has read, and past the patience bound that Start works now", () => {
    expect(readingWords(0)).toBe("Reading your words and the code graph…");
    expect(readingWords(6)).toMatch(/Writing the brief .* · 6s$/);
    expect(readingWords(READ_PATIENCE_S)).toMatch(/^Still writing the brief · 12s\. Start works now/);
    expect(readingWords(30, { starting: true })).toMatch(/^Starting with your words as written/);
  });
});

describe("Helm's read in the New work box", () => {
  it("counts while it reads, and a second read of changed words is sent, not refused", async () => {
    const reads = engine();
    await openComposer();
    await type(tid("px-compose-text"), "Fix the serving-scaling test so it clicks the + control");
    await wait(1250);
    expect(reads).toHaveLength(1);
    expect(tid("px-ub")!.getAttribute("data-state")).toBe("reading");
    await wait(1100);
    expect(Number(tid("px-brief-reading-words")!.getAttribute("data-secs"))).toBeGreaterThanOrEqual(1);

    // The words change while the first read is still running: the new read goes out.
    await type(tid("px-compose-text"), "Fix the serving-scaling test so it clicks the + control twice");
    await wait(1250);
    expect(reads).toHaveLength(2);
    expect(document.body.textContent).not.toMatch(/still in progress/);
    await act(async () => { reads[0].resolve(UNDERSTOOD); });
    await settle();
    await act(async () => { reads[1].resolve(UNDERSTOOD); });
    await settle();
    expect(tid("px-ub")!.getAttribute("data-state")).toBe("ready");
    expect(tid("px-ub-v-title")!.textContent).toBe("Double the servings from the page");
  });

  it("a read that throws ends in the fallback card, never in reading", async () => {
    engine("throw");
    await openComposer();
    await type(tid("px-compose-text"), "Fix the serving-scaling test so it clicks the + control");
    await wait(1250);
    expect(tid("px-ub")!.getAttribute("data-state")).toBe("fallback");
  });
});

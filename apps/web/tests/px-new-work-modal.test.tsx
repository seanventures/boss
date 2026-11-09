// @vitest-environment happy-dom
/**
 * THE NEW WORK MODAL (wireframe new-project-v2/d-new-work.html, parity e-new-work-parity.html),
 * on the engine's own shapes:
 *   - it opens from the header, from the Work tab's bar (typing there brings the words), from N,
 *     and from an Ask-Helm answer's Edit (`compose=1`: prefilled, never sent); Esc keeps a draft;
 *   - Helm's understanding (POST …/work/understand): the brief card with Helm's additions marked,
 *     its question with quick replies; each answer updates the brief ("+ from chat"), and what the
 *     person asked for sets the chips; no turn count or cap is ever shown and the talk goes on;
 *   - Looks right folds the brief; Fix it edits it; Start sends the brief and the chips; Add to
 *     Next goes through the plan route; no model → Start uses the words;
 *   - the Workflow chip's popover lists this project's workflows with steps, estimate and old size;
 *   - Open in Ask Helm carries the draft (never sent).
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
import { $$, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "helpdesk-ai-demo";
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const type = async (el: Element | null, value: string) => {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    (el as HTMLElement).dispatchEvent(new Event("input", { bubbles: true }));
  });
};
const key = async (k: string, opts: KeyboardEventInit = {}, target: EventTarget = document.body) => {
  await act(async () => { target.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true, ...opts })); });
  await settle();
};
/** Helm reads after a pause in typing. */
const pause = async () => { await act(async () => { await new Promise((r) => setTimeout(r, 1250)); }); await settle(); };
const at = (q: string) => window.history.replaceState(null, "", `/?org=org_1&${q}`);
const params = () => new URLSearchParams(window.location.search);

const WORDS = "Let agents tag tickets by product.\nWhen a ticket comes in, pick the product from the email and the customer's plan; staff can change it in the inbox.\nLead wants to filter the inbox by product.";
const BRIEF = {
  title: "Auto-tag new tickets by product",
  what_changes: "Each new ticket gets a product tag picked from the email text and the customer’s plan, with a confidence.",
  area: [{ path: "api/tickets/intake.py", why: "writes tickets", new: false }, { path: "migrations/tickets_product.py", why: "tickets.product", new: true }],
  done_when: ["A new ticket has a product tag within 10 s", "The inbox filters by product"],
  risk: { level: "medium", why: "touches intake" }, added: ["with a confidence"], open_questions: [],
};
const FIRST = {
  status: "understood", brief: BRIEF, reply: "There’s already a tags table, but nothing writes product tags yet. One question:",
  ask: { text: "Should the 12,000 existing tickets be tagged too, or only new ones?", replies: ["Only new", "Re-tag all once", "Last 30 days"] },
  applied: [], settings: {}, agent: { name: "Helm", role_key: "engineering_captain" }, graph: { indexed: true, files: 3 },
};
const SECOND = {
  ...FIRST, ask: null, reply: "Added it as a background job. Set both below.", applied: ["Done when +1", "priority high"],
  brief: { ...BRIEF, done_when: [...BRIEF.done_when, "Re-tags the 12,000 existing tickets once, in the background"] },
  settings: { priority: "high", notify: "#support-eng" },
};
const DETAIL = {
  ...FX_WORK_PLAN,
  steps: [{ id: "meeting", label: "Plan", you: false }, { id: "build", label: "Build", you: false }, { id: "deploy", label: "Deploy", you: true, approve: "the deploy" }],
  options: [{ key: "minor_feature", name: "Minor Feature", detail: "", work_class: "feature.minor", fits: true, steps: 7, approvals: ["Deploy"], minutes: 18, usd: 1.2, was: "Small" },
            { key: "major_feature", name: "Major Feature", detail: "", work_class: "feature.major", fits: true, steps: 11, approvals: ["Spec review", "Deploy"], minutes: 75, usd: 7.4, was: "Spec first · Major" }],
  defaults: { tracker: { provider: "github_issues", name: "GitHub", where: "paramg/helpdesk-ai-demo", connected: false },
              targets: [{ id: "dt_s", label: "Staging", environment: "staging", app: "helpdesk", url: "http://10.0.0.5:8004", port: 8004, prod: false }],
              target: "dt_s", tell: { routes: [{ event: "needs_you", label: "Needs you", words: "Email" }, { event: "pr_ready", label: "PR ready", words: "Email" }] }, priority: "normal" },
};

function engine(understand: (c: Call, n: number) => unknown = (_c, n) => (n === 1 ? FIRST : SECOND)) {
  let turns = 0;
  return stubFetch((c) => {
    if (c.method === "POST" && c.path === `/v1/projects/${P}/work/understand`) { turns += 1; return { body: understand(c, turns) }; }
    if (c.method === "GET" && c.path === `/v1/projects/${P}/work/plan`) return { body: { ...DETAIL, kind: new URLSearchParams(c.search).get("kind") } };
    if (c.method === "POST" && c.path === `/v1/projects/${P}/plan`) return { body: { item_id: "wi_new", lane: "next", position: 1 } };
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

async function open(q = "&tab=work") {
  at(`project_id=${P}&area=project${q}`);
  await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, "owner");
  await settle(10);
}
const posts = (calls: Call[], path: string) => calls.filter((c) => c.method === "POST" && c.path === path);
const said = () => (tid("px-composer")?.textContent ?? "");

beforeEach(() => { window.history.replaceState(null, "", "/"); try { window.localStorage.clear(); } catch { /* none */ } });
afterEach(() => { unmount(); });

describe("it opens from every door", () => {
  it("an Ask-Helm answer's Edit opens it prefilled on any tab, and nothing is sent", async () => {
    const calls = engine();
    await open("&tab=overview&compose=1&compose_kind=bug&compose_text=SLA+timer+ignores+weekends");
    expect(tid("px-composer")).not.toBeNull();
    expect((tid("px-compose-text") as HTMLTextAreaElement).value).toBe("SLA timer ignores weekends");
    expect(tid("px-compose-bug")!.getAttribute("aria-selected")).toBe("true");
    expect(params().get("compose")).toBeNull();
    expect(params().get("tab")).toBe("overview"); // stays where it was opened
    expect(posts(calls, `/v1/projects/${P}/work`)).toHaveLength(0);
  });

  it("N opens it; Esc closes it and keeps the draft, which comes back", async () => {
    engine(() => ({ status: "empty" }));
    await open();
    await key("n");
    expect(tid("px-composer")).not.toBeNull();
    await type(tid("px-compose-text"), "Export swaps as CSV");
    await key("Escape");
    expect(tid("px-composer")).toBeNull();
    await press(tid("px-head-new-work"));
    expect((tid("px-compose-text") as HTMLTextAreaElement).value).toBe("Export swaps as CSV");
    expect(tid("px-nw-back")!.textContent).toContain("Your draft is back");
  });

  it("typing in the Work tab's bar opens it with the words", async () => {
    engine(() => ({ status: "empty" }));
    await open();
    await type(tid("px-nw-bar-input"), "A");
    expect(tid("px-composer")).not.toBeNull();
    expect((tid("px-compose-text") as HTMLTextAreaElement).value).toBe("A");
    // A kept draft comes back with the new keys added, never replaced by them.
    await type(tid("px-compose-text"), "Add CSV export");
    await key("Escape");
    await type(tid("px-nw-bar-input"), "!");
    expect((tid("px-compose-text") as HTMLTextAreaElement).value).toBe("Add CSV export!");
  });
});

describe("Helm's understanding and Talk it through", () => {
  it("reads the words into a brief with Helm's additions marked, asks one question, and the brief updates from each answer", async () => {
    const calls = engine();
    await open();
    await press(tid("px-nw-bar-input"));
    await type(tid("px-compose-text"), WORDS);
    await pause();
    const first = posts(calls, `/v1/projects/${P}/work/understand`);
    expect(first).toHaveLength(1);
    expect((first[0].body as { text: string; kind: string }).kind).toBe("feature");
    expect(tid("px-ub-v-title")!.textContent).toBe("Auto-tag new tickets by product");
    expect(tid("px-ub-heading")!.textContent).toBe("Task");
    expect(tid("px-ub")!.textContent).not.toMatch(/understood/i);
    expect($$("[data-testid='px-ub'] mark").map((m) => m.textContent)).toContain("with a confidence");
    expect(tid("px-ub-row-area")!.textContent).toContain("api/tickets/intake.py");
    expect(tid("px-ub-row-area")!.textContent).toContain("+ migrations/tickets_product.py");
    const helm = $$("[data-testid='px-nw-msg-helm']").map((m) => m.textContent);
    expect(helm[1]).toContain("12,000 existing tickets");
    expect($$("[data-testid='px-nw-replies'] button").map((b) => b.textContent)).toEqual(["Only new", "Re-tag all once", "Last 30 days"]);
    // No turn count and no cap, anywhere.
    expect(said()).not.toMatch(/\d\s*\/\s*4|turns?\b|Up to 4/i);
    // A quick reply: the brief updates, the chips follow what was asked for.
    await press($$("[data-testid='px-nw-replies'] button")[1]);
    const second = posts(calls, `/v1/projects/${P}/work/understand`)[1].body as { chat: { from: string; text: string; ask?: boolean }[] };
    expect(second.chat.at(-1)).toEqual({ from: "you", text: "Re-tag all once" });
    expect(second.chat.some((m) => m.from === "helm" && m.ask)).toBe(true);
    expect(tid("px-ub-row-chat0")!.textContent).toContain("Re-tags the 12,000 existing tickets");
    expect(said()).toContain("Brief updated · Done when +1 · priority high");
    expect(tid("px-nw-priority")!.textContent).toBe("High");
    expect(tid("px-nw-chip-priority")!.textContent).toContain("set in chat");
    expect(tid("px-nw-tell")!.textContent).toContain("#support-eng");
    // The person may keep talking: more turns, still no cap.
    for (let i = 0; i < 5; i++) {
      await type(tid("px-nw-reply"), `One more thing ${i}`);
      await press(tid("px-nw-send"));
    }
    expect(posts(calls, `/v1/projects/${P}/work/understand`)).toHaveLength(7);
    expect($$("[data-testid='px-nw-msg-you']")).toHaveLength(6);
    expect(said()).not.toMatch(/\d\s*\/\s*4|Up to 4/i);
    // Looks right folds the brief and the words; Start sends the brief and the chips.
    await press(tid("px-ub-ok"));
    expect(tid("px-ub")!.getAttribute("data-state")).toBe("confirmed");
    expect(tid("px-nw-words-fold")).not.toBeNull();
    await press(tid("px-compose-start"));
    const start = posts(calls, `/v1/projects/${P}/work`)[0].body as Record<string, unknown>;
    expect(start.kind).toBe("feature");
    expect((start.brief as { title: string; done_when: string[] }).done_when).toHaveLength(3);
    expect(start.priority).toBe("high");
    expect(start.notify_channel).toBe("#support-eng");
    expect(tid("px-composer")).toBeNull();
    expect(tid("px-said")!.textContent).toMatch(/Started/);
  });

  it("Fix it edits the brief in place, and Start sends the edit", async () => {
    const calls = engine();
    await open();
    await press(tid("px-nw-bar-input"));
    await type(tid("px-compose-text"), WORDS);
    await pause();
    await press(tid("px-ub-fix"));
    await type(tid("px-ub-e-title"), "Tag tickets by product");
    await press(tid("px-ub-e-done-add"));
    await type(tid("px-ub-e-done-2"), "Staff can undo a tag");
    await press(tid("px-ub-fix"));
    expect(tid("px-ub-v-title")!.textContent).toBe("Tag tickets by product");
    await press(tid("px-compose-start"));
    const b = (posts(calls, `/v1/projects/${P}/work`)[0].body as { brief: { title: string; done_when: string[] } }).brief;
    expect(b.title).toBe("Tag tickets by product");
    expect(b.done_when.at(-1)).toBe("Staff can undo a tag");
  });

  it("with no model, the card says Start uses the words, and Start sends no brief", async () => {
    const calls = engine(() => ({ status: "no_model", brief: null, reply: "I couldn't read it just now.", why: "no model answered" }));
    await open();
    await press(tid("px-nw-bar-input"));
    await type(tid("px-compose-text"), WORDS);
    await pause();
    expect(tid("px-ub")!.getAttribute("data-state")).toBe("fallback");
    expect(tid("px-ub")!.textContent).toContain("Start uses your words as written");
    await press(tid("px-compose-start"));
    expect(posts(calls, `/v1/projects/${P}/work`)[0].body).toEqual({ kind: "feature", text: WORDS });
  });

  it("Open in Ask Helm carries the draft to the captain, never sent", async () => {
    engine();
    await open();
    await press(tid("px-nw-bar-input"));
    await type(tid("px-compose-text"), WORDS);
    await pause();
    await press(tid("px-nw-open-ask"));
    expect(tid("px-composer")).toBeNull();
    expect(params().get("drawer")).toBe("captain");
    const draft = (tid("px-drawer-captain")?.querySelector("textarea, input[type='text']") as HTMLTextAreaElement | null)?.value ?? params().get("cap_draft") ?? "";
    expect(draft).toContain("Let agents tag tickets by product");
  });
});

describe("Picked for you, Add to Next", () => {
  it("the Workflow chip lists this project's workflows with steps, estimate and the old size, and Steps shows who does each", async () => {
    const calls = engine(() => ({ status: "empty" }));
    await open();
    await press(tid("px-nw-bar-input"));
    expect(calls.some((c) => c.path === `/v1/projects/${P}/work/plan` && new URLSearchParams(c.search).get("detail") === "true")).toBe(true);
    expect(tid("px-compose-change")!.textContent).toContain("3 steps");
    expect(tid("px-compose-change")!.textContent).toContain("1 you approve: the deploy");
    await press(tid("px-compose-change"));
    const major = tid("px-compose-wf-major_feature")!.textContent!;
    expect(major).toContain("11 steps");
    expect(major).toContain("was “Spec first · Major”");
    await press(tid("px-nw-steps-open"));
    expect($$("[data-testid='px-nw-steps'] li").map((l) => l.textContent)).toEqual(["1Plan", "2Build", "✋Deployyou approve the deploy"]);
  });

  it("Add to Next goes through the plan route with the chips, and nothing starts", async () => {
    const calls = engine(() => ({ status: "empty" }));
    await open();
    await press(tid("px-nw-bar-input"));
    await type(tid("px-compose-text"), "Export swaps as CSV");
    await press(tid("px-nw-chip-priority"));
    await press(tid("px-nw-prio-urgent"));
    await press(tid("px-nw-chip-issue"));
    await press(tid("px-nw-issue-create"));
    await press(tid("px-nw-next"));
    expect(posts(calls, `/v1/projects/${P}/plan`)[0].body).toEqual({ kind: "feature", text: "Export swaps as CSV", priority: "urgent", issue: { mode: "create" } });
    expect(posts(calls, `/v1/projects/${P}/work`)).toHaveLength(0);
    expect(tid("px-composer")).toBeNull();
    expect(tid("px-said")!.textContent).toMatch(/Added to Next \(#1\)/);
  });
});

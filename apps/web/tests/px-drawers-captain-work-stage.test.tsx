// @vitest-environment happy-dom
/**
 * THE PROJECT PAGE'S DRAWERS (screens 05b, 09; ACCEPTANCE 05b-*, 09-*, X-50/51).
 *
 * - Ask the captain (09): `&drawer=captain` opens a drawer, not the generic dock — Helm, the
 *   blue Captain tag, "HelpDesk AI Demo only · knows 412 facts", past conversations, and the
 *   dock's own conversation (AgentChat, subject project:<id>, the captain's role). Its chips come
 *   from the record and a tap SENDS; a door's question (`cap_ask`) is sent once and leaves the URL.
 * - An answer that opens with "A → B → C" is drawn as flow chips (lib/answer-flow).
 * - The work drawer (05b): Run #43 · $2.10 · 31 min, the one amber "Merge PR #3", How it went in
 *   words with each proof link (PRD, Diff, PR #3 ↗), Preview and the Plan · Build · Review cost bar,
 *   and the expand icon to the full build page.
 * - A journey node opens what happened at that stage (ready's checklist, production's set-up).
 *
 * The turn is the console's server action, faked here; every message it receives is kept.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

const sent: Array<Record<string, string>> = [];
vi.mock("../app/actions", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  askBossAction: vi.fn(async (_prev: unknown, fd: FormData) => {
    const fields: Record<string, string> = {};
    fd.forEach((v, k) => { fields[k] = String(v); });
    sent.push(fields);
    return { ok: true, data: { thread_id: "athr_1", messages: [
      { role: "user", content: fields.message },
      { role: "assistant", content: "! Merge PR #3 → ● Search filters → ✓ Staging → Demo ready\nTwo things: **Merge PR #3**, then Beacon redeploys staging." }],
      proposed_actions: [], tool_calls: [], next_steps: [] } };
  }),
}));

import { FX_DEPLOYMENTS, FX_ONBOARDING, FX_OVERVIEW, FX_WORK } from "../lib/project-experience-fixtures";
import { leadingFlow } from "../lib/answer-flow";
import { $, $$, mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

const { ProjectPage } = await import("../app/projects/project-page");

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "helpdesk-ai-demo";
const settle = async (n = 10) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const params = () => new URLSearchParams(window.location.search);

const CAPTAIN = {
  agent_id: "agt_helm", name: "Helm", role_key: "engineering_captain", avatar: "", status: "Watching 1 build",
  squad: [{ name: "Juno", role_key: "product_manager", role: "Plans" }], scope: { label: "HelpDesk AI Demo only" }, facts: 412,
  recent: [{ act_id: "act_1", role_key: "engineering_captain", label: "Build · Ticket search filters", state: "now", at: "2026-11-06T10:00:00Z",
             href: `?project_id=${P}&area=agent_ops&act=act_1` }],
  starters: [{ label: "Plan next week", message: "Plan next week for this project.", icon: "map" },
             { label: "Why is PR #3 waiting?", message: "Why is PR #3 waiting, and what do I need to do?", icon: "question" },
             { label: "What did this cost?", message: "What did this project cost this week, and on what?", icon: "coins" }],
};
const STORY = {
  id: "wi_visual", number: 43, minutes: 31, cost_usd: 2.1, preview: "http://203.0.113.20:8104",
  cost_parts: [{ label: "Plan", usd: 0.2 }, { label: "Build", usd: 1.3 }, { label: "Review", usd: 0.6 }],
  pr: { number: 3, external_url: "https://github.com/paramg/helpdesk-ai-demo/pull/3" },
  stages: [
    { key: "plan", name: "Plan", state: "done", sub: "PRD agreed", person: false, link: { label: "PRD", href: `?project_id=${P}&area=meetings&meeting=mtg_1` } },
    { key: "design", name: "You approved", state: "done", sub: "Param", at: "2026-11-03T14:02:00Z", person: true, link: null },
    { key: "build", name: "Build", state: "done", sub: "11 files changed", person: false, link: { label: "Diff", external_url: "https://github.com/paramg/helpdesk-ai-demo/pull/3/files" } },
    { key: "review", name: "Review & test", state: "done", sub: "Tests pass · 2 review rounds", person: false, link: null },
    { key: "pr", name: "Merge", state: "waiting", sub: "Only a person merges", person: true, link: { label: "PR #3", external_url: "https://github.com/paramg/helpdesk-ai-demo/pull/3" } },
    { key: "ship", name: "Staging", state: "todo", sub: "Auto after merge", person: false, link: null },
  ],
};

function engine() {
  return stubFetch((c) => {
    if (c.path.endsWith("/api/agent-thread")) return { body: { thread_id: "athr_1" } };
    if (c.path === "/api/agent") return { body: { tasks: [], threads: [], messages: [] } };
    if (c.method !== "GET") return { body: { ok: true } };
    if (c.path === `/v1/projects/${P}/overview`) return { body: FX_OVERVIEW };
    if (c.path === `/v1/projects/${P}/work`) return { body: FX_WORK };
    if (c.path === `/v1/projects/${P}/captain`) return { body: CAPTAIN };
    if (c.path === `/v1/projects/${P}/work/wi_visual/story`) return { body: STORY };
    if (c.path === `/v1/projects/${P}/deployments`) return { body: FX_DEPLOYMENTS };
    if (c.path === `/v1/projects/${P}/onboarding`) return { body: { ...FX_ONBOARDING, state: "ready" } };
    return undefined;
  });
}

async function open(q: string) {
  window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project${q}`);
  await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, "owner");
  await settle(14);
}

beforeEach(() => { sent.length = 0; window.history.replaceState(null, "", "/"); });
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("Ask the captain (screen 09)", () => {
  it("is a drawer with the captain, its scope and what it knows — and the dock's own conversation", async () => {
    engine();
    await open("&drawer=captain");
    const d = tid("px-drawer-captain")!;
    expect(d).not.toBeNull();
    expect(d.textContent).toContain("Helm");
    expect(d.textContent).toContain("Captain");
    expect(tid("px-capd-scope")!.textContent).toContain("HelpDesk AI Demo only");
    expect(tid("px-capd-scope")!.textContent).toContain("knows 412 facts");
    expect(tid("px-capd-chat")!.querySelector(".agent-chat-shell")).not.toBeNull();
    expect((d.querySelector("textarea") as HTMLTextAreaElement).placeholder).toBe("Ask Helm, or describe new work…");
    // Past conversations: what the crew did lately, each a link.
    await press(tid("px-capd-history"));
    expect(tid("px-capd-history-panel")!.textContent).toContain("Build · Ticket search filters");
    expect(tid("px-capd-recent-act_1")!.getAttribute("href")).toContain("area=agent_ops");
  });

  it("its chips come from the record and a tap sends; the answer's path is drawn as chips", async () => {
    engine();
    await open("&drawer=captain");
    const chips = $$("[data-testid='agent-starters'] button").map((b) => b.textContent);
    expect(chips).toEqual(["Plan next week", "Why is PR #3 waiting?", "What did this cost?"]);
    await press($$("[data-testid='agent-starters'] button")[1]);
    await settle(20);
    expect(sent.map((f) => f.message)).toEqual(["Why is PR #3 waiting, and what do I need to do?"]);
    const flow = tid("agent-flow")!;
    expect([...flow.querySelectorAll(".agent-flow-n")].map((n) => [n.textContent, n.getAttribute("data-s")])).toEqual([
      ["Merge PR #3", "waiting"], ["Search filters", "now"], ["✓ Staging", "done"], ["Demo ready", "todo"]]);
  });

  it("a door's question is sent once and leaves the URL; Esc closes the drawer", async () => {
    engine();
    await open("&drawer=captain&cap_ask=Plan%20next%20week%20for%20this%20project.");
    await settle(20);
    expect(sent.map((f) => f.message)).toEqual(["Plan next week for this project."]);
    expect(params().get("cap_ask")).toBeNull();
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    await settle();
    expect(params().get("drawer")).toBeNull();
    expect(tid("px-drawer-captain")).toBeNull();
  });

  it("the header's Ask opens the captain drawer, not the generic dock", async () => {
    engine();
    await open("");
    await press(tid("px-head-ask"));
    expect(params().get("drawer")).toBe("captain");
    expect(params().get("agent")).toBeNull();
    expect(tid("px-drawer-captain")).not.toBeNull();
  });
});

describe("the work drawer (screen 05b)", () => {
  it("tells how it went in words with each proof, the one action, preview and cost", async () => {
    engine();
    await open("&tab=work&drawer=work:wi_visual");
    const d = tid("px-drawer-work")!;
    expect(d).not.toBeNull();
    expect(tid("px-drawer-work-number")!.textContent).toBe("Run #43");
    expect(d.textContent).toContain("$2.10");
    expect(d.textContent).toContain("31 min");
    expect(tid("px-drawer-work-expand")!.getAttribute("href")).toContain("area=code_tasks");
    expect(tid("px-drawer-work-merge")!.textContent).toContain("Merge PR #3");
    expect($$("[data-testid='px-drawer-work-steps'] .px-step").map((r) => r.querySelector("b")!.textContent))
      .toEqual(["Plan", "You approved", "Build", "Review & test", "Merge", "Staging"]);
    expect(tid("px-drawer-step-plan")!.textContent).toContain("PRD agreed");
    expect(tid("px-drawer-step-link-plan")!.getAttribute("href")).toContain("area=meetings");
    expect(tid("px-drawer-step-link-build")!.getAttribute("href")).toBe("https://github.com/paramg/helpdesk-ai-demo/pull/3/files");
    expect(tid("px-drawer-step-link-pr")!.getAttribute("target")).toBe("_blank");
    expect(tid("px-drawer-step-pr")!.textContent).toContain("Only a person merges");
    expect(tid("px-drawer-work-preview")!.getAttribute("href")).toBe("http://203.0.113.20:8104");
    expect(tid("px-drawer-work-cost")!.querySelectorAll("span").length).toBe(3);
    expect(d.textContent).toContain("Plan · Build · Review");
  });
});

describe("journey node drawers", () => {
  it("ready shows its checklist; production says how to set it up; working lists what is in flight", async () => {
    engine();
    await open("&drawer=stage:ready");
    expect(tid("px-drawer-stage")).not.toBeNull();
    expect(tid("px-stage-checklist")!.querySelectorAll(".px-li").length).toBeGreaterThan(0);
    unmount();
    engine();
    await open("&drawer=stage:production");
    expect(tid("px-drawer-stage")!.textContent).toMatch(/production/i);
    unmount();
    engine();
    await open("&drawer=stage:working");
    expect(tid("px-stage-work")!.textContent).toContain("Ticket search filters");
  });
});

describe("an answer's leading path (lib/answer-flow)", () => {
  it("draws a chain of short labels and leaves prose alone", () => {
    expect(leadingFlow("! Merge PR #3 → ● Search filters → ✓ Staging → Demo ready\nTwo things.")).toEqual({
      nodes: [{ label: "Merge PR #3", state: "waiting" }, { label: "Search filters", state: "now" },
              { label: "Staging", state: "done" }, { label: "Demo ready", state: "todo" }],
      rest: "Two things." });
    expect(leadingFlow("The build failed → so I retried it with the right base branch.").nodes).toEqual([]);
    expect(leadingFlow("No arrow here.").nodes).toEqual([]);
    expect(leadingFlow("A → B → C → D → E → F → G").nodes).toEqual([]);
    expect($).toBeTypeOf("function");
  });
});

describe("a Needs-you item acts in place (PX-05)", () => {
  it("approves through the decision route, and Reject asks why in one line", async () => {
    const calls = engine();
    await open("&drawer=needs:dec_merge3");
    expect(tid("px-drawer-needs-approve")!.textContent).toContain("Merge PR #3");
    await press(tid("px-drawer-needs-approve"));
    expect(calls.some((c) => c.method === "POST" && c.path === "/v1/decisions/dec_merge3/approve")).toBe(true);
    expect(tid("px-drawer-needs")!.textContent).toContain("BOSS merges it now");
    await press(tid("px-drawer-needs-reject"));
    const box = tid("px-drawer-needs-reason") as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(box, "Wrong colours");
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await press(tid("px-drawer-needs-reject-send"));
    expect(calls.find((c) => c.method === "POST" && c.path === "/v1/decisions/dec_merge3/reject")?.body).toEqual({ note: "Wrong colours" });
  });
});

describe("closing a drawer never adds a history entry (PX-33)", () => {
  it("Esc on a drawer this page opened steps back; Back then does not reopen it", async () => {
    engine();
    await open("");
    const before = window.history.length;
    await press(tid("px-needs-dec_merge3"));
    expect(params().get("drawer")).toBe("needs:dec_merge3");
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    await settle(12);
    expect(params().get("drawer")).toBeNull();
    expect(window.history.length).toBe(before + 1); // the drawer's own entry, stepped back over — no new one
  });
  it("Esc on a drawer that came in on a link replaces it away", async () => {
    engine();
    await open("&drawer=stage:ready");
    const before = window.history.length;
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    await settle();
    expect(params().get("drawer")).toBeNull();
    expect(window.history.length).toBe(before);
  });
});

describe("the workflow run page's times are the viewer's (PX-46)", () => {
  it("never prints a UTC 'Z' stamp", async () => {
    const { startedLabel } = await import("../app/delivery-workflows-ui");
    expect(startedLabel("2026-11-06T16:56:00Z")).not.toMatch(/Z$/);
    expect(startedLabel("2026-11-06T16:56:00Z")).toMatch(/Nov 6/);
    expect(startedLabel("")).toBe("");
  });
});

describe("choosing a captain (PX-13)", () => {
  it("captains first with Recommended, the rest behind Show all with a search; the current one is not re-sent", async () => {
    const { CaptainPicker, captainChoices } = await import("../app/projects/captain-picker");
    const chosen: string[] = [];
    const choices = captainChoices([
      { agent_id: "agt_helm", role_key: "engineering_captain", name: "Helm", role: "Engineering Captain", line: "Owns the project end to end", captain_capable: true, recommended: true },
      { agent_id: "agt_pc", role_key: "product_captain", name: "Polaris", role: "Product Captain", line: "Leads from the product side", captain_capable: true },
      { agent_id: "agt_pm", role_key: "product_manager", name: "Juno", role: "Product Manager", line: "Plans on this project" },
      { agent_id: "agt_qa", role_key: "qa_engineer", name: "Vera", role: "QA Engineer", line: "Tests on this project" },
    ], []);
    await mountAs(<CaptainPicker choices={choices} current="engineering_captain" onChoose={(m) => chosen.push(m.role_key)} />, "owner");
    const names = () => $$("[data-testid^='px-set-captain-opt-']").map((b) => b.getAttribute("data-testid")!.replace("px-set-captain-opt-", ""));
    expect(names()).toEqual(["engineering_captain", "product_captain"]);
    expect(tid("px-set-captain-opt-engineering_captain")!.textContent).toContain("Current");
    expect((tid("px-set-captain-opt-engineering_captain") as HTMLButtonElement).disabled).toBe(true);
    await press(tid("px-set-captain-opt-engineering_captain"));
    expect(chosen).toEqual([]);
    expect(tid("px-set-captain-all")!.textContent).toBe("Show all agents (4)");
    await press(tid("px-set-captain-all"));
    expect(names()).toHaveLength(4);
    const box = tid("px-set-captain-search") as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(box, "test");
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(names()).toEqual(["qa_engineer"]);
    await press(tid("px-set-captain-opt-qa_engineer"));
    expect(chosen).toEqual(["qa_engineer"]);
  });
});

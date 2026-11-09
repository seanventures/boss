// @vitest-environment happy-dom
/**
 * AN ANSWER'S CARDS (screen 09; ACCEPTANCE 09-07, 09-09, 09-11..09-14; engine turn_cards).
 *
 * - The "New work" card: teal sparkle, "New work · VIP customer flag", ≈ $3 and ≈ 30m chips with
 *   their basis, the plan strip (Plan with Juno's face → You approve → Build → PR), and Start /
 *   Edit / Not now. Start sends THE start door with the card's body, then records "started" on
 *   the card; Edit opens the Work tab's composer prefilled; Not now hides it and records that.
 * - Citation chips open what the answer named: in-console ones keep the console, GitHub ones
 *   open a new tab with ↗.
 * - In the conversation (AgentChat, the dock and the captain drawer alike) the card renders from
 *   the turn's message, and the offer it came from is not repeated as a second button.
 * - A flow never leaves an arrow at a line's end: each step is its wire + node, and the step
 *   that starts a new line hides its wire.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

const PROPOSAL = {
  kind: "new_work", title: "VIP customer flag", text: "Add a VIP customer flag that jumps the queue.", work_kind: "feature",
  estimate: { usd: 3, minutes: 30, basis: "from 4 past Minor Feature runs here", runs: 4 },
  steps: [{ key: "plan", label: "Plan", type: "agent", role_key: "product_manager" }, { key: "approve", label: "You approve", type: "you" },
          { key: "build", label: "Build", icon: "hammer" }, { key: "pr", label: "PR", icon: "pr" }],
  start: { href: "/v1/projects/helpdesk/work", method: "POST",
           body: { kind: "feature", text: "Add a VIP customer flag that jumps the queue.", title: "VIP customer flag", source_ref: "chat:amsg_2" } },
  edit_href: "?project_id=helpdesk&area=project&tab=work&compose=1&compose_kind=feature&compose_text=Add+a+VIP+customer+flag",
  offer_id: "act_offer_vip", state: "open",
};
const CITES = [
  { kind: "issue", ref: "#12", label: "#12 weekend SLA", href: "https://github.com/acme/helpdesk/issues/12", external: true },
  { kind: "file", ref: "server/sla.py", label: "sla.py", href: "https://github.com/acme/helpdesk/blob/main/server/sla.py", external: true },
  { kind: "pr", ref: "#3", label: "PR #3", href: "?project_id=helpdesk&area=project&tab=work&drawer=run%3Awfr_1", external: false },
];

vi.mock("../app/actions", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  askBossAction: vi.fn(async (_prev: unknown, fd: FormData) => ({ ok: true, data: {
    thread_id: "athr_1",
    messages: [{ id: "amsg_1", role: "user", content: String(fd.get("message")) },
               { id: "amsg_2", role: "assistant", content: "I'll plan it as a minor feature.", proposal: PROPOSAL, citations: CITES }],
    proposed_actions: [{ id: "act_offer_vip", tool: "create_code_task", args: {}, label: "Build the VIP flag" },
                       { id: "act_other", tool: "investigate", args: {}, label: "Look at the SLA code" }],
    tool_calls: [], next_steps: [], proposal: PROPOSAL, citations: CITES } })),
}));

import { $, $$, mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";
const { ProposalCard, CitationChips, AnswerFlow, proposalOf, citationsOf } = await import("../app/answer-cards");
const { AgentChat } = await import("../app/agent-ui");

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const settle = async (n = 10) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };

afterEach(() => { unmount(); vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });

describe("the New work card", () => {
  it("shows what, ≈ cost and ≈ time with their basis, and the plan strip", async () => {
    stubFetch(() => ({ body: {} }));
    await mountAs(<ProposalCard projectId="helpdesk" threadId="athr_1" messageId="amsg_2" proposal={proposalOf(PROPOSAL)!} />);
    expect(tid("agent-proposal-card")!.textContent).toContain("New work · VIP customer flag");
    expect(tid("agent-proposal-usd")!.textContent).toBe("≈ $3");
    expect(tid("agent-proposal-time")!.textContent).toBe("≈ 30m");
    expect(tid("agent-proposal-usd")!.getAttribute("title")).toContain("from 4 past Minor Feature runs here");
    const plan = $$('[data-testid="agent-proposal-plan"] [role="listitem"]').map((n) => n.textContent);
    expect(plan).toEqual(["Plan", "You approve", "Build", "PR"]);
    expect($('[data-testid="agent-proposal-plan"] [role="listitem"][data-s="you"]')).not.toBeNull();
    // Start is the one primary; Edit and Not now are quiet.
    expect($$('[data-testid="agent-proposal-card"] [data-kind="primary"]').map((b) => b.textContent?.trim())).toEqual(["Start"]);
  });

  it("Start sends THE start door with the card's body, then records the start on the card", async () => {
    const calls = stubFetch((c) => c.path === "/v1/projects/helpdesk/work"
      ? { body: { run_id: "wfr_9", href: "?project_id=helpdesk&area=project&tab=work&drawer=run%3Awfr_9" } } : { body: { proposal: {} } });
    await mountAs(<ProposalCard projectId="helpdesk" threadId="athr_1" messageId="amsg_2" proposal={proposalOf(PROPOSAL)!} />);
    await press(tid("agent-proposal-start"));
    const startCall = calls.find((c) => c.path === "/v1/projects/helpdesk/work")!;
    expect(startCall.method).toBe("POST");
    expect(startCall.body).toMatchObject({ kind: "feature", text: PROPOSAL.text, title: "VIP customer flag", source_ref: "chat:amsg_2" });
    const recorded = calls.find((c) => c.path === "/v1/engine/agent/proposal")!;
    expect(recorded.body).toMatchObject({ thread_id: "athr_1", message_id: "amsg_2", outcome: "started", run_id: "wfr_9" });
    expect(tid("agent-proposal-started")!.textContent).toContain("Started");
    expect(tid("agent-proposal-open")!.getAttribute("href")).toContain("drawer=run%3Awfr_9");
  });

  it("a start the project isn't ready for says why and keeps the card", async () => {
    stubFetch((c) => c.path === "/v1/projects/helpdesk/work"
      ? { status: 409, body: { detail: { message: "This project has no deploy target yet.", readiness: {} } } } : { body: {} });
    await mountAs(<ProposalCard projectId="helpdesk" threadId="athr_1" messageId="amsg_2" proposal={proposalOf(PROPOSAL)!} />);
    await press(tid("agent-proposal-start"));
    expect(tid("agent-proposal-said")!.textContent).toContain("no deploy target");
    expect(tid("agent-proposal-start")).not.toBeNull();
  });

  it("Edit opens the Work composer prefilled; Not now hides the card and records it", async () => {
    const calls = stubFetch(() => ({ body: {} }));
    await mountAs(<ProposalCard projectId="helpdesk" threadId="athr_1" messageId="amsg_2" proposal={proposalOf(PROPOSAL)!} />);
    const edit = tid("agent-proposal-edit")!.getAttribute("href")!;
    expect(edit).toContain("tab=work"); expect(edit).toContain("compose=1"); expect(edit).toContain("compose_text=");
    await press(tid("agent-proposal-dismiss"));
    expect(tid("agent-proposal-card")).toBeNull();
    expect(calls.find((c) => c.path === "/v1/engine/agent/proposal")!.body).toMatchObject({ outcome: "dismissed" });
  });

  it("a card already started reads Started, a dismissed one is gone", async () => {
    stubFetch(() => ({ body: {} }));
    await mountAs(<div>
      <ProposalCard projectId="helpdesk" threadId="t" messageId="m" proposal={proposalOf({ ...PROPOSAL, state: "started", href: "?tab=work" })!} />
      <ProposalCard projectId="helpdesk" threadId="t" messageId="m2" proposal={proposalOf({ ...PROPOSAL, state: "dismissed" })!} />
    </div>);
    expect($$('[data-testid="agent-proposal-card"]').length).toBe(1);
    expect(tid("agent-proposal-started")).not.toBeNull();
  });
});

describe("citation chips", () => {
  it("each opens its record: GitHub in a new tab with ↗, the console in place", async () => {
    await mountAs(<CitationChips items={citationsOf(CITES)} />);
    const file = tid("agent-cite-file")!;
    expect(file.textContent).toContain("sla.py");
    expect(file.getAttribute("target")).toBe("_blank");
    expect(file.textContent).toContain("↗");
    const pr = tid("agent-cite-pr")!;
    expect(pr.getAttribute("target")).toBeNull();
    expect(pr.getAttribute("href")).toContain("drawer=run%3Awfr_1");
    expect(tid("agent-cite-issue")!.textContent).toContain("#12 weekend SLA");
  });
  it("drops what has no link", () => {
    expect(citationsOf([{ kind: "pr", label: "PR #9", href: "" }, null, "x"])).toEqual([]);
    expect(proposalOf({ kind: "other" })).toBeNull();
  });
});

describe("in the conversation", () => {
  it("the turn's card and chips render under the answer, and its offer is not a second button", async () => {
    stubFetch(() => ({ body: {} }));
    await mountAs(<AgentChat projectId="helpdesk" actor="ceo" fresh />);
    const box = $("textarea") as HTMLTextAreaElement;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      set.call(box, "Also add a VIP customer flag that jumps the queue.");
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await press($('[data-testid="agent-send"]') ?? $('button[type="submit"]'));
    await settle(20);
    expect(tid("agent-proposal-card")!.textContent).toContain("VIP customer flag");
    expect(tid("agent-cites")).not.toBeNull();
    expect(tid("agent-proposal-create_code_task")).toBeNull();
    expect(tid("agent-proposal-investigate")).not.toBeNull();
  });
});

describe("flows wrap without a dangling arrow", () => {
  it("each later step carries its wire, and a step that starts a new line hides it", async () => {
    // happy-dom has no layout: give each step an offsetTop like a 2-line wrap would.
    const tops = [0, 0, 30, 30];
    let n = 0;
    const desc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetTop");
    Object.defineProperty(HTMLElement.prototype, "offsetTop", { configurable: true, get() {
      return (this as HTMLElement).hasAttribute("data-flow-step") ? tops[n++ % tops.length] : 0; } });
    try {
      await mountAs(<AnswerFlow testId="f" nodes={[{ label: "Merge PR #3", state: "waiting" }, { label: "Search filters", state: "now" },
                                                   { label: "Staging", state: "done" }, { label: "Demo ready", state: "todo" }]} />);
      const steps = $$('[data-testid="f"] > [data-flow-step]');
      expect(steps.length).toBe(4);
      expect(steps[0].querySelector(".agent-flow-w")).toBeNull();
      expect(steps.slice(1).every((s) => s.querySelector(".agent-flow-w"))).toBe(true);
      expect(steps.map((s) => s.hasAttribute("data-wrap"))).toEqual([false, false, true, false]);
    } finally {
      if (desc) Object.defineProperty(HTMLElement.prototype, "offsetTop", desc);
    }
  });
});

// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * KG-V2: no console page showed an agent conversation. Agent Studio › Activity › Talks listed
 * conversations by title, and opening one showed the raw actor "executive_console".
 *
 * The conversation view reads back the brief, the agent's answer and what it did. Breaks covered:
 * any raw thread or graph id (athr_…, kgn_…) shown as text — a citation, a quoted id, a bare id;
 * the console's actor shown instead of the signed-in person ("the operator" with nobody signed in);
 * a tool call in tool-speak rather than plain words, or a recorded learning that does not open its
 * node; a long brief that does not fold; markdown (tables, code) shown raw; the footer's counts;
 * a talk row that carries no conversation; the graph's conversation node with no way back to it.
 */

vi.mock("../app/actions", () => ({ loadConversationMessagesAction: vi.fn() }));
(globalThis as Record<string, unknown>).React = React;
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { ConversationView } = await import("../app/agents/conversation-sheet");
const conv = await import("../lib/agent-conversation");
const { parseActivity } = await import("../lib/agents-live-board");

const B3 = "athr_07fbefa3e030461b9418";
const ROOT = "kgn_dfb8460ea1c647b89ec2", TRAIL = "kgn_a78fbb6dde904486b314", PREC = "kgn_3c4ddcc9bce543e6938f";
const SYMBOL = "kgn_464483c51f9c41ec8e1f", B4_CAUSE = "kgn_142e3fd6f2744f7981e8";
const RAW_ID = /\b(?:kgn|athr)_[A-Za-z0-9]+|executive_console/;

let host: HTMLDivElement | null = null;
afterEach(() => { host?.remove(); host = null; });
function render(node: React.ReactElement): HTMLElement {
  host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => root.render(node));
  return host;
}

const BRIEF = [
  "# B3 · Incident: repayments post more than the customer paid", "",
  "**From:** NorthPeak Bank, payments operations (on call) · **Repository:** `northpeak-fineract`", "",
  "**Incident INC-2041 · Severity 2 · opened 06:40**", "",
  "**What we see.** Overnight, the end-of-day batch posted the day's standing-order loan", "repayments.", "",
  "| Loan | Cash received | Interest booked | Principal booked |", "|---|---|---|---|",
  "| L-118204 | 250.00 | 31.40 | 250.00 |", "| L-118377 | 1,200.00 | 96.15 | 1,200.00 |", "",
  "**What we need.**", "- Find the cause and fix it.", "- Add a test that would have caught this.", "",
  "Investigate this incident in the NorthPeak Lending project.",
].join("\n");

const FILE = "fineract-loan/src/main/java/org/apache/fineract/portfolio/loanaccount/domain/transactionprocessor/impl/FineractStyleLoanRepaymentScheduleTransactionProcessor.java";

function wireMessages() {
  return {
    thread_id: B3,
    messages: [
      { id: "amsg_1", role: "user", content: BRIEF, created_at: "2026-10-25T16:04:45Z", tool_calls: [] },
      { id: "amsg_2", role: "assistant", agent_role: "sre", created_at: "2026-10-25T16:09:00Z",
        content: "**Root cause:** the on-time branch never decrements:\n```java\nprincipalPortion = currentInstallment.payPrincipalComponent(transactionDate, transactionAmountRemaining);\n```\n| Loan | Over |\n|---|---|\n| L-118204 | 31.40 |",
        tool_calls: [
          { tool: "kg_precedent", ok: true, args: { query: "repayment posts more than paid" }, refs: [ROOT, TRAIL, PREC] },
          { tool: "list_repositories", ok: true, args: { project: "northpeak-lending" } },
          { tool: "list_repositories", ok: true, args: { project: "northpeak-lending" } },
          { tool: "git_history", ok: false, args: { repo: "northpeak-fineract", search: "repayment" }, error: "no such repo" },
          { tool: "read_code", ok: true, args: { file: FILE } },
        ] },
      { id: "amsg_3", role: "user", created_at: "2026-10-25T16:13:47Z",
        content: "Please record what you learned from this incident (INC-2041) now.", tool_calls: [] },
      { id: "amsg_4", role: "assistant", agent_role: "sre", created_at: "2026-10-25T16:14:45Z",
        content: `Confirmed recorded:\n\n- **Root cause** \`${ROOT}\` (learning:root_cause)\n- **Precedent** [L:${PREC}]\n\nThe governing symbol (\`${SYMBOL}\`) and the earlier talk ${B3} too.`,
        tool_calls: [
          { tool: "record_learning", ok: true, args: { learning_kind: "root_cause", title: "INC-2041: standard-strategy repayments overpost principal+interest" } },
          { tool: "record_learning", ok: true, args: { learning_kind: "precedent", title: "Sequential component-allocation chains silently overpost" } },
          { tool: "kg_search", ok: true, args: { query: "INC-2041" }, refs: [ROOT, TRAIL, PREC] },
        ] },
    ],
  };
}

function wireKnowledge(asked: { kind: string; name: string | null } = { kind: "console", name: null }) {
  return {
    thread_id: B3, title: "B3 · Incident: repayments post more than the customer paid", started_at: "2026-10-25T16:04:45Z",
    agent: { role: "sre", name: "Site Reliability Engineer", agent_id: "agt_ben6rp4pvr" },
    asked_by: asked,
    produced: [
      { id: ROOT, kind: "root_cause", title: "INC-2041: standard-strategy repayments overpost principal+interest" },
      { id: TRAIL, kind: "investigation", title: "INC-2041 investigation trail" },
      { id: PREC, kind: "precedent", title: "Sequential component-allocation chains silently overpost when an intermediate decrement is dropped" },
    ],
    used: [{ id: B4_CAUSE, kind: "root_cause", title: "INC-2088: early repayments overpost", hows: ["consulted"], learned_in: "athr_other" }],
    names: {
      [ROOT]: { title: "INC-2041: standard-strategy repayments overpost principal+interest", kind: "root_cause" },
      [PREC]: { title: "Sequential component-allocation chains silently overpost when an intermediate decrement is dropped", kind: "precedent" },
      [SYMBOL]: { title: "FineractStyleLoanRepaymentScheduleTransactionProcessor.handleTransactionThatIsOnTimePaymentOfInstallment", kind: "symbol" },
    },
    learned: 3, used_earlier: 1,
  };
}

function view(o: { signedIn?: string | null; asked?: { kind: string; name: string | null } } = {}) {
  return render(<ConversationView messages={conv.parseMessages(wireMessages())}
                                  knowledge={conv.parseKnowledge(wireKnowledge(o.asked))}
                                  signedIn={o.signedIn ?? null} orgId={null} />);
}

describe("an agent conversation, read back", () => {
  it("never shows a raw thread or graph id, or the console's actor, as text", () => {
    const el = view({ signedIn: "Sean Example" });
    expect(el.textContent ?? "").not.toMatch(RAW_ID);
    // …nor while the graph's names have not arrived (or cannot be read).
    const bare = render(<ConversationView messages={conv.parseMessages(wireMessages())} knowledge={null} />);
    expect(bare.textContent ?? "").not.toMatch(RAW_ID);
    expect(bare.textContent).toContain("a graph item");
  });

  it("shows a citation and a quoted id as a chip with the item's title that opens it in the graph", () => {
    const el = view();
    const chips = [...el.querySelectorAll<HTMLAnchorElement>("[data-testid=cv-node-chip]")];
    const prec = chips.find((c) => c.textContent?.startsWith("Sequential component-allocation"));
    expect(prec?.getAttribute("href")).toBe(`/?area=knowledge&kg_node=${PREC}`);
    expect(chips.some((c) => c.textContent?.startsWith("FineractStyleLoanRepayment"))).toBe(true);
    expect(chips.some((c) => c.textContent === "INC-2041: standard-strategy repayments overpost principal+interest")).toBe(true);
    // A thread id in an answer is a link to that conversation, never its id.
    const thread = el.querySelector<HTMLAnchorElement>("[data-testid=cv-thread-chip]");
    expect(thread?.textContent).toBe("a conversation");
    expect(thread?.getAttribute("href")).toBe(`/?area=workforce&view=activity&conversation=${B3}`);
  });

  it("heads the conversation with its title, the agent and its Passport, who asked, and when", () => {
    const el = view({ signedIn: "Sean Example" });
    expect(el.querySelector("[data-testid=cv-title]")?.textContent).toBe("B3 · Incident: repayments post more than the customer paid");
    expect(el.querySelector("[data-testid=cv-agent]")?.textContent).toContain("Site Reliability Engineer");
    expect(el.querySelector("[data-testid=cv-passport]")?.textContent).toBe("Passport");
    expect(el.querySelector("[data-testid=cv-asker]")?.textContent).toBe("Asked by Sean Example");
    expect(el.querySelector("[data-testid=cv-started]")?.getAttribute("dateTime")).toBe("2026-10-25T16:04:45Z");
    expect(view().querySelector("[data-testid=cv-asker]")?.textContent).toBe("Asked by the operator");
    expect(view({ asked: { kind: "person", name: "Release Manager" }, signedIn: "Sean Example" })
      .querySelector("[data-testid=cv-asker]")?.textContent).toBe("Asked by Release Manager");
  });

  it("says what each answer did in plain words, and a recorded learning opens its node", () => {
    const el = view();
    const lists = [...el.querySelectorAll("[data-testid=cv-did]")].map((d) =>
      [...d.querySelectorAll("[data-testid=cv-did-line]")].map((l) => l.textContent));
    expect(lists[0]).toEqual([
      "Searched past incidents (kg_precedent) → 3 found",
      "Listed the repositories ×2",
      "Searched commits for “repayment” · failed",
      "Read FineractStyle…Processor.java",
    ]);
    expect(lists[1]).toEqual([
      "Recorded learning: root cause",
      "Recorded learning: precedent",
      "Searched the knowledge graph for “INC-2041” → 3 found",
    ]);
    const learned = [...el.querySelectorAll<HTMLAnchorElement>("[data-testid=cv-did-learning]")];
    expect(learned.map((a) => a.getAttribute("href"))).toEqual([
      `/?area=knowledge&kg_node=${ROOT}`, `/?area=knowledge&kg_node=${PREC}`]);
  });

  it("folds a long brief after twelve lines, and renders markdown tables and code", () => {
    const el = view();
    const ask = el.querySelector("[data-testid=cv-ask]")!;
    expect(ask.textContent).not.toContain("Investigate this incident");
    const more = ask.querySelector<HTMLButtonElement>("[data-testid=cv-show-all]")!;
    expect(more.textContent).toBe("Show all");
    act(() => more.click());
    expect(ask.textContent).toContain("Investigate this incident");
    expect(ask.querySelector("table.md-table")).not.toBeNull();
    const answer = el.querySelector("[data-testid=cv-answer]")!;
    expect(answer.querySelector("pre.md-pre code")?.textContent).toContain("payPrincipalComponent");
    expect(answer.querySelector("table.md-table")).not.toBeNull();
    // A fold never cuts a table in two: it folds before it.
    const table = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "| A | B |", "|---|---|", "| 1 | 2 |", "k", "l"].join("\n");
    expect(conv.foldText(table).shown.split("\n")).toHaveLength(10);
    // The short follow-up does not fold.
    expect(el.querySelectorAll("[data-testid=cv-show-all]")).toHaveLength(1);
  });

  it("says in the footer what it wrote to the graph and what earlier learning it used", () => {
    const el = view();
    expect(el.querySelector("[data-testid=cv-foot-line]")?.textContent)
      .toBe("Wrote to the knowledge graph: 3 learnings · used 1 earlier learning");
    expect(conv.footerWords({ learned: 1, usedEarlier: 3 })).toBe("Wrote to the knowledge graph: 1 learning · used 3 earlier learnings");
  });
});

describe("the ways in", () => {
  it("a talk row carries its conversation", () => {
    const page = parseActivity({ items: [{ act_id: "act_1", kind: "chat", links: { thread_id: B3 } }] });
    expect(page.items[0].threadId).toBe(B3);
  });

  it("links a conversation by the console's convention, and refuses what is not a thread", () => {
    expect(conv.conversationHref(B3, "org_1")).toBe(`/?org=org_1&area=workforce&view=activity&conversation=${B3}`);
    expect(conv.isThreadId(B3)).toBe(true);
    expect(conv.isThreadId("javascript:alert(1)")).toBe(false);
    // A link from a page on another project of the org stays in that project.
    expect(conv.conversationHref(B3, null, "northpeak-lending"))
      .toBe(`/?area=workforce&view=activity&conversation=${B3}&project_id=northpeak-lending`);
    expect(conv.inThisProject(`/?area=knowledge&kg_node=${PREC}`, "northpeak-lending"))
      .toBe(`/?area=knowledge&kg_node=${PREC}&project_id=northpeak-lending`);
    expect(conv.inThisProject("/?area=knowledge&project_id=other", "northpeak-lending")).toBe("/?area=knowledge&project_id=other");
  });

  it("the graph's conversation node offers Open conversation, and a build's run does not", async () => {
    const { PanelActions } = await import("../app/knowledge/node-panel");
    const node = (key: string) => ({ id: "kgn_run", key, kind: "run", label: "B3 · Incident", layer: "learning" }) as never;
    const props = { w: { projectId: "p", actor: "a" }, detail: null, archived: false, rulesLocked: null, onChanged: () => {}, orgId: "org_1" };
    const el = render(<PanelActions {...props} node={node(`run:${B3}`)} />);
    const open = el.querySelector<HTMLAnchorElement>("[data-testid=kg-panel-open-conversation]");
    expect(open?.textContent).toBe("Open conversation");
    expect(open?.getAttribute("href")).toBe(`/?org=org_1&area=workforce&view=activity&conversation=${B3}`);
    const build = render(<PanelActions {...props} node={node("run:crun_123")} />);
    expect(build.querySelector("[data-testid=kg-panel-open-conversation]")).toBeNull();
  });
});

// @vitest-environment happy-dom
/**
 * THE WORK TREE (BOSSAPP-830/831): one tree of work items with status chips and a blocked-by
 * badge; a row opens in place with Mark done, the blocker picker, the stage editor (JSON, said
 * wrong before it is sent) and the stage timeline.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WorkTreeView } from "../app/projects/work-tree";
import { flatten, parseTree, readPolicy } from "../lib/work-items";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

const TREE = {
  count: 3, by_status: { active: 1, blocked: 1, todo: 1 }, goals: [], labels: [{ name: "payments", color: "gray", items: 1 }],
  items: [
    { id: "w1", human_key: "WI-1", title: "Checkout", status: "active", parent_id: null, labels: [{ name: "payments", color: "gray" }],
      blocked_by: [], assignee: "agent:pm", subject_type: "item",
      children: [
        { id: "w2", human_key: "WI-2", title: "Card form", status: "blocked", parent_id: "w1", labels: [],
          blocked_by: [{ id: "w3", human_key: "WI-3", title: "Payments API", status: "todo" }],
          assignee: "agent:frontend", subject_type: "item", children: [] },
      ] },
    { id: "w3", human_key: "WI-3", title: "Payments API", status: "todo", parent_id: null, labels: [], blocked_by: [],
      subject_type: "workflow_run", children: [] },
  ],
};
const ITEM = {
  item: { ...TREE.items[0].children[0], description: "The card form",
    execution_policy: { stages: [{ kind: "review", participants: ["agent:qa"], comment_required: false, max_rounds: 3 }] },
    execution_state: { review: { feedback: "tighten copy", round: 2 } },
    relations: [{ kind: "blocked_by", item: { id: "w3", human_key: "WI-3", title: "Payments API", status: "todo" } }],
    runs: [{ kind: "workflow_run", id: "wfr_1", status: "paused" }],
    stages: [
      { stage_index: 0, stage_kind: "review", round: 1, event: "raised", actor: "boss", comment: null, created_at: null },
      { stage_index: 0, stage_kind: "review", round: 1, event: "rejected", actor: "person:ceo", comment: "tighten copy", created_at: null },
    ] },
};

afterEach(() => { unmount(); vi.restoreAllMocks(); });

function serve(calls: { url: string; method: string; body: unknown }[]) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ url: String(url), method, body: init?.body ? JSON.parse(String(init.body)) : null });
    const body = String(url).startsWith("/v1/work-items/tree") ? TREE
      : method === "GET" ? ITEM : { status: "in_review", decision_id: "dec_9" };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

describe("the work tree", () => {
  it("parses the tree and reads a policy typed as JSON", () => {
    const tree = parseTree(TREE);
    expect(flatten(tree.items).map(({ node, depth }) => `${node.human_key}@${depth}`)).toEqual(["WI-1@0", "WI-2@1", "WI-3@0"]);
    expect(tree.items[0].children[0].blocked_by[0].human_key).toBe("WI-3");
    expect(readPolicy("").policy).toBeNull();
    expect(readPolicy("{").error).toBe("Not valid JSON");
    expect(readPolicy('{"stages":[{"kind":"vote","participants":["x"]}]}').error).toBe("Stage 1: kind is review or approval");
    expect(readPolicy('{"stages":[{"kind":"review","participants":[]}]}').error).toBe("Stage 1: name a participant");
    expect(readPolicy('[{"kind":"approval","participants":["person:ceo"]}]').policy?.stages).toHaveLength(1);
  });

  it("draws rows with status chips and the blocked-by badge, and opens one in place", async () => {
    const calls: { url: string; method: string; body: unknown }[] = [];
    serve(calls);
    await mount(<WorkTreeView projectId="p1" />);
    expect($$('[role="treeitem"]')).toHaveLength(3);
    const row = $('[data-testid="wt-row-w2"]')!;
    expect(row.querySelector('[data-testid="wt-status"]')?.textContent).toBe("Blocked");
    expect(row.querySelector('[data-testid="wt-blocked"]')?.textContent).toBe("Blocked by WI-3");
    // Folding the epic hides its child.
    ($('[data-testid="wt-row-w1"] .wt-fold') as HTMLButtonElement).click();
    await settle();
    expect($('[data-testid="wt-row-w2"]')).toBeNull();
    ($('[data-testid="wt-row-w1"] .wt-fold') as HTMLButtonElement).click();
    await settle();
    ($('[data-testid="wt-row-w2"] .wt-open') as HTMLButtonElement).click();
    await settle();
    expect($('[data-testid="wt-panel"]')).not.toBeNull();
    expect($('[data-testid="wt-feedback"]')?.textContent).toBe("Sent back (round 2): tighten copy");
    expect($$('[data-testid="wt-timeline"] li').map((li) => li.querySelector(".wt-ev")?.textContent)).toEqual(["Asked", "Sent back"]);
    // A bad policy is said before anything is sent.
    const box = $('[data-testid="wt-policy"]') as HTMLTextAreaElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
    setter.call(box, "{oops");
    box.dispatchEvent(new Event("input", { bubbles: true }));
    await settle();
    expect($('[data-testid="wt-policy-error"]')?.textContent).toBe("Not valid JSON");
    expect(($('[data-testid="wt-policy-save"]') as HTMLButtonElement).disabled).toBe(true);
    // Mark done goes through the done door, which may answer In review.
    ($('[data-testid="wt-done"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.some((c) => c.method === "POST" && c.url === "/v1/work-items/w2/complete")).toBe(true);
    expect($('[data-testid="wt-said"]')?.textContent).toBe("In review — its first stage is in Needs you.");
  });

  // BOSSAPP-1038: the graph and the work list are different stores. A run or PR that only
  // lives on the list must still be a row, and a run the graph already has must not be drawn twice.
  it("draws list-store runs beside the graph and does not duplicate one the graph already has", async () => {
    const calls: { url: string; method: string; body: unknown }[] = [];
    const opened: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push({ url: String(url), method, body: init?.body ? JSON.parse(String(init.body)) : null });
      const body = String(url).startsWith("/v1/work-items/tree")
        ? { count: 1, by_status: { active: 1 }, goals: [], labels: [], items: [
            { id: "w_graph", human_key: "WI-9", title: "Graph only", status: "active",
              subject_type: "workflow_run", subject_id: "wfr_linked", children: [] },
          ] }
        : {};
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    await mount(<WorkTreeView projectId="p1" onOpenListed={(id) => opened.push(id)} listed={[
      { id: "wfr_linked", label: "Already in the graph", state: "running", source: "workflow_run" },
      { id: "wfr_new", label: "Unlinked run", title: "Unlinked run", state: "failed", source: "workflow_run" },
    ]} />);
    expect($('[data-testid="wt-empty"]')).toBeNull();
    expect($('[data-testid="wt-row-w_graph"]')).not.toBeNull();
    expect(document.body.textContent).toContain("Unlinked run");
    expect(document.body.textContent).not.toContain("Already in the graph");
    const chip = $('[data-testid="wt-row-run:wfr_new"] [data-testid="wt-status"]');
    expect(chip?.textContent).toBe("Failed");
    expect(document.body.textContent).not.toContain("Cancelled");
    ($('[data-testid="wt-row-run:wfr_new"] .wt-open') as HTMLButtonElement).click();
    await settle();
    expect($('[data-testid="wt-panel"]')).toBeNull();
    expect(opened).toEqual(["wfr_new"]);
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    expect(calls.some((c) => c.method === "GET" && c.url.includes("/v1/work-items/") && !c.url.includes("/tree"))).toBe(false);
  });
});

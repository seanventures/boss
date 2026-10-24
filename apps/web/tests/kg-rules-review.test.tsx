// @vitest-environment happy-dom
/**
 * RULES REVIEW (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.7) on the composed Fineract rules: the queue
 * with evidence chips that focus the code, Approve / Edit (a new version, approved in the act) /
 * Reject with a note, bulk approve behind a confirm, the filters, and the Facts sub-tab.
 * Plus the Learned-so-far strip (§4.4): it lists a run's learnings as links into the Graph, says
 * nothing before the live route exists, and reads nothing while its area is off screen.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LearnedStrip } from "../app/knowledge/learned-strip";
import { RulesReview } from "../app/knowledge/rules-review";
import { AreaShownContext } from "../lib/area-shown";
import { FX, calls, mockKgFetch } from "./_helpers/kg-fetch";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), usePathname: () => "/", useSearchParams: () => new URLSearchParams() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); vi.useRealTimers(); });

const proposed = FX.rules.items.filter((r) => r.status === "proposed");
const card = (id: string) => $(`[data-rule="${id}"]`)!;
const press = async (el: Element | null) => { await act(async () => (el as HTMLElement).click()); await settle(); };
function type(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

function Review(p: Partial<React.ComponentProps<typeof RulesReview>> = {}) {
  return <RulesReview projectId="northpeak-bank-b" actor="ceo" onFocusNode={() => {}} {...p} />;
}

describe("Rules review", () => {
  it("lists the proposed rules with category, confidence, who proposed them and their evidence", async () => {
    mockKgFetch({ "/view/rules$": { body: { ...FX.rules, items: proposed } } });
    await mount(<Review />);
    expect(calls.some((c) => c.includes("/view/rules") && c.includes("status=proposed"))).toBe(true);
    expect($$('[data-testid="kg-rule-card"]')).toHaveLength(proposed.length);
    const first = card(proposed[0].node.id);
    expect(first.textContent).toContain(proposed[0].title);
    expect(first.textContent).toContain(`${Math.round(proposed[0].confidence * 100)}% confident`);
    expect(first.textContent).toContain("Proposed by Scout");
    const ev = first.querySelector('[data-testid="kg-evidence-code"]') as HTMLAnchorElement;
    const e = proposed[0].evidence[0];
    expect(ev.href).toContain(`#L${e.lines![0]}-L${e.lines![1]}`);
    expect($('[data-testid="kg-review-status-proposed"]')?.textContent).toBe("Proposed 4");
  });

  it("an evidence chip focuses the symbol in the graph", async () => {
    mockKgFetch({ "/view/rules$": { body: { ...FX.rules, items: proposed } } });
    const onFocusNode = vi.fn();
    await mount(<Review onFocusNode={onFocusNode} />);
    await press(card(proposed[0].node.id).querySelector('[data-testid="kg-evidence-focus"]'));
    expect(onFocusNode).toHaveBeenCalledWith(proposed[0].evidence[0].symbol_id);
  });

  it("Approve sends the review with step-up confirmed; the queue reloads", async () => {
    const fn = mockKgFetch({ "/view/rules$": { body: { ...FX.rules, items: proposed } } });
    await mount(<Review />);
    await press(card(proposed[0].node.id).querySelector('[data-testid="kg-rule-approve"]'));
    const post = fn.mock.calls.find(([u, i]) => String(u).includes(`/rules/${proposed[0].node.id}/approve`) && i?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({ project_id: "northpeak-bank-b", actor: "ceo", step_up: "confirmed" });
    expect($('[data-testid="kg-review-toast"]')?.textContent).toMatch(/Approved/);
    expect(calls.filter((c) => c.startsWith("GET /v1/knowledge/view/rules")).length).toBe(2);
  });

  it("Edit is an inline form that saves version n+1, approved in the same act", async () => {
    const fn = mockKgFetch({ "/view/rules$": { body: { ...FX.rules, items: proposed } } });
    await mount(<Review />);
    const c = card(proposed[1].node.id);
    await press(c.querySelector('[data-testid="kg-rule-edit-open"]'));
    await act(async () => type(c.querySelector('[data-testid="kg-edit-statement"]') as HTMLTextAreaElement, "A transaction cannot be dated before the disbursement date."));
    await act(async () => { (c.querySelector('[data-testid="kg-edit-category"]') as HTMLSelectElement).value = "state_transition"; c.querySelector('[data-testid="kg-edit-category"]')!.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(c.textContent).toContain("Saving makes version 2");
    await press(c.querySelector('[data-testid="kg-edit-save"]'));
    const post = fn.mock.calls.find(([u]) => String(u).includes(`/rules/${proposed[1].node.id}/edit`));
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
      statement: "A transaction cannot be dated before the disbursement date.", category: "state_transition", step_up: "confirmed",
    });
    expect($('[data-testid="kg-review-toast"]')?.textContent).toContain("version 2");
  });

  it("Reject asks for an optional note and sends it", async () => {
    const fn = mockKgFetch({ "/view/rules$": { body: { ...FX.rules, items: proposed } } });
    await mount(<Review />);
    const c = card(proposed[2].node.id);
    await press(c.querySelector('[data-testid="kg-rule-reject"]'));
    await act(async () => type(c.querySelector('[data-testid="kg-reject-note"]') as HTMLInputElement, "Only for tranche loans"));
    await press(c.querySelector('[data-testid="kg-reject-yes"]'));
    const post = fn.mock.calls.find(([u]) => String(u).includes(`/rules/${proposed[2].node.id}/reject`));
    expect(JSON.parse(String(post?.[1]?.body)).note).toBe("Only for tranche loans");
  });

  it("bulk approve shows the count and asks before it sends", async () => {
    mockKgFetch({ "/view/rules$": { body: { ...FX.rules, items: proposed } } });
    await mount(<Review />);
    for (const r of proposed.slice(0, 3)) {
      await act(async () => (card(r.node.id).querySelector('[data-testid="kg-rule-pick"]') as HTMLInputElement).click());
    }
    expect($('[data-testid="kg-bulk"]')?.textContent).toBe("Approve 3 selected");
    await press($('[data-testid="kg-bulk"]'));
    expect($('[data-testid="kg-bulk-confirm"]')?.textContent).toContain("Approve 3 rules?");
    expect(calls.some((c) => c.startsWith("POST"))).toBe(false);
    await press($('[data-testid="kg-bulk-yes"]'));
    expect(calls.filter((c) => c.startsWith("POST") && c.endsWith("/approve"))).toHaveLength(3);
    expect($('[data-testid="kg-review-toast"]')?.textContent).toContain("3 rules approved");
  });

  it("filters by status, scan and area re-query; an empty queue is a designed card", async () => {
    mockKgFetch();
    await mount(<Review initialScan="scan_fixture_loan_1" />);
    expect(calls.some((c) => c.includes("scan_id=scan_fixture_loan_1"))).toBe(true);
    await press($('[data-testid="kg-review-status-all"]'));
    expect(calls.some((c) => c.includes("/view/rules?") && !c.includes("status="))).toBe(true);
    unmount();
    mockKgFetch({ "/view/rules$": { body: { items: [], counts: { proposed: 0, approved: 0, rejected: 0, superseded: 0 }, scan_id: null } } });
    await mount(<Review />);
    expect($('[data-testid="kg-review-empty"]')?.textContent).toContain("Nothing waiting for review");
  });

  it("the Facts sub-tab is the old promotion queue", async () => {
    mockKgFetch();
    await mount(<Review initialSub="facts" />);
    expect($('[data-testid="kg-fact-card"]')?.textContent).toContain("Disbursement dates are validated against the office calendar");
    await press($('[data-testid="kg-fact-promote"]'));
    expect(calls.some((c) => c.startsWith("POST /v1/knowledge/facts/fact-1/promote"))).toBe(true);
    expect($('[data-testid="kg-facts-empty"]')).not.toBeNull();
  });
});

describe("Learned so far", () => {
  it("lists the run's learnings and what it consulted, each a link into the Graph", async () => {
    mockKgFetch({ "/view/live$": { body: FX.live } });
    await mount(<LearnedStrip projectId="northpeak-bank-b" actor="ceo" runId="run_fixture_builder_1" live={false} orgId="o1" />);
    const items = $$('[data-testid="kg-learned-item"]');
    expect(items.length).toBeGreaterThanOrEqual(FX.live.learnings.length);
    expect(items[0].getAttribute("href")).toMatch(/^\/\?org=o1&area=knowledge&kg_node=kgn_fixture_/);
    expect($('[data-testid="kg-learned-count"]')?.textContent).toMatch(/learned/);
    expect(calls.some((c) => c.includes("/view/live") && c.includes("run=run_fixture_builder_1"))).toBe(true);
  });

  it("draws nothing before the live route exists, and nothing for a finished run that learned nothing", async () => {
    mockKgFetch({ "/view/live$": { status: 404, body: { detail: "Not Found" } } });
    await mount(<LearnedStrip projectId="p" actor="ceo" runId="r1" live />);
    expect($('[data-testid="kg-learned-strip"]')).toBeNull();
    unmount();
    mockKgFetch({ "/view/live$": { body: { ...FX.live, learnings: [], rules: [], edges: [], active_runs: 0 } } });
    await mount(<LearnedStrip projectId="p" actor="ceo" runId="r1" live={false} />);
    expect($('[data-testid="kg-learned-strip"]')).toBeNull();
  });

  it("a live run that has learned nothing yet says so", async () => {
    mockKgFetch({ "/view/live$": { body: { ...FX.live, learnings: [], rules: [], edges: [], active_runs: 1 } } });
    await mount(<LearnedStrip projectId="p" actor="ceo" runId="r1" live />);
    expect($('[data-testid="kg-strip-empty"]')?.textContent).toBe("Nothing learned yet");
  });

  it("reads nothing while its area is off screen, and polls only while the run is live", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockKgFetch({ "/view/live$": { body: { ...FX.live, active_runs: 1, poll_after_ms: 3000 } } });
    await mount(<AreaShownContext.Provider value={false}><LearnedStrip projectId="p" actor="ceo" runId="r1" live /></AreaShownContext.Provider>);
    await act(async () => { vi.advanceTimersByTime(10000); });
    expect(calls.filter((c) => c.includes("/view/live"))).toHaveLength(0);
    unmount();
    mockKgFetch({ "/view/live$": { body: { ...FX.live, active_runs: 1, poll_after_ms: 3000 } } });
    await mount(<LearnedStrip projectId="p" actor="ceo" runId="r1" live />);
    await act(async () => { vi.advanceTimersByTime(3100); });
    await settle();
    const n = calls.filter((c) => c.includes("/view/live")).length;
    expect(n).toBeGreaterThanOrEqual(2);
    expect(calls.filter((c) => c.includes("/view/live"))[1]).toContain(`since=${encodeURIComponent(FX.live.cursor)}`);
  });
});

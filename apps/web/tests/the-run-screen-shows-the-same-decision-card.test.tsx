// @vitest-environment happy-dom
/**
 * THE SAME CARD AT THE STEP THAT WAITS (approvals wireframe B).
 *
 * The Workflow run screen's waiting step had a "Review and decide" link to Approvals and the
 * step's preview; the decision looked different in the two places. It now reads the decision
 * and draws the Approvals card itself (title, status line, answers, links), with the link kept
 * as the fallback while it loads or when the decision cannot be read.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));
vi.mock("../app/submit-button", () => ({
  SubmitButton: (props: React.ButtonHTMLAttributes<HTMLButtonElement>) =>
    React.createElement("button", { ...props, type: props.type ?? "submit" }, props.children),
}));
vi.mock("../app/client-relative-time", () => ({
  ClientRelativeTime: ({ iso }: { iso?: string }) => React.createElement("time", null, iso ?? ""),
}));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { ReviewAndDecide } = await import("../app/delivery-workflows-ui");

const STEP = {
  step_id: "merge", kind: "capability", status: "waiting", capability: "merge", output: {}, evidence: [],
  error: null, approval: { approver_role: "release_manager" }, due_at: null, timeout_applied: null, attempt: 1,
  started_at: null, completed_at: null, decision_id: "dec_m1", decision_status: "pending",
};
const DECISION = {
  id: "dec_m1", status: "pending", attention: "needs_action", question: "Merge change #4?", risk_tier: 2,
  recommendation: { action: "review_pull_request", pull_request_url: "http://boss-local/local_hd/helpdesk/pull/4",
                    title: "Ticket filters", workflow_run_id: "wfr_7", run_no: 7 },
  options: [], cost_summary: {}, created_at: "2026-11-21T10:00:00Z", updated_at: "2026-11-21T10:00:00Z",
  allowed_actions: [
    { action: "approve", label: "Approve and merge", tone: "approve", effect: "", needs: null },
    { action: "reject", label: "Keep blocked", tone: "reject", effect: "", needs: null },
  ],
};

let host: HTMLElement;
let root: Root;
let answer: { ok: boolean; body: unknown };
const asked: string[] = [];
beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  asked.length = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    asked.push(String(url));
    return new Response(JSON.stringify(answer.body), { status: answer.ok ? 200 : 404 });
  }));
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

async function mount() {
  await act(async () => {
    root.render(<ReviewAndDecide step={STEP as never} projectId="helpdesk" actor="ceo" />);
  });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

describe("the waiting step draws the Approvals card", () => {
  it("reads the decision and shows its three lines, answers and links", async () => {
    answer = { ok: true, body: DECISION };
    await mount();
    expect(asked[0]).toBe("/v1/decisions/dec_m1?project_id=helpdesk");
    const card = host.querySelector('[data-testid="dw-step-decision"] [data-testid="decision-row"]')!;
    expect(card).not.toBeNull();
    expect(card.querySelector('[data-testid="decision-title"]')!.textContent).toBe("Merge change #4 · Ticket filters");
    expect(card.querySelector('[data-testid="decision-status"]')!.textContent).toBe("✓ Ready · Checks ›");
    const buttons = [...card.querySelectorAll(".dq-card-act form > button")].map((b) => b.textContent);
    expect(buttons).toEqual(["Approve and merge", "Keep blocked"]);
    expect((card.querySelector('input[name="actor"]') as HTMLInputElement).value).toBe("ceo");
    expect(card.querySelector('[data-testid="decision-link-run"]')!.textContent).toBe("Workflow #7");
    expect(host.querySelector('[data-testid="dw-step-review"]')).toBeNull();
  });

  it("keeps the Review and decide link when the decision cannot be read or is already answered", async () => {
    answer = { ok: false, body: {} };
    await mount();
    expect(host.querySelector('[data-testid="dw-step-review"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="decision-row"]')).toBeNull();

    act(() => root.unmount());
    root = createRoot(host);
    answer = { ok: true, body: { ...DECISION, status: "approved", attention: "resolved", allowed_actions: [] } };
    await mount();
    expect(host.querySelector('[data-testid="decision-row"]')).toBeNull();
    expect(host.querySelector('[data-testid="dw-step-review"]')).not.toBeNull();
  });
});

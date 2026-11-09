// @vitest-environment happy-dom
/**
 * A workflow gate's card names its case and shows what it is deciding (engine
 * workflows._gate_card → recommendation.gate).
 *
 * Two AML alerts waiting at one gate read identically ("Decide this alert… (2)" under a
 * "resume workflow" subtitle), the why said the AML Analyst's recommendation was "above" with
 * nothing above it, and it named the run and step by their ids, all in a monospace chip.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DecisionFeedItem } from "../lib/decisions";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));
vi.mock("../app/actions", () => ({
  createDecisionDigestAction: vi.fn(), resolveDecisionAction: vi.fn(),
}));
vi.mock("../app/submit-button", () => ({
  SubmitButton: (props: React.ButtonHTMLAttributes<HTMLButtonElement>) =>
    React.createElement("button", { ...props, type: props.type ?? "submit" }, props.children),
}));
vi.mock("../app/client-relative-time", () => ({
  ClientRelativeTime: ({ iso }: { iso?: string }) => React.createElement("time", null, iso ?? ""),
}));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { DecisionInbox, reasonInWords } = await import("../app/decision-inbox-ui");

const GUIDANCE = "The AML Analyst's recommendation, its confidence and the evidence and "
  + "precedent it cites are above.";

function gate(id: string, alert: string, summary: string): DecisionFeedItem {
  return {
    id, status: "pending", riskTier: 2, defaultAction: "hold",
    question: "Decide this alert: close it as a false positive, or escalate it?",
    reason: `${GUIDANCE} The AML Alert Investigation run for ${alert} is waiting here for your `
      + "answer. The AML Analyst (Sift) produced what you are reviewing, at the “AML analysis” "
      + "step.",
    recommendation: {
      action: "resume_workflow", workflow_run_id: `wfr_${id}`, step_id: "analyst_decision",
      reason_label: "Decide", reason_prompt: "Your determination and why",
      gate: { subject: alert, workflow_name: "AML Alert Investigation", summary,
              produced_by_title: "AML analysis", produced_by_agent: "AML Analyst (Sift)" },
    },
    options: [],
    allowedActions: [
      { action: "approve", label: "Decide", tone: "approve", effect: "", needs: "note" },
      { action: "reject", label: "Stop the run", tone: "reject", effect: "", needs: null },
    ],
    costSummary: {}, createdAt: "2026-11-02T06:36:00Z", updatedAt: "2026-11-02T06:36:00Z",
  } as unknown as DecisionFeedItem;
}

const THREE = "ALERT-3 · STRUCT-02 on ACC-0047 (Quentin Marlow)";
const TWO = "ALERT-2 · FANOUT-01 on ACC-0013 (Harbor Point Catering LLC)";

let host: HTMLElement;
let root: Root;
beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function mount(items: DecisionFeedItem[]) {
  act(() => root.render(
    <DecisionInbox feed={{ projectId: "p", apiReachable: true, items }}
                   tasks={[]} roles={[]} projectId="p" actorId="ceo" orgId="org_1" />));
  return [...host.querySelectorAll('[data-testid="decision-row"]')];
}

describe("a workflow gate's card", () => {
  it("names its case, so two alerts at one gate read apart", () => {
    const rows = mount([
      gate("a3", THREE, "Recommends ESCALATE for ALERT-3 (confidence 0.93): structured cash."),
      gate("a2", TWO, "Recommends CLOSE AS A FALSE POSITIVE for ALERT-2 (confidence 0.86)."),
    ]);
    const subtitles = rows.map((r) => r.querySelector('[data-testid="decision-subtitle"]')!
      .textContent);
    expect(subtitles).toEqual([`AML Alert Investigation, ${THREE}`,
                               `AML Alert Investigation, ${TWO}`]);
    for (const row of rows) {
      expect(row.querySelector(".dq-title")!.textContent).not.toMatch(/\(\d+\)$/);
      expect(row.textContent).not.toMatch(/resume workflow/i);
      expect(row.querySelector(".dq-impact")!.textContent).toBe("workflow decision");
    }
  });

  it("shows what the step before produced, and who, above the why", () => {
    const [row] = mount([gate("a3", THREE, "Recommends ESCALATE for ALERT-3 (confidence 0.93).")]);
    const summary = row.querySelector('[data-testid="decision-gate-summary"]')!;
    expect(summary.textContent).toContain("What the AML Analyst (Sift) produced");
    expect(summary.textContent).toContain("Recommends ESCALATE for ALERT-3 (confidence 0.93).");
    const html = row.innerHTML;
    expect(html.indexOf("decision-gate-summary")).toBeLessThan(html.indexOf("dq-why"));
    const open = row.querySelector('[data-testid="decision-open-run"]') as HTMLAnchorElement;
    expect(open.textContent).toBe("Open the run");
    expect(open.getAttribute("href")).toBe("/?org=org_1&area=delivery_workflows&wfr=wfr_a3");
  });

  it("says why in prose with no ids, and keeps the reason box", () => {
    const [row] = mount([gate("a3", THREE, "Recommends ESCALATE.")]);
    const why = row.querySelector("p.dq-why")!;
    expect(why.textContent).toContain(`The AML Alert Investigation run for ${THREE} is waiting`);
    expect(why.classList.contains("dq-ev")).toBe(false);
    // The visible text carries no run id or step key (the link's href may).
    expect(row.textContent).not.toMatch(/wfr_|analyst_decision|aml_analysis/);
    expect(row.querySelector('[data-testid="decision-reasoned-approve"] textarea')).not.toBeNull();
    expect([...row.querySelectorAll("button")].map((b) => b.textContent))
      .toEqual(expect.arrayContaining(["Decide", "Stop the run"]));
  });

  it("drops the id sentences an older gate wrote", () => {
    expect(reasonInWords("Review the draft. Workflow wfr_e6ebafcb3bae4f19b2e8 is waiting at "
      + "decision step sar_filing. It was produced by step 'aml_analysis'."))
      .toBe("Review the draft.");
  });
});

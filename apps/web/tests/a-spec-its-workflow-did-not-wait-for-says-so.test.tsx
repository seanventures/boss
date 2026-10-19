// @vitest-environment happy-dom
/**
 * A PRD card whose workflow's build is already running says so, and offers only Acknowledge or
 * Reject (engine spec_gate.py). wfr_723213d8: the run went past "Approve the spec" and built;
 * the card still read "Approving publishes the PRD and starts a governed project" — a second
 * build of work already building.
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
const { DecisionInbox } = await import("../app/decision-inbox-ui");

const SAID = "The workflow did not wait for this spec: its build crun_e18 is already running.";

function prd(skipped: boolean): DecisionFeedItem {
  return {
    id: "dec_prd", status: "pending", riskTier: 2, kind: "general",
    question: "Publish the PRD the panel drafted?",
    recommendation: {
      action: "execute_agent_tool", tool: "publish_document", start_code_task: true,
      ...(skipped ? { spec_gate_skipped: { run_id: "wfr_7232", build_run_id: "crun_e18",
                                           said: SAID } } : {}),
    },
    options: [],
    allowedActions: skipped ? [
      { action: "approve", label: "Acknowledge", tone: "approve", effect: "Starts nothing", needs: null },
      { action: "reject", label: "Reject", tone: "reject", effect: "", needs: null },
    ] : [
      { action: "approve", label: "Approve", tone: "approve", effect: "", needs: null },
      { action: "reject", label: "Reject", tone: "reject", effect: "", needs: null },
    ],
    costSummary: {}, createdAt: "2026-10-19T00:00:00Z", updatedAt: "2026-10-19T00:00:00Z",
  } as unknown as DecisionFeedItem;
}

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

function card(skipped: boolean) {
  act(() => root.render(
    <DecisionInbox feed={{ projectId: "p", apiReachable: true, items: [prd(skipped)] }}
                   tasks={[]} roles={[]} projectId="p" actorId="ceo" />));
  return host.querySelector('[data-testid="decision-row"]')!;
}

describe("a PRD card whose build is already running", () => {
  it("says the build is running, and that approving starts nothing", () => {
    const row = card(true);
    expect(row.querySelector(".dq-effect")!.textContent).toBe(SAID);
    expect(row.textContent).not.toContain("starts a governed project");
    // Approving is not sent as work starting ("the project starts in a few seconds").
    expect(row.querySelector('input[name="starts_work"]')).toBeNull();
    const labels = [...row.querySelectorAll(".dq-actions button[name=\"action\"]")].map((b) => b.textContent);
    expect(labels).toEqual(["Acknowledge", "Reject"]);
  });

  it("an ordinary PRD card still says what approving starts", () => {
    const row = card(false);
    expect(row.querySelector(".dq-effect")!.textContent).toContain("starts a governed project");
    expect(row.querySelector('input[name="starts_work"]')).not.toBeNull();
  });
});

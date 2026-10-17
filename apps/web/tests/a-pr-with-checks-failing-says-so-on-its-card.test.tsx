// @vitest-environment happy-dom
/**
 * A pull request that opened with its checks failing says so on its merge card, first and
 * security first, and its merge is a person's stated decision (engine checks_failing.py).
 *
 * The build's checks still failed after its repair rounds (HelpDesk AI: one SSRF finding), so
 * the PR opened as a draft labelled `checks failing` instead of the build waiting for a waiver.
 * The card must not let a merge happen silently: "Merge with checks failing" is drawn with a
 * reason box and sends a forced merge; a plain card is unchanged.
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
const { checksFailingOf } = await import("../app/checks-failing");

const SSRF = "HIGH: POST /api/fetch-preview fetches any URL the caller gives it (SSRF)";
const CHECKS = {
  gates: [
    { gate: "written_tests_pass", label: "New tests pass", security: false,
      detail: "2 of 14 tests fail" },
    { gate: "security_clean", label: "Security clean", security: true },
  ],
  findings: [
    { gate: "code_review_passed", stage: "review_code", security: false, severity: "major",
      where: "server/tickets.ts:14", text: "the ticket list ignores the status filter" },
    { gate: "security_clean", stage: "security_check", security: true, severity: "finding",
      text: SSRF },
  ],
  security: true, draft: true, label: "checks failing",
};

function card(checks: Record<string, unknown> | null): DecisionFeedItem {
  return {
    id: "dec_pr", status: "pending", riskTier: 2,
    question: checks ? "Checks failing — decide the merge: HelpDesk AI (PR #9)"
                     : "Approve code change: HelpDesk AI (PR #9)",
    recommendation: {
      action: "review_pull_request", pull_request_url: "https://github.com/acme/web/pull/9",
      ...(checks ? { checks_failing: checks } : {}),
    },
    options: [],
    allowedActions: checks ? [
      { action: "approve", label: "Merge with checks failing", tone: "reject",
        effect: "Merges this pull request although BOSS's checks are failing.", needs: "note" },
      { action: "reject", label: "Keep blocked", tone: "reject", effect: "", needs: null },
    ] : [
      { action: "approve", label: "Approve and merge", tone: "approve", effect: "", needs: null },
      { action: "reject", label: "Keep blocked", tone: "reject", effect: "", needs: null },
    ],
    costSummary: {}, createdAt: "2026-10-17T00:00:00Z", updatedAt: "2026-10-17T00:00:00Z",
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

function mount(item: DecisionFeedItem) {
  act(() => root.render(
    <DecisionInbox feed={{ projectId: "p", apiReachable: true, items: [item] }}
                   tasks={[]} roles={[]} projectId="p" actorId="ceo" />));
  return host.querySelector('[data-testid="decision-row"]')!;
}

describe("a merge card whose checks are failing", () => {
  it("leads with the failing checks, security first, every finding in full", () => {
    const row = mount(card(CHECKS));
    const banner = row.querySelector('[data-testid="decision-checks-failing"]')!;
    expect(banner).not.toBeNull();
    expect(banner.getAttribute("role")).toBe("alert");
    expect(row.querySelector('[data-testid="decision-checks-security"]')!.textContent)
      .toBe("security finding open");
    const gates = [...banner.querySelectorAll('[data-testid="decision-checks-gates"] li')];
    expect(gates[0].textContent).toContain("Security clean");
    expect(gates[1].textContent).toContain("2 of 14 tests fail");
    const findings = banner.querySelector('[data-testid="decision-checks-findings"]')!;
    expect(findings.hasAttribute("open")).toBe(true);
    const items = [...findings.querySelectorAll("li")].map((li) => li.textContent ?? "");
    expect(items[0]).toContain(SSRF);
    expect(items[1]).toContain("the ticket list ignores the status filter");
    // Before the buttons, after the title.
    const html = row.innerHTML;
    expect(html.indexOf("decision-checks-failing")).toBeGreaterThan(html.indexOf("dq-title"));
  });

  it("asks for a reason and sends a forced merge; Keep blocked stays one press", () => {
    const row = mount(card(CHECKS));
    const reasoned = row.querySelector('[data-testid="decision-reasoned-approve"]')!;
    expect(reasoned).not.toBeNull();
    expect((reasoned.querySelector('input[name="force"]') as HTMLInputElement).value).toBe("true");
    expect(reasoned.querySelector('textarea[name="note"]')).not.toBeNull();
    const merge = reasoned.querySelector('[data-testid="decision-approve"]') as HTMLButtonElement;
    expect(merge.textContent).toBe("Merge with checks failing");
    expect(merge.value).toBe("approve");
    const labels = [...row.querySelectorAll("button")].map((b) => b.textContent);
    expect(labels).toContain("Keep blocked");
    expect(labels).not.toContain("Approve and merge");
  });

  it("a passing PR's card is unchanged", () => {
    const row = mount(card(null));
    expect(row.querySelector('[data-testid="decision-checks-failing"]')).toBeNull();
    expect(row.querySelector('[data-testid="decision-reasoned-approve"]')).toBeNull();
    expect(row.querySelector('input[name="force"]')).toBeNull();
    const labels = [...row.querySelectorAll("button")].map((b) => b.textContent);
    expect(labels).toContain("Approve and merge");
  });

  it("reads only a record that names a gate", () => {
    expect(checksFailingOf({})).toBeNull();
    expect(checksFailingOf({ checks_failing: { gates: [] } })).toBeNull();
    expect(checksFailingOf({ checks_failing: CHECKS })!.gates[0].gate).toBe("security_clean");
  });
});

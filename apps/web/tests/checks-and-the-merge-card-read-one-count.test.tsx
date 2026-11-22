// @vitest-environment happy-dom
/**
 * DRY RUN 3 GAP #4: Checks, the packet and the merge card read ONE count source, so they never
 * contradict each other.
 *
 *   - "gwlint 0 found" never sits beside a packet with 2 open: a packet, once there, is the only
 *     source (checkSource); a counts-only packet still draws its row, severity bar and numbers.
 *   - the severity bar, the original → now table and the Contract tests row render whenever the
 *     data exists.
 *   - "Old tests: unknown (why)" instead of "Checks failing · Old tests broke" when no failing
 *     test can be named.
 *   - the merge card never says Ready and Checks failing at the same time.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DecisionFeedItem } from "../lib/decisions";
import { checkSource, parsePacket, parseValidation, type ReviewPacket } from "../lib/validation-review";
import { PACKET_READY, VALIDATION_OPEN } from "../lib/validation-review-fixtures";
import { parseChangeSet } from "../lib/repo-links";
import { splitChecksFailing, checksFailingOf, failingGateLabels } from "../app/checks-failing";
import { CheckRows } from "../app/validation/checks-rows";
import { DecisionInbox } from "../app/decision-inbox-ui";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(""),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;

let host: HTMLElement;
let root: Root;
beforeEach(() => { host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
afterEach(() => { act(() => root.unmount()); host.remove(); });
const render = (node: React.ReactElement) => { act(() => root.render(node)); return host; };

const OLD_TESTS_UNNAMED = { gates: [{ gate: "existing_tests_intact", label: "Old tests intact", security: false }], findings: [], security: false };
const REAL = { gates: [{ gate: "written_tests_pass", label: "New tests pass", security: false, detail: "2 of 14 tests fail" }], findings: [], security: false };

function card(checks: Record<string, unknown> | null, packet: unknown = PACKET_READY): DecisionFeedItem {
  return {
    id: "dec_m", status: "pending", riskTier: 2, question: "Merge change #9",
    recommendation: { action: "review_pull_request", packet, ...(checks ? { checks_failing: checks } : {}) },
    options: [],
    allowedActions: [
      { action: "approve", label: checks ? "Merge with checks failing" : "Approve and merge", tone: "reject", effect: "", needs: checks ? "note" : null },
      { action: "reject", label: "Keep blocked", tone: "reject", effect: "", needs: null },
    ],
    costSummary: {}, createdAt: "2026-11-21T00:00:00Z", updatedAt: "2026-11-21T00:00:00Z",
  } as unknown as DecisionFeedItem;
}
const inbox = (item: DecisionFeedItem) => render(
  <DecisionInbox feed={{ projectId: "p", apiReachable: true, items: [item] }} tasks={[]} roles={[]} projectId="p" actorId="ceo" />);

describe("one count source", () => {
  it("a packet is the only source once there: never mixed with the record's findings", () => {
    const v = parseValidation({ ...VALIDATION_OPEN, packet: undefined })!;
    const p = parsePacket(PACKET_READY)!;
    expect(checkSource(v, p).findings).toBe(p.findings);
    // The record's packet (GET …/validation) wins over the decision's.
    const live = { ...p, findings: [] } as ReviewPacket;
    expect(checkSource({ ...v, packet: live }, p).packet).toBe(live);
    expect(checkSource({ ...v, packet: live }, p).findings).toEqual([]);
    expect(checkSource(v, null).findings).toBe(v.findings);
  });

  it("a counts-only packet with 2 open draws gwlint 9 found → 2 open with its severity bar, never 0 found", () => {
    const thin = { ...PACKET_READY, verdict: { state: "blocked", reasons: [] }, findings: [],
      counts: { P0: { found: 4, open: 2, waived: 0 }, P1: { found: 4, open: 0, waived: 0 }, P2: { found: 1, open: 0, waived: 0 }, P3: { found: 0, open: 0, waived: 0 }, unmapped: 0 },
      validators: [{ id: "gwlint", ruleset: "northgate v3", before: 9, after_open: 2, after_waived: 0, exit: 1, evidence: "E-4" }] };
    const cs = parseChangeSet({ number: 9, state: "open", source: "local", checks: { tests_passed: "true", run_status: "completed" } });
    const el = render(<CheckRows cs={cs} v={parseValidation({ ...VALIDATION_OPEN, findings: [] })} packet={parsePacket(thin)} onJump={() => {}} />);
    const row = el.querySelector('[data-checker="gwlint"]')!;
    expect(row.querySelector('[data-testid="ck-words"]')!.textContent).toBe("9 found → 2 open");
    expect(row.querySelector('[data-testid="vr-sevbar"]')).not.toBeNull();
    expect(el.textContent).not.toContain("0 found");
  });

  it("with findings, the gwlint row has the severity bar, the original → now table and the Contract tests row", () => {
    const cs = parseChangeSet({ number: 9, state: "open", source: "local" });
    const el = render(<CheckRows cs={cs} v={null} packet={parsePacket(PACKET_READY)} onJump={() => {}} />);
    const row = el.querySelector('[data-checker="gwlint"]')!;
    expect(row.querySelector('[data-testid="vr-sevbar"]')).not.toBeNull();
    expect(row.textContent).toContain("Original → now");
    expect(el.querySelector('[data-testid="ck-contract"]')!.textContent).toContain("2 / 2 pass");
  });
});

describe("old tests nobody can name", () => {
  it("are 'unknown', never a failing gate", () => {
    const s = splitChecksFailing(checksFailingOf({ checks_failing: OLD_TESTS_UNNAMED }));
    expect(s.real).toBeNull();
    expect(s.oldTestsUnknown).toBe("no failing test could be named");
    expect(failingGateLabels({ checks_failing: OLD_TESTS_UNNAMED })).toBe("");
    // Named in the detail: still a failing gate.
    const named = { gates: [{ gate: "existing_tests_intact", label: "x", security: false, detail: "TestHealthz failed" }], findings: [] };
    expect(splitChecksFailing(checksFailingOf({ checks_failing: named })).real).not.toBeNull();
  });

  it("the merge card says Ready and 'Old tests: unknown (why)', never Checks failing", () => {
    const el = inbox(card(OLD_TESTS_UNNAMED));
    expect(el.querySelector('[data-testid="vr-verdict"]')!.textContent).toContain("Ready");
    expect(el.querySelector('[data-testid="decision-checks-failing"]')).toBeNull();
    expect(el.querySelector('[data-testid="decision-old-tests-unknown"]')!.textContent).toBe("Old tests: unknown (no failing test could be named).");
    expect(el.textContent).not.toMatch(/checks failing/i);
    expect(el.querySelector('[data-testid="decision-approve"]')!.textContent).toBe("Merge");
  });
});

describe("the merge card never says Ready and Checks failing at once", () => {
  it("a ready packet with a real failing gate reads Checks failing in its verdict", () => {
    const el = inbox(card(REAL));
    const verdict = el.querySelector('[data-testid="vr-verdict"]')!;
    expect(verdict.textContent).toContain("Checks failing");
    expect(verdict.textContent).not.toContain("Ready");
    expect(el.querySelector('[data-testid="decision-checks-failing"]')).not.toBeNull();
  });

  it("a ready packet and nothing failing still reads Ready", () => {
    const el = inbox(card(null));
    expect(el.querySelector('[data-testid="vr-verdict"]')!.textContent).toContain("Ready");
  });
});

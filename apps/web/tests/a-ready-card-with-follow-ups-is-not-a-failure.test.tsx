// @vitest-environment happy-dom
/**
 * A BUILD WHOSE ONLY OPEN ITEMS ARE FOLLOW-UPS IS READY (engine quality_bar.merge_followups,
 * operator 2026-11-18: "there is no need to fail, what user would do if the review fails?").
 *
 * Recipe box opened with MAJOR review findings still open. At the default (standard) bar they
 * are follow-ups BOSS fixes itself, so the card is a normal ready-to-merge one: its title says
 * "Recipe box is ready — 1 follow-up queued, BOSS is fixing it", the follow-ups are listed folded
 * (P1 first), the merge is one press — never "Checks failing", never a reason to type.
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
const { followUpsOf } = await import("../app/checks-failing");
const { parseWorkItem } = await import("../lib/project-experience");


const FOLLOWUPS = {
  count: 2, p1: 1, queued: true, bar: "standard",
  items: [
    { tier: "P1", severity: "major", text: "styles/globals.css:161 — the serving control overflows on phones" },
    { tier: "P2", severity: "minor", text: "pages/new.js:40 — an empty amount is saved as 0" },
  ],
};

function card(): DecisionFeedItem {
  return {
    id: "dec_rb", status: "pending", riskTier: 2,
    question: "Recipe box is ready — 2 follow-ups queued, BOSS is fixing them (change #1)",
    recommendation: {
      action: "review_pull_request", pull_request_url: "http://boss-local/local_rb/recipe-box/pull/1",
      followups: FOLLOWUPS,
    },
    options: [],
    allowedActions: [
      { action: "approve", label: "Approve and merge", tone: "approve", effect: "", needs: null },
      { action: "reject", label: "Keep blocked", tone: "reject", effect: "", needs: null },
    ],
    costSummary: {}, createdAt: "2026-11-18T00:00:00Z", updatedAt: "2026-11-18T00:00:00Z",
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

describe("a merge card whose only open items are follow-ups", () => {
  it("is a normal ready card: no failing banner, no reason box, one-press merge", () => {
    const row = mount(card());
    expect(row.querySelector(".dq-title")!.textContent)
      .toBe("Recipe box is ready — 2 follow-ups queued, BOSS is fixing them (change #1)");
    expect(row.querySelector('[data-testid="decision-checks-failing"]')).toBeNull();
    expect(row.querySelector('[data-testid="decision-reasoned-approve"]')).toBeNull();
    expect(row.querySelector('input[name="force"]')).toBeNull();
    expect(row.textContent).not.toContain("Checks failing");
    const labels = [...row.querySelectorAll("button")].map((b) => b.textContent);
    expect(labels).toContain("Approve and merge");
  });

  it("lists the follow-ups folded, P1 first, and says BOSS is fixing them", () => {
    const row = mount(card());
    const box = row.querySelector('[data-testid="decision-follow-ups"]')!;
    expect(box).not.toBeNull();
    expect(box.getAttribute("role")).toBeNull();
    expect(box.hasAttribute("open")).toBe(false);
    expect(box.querySelector("summary")!.textContent).toContain("Ready to merge.");
    expect(box.querySelector("summary")!.textContent).toContain("2 follow-ups queued — BOSS is fixing them (1 P1)");
    const items = [...box.querySelectorAll("li")].map((li) => li.textContent ?? "");
    expect(items[0]).toContain("P1 [MAJOR]");
    expect(items[1]).toContain("P2 [MINOR]");
  });

  it("a failing card never also reads as ready", () => {
    expect(followUpsOf({ followups: FOLLOWUPS,
      checks_failing: { gates: [{ gate: "security_clean", label: "Security clean", security: true }] } }))
      .toBeNull();
    expect(followUpsOf({})).toBeNull();
    expect(followUpsOf({ followups: { count: 0, items: [] } })).toBeNull();
  });
});

describe("the project's work drawer", () => {
  it("carries the follow-ups words beside Ready to merge", () => {
    const w = parseWorkItem({ id: "build:crun_rb", label: "Recipe box", state: "needs_you",
      links: { merge_pr: { number: 1, external_url: "http://boss-local/local_rb/recipe-box/pull/1",
                           followups: { count: 1, queued: true, words: "1 follow-up queued, BOSS is fixing it" } } } });
    expect(w.links.merge_pr!.followups).toEqual(
      { count: 1, queued: true, words: "1 follow-up queued, BOSS is fixing it" });
    const plain = parseWorkItem({ id: "x", links: { merge_pr: { number: 2, external_url: "u" } } });
    expect(plain.links.merge_pr!.followups).toBeNull();
  });
});

// @vitest-environment happy-dom
/**
 * An approved release card lists every commit it ships. The what-ships box used to show
 * "Ships 8 commits…" and hide the eight commits in a 4rem label column.
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
const { ActionPreview } = await import("../app/action-preview");

const SENTENCE = "Ships 8 commits beyond what is serving.";
const COMMITS = Array.from({ length: 8 }, (_, i) => ({
  label: `aaa000${i} · this run · approved, reviewed`,
  value: `fix ${i}: the price`,
}));

const PREVIEW = {
  kind: "fields" as const,
  release: true,
  content_hash: "sha256:release8",
  fields: [{ label: "What ships", value: SENTENCE }, ...COMMITS],
};

function approvedRelease(): DecisionFeedItem {
  return {
    id: "dec_release", status: "approved", riskTier: 2,
    question: "Release the incident fix",
    recommendation: {
      capability: "release_approval", step_label: "Release",
      action_preview: PREVIEW,
    },
    options: [],
    allowedActions: [],
    costSummary: {}, createdAt: "2026-11-05T00:00:00Z", updatedAt: "2026-11-05T00:00:00Z",
    approvedBy: "sean", approvedAt: "2026-11-05T00:01:00Z",
  } as unknown as DecisionFeedItem;
}

let host: HTMLElement;
let root: Root;
beforeEach(() => {
  window.history.pushState(null, "", "/?decision_filter=resolved");
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function mount(node: React.ReactElement) {
  act(() => root.render(node));
  return host;
}

function expectCommits(rootEl: ParentNode) {
  const box = rootEl.querySelector('[data-testid="ap-what-ships"]');
  expect(box?.textContent).toBe(SENTENCE);
  const items = [...rootEl.querySelectorAll('[data-testid="ap-commit"]')];
  expect(items).toHaveLength(8);
  items.forEach((item, i) => {
    expect(item.textContent).toContain(`aaa000${i}`);
    expect(item.textContent).toContain(`fix ${i}: the price`);
    expect(item.querySelector("dt")).toBeNull();
  });
}

describe("an approved release lists every commit it ships", () => {
  it("shows each short sha and message, not only the count", () => {
    const page = mount(<ActionPreview preview={PREVIEW} />);
    expectCommits(page);
  });

  it("keeps that list on an approved decision card", () => {
    const page = mount(
      <DecisionInbox feed={{ projectId: "p", apiReachable: true, items: [approvedRelease()] }}
                     tasks={[]} roles={[]} projectId="p" actorId="ceo" />);
    const card = page.querySelector('[data-testid="decision-row"]')!;
    expect(card.getAttribute("data-decision-status")).toBe("approved");
    expectCommits(card);
  });
});

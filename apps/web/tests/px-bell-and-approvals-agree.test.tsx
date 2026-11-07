// @vitest-environment happy-dom
/**
 * X-14: the bell and Approvals say the same number. The bell counts decisions AND what only a
 * person can set up on the project (a prod host, a value nothing sets), from /v1/agents/live
 * `attention`; Approvals' Needs action counts and lists those same setup items from the same poll,
 * so the bell never says 1 over a queue that says 0. A setup item opens where it is set.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AttentionSummary } from "../lib/agents-live-board";

const ATTENTION: AttentionSummary = {
  count: 1, decisions: 0, setup: 1,
  items: [{ id: "setup:prod-host", roleKey: null, title: "Pick a prod host", decideInline: false, kind: "setup",
            href: "?project_id=helpdesk&area=infra&add=target" }],
};

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));
vi.mock("../lib/agents-live", async (orig) => ({ ...(await orig<object>()), useAttention: () => ATTENTION }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
afterEach(() => { act(() => root?.unmount()); root = null; document.body.innerHTML = ""; });

describe("the bell and Approvals agree (X-14)", () => {
  it("Needs action counts and lists the setup item the bell counts", async () => {
    const { DecisionInbox } = await import("../app/decision-inbox-ui");
    const host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root!.render(<DecisionInbox feed={{ projectId: "helpdesk", apiReachable: true, items: [] }} tasks={[]} roles={[]} projectId="helpdesk" actorId="ceo" />);
    });
    const count = document.querySelector("[data-testid='decision-filter-needs-action'] .dq-filter-count")!.textContent;
    expect(count).toBe(String(ATTENTION.count));
    expect(document.querySelector("[data-testid='decision-setup-setup:prod-host']")!.textContent).toContain("Pick a prod host");
    expect(document.body.textContent).not.toContain("Queue is clear");
  });

  it("a setup item on the bell opens where it is set", async () => {
    const { reviewPatch } = await import("../app/agents/presence-pill");
    expect(reviewPatch(ATTENTION.items[0])).toMatchObject({ area: "infra", add: "target", project_id: "helpdesk" });
  });

  it("the attention parse keeps the kind, the link and the two parts of the count", async () => {
    const { parseAttentionSummary } = await import("../lib/agents-live-board");
    const got = parseAttentionSummary({ count: 3, decisions: 2, setup: 1, items: [{ id: "s1", title: "Pick a prod host", kind: "setup", href: "?area=infra" }] });
    expect(got).toMatchObject({ count: 3, decisions: 2, setup: 1, items: [{ id: "s1", kind: "setup", href: "?area=infra" }] });
  });
});

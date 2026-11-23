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

let ATTENTION: AttentionSummary = {
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
const PROD_HOST = ATTENTION;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
afterEach(() => { act(() => root?.unmount()); root = null; document.body.innerHTML = ""; ATTENTION = PROD_HOST; });

async function renderInbox() {
  const { DecisionInbox } = await import("../app/decision-inbox-ui");
  const host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<DecisionInbox feed={{ projectId: "helpdesk", apiReachable: true, items: [] }} tasks={[]} roles={[]} projectId="helpdesk" actorId="ceo" />);
  });
}

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

  it("the setup item is ONE quiet line, a link to where it is set, never a box", async () => {
    await renderInbox();
    const line = document.querySelector<HTMLElement>("[data-testid='decision-inbox-setup']")!;
    expect(line.tagName).toBe("P");
    expect(line.className).toBe("dq-also");
    expect(line.textContent).toBe("To set up, only you can do these: Pick a prod host ›");
    // No card, no eyebrow, no second count beside Needs you.
    expect(line.querySelector("article, .eyebrow, .decision-card")).toBeNull();
    act(() => { line.querySelector<HTMLButtonElement>("[data-testid='decision-setup-setup:prod-host']")!.click(); });
    expect(new URLSearchParams(window.location.search).get("area")).toBe("infra");
  });

  it("a setup item the poll did not list (five decisions filled it) links to the project page that lists it, not an orphan '+1 more'", async () => {
    ATTENTION = { count: 7, decisions: 6, setup: 1, items: [] };
    await renderInbox();
    expect(document.querySelector("[data-testid='decision-filter-needs-action'] .dq-filter-count")!.textContent).toBe("1");
    const line = document.querySelector<HTMLElement>("[data-testid='decision-inbox-setup']")!;
    expect(line.textContent).toBe("To set up, only you can do these: 1 on the project page ›");
    expect(document.body.textContent).not.toContain("more in the project's Needs you");
    act(() => { line.querySelector<HTMLButtonElement>("[data-testid='decision-setup-more']")!.click(); });
    const q = new URLSearchParams(window.location.search);
    // The project's Needs you lists every setup item (the Setup area is the console-wide board).
    expect([q.get("area"), q.get("project_id")]).toEqual(["project", "helpdesk"]);
  });

  it("the setup line is in the page's type scale and quiet: tokens only, no amber", async () => {
    const { readFileSync } = await import("node:fs");
    const css = readFileSync("app/globals.css", "utf8");
    expect(css).toMatch(/\.dq-also \{[^}]*font-size: var\(--fs-meta\);[^}]*color: var\(--muted\);/);
    expect(css).not.toMatch(/\.dq-also \{[^}]*amber/);
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

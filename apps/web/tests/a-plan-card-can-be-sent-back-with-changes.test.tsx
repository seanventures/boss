// @vitest-environment happy-dom
/**
 * A PLAN CARD OFFERS REQUEST CHANGES, IN THE PROJECT PAGE'S DRAWER TOO (AR-24).
 *
 * northgate-gateway, dec_dcd91e76 (2026-11-16): the plan for a follow-up left its brief's HIGH
 * path traversal out, and the card offered only Approve or Reject — rejecting stops the build,
 * approving ships the narrowed plan. The engine now reads "revise" on a plan card (the build
 * plans again from the note, plan_coverage.on_plan_sent_back); the drawer sends it with the
 * person's note, and only with one.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { DecisionActs } from "../app/projects/drawers-more";
import type { PxCtx } from "../app/projects/project-page";
import { FX_OVERVIEW } from "../lib/project-experience-fixtures";
import { parseOverview, type NeedsYou } from "../lib/project-experience";
import { click, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function card(kind: "plan" | "deploy"): NeedsYou {
  const wire = {
    id: "dec_plan1", kind, label: kind === "plan" ? "Approve plan" : "Approve deploy",
    detail: "The plan still leaves 1 item of its brief out.",
    mini: [],
    action: { label: kind === "plan" ? "Approve plan" : "Approve deploy", href: "?drawer=needs:dec_plan1",
              decision_id: "dec_plan1" },
  };
  return parseOverview({ ...FX_OVERVIEW, needs_you: [wire] }).needs_you[0];
}

const ctx = { projectId: "northgate", work: null, overview: { recent_work: [] }, open: vi.fn(),
              refreshWork: vi.fn() } as unknown as PxCtx;

async function drawer(n: NeedsYou): Promise<Call[]> {
  const calls = stubFetch((c) => (c.method === "POST" ? { body: { id: "dec_plan1", status: "revision_requested" } } : undefined));
  await mountAs(<div><DecisionActs ctx={ctx} n={n} /></div>);
  return calls;
}

async function type(el: HTMLInputElement, value: string) {
  const { act } = await import("react");
  await act(async () => {
    const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

afterEach(() => unmount());

describe("a plan card's Request changes", () => {
  it("sends the note to the decision's revise route", async () => {
    const n = card("plan");
    expect(n.kind).toBe("plan");
    const calls = await drawer(n);
    await click(tid("px-drawer-needs-revise"));
    const send = tid("px-drawer-needs-revise-send") as HTMLButtonElement;
    expect(send.disabled).toBe(true);                 // no note, nothing to send
    await type(tid("px-drawer-needs-changes") as HTMLInputElement, "Fix the HIGH path traversal first");
    expect((tid("px-drawer-needs-revise-send") as HTMLButtonElement).disabled).toBe(false);
    await click(tid("px-drawer-needs-revise-send"));
    const sent = calls.find((c) => c.method === "POST" && c.path === "/v1/decisions/dec_plan1/revise");
    expect(sent, JSON.stringify(calls)).toBeTruthy();
    expect(sent!.body).toEqual({ note: "Fix the HIGH path traversal first" });
  });

  it("is offered on a plan card only", async () => {
    await drawer(card("deploy"));
    expect(tid("px-drawer-needs-revise")).toBeNull();
  });
});

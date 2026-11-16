// @vitest-environment happy-dom
/**
 * A REJECT CAN SAY WHY IN WORDS (BOSSAPP-1321, AR-27).
 *
 * northgate-gateway (2026-11-16): rejecting a plan card offered only the "Why no?" chips, so the
 * reason a person had in mind never reached the build ("The plan was rejected", nothing more)
 * and the next plan could not read it. The card now has an optional reason box under "Why no?"
 * (`reject_note`), and the console sends it as the rejection's note; the Request changes note
 * beside it is never sent in its place.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DecisionFeedItem } from "../lib/decisions";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
  redirect: vi.fn(),
}));
vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("next/headers", () => ({
  headers: async () => new Headers(),
  cookies: async () => ({ get: () => undefined }),
}));
vi.mock("../lib/client", () => ({
  PROJECT_ID: "northgate",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "northgate",
  API_URL: "http://127.0.0.1:8000",
  authHeaders: (extra?: Record<string, string>) => extra ?? {},
  client: { rejectDecision: vi.fn(async () => ({})), reviseDecision: vi.fn(async () => ({})) },
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

const PLAN: DecisionFeedItem = {
  id: "dec_plan", status: "pending", riskTier: 2,
  question: "Approve the plan: KAN-9 Northgate API Gateway",
  recommendation: { action: "resume_code_task", run_id: "crun_b144" },
  options: [],
  allowedActions: [
    { action: "approve", label: "Approve plan", tone: "approve", effect: "", needs: null },
    { action: "reject", label: "Reject", tone: "reject", effect: "", needs: null },
    { action: "revise", label: "Request changes", tone: "neutral", effect: "", needs: "note" },
  ],
  costSummary: {}, createdAt: "2026-11-16T00:00:00Z", updatedAt: "2026-11-16T00:00:00Z",
} as unknown as DecisionFeedItem;

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

describe("rejecting a plan card", () => {
  it("offers an optional reason in words under Why no?, beside the chips", () => {
    act(() => root.render(
      <DecisionInbox feed={{ projectId: "northgate", apiReachable: true, items: [PLAN] }}
                     tasks={[]} roles={[]} projectId="northgate" actorId="ceo" />));
    const why = host.querySelector('[data-testid="decision-why"]')!;
    expect(why).not.toBeNull();
    const box = why.querySelector('[data-testid="decision-reject-note"]') as HTMLTextAreaElement;
    expect(box).not.toBeNull();
    expect(box.name).toBe("reject_note");
    expect(box.required).toBe(false);
    // Its own field: the Request changes box keeps the name `note`.
    expect(host.querySelectorAll('textarea[name="note"]').length).toBe(1);
  });

  it("sends the written reason as the rejection's note, never the revise note", async () => {
    const { client } = await import("../lib/client");
    const { resolveDecisionAction } = await import("../app/actions");
    const fd = new FormData();
    fd.set("decision_id", "dec_plan");
    fd.set("project_id", "northgate");
    fd.set("actor", "ceo");
    fd.set("note", "");                       // the Request changes box, left empty
    fd.set("reject_note", "  Fix the HIGH path traversal before anything else  ");
    fd.set("action", "reject");
    await expect(resolveDecisionAction(fd)).resolves.toMatchObject({ ok: true });
    const reject = client.rejectDecision as unknown as ReturnType<typeof vi.fn>;
    expect(reject).toHaveBeenCalledTimes(1);
    expect(reject.mock.calls[0][0]).toBe("dec_plan");
    expect(reject.mock.calls[0][1].note).toBe("Fix the HIGH path traversal before anything else");

    // A revise on the same card reads only its own box.
    const fd2 = new FormData();
    fd2.set("decision_id", "dec_plan");
    fd2.set("project_id", "northgate");
    fd2.set("actor", "ceo");
    fd2.set("note", "Cover AC-7 too");
    fd2.set("reject_note", "not this");
    fd2.set("action", "revise");
    await resolveDecisionAction(fd2);
    const revise = client.reviseDecision as unknown as ReturnType<typeof vi.fn>;
    expect(revise.mock.calls[0][1].note).toBe("Cover AC-7 too");
  });
});

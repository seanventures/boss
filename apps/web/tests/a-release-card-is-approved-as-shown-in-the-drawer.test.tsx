// @vitest-environment happy-dom
/**
 * A DEPLOY'S RELEASE CARD IS APPROVED AS SHOWN, IN THE PROJECT PAGE'S DRAWER TOO.
 *
 * helpdesk-ai-v3, dec_8eaeba2f (2026-11-07): the Needs-you drawer for "Deploy … to Staging"
 * said the release "carries commits from rejected or unreviewed runs", but showed no commits
 * and sent no hash, so every "Approve deploy" was refused: "This release card lists the
 * commits it would ship, and this approval was not of that list … Reload the card". The engine
 * guard stays (release_scope.refuse_approval); the drawer now draws the card's commits, as
 * Approvals does, and approves with its hash — and, when the card needs a reason, with it.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { DecisionActs, NeedsBody } from "../app/projects/drawers-more";
import type { PxCtx } from "../app/projects/project-page";
import { FX_OVERVIEW } from "../lib/project-experience-fixtures";
import { parseOverview, type NeedsYou } from "../lib/project-experience";
import { click, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const HASH = "sha256:" + "a".repeat(64);

function wireCard(needsReason: boolean) {
  return {
    id: "dec_deploy2", kind: "deploy", label: "Deploy PR #2 (not merged) to Staging",
    detail: "Deploy paramg/helpdesk-ai-v3@99b569f71330 to 2.29.2.202?",
    mini: [{ label: "PR #2 not merged", state: "failed" }, { label: "Staging · you", state: "waiting" },
           { label: "Smoke test", state: "todo" }],
    action: { label: "Approve deploy", href: "?drawer=needs:dec_deploy2", decision_id: "dec_deploy2" },
    release: {
      content_hash: HASH, needs_reason: needsReason,
      sentence: "Ships 2 commits beyond the serving commit fa64985: 1 from this run, 1 from other work.",
      preview: {
        kind: "fields", release: true, content_hash: HASH, redactions: 0, step_label: "Release",
        fields: [
          { name: "what", label: "What ships", value: "Ships 2 commits beyond the serving commit fa64985: 1 from this run, 1 from other work." },
          { name: "c1", label: "36e91e4 · this run · approved, reviewed", value: "Route new tickets to a queue" },
          { name: "c2", label: "178d92c · no BOSS run · unknown, NOT reviewed", value: "More tests" },
        ],
      },
    },
  };
}

function needs(needsReason: boolean): NeedsYou {
  const parsed = parseOverview({ ...FX_OVERVIEW, needs_you: [wireCard(needsReason)] });
  return parsed.needs_you[0];
}

const ctx = { projectId: "helpdesk", work: null, overview: { recent_work: [] }, open: vi.fn(),
              refreshWork: vi.fn() } as unknown as PxCtx;

async function drawer(n: NeedsYou): Promise<Call[]> {
  const calls = stubFetch((c) => (c.method === "POST" ? { body: { id: "dec_deploy2", status: "approved" } } : undefined));
  await mountAs(<div><NeedsBody ctx={ctx} n={n} /><DecisionActs ctx={ctx} n={n} /></div>);
  return calls;
}

afterEach(() => unmount());

describe("the drawer's release card", () => {
  it("the parser keeps the card the engine sent", () => {
    const n = needs(false);
    expect(n.release?.content_hash).toBe(HASH);
    expect(n.release?.needs_reason).toBe(false);
  });

  it("shows every commit it ships, in words, and approves with the card's hash", async () => {
    const calls = await drawer(needs(false));
    const list = tid("px-drawer-needs-release")!;
    expect(list.textContent).toContain("Ships 2 commits beyond the serving commit fa64985");
    expect(list.textContent).toContain("36e91e4 · this run · approved, reviewed");
    expect(list.textContent).toContain("178d92c · no BOSS run · unknown, NOT reviewed");
    await click(tid("px-drawer-needs-approve"));
    const sent = calls.find((c) => c.method === "POST" && c.path === "/v1/decisions/dec_deploy2/approve");
    expect(sent, JSON.stringify(calls)).toBeTruthy();
    expect((sent!.body as Record<string, unknown>).content_hash).toBe(HASH);
    expect((sent!.body as Record<string, unknown>).force).toBeUndefined();
  });

  it("a card that needs a reason asks for it first, then sends it with the hash", async () => {
    const calls = await drawer(needs(true));
    const approve = tid("px-drawer-needs-approve") as HTMLButtonElement;
    expect(approve.disabled).toBe(true);
    const why = tid("px-drawer-needs-why") as HTMLInputElement;
    expect(why).not.toBeNull();
    const { act } = await import("react");
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(why, "Staging only; PR #2 is reviewed in GitHub");
      why.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect((tid("px-drawer-needs-approve") as HTMLButtonElement).disabled).toBe(false);
    await click(tid("px-drawer-needs-approve"));
    const sent = calls.find((c) => c.method === "POST" && c.path === "/v1/decisions/dec_deploy2/approve");
    expect(sent).toBeTruthy();
    expect(sent!.body).toMatchObject({ content_hash: HASH, force: true,
                                       note: "Staging only; PR #2 is reviewed in GitHub" });
  });

  it("a card without a release sends a plain approval, as before", async () => {
    const plain = { ...needs(false), release: null };
    const calls = await drawer(plain);
    expect(tid("px-drawer-needs-release")).toBeNull();
    await click(tid("px-drawer-needs-approve"));
    const sent = calls.find((c) => c.method === "POST");
    expect(sent!.body).toEqual({ note: "" });
  });
});

// @vitest-environment happy-dom
/**
 * AN ALWAYS-ON CARD ANSWERS IN ITS OWN WORDS, AND EACH ENTRY ON ITS OWN.
 *
 * boss-dogfood dec_558cbcf6 (2026-11-07): "Agents ask about 2 things: 2 spent budgets" offered
 * only "Approve all / Dismiss all". The engine now sends the card's own answers ("Give each $1
 * more", "Close both", "I'll take them": needs.ambient_answers) and every entry's own question,
 * finding and answers. The drawer draws both: the card's answers post the decision action that
 * carries each; an entry's answer posts to /v1/ambient/cards/<id>/entries/<work item>/answer.
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

const BUDGET = [{ key: "approve", label: "Give it $1 more" }, { key: "reject", label: "Close it" },
                { key: "take", label: "I'll take it" }];

function card(): NeedsYou {
  const wire = {
    id: "dec_558", kind: "question", label: "2 squads ran out of budget",
    detail: "2 squads ran out of budget: this install, esbuild bump", mini: [],
    action: { label: "Answer", href: "?drawer=needs:dec_558", decision_id: "dec_558" },
    answers: [{ action: "approve", label: "Give each $1 more" }, { action: "reject", label: "Close both" },
              { action: "revise", label: "I'll take them" }],
    entries: [
      { work_item_id: "work_a", question: "Infra Steward's squad used its $1.50 on “Updates restarted a build” — give it $1 more, or close it?",
        detail: "Found so far: The build was restarted by an update", answers: BUDGET },
      { work_item_id: "work_b", question: "Infra Steward's squad used its $1.50 on “esbuild build” — give it $1 more, or close it?",
        detail: "Found so far: The stall is more likely an npm timeout", answers: BUDGET },
    ],
  };
  return parseOverview({ ...FX_OVERVIEW, needs_you: [wire] }).needs_you[0];
}

const ctx = { projectId: "boss-dogfood", work: null, overview: { recent_work: [] }, open: vi.fn(),
              refreshWork: vi.fn() } as unknown as PxCtx;

async function drawer(n: NeedsYou): Promise<Call[]> {
  const calls = stubFetch((c) => (c.method === "POST" ? { body: { card: "narrowed" } } : undefined));
  await mountAs(<div><NeedsBody ctx={ctx} n={n} /><DecisionActs ctx={ctx} n={n} /></div>);
  return calls;
}

afterEach(() => unmount());

describe("an always-on card's answers", () => {
  it("draws each entry with its own finding and its own three buttons", async () => {
    await drawer(card());
    const a = tid("px-drawer-needs-entry-work_a")!;
    expect(a.textContent).toContain("Found so far: The build was restarted by an update");
    const labels = Array.from(a.querySelectorAll("button")).map((b) => b.textContent?.trim());
    expect(labels).toEqual(["Give it $1 more", "Close it", "I'll take it"]);
    expect(tid("px-drawer-needs-entry-work_b")).not.toBeNull();
  });

  it("an entry's answer goes to that entry alone", async () => {
    const calls = await drawer(card());
    const b = tid("px-drawer-needs-entry-work_b")!;
    await click(b.querySelector("[data-testid='px-drawer-needs-entry-answer-reject']") as HTMLElement);
    const sent = calls.find((c) => c.method === "POST");
    expect(sent?.path).toBe("/v1/ambient/cards/dec_558/entries/work_b/answer");
    expect(sent?.body).toMatchObject({ project_id: "boss-dogfood", answer: "reject" });
  });

  it("the question stands on its own line, apart from what was found so far", async () => {
    const one = parseOverview({ ...FX_OVERVIEW, needs_you: [{
      id: "dec_1", kind: "question", label: "This install: squad out of budget", mini: [],
      question: "Infra Steward's squad used its $1.50 on “Updates restarted a build” — give it $1 more, or close it?",
      detail: "Found so far: The two restarts are not yet shown to be update-triggered…",
      action: { label: "Answer", href: "?drawer=needs:dec_1", decision_id: "dec_1" },
      answers: [{ action: "approve", label: "Give it $1 more" }] }] }).needs_you[0];
    await drawer(one);
    expect(tid("px-drawer-needs-question")!.textContent).toContain("give it $1 more, or close it?");
    expect(tid("px-drawer-needs-detail")!.textContent).toMatch(/^Found so far: /);
    expect(tid("px-drawer-needs-question")!.textContent).not.toContain("Found so far");
  });

  it("the card's own answers say what they do and post the action that carries them", async () => {
    const calls = await drawer(card());
    const acts = tid("px-drawer-needs-acts")!;
    expect(Array.from(acts.querySelectorAll("button")).map((b) => b.textContent?.trim()))
      .toEqual(["Give each $1 more", "Close both", "I'll take them"]);
    expect(acts.textContent).not.toContain("Approve all");
    await click(tid("px-drawer-needs-answer-reject"));
    const sent = calls.find((c) => c.method === "POST");
    expect(sent?.path).toBe("/v1/decisions/dec_558/reject");
  });
});

// @vitest-environment happy-dom
/**
 * The plan row's button shows the short label and keeps the full title in its
 * title attribute. When that title says more than the label, the row shows it.
 * A sentence of more than six words is a paragraph. A blank title, or one that
 * only repeats the label, adds nothing.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));

import { RoadmapTab, planTitleSentence } from "../app/projects/tab-roadmap";
import type { PxCtx } from "../app/projects/project-page";
import { mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LABEL = "SLA";
const TITLE = "Fix the weekend SLA so Friday tickets do not breach";
const SHORT = "Pause the weekend clock";

const ctx = {
  projectId: "helpdesk-ai-demo", actor: "ceo", orgId: "org_1", demo: false,
  overview: { captain: { name: "Helm" } }, work: null, workState: "ready",
  refreshWork: () => {}, onboarding: null, tab: () => {}, open: () => {}, ask: () => {},
} as unknown as PxCtx;

function engine(card: { label: string; title: string }) {
  return stubFetch((c: Call) => {
    if (c.path.endsWith("/plan")) {
      return {
        body: {
          now: [{
            id: "wi_sla", label: card.label, title: card.title, lane: "now", state: "running",
            agent: null, href: "", source: "roadmap", external_url: "", start: null,
          }],
          next: [], later: [], counts: { now: 1, next: 0, later: 0 },
        },
      };
    }
    if (c.path.startsWith("/v1/ambient/roadmap")) {
      return { body: { items: [], counts: { open: 0, accepted: 0, rejected: 0, snoozed: 0 } } };
    }
    return { body: {} };
  });
}

async function open(card: { label: string; title: string }) {
  engine(card);
  await mountAs(<RoadmapTab ctx={ctx} />, "owner");
  for (let i = 0; i < 8 && !tid("px-rm-item-wi_sla"); i++) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
}

function row(): HTMLElement {
  const el = tid("px-rm-item-wi_sla");
  if (!el) throw new Error("plan row missing");
  return el;
}

afterEach(() => { unmount(); });

describe("planTitleSentence", () => {
  it("returns a trimmed title that says more than the label", () => {
    expect(planTitleSentence(LABEL, `  ${TITLE}  `)).toBe(TITLE);
    expect(planTitleSentence(LABEL, SHORT)).toBe(SHORT);
  });

  it("returns null when the title is blank or only the label", () => {
    expect(planTitleSentence(LABEL, "")).toBeNull();
    expect(planTitleSentence(LABEL, "   ")).toBeNull();
    expect(planTitleSentence(LABEL, LABEL)).toBeNull();
    expect(planTitleSentence(`  ${LABEL}  `, LABEL)).toBeNull();
    expect(planTitleSentence(LABEL, `  ${LABEL}  `)).toBeNull();
  });
});

describe("the plan card shows the full title", () => {
  it("shows a long title as a paragraph and keeps the label on the button", async () => {
    await open({ label: LABEL, title: `  ${TITLE}  ` });
    const button = row().querySelector("button.px-rm-t");
    expect(button?.textContent).toBe(LABEL);
    expect(button?.querySelector("p")).toBeNull();
    const sentence = row().querySelector("p");
    expect(sentence?.textContent ?? null).toBe(TITLE);
    expect(TITLE.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);
    expect(sentence?.tagName).toBe("P");
  });

  it("shows a shorter title in the row without a paragraph", async () => {
    expect(SHORT.split(/\s+/).filter(Boolean).length).toBeLessThanOrEqual(6);
    await open({ label: LABEL, title: SHORT });
    const button = row().querySelector("button.px-rm-t");
    expect(button?.textContent).toBe(LABEL);
    const sentence = [...row().querySelectorAll("span, p")].find((el) => el.textContent === SHORT) ?? null;
    expect(sentence?.textContent ?? null).toBe(SHORT);
    expect(sentence?.tagName).toBe("SPAN");
  });

  it("adds no sentence when the title only repeats the label", async () => {
    await open({ label: LABEL, title: LABEL });
    expect(row().querySelector("button.px-rm-t")?.textContent).toBe(LABEL);
    expect(row().querySelector("p")).toBeNull();
    expect([...row().querySelectorAll("span")].some((el) => el.textContent === LABEL)).toBe(false);
  });
});

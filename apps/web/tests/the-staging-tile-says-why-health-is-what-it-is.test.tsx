/**
 * The staging tile says why its health is what it is, in the tile, not only on the title.
 * A blank reason says nothing. A sentence over six words is a paragraph.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { OverviewTab, stagingHealthReasonSentence } from "../app/projects/tab-overview";
import type { PxCtx } from "../app/projects/project-page";
import { parseOverview } from "../lib/project-experience";
import { FX_OVERVIEW } from "../lib/project-experience-fixtures";
import { mountAs, unmount } from "./_helpers/multirepo-fixtures";

const LONG = "the staging probes have been failing since last night";
const SIX = "one two three four five six";
const SHORT = "probe down";

function ctxFor(reason: string | undefined, health: "degraded" | "down"): PxCtx {
  return {
    projectId: "helpdesk-ai-demo",
    actor: "ceo",
    orgId: null,
    overview: parseOverview({
      ...FX_OVERVIEW,
      tiles: {
        ...FX_OVERVIEW.tiles,
        staging: {
          ...FX_OVERVIEW.tiles.staging,
          health,
          health_reason: reason,
          p95_source: "probe",
          window: "24 h",
        },
      },
    }),
    demo: false,
    work: null,
    workState: "ready",
    refreshWork: () => undefined,
    onboarding: null,
    tab: () => undefined,
    open: () => undefined,
    ask: () => undefined,
  };
}

async function show(reason: string | undefined, health: "degraded" | "down" = "degraded"): Promise<HTMLElement> {
  unmount();
  await mountAs(<OverviewTab ctx={ctxFor(reason, health)} />);
  const tile = document.querySelector("[data-testid='px-tile-staging']");
  expect(tile).not.toBeNull();
  return tile as HTMLElement;
}

function sentenceEl(tile: HTMLElement, sentence: string): Element | undefined {
  return [...tile.querySelectorAll("p, span")].find((el) => el.textContent === sentence);
}

afterEach(() => unmount());

describe("stagingHealthReasonSentence", () => {
  it("is the trimmed reason, or nothing", () => {
    expect(stagingHealthReasonSentence(`  ${LONG}  `)).toBe(LONG);
    expect(stagingHealthReasonSentence(SHORT)).toBe(SHORT);
    expect(stagingHealthReasonSentence(SIX)).toBe(SIX);
    expect(stagingHealthReasonSentence(null)).toBeNull();
    expect(stagingHealthReasonSentence(undefined)).toBeNull();
    expect(stagingHealthReasonSentence("")).toBeNull();
    expect(stagingHealthReasonSentence(" \n\t ")).toBeNull();
  });
});

describe("the staging tile says why health is what it is", () => {
  it("shows a long reason as a paragraph and keeps the health word and the p95 source", async () => {
    const tile = await show(`  ${LONG}  `);
    const health = tile.querySelector("[data-testid='px-tile-staging-health']");
    expect(health).not.toBeNull();
    expect(health!.textContent).toBe("Degraded");
    expect(health!.getAttribute("title")).toBe(`  ${LONG}  `);
    expect(tile.textContent).toContain(LONG);
    expect(sentenceEl(tile, LONG)?.tagName).toBe("P");
    expect(tile.querySelector("[data-testid='px-tile-staging-p95']")).not.toBeNull();
    expect(tile.querySelector("[data-testid='px-tile-staging-p95-source']")?.textContent).toBe("From health probes.");
  });

  it("shows a shorter reason as a span, including a sentence of six words", async () => {
    const short = await show(SHORT, "down");
    expect(short.querySelector("[data-testid='px-tile-staging-health']")!.textContent).toBe("Down");
    expect(sentenceEl(short, SHORT)?.tagName).toBe("SPAN");
    expect(short.querySelector("[data-testid='px-tile-staging-p95-source']")?.textContent).toBe("From health probes.");

    const six = await show(SIX);
    expect(six.querySelector("[data-testid='px-tile-staging-health']")!.textContent).toBe("Degraded");
    expect(sentenceEl(six, SIX)?.tagName).toBe("SPAN");
  });

  it("says nothing when the reason is blank", async () => {
    const none = await show(undefined);
    const blank = await show(" \n\t ");
    expect(none.querySelector("[data-testid='px-tile-staging-health']")!.textContent).toBe("Degraded");
    expect(none.querySelector("p")).toBeNull();
    expect(sentenceEl(none, LONG)).toBeUndefined();
    expect(sentenceEl(none, SHORT)).toBeUndefined();
    expect(blank.textContent).toBe(none.textContent);
    expect(blank.querySelector("[data-testid='px-tile-staging-p95-source']")?.textContent).toBe("From health probes.");
  });
});

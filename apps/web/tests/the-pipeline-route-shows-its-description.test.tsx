// @vitest-environment happy-dom
/**
 * A pipeline route shows its description when that line is not just the title.
 * A blank description, or one that only repeats the title, says nothing.
 * A sentence over six words is a paragraph.
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { Route, routeDescriptionSentence } from "../app/workflows/pipeline-view";
import type { PipelineStage } from "../lib/pipeline-model";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SHORT = "Only when new";
const SIX = "Routes only when the incident is";
const LONG = "Routes only when the incident is new";

function stage(id: string, title: string, description: string, frameNote?: string): PipelineStage {
  return {
    id,
    kind: "condition",
    number: null,
    title,
    description,
    frameNote,
    runsAs: { label: "Condition", tone: "control", tip: "Routes the flow" },
    status: "not_run",
    contract: null,
    design: {
      status: "not_reviewed",
      reference: "",
      can: { send_for_review: false, lock: false, revise: false },
      source: "default",
    },
    placement: "main",
  };
}

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

function mount(node: React.ReactElement): HTMLElement {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(node));
  return host;
}

function routeSource(): string {
  const src = codeOnly(join(__dirname, "../app/workflows/pipeline-view.tsx"));
  const start = src.indexOf("function Route");
  const end = src.indexOf("function referenceWords");
  return src.slice(start, end);
}

describe("routeDescriptionSentence", () => {
  it("returns the trimmed description when it is not the title", () => {
    expect(routeDescriptionSentence("Is it new?", "  Routes only when the incident is new  ")).toBe(LONG);
    expect(routeDescriptionSentence("Is it new?", SHORT)).toBe(SHORT);
  });

  it("returns nothing when the description only repeats the title", () => {
    expect(routeDescriptionSentence("Is it new?", "Is it new?")).toBeNull();
    expect(routeDescriptionSentence("  Is it new?  ", "Is it new?")).toBeNull();
    expect(routeDescriptionSentence("Is it new?", "  Is it new?  ")).toBeNull();
  });

  it("returns nothing when the description is blank", () => {
    expect(routeDescriptionSentence("Is it new?", "")).toBeNull();
    expect(routeDescriptionSentence("Is it new?", "   ")).toBeNull();
    expect(routeDescriptionSentence("Is it new?", "\n\t")).toBeNull();
    expect(routeDescriptionSentence("Is it new?", null)).toBeNull();
    expect(routeDescriptionSentence("Is it new?", undefined)).toBeNull();
  });
});

describe("the pipeline route shows its description", () => {
  it("shows a different description beside the title, as a paragraph past six words", () => {
    const shown = mount(
      <>
        <Route stage={stage("long", "Is it new?", `  ${LONG}  `, "Yes: Evidence loop")} />
        <Route stage={stage("six", "Is it new?", SIX)} />
        <Route stage={stage("short", "Is it new?", SHORT)} />
        <Route stage={stage("same", "Is it new?", "  Is it new?  ")} />
        <Route stage={stage("blank", "Is it new?", "   ")} />
      </>,
    );

    const long = shown.querySelector('[data-testid="route-long"]') as HTMLElement;
    const sentence = shown.querySelector('[data-testid="route-desc-long"]') as HTMLElement;
    expect(sentence.tagName).toBe("P");
    expect(sentence.textContent).toBe(LONG);
    expect(sentence.hasAttribute("data-user-text")).toBe(true);
    expect(long.getAttribute("title")).toBe(`  ${LONG}  `);
    expect(long.getAttribute("aria-label")).toBe(`Condition:   ${LONG}  `);
    expect(long.querySelector(".pl-route-title")?.textContent).toBe("Is it new?");
    expect(long.textContent).toContain("Yes: Evidence loop");

    const six = shown.querySelector('[data-testid="route-desc-six"]') as HTMLElement;
    const short = shown.querySelector('[data-testid="route-desc-short"]') as HTMLElement;
    expect(six.tagName).toBe("SPAN");
    expect(six.textContent).toBe(SIX);
    expect(six.hasAttribute("data-user-text")).toBe(true);
    expect(short.tagName).toBe("SPAN");
    expect(short.textContent).toBe(SHORT);

    for (const id of ["same", "blank"]) {
      const row = shown.querySelector(`[data-testid="route-${id}"]`) as HTMLElement;
      expect(row.querySelector(`[data-testid="route-desc-${id}"]`)).toBeNull();
      expect(row.querySelector(".pl-route-title")?.textContent).toBe("Is it new?");
    }
    const same = shown.querySelector('[data-testid="route-same"]') as HTMLElement;
    expect(same.getAttribute("title")).toBe("  Is it new?  ");
    expect(same.getAttribute("aria-label")).toBe("Condition:   Is it new?  ");
    const blank = shown.querySelector('[data-testid="route-blank"]') as HTMLElement;
    expect(blank.getAttribute("title")).toBe("   ");
    expect(blank.getAttribute("aria-label")).toBe("Condition:    ");
  });

  it("calls routeDescriptionSentence with the stage title and description", () => {
    expect(routeSource()).toContain("routeDescriptionSentence(stage.title, stage.description)");
  });
});

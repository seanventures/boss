// @vitest-environment happy-dom

import React, { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  type ArchitectureReview,
  answerable,
  architectureDocOf,
  architecturePill,
  missingSections,
} from "../lib/architecture";

/**
 * Architecture approval in the console: the step's panel (modeled on the PRD panel) shows where
 * it stands in words, every version of the document, and the same Approve / Request changes /
 * Reject the Approvals card offers; the Approvals card shows the document itself; the builder
 * draws the step with its own glyph.
 */
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { ArchitecturePanel } = await import("../app/architecture-ui");
const { workflowGateEffect } = await import("../app/decision-inbox-ui");
const { stepWorkOf } = await import("../app/delivery-workflows-ui");
const { StepIcon } = await import("../app/step-icons");

const SECTIONS = [
  { key: "context", heading: "Context" }, { key: "components", heading: "Components" },
  { key: "data_model", heading: "Data model" }, { key: "apis", heading: "APIs" },
  { key: "security", heading: "Security and authorization" },
  { key: "deployment", heading: "Deployment" }, { key: "risks", heading: "Risks and trade-offs" },
  { key: "decisions", heading: "Decisions and alternatives" },
];

const ACTIONS = [
  { action: "approve", label: "Approve", tone: "approve", effect: "Approves this version." },
  { action: "revise", label: "Request changes", tone: "neutral", effect: "Back to the architect.",
    needs: "note" },
  { action: "reject", label: "Reject", tone: "reject", effect: "Stops the run." },
] as ArchitectureReview["decision"] extends infer D ? D extends { actions: infer A } ? A : never : never;

const REVIEW: ArchitectureReview = {
  id: "arch_1", workflow_run_id: "wfr_1", step_id: "arch", status: "awaiting_approval",
  status_words: "Version 2 is waiting for a person to approve it, request changes, or reject it.",
  architect: "chief_architect", title: "Helpdesk", current_version: 2, notes: [
    { version: 1, note: "Add row-level security.", by: "ceo", at: "2026-10-09T10:00:00Z" }],
  decision: { id: "dec_2", status: "pending", actions: ACTIONS },
  sections: SECTIONS,
  versions: [
    { artifact_id: "art_1", version: 1, markdown: "# Architecture\n\n## Context\nFirst draft.",
      missing_sections: ["apis"] },
    { artifact_id: "art_2", version: 2, markdown: "# Architecture\n\n## Security and authorization\nRow-level security.",
      missing_sections: [], revised_for: "Add row-level security." },
  ],
};

describe("what the console reads from a review", () => {
  it("names where it stands in short words and only answers an open version", () => {
    expect(architecturePill(REVIEW)).toEqual({ label: "Needs you · v2", tone: "warn" });
    expect(architecturePill({ ...REVIEW, status: "approved", approved_version: 2 }).label)
      .toBe("Approved · v2");
    expect(architecturePill({ ...REVIEW, status: "revision_requested" }).label).toBe("Revising · v3");
    expect(answerable(REVIEW)).toBe(true);
    expect(answerable({ ...REVIEW, status: "approved" })).toBe(false);
  });

  it("says which sections a version left out, by heading", () => {
    expect(missingSections(REVIEW, REVIEW.versions![0])).toEqual(["APIs"]);
  });

  it("finds the document on an Approvals card only for an architecture review", () => {
    expect(architectureDocOf({ action: "review_architecture", document_markdown: "## Context", version: 3 }))
      .toEqual({ markdown: "## Context", version: 3 });
    expect(architectureDocOf({ action: "review_pull_request", document_markdown: "x" })).toBeNull();
  });

  it("says what approving does on the card, never 'no follow-on'", () => {
    const said = workflowGateEffect({ workflow_run_id: "wfr_1", capability: "architecture",
                                      app_name: "Helpdesk" });
    expect(said).toContain("Request changes");
    expect(said).toContain("brief");
  });

  it("opens the review as the step's work, and draws the step with its own glyph", () => {
    expect(stepWorkOf({ capability: "architecture",
                        output: { await: { kind: "architecture", id: "arch_9" } } } as never))
      .toEqual({ kind: "architecture", id: "arch_9" });
    expect(renderToStaticMarkup(createElement(StepIcon, { capability: "architecture" })))
      .toContain("<svg");
  });
});

describe("the Architecture step's panel", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      expect(String(url)).toContain("/v1/architecture-reviews/arch_1?project_id=prj_1");
      return new Response(JSON.stringify(REVIEW), { status: 200 });
    }));
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  });

  it("shows the status in words, every version, the document, and a way to answer", async () => {
    await act(async () => {
      root.render(createElement(ArchitecturePanel, { reviewId: "arch_1", projectId: "prj_1" }));
    });
    await act(async () => { await Promise.resolve(); });
    const text = host.textContent ?? "";
    expect(text).toContain("Needs you · v2");
    expect(text).toContain("waiting for a person");
    expect(text).toContain("Version 2 answers: “Add row-level security.”");
    expect(text).toContain("Row-level security.");
    expect([...host.querySelectorAll("[role=tab]")].map((b) => b.textContent)).toEqual(["v1", "v2"]);
    // No signed-in actor here: the panel sends the person to the Approvals card.
    expect(text).toContain("Review in Approvals");

    const v1 = [...host.querySelectorAll("[role=tab]")][0] as HTMLButtonElement;
    await act(async () => { v1.click(); });
    expect(host.textContent).toContain("First draft.");
    expect(host.querySelector("[data-testid=architecture-missing]")?.textContent).toContain("APIs");
  });

  it("offers nothing to press once it is approved", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      ...REVIEW, status: "approved", approved_by: "ceo", approved_version: 2,
      status_words: "Version 2 was approved by ceo. Build steps receive it in their brief.",
      decision: { id: "dec_2", status: "approved", actions: [] },
    }), { status: 200 })));
    await act(async () => {
      root.render(createElement(ArchitecturePanel, { reviewId: "arch_1", projectId: "prj_1" }));
    });
    await act(async () => { await Promise.resolve(); });
    expect(host.textContent).toContain("approved by ceo");
    expect(host.textContent).not.toContain("Review in Approvals");
  });
});

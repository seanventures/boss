/**
 * A workflow definition's description is a sentence on its registry row.
 * A blank description says nothing. A sentence over six words is a paragraph.
 * The page still says how many workflows are not shown.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { WorkflowDefinitionSummary } from "@boss/api-client";
import { WorkflowRegistryView, definitionDescriptionSentence } from "../app/workflow-registry-ui";

const STAMP = "2026-07-18T00:00:00Z";
const LONG = "This workflow ships a release after the checks pass.";
const SIX = "Ships the release after checks pass.";
const SHORT = "Ships the release.";
const HIDDEN = "This ninth workflow stays off the page entirely.";

function definition(n: number, description?: string): WorkflowDefinitionSummary {
  return {
    id: `wfd_${n}`,
    project_id: "boss-dogfood",
    workflow_key: `wf-${n}`,
    name: `Workflow ${n}`,
    description,
    status: "active",
    active_version_id: `wfv_${n}`,
    created_by: "ceo",
    created_at: STAMP,
    updated_at: STAMP,
    version_count: n,
  };
}

function markup(definitions: WorkflowDefinitionSummary[]): string {
  return renderToStaticMarkup(
    <WorkflowRegistryView
      apiReachable
      definitions={definitions}
      mappings={[]}
      runs={[]}
    />,
  );
}

function innerTexts(html: string, tag: string): string[] {
  const re = new RegExp(`<${tag}\\b[^>]*>[^<]*</${tag}>`, "g");
  return (html.match(re) ?? []).map((el) => el.replace(/<[^>]+>/g, ""));
}

describe("the workflow registry shows the description", () => {
  it("shows a long description as a paragraph on the row", () => {
    const html = markup([
      definition(1, `  ${LONG}  `),
      definition(2, SHORT),
      definition(3, SIX),
      definition(4, "   "),
      definition(5),
      definition(6),
      definition(7),
      definition(8),
      definition(9, HIDDEN),
    ]);
    expect(innerTexts(html, "p").find((text) => text === LONG)).toBe(LONG);
    expect(innerTexts(html, "span")).toContain(SHORT);
    expect(innerTexts(html, "span")).toContain(SIX);
    expect(innerTexts(html, "p")).not.toContain(SHORT);
    expect(innerTexts(html, "p")).not.toContain(SIX);
    expect(innerTexts(html, "p")).toContain("8 workflows are on this page. 1 more are not.");
    expect(html).toContain("Workflow 1");
    expect(html).toContain("wf-1");
    expect(html).toContain("active");
    expect(html).toContain("1 versions");
    expect(html).not.toContain(HIDDEN);
    expect(html).not.toContain("Workflow 9");
  });
});

describe("definitionDescriptionSentence", () => {
  it("is the trimmed description, or nothing", () => {
    expect(definitionDescriptionSentence(`  ${LONG}  `)).toBe(LONG);
    expect(definitionDescriptionSentence(SHORT)).toBe(SHORT);
    expect(definitionDescriptionSentence("   ")).toBeNull();
    expect(definitionDescriptionSentence("")).toBeNull();
    expect(definitionDescriptionSentence(undefined)).toBeNull();
    expect(definitionDescriptionSentence(null)).toBeNull();
  });
});

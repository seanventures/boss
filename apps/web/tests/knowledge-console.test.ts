import { readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { BasedOn } from "../app/knowledge-console";
import { KnowledgeExplorer, KnowledgeSettings } from "../app/knowledge-console";

describe("knowledge console", () => {
  it("shows a search hit, why, and blast radius from the project", () => {
    const html = renderToStaticMarkup(
      createElement(KnowledgeExplorer, {
        projectId: "proj",
        actorId: "ceo",
        initialHits: [
          {
            node_id: "sym-1",
            name: "reserve",
            body: "def reserve",
            trust_tier: "observed",
          },
        ],
        initialWhy: [{ name: "Auth sign-off", body: "Auth changes need security sign-off", trust_tier: "governed" }],
        initialBlast: ["commit", "place"],
      }),
    );
    expect(html).toContain("reserve");
    expect(html).toContain("Auth changes need security sign-off");
    expect(html).toContain("commit");
    expect(html).toContain("place");
    expect(html).toContain("data-area=\"knowledge-explorer\"");
  });

  it("states the settings that are real and the ones that were not measured", () => {
    const html = renderToStaticMarkup(
      createElement(KnowledgeSettings, {
        projectId: "proj",
        actorId: "ceo",
        initial: {
          retention_years: 1,
          embedding_model: null,
          vector_index: "not_created",
          budget_category: "knowledge",
          ship_gate: "case_study_primary_not_run",
          cost_usd: null,
          sources: ["adr folder", "connector document"],
        },
      }),
    );
    expect(html).toContain("one year");
    expect(html).toContain("not created");
    expect(html).toContain("knowledge");
    expect(html).toContain("has not been run");
    expect(html).toContain("not been measured");
    expect(html).not.toContain("goldeneye");
  });

  it("names the facts a decision used", () => {
    const html = renderToStaticMarkup(createElement(BasedOn, { factIds: ["fact-1", "fact-2"] }));
    expect(html).toContain("Based on");
    expect(html).toContain("fact-1");
    expect(html).toContain("fact-2");
  });

  it("mounts the explorer and settings on both consoles", () => {
    for (const name of ["page.tsx", "page-community.tsx"]) {
      const source = readFileSync(new URL(`../app/${name}`, import.meta.url), "utf8");
      expect(source).toContain("KnowledgeExplorer");
      expect(source).toContain("KnowledgeSettings");
    }
    const inbox = readFileSync(new URL("../app/decision-inbox-ui.tsx", import.meta.url), "utf8");
    expect(inbox).toContain("BasedOn");
  });
});

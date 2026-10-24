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
    expect(html).toContain("No embedding model is set.");
    expect(html).toContain("focus=embedding_model");
    expect(html).toContain("not created");
    expect(html).toContain("knowledge");
    expect(html).toContain("has not been run");
    expect(html).toContain("not been measured");
  });

  it("says what agents get under each value of the knowledge_graph setting", () => {
    const render = (agent_access: string) => renderToStaticMarkup(
      createElement(KnowledgeSettings, {
        projectId: "proj",
        actorId: "ceo",
        initial: {
          agent_access,
          retention_years: 1,
          embedding_model: null,
          vector_index: "not_created",
          budget_category: "knowledge",
          ship_gate: "case_study_primary_not_run",
          cost_usd: null,
          sources: ["adr folder"],
        },
      }),
    );
    expect(render("keyword")).toContain("Keyword search only: no graph, meaning search, learned facts or precedent.");
    expect(render("keyword")).toContain("knowledge_graph = keyword");
    expect(render("on")).toContain("learned facts and precedent");
    expect(render("off")).toContain("agents work without the graph");
  });

  it("names the embedding slot, and why meaning-based search is off", () => {
    const html = renderToStaticMarkup(
      createElement(KnowledgeSettings, {
        projectId: "proj",
        actorId: "ceo",
        initial: {
          retention_years: 1,
          embedding_model: { provider: "ollama", model: "nomic-embed-text", dimension: 768,
                             model_id: "ollama:nomic-embed-text:768", check: "failed" },
          retrieval: { mode: "text+graph",
                       reason: "meaning-based search is off: the embedding model check failed: connection refused" },
          vector_index: "hnsw",
          budget_category: "knowledge",
          ship_gate: "case_study_primary_not_run",
          cost_usd: null,
          sources: ["adr folder"],
        },
      }),
    );
    expect(html).toContain("nomic-embed-text (768 dimensions), check failed.");
    expect(html).toContain("check failed: connection refused");
  });

  it("names the facts a decision used", () => {
    const html = renderToStaticMarkup(createElement(BasedOn, { factIds: ["fact-1", "fact-2"] }));
    expect(html).toContain("Based on");
    expect(html).toContain("fact-1");
    expect(html).toContain("fact-2");
  });

  it("mounts the explorer and settings on the console", () => {
    // This tree has one console page (app/page.tsx); there is no page-community.tsx.
    const source = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");
    expect(source).toContain("KnowledgeExplorer");
    expect(source).toContain("KnowledgeSettings");
    const inbox = readFileSync(new URL("../app/decision-inbox-ui.tsx", import.meta.url), "utf8");
    expect(inbox).toContain("BasedOn");
  });
});

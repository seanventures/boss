import { readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

/** The knowledge settings moved to Graph › Settings (app/knowledge/kg-settings.tsx, KG spec §5.1);
 *  these are the same promises, held on their new home. Search, why and blast radius are held by
 *  tests/kg-parity.test.tsx and tests/kg-node-panel.test.tsx. */
import { BasedOn } from "../app/knowledge-console";
import { KgSettings } from "../app/knowledge/kg-settings";

describe("knowledge console", () => {
  it("states the settings that are real and the ones that were not measured", () => {
    const html = renderToStaticMarkup(
      createElement(KgSettings, {
        projectId: "proj",
        actor: "ceo",
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
    expect(html).toContain("knowledge-embedding-set");
    expect(html).toContain("focus=embedding_model");
    expect(html).toContain("Set it in Setup › Models › Manage");
    expect(html).toContain("not created");
    expect(html).toContain("knowledge");
    expect(html).toContain("has not been run");
    expect(html).toContain("not been measured");
  });

  it("shows each repository's code graph with Index or Re-index", () => {
    const base = {
      retention_years: 1, embedding_model: null, vector_index: "not_created", budget_category: "knowledge",
      ship_gate: "case_study_primary_not_run", cost_usd: null, sources: [],
    };
    const html = renderToStaticMarkup(
      createElement(KgSettings, {
        projectId: "proj",
        actor: "ceo",
        initial: {
          ...base,
          repositories: [
            { repo: "bank/core", branch: "main", status: "indexed",
              summary: "51,221 nodes, 155,764 edges from 6,508 files at d563684 on main" },
            { repo: "bank/web", branch: "main", status: "pending",
              summary: "Not indexed yet: queued at the next knowledge tick (within a minute)." },
            { repo: "bank/batch", branch: "main", status: "running", summary: "Indexing main (running)." },
          ],
        },
      }),
    );
    expect(html).toContain("51,221 nodes, 155,764 edges");
    expect(html).toContain(">Re-index<");
    expect(html).toContain(">Index<");
    expect(html).toContain("Indexing…");
    expect(html).toContain("No model is called");
    const none = renderToStaticMarkup(
      createElement(KgSettings, { projectId: "proj", actor: "ceo", initial: { ...base, repositories: [] } }),
    );
    expect(none).toContain("No repository is registered in this project.");
  });

  it("says what agents get under each value of the knowledge_graph setting", () => {
    const render = (agent_access: string) => renderToStaticMarkup(
      createElement(KgSettings, {
        projectId: "proj",
        actor: "ceo",
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
      createElement(KgSettings, {
        projectId: "proj",
        actor: "ceo",
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

  it("mounts the Graph on the console, and the old places say where the knowledge screens went", () => {
    // This tree has one console page (app/page.tsx); there is no page-community.tsx.
    const source = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");
    expect(source).toContain("<GraphArea ");
    expect(source).toContain('<KnowledgeMovedCard to="explorer"');
    expect(source).toContain('<KnowledgeMovedCard to="facts"');
    expect(readFileSync(new URL("../app/knowledge/graph-area.tsx", import.meta.url), "utf8")).toContain("<KgSettings ");
    const inbox = readFileSync(new URL("../app/decision-inbox-ui.tsx", import.meta.url), "utf8");
    expect(inbox).toContain("BasedOn");
  });
});

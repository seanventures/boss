// @vitest-environment happy-dom
/**
 * A bank's Knowledge Graph from its rulebook, console side (rulebook source G3, G4, G6, G7, G9):
 * - Documents › Upload a document (PDF): the file goes up as base64, the PREVIEW lists the
 *   proposed sections (title, first lines, page, counts); drop / merge / split change it; only
 *   "Add the document" writes; Discard writes nothing.
 * - Documents › DQ sample: "Load the NorthPeak Bank rulebook" stages the shipped PDF through the
 *   same preview; once added, "Extract rules" starts rulebook_extraction with the document.
 * - Rules review: a data-quality rule's card — id and version, dimension, severity, check,
 *   Warning/Error thresholds, what it protects, and the clause it was read from with its quote.
 * - Graph search: "Compare by words and by meaning" shows both columns and marks what only meaning
 *   found.
 * - Ask BOSS: an answer's [R:kgn_…]/[D:kgn_…] become links named by the cited items, no raw id.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CitedAnswer } from "../app/agents/cited-answer";
import { DocumentsTab } from "../app/knowledge/kg-documents";
import { SearchCompare } from "../app/knowledge/kg-search-compare";
import { RulesReview, thresholdValue } from "../app/knowledge/rules-review";
import { citedIdsIn, withCitationLinks } from "../lib/answer-citations";
import type { KgRule } from "../lib/kg-view-types";
import { mockKgFetch } from "./_helpers/kg-fetch";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), usePathname: () => "/", useSearchParams: () => new URLSearchParams() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const press = async (el: Element | null) => { await act(async () => (el as HTMLElement).click()); await settle(); };
const W = { projectId: "northpeak-lending", actor: "ceo" };
const PACKS = { current: { pack_id: "banking-dq", version: "1" }, available: [{ pack_id: "banking-dq", version: "1" }] };

const section = (key: string, title: string, kind: string, page: number, first: string[] = []) => ({
  key, title, label: title, kind, page, page_end: page, first_lines: first, line_count: 3, chars: 120,
});
const PREVIEW = {
  upload_id: "kgup_1", title: "NorthPeak Bank: Loan Data Quality Rulebook v3.2", filename: "NorthPeak_Loan_DQ_Rulebook_v3.2.pdf",
  pages: 45, status: "pending", pattern: null, proposed_by_kind: "person", document_id: null,
  counts: { sections: 3, rule_cards: 1, headings: 1, other: 1 },
  sections: [
    section("s1", "NorthPeak Bank: Loan Data Quality Rulebook v3.2", "title_page", 1, ["Version 3.2 · Effective 2026-07-01"]),
    section("s2", "0.1 Purpose", "heading", 3, ["This rulebook sets out the data quality rules"]),
    section("s3", "NPB-DQ-LN-003 v2 — Principal outstanding reconciles to its components", "rule_card", 15,
            ["Statement: The loan balance the bank reports must equal what was disbursed"]),
  ],
};
const MERGED = { ...PREVIEW, counts: { sections: 2, rule_cards: 1, headings: 0, other: 1 },
                 sections: [PREVIEW.sections[0], { ...PREVIEW.sections[2], key: "s2" }] };

function postsTo(fn: ReturnType<typeof mockKgFetch>, path: RegExp) {
  return fn.mock.calls.filter(([u, i]) => path.test(new URL(String(u), "http://x").pathname) && i?.method === "POST")
    .map(([, i]) => JSON.parse(String(i?.body)));
}

describe("Documents › Upload a document", () => {
  it("uploads the file, shows the proposed sections, and writes only when a person confirms", async () => {
    const fn = mockKgFetch({
      "/v1/knowledge/packs$": { body: PACKS },
      "/v1/knowledge/documents$": { body: { documents: [] } },
      "/v1/knowledge/documents/uploads$": (u) => ({ body: u ? PREVIEW : PREVIEW }),
      "/v1/knowledge/documents/uploads/kgup_1/confirm$": { body: { document_id: "kgn_doc1", clause_ids: [], title: PREVIEW.title, sections: 3, rule_cards: 1 } },
    });
    await mount(<DocumentsTab {...W} />);
    const input = $('[data-testid="kg-upload-file"]') as HTMLInputElement;
    const file = new File([new Uint8Array([37, 80, 68, 70, 45])], "rulebook.pdf", { type: "application/pdf" });
    Object.defineProperty(input, "files", { value: [file] });
    await act(async () => { input.dispatchEvent(new Event("change", { bubbles: true })); });
    await settle();
    const [staged] = postsTo(fn, /\/documents\/uploads$/);
    expect(staged).toMatchObject({ project_id: "northpeak-lending", actor: "ceo", step_up: "confirmed", filename: "rulebook.pdf" });
    expect(atob(staged.content_base64)).toBe("%PDF-");
    // The preview: titles, kinds, pages, first lines, counts — and nothing written yet.
    expect($('[data-testid="kg-upload-preview"]')).toBeTruthy();
    expect($('[data-testid="kg-upload-counts"]')?.textContent).toContain("45 pages, 3 sections: 1 rule cards, 1 headings, 1 other");
    const rows = $$('[data-testid="kg-upload-section"]');
    expect(rows).toHaveLength(3);
    expect(rows[2].getAttribute("data-kind")).toBe("rule_card");
    expect(rows[2].textContent).toContain("Rule card");
    expect(rows[2].textContent).toContain("p. 15");
    expect(rows[2].textContent).toContain("The loan balance the bank reports");
    expect($('[data-testid="kg-upload-note"]')?.textContent).toContain("Nothing is in the Knowledge Graph yet");
    expect(postsTo(fn, /\/confirm$/)).toHaveLength(0);
    await press($('[data-testid="kg-upload-confirm"]'));
    expect(postsTo(fn, /\/uploads\/kgup_1\/confirm$/)).toEqual([{ project_id: "northpeak-lending", actor: "ceo", step_up: "confirmed" }]);
    expect($('[data-testid="kg-upload-done"]')?.textContent).toContain("3 sections, 1 of them rule cards");
    expect($('[data-testid="kg-upload-preview"]')).toBeNull();
  });

  it("merge, split and drop change the proposal on the server; discard writes nothing", async () => {
    const fn = mockKgFetch({
      "/v1/knowledge/packs$": { body: PACKS },
      "/v1/knowledge/documents$": { body: { documents: [] } },
      "/v1/knowledge/documents/uploads$": { body: { uploads: [PREVIEW] } },
      "/uploads/kgup_1/edit$": { body: MERGED },
      "/uploads/kgup_1/sections/s2$": { body: { key: "s2", lines: ["NPB-DQ-LN-003 v2 — Principal…", "Statement: …", "Check: …"] } },
      "/uploads/kgup_1/discard$": { body: { ...PREVIEW, status: "discarded" } },
    });
    await mount(<DocumentsTab {...W} />);
    expect($$('[data-testid="kg-upload-section"]')).toHaveLength(3);
    await press($$('[data-testid="kg-upload-merge"]')[1]);
    expect(postsTo(fn, /\/edit$/)[0].edits).toEqual([{ op: "merge", key: "s2" }]);
    expect($$('[data-testid="kg-upload-section"]')).toHaveLength(2);
    // The last section cannot merge with what is not there.
    expect(($$('[data-testid="kg-upload-merge"]')[1] as HTMLButtonElement).disabled).toBe(true);
    await press($$('[data-testid="kg-upload-split"]')[1]);
    expect($('[data-testid="kg-upload-lines"]')?.textContent).toContain("Statement: …");
    await press($$('[data-testid="kg-upload-split-here"]')[1]);
    expect(postsTo(fn, /\/edit$/)[1].edits).toEqual([{ op: "split", key: "s2", line: 2 }]);
    await press($$('[data-testid="kg-upload-drop"]')[0]);
    expect(postsTo(fn, /\/edit$/)[2].edits).toEqual([{ op: "drop", key: "s1" }]);
    await press($('[data-testid="kg-upload-discard"]'));
    expect(postsTo(fn, /\/discard$/)).toHaveLength(1);
    expect(postsTo(fn, /\/confirm$/)).toHaveLength(0);
    expect($('[data-testid="kg-upload-done"]')?.textContent).toBe("Discarded: nothing was added.");
  });

  it("a long uploaded document lists 8 sections, then all, and offers Extract rules", async () => {
    const clauses = Array.from({ length: 12 }, (_, i) => ({
      id: `kgn_c${i}`, section: `NPB-DQ-LN-00${i}`, text: `NPB-DQ-LN-00${i} v1 — Rule ${i}\nStatement: s${i}`, citation: "", url: "",
      page: 15 + i, page_end: 15 + i, kind: i ? "rule_card" : "heading", heading: i ? `NPB-DQ-LN-00${i} v1 — Rule ${i}` : "6. Loan master rules",
    }));
    const fn = mockKgFetch({
      "/v1/knowledge/packs$": { body: PACKS },
      "/v1/knowledge/documents$": { body: { documents: [{ id: "kgn_doc1", title: "Rulebook", source_url: "", citation: "", added_by: "ceo", added_at: "", filename: "rb.pdf", pages: 45, clauses }] } },
      "/api/delivery-workflows$": { body: { run: { id: "wfr_rbx_1" } } },
    });
    await mount(<DocumentsTab {...W} />);
    expect($('[data-testid="kg-doc-file"]')?.textContent).toContain("45 pages, 12 sections, 11 rule cards");
    expect($$('[data-testid="kg-doc-clause"]')).toHaveLength(8);
    expect($$('[data-testid="kg-doc-check"]')).toHaveLength(0);
    expect($$('[data-testid="kg-doc-clause-page"]')[1].textContent).toBe("p. 16");
    await press($('[data-testid="kg-doc-more"]'));
    expect($$('[data-testid="kg-doc-clause"]')).toHaveLength(12);
    await press($('[data-testid="kg-extract-rules"]'));
    const start = fn.mock.calls.find(([u, i]) => String(u).includes("/api/delivery-workflows") && i?.method === "POST");
    expect(JSON.parse(String(start?.[1]?.body))).toMatchObject({ action: "start", workflow_key: "rulebook_extraction", input: { document_id: "kgn_doc1" } });
    expect($('[data-testid="kg-extract-msg"]')?.textContent).toContain("Each rule it finds waits in Rules review");
    expect($('[data-testid="kg-extract-msg"] a')?.getAttribute("href")).toContain("wfr=wfr_rbx_1");
  });

  it("Extract rules installs the workflow on a project that predates it, or says why it cannot", async () => {
    const clauses = [{ id: "kgn_c1", section: "NPB-DQ-LN-001", text: "NPB-DQ-LN-001 v1 — Rule", citation: "", url: "",
                       page: 15, page_end: 15, kind: "rule_card", heading: "NPB-DQ-LN-001 v1 — Rule" }];
    const docs = { documents: [{ id: "kgn_doc1", title: "Rulebook", source_url: "", citation: "", added_by: "ceo", added_at: "", filename: "rb.pdf", pages: 45, clauses }] };
    const installs = (skipped?: string) => {
      let n = 0;
      return mockKgFetch({
        "/v1/knowledge/packs$": { body: PACKS },
        "/v1/knowledge/documents$": { body: docs },
        // start (no workflow) → install → start again.
        "/api/delivery-workflows$": (u) => {
          if (u.search) return { body: {} };             // a read (?view=…), not a press
          n += 1;
          if (n === 1) return { status: 400, body: { error: "There is no delivery workflow 'rulebook_extraction' in this project." } };
          if (n === 2) return { body: { installed: [{ key: "rulebook_extraction", version_id: skipped ? null : "v1", ...(skipped ? { skipped } : {}) }] } };
          return { body: { run: { id: "wfr_rbx_2" } } };
        },
      });
    };
    const actions = (fn: ReturnType<typeof mockKgFetch>) => fn.mock.calls
      .filter(([u, i]) => String(u).includes("/api/delivery-workflows") && i?.method === "POST")
      .map(([, i]) => JSON.parse(String(i?.body)).action);

    let fn = installs();
    await mount(<DocumentsTab {...W} />);
    await press($('[data-testid="kg-extract-rules"]'));
    // "install" is the proxy's action name ("catalogue/install" was refused as unknown).
    expect(actions(fn)).toEqual(["start", "install", "start"]);
    expect($('[data-testid="kg-extract-msg"] a')?.getAttribute("href")).toContain("wfr=wfr_rbx_2");
    unmount();

    fn = installs("needs an ontology pack granting the 'data_quality_rule' kind(s)");
    await mount(<DocumentsTab {...W} />);
    await press($('[data-testid="kg-extract-rules"]'));
    expect(actions(fn)).toEqual(["start", "install"]);
    expect($('[data-testid="kg-extract-msg"]')?.textContent).toContain("needs an ontology pack granting the 'data_quality_rule' kind(s)");
  });
});

describe("Documents › DQ sample", () => {
  const status = (o: Record<string, unknown> = {}) => ({
    available: true, title: PREVIEW.title, filename: PREVIEW.filename, pending_upload_id: null, document_id: null,
    sections: 0, rules: { proposed: 0, approved: 0 }, workflow_key: "rulebook_extraction", can_extract: true,
    extract_blocked_reason: null, ...o });

  it("loads the shipped rulebook through the same preview and confirm", async () => {
    let added = false;
    const fn = mockKgFetch({
      "/v1/knowledge/packs$": { body: PACKS },
      "/v1/knowledge/documents$": { body: { documents: [] } },
      "/v1/knowledge/samples/dq-rulebook$": () => ({ body: added ? status({ document_id: "kgn_doc1", sections: 136, rules: { proposed: 96, approved: 2 } }) : status() }),
      "/uploads/kgup_1/confirm$": () => { added = true; return { body: { document_id: "kgn_doc1", clause_ids: [], title: PREVIEW.title, sections: 136, rule_cards: 96 } }; },
    });
    await mount(<DocumentsTab {...W} />);
    expect($('[data-testid="kg-dq-sample-state"]')?.textContent).toBe("Not loaded");
    // The POST answers with the preview (the GET of the same path answers the status).
    fn.mockImplementationOnce(async () => new Response(JSON.stringify(PREVIEW), { status: 200 }));
    await press($('[data-testid="kg-dq-load"]'));
    expect($('[data-testid="kg-dq-sample"] [data-testid="kg-upload-preview"]')).toBeTruthy();
    await press($('[data-testid="kg-dq-sample"] [data-testid="kg-upload-confirm"]'));
    expect($('[data-testid="kg-dq-msg"]')?.textContent).toContain("136 sections, 96 of them rule cards");
    expect($('[data-testid="kg-dq-sample-state"]')?.textContent).toBe("Loaded: 136 sections, 96 rules waiting for review, 2 approved");
    expect($('[data-testid="kg-dq-sample"] [data-testid="kg-extract-rules"]')).toBeTruthy();
  });

  it("says why rules cannot be extracted when the project has no data-quality kinds", async () => {
    mockKgFetch({
      "/v1/knowledge/packs$": { body: PACKS },
      "/v1/knowledge/documents$": { body: { documents: [] } },
      "/v1/knowledge/samples/dq-rulebook$": { body: status({ document_id: "kgn_doc1", sections: 136, can_extract: false,
        extract_blocked_reason: "This project's ontology has no data-quality kinds (data_quality_rule). Assign the banking-dq pack." }) },
    });
    await mount(<DocumentsTab {...W} />);
    const button = $('[data-testid="kg-dq-sample"] [data-testid="kg-extract-rules"]') as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect($('[data-testid="kg-extract-blocked"]')?.textContent).toContain("Assign the banking-dq pack");
  });
});

const DQ_RULE: KgRule = {
  node: { id: "kgn_dq_ln003", key: "NPB-DQ-LN-003", kind: "data_quality_rule", layer: "domain",
          label: "NPB-DQ-LN-003 — Principal outstanding reconciles to its components", trust_tier: "inferred" },
  kind: "data_quality_rule", title: "Principal outstanding reconciles to its components",
  statement: "The loan balance the bank reports must equal what was disbursed, less what was repaid and written off.",
  rationale: null, category: "validation", scope: { repo: null, areas: [], packages: [], symbols: [] }, status: "proposed",
  confidence: 0.95, review: null, version: 1, previous_id: null, evidence: [], scan_id: "rbx_1",
  provenance: { created_by: "rulebook_analyst", created_by_kind: "agent", agent_role: "rulebook_analyst", agent_name: "Rubric",
                run_id: null, at: "2026-11-01T10:00:00Z", events: [] } as unknown as KgRule["provenance"],
  clause_evidence: [{ clause_id: "kgn_clause_ln003", section: "NPB-DQ-LN-003", document: "NorthPeak Bank: Loan Data Quality Rulebook v3.2",
                      citation: "…, p. 15", page: 15, page_end: 15, quote: "Check: For every active loan, principal outstanding equals…" }],
  dq: {
    rule_id: "NPB-DQ-LN-003", version: 2, title: "Principal outstanding reconciles to its components", dimension: "accuracy",
    severity: "High", check: "For every active loan, principal outstanding equals principal disbursed minus …",
    owner: "Loan Operations", system: "S-LSP", feed: "F-01", cdes: ["CDE-LN-001"],
    columns: ["m_loan.principal_outstanding_derived"], tables: ["m_loan"],
    report_lines: ["Call Report RC-C Part I item 12 (RCON2122)"], mdrm: ["RCON2122"], related: ["NPB-DQ-GL-002"],
    legal_reference: null, page: 15,
    thresholds: [
      { level: "warning", text: "more than 0.05% of active loans fail at COB.", metric: "rate", value: 0.0005, node_id: "kgn_t1" },
      { level: "error", text: "more than 0.25% of active loans fail at COB.", metric: "rate", value: 0.0025, node_id: "kgn_t2" },
    ],
    links: { linked: { cde: ["CDE-LN-001"], column: ["m_loan.principal_outstanding_derived"] }, unmatched: { report_line: ["BHCK2122"] } },
  },
};

describe("Rules review › a data-quality rule", () => {
  it("shows the rule id, check, thresholds, what it protects and the clause it was read from", async () => {
    mockKgFetch({ "/view/rules$": { body: { items: [DQ_RULE], counts: { proposed: 1, approved: 0, rejected: 0, superseded: 0 }, scan_id: null, scans: [] } } });
    const onFocusNode = vi.fn();
    await mount(<RulesReview {...W} onFocusNode={onFocusNode} />);
    const card = $('[data-rule="kgn_dq_ln003"]')!;
    expect(card.getAttribute("data-kind")).toBe("data_quality_rule");
    expect($('[data-testid="kg-dq-id"]')?.textContent).toBe("NPB-DQ-LN-003 v2");
    expect(card.textContent).toContain("accuracy");
    expect(card.textContent).toContain("High severity");
    const t = $$('[data-testid="kg-dq-threshold"]');
    expect(t.map((x) => x.getAttribute("data-level"))).toEqual(["warning", "error"]);
    expect(t[0].textContent).toContain("Warning whenmore than 0.05% of active loans fail at COB.");
    expect(t[1].textContent).toContain("failure rate > 0.25%");
    expect(card.textContent).toContain("CDE-LN-001");
    expect($('[data-testid="kg-dq-links"]')?.textContent).toBe("Linked in the graph: 1 CDE, 1 column. Not in the graph yet: 1 report line.");
    const clause = $('[data-testid="kg-rule-clause"]')!;
    expect(clause.textContent).toContain("NorthPeak Bank: Loan Data Quality Rulebook v3.2, NPB-DQ-LN-003, p. 15");
    expect(clause.textContent).toContain("Check: For every active loan");
    await press($('[data-testid="kg-clause-focus"]'));
    expect(onFocusNode).toHaveBeenCalledWith("kgn_clause_ln003");
    expect(card.textContent).not.toMatch(/kgn_/);
  });

  it("edits a data-quality rule by title and statement only", async () => {
    const fn = mockKgFetch({ "/view/rules$": { body: { items: [DQ_RULE], counts: { proposed: 1, approved: 0, rejected: 0, superseded: 0 }, scan_id: null, scans: [] } } });
    await mount(<RulesReview {...W} onFocusNode={() => {}} />);
    await press($('[data-testid="kg-rule-edit-open"]'));
    expect($('[data-testid="kg-edit-category"]')).toBeNull();
    await act(async () => { ($('[data-testid="kg-rule-edit"]') as HTMLFormElement).requestSubmit(); });
    await settle();
    const [edit] = postsTo(fn, /\/rules\/kgn_dq_ln003\/edit$/);
    expect(Object.keys(edit).sort()).toEqual(["actor", "project_id", "statement", "step_up", "title"]);
  });

  it("says a threshold in the rulebook's own units", () => {
    expect(thresholdValue({ metric: "rate", value: 0.0005 })).toBe("0.05%");
    expect(thresholdValue({ metric: "amount_usd", value: 25000 })).toBe("USD 25,000");
    expect(thresholdValue({ metric: "minutes", value: 30 })).toBe("30 min");
    expect(thresholdValue({ metric: "count", value: 5 })).toBe("5");
    expect(thresholdValue({ metric: "count", value: null })).toBeNull();
  });
});

describe("Search by words and by meaning", () => {
  const hit = (id: string, label: string) => ({ node: { id, key: id, kind: "data_quality_rule", layer: "domain", label, trust_tier: "inferred" }, path: null, match: "meaning", score: 0.9 });
  it("shows both columns and marks what only meaning found", async () => {
    mockKgFetch({
      "/view/search/compare$": { body: {
        q: "What happens when a payment is short?", meaning_available: true, reason: null,
        words: [hit("kgn_tx010", "NPB-DQ-TX-010 — A short payment never marks the installment completed")],
        meaning: [hit("kgn_tx009", "NPB-DQ-TX-009 — A short payment is held in suspense until a full periodic payment"),
                  hit("kgn_tx010", "NPB-DQ-TX-010 — A short payment never marks the installment completed")],
        only_by_meaning: ["kgn_tx009"] } },
    });
    const onPick = vi.fn();
    await mount(<SearchCompare w={W} q="What happens when a payment is short?" onPick={onPick} onClose={() => {}} />);
    expect($$('[data-testid="kg-cmp-words"] [data-testid="kg-cmp-hit"]')).toHaveLength(1);
    const meaning = $$('[data-testid="kg-cmp-meaning"] [data-testid="kg-cmp-hit"]');
    expect(meaning).toHaveLength(2);
    expect(meaning[0].querySelector('[data-testid="kg-cmp-only"]')?.textContent).toBe("only by meaning");
    expect(meaning[1].querySelector('[data-testid="kg-cmp-only"]')).toBeNull();
    expect($('[data-testid="kg-cmp-summary"]')?.textContent).toBe("1 of the 2 found by meaning share no searched words with the query.");
    await press(meaning[0]);
    expect(onPick.mock.calls[0][0].node.id).toBe("kgn_tx009");
  });

  it("without an embedding model the meaning column says why", async () => {
    mockKgFetch({ "/view/search/compare$": { body: { q: "x", words: [], meaning: [], meaning_available: false,
      reason: "No embedding model is set up for this project.", only_by_meaning: [] } } });
    await mount(<SearchCompare w={W} q="x" onPick={() => {}} onClose={() => {}} />);
    expect($('[data-testid="kg-cmp-meaning"]')?.textContent).toContain("No embedding model is set up");
  });
});

describe("Ask BOSS › citations", () => {
  const answer = "Two rules protect it: [R:kgn_dq_ln003] and [R:kgn_dq_gl002], read from [D:kgn_clause_ln003].";
  it("turns citations into named links and never shows an id", () => {
    expect(citedIdsIn(answer)).toEqual(["kgn_dq_ln003", "kgn_dq_gl002", "kgn_clause_ln003"]);
    const md = withCitationLinks(answer, { kgn_dq_ln003: { title: "NPB-DQ-LN-003 — Principal outstanding", kind: "data_quality_rule", kind_label: "Data quality rule" } },
                                 (id) => `/?kg_node=${id}`);
    expect(md).toBe("Two rules protect it: [NPB-DQ-LN-003 — Principal outstanding](/?kg_node=kgn_dq_ln003) and [a cited item](/?kg_node=kgn_dq_gl002), read from [a cited item](/?kg_node=kgn_clause_ln003).");
  });

  it("renders the answer with clickable citations by name", async () => {
    mockKgFetch({ "/view/names$": (u) => ({ body: { names: {
      kgn_dq_ln003: { title: "NPB-DQ-LN-003 — Principal outstanding reconciles to its components", kind: "data_quality_rule", kind_label: "Data quality rule", layer: "domain" },
      kgn_dq_gl002: { title: "NPB-DQ-GL-002 — Principal outstanding reconciles to the GL", kind: "data_quality_rule", kind_label: "Data quality rule", layer: "domain" },
      kgn_clause_ln003: { title: "NPB-DQ-LN-003", kind: "clause", kind_label: "Clause", layer: "domain" },
      ...(u.searchParams.get("ids") ? {} : {}) } } }) });
    await mount(<CitedAnswer {...W} text={answer} />);
    const cites = $$('[data-testid="agent-cite"]');
    expect(cites.map((c) => c.getAttribute("data-kind"))).toEqual(["data_quality_rule", "data_quality_rule", "clause"]);
    expect(cites[0].textContent).toBe("Data quality ruleNPB-DQ-LN-003 — Principal outstanding reconciles to its components");
    expect(cites[0].getAttribute("href")).toContain("kg_node=kgn_dq_ln003");
    const body = document.body.textContent ?? "";
    expect(body).not.toContain("[R:");
    expect(body).not.toMatch(/kgn_/);
  });
});

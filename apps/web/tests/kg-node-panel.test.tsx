// @vitest-environment happy-dom
/**
 * THE DETAIL PANEL (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.6), on the real disburseLoan() method and
 * the composed rule and learning: every section in order (header, statement, attributes,
 * connections with Show all paging, vector, learned → used, provenance, actions), each connection
 * clickable, and the actions pre-greyed for a viewer without the permission they need.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NodePanel, type BubbleSummary } from "../app/knowledge/node-panel";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import type { KgNodeDetailResponse } from "../lib/kg-view-types";
import { FX, calls, mockKgFetch } from "./_helpers/kg-fetch";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), usePathname: () => "/", useSearchParams: () => new URLSearchParams() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const me = { principal_id: "usr_v", display_name: "V", org: { id: "o", name: "O" }, csrf_token: "c", epoch: 1 } as unknown as MeResponse;
const org = (permissions: string[]): MePermissionsResponse => ({ epoch: 1, scope: "org", permissions, step_up_fresh_until: null, catalogue_fingerprint: "x" });

const METHOD = FX.detail.node.id;
const rule = FX.rules.items.find((r) => r.status === "proposed")!;
const learning = FX.live.learnings[0];

const RULE_DETAIL: KgNodeDetailResponse = {
  ...FX.detail, node: rule.node, statement: rule.statement, path: null, line: null, end_line: null, area: null, package: null, language: null,
  attrs: { category: rule.category, confidence: rule.confidence, status: rule.status },
  connections: [{ kind: "evidenced_by", direction: "out", label: "Evidence", total: 1, items: [FX.hood.nodes[1]] }],
  provenance: rule.provenance, lineage: null,
};
const LEARN_DETAIL: KgNodeDetailResponse = {
  ...FX.detail, node: learning.node, statement: learning.statement, attrs: { detail: learning.detail, confidence: learning.confidence },
  connections: [], provenance: learning.provenance,
  vector: { embedded: true, model_id: "azure:text-embedding-3-small:1536", dimensions: 1536, neighbours: [{ node: FX.live.learnings[1].node, score: 0.82 }] },
  lineage: { learned: { run_id: "run_fixture_builder_1", agent_role: "backend_engineer", agent_name: "Builder", step: "implement-repair1", at: learning.at },
             used: [{ run_id: "run_fixture_builder_2", agent_role: "backend_engineer", agent_name: "Builder", how: "consulted", at: "2026-10-24T12:00:00Z" }] },
};

function Panel(p: Partial<React.ComponentProps<typeof NodePanel>>) {
  return <NodePanel projectId="northpeak-bank-b" actor="ceo" nodeId={METHOD} onFocusNode={() => {}} {...p} />;
}

describe("the detail panel", () => {
  it("says what to do when nothing is selected", async () => {
    mockKgFetch();
    await mount(<Panel nodeId={null} />);
    expect($('[data-testid="kg-panel-none"]')?.textContent).toMatch(/Select a node/);
  });

  it("a method: header, attributes with the code link, connections, vector reason, provenance — in that order", async () => {
    mockKgFetch();
    await mount(<Panel />);
    expect($('[data-testid="kg-panel-layer"]')?.textContent).toBe("Code");
    expect($('[data-testid="kg-panel-kind"]')?.textContent).toBe("Method · Java");
    expect($('[data-testid="kg-panel-title"]')?.textContent).toBe("disburseLoan()");
    expect($('[data-testid="kg-panel-copy"]')).not.toBeNull();
    const link = $('[data-testid="kg-panel-code-link"]') as HTMLAnchorElement;
    expect(link.href).toBe("https://github.com/paramg/northpeak-fineract-b/blob/HEAD/fineract-provider/src/main/java/org/apache/fineract/portfolio/loanaccount/service/LoanWritePlatformServiceJpaRepositoryImpl.java#L317-L320");
    expect(link.textContent).toContain("LoanWritePlatformServiceJpaRepositoryImpl.java:317–320");
    const attrs = $('[data-testid="kg-panel-attrs"]')!.textContent!;
    for (const t of ["org.apache.fineract.portfolio.loanaccount.service", "fineract-provider", "method", "java"]) expect(attrs).toContain(t);
    expect($('[data-testid="kg-panel-conn-summary"]')?.textContent).toBe("Calls 119 · References 23 · Dispatches to 20 · Overrides 1 · Called by 5 · Dispatched from 3 · Defined in 1");
    expect($('[data-testid="kg-panel-vector-reason"]')?.textContent).toBe("Not embedded: code symbols are matched by structure, not meaning");
    expect($('[data-testid="kg-panel-provenance"]')?.textContent).toContain("Indexed from the repository on ");
    const order = $$('[data-testid^="kg-panel-"]').map((e) => e.getAttribute("data-testid"))
      .filter((t) => ["kg-panel-attrs", "kg-panel-connections", "kg-panel-vector", "kg-panel-provenance", "kg-panel-why"].includes(t!));
    expect(order).toEqual(["kg-panel-attrs", "kg-panel-connections", "kg-panel-vector", "kg-panel-provenance", "kg-panel-why"]);
  });

  it("each connection focuses its node; Show all reads the next 20 of that group", async () => {
    const fn = mockKgFetch();
    const onFocusNode = vi.fn();
    await mount(<Panel onFocusNode={onFocusNode} />);
    const calls1 = $('[data-testid="kg-conn-group"][data-kind="calls"][data-direction="out"]')!;
    expect(calls1.querySelectorAll('[data-testid="kg-conn-item"]')).toHaveLength(20);
    await act(async () => (calls1.querySelector('[data-testid="kg-conn-item"]') as HTMLButtonElement).click());
    expect(onFocusNode).toHaveBeenCalledWith(FX.detail.connections[0].items[0].id, FX.detail.connections[0].items[0]);
    // The next page: the route answers the group's next items.
    const next = FX.hood.nodes.slice(1, 21);
    fn.mockClear();
    mockKgFetch({ "/view/node/": () => ({ body: { ...FX.detail, connections: [{ ...FX.detail.connections[0], items: next }] } }) });
    await act(async () => (calls1.querySelector('[data-testid="kg-conn-more"]') as HTMLButtonElement).click());
    await settle();
    expect(calls.some((c) => c.includes("edge=calls") && c.includes("direction=out") && c.includes("offset=20"))).toBe(true);
    expect(calls1.querySelectorAll('[data-testid="kg-conn-item"]')).toHaveLength(40);
    expect(calls1.querySelector('[data-testid="kg-conn-more"]')?.textContent).toContain("40 of 119");
  });

  it("why and blast radius (moved from the old explorer) read on press", async () => {
    mockKgFetch();
    await mount(<Panel />);
    await act(async () => ($('[data-testid="kg-why"]') as HTMLButtonElement).click());
    await settle();
    expect($('[data-testid="kg-why-list"]')?.textContent).toContain("A loan is disbursed only once it is approved");
    await act(async () => ($('[data-testid="kg-blast"]') as HTMLButtonElement).click());
    await settle();
    expect($('[data-testid="kg-blast-list"]')?.textContent).toContain("bulkLoanDisbursal");
  });

  it("a proposed rule: statement with its trust, provenance with the agent, and Approve / Edit / Reject", async () => {
    mockKgFetch({ [`/view/node/${rule.node.id}$`]: { body: RULE_DETAIL } });
    const onEditRule = vi.fn();
    await mount(<Panel nodeId={rule.node.id} onEditRule={onEditRule} />);
    expect($('[data-testid="kg-panel-layer"]')?.textContent).toBe("Rules");
    expect($('[data-testid="kg-panel-statement"]')?.textContent).toContain(rule.statement);
    expect($('[data-testid="kg-panel-trust"]')?.textContent).toBe("Proposed · not yet trusted by agents");
    expect($('[data-testid="kg-panel-attrs"]')?.textContent).toContain(`${Math.round(rule.confidence * 100)}%`);
    expect($('[data-testid="kg-panel-provenance"]')?.textContent).toContain("Proposed by Scout (Chief Architect)");
    expect($('[data-testid="kg-panel-provenance"]')?.textContent).toContain("its “scan” step");
    await act(async () => ($('[data-testid="kg-panel-edit"]') as HTMLButtonElement).click());
    expect(onEditRule).toHaveBeenCalledWith(rule.node.id);
    await act(async () => ($('[data-testid="kg-panel-approve"]') as HTMLButtonElement).click());
    await settle();
    expect(calls.some((c) => c.startsWith("POST") && c.includes(`/rules/${rule.node.id}/approve`))).toBe(true);
    expect($('[data-testid="kg-panel-msg"]')?.textContent).toMatch(/Approved/);
  });

  it("an approved rule names who approved it and when", async () => {
    const approved = FX.rules.items.find((r) => r.status === "approved")!;
    mockKgFetch({ "/view/node/": { body: { ...RULE_DETAIL, node: approved.node, statement: approved.statement, provenance: approved.provenance } } });
    await mount(<Panel nodeId={approved.node.id} />);
    expect($('[data-testid="kg-panel-trust"]')?.textContent).toMatch(/^Approved by the operator · /);
    expect($('[data-testid="kg-panel-approve"]')).toBeNull();
    expect($('[data-testid="kg-panel-timeline"]')?.querySelectorAll("li")).toHaveLength(2);
  });

  it("a learning: what was learned, its detail, the vector neighbours, learned → used, and Open in Factory run", async () => {
    mockKgFetch({ "/view/node/": { body: LEARN_DETAIL } });
    const onFocusNode = vi.fn();
    await mount(<Panel nodeId={learning.node.id} onFocusNode={onFocusNode} />);
    expect($('[data-testid="kg-panel-layer"]')?.textContent).toBe("Learning");
    expect($('[data-testid="kg-panel-kind"]')?.textContent).toContain("Test failure");
    expect($('[data-testid="kg-panel-statement"]')?.textContent).toContain(learning.statement);
    expect($('[data-testid="kg-panel-attrs"]')?.textContent).toContain("LinkedAccountRequiredException");
    expect($('[data-testid="kg-panel-vector-model"]')?.textContent).toContain("1,536 dimensions");
    await act(async () => ($('[data-testid="kg-panel-vector"] [data-testid="kg-conn-item"]') as HTMLButtonElement).click());
    expect(onFocusNode).toHaveBeenCalledWith(FX.live.learnings[1].node.id, FX.live.learnings[1].node);
    const lineage = $('[data-testid="kg-panel-lineage"]')!.textContent!;
    expect(lineage).toContain("Learned by Builder (Backend Engineer)");
    expect(lineage).toContain("its “implement repair1” step");
    expect(lineage).toContain("used by Builder");
    expect(($('[data-testid="kg-panel-open-run"]') as HTMLAnchorElement).getAttribute("href")).toBe("/?area=code_tasks&build=ctr_fixture_build_1");
  });

  it("a lesson from incidents lists where it was learned and where it was applied, each opening its incident", async () => {
    const LESSON: KgNodeDetailResponse = { ...LEARN_DETAIL, usage: { shown: 0, consulted_runs: 1, reused_runs: 1,
      incidents: [{ id: "kgn_inc_1", label: "INC-1", how: "taught" }, { id: "kgn_inc_2", label: "INC-2", how: "reused" }] } };
    mockKgFetch({ "/view/node/": { body: LESSON } });
    const onFocusNode = vi.fn();
    await mount(<Panel nodeId={learning.node.id} onFocusNode={onFocusNode} />);
    expect($('[data-testid="kg-panel-incidents"]')?.textContent).toBe("Learned in INC-1 · applied in INC-2");
    await act(async () => ($$('[data-testid="kg-panel-incident"]')[1] as HTMLButtonElement).click());
    expect(onFocusNode).toHaveBeenCalledWith("kgn_inc_2");
  });

  it("an area bubble is summarised from the overview, with its edges clickable and Open its packages", async () => {
    mockKgFetch();
    const area = FX.overview.areas[0];
    const other = FX.overview.areas[1];
    const edge = FX.overview.edges[0];
    const bubble: BubbleSummary = { node: area, out: [{ edge, other }], in: [] };
    const onDrill = vi.fn();
    const onFocusNode = vi.fn();
    await mount(<Panel nodeId={area.id} bubble={bubble} onDrill={onDrill} onFocusNode={onFocusNode} />);
    expect(calls.filter((c) => c.includes("/view/node/"))).toHaveLength(0);
    expect($('[data-testid="kg-panel-kind"]')?.textContent).toBe("Area");
    expect($('[data-testid="kg-panel-attrs"]')?.textContent).toContain("3,832");
    expect($('[data-testid="kg-panel-connections"]')?.textContent).toContain("16,749 calls");
    await act(async () => ($('[data-testid="kg-panel-drill"]') as HTMLButtonElement).click());
    expect(onDrill).toHaveBeenCalledWith(area.id);
    expect($('[data-testid="kg-scan"]')).not.toBeNull();
  });

  it("Scan for rules here shows the cost preview before anything starts, then starts the workflow", async () => {
    mockKgFetch();
    const area = FX.overview.areas[0];
    await mount(<Panel nodeId={area.id} bubble={{ node: area, out: [], in: [] }} />);
    await act(async () => ($('[data-testid="kg-scan"]') as HTMLButtonElement).click());
    expect($('[data-testid="kg-scan-preview"]')?.textContent).toContain("up to 60 files");
    expect(calls.some((c) => c.startsWith("POST"))).toBe(false);
    await act(async () => ($('[data-testid="kg-scan-start"]') as HTMLButtonElement).click());
    await settle();
    expect(calls.some((c) => c.startsWith("POST /api/delivery-workflows"))).toBe(true);
    expect($('[data-testid="kg-scan-msg"]')?.textContent).toMatch(/Scan started/);
  });

  it("a fact can be forgotten, after a confirm", async () => {
    mockKgFetch();
    await mount(<Panel nodeId="kgn_fixture_fact_01" />);
    await act(async () => ($('[data-testid="kg-panel-forget"]') as HTMLButtonElement).click());
    await act(async () => ($('[data-testid="kg-panel-forget-yes"]') as HTMLButtonElement).click());
    await settle();
    expect(calls.some((c) => c.startsWith("POST /v1/knowledge/facts/kgn_fixture_fact_01/forget"))).toBe(true);
  });

  it("without rule review the actions are greyed with the reason and do nothing", async () => {
    mockKgFetch({ [`/view/node/${rule.node.id}$`]: { body: RULE_DETAIL } });
    await mount(
      <IdentityProvider me={me} org={org(["knowledge:read", "project:read"])}>
        <Panel nodeId={rule.node.id} />
      </IdentityProvider>,
    );
    const approve = $('[data-testid="kg-panel-approve"]')!;
    expect(approve.getAttribute("aria-disabled")).toBe("true");
    expect(approve.getAttribute("data-permission")).toBe("knowledge:review");
    expect(approve.getAttribute("title")).toMatch(/^Needs “.+” · ask an /);
    await act(async () => approve.click());
    await settle();
    expect(calls.some((c) => c.startsWith("POST"))).toBe(false);
  });

  it("on an archived project, approve and scan are disabled with the reason", async () => {
    mockKgFetch({ [`/view/node/${rule.node.id}$`]: { body: RULE_DETAIL } });
    await mount(<Panel nodeId={rule.node.id} archived />);
    const approve = $('[data-testid="kg-panel-approve"]') as HTMLButtonElement;
    expect(approve.disabled).toBe(true);
    expect(approve.title).toMatch(/archived/);
  });

  it("a node that is gone says so, with Retry", async () => {
    mockKgFetch({ "/view/node/": { status: 404, body: { detail: "no such node" } } });
    await mount(<Panel />);
    expect($('[data-testid="kg-panel-error"]')?.textContent).toContain("no longer in the graph");
    expect($('[data-testid="kg-panel-error"] button')?.textContent).toBe("Retry");
  });
});

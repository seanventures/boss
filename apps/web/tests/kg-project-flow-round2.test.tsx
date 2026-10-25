// @vitest-environment happy-dom
/**
 * KNOWLEDGE GRAPH PROJECT FLOW, TESTER ROUND 2 (BOSSAPP-768). Each finding, rendered:
 * a group heading is not a node; provenance in words (never "the code index" for a learning,
 * "an agent (not recorded)" before names were kept); a scan by what it read and when; the
 * lifecycle card for any project with a code graph; words, not ids, in a headline; inline code;
 * a live refresh keeps the tree open.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useSearchParams: () => null, useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), usePathname: () => "/" }));

import { KgLifecycleCard } from "../app/knowledge/kg-lifecycle";
import { InlineCode, NodePanel } from "../app/knowledge/node-panel";
import { ProjectTree } from "../app/knowledge/project-tree";
import { RulesReview } from "../app/knowledge/rules-review";
import { withoutIds } from "../lib/actor-words";
import { FX, calls, mockKgFetch } from "./_helpers/kg-fetch";
import { $, $$, mount, rerender, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
beforeEach(() => window.history.replaceState(null, "", "/?area=knowledge"));
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("a tree group heading is not a node", () => {
  it("selecting 'Investigations' summarises the group and never asks for it as a node", async () => {
    mockKgFetch();
    await mount(<NodePanel projectId="p" actor="ceo" nodeId="learning:investigation" onFocusNode={() => {}}
                           preview={{ id: "learning:investigation", key: "learning:investigation", kind: "group", layer: "learning",
                                      label: "Investigations", trust_tier: "observed", counts: { items: 3 } }} />);
    expect($('[data-testid="kg-panel-error"]')).toBeNull();
    expect($('[data-testid="kg-panel-group"]')?.textContent).toContain("3 items in this group.");
    expect(calls.some((c) => c.includes("/view/node/"))).toBe(false);
  });
});

describe("provenance in words", () => {
  const learned = (provenance: Record<string, unknown>) => ({
    ...FX.detail, node: { ...FX.detail.node, kind: "investigation", layer: "learning", label: "Why the allocator overbooks" },
    statement: "The `mifos-standard-strategy` allocator skips the decrement.", lineage: null,
    provenance: { created_by: "system", at: "2026-10-25T05:32:22Z", events: [{ at: "2026-10-25T05:32:22Z", what: "recorded", by: "an agent (not recorded)" }], ...provenance },
  });

  it("a learning recorded before names were kept says so in words, never the indexer", async () => {
    mockKgFetch({ "/view/node/": { body: learned({ created_by_kind: "agent", who: "an agent (not recorded)" }) } });
    await mount(<NodePanel projectId="p" actor="ceo" nodeId="kgn_l" onFocusNode={() => {}} />);
    await settle();
    expect($('[data-testid="kg-panel-who"]')?.textContent).toBe("an agent, before BOSS kept agents' names");
    expect($('[data-testid="kg-panel-provenance"]')?.textContent).not.toMatch(/code index|knowledge\.learning/);
    // Backticked code in what was learned is code.
    expect($('[data-testid="kg-panel-statement"] code')?.textContent).toBe("mifos-standard-strategy");
  });

  it("an agent's learning names it, its role and its Passport; BOSS itself is 'BOSS, the general agent'", async () => {
    mockKgFetch({ "/view/node/": { body: learned({ created_by: "sre", created_by_kind: "agent", agent_role: "sre",
                                                   agent_name: "Beacon", agent_id: "agt_7f3a9c2e11d84b0a", who: "Beacon" }) } });
    await mount(<NodePanel projectId="p" actor="ceo" nodeId="kgn_l" onFocusNode={() => {}} />);
    await settle();
    expect($('[data-testid="kg-panel-who"]')?.textContent).toBe("Beacon (SRE)");
    expect($('[data-testid="kg-panel-passport"]')?.getAttribute("data-passport")).toBe("agt_7f3a9c2e11d84b0a");
    unmount();
    mockKgFetch({ "/view/node/": { body: learned({ created_by_kind: "agent", agent_role: "boss", agent_name: "BOSS (general)", who: "BOSS (general)" }) } });
    await mount(<NodePanel projectId="p" actor="ceo" nodeId="kgn_l" onFocusNode={() => {}} />);
    await settle();
    expect($('[data-testid="kg-panel-who"]')?.textContent).toBe("BOSS, the general agent");
  });

  it("inline code renders only backticks, nothing else", () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    return mount(<p><InlineCode text={"Use `a_b()` then <b>x</b> and `c`"} /></p>).then(() => {
      expect($$("code").map((c) => c.textContent)).toEqual(["a_b()", "c"]);
      expect($("p")?.textContent).toContain("<b>x</b>");
    });
  });
});

describe("a scan by what it read and when", () => {
  it("the Scan filter lists the scan's title and date, never rscan_…", async () => {
    mockKgFetch({ "/view/rules$": { body: { ...FX.rules, scans: [{ id: "rscan_caa44184979a_scan",
      label: "Business rules scan: loan repayment schedule transaction processors · Oct 25, 02:50", rules: 11 }] } } });
    await mount(<RulesReview projectId="p" actor="ceo" onFocusNode={() => {}} />);
    await settle();
    const options = [...($('[data-testid="kg-review-scan"]') as HTMLSelectElement).options].map((o) => o.textContent ?? "");
    expect(options).toContain("Business rules scan: loan repayment schedule transaction processors · Oct 25, 02:50 (11 rules)");
    expect(options.some((o) => /rscan_/.test(o))).toBe(false);
  });
});

describe("the lifecycle card for any project with a code graph", () => {
  const setup = (over: Record<string, unknown>) => ({
    project_id: "northpeak-bank", name: "NorthPeak Bank", project_type: "app", archived: false, has_graph: true,
    steps: [{ key: "import", state: "done", done: true, summary: "" }, { key: "team", state: "current", done: false, summary: "" },
            { key: "scan", state: "done", done: true, summary: "" }, { key: "review", state: "done", done: true, summary: "" }],
    next: "team", done_count: 3, repositories: [{ repo: "acme/fineract", status: "indexed", graph: { nodes: 51221, edges: 1 } }], code_nodes: 51221,
    team: { chosen: false, role_keys: [], candidates: [] }, scans: { count: 2, running: 0, completed: 1, latest: null },
    scan_defaults: { max_files: 60, max_rules: 25, budget_usd: 2 }, areas: [], rules: { proposed: 0, approved: 11, rejected: 0 }, learnings: 3,
    ...over,
  });

  it("an app project with 51k code nodes gets it; one without a graph gets nothing", async () => {
    mockKgFetch({ "/v1/knowledge/setup$": { body: setup({}) } });
    await mount(<KgLifecycleCard projectId="northpeak-bank" actor="ceo" projectType="app" />);
    await settle();
    expect($('[data-testid="kg-lifecycle-code"] .kg-life-value')?.textContent).toBe("51,221");
    expect($('[data-testid="kg-lifecycle-continue"]')?.textContent).toBe("Continue: Assign the team");
    unmount();
    mockKgFetch({ "/v1/knowledge/setup$": { body: setup({ has_graph: false, code_nodes: 0, repositories: [] }) } });
    await mount(<KgLifecycleCard projectId="helpdesk" actor="ceo" projectType="app" />);
    await settle();
    expect($('[data-testid="kg-lifecycle"]')).toBeNull();
  });
});

describe("words, not ids", () => {
  it("a headline drops the record ids in it", () => {
    expect(withoutIds("Reconcile expired model invocation minv_5733948397584a8182c7")).toBe("Reconcile expired model invocation");
    expect(withoutIds("Run wfr_6267c5d3caa44184979a failed.")).toBe("Run failed.");
    expect(withoutIds("Install a BOSS self-fix? fix(engine): keep the rail")).toBe("Install a BOSS self-fix? fix(engine): keep the rail");
    expect(withoutIds("knowledge_graph is on")).toBe("knowledge_graph is on");
  });
});

describe("a live refresh keeps the tree open", () => {
  it("bumping refreshKey re-reads the open groups instead of collapsing them", async () => {
    mockKgFetch();
    const tree = (k: number) => <ProjectTree projectId="p" actor="ceo" tab="rules" onTab={() => {}} query="" selectedId={null} onPick={() => {}} refreshKey={k} />;
    await mount(tree(0));
    const first = $$('[data-testid="kg-tree-row"]')[0];
    await act(async () => (first.querySelector(".kg-row-caret") as HTMLElement).click());
    await settle();
    const opened = $$('[data-testid="kg-tree-row"]').length;
    expect(opened).toBeGreaterThan(1);
    await rerender(tree(1));
    await settle();
    expect($$('[data-testid="kg-tree-row"]').length).toBe(opened);
    expect($$('[data-testid="kg-tree-row"]')[0].getAttribute("aria-expanded")).toBe("true");
  });
});

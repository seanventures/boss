// @vitest-environment happy-dom
/**
 * PROVENANCE IN WORDS, EVERYWHERE (KG tester round 4, P1). The panels said "Indexed by the code
 * index", "scan id rscan_caa44184979a_scan", "content key d376f6deefbc3548", "Run wfr_6267c5d3… ↗
 * · step scan", "Approved by an owner (legacy record)", an "Approved by" node named "ceo",
 * "approved by person", "Written by an agent (not recorded)". One formatter (lib/kg-provenance.ts)
 * now writes every line, and this test fails on any raw id pattern in what the node panel (code,
 * rule and learning) and the Rules review render as provenance.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useSearchParams: () => null, useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), usePathname: () => "/" }));

import { NodePanel } from "../app/knowledge/node-panel";
import { RulesReview } from "../app/knowledge/rules-review";
import { agentName, eventWords, linesText, personName, provenanceLines, rawIdsIn } from "../lib/kg-provenance";
import type { KgNodeDetailResponse, KgProvenance } from "../lib/kg-view-types";
import { FX, mockKgFetch } from "./_helpers/kg-fetch";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
beforeEach(() => window.history.replaceState(null, "", "/?area=knowledge&org=o1"));
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

/** Everything a panel renders AS PROVENANCE: who and where from, the timeline, the trust line,
 *  the lineage, the attributes, and the people it connects to. */
function renderedProvenance(): string {
  const parts = ["kg-panel-provenance", "kg-panel-trust", "kg-panel-lineage", "kg-panel-attrs"]
    .map((id) => $(`[data-testid="${id}"]`)?.textContent ?? "");
  const people = $$('[data-testid="kg-panel-connections"] .kg-conn-label').map((el) => el.textContent ?? "");
  return [...parts, ...people].join(" | ");
}

const RAW_RULE: KgNodeDetailResponse = {
  ...FX.detail,
  node: { id: "kgn_rule_cap", key: "rule:cap", kind: "rule", layer: "rules", label: "Charge-payment allocated amount capped", trust_tier: "approved", status: "approved" },
  statement: "The allocated amount is capped at the charge outstanding.",
  repo: null, path: null, line: null, end_line: null, area: null, package: null, language: null,
  attrs: { status: "approved", category: "limit", confidence: 0.8, scan_id: "rscan_caa44184979a_scan", content_key: "d376f6deefbc3548",
           dedupe_key: "579cbd01e6ed7861", seen_count: 1, injectable: true, last_seen_run: "wfr_6267c5d3caa4aa11" },
  connections: [{ kind: "approved_by", direction: "out", label: "Approved by", total: 1,
                  items: [{ id: "kgn_person_ceo", key: "person:ceo", kind: "person", layer: "domain", label: "the operator", trust_tier: "observed" }] }],
  lineage: null,
  provenance: {
    created_by: "chief_architect", created_by_kind: "agent", agent_role: "chief_architect", agent_name: "Chief Architect",
    agent_id: "agt_4xy05zgxem", workflow_run_id: "wfr_6267c5d3caa4aa11", run_id: "wfr_6267c5d3caa4aa11", step: "scan",
    run_title: "Business rules scan: loanschedule.domain (interest calc)", run_at: "2026-10-25T02:50:00Z",
    model_id: "claude-opus-5-5", at: "2026-10-25T02:51:00Z", approved_by: "ceo", approved_by_name: null, approved_at: "2026-10-25T03:00:00Z",
    events: [{ at: "2026-10-25T02:51:00Z", what: "proposed", by: "Chief Architect" },
             { at: "2026-10-25T03:00:00Z", what: "approved", by: "ceo", by_name: null }],
  } as KgProvenance,
};

const CODE: KgNodeDetailResponse = {
  ...FX.detail,
  provenance: { ...FX.detail.provenance, source: { repo: "paramg/northpeak-fineract-b", sha: "63dd10e", branch: "develop", at: "2026-10-24T17:18:00Z" } } as KgProvenance,
};

const learning = (provenance: Partial<KgProvenance>, lineage: KgNodeDetailResponse["lineage"] = null): KgNodeDetailResponse => ({
  ...FX.detail,
  node: { id: "kgn_a9b18071c1564a04b1e2", key: "root_cause:x", kind: "root_cause", layer: "learning", label: "Advance-payment handler never decrements", trust_tier: "inferred" },
  statement: "It never decrements `transactionAmountRemaining`.",
  attrs: { learning_kind: "root_cause", confidence: 0.75, dedupe_key: "2996ebfff5011e73", subject_key: null, last_seen_at: "2026-10-25T07:20:40Z",
           evidence: [{ ref: "INC-2041 reconciliation rows", type: "metric" }] },
  connections: [], lineage,
  provenance: { created_by: "system", at: "2026-10-25T07:20:40Z", events: [{ at: "2026-10-25T07:20:40Z", what: "recorded", by: null }], ...provenance } as KgProvenance,
});

async function panel(detail: KgNodeDetailResponse) {
  mockKgFetch({ "/view/node/": { body: detail } });
  await mount(<NodePanel projectId="northpeak-bank" actor="ceo" orgId="o1" nodeId={detail.node.id} onFocusNode={() => {}} />);
  await settle();
}

describe("the node panel says where it came from in words", () => {
  it("code: Indexed from <repo>@<short sha> on <date>", async () => {
    await panel(CODE);
    const text = $('[data-testid="kg-panel-provenance"] .kg-prov li')!.textContent!;
    expect(text).toMatch(/^Indexed from paramg\/northpeak-fineract-b@63dd10e on Oct 2\d/);
    expect(rawIdsIn(renderedProvenance())).toEqual([]);
  });

  it("a rule: the agent, its role and Passport, the run by its title, the approver once — never a record key", async () => {
    await panel(RAW_RULE);
    const text = renderedProvenance();
    expect(rawIdsIn(text)).toEqual([]);
    expect(text).toContain("Proposed by Chief Architect · Passport");
    expect(text).toContain("In “Business rules scan: loanschedule.domain (interest calc)” ↗");
    expect(text).toContain("its “scan” step");
    expect(text).toContain("Approved by the operator");
    expect($('[data-testid="kg-panel-passport"]')?.getAttribute("data-passport")).toBe("agt_4xy05zgxem");
    expect($('[data-testid="kg-panel-passport"]')?.getAttribute("title")).toContain("agt_4xy05zgxem");   // the id, as a tooltip
    expect($('[data-testid="kg-panel-run-link"]')?.getAttribute("href")).toContain("wfr=wfr_6267c5d3caa4aa11");
    const timeline = $$('[data-testid="kg-panel-timeline"] li').map((li) => li.textContent);
    expect(timeline.filter((t) => /approved/.test(t ?? ""))).toHaveLength(1);
    expect(text).not.toMatch(/scan id|content key|dedupe key/);
  });

  for (const [name, prov, who] of [
    ["an addressed agent, with its Passport", { created_by: "sre", created_by_kind: "agent", agent_role: "sre", agent_name: "Site Reliability Engineer",
      agent_id: "agt_4xy05zgxem", step: "chat", conversation_title: "B4 · Incident: early repayments post more than the customer paid",
      conversation_at: "2026-10-25T07:13:13Z" }, "Site Reliability Engineer (SRE)"],
    ["BOSS itself", { created_by_kind: "agent", agent_role: "boss", agent_name: "BOSS (general)", who: "BOSS (general)", step: "chat" }, "BOSS, the general agent"],
    ["one written before names were kept", { created_by_kind: "agent", who: "an agent (not recorded)" }, "an agent, before BOSS kept agents' names"],
  ] as const) {
    it(`a learning by ${name}: in words`, async () => {
      await panel(learning(prov as Partial<KgProvenance>));
      expect($('[data-testid="kg-panel-who"]')?.textContent).toBe(who);
      expect(rawIdsIn(renderedProvenance())).toEqual([]);
      // Its evidence, incident id included, is shown (tester: INC-2041 was only in the attrs).
      expect($('[data-testid="kg-panel-evidence"]')?.textContent).toContain("INC-2041 reconciliation rows");
    });
  }

  it("lineage names the runs that used it by their titles", async () => {
    await panel(learning({ created_by_kind: "agent", agent_role: "sre", agent_name: "Site Reliability Engineer", workflow_run_id: "wfr_6267c5d3caa4aa11",
                           run_id: "wfr_6267c5d3caa4aa11", run_title: "Incident triage" },
      { learned: { run_id: "wfr_6267c5d3caa4aa11", agent_role: "sre", agent_name: "Site Reliability Engineer", step: "investigate", at: "2026-10-25T07:20:40Z" },
        used: [{ run_id: "athr_3edbe8c654bc4aacaeba", agent_role: "software_engineer", agent_name: null, how: "cited", at: "2026-10-25T08:00:00Z",
                 run_title: "B5 · Fix the advance-payment handler" },
               { run_id: "wfr_aa11bb22cc33dd44", agent_role: null, agent_name: null, how: "consulted", at: "2026-10-25T09:00:00Z" }] }));
    const lineage = $('[data-testid="kg-panel-lineage"]')!.textContent!;
    expect(lineage).toContain("Learned by Site Reliability Engineer (SRE) in “Incident triage”");
    expect(lineage).toContain("Software Engineer in “B5 · Fix the advance-payment handler” (cited");
    expect(lineage).toContain("an agent in a later run (consulted");
    expect(rawIdsIn(renderedProvenance())).toEqual([]);
  });
});

describe("the Rules review card says who in words", () => {
  it("proposer by name and role, reviewer by display name or 'the operator', never an id", async () => {
    const items = FX.rules.items.map((r, i) => ({ ...r, review: i === 0 ? { by: "ceo", at: "2026-10-25T03:00:00Z", decision_id: null, note: null } : r.review }));
    mockKgFetch({ "/view/rules$": { body: { ...FX.rules, items } } });
    await mount(<RulesReview projectId="p" actor="ceo" onFocusNode={() => {}} />);
    await settle();
    const metas = $$('[data-testid="kg-rule-card"] .kg-card-meta').map((m) => m.textContent ?? "");
    expect(metas.length).toBeGreaterThan(0);
    for (const m of metas) expect(rawIdsIn(m)).toEqual([]);
    expect(metas.join(" ")).toMatch(/(Approved|Rejected|Reviewed) by the operator/);
  });

  it("names the approver: the person's display name, else 'the operator' (round 6)", async () => {
    const items = FX.rules.items.slice(0, 2).map((r, i) => ({ ...r, status: "approved",
      review: { by: i === 0 ? "usr_example1abc" : "ceo", by_name: i === 0 ? "Example Person" : null, at: "2026-10-25T03:00:00Z", decision_id: null, note: null } }));
    mockKgFetch({ "/view/rules$": { body: { ...FX.rules, items } } });
    await mount(<RulesReview projectId="p" actor="ceo" onFocusNode={() => {}} />);
    await settle();
    expect($$('[data-testid="kg-rule-approver"]').map((e) => e.textContent?.split(" · ")[0]))
      .toEqual(["Approved by Example Person", "Approved by the operator"]);
  });
});

describe("the formatter", () => {
  it("never lets a raw id through, whatever the record holds", () => {
    const lines = linesText(provenanceLines(RAW_RULE.provenance, { layer: "rules", orgId: "o1" }));
    expect(lines.every((l) => rawIdsIn(l).length === 0)).toBe(true);
    expect(personName("usr_kdzhawzy7mxmp5hpb3bqlgqojs", null)).toBe("the operator");
    expect(personName("usr_x1y2z3a4", "Ada Lovelace")).toBe("Ada Lovelace");
    expect(personName("ceo", null)).toBe("the operator");
    expect(eventWords({ at: "x", what: "approved", by: "person" })).toBe("approved by the operator");
    expect(eventWords({ at: "x", what: "proposed", by: "chief_architect" })).toBe("proposed by Chief Architect");
    expect(agentName({ agent_name: "Atlas", agent_role: "chief_architect" })).toBe("Atlas (Chief Architect)");
    expect(rawIdsIn("Run wfr_6267c5d3caa4 ↗")).not.toEqual([]);
    expect(rawIdsIn("content key d376f6deefbc3548")).not.toEqual([]);
    expect(rawIdsIn("Indexed from acme/core@63dd10e on Oct 24")).toEqual([]);
  });
});

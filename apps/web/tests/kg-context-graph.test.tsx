// @vitest-environment happy-dom
/**
 * THE TWO GRAPHS ON ONE CANVAS (design §4.2, §4.3, §4.7.3): the Domain and Context chips, the
 * kinds named and coloured from the ontology packs (so a new pack needs no console change), a
 * case file's history grouped by kind, and the Context card — a decision in words, which step,
 * run, agent, versions and fingerprint, and its links, each one press from its own Focus. No raw
 * ids anywhere. The values are NorthPeak Lending's real compliance run (Regulation Z §1026.36).
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NodePanel, contextFacts } from "../app/knowledge/node-panel";
import { groupFocus, groupKey, kindName, kindToken } from "../lib/kg-layout";
import { layersParam } from "../lib/kg-view";
import {
  KG_LAYERS, kindMetaOf, kindStyleVars, kindVar, productLayerOf, registerKinds,
  type KgContextCard, type KgNodeDetailResponse, type KgViewNode,
} from "../lib/kg-view-types";
import { FX, mockKgFetch } from "./_helpers/kg-fetch";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), usePathname: () => "/", useSearchParams: () => new URLSearchParams() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const KINDS = {
  compliance_case: { layer: "context", label: "Compliance case", plural: "Compliance cases", color: "#ff9f4a" },
  remediation_package: { layer: "context", label: "Remediation package", plural: "Remediation packages", color: "#ff7a4a" },
  agent_decision: { layer: "context", label: "Agent decision", plural: "Agent decisions", color: "#38bdf8" },
  aml_case: { layer: "context", label: "AML case", plural: "AML cases", color: "#e11d48" },
  clause: { layer: "domain", label: "Clause", plural: "Clauses", color: "#8a6bff" },
} as const;

const node = (id: string, kind: string, layer: KgViewNode["layer"], label: string, sub?: string): KgViewNode =>
  ({ id, key: `${kind}:${id}`, kind, layer, label, sub, trust_tier: "inferred" });

const CASE = node("kgn_case_1", "compliance_case", "context", "Compliance case: “§1026.36(c)(1)(ii) Partial payments”", "Open case");
const RULE = node("kgn_rule_1", "rule", "rules", "Repayment-like transaction classification", "proposed · other");
const RUN = node("kgn_run_1", "run", "learning", "NorthPeak Regulatory Compliance (mine) · Nov 01, 05:26", "Workflow run");
const PRECEDENT = node("kgn_prec_1", "precedent", "learning", "Suspense handling is absent from the repayment processor");

const CARD: KgContextCard = {
  decision_type: "Confirmed",
  step: "Lane confirmation",
  statement: "Confirmed: “Repayment-like transaction classification” — the cited code only classifies transaction types and has no date-of-receipt crediting.",
  agent: { name: "Lane agent", kind: "agent", role: "Lane agent", fingerprint: "3fa9c0d1e2b4" },
  workflow_version: "v4",
  policy_version: "p7",
  fingerprint: "3fa9c0d1e2b4",
  run: { label: RUN.label, at: "2026-11-01T05:26:05Z", status: "completed", node_id: RUN.id, workflow_run_id: "wfr_63406397a6da435b8bbb" },
  at: "2026-11-01T05:28:31Z",
  links: [
    { key: "case", label: "Case", items: [CASE], total: 1 },
    { key: "run", label: "Run", items: [RUN], total: 1 },
    { key: "cites", label: "Cites", items: [RULE], total: 1 },
    { key: "precedent_produced", label: "Precedent produced", items: [PRECEDENT], total: 1 },
  ],
};

const DECISION: KgNodeDetailResponse = {
  ...FX.detail,
  node: node("kgn_dec_1", "agent_decision", "context", "Confirmed", "Lane confirmation step"),
  statement: CARD.statement ?? null, path: null, line: null, end_line: null, area: null, package: null, language: null, repo: null,
  attrs: { run_id: "wfr_63406397a6da435b8bbb", step_id: "lane_confirmation", decision_type: "Confirmed", agent_fingerprint: "sha256:3fa9c0d1e2b4", workflow_version: "wfv_9ad0851cfb564bfca282" },
  connections: [], lineage: null, context: CARD,
};

describe("the chips and the packs' kinds", () => {
  it("offers Code · Rules · Learning · Domain · Context, and All asks for both graphs", () => {
    expect([...KG_LAYERS]).toEqual(["code", "rules", "learning", "domain", "context"]);
    expect(layersParam(new Set())).toBe("code,rules,learning,domain,context");
    expect(layersParam(new Set(["context"]))).toBe("context");
  });

  it("names and colours a kind from its pack, so a new pack (an AML case) needs no console change", () => {
    expect(kindName("aml_case")).toBe("AML case");          // the shipped banking-aml kind is named even before its pack arrives
    registerKinds(KINDS as never);
    expect(kindMetaOf("aml_case")?.label).toBe("AML case");
    expect(kindName("aml_case")).toBe("AML case");
    expect(productLayerOf("aml_case")).toBe("context");
    expect(productLayerOf("agent_decision")).toBe("context");
    expect(productLayerOf("clause")).toBe("domain");
    expect(kindToken("compliance_case")).toBe(kindVar("compliance_case"));
    expect(kindStyleVars().vars[kindVar("compliance_case")]).toBe("#ff9f4a");
    // The layer bubbles have colours and names of their own.
    expect(kindToken("layer", null, "layer:context")).toBe("--kg-layer-context");
    expect(kindName("layer", null, "layer:domain")).toBe("Domain layer");
  });

  it("a case file's history folds by kind, in the pack's plural — never by package or id", () => {
    registerKinds(KINDS as never);
    expect(groupKey({ layer: "context", key: "x", kind: "agent_decision" })).toEqual({ key: "context:agent_decision", label: "agent decisions" });
    const center = CASE.id;
    const runs = Array.from({ length: 14 }, (_v, i) => ({ ...node(`kgn_r${i}`, "run", "learning", `Run ${i} · Nov 01, 0${i % 9}:00`), hop: 1 }));
    const g = groupFocus(center, [{ ...CASE, hop: 0 }, ...runs],
      runs.map((r) => ({ src: r.id, dst: center, kind: "worked_on", weight: 1, label: null })), { 1: 6 });
    const group = g.nodes.find((n) => n.kind === "group");
    expect(group?.label).toBe("+8 runs");
  });
});

describe("the Context card", () => {
  it("says the decision, where and who in words — no raw id, no wfr_ or wfv_", () => {
    const facts = contextFacts(CARD);
    expect(facts.map((f) => f.k)).toEqual(["Step", "Run", "Agent", "Workflow", "Policy", "Fingerprint", "When"]);
    expect(facts.find((f) => f.k === "Agent")?.v).toBe("Lane agent");
    expect(contextFacts({ ...CARD, agent: { name: "BOSS", kind: "deterministic", role: null, fingerprint: "deterministic" }, fingerprint: "deterministic" })
      .find((f) => f.k === "Agent")?.v).toBe("BOSS · a deterministic step (no model)");
    expect(contextFacts({ ...CARD, agent: { name: "Acme screening agent", kind: "outside", role: null, fingerprint: null } })
      .find((f) => f.k === "Agent")?.v).toBe("Acme screening agent · an outside agent");
    for (const f of facts) expect(f.v).not.toMatch(/\b(kgn|wfr|wfv)_/);
  });

  it("a decision's panel: Context badge, its type and statement, the facts, and links that focus", async () => {
    mockKgFetch({ "/view/node/kgn_dec_1$": { body: DECISION } });
    const focused: string[] = [];
    await mount(<NodePanel projectId="northpeak-lending" actor="ceo" nodeId="kgn_dec_1" onFocusNode={(id) => focused.push(id)} />);
    await settle();
    expect($('[data-testid="kg-panel-layer"]')?.textContent).toBe("Context");
    expect($('[data-testid="kg-ctx-type"]')?.textContent).toBe("Confirmed");
    // The sub-line is words, in the body font: monospace is for code paths only.
    expect($('[data-testid="kg-panel-sub"]')?.textContent).toBe("Lane confirmation step");
    expect($('[data-testid="kg-panel-sub"]')?.classList.contains("inf-mono")).toBe(false);
    expect($('[data-testid="kg-ctx-statement"]')?.textContent).toMatch(/Repayment-like transaction classification/);
    // The statement once, in the card (not again as a bare Statement section).
    expect($('[data-testid="kg-panel-statement"]')).toBeNull();
    const facts = $('[data-testid="kg-ctx-facts"]')?.textContent ?? "";
    expect(facts).toMatch(/Lane confirmation/);
    expect(facts).toMatch(/v4/);
    expect(facts).toMatch(/3fa9c0d1e2b4/);
    expect($('[data-testid="kg-ctx-open-run"]')?.getAttribute("href")).toMatch(/wfr=wfr_63406397a6da435b8bbb/);
    const heads = $$('[data-testid="kg-ctx-links"] .kg-ctx-link-head').map((h) => h.textContent);
    expect(heads).toEqual(["Case", "Run", "Cites", "Precedent produced"]);
    // The raw attributes (run id, fingerprint, version id) are the card's facts, never shown raw.
    const attrs = $('[data-testid="kg-panel-attrs"]')?.textContent ?? "";
    expect(attrs).not.toMatch(/wfr_|wfv_|sha256/);
    // Each link focuses its node.
    const items = $$('[data-testid="kg-ctx-links"] [data-testid="kg-conn-item"]') as HTMLButtonElement[];
    items[0].click();
    items[2].click();
    expect(focused).toEqual([CASE.id, RULE.id]);
  });
});

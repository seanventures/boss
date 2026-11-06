// @vitest-environment happy-dom
/**
 * Agents at work reaches the rest of the console (design §11.3, §11.5, §11.6):
 *   - Command Center's card counts Needs you · Running · Resolved today, each the newest item's
 *     diagram and a link into the area; it stays away until there is ambient work;
 *   - Needs you stays ONE queue: an ambient item waiting on a person is a row of the existing
 *     card, unless the decisions store already listed its decision;
 *   - an Ask BOSS answer citing a ledger row `[act:…]` links to that decision's flow chart.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { parseList } from "../../lib/ambient";
import { demoList } from "../../lib/ambient-demo";
import { citedActsIn, withActLinks } from "../../lib/answer-citations";
import { $$, mountAs, tid, unmount } from "../_helpers/multirepo-fixtures";

const { AgentsAtWorkCard, AmbientNeedRows, todayBuckets } = await import("../../app/command-center/agents-at-work");

afterEach(() => unmount());

const view = () => {
  const v = parseList(demoList("all"));
  const today = new Date().toISOString();
  return { ...v, items: v.items.map((i) => ({ ...i, resolved_at: i.resolved_at ? today : null })) };
};

describe("Command Center's Agents at work card", () => {
  it("counts need you, running and resolved today, each with a small diagram", async () => {
    await mountAs(<AgentsAtWorkCard view={view()} orgId="org_1" />);
    expect(tid("aaw-needs_you")?.textContent).toContain("1");
    expect(tid("aaw-running")?.textContent).toContain("2");
    expect(tid("aaw-resolved")?.textContent).toContain("2");
    for (const k of ["needs_you", "running", "resolved"]) expect(tid(`aaw-${k}`)?.querySelector('[data-testid="aa-thumb"]')).toBeTruthy();
    expect(tid("aaw-needs_you")?.getAttribute("href")).toBe("/?org=org_1&area=agent_ops&item=work_payments");
  });

  it("stays away until there is ambient work", async () => {
    await mountAs(<AgentsAtWorkCard view={{ filter: "all", items: [], counts: { needs_you: 0, running: 0 }, next_before: null }} />);
    expect(tid("cc-agents-at-work")).toBeNull();
  });

  it("counts only today's resolutions", () => {
    const v = parseList(demoList("all"));
    const old = { ...v, items: v.items.map((i) => ({ ...i, resolved_at: i.resolved_at ? "2020-01-01T00:00:00Z" : null, updated_at: "2020-01-01T00:00:00Z" })) };
    expect(todayBuckets(old).resolved.n).toBe(0);
  });
});

describe("Needs you is one queue", () => {
  it("lists an ambient item waiting on a person, with its diagram and Open", async () => {
    await mountAs(<div><AmbientNeedRows view={view()} listedDecisionIds={new Set()} /></div>);
    const rows = $$('[data-kind="ambient"]');
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain("payments p99 4.8s after release");
    expect(rows[0].querySelector('[data-testid="aa-thumb"]')).toBeTruthy();
    expect(tid("cc-need-ambient-open")?.getAttribute("href")).toContain("item=work_payments");
  });

  it("does not list it twice when its decision is already a row", async () => {
    await mountAs(<div><AmbientNeedRows view={view()} listedDecisionIds={new Set(["dec_prod_rb"])} /></div>);
    expect($$('[data-kind="ambient"]')).toHaveLength(0);
  });
});

describe("Ask BOSS cites the ledger", () => {
  it("turns [act:…] into links to the decision's flow chart, never showing the id", () => {
    const answer = "I rolled back staging [act:act_rb] after the squad voted 2–1 [act:act_rb2].";
    expect(citedActsIn(answer)).toEqual(["act_rb", "act_rb2"]);
    const linked = withActLinks(answer, "?org=org_1&area=agent");
    expect(linked).toContain("[see the flow 1 ↗](/?org=org_1&area=agent_ops&act=act_rb)");
    expect(linked).toContain("&act=act_rb2)");
    expect(linked.replace(/\(\/\?[^)]*\)/g, "")).not.toContain("act_rb");
  });
});

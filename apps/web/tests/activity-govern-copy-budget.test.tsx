// @vitest-environment happy-dom
/**
 * The Log and Govern read like the rest of the console (spec §5.1): no visible run of text over
 * six words outside a tooltip, no paragraphs, and none of the words these screens retired —
 * internals and jargon that used to be their copy ("ledger", "knob", "intensity", "utilization",
 * "deadletter", "nothing was invented"…). Walked over the rendered screens, not grepped.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { overBudget } from "./_helpers/copy-budget";
import { fakeGovernApi, newGovern } from "./_helpers/govern-fixtures";
import { FACETS, fakeLogApi, newLog, row, rows } from "./_helpers/log-fixtures";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { LogExplorer } = await import("../app/activity/log-explorer");
const { GovernHome } = await import("../app/govern/govern-home");

const BANNED = ["ledger", "deterministic", "knob", "ceo", "llm_calls", "governed", "enforced", "intensity",
                "envelope ", "deadletter", "invented", "utilization"];

/** Visible text that is the screen's own words (not the operator's, not a tooltip). */
function ownWords(root: Element): string {
  const out: string[] = [];
  for (const el of [root, ...root.querySelectorAll("*")]) {
    if (el.closest("[title], [data-user-text], [aria-hidden='true'], option, .ag-sr")) continue;
    out.push([...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? "").join(" "));
  }
  return out.join(" ").toLowerCase();
}

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("the Log's copy", () => {
  it("every state and the open drawer stay within the budget", async () => {
    const log = newLog({ page: { items: [...rows(4), row("x9", { source: "deadletter", kind: "job", level: "error",
                                                                  body: "gave up after three attempts on the worker" })] },
                         facets: FACETS });
    vi.stubGlobal("fetch", fakeLogApi(log));
    setConsoleLive(true);
    window.history.replaceState({}, "", "/?area=activity&wfr=wfr_1&step=plan&step_name=Plan+it&row=x9");
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    const root = $(".lx")!;
    expect(overBudget(root)).toEqual([]);
    expect(root.querySelectorAll("p")).toHaveLength(0);
    const words = ownWords(root);
    for (const banned of BANNED) expect(words, banned).not.toContain(banned);
  });
});

describe("Govern's copy", () => {
  it("every tab and an open editor stay within the budget", async () => {
    const g = newGovern();
    g.spend = { window: "7d", money_usd_micros: 3_000_000, included_usd_micros: 0,
                daily: [{ day: "2026-10-04", money_usd_micros: 3_000_000, included_usd_micros: 0 }], daily_cap_usd_micros: 10_000_000,
                scorecard: { calls: 9, cache_ratio: 0.6, by_stage: [{ stage: "implement", calls: 9, cost_usd_micros: 3_000_000 }] },
                savings: [{ kind: "cache_leverage", severity: "medium", finding: "Cache hit ratio is 12% across many tokens",
                            action: "Keep prefixes stable", potential_saving_usd_micros: 1_000_000 }],
                top_drivers: [{ id: "claude-sonnet", amount_usd_micros: 3_000_000 }], spent_vs_cap: g.view.meter };
    g.envelopes = { allocations: [{ id: "bua_1", parent_allocation_id: null, scope_type: "department", scope_id: "Platform",
                                    amount_usd_micros: 5_000_000, available_usd_micros: 4_000_000, reserved_usd_micros: 0,
                                    actual_usd_micros: 1_000_000, period_start: "2026-10-01", period_end: "2026-10-31" }],
                    finops: { state: "recorded", summary: { anomalies_open_count: 1,
                              spend_by_provider: [{ provider: "aws", cost_usd_micros: 9_000_000, event_count: 4 }] } },
                    reconciliations: [{ id: "r1", commitment_id: "c1", provider: "aws", committed_usd_micros: 10_000_000,
                                        actual_usd_micros: 4_000_000, utilization: 0.4, verdict: "under_utilized", decision_id: "dec_1" }],
                    initiative_rule: { tier_min: 2, amount_usd_micros: 50_000_000 } };
    vi.stubGlobal("fetch", fakeGovernApi(g));
    setConsoleLive(true);
    window.history.replaceState({}, "", "/?area=budget");
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-more-pace]"));
    await click($("[data-testid=gv-edit-pace]"));
    await act(async () => { await new Promise((r) => setTimeout(r, 200)); });
    await settle();
    for (const tab of ["limits", "spend", "envelopes"]) {
      if (tab !== "limits") await click($(`[data-testid=gv-tab-${tab}]`));
      const root = $("[data-testid=gv]")!;
      expect(overBudget(root), tab).toEqual([]);
      expect(root.querySelectorAll("p"), tab).toHaveLength(0);
      const words = ownWords(root);
      for (const banned of BANNED) expect(words, `${tab}: ${banned}`).not.toContain(banned);
    }
  });
});

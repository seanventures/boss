// @vitest-environment happy-dom
/**
 * The chart draws the selected window's days and titles itself with that window.
 * A window without its own days keeps the flat fourteen-day series.
 * The scorecard draws the selected window's agents when that list is present.
 * Included subscription value sits beside Spend and is not added into it.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it } from "vitest";

import type { ReportFeed } from "../lib/reports";

(globalThis as Record<string, unknown>).React = React;
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { ReportsView } = await import("../app/reports-ui");

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

type Day = { date: string; tasks: number; succeeded: number; failed: number };
type Agent = {
  role_key: string;
  display_name: string;
  trust_score: number;
  autonomy_level: number;
  successful_runs: number;
  policy_violations: number;
  scope_violations: number;
  last_reviewed_at: string | null;
  last_violation_at: string | null;
};

function day(date: string, tasks: number, succeeded: number, failed: number): Day {
  return { date, tasks, succeeded, failed };
}

function agent(key: string): Agent {
  return {
    role_key: key,
    display_name: key,
    trust_score: 50,
    autonomy_level: 1,
    successful_runs: 1,
    policy_violations: 0,
    scope_violations: 0,
    last_reviewed_at: null,
    last_violation_at: null,
  };
}

function agents(prefix: string, count: number): Agent[] {
  return Array.from({ length: count }, (_, i) => agent(`${prefix}-${String(i + 1).padStart(2, "0")}`));
}

function windowOf(daily?: Day[], extra?: { agents?: Agent[]; included?: number }) {
  return {
    tasks: 1,
    jobs: 1,
    jobs_succeeded: 1,
    jobs_failed: 0,
    decisions: 0,
    decisions_approved: 0,
    spend_usd_micros: 2_500_000,
    by_kind: [] as { kind: string; total: number; succeeded: number; failed: number; avg_seconds: number }[],
    by_model: [] as { model: string; count: number; spend_usd_micros: number }[],
    decision_latency: { count: 0, avg_seconds: 0 },
    ...(daily ? { daily } : {}),
    ...(extra && "agents" in extra ? { agents: extra.agents } : {}),
    ...(extra && "included" in extra ? { included_in_subscription_usd_micros: extra.included } : {}),
  };
}

function report(
  windows: { today: ReturnType<typeof windowOf>; week: ReturnType<typeof windowOf>; month: ReturnType<typeof windowOf> },
  flat: Day[],
  flatAgents: Agent[] = [],
): ReportFeed {
  return {
    apiReachable: true,
    data: {
      project_id: "p",
      generated_at: "2026-03-04T00:00:00Z",
      windows,
      daily: flat,
      by_kind: [],
      by_model: [],
      agents: flatAgents,
      decision_latency: { count: 0, avg_seconds: 0 },
    },
  };
}

async function show(feed: ReportFeed): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<ReportsView report={feed} />);
  });
  return host;
}

async function select(win: "today" | "week" | "month") {
  const tab = host!.querySelector(`[data-testid="rep-tab-${win}"]`) as HTMLButtonElement;
  await act(async () => {
    tab.click();
  });
}

function chartTitle(markup: string): string {
  return markup.match(/class="wf-section-title">([^<]*)<\/div>\s*<div class="rep-chart"/)?.[1] ?? "";
}

function chartDates(markup: string): string[] {
  return [...markup.matchAll(/class="rep-col" title="([^,"]+)/g)].map((m) => m[1]!);
}

describe("the report chart follows the selected window", () => {
  it("draws the week's days and titles the chart Last 7 days", () => {
    const markup = renderToStaticMarkup(
      <ReportsView
        report={report(
          {
            today: windowOf([day("2026-03-04", 1, 0, 0)]),
            week: windowOf([day("2026-03-02", 4, 3, 1), day("2026-03-03", 2, 2, 0)]),
            month: windowOf([day("2026-02-10", 8, 8, 0)]),
          },
          [day("2026-01-01", 9, 9, 0)],
        )}
      />,
    );
    expect(chartTitle(markup)).toBe("Last 7 days");
    expect(chartDates(markup)).toEqual(["2026-03-02", "2026-03-03"]);
  });

  it("keeps the fourteen-day series when the selected window has no days", () => {
    const markup = renderToStaticMarkup(
      <ReportsView
        report={report(
          { today: windowOf(), week: windowOf(), month: windowOf() },
          [day("2026-01-01", 9, 9, 0), day("2026-01-02", 1, 0, 1)],
        )}
      />,
    );
    expect(chartTitle(markup)).toBe("Activity, last 14 days");
    expect(chartDates(markup)).toEqual(["2026-01-01", "2026-01-02"]);
  });

  it("draws an empty window series instead of the fourteen-day chart", () => {
    const markup = renderToStaticMarkup(
      <ReportsView
        report={report(
          { today: windowOf(), week: windowOf([]), month: windowOf() },
          [day("2026-01-01", 9, 9, 0)],
        )}
      />,
    );
    expect(chartTitle(markup)).toBe("Last 7 days");
    expect(chartDates(markup)).toEqual([]);
  });

  it("retitles the chart when Today or Last 30 days is selected", async () => {
    const view = await show(
      report(
        {
          today: windowOf([day("2026-03-04", 1, 1, 0)]),
          week: windowOf([day("2026-03-02", 4, 3, 1)]),
          month: windowOf([day("2026-02-10", 8, 7, 1), day("2026-02-11", 1, 1, 0)]),
        },
        [day("2026-01-01", 9, 9, 0)],
      ),
    );
    expect(view.querySelector(".rep-chart")?.previousElementSibling?.textContent).toBe("Last 7 days");
    await select("today");
    expect(view.querySelector(".rep-chart")?.previousElementSibling?.textContent).toBe("Today");
    expect([...view.querySelectorAll(".rep-col")].map((col) => col.getAttribute("title")?.split(",")[0])).toEqual(["2026-03-04"]);
    await select("month");
    expect(view.querySelector(".rep-chart")?.previousElementSibling?.textContent).toBe("Last 30 days");
    expect([...view.querySelectorAll(".rep-col")].map((col) => col.getAttribute("title")?.split(",")[0])).toEqual(["2026-02-10", "2026-02-11"]);
  });
});

function rowKeys(markup: string): string[] {
  return [...markup.matchAll(/class="mono"> ([^<]+)</g)].map((m) => m[1]!);
}

describe("the agent scorecard follows the selected window", () => {
  it("draws the window's agents and says how many of that list are not on the page", () => {
    const weekAgents = agents("week", 15);
    const markup = renderToStaticMarkup(
      <ReportsView
        report={report(
          { today: windowOf(), week: windowOf(undefined, { agents: weekAgents }), month: windowOf() },
          [],
          agents("flat", 2),
        )}
      />,
    );
    expect(rowKeys(markup)).toEqual(weekAgents.slice(0, 12).map((a) => a.role_key));
    expect(markup).toContain("12 agents are on this page. 3 more are not.");
    expect(markup).toContain(">15 agents<");
    expect(markup).not.toContain("flat-01");
  });

  it("stays quiet when the window lists twelve or fewer, even if the flat list is longer", () => {
    const markup = renderToStaticMarkup(
      <ReportsView
        report={report(
          { today: windowOf(), week: windowOf(undefined, { agents: agents("week", 2) }), month: windowOf() },
          [],
          agents("flat", 15),
        )}
      />,
    );
    expect(rowKeys(markup)).toEqual(["week-01", "week-02"]);
    expect(markup).not.toContain("more are not");
    expect(markup).not.toContain("are on this page");
    expect(markup).not.toContain("flat-01");
  });

  it("draws no scorecard rows when the window's agent list is empty", () => {
    const markup = renderToStaticMarkup(
      <ReportsView
        report={report(
          { today: windowOf(), week: windowOf(undefined, { agents: [] }), month: windowOf() },
          [],
          agents("flat", 1),
        )}
      />,
    );
    expect(rowKeys(markup)).toEqual([]);
    expect(markup).not.toContain("flat-01");
    expect(markup).not.toContain("more are not");
  });

  it("draws Today's agents after that window is selected", async () => {
    const view = await show(
      report(
        {
          today: windowOf(undefined, { agents: agents("today", 1) }),
          week: windowOf(undefined, { agents: agents("week", 13) }),
          month: windowOf(),
        },
        [],
        agents("flat", 2),
      ),
    );
    expect(view.textContent).toContain("12 agents are on this page. 1 more are not.");
    await select("today");
    expect(view.textContent).toContain("today-01");
    expect(view.textContent).not.toContain("week-01");
    expect(view.textContent).not.toContain("more are not");
    await select("month");
    expect(view.textContent).toContain("flat-01");
    expect(view.textContent).not.toContain("more are not");
  });
});

function spendCard(markup: string): string {
  return [...markup.matchAll(/<div class="wf-stat[^"]*">[\s\S]*?<\/div>/g)].map((m) => m[0]).find((card) => card.includes(">Spend<")) ?? "";
}

describe("included subscription value sits beside Spend", () => {
  it("shows the window amount beside Spend and does not add it in", () => {
    const markup = renderToStaticMarkup(
      <ReportsView
        report={report(
          { today: windowOf(), week: windowOf(undefined, { included: 1_250_000 }), month: windowOf() },
          [],
        )}
      />,
    );
    const card = spendCard(markup);
    expect(card).toContain(">$2.50<");
    expect(card).toContain("$1.25 included in the subscription");
    expect(markup).not.toContain("$3.75");
  });

  it("omits the line when the window does not carry the field", () => {
    const markup = renderToStaticMarkup(
      <ReportsView report={report({ today: windowOf(), week: windowOf(), month: windowOf() }, [])} />,
    );
    expect(markup).not.toContain("included in the subscription");
  });

  it("still shows the line when the included amount is zero", () => {
    const markup = renderToStaticMarkup(
      <ReportsView
        report={report(
          { today: windowOf(), week: windowOf(undefined, { included: 0 }), month: windowOf() },
          [],
        )}
      />,
    );
    const card = spendCard(markup);
    expect(card).toContain(">$2.50<");
    expect(card).toContain("$0.00 included in the subscription");
  });

  it("follows the selected window's included amount", async () => {
    const view = await show(
      report(
        {
          today: windowOf(undefined, { included: 1_000_000 }),
          week: windowOf(),
          month: windowOf(undefined, { included: 0 }),
        },
        [],
      ),
    );
    expect(view.textContent).not.toContain("included in the subscription");
    await select("today");
    expect(view.textContent).toContain("$1.00 included in the subscription");
    expect(view.textContent).toContain("$2.50");
    expect(view.textContent).not.toContain("$3.50");
    await select("month");
    expect(view.textContent).toContain("$0.00 included in the subscription");
  });
});

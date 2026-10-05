// @vitest-environment happy-dom
/**
 * The Command Center's graphics tell the truth (command-center spec §10.2): every mark comes from
 * the view, nothing goes past now, nothing moves unless something changed, and the words fit the
 * copy budget. Rendered from the view fixture through the page's own parser.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { NOW_MS, flow, freshView, hours, quietView, view } from "./_helpers/command-center-view-fixture";
import { $, $$, mount, rerender, unmount } from "./_helpers/live-render";
import { parseCommandCenterView, type CommandCenterView } from "../lib/command-center-view";
import {
  cumulative, floorLayout, newComets, nowPosition, recencyOpacity, stripScale,
} from "../lib/command-center-graphics";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Floor } = await import("../app/command-center/floor");
const { TodayChart } = await import("../app/command-center/today-chart");
const { DayStrip } = await import("../app/command-center/day-strip");
const { WeekBars } = await import("../app/command-center/week-bars");

const V = (w: Record<string, unknown>) => parseCommandCenterView(w);
const noop = () => {};
const BANNED = /\b(capability|binding|governed|immutable|frontier|runtime|spec|namespace|fork|terminal|ETA|estimated|almost done|mission|office|alive|forecast|time to cap|projected|health score|AI-powered)\b|%/i;

const floor = (v: CommandCenterView, over: Partial<React.ComponentProps<typeof Floor>> = {}) => (
  <Floor view={v} projectId="p1" motion="full" band="wide" onYou={noop} onThread={noop} {...over} />
);

afterEach(() => unmount());

function visibleText(): string {
  const root = document.body.cloneNode(true) as HTMLElement;
  root.querySelectorAll(".wf-sr, [aria-hidden='true'], title").forEach((n) => n.remove());
  return root.textContent ?? "";
}

describe("the Floor", () => {
  it("draws a face for each agent at work and a dim dot for each idle one", async () => {
    const v = V(view());
    await mount(floor(v));
    const busy = v.agents.filter((a) => a.state !== "idle");
    const idle = v.agents.filter((a) => a.state === "idle");
    expect($$("[data-testid='cc-floor-face']")).toHaveLength(busy.length);
    expect($$("[data-testid='cc-floor-face'] .ag-avatar")).toHaveLength(busy.length);
    expect($$("[data-testid='cc-floor-dot']")).toHaveLength(idle.length);
    expect($$("[data-testid='cc-floor-dot'] .ag-avatar")).toHaveLength(0);
  });

  it("draws one edge per hand-off between known nodes, and none to a guessed one", async () => {
    const v = V(view());
    await mount(floor(v));
    expect($$("[data-testid='cc-floor-edge']")).toHaveLength(v.flows.length);
    await rerender(floor(V(view({ flows: [flow(), flow({ from: "nobody_here", newest_act_id: "act_x" })] }))));
    expect($$("[data-testid='cc-floor-edge']")).toHaveLength(1);
    expect($("[data-testid='cc-floor-edge']")?.getAttribute("data-tip")).toMatch(/^Juno → Forge · 3 hand-offs · \d+[mhd] ago$/);
  });

  it("threads run to You only from an approval that names its agent; stopped work is red", async () => {
    await mount(floor(V(view())));
    const threads = $$("[data-testid='cc-floor-thread']");
    expect(threads.map((t) => t.getAttribute("data-tone")).sort()).toEqual(["amber", "red"]);
    unmount();
    await mount(floor(V(view({ attention: [{ ...(view().attention as Record<string, unknown>[])[1], role_key: null }] }))));
    expect($$("[data-testid='cc-floor-thread']")).toHaveLength(0);
  });

  it("says the team map in words", async () => {
    await mount(floor(V(view())));
    expect($("svg[role='img']")?.getAttribute("aria-label"))
      .toBe("Team map: 2 working, 1 waiting on you, 6 hand-offs in the last hour");
    expect($("[data-testid='cc-floor-sr-list']")?.textContent).toContain("Forge");
  });

  it("all idle: a quiet ring and how long since the last work", async () => {
    await mount(<Floor view={V(quietView())} projectId="p1" motion="full" band="wide" onYou={noop} onThread={noop} />);
    expect($$("[data-testid='cc-floor-face']")).toHaveLength(0);
    expect($("[data-testid='cc-floor-footer']")?.textContent).toMatch(/^All idle · last work \d+h$/);
  });

  it("a fresh team says it is ready", async () => {
    await mount(floor(V(freshView())));
    expect($("[data-testid='cc-floor-footer']")?.textContent).toBe("Your team is ready");
    expect($("[data-testid='cc-floor-footer']")?.getAttribute("title")).toBe("7 agents, idle until work starts");
  });

  it("busy: n working · n idle; List shows the same facts as a list", async () => {
    await mount(floor(V(view())));
    expect($("[data-testid='cc-floor-footer']")?.textContent).toBe("3 working · 4 idle");
    $("[data-testid='cc-floor-list-toggle']")!.click();
    await rerender(floor(V(view())));
    expect($("[data-testid='cc-floor-list']")).not.toBeNull();
    expect($("[data-testid='cc-floor-stage']")).toBeNull();
    expect($("[data-testid='cc-floor-list-toggle']")?.getAttribute("aria-pressed")).toBe("true");
  });

  it("no agents: says so and links to Agents", async () => {
    await mount(floor(V(view({ agents: [], teams: [] }))));
    expect($("[data-testid='cc-floor-empty']")?.textContent).toContain("No agents yet");
  });

  it("a new act flies a comet with full motion, and none under reduced motion or none", async () => {
    const raf = vi.spyOn(globalThis, "requestAnimationFrame");
    await mount(floor(V(view())));
    expect(raf).not.toHaveBeenCalled(); // first paint: nothing flies
    await rerender(floor(V(view({ flows: [flow({ newest_act_id: "act_new" })] }))));
    expect(raf).toHaveBeenCalled();
    unmount();
    raf.mockClear();
    for (const motion of ["reduced", "none"] as const) {
      await mount(floor(V(view()), { motion }));
      await rerender(floor(V(view({ flows: [flow({ newest_act_id: `act_${motion}` })] })), { motion }));
      expect(raf).not.toHaveBeenCalled();
      expect($$(".cc-comet")).toHaveLength(0);
      unmount();
    }
    raf.mockRestore();
  });

  it("reduced motion lights the edge instead; none holds every face still and says why on You", async () => {
    await mount(floor(V(view()), { motion: "reduced" }));
    await rerender(floor(V(view({ flows: [flow({ newest_act_id: "act_lit" })] })), { motion: "reduced" }));
    expect($("[data-testid='cc-floor-edge']")?.hasAttribute("data-lit")).toBe(true);
    unmount();
    await mount(floor(V(view()), { motion: "none", motionWhy: "Motion paused while the host struggles" }));
    expect($("[data-testid='cc-floor-stage']")?.hasAttribute("data-still")).toBe(true);
    expect($$("[data-testid='cc-floor-face']").every((f) => f.hasAttribute("data-still"))).toBe(true);
    expect($("[data-testid='cc-floor-you']")?.getAttribute("title")).toBe("Motion paused while the host struggles");
  });

  it("at most eight faces move", async () => {
    const agents = Array.from({ length: 12 }, (_, i) => ({ role_key: `agent_${String(i).padStart(2, "0")}`, name: `A${i}`,
                                                          team: "engineering", state: "working", line: "Now: Coding" }));
    await mount(floor(V(view({ agents, flows: [], attention: [] }))));
    const moving = $$("[data-testid='cc-floor-face']").filter((f) => !f.hasAttribute("data-still"));
    expect(moving).toHaveLength(8);
  });

  it("narrow and phone draw the compact teams, never the ring", async () => {
    await mount(floor(V(view()), { band: "phone" }));
    expect($("[data-testid='cc-floor-compact']")).not.toBeNull();
    expect($("[data-testid='cc-floor-stage']")).toBeNull();
  });

  it("fits the copy budget and uses no banned word", async () => {
    await mount(floor(V(view())));
    expect(overBudget(document.body)).toEqual([]);
    expect(visibleText()).not.toMatch(BANNED);
  });
});

describe("floorLayout", () => {
  const v = V(view());
  it("is deterministic: same input, same coordinates", () => {
    expect(floorLayout(v.teams, v.agents, { width: 720, height: 440 }))
      .toEqual(floorLayout(v.teams, v.agents, { width: 720, height: 440 }));
  });
  it("never moves a node when only its state changes", () => {
    const a = floorLayout(v.teams, v.agents, { width: 720, height: 440 });
    const woke = v.agents.map((x) => (x.roleKey === "staff_engineer" ? { ...x, state: "working" as const } : x));
    expect(floorLayout(v.teams, woke, { width: 720, height: 440 }).nodes).toEqual(a.nodes);
  });
  it("puts Leadership at 12 o'clock and sorts members by role_key", () => {
    const l = floorLayout(v.teams, v.agents, { width: 720, height: 440 });
    expect(l.arcs[0].team).toBe("leadership");
    expect(l.nodes.filter((n) => n.team === "leadership").map((n) => n.roleKey)).toEqual(["ceo_agent", "pm"]);
    expect(l.radius).toBe(176);
  });
  it("shrinks idle dots past 48 agents and folds idle past 120", () => {
    const many = (n: number) => Array.from({ length: n }, (_, i) => ({ roleKey: `r${i}`, name: `R${i}`, team: "engineering", state: "idle" as const, line: "" }));
    expect(floorLayout([], many(49), { width: 720, height: 440 }).idleR).toBe(3);
    expect(floorLayout([], many(121), { width: 720, height: 440 }).arcs[0].idleFolded).toBe(121);
  });
});

describe("newComets", () => {
  const f = (id: string) => V(view({ flows: [flow({ newest_act_id: id })] })).flows[0];
  it("flies none on the first poll, only new acts after, at most twelve", () => {
    expect(newComets(null, [f("a")])).toEqual([]);
    expect(newComets([f("a")], [f("a"), f("b")]).map((x) => x.newestActId)).toEqual(["b"]);
    const lots = Array.from({ length: 20 }, (_, i) => f(`n${i}`));
    expect(newComets([], lots)).toHaveLength(12);
  });
  it("fades an edge by recency, never below 0.25", () => {
    expect(recencyOpacity(NOW_MS - 2 * 60_000, NOW_MS)).toBe(0.9);
    expect(recencyOpacity(NOW_MS - 90 * 60_000, NOW_MS)).toBe(0.25);
  });
});

describe("Today", () => {
  const v = V(view());
  it("the today line never goes past now; yesterday covers the day", () => {
    const { index, frac } = nowPosition(v.today.hours, NOW_MS);
    expect(index).toBe(39);
    expect(frac).toBeCloseTo(40 / 60, 3);
    const c = cumulative(v.today.hours, index, frac, "work");
    expect(Math.max(...c.today.map(([h]) => h))).toBeCloseTo(15 + 40 / 60, 3);
    expect(c.yesterday[c.yesterday.length - 1][0]).toBe(24);
    expect(c.todayTotal).toBe(14);
    expect(c.yesterdayTotal).toBe(11);
  });

  it("draws the line, a now dot labelled with today's number and yesterday's two labels", async () => {
    await mount(<TodayChart view={v} tab="work" onTab={noop} motion="full" nowMs={NOW_MS} tz="UTC" />);
    expect($("[data-testid='cc-today-line']")).not.toBeNull();
    expect($(".cc-today-now")?.textContent).toBe("14");
    const labels = $$(".cc-yday-label").map((t) => t.textContent);
    expect(labels[1]).toBe("11 all day");
    expect(labels[0]).toMatch(/^\d+ by now$/);
    expect($("figcaption")?.textContent).toMatch(/^Work today: 14 by 15:40\./);
    expect($$("table tbody tr")).toHaveLength(24);
  });

  it("with nothing on either day: no lines, the axis, and the words", async () => {
    const empty = V(view({ today: { ...(view().today as Record<string, unknown>), hours: hours() } }));
    expect(cumulative(empty.today.hours, 39, 0.5, "work").today).toEqual([]);
    await mount(<TodayChart view={empty} tab="work" onTab={noop} motion="full" nowMs={NOW_MS} tz="UTC" />);
    expect($("[data-testid='cc-today-line']")).toBeNull();
    expect($("[data-testid='cc-today-yesterday']")).toBeNull();
    expect($("[data-testid='cc-today-empty']")?.textContent).toBe("No work yet today");
    expect($(".cc-axis")).not.toBeNull();
  });

  it("Spend: a cap line only for a day cap in the viewer's own zone; otherwise the month meter", async () => {
    const dayCap = (tz: string) => V(view({ budget: { ...(view().budget as Record<string, unknown>), budget_timezone: tz,
                                                     day: { cap_usd_micros: 10_000_000, actual_usd_micros: 4_120_000 } } }));
    await mount(<TodayChart view={dayCap("UTC")} tab="spend" onTab={noop} motion="full" nowMs={NOW_MS} tz="UTC" />);
    expect($("[data-testid='cc-today-cap']")?.textContent).toBe("Cap $10");
    unmount();
    await mount(<TodayChart view={dayCap("Asia/Kolkata")} tab="spend" onTab={noop} motion="full" nowMs={NOW_MS} tz="UTC" />);
    expect($("[data-testid='cc-today-cap']")).toBeNull();
    expect($("[data-testid='cc-today-meter']")?.textContent).toBe("$84 of $200 · this month");
    unmount();
    await mount(<TodayChart view={v} tab="spend" onTab={noop} motion="full" nowMs={NOW_MS} tz="UTC" />);
    expect($("[data-testid='cc-today-cap']")).toBeNull();
    expect($("[data-testid='cc-today-meter']")?.textContent).toBe("$84 of $200 · this month");
    expect($(".cc-today-now")?.textContent).toBe("$4.12");
  });

  it("Spend all covered by a plan: money stays at zero and Included is drawn apart", async () => {
    const plan = V(view({ today: { ...(view().today as Record<string, unknown>),
                                   hours: hours({}, {}, { 36: 3_800_000 }), money_micros: 0 } }));
    await mount(<TodayChart view={plan} tab="spend" onTab={noop} motion="full" nowMs={NOW_MS} tz="UTC" />);
    expect($("[data-testid='cc-today-included'] text")?.textContent).toBe("Included $3.80");
  });

  it("the tabs press, carry their tooltips, and fit the copy budget", async () => {
    const onTab = vi.fn();
    await mount(<TodayChart view={v} tab="work" onTab={onTab} motion="full" nowMs={NOW_MS} tz="UTC" />);
    $("[data-testid='cc-today-tab-spend']")!.click();
    expect(onTab).toHaveBeenCalledWith("spend");
    expect($("[data-testid='cc-today-tab-work']")?.getAttribute("title")).toBe("Agent work finished, by hour");
    expect(overBudget(document.body)).toEqual([]);
    expect(visibleText()).not.toMatch(BANNED);
  });
});

describe("the LAST 24H strip", () => {
  const v = V(view());
  const strip = (over: Partial<React.ComponentProps<typeof DayStrip>> = {}, day = v.day) => (
    <DayStrip day={day} nowMs={NOW_MS} generatedAt={v.generatedAt} windowHours={24} rowsMax={10} motion="full"
              hoverRunId={null} onHover={noop} onStartWork={noop} {...over} />
  );

  it("a live bar ends at now with a ▶; a failed run ends in a red square; a finished one in a circle", async () => {
    await mount(strip());
    const live = $("[data-testid='cc-day-bar-wfr_182']")!;
    const left = parseFloat(live.style.left), width = parseFloat(live.style.width);
    expect(left + width).toBeCloseTo(100, 3);
    expect(live.querySelector(".cc-day-cap")?.getAttribute("data-cap")).toBe("live");
    expect($("[data-testid='cc-day-bar-wfr_170'] .cc-day-cap")?.getAttribute("data-cap")).toBe("failed");
    expect($("[data-testid='cc-day-bar-wfr_169'] .cc-day-cap")?.getAttribute("data-cap")).toBe("done");
    expect($("[data-testid='cc-day-bar-wfr_169'] .cc-day-clip")).not.toBeNull(); // started before the window
  });

  it("each bar links to its run and says it in full; gate spans say how long you were waited for", async () => {
    await mount(strip());
    const bar = $("[data-testid='cc-day-bar-wfr_182']") as HTMLAnchorElement;
    expect(bar.getAttribute("href")).toContain("wfr=wfr_182");
    expect(bar.getAttribute("aria-label")).toBe("Ship it #182 · Ship it · step 4 of 7 · Coding · 6h 40m");
    expect($("[data-testid='cc-day-bar-wfr_182'] [data-testid='cc-day-gate']")?.getAttribute("title")).toBe("Waited for you 18m");
    expect($$("[data-testid='cc-day-bar-wfr_182'] .cc-day-tick")).toHaveLength(3);
  });

  it("caps its rows and counts the rest, the server's too", async () => {
    await mount(strip({ rowsMax: 2 }, { ...v.day, more: 3 }));
    expect($$(".cc-day-bar")).toHaveLength(2);
    expect($("[data-testid='cc-day-more']")?.textContent).toBe("+5 more");
  });

  it("highlights the bar its MissionRow hovers", async () => {
    await mount(strip({ hoverRunId: "wfr_video" }));
    expect($("[data-testid='cc-day-bar-wfr_video']")?.hasAttribute("data-hover")).toBe(true);
  });

  it("empty: says so and offers a way to start", async () => {
    const start = vi.fn();
    await mount(strip({ onStartWork: start }, { since: v.day.since, rows: [], more: 0 }));
    expect($("[data-testid='cc-day-strip']")?.textContent).toContain("No runs in 24h");
    $("[data-testid='cc-day-start']")!.click();
    expect(start).toHaveBeenCalled();
  });

  it("the x-scale clamps to the window", () => {
    const x = stripScale(0, 1000, 100);
    expect(x(-5)).toBe(0);
    expect(x(2000)).toBe(100);
    expect(x(500)).toBe(50);
  });

  it("fits the copy budget (run names are the operator's) and uses no banned word", async () => {
    await mount(strip());
    expect(overBudget(document.body)).toEqual([]);
    expect(visibleText()).not.toMatch(BANNED);
  });
});

describe("THIS WEEK", () => {
  it("one line of totals, and its tooltip says builds and model hours", async () => {
    const v = V(view());
    await mount(<WeekBars week={v.week} tz="UTC" />);
    const line = $("[data-testid='cc-week-line']")!;
    expect(line.textContent).toBe("42 runs·38 passed·3 deploys·$12.40");
    expect(line.getAttribute("title")).toBe("9 builds (7 passed) · 6 PRs · 128 jobs · 2.1M tokens · 3.4 model hours");
    expect($$("[data-testid='cc-week-bar']")).toHaveLength(7);
    expect($$("[data-testid='cc-week-bar']")[2].getAttribute("title")).toBe("Wed 30 Sep · 7 finished · 1 failed");
    const more = $("[data-testid='cc-week-more']")!;
    expect([...more.querySelectorAll("[title]")].map((e) => e.getAttribute("title")))
      .toEqual(["Code tasks: an agent edits code and opens a PR", "Time models spent answering"]);
    expect(overBudget(document.body)).toEqual([]);
    expect(visibleText()).not.toMatch(BANNED);
  });

  it("an empty week keeps its baseline and says so", async () => {
    await mount(<WeekBars week={V(freshView()).week} tz="UTC" />);
    expect($("[data-testid='cc-week-line']")?.textContent).toBe("No runs this week");
    expect($(".cc-week-done")).toBeNull();
  });

  it("is never animated", async () => {
    await mount(<WeekBars week={V(view()).week} tz="UTC" />);
    expect($$("[data-new], [data-woke], [data-arrive]")).toHaveLength(0);
  });
});

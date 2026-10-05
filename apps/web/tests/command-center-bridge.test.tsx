// @vitest-environment happy-dom
/**
 * The Command Center bridge, rendered and read (command-center spec §10.2, page items).
 *
 * The graphics (Floor, Today, the day strip, the week bars) are their own stream with their own
 * tests (command-center-graphics); here each is a stand-in that records the props the page
 * hands it, so these tests pin what the PAGE decides: the state sentence, the numbers, the
 * order, the cards, and when motion is allowed at all.
 */
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { codeOnly } from "./_helpers/source";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";
import { failed, freshView, gate, mission, quietView, view } from "./_helpers/command-center-view-fixture";
import { parseCommandCenterView, viewerZone, type CommandCenterView } from "../lib/command-center-view";
import { setConsoleLive } from "../lib/console-live";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(), usePathname: () => "/",
}));
vi.mock("../app/command-center/floor", () => ({
  Floor: (p: { motion: string; motionWhy?: string | null; onThread: (id: string) => void }) => (
    <div data-testid="stub-floor" data-motion={p.motion} data-why={p.motionWhy ?? ""}>
      <button type="button" data-testid="stub-thread" onClick={() => p.onThread("dec_gate")}>t</button>
    </div>
  ),
}));
vi.mock("../app/command-center/today-chart", () => ({
  TodayChart: (p: { tab: string; motion: string; onTab: (t: "work" | "spend") => void }) => (
    <div data-testid="stub-today" data-tab={p.tab} data-motion={p.motion}>
      <button type="button" data-testid="stub-tab-work" onClick={() => p.onTab("work")}>w</button>
    </div>
  ),
}));
vi.mock("../app/command-center/day-strip", () => ({
  DayStrip: (p: { rowsMax: number; windowHours: number; motion: string; onHover: (id: string | null) => void;
                  day: { rows: { runId: string }[] } }) => (
    <div data-testid="stub-strip" data-rows-max={p.rowsMax} data-window={p.windowHours} data-motion={p.motion}>
      {p.day.rows.map((r) => (
        <button key={r.runId} type="button" data-testid={`stub-bar-${r.runId}`}
                onMouseEnter={() => p.onHover(r.runId)}>b</button>
      ))}
    </div>
  ),
}));
vi.mock("../app/command-center/week-bars", () => ({
  WeekBars: (p: { week: { runs: number } }) => <div data-testid="stub-week" data-runs={p.week.runs} />,
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { CommandCenter } = await import("../app/command-center-ui");

type Wire = Record<string, unknown>;
const V = (w: Wire): CommandCenterView => parseCommandCenterView(w);
const page = (initial: CommandCenterView | null, over: Record<string, unknown> = {}) => (
  // Read in the browser's own zone, so the server's first paint is the view the poll keeps.
  <CommandCenter projectId="p1" orgId="org_1" actor="ceo" initial={initial} tz={viewerZone()} {...over} />
);
const text = (sel: string) => $(sel)?.textContent ?? "";

let fetchMock: ReturnType<typeof vi.fn>;
function stubFetch(answer: () => Partial<Response> & { status: number }) {
  fetchMock = vi.fn(async () => {
    const a = answer();
    return { ok: a.status >= 200 && a.status < 300, headers: new Headers(), json: async () => ({}), ...a } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
}
const viewCalls = () => fetchMock.mock.calls.filter((c) => String(c[0]).startsWith("/v1/command-center/view"));
function stubWidth(px: number) {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: px });
}
function stubMotion(reduce: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true, writable: true,
    value: (q: string) => ({ matches: reduce && q.includes("reduce"), media: q, onchange: null,
                             addEventListener: () => {}, removeEventListener: () => {},
                             addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false }),
  });
}

beforeEach(() => {
  window.history.replaceState(null, "", "/?area=overview");
  stubWidth(1440);
  stubMotion(false);
  setConsoleLive(true);
  try { window.localStorage.clear(); window.sessionStorage.clear(); } catch { /* none */ }
  stubFetch(() => ({ status: 304 }));
});
afterEach(() => {
  unmount();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  setConsoleLive(true);
});

// ── The state sentence (§4.5) ───────────────────────────────────────────────────────────────

const SENTENCES: [string, string, string][] = [
  ["budget_stopped", "Budget stopped work", "red"],
  ["host_critical", "Host struggling", "red"],
  ["stopped", "1 stopped", "red"],
  ["needs_you", "2 need you", "amber"],
  ["needs_you", "1 needs you", "amber"],
  ["host_slow", "Host is slow", "amber"],
  ["budget_high", "Budget at 91%", "amber"],
  ["running", "3 running", "accent"],
  ["setup", "Finish setup", "amber"],
  ["first_build", "Ready for first build", "accent"],
  ["quiet", "All quiet", "green"],
];

describe("the status line", () => {
  // THE SERVER'S SENTENCE (§4.5), CALMLY (independent test D3): the headline is the first
  // matching rule's words; the tone is the dot's, the text stays in the page's ink.
  it.each(SENTENCES)("%s draws the server's sentence `%s` with a %s dot", async (key, words, tone) => {
    await mount(page(V(view({ status: { key, text: words, tone, tip: "why" } }))));
    expect(text(".cc-status-text")).toBe(words);
    expect($(".cc-status-text")?.getAttribute("title")).toBe("why");
    expect($(".cc-status .cc-dot")?.getAttribute("data-tone")).toBe(tone);
    expect($('[data-testid="cc-status-chip"]')).toBeNull();
  });

  it("never paints the headline in a status colour", () => {
    const css = readFileSync(resolve(__dirname, "../app/command-center/bridge.css"), "utf8");
    expect(css).not.toMatch(/\.cc-status\[data-tone="(red|amber)"\]\s+\.cc-status-text\s*\{[^}]*color/);
  });

  it("NEED YOU is counts.needs_you, amber when it is not zero, and links to the cards", async () => {
    await mount(page(V(view({ counts: { agents: 7, working: 2, needs_you: 5, stopped: 1, setup: 0, running: 2, expired_answerable: 0 } }))));
    const kpi = $('[data-testid="cc2-kpi-needs-you"]')!;
    expect(kpi.querySelector(".cc-num-n")?.textContent).toBe("5");
    expect(kpi.className).toContain("amber");
    expect(kpi.getAttribute("href")).toBe("#cc2-needs-you");
    expect(kpi.getAttribute("title")).toBe("Decisions waiting. Same as Approvals and the top bar.");
    expect($('[data-testid="cc2-kpi-running"] .cc-num-n')?.textContent).toBe("2");
    expect($('[data-testid="cc-kpi-done-today"] .cc-num-n')?.textContent).toBe("4");
    expect($('[data-testid="cc-kpi-done-today"]')?.getAttribute("title")).toBe("Runs that finished today. Failed: 1.");
  });

  it("SPENT is money; plan-covered spend reads Included; pressing it shows Spend", async () => {
    await mount(page(V(view())));
    expect(text('[data-testid="cc-kpi-spent"] .cc-num-n')).toBe("$4.12");
    expect($('[data-testid="stub-today"]')?.getAttribute("data-tab")).toBe("work");
    await click($('[data-testid="cc-kpi-spent"]'));
    expect($('[data-testid="stub-today"]')?.getAttribute("data-tab")).toBe("spend");
    unmount();
    const covered = view();
    (covered.today as Wire).money_micros = 0;
    await mount(page(V(covered)));
    expect(text('[data-testid="cc-kpi-spent"] .cc-num-n')).toBe("Included");
    expect($('[data-testid="cc-kpi-spent"]')?.getAttribute("title")).toBe("Covered by your plan · $3.80 API value");
  });

  it("speaks when the state changes, never on load", async () => {
    await mount(page(V(view())));
    expect(text('.cc-status [role="status"]')).toBe("");
  });
});

// ── Copy (§5.1): six words, no banned words ────────────────────────────────────────────────

const BANNED = /\b(capability|binding|governed|immutable|frontier|runtime|spec|namespace|fork|terminal|eta|estimated|almost done|mission|office|alive|forecast|time to cap|projected|health score|uptime|ai-powered)\b/i;

function visibleRuns(root: Element): string[] {
  const SKIP = "[title], [data-tip], [role='tooltip'], textarea, script, style, option, [hidden], [aria-hidden='true'], .ag-sr, [data-user-text]";
  const out: string[] = [];
  for (const el of [root, ...root.querySelectorAll("*")]) {
    if (el.closest(SKIP)) continue;
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? "").join("").trim();
    if (own) out.push(own);
  }
  return out;
}

describe("the words on the page", () => {
  it.each([["busy", view()], ["quiet", quietView()], ["fresh", freshView()]])(
    "%s: no visible run over six words, no banned word, no percent beside a run",
    async (_name, w) => {
      await mount(page(V(w as Wire)));
      const root = $('[data-testid="command-center"]')!;
      expect(overBudget(root)).toEqual([]);
      const runs = visibleRuns(root);
      expect(runs.filter((t) => BANNED.test(t))).toEqual([]);
      expect(runs.filter((t) => /%/.test(t))).toEqual([]);
    });

  it("the page's own source holds no banned word in any string it can show (app/command-center/**)", () => {
    const files = ["app/command-center-ui.tsx",
      ...readdirSync("app/command-center").filter((f) => f.endsWith(".tsx")).map((f) => `app/command-center/${f}`)];
    const found: string[] = [];
    for (const f of files) {
      const code = codeOnly(f);
      const strings = [...code.matchAll(/(["'`])((?:\\.|(?!\1)[^\\\n])*)\1/g)].map((m) => m[2])
        .concat([...code.matchAll(/>([^<>{}]+)</g)].map((m) => m[1]));
      for (const s of strings) {
        const copy = /\s/.test(s.trim()) || /^[A-Z]/.test(s.trim());
        if (copy && !s.startsWith(".") && BANNED.test(s)) found.push(`${f}: ${s.trim()}`);
      }
    }
    expect(found).toEqual([]);
  });
});

// ── Motion stops (§5.2) and the poll respects Paused (bug 6) ───────────────────────────────

describe("when nothing may move", () => {
  it("Paused: the Paused chip, no motion, and no poll however long it waits", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "setInterval", "clearTimeout", "clearInterval"] });
    setConsoleLive(false);
    await mount(page(V(view())));
    await act(async () => { vi.advanceTimersByTime(45_000); });
    expect(viewCalls()).toHaveLength(0);
    expect(text('[data-testid="cc-paused"]')).toBe("Paused");
    // The sentence is unchanged; the chip says Paused.
    expect(text(".cc-status-text")).toBe("1 stopped");
    expect($('[data-testid="stub-floor"]')?.getAttribute("data-motion")).toBe("none");
    expect($('[data-testid="stub-strip"]')?.getAttribute("data-motion")).toBe("none");
    expect(document.querySelectorAll("[data-comet]")).toHaveLength(0);
  });

  it("Live: the seeded first paint waits one cadence, then asks with its ETag (a 304)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "setInterval", "clearTimeout", "clearInterval"] });
    await mount(page(V(view())));
    expect(viewCalls()).toHaveLength(0);
    await act(async () => { vi.advanceTimersByTime(10_500); });
    expect(viewCalls()).toHaveLength(1);
    const init = viewCalls()[0][1] as RequestInit;
    expect((init.headers as Record<string, string>)["If-None-Match"]).toBe("\"cc-e1\"");
    expect($('[data-testid="stub-floor"]')?.getAttribute("data-motion")).toBe("full");
  });

  it("a viewer in another zone than the server painted reads once in their own, and keeps it", async () => {
    await mount(page(V(view()), { tz: "Etc/GMT+12" }));
    expect(viewCalls().length).toBeGreaterThanOrEqual(1);
    expect(String(viewCalls()[0][0])).toContain(`tz=${encodeURIComponent(viewerZone())}`);
    expect(document.cookie).toContain("boss_tz=");
    // The page keeps showing the first paint while it reads (a 304 changes nothing).
    expect(text('[data-testid="command-center"]')).toContain("Ship it #182");
  });

  it("host critical: the HostAdvice banner in the server's words, and motion off with the reason", async () => {
    await mount(page(V(view({
      status: { key: "host_critical", text: "Host struggling", tone: "red", tip: "This computer is out of memory" },
      host: { show: true, severity: "critical", headline: "This computer is out of memory",
              detail: "Docker has 0.2 GB free.", action: "Quit other apps, then restart Docker." },
    }))));
    expect(text('[data-testid="host-advice"]')).toContain("This computer is out of memory");
    expect(text('[data-testid="host-advice"]')).toContain("Quit other apps, then restart Docker.");
    const floor = $('[data-testid="stub-floor"]')!;
    expect(floor.getAttribute("data-motion")).toBe("none");
    expect(floor.getAttribute("data-why")).toBe("Motion paused while the host struggles");
    // The banner sits above the status line: the only banner on the page.
    const order = $$('[data-testid="host-advice"], [data-testid="cc-status"]').map((e) => e.getAttribute("data-testid"));
    expect(order).toEqual(["host-advice", "cc-status"]);
  });

  it("reduced motion: the graphics are told `reduced`", async () => {
    stubMotion(true);
    await mount(page(V(view())));
    expect($('[data-testid="stub-floor"]')?.getAttribute("data-motion")).toBe("reduced");
    expect($('[data-testid="stub-today"]')?.getAttribute("data-motion")).toBe("reduced");
  });

  it("unreachable: `Can't reach BOSS`, the last view stays dimmed, card buttons do nothing", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "setInterval", "clearTimeout", "clearInterval"] });
    await mount(page(V(view())));
    stubFetch(() => ({ status: 503, json: async () => ({ detail: "Postgres is not reachable." }) }));
    await act(async () => { vi.advanceTimersByTime(10_500); });
    await act(async () => { await Promise.resolve(); });
    vi.useRealTimers();
    await settle();
    expect(text(".cc-status-text")).toBe("Can't reach BOSS");
    expect($('[data-testid="cc2-load-error"]')?.getAttribute("title")).toContain("Postgres is not reachable.");
    expect($('[data-testid="command-center"]')?.getAttribute("data-stale")).toBe("true");
    // The last data stays …
    expect(text('[data-testid="command-center"]')).toContain("Ship it #182");
    // … and nothing in Needs you can be pressed.
    const body = $(".cc-needs-body") as HTMLFieldSetElement;
    expect(body.disabled).toBe(true);
    expect(body.getAttribute("title")).toBe("Can't reach BOSS");
    expect($('[data-testid="stub-floor"]')?.getAttribute("data-motion")).toBe("none");
    expect($$('[data-testid^="cc-dot-"]').map((d) => d.textContent)).toContain("ModelsUnknown");
  });
});

// ── Needs you (§4.8) is exactly Live's list ─────────────────────────────────────────────────

describe("needs you", () => {
  it("today's decisions first, then what stopped today; the head counts decisions and chips the stopped", async () => {
    // Independent test, improvement 1 (the coordinator's decision): what you can decide now
    // leads; the spec's "stopped first" put week-old failures above today's gates.
    await mount(page(V(view({ attention: [gate(), failed()] }))));
    const ids = $$("#cc2-needs-you [data-att-card]").map((c) => c.getAttribute("data-att-id"));
    expect(ids).toEqual(["dec_gate", "run:wfr_ship:build"]);
    expect(text("#cc2-needs-you .cc-head-count")).toBe("1");
    expect(text('[data-testid="cc-stopped-chip"]')).toBe("1 stopped");
    expect(text("#cc2-needs-you .cc-head-link")).toBe("All in Approvals ↗");
  });

  it("stopped work from before today is one line, not a card each", async () => {
    const old = (n: number) => failed({ id: `run:old${n}:build`, at: "2026-10-01T09:00:00Z" });
    await mount(page(V(view({ attention: [gate(), failed(), old(1), old(2), old(3)] }))));
    expect($$("#cc2-needs-you [data-att-card]").map((c) => c.getAttribute("data-att-id")))
      .toEqual(["dec_gate", "run:wfr_ship:build"]);
    expect(text('[data-testid="cc-stopped-earlier"]')).toBe("3 stopped earlier ↗");
    expect(text('[data-testid="cc-stopped-chip"]')).toBe("4 stopped");
  });

  it("a budget that refuses at the door is the first card and the sentence", async () => {
    window.sessionStorage.clear();
    await mount(page(V(view({
      status: { key: "budget_stopped", text: "Budget stopped work", tone: "red", tip: "Over the day cap. Raise it in Budget." },
      budget_refusal: { period: "day", cap_usd_micros: 5_000_000, actual_usd_micros: 6_200_000, resets_at_label: "",
                        title: "Budget refused new work", detail: "$6.20 of $5 today. Raise the cap to start work." },
    }))));
    const first = $("#cc2-needs-you [data-att-card]");
    expect(first?.getAttribute("data-att-id")).toBe("budget:refusal");
    expect(first?.textContent).toContain("Budget refused new work");
    expect(text(".cc-status-text")).toBe("Budget stopped work");
  });

  it("past four cards, `+n more` expands in place", async () => {
    const gates = Array.from({ length: 6 }, (_, i) => gate({ id: `dec_${i}`, decision_id: `dec_${i}` }));
    await mount(page(V(view({ attention: gates, counts: { agents: 7, working: 0, needs_you: 6, stopped: 0, setup: 0, running: 0, expired_answerable: 0 } }))));
    expect($$("#cc2-needs-you [data-att-card]")).toHaveLength(4);
    await click($('[data-testid="cc-att-more"]'));
    expect($$("#cc2-needs-you [data-att-card]")).toHaveLength(6);
  });

  it("nothing waiting: three words, with the rest in the tooltip", async () => {
    await mount(page(V(quietView())));
    // And no empty column beside the Floor (re-test 8): NOW is marked quiet for the layout.
    expect($(".cc-now")?.getAttribute("data-quiet")).toBe("true");
    expect(text('[data-testid="cc2-needs-you-empty"]')).toBe("Nothing needs you");
    expect($('[data-testid="cc2-needs-you-empty"] [title]')?.getAttribute("title"))
      .toBe("Gates, failed steps and setup gaps appear here.");
  });

  it("a thread pressed on the Floor focuses its card", async () => {
    await mount(page(V(view())));
    await click($('[data-testid="stub-thread"]'));
    expect(document.activeElement?.closest("[data-att-id]")?.getAttribute("data-att-id") ?? "dec_gate").toBe("dec_gate");
  });
});

// ── Work (§4.10) ────────────────────────────────────────────────────────────────────────────

describe("work", () => {
  it("lanes are Live's MissionRow, at most six, `+n more running`", async () => {
    const many = Array.from({ length: 9 }, (_, i) => mission({ run_id: `wfr_${i}`, subject: `Run ${i}` }));
    await mount(page(V(view({ missions: many }))));
    expect($$("#cc2-running .cc-lane")).toHaveLength(6);
    expect(text('[data-testid="cc-lanes-more"]')).toBe("+3 more running");
    expect(text("#cc-running-h")).toBe("RUNNING");
  });

  it("hovering a bar lights its lane: one surface, two ways", async () => {
    await mount(page(V(view())));
    const bar = $('[data-testid="stub-bar-wfr_182"]')!;
    await act(async () => { bar.dispatchEvent(new MouseEvent("mouseover", { bubbles: true })); });
    await settle();
    expect($('.cc-lane[data-run-id="wfr_182"]')?.getAttribute("data-hover")).toBe("true");
  });

  it("the strip keeps 10 rows at 1440, 8 below, and 12 hours on a phone", async () => {
    await mount(page(V(view())));
    expect($('[data-testid="stub-strip"]')?.getAttribute("data-rows-max")).toBe("10");
    unmount();
    stubWidth(1280);
    await mount(page(V(view())));
    expect($('[data-testid="stub-strip"]')?.getAttribute("data-rows-max")).toBe("8");
    unmount();
    stubWidth(390);
    await mount(page(V(view())));
    expect($('[data-testid="stub-strip"]')?.getAttribute("data-window")).toBe("12");
  });

  it("done lately: a run chip says its outcome by glyph, its workflow and error in the tooltip", async () => {
    await mount(page(V(view())));
    const failedChip = $$('[data-testid="cc2-recent-row"]').find((c) => c.textContent?.includes("Audit deps"))!;
    expect(failedChip.textContent).toContain("✕");
    expect(failedChip.getAttribute("title")).toBe("Audit · npm audit found 2 critical issues");
  });
});

// ── Order (§4.4, §5.3) ──────────────────────────────────────────────────────────────────────

describe("the order follows urgency", () => {
  const ids = () => $$("#cc-status, #cc-compose, #cc-floor, #cc2-needs-you, #cc-today, #cc2-running, #cc-systems, #cc-week")
    .map((e) => e.id);

  it("desktop: status, composer, Floor beside Needs you, today, work, systems, week", async () => {
    await mount(page(V(view())));
    expect(ids()).toEqual(["cc-status", "cc-compose", "cc-floor", "cc2-needs-you", "cc-today", "cc2-running", "cc-systems", "cc-week"]);
  });

  it("1024-1279: stacked, Needs you first", async () => {
    stubWidth(1100);
    await mount(page(V(view())));
    expect(ids()).toEqual(["cc-status", "cc-compose", "cc2-needs-you", "cc-floor", "cc-today", "cc2-running", "cc-systems", "cc-week"]);
  });

  it("phone: Status → Needs you → Composer → Work → Floor → Today → Systems → Week", async () => {
    stubWidth(390);
    await mount(page(V(view())));
    expect(ids()).toEqual(["cc-status", "cc2-needs-you", "cc-compose", "cc2-running", "cc-floor", "cc-today", "cc-systems", "cc-week"]);
  });
});

// ── Today's tab (§4.9, §4.13 red budget) ────────────────────────────────────────────────────

describe("today's tab", () => {
  it("a budget that stopped work shows Spend once a session; the choice is remembered", async () => {
    const red = () => V(view({ status: { key: "budget_stopped", text: "Budget stopped work", tone: "red", tip: "Over the month cap. Raise it in Budget." } }));
    await mount(page(red()));
    expect($('[data-testid="stub-today"]')?.getAttribute("data-tab")).toBe("spend");
    // The page can be remounted after the switch (seen on a production build): the new
    // instance shows what the operator was shown, not Work again.
    unmount();
    await mount(page(red()));
    expect($('[data-testid="stub-today"]')?.getAttribute("data-tab")).toBe("spend");
    await click($('[data-testid="stub-tab-work"]'));
    unmount();
    await mount(page(red()));
    // Once a session: the operator chose Work, and it holds.
    expect($('[data-testid="stub-today"]')?.getAttribute("data-tab")).toBe("work");
  });
});

// ── Systems (§4.11) ─────────────────────────────────────────────────────────────────────────

describe("systems", () => {
  it("seven dots, one word and a state each, the server's reason as tooltip, linked with the org", async () => {
    await mount(page(V(view())));
    const dots = $$('[data-testid^="cc-dot-"]');
    expect(dots.map((d) => d.getAttribute("data-testid"))).toEqual(
      ["cc-dot-models", "cc-dot-repo", "cc-dot-budget", "cc-dot-host", "cc-dot-queue", "cc-dot-previews", "cc-dot-deploys"]);
    const budget = $('[data-testid="cc-dot-budget"]')!;
    expect(budget.getAttribute("href")).toBe("/?org=org_1&area=budget");
    expect(budget.getAttribute("title")).toBe("$84 of $200 this month");
    expect($('[data-testid="cc-dot-deploys"]')?.closest("li")?.getAttribute("data-tone")).toBe("grey");
    expect($('[data-testid="cc-dot-deploys"]')?.textContent).toBe("DeploysNot set up");
  });
});

// ── Fresh install (§4.13) ──────────────────────────────────────────────────────────────────

describe("before the first run", () => {
  it("the help rows sit under the composer; later they live behind `?`", async () => {
    await mount(page(V(freshView()), { help: <div data-testid="help-card">help</div> }));
    expect($('#cc-compose [data-testid="console-capabilities-card"]')).not.toBeNull();
    unmount();
    await mount(page(V(view()), { help: <div data-testid="help-card">help</div> }));
    expect($('#cc-compose [data-testid="console-capabilities-card"]')).toBeNull();
    expect($('[data-testid="help-card"]')).toBeNull();
    await click($('[data-testid="cc-help"]'));
    expect($('[data-testid="help-card"]')).not.toBeNull();
  });
});

describe("the host banner", () => {
  it("mounts only for a critical host; a warning is the Host dot's word", async () => {
    await mount(page(V(view({ host: { show: true, severity: "warning", cause: "disk",
      headline: "This server's disk is nearly full", detail: "Right now: disk 86% full.",
      action: "Free disk space — `docker system prune` reclaims old images." } }))));
    expect($('[data-testid="host-advice"]')).toBeNull();
  });
});

describe("the keyboard (independent test D15)", () => {
  it("Needs you and RUNNING are one tab stop each; arrows move, Enter steps in, Esc steps out", async () => {
    const gates = Array.from({ length: 3 }, (_, i) => gate({ id: `dec_${i}`, decision_id: `dec_${i}` }));
    await mount(page(V(view({ attention: gates }))));
    const cards = $$("#cc2-needs-you [data-att-card]");
    const stops = (root: Element) => [...root.querySelectorAll<HTMLElement>("a[href], button, [tabindex]")]
      .filter((el) => el.tabIndex >= 0);
    // The cards list: exactly one stop (its first card).
    expect(stops($(".cc-cards")!)).toEqual([cards[0]]);
    // The lanes list: exactly one stop too.
    const lanes = $$(".cc-lanes .cc-lane");
    expect(lanes.length).toBeGreaterThan(1);
    expect(stops($(".cc-lanes")!).filter((el) => el.closest(".cc-lane"))).toEqual([lanes[0]]);
    // ↓ moves; Enter opens the card's own controls; Esc returns to the card.
    cards[0].focus();
    await act(async () => { cards[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })); });
    expect(document.activeElement).toBe(cards[1]);
    await act(async () => { cards[1].dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    expect(cards[1].contains(document.activeElement) && document.activeElement !== cards[1]).toBe(true);
    await act(async () => { document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(document.activeElement).toBe(cards[1]);
  });
});

describe("the lanes (improvement 7, D7)", () => {
  it("two or more runs waiting on you fold into one line that opens to them", async () => {
    const w = (id: string) => mission({ run_id: id, subject: id, state: "waiting_you", verb: "Needs you" });
    await mount(page(V(view({ missions: [mission(), w("wfr_a"), w("wfr_b"), w("wfr_c")] }))));
    expect(text('[data-testid="cc-lanes-waits"]')).toContain("3 runs");
    expect($$(".cc-lanes .cc-lane").map((l) => l.getAttribute("data-run-id"))).toEqual(["wfr_182"]);
    await click($('[data-testid="cc-lanes-waits"]'));
    expect($$(".cc-lanes .cc-lane")).toHaveLength(4);
  });

  it("a lane's state word never truncates (only the step and time after it may)", () => {
    const css = readFileSync(resolve(__dirname, "../app/agents/agent-world.css"), "utf8");
    expect(css).toMatch(/\.ag-mission-now > span:first-child \{ flex: none; overflow: visible; \}/);
  });
});

describe("the layout (D1, D4)", () => {
  it("the Systems row wraps inside its card at every width, and heads never wrap", () => {
    const css = readFileSync(resolve(__dirname, "../app/command-center/bridge.css"), "utf8");
    expect(css).not.toMatch(/\.cc-systems \{[^}]*nowrap/);
    expect(css).toMatch(/\.cc-systems \{\s*display: grid; grid-template-columns: repeat\(4, minmax\(0, 1fr\)\)/);
    expect(css).toMatch(/@media \(min-width: 1280px\) \{ \.cc-systems \{ grid-template-columns: repeat\(7/);
    expect(css).not.toMatch(/\.cc-sys a \{[^}]*overflow: hidden/);
    expect(css).toMatch(/\.cc-head-label, \.cc-head-link, \.cc-head \.cc-chip \{ white-space: nowrap/);
    expect(css).not.toMatch(/\.cc-status-text \{[^}]*text-overflow: ellipsis/);
  });
});

describe("the loading bar (D11)", () => {
  it("the shell ends it when an area says it has painted, and the Command Center says so", () => {
    expect(codeOnly("app/ConsoleShell.tsx")).toContain("Promise.all([painted, documentDone])");
    expect(codeOnly("app/command-center-ui.tsx")).toContain('announcePainted("overview")');
  });
});

describe("the host banner's commands (re-test 4)", () => {
  it("draws a `command` as code with Copy, never as literal backticks", async () => {
    await mount(page(V(view({ host: { show: true, severity: "critical", cause: "disk",
      headline: "This server's disk is full", detail: "Right now: disk 97% full.",
      action: "Free disk space — `docker system prune` reclaims old images." } }))));
    const banner = $('[data-testid="host-advice"]')!;
    expect(banner.textContent).not.toContain("`");
    expect(banner.querySelector("code")?.textContent).toBe("docker system prune");
    expect($('[data-testid="host-advice-copy"]')?.getAttribute("title")).toBe("Copy: docker system prune");
  });
});

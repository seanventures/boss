// @vitest-environment happy-dom
/**
 * PARITY (cc v2): every fact and action the Command Center at main 604ee67d offered maps to a
 * place on the redesigned page — kept, moved (named), changed (with its reason), or removed at
 * the operator's request with its content moved — and none is dropped. A row passes only by a
 * check on the rendered page (or its source), or by naming the test that holds it (which must
 * exist). A row with neither fails. This is the table in the PR notes.
 */
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { $, $$, click, mount, unmount } from "./_helpers/live-render";
import { apps, failed, freshView, gate, mission, view } from "./_helpers/command-center-view-fixture";
import { parseCommandCenterView, viewerZone } from "../lib/command-center-view";
import { codeOnly } from "./_helpers/source";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(), usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as { React?: typeof React }).React = React;

const { CommandCenter } = await import("../app/command-center-ui");
const { AskBossHero } = await import("../app/ask-boss-hero");
const { OnboardingHelpCard } = await import("../app/onboarding-help-card");

type Wire = Record<string, unknown>;
const REPO = resolve(__dirname, "../../..");
const setupGap = {
  id: "ready:github", kind: "readiness", title: "Connect a repository", detail: "",
  fix: { area: "setup", label: "Fix in Repo and models", focus: "repository" }, actions: [], answers: [],
  decide_inline: false, at: null,
};
const busy = () => view({ attention: [failed(), gate(), setupGap], missions: [mission()] });

async function show(w: Wire = busy(), firstRun = false) {
  await mount(
    <CommandCenter projectId="p1" orgId="org_1" actor="ceo" tz={viewerZone()}
      initial={parseCommandCenterView(w)}
      composer={<AskBossHero projectId="p1" actor="ceo" orgId="org_1" deploymentAvailable={false}
                             layout="hero" ideasInline={firstRun} />}
      help={<OnboardingHelpCard />} />,
  );
}
const tid = (id: string) => $(`[data-testid="${id}"]`);
const src = (p: string) => codeOnly(resolve(__dirname, "..", p));

beforeEach(() => {
  window.history.replaceState(null, "", "/?area=overview");
  vi.stubGlobal("fetch", vi.fn(async (url: string) => (String(url).startsWith("/v1/factory/apps")
    ? { ok: true, status: 200, headers: new Headers(), json: async () => apps() }
    : { ok: false, status: 304, headers: new Headers(), json: async () => ({}) }) as unknown as Response));
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

type Row = {
  row: number; before: string; now: string;
  status: "Kept" | "Moved" | "Changed" | "Removed as asked, content moved";
  dom?: () => Promise<void> | void;
  covered?: string;
};

const ROWS: Row[] = [
  { row: 1, before: "Critical host banner above everything", now: "Same banner, above the hero", status: "Kept",
    dom: async () => { await show(view({ host: { show: true, severity: "critical", headline: "Memory is almost full" } })); expect($(".cc > :first-child")?.textContent).toContain("Memory is almost full"); } },
  { row: 2, before: "Status line: the server's sentence, its dot and tooltip", now: "Hero eyebrow: systems word · the server's sentence · last work; tooltip kept", status: "Moved",
    dom: async () => { await show(); expect(tid("cc-status-text")?.textContent).toBe("1 stopped"); expect(tid("cc-status")?.getAttribute("title")).toBeTruthy(); } },
  { row: 3, before: "Paused chip", now: "Eyebrow chip", status: "Kept", covered: "apps/web/tests/command-center-v2.test.tsx" },
  { row: 4, before: "State change announced (live region), never on load", now: "Same live region in the eyebrow", status: "Kept",
    dom: async () => { await show(); expect($('.cc-area-name [role="status"][aria-live="polite"]')?.textContent).toBe(""); } },
  { row: 5, before: "`?` help popover (What you can do here)", now: "`?` at the end of the eyebrow", status: "Kept",
    dom: async () => { await show(); await click(tid("cc-help")); expect($(".cc-help-pop")).not.toBeNull(); } },
  { row: 6, before: "KPI RUNNING → #cc2-running", now: "In flight badge (counts.running); id cc2-running kept", status: "Moved",
    dom: async () => { await show(); expect(tid("cc2-kpi-running")?.textContent).toBe("2"); expect($("#cc2-running")).not.toBeNull(); } },
  { row: 7, before: "KPI NEED YOU (counts.needs_you) → #cc2-needs-you", now: "Needs you badge; id cc2-needs-you kept", status: "Moved",
    dom: async () => { await show(); expect(tid("cc2-kpi-needs-you")?.textContent).toBe("1"); expect($("#cc2-needs-you")).not.toBeNull(); } },
  { row: 8, before: "KPI DONE TODAY (failed in tooltip)", now: "`n done today` heads In flight's finished chips", status: "Moved",
    dom: async () => { await show(); expect(tid("cc-kpi-done-today")?.textContent).toBe("4 done today · 1 failed"); } },
  { row: 9, before: "KPI SPENT today (Included when plan-covered)", now: "Health card: the ring is the budget's own period (day or month); today's money and its included API value on a line under it", status: "Moved",
    dom: async () => { await show(); expect(tid("cc-today-spend")?.textContent).toContain("Today: $4.12"); } },
  { row: 10, before: "Ask BOSS composer: Build, Talk to BOSS, Research, ⌘/Ctrl+Enter, refusal checklist + Build anyway, turn/answer view, proposed actions",
    now: "The same AskBossHero, laid out as `hero`: only the look changed", status: "Kept",
    dom: async () => {
      await show();
      expect(tid("split-build")).not.toBeNull(); expect(tid("askboss-talk")).not.toBeNull();
      const hero = src("app/ask-boss-hero.tsx");
      expect(hero).toContain('e.key === "Enter" && (e.metaKey || e.ctrlKey)');
      expect(hero).toContain("<StartRefusal");
      expect(hero).toContain('id: "research"');
    },
    covered: "apps/web/tests/the-button-runs-what-it-says.test.ts" },
  { row: 11, before: "Workflow picker and agent picker", now: "Chips on the composer row", status: "Kept",
    dom: async () => { await show(); expect(tid("askboss-agents-toggle")).not.toBeNull(); },
    covered: "apps/web/tests/command-center-picks-the-team.test.ts" },
  { row: 12, before: "Ideas (examples, research seeds, the 16 templates, See all grid)", now: "Ideas chip kept; the templates are a shelf; All 16 ↗ opens them on their own page", status: "Kept",
    dom: async () => { await show(); await click(tid("askboss-examples")); expect(tid("askboss-ideas")).not.toBeNull(); await click(tid("cc-templates-all")); expect(tid("cc-gallery")).not.toBeNull(); } },
  { row: 13, before: "Template page at ?template=<slug> (deep link; Back closes it)", now: "The same page; a template card opens it", status: "Kept",
    dom: async () => { await show(); await click($$('[data-testid="cc-tpl"]')[2]); expect(window.location.search).toContain("template="); expect(tid("askboss-sheet")).not.toBeNull(); } },
  { row: 14, before: "First run: help rows under the composer, Ideas inline", now: "Same, under the hero composer", status: "Kept",
    dom: async () => { await show(freshView(), true); expect($(".cc-help-rows")).not.toBeNull(); } },
  { row: 15, before: "The Floor: every agent in its team, with its state; You at the centre", now: "Your team: nine tiles of real faces; working glows, waiting on you wears amber", status: "Removed as asked, content moved",
    dom: async () => { await show(); expect($$('[data-testid^="cc-tile-"]').length).toBeGreaterThan(0); expect($('.cc-face-in[data-waiting="true"]')).not.toBeNull(); } },
  { row: 16, before: "Floor › Open map ↗", now: "Map ↗ in the team's head", status: "Moved",
    dom: async () => { await show(); expect(tid("cc-open-map")?.getAttribute("href")).toContain("view=organization"); } },
  { row: 17, before: "Floor agent card: its live line, Passport ↗ and Talk", now: "A face opens the same: its live line, Talk, Open ↗", status: "Moved",
    dom: async () => { await show(); await click(tid("cc-face")); expect(tid("cc-face-talk")).not.toBeNull(); expect(tid("cc-face-open")?.getAttribute("href")).toContain("role="); } },
  { row: 18, before: "Floor footer counts", now: "Team footer: working · waiting on you · ready", status: "Kept",
    dom: async () => { await show(); expect(tid("cc-team-foot")?.textContent).toContain("waiting on you"); } },
  { row: 19, before: "Floor hand-offs of the last hour (edges → their runs)", now: "Agent Studio › Organization's Flow via Map ↗; runs from In flight", status: "Removed as asked, content moved",
    covered: "apps/web/tests/org-board-polls-politely.test.tsx" },
  { row: 20, before: "Floor list toggle", now: "The tiles are the list; Agent Studio ↗ for the directory", status: "Removed as asked, content moved",
    dom: async () => { await show(); expect(tid("cc-agent-studio")?.getAttribute("href")).toContain("view=directory"); } },
  { row: 21, before: "Needs you: an AttentionCard per item (server buttons; approve/reject inline when allowed; Retry/Skip; Waive; Review ↗; doctor's options)",
    now: "A sentence per item with its face; Review opens that same AttentionCard in place", status: "Changed",
    dom: async () => { await show(); await click($$('[data-testid="cc-need-review"]')[0]); expect($("[data-att-card]")).not.toBeNull(); expect(tid("ag-att-approve")).not.toBeNull(); } },
  { row: 22, before: "+n more cards", now: "+n more past three rows", status: "Kept", covered: "apps/web/tests/command-center-v2.test.tsx" },
  { row: 44, before: "Stopped count chip beside Needs you", now: "`n stopped` chip in the Needs you head", status: "Kept",
    dom: async () => { await show(); expect(tid("cc-stopped-chip")?.textContent).toBe("1 stopped"); } },
  { row: 23, before: "`n stopped earlier ↗`", now: "Same link, in the card's foot", status: "Kept", covered: "apps/web/tests/command-center-v2.test.tsx" },
  { row: 24, before: "Setup: <gap> ↗ to its Repo and models row", now: "A setup row with Fix, never folded", status: "Kept",
    dom: async () => { await show(); expect(tid("cc-need-fix")?.getAttribute("href")).toContain("focus=repository"); } },
  { row: 25, before: "`n expired · still answerable ↗`", now: "Same link, in the card's foot", status: "Kept",
    dom: async () => { await show(); expect(tid("cc2-expired-answerable")?.textContent).toBe("3 expired · still answerable ↗"); } },
  { row: 26, before: "All in Approvals ↗", now: "Approvals ↗", status: "Kept",
    dom: async () => { await show(); expect(tid("cc-approvals-link")?.getAttribute("href")).toContain("decision_filter=needs_action"); } },
  { row: 27, before: "A budget refusing at the door is the first card", now: "The first row", status: "Kept", covered: "apps/web/tests/command-center-v2.test.tsx" },
  { row: 28, before: "Can't reach BOSS: last view dimmed, nothing pressable", now: "Same (fieldset disabled, data-stale)", status: "Kept", covered: "apps/web/tests/command-center-v2.test.tsx" },
  { row: 29, before: "Today chart (work/spend by hour) and its Budget ↗", now: "Money: the Health ring → Budget; work: the 14-week pulse", status: "Removed as asked, content moved",
    dom: async () => { await show(); expect(tid("cc-budget-link")?.getAttribute("href")).toContain("area=budget"); expect(tid("cc-heat")).not.toBeNull(); } },
  { row: 30, before: "LAST 24H strip: run bars → their runs; gates", now: "In flight rows → their runs; gates are Needs you rows", status: "Removed as asked, content moved",
    dom: async () => { await show(); expect(tid("cc-flight-row")?.getAttribute("href")).toContain("wfr=wfr_182"); } },
  { row: 31, before: "Start work ↗ when nothing runs", now: "`build above` focuses the composer, by the radar", status: "Moved",
    dom: async () => { await show(freshView()); expect(tid("cc-start-work")).not.toBeNull(); } },
  { row: 32, before: "RUNNING lanes (MissionRow), +n more running, waits folded", now: "In flight rows: face, verb, step k/n, time; +n more running; stalled folded", status: "Changed",
    covered: "apps/web/tests/command-center-v2.test.tsx" },
  { row: 33, before: "Workflow ↗", now: "Workflows ↗ (In flight); Resume or close (stalled)", status: "Kept",
    dom: async () => { await show(); expect(tid("cc-workflows-link")?.getAttribute("href")).toContain("area=delivery_workflows"); } },
  { row: 34, before: "DONE LATELY: finished-run chips (outcome glyph, error tooltip)", now: "The same chips under In flight", status: "Moved",
    dom: async () => { await show(); expect($$('[data-testid="cc2-recent-row"]').length).toBe(2); } },
  { row: 35, before: "DONE LATELY: outcome chips (PR opened, report, minutes)", now: "PRs on the app shelf; every move in Latest from your team; Activity log ↗", status: "Moved",
    dom: async () => { await show(); expect(tid("cc-activity-link")?.getAttribute("href")).toContain("view=activity"); expect(tid("cc-feed")).not.toBeNull(); } },
  { row: 36, before: "SYSTEMS: seven dots, server tooltips, links with the org", now: "Health card: the same seven dots", status: "Moved",
    dom: async () => { await show(); expect($$('[data-testid^="cc-dot-"]').length).toBe(7); } },
  { row: 37, before: "THIS WEEK: bars + delivery line (runs, builds, PRs, jobs, deploys, tokens, model hours) + Activity ↗",
    now: "The pulse (14 weeks by day) + a `This week` line (finished · failed) that opens to the whole delivery line + Activity log ↗", status: "Removed as asked, content moved",
    dom: async () => { await show(); const t = tid("cc-week-line")?.getAttribute("aria-label") ?? ""; expect(t).toContain("42 runs (38 passed)"); expect(t).toContain("3.4 model hours"); } },
  { row: 38, before: "Motion only when Live, visible, reachable, host fine; reduced honoured", now: "Same rule (motionOf); stills otherwise", status: "Kept", covered: "apps/web/tests/command-center-v2.test.tsx" },
  { row: 39, before: "The loading bar ends when the page is on screen", now: "Same (announcePainted)", status: "Kept",
    dom: () => { expect(src("app/command-center-ui.tsx")).toContain('announcePainted("overview")'); } },
  { row: 40, before: "Viewer's zone cookie; reads in their zone", now: "Same", status: "Kept",
    dom: () => { expect(src("app/command-center-ui.tsx")).toContain("TZ_COOKIE"); } },
  { row: 41, before: "One read, seeded, polled with its ETag", now: "Same, plus /v1/factory/apps through the same poll", status: "Kept", covered: "apps/web/tests/command-center-v2.test.tsx" },
  { row: 42, before: "+ New build (top bar)", now: "Unchanged in the shell's top bar", status: "Kept",
    dom: () => { expect(src("app/ConsoleShell.tsx")).toContain("New build"); } },
  { row: 43, before: "Deep links: ?area=overview, org=, #cc2-needs-you, #cc2-running, ?template=", now: "All land: ids kept, the org on every link", status: "Kept",
    dom: async () => { await show(); expect($("#cc2-needs-you")).not.toBeNull(); expect($("#cc2-running")).not.toBeNull(); expect($$('.cc a[href^="/?"]').every((a) => (a.getAttribute("href") ?? "").includes("org=org_1"))).toBe(true); } },
];

describe("the parity table holds (cc v2)", () => {
  it("every row is held by a check here or names the test that holds it, and that test exists", () => {
    for (const r of ROWS) {
      expect(!!r.dom || !!r.covered, `row ${r.row} has no check`).toBe(true);
      if (r.covered) expect(existsSync(resolve(REPO, r.covered)), `row ${r.row}: ${r.covered} is missing`).toBe(true);
    }
  });

  it.each(ROWS.filter((r) => r.dom).map((r) => [r.row, r.before, r] as const))("row %i — %s", async (_n, _b, r) => {
    await r.dom!();
  });
});

it("the look is cc.css, imported by the layout", () => {
  expect(readFileSync(resolve(__dirname, "../app/layout.tsx"), "utf8")).toContain('import "./command-center/cc.css";');
});

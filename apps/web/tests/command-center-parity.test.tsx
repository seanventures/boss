// @vitest-environment happy-dom
/**
 * PARITY (command-center spec §9): everything the old Command Center showed or let the operator
 * do maps to the bridge — kept, moved (named), changed (a truth bug, with its reason) — and
 * nothing operator-facing is dropped. This is the enforcing test: all 55 rows are here, and a
 * row passes only by a check on the rendered page or by naming the test that holds it (that
 * file must exist). A row with neither fails.
 */
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import React from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { $, $$, click, mount, unmount } from "./_helpers/live-render";
import { failed, freshView, gate, view } from "./_helpers/command-center-view-fixture";
import { parseCommandCenterView, viewerZone } from "../lib/command-center-view";
import { decisionsHref, runHref } from "../lib/command-center";
import { codeOnly } from "./_helpers/source";
import { overBudget } from "./_helpers/copy-budget";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(), usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
// The composer's pickers are written for the automatic JSX runtime; this suite renders classic.
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

async function show(w: Wire = view({ attention: [failed(), gate(), setupGap] }), firstRun = false) {
  await mount(
    <CommandCenter projectId="p1" orgId="org_1" actor="ceo" tz={viewerZone()}
      initial={parseCommandCenterView(w)}
      composer={<AskBossHero projectId="p1" actor="ceo" orgId="org_1" deploymentAvailable={false}
                             ideasInline={firstRun} />}
      help={<OnboardingHelpCard />} />,
  );
}
async function showDown() {
  await mount(<CommandCenter projectId="p1" orgId="org_1" actor="ceo" tz={viewerZone()} initial={null}
                             initialError="Postgres is not reachable." />);
}
const tid = (id: string) => $(`[data-testid="${id}"]`);
const txt = (sel: string) => $(sel)?.textContent ?? "";
const src = (p: string) => codeOnly(resolve(__dirname, "..", p));

type Row = {
  row: number; today: string; now: string;
  status: "Kept" | "Moved" | "Changed" | "Dropped" | "Kept + Changed" | "Kept + richer" | "Restored on real data";
  /** A check on the rendered page (or its source, where the page is the source). */
  dom?: () => Promise<void> | void;
  /** Where it is held instead: a test file (repo-relative) that must exist. */
  covered?: string;
};

const ROWS: Row[] = [
  { row: 1, today: "Rail entry sub 'What changed · top risks · next actions'", now: "Sub `What needs you · what's moving`", status: "Changed",
    dom: () => {
      const shell = src("app/ConsoleShell.tsx");
      expect(shell).toContain('label: "Command Center", sub: "What needs you · what\'s moving"');
      expect(shell).not.toContain("top risks · next actions");
    } },
  { row: 2, today: "Landing default overview; Setup redirect when not ready", now: "Unchanged", status: "Kept",
    dom: () => { expect(src("app/page.tsx")).toContain(': "overview";'); }, covered: "apps/web/tests/setup-landing-and-badge.test.ts" },
  { row: 3, today: "Ask BOSS aura idle/thinking/slow/answered", now: "✦ aura in the composer, same states", status: "Kept",
    dom: async () => {
      await show();
      expect($("#cc-compose .askboss")?.getAttribute("data-state")).toBe("idle");
      expect($("#cc-compose .askboss-glyph")).not.toBeNull();
    } },
  { row: 4, today: "Eyebrow `Ask BOSS`, title `What do you want to do?`", now: "Title → textarea aria-label; eyebrow → section aria-label", status: "Moved",
    dom: async () => {
      await show();
      expect(tid("askboss-input")?.getAttribute("aria-label")).toBe("What do you want to do?");
      expect($("#cc-compose .askboss")?.getAttribute("aria-label")).toBe("Ask BOSS");
      expect(txt("#cc-compose")).not.toContain("What do you want to do?");
    } },
  { row: 5, today: "HelpTip 'Ask about current work or describe something to build…'", now: "HelpTip on ✦", status: "Kept",
    dom: async () => { await show(); expect($("#cc-compose .askboss-glyph button")).not.toBeNull(); } },
  { row: 6, today: "Sub 'Gated actions require your approval.'", now: "✦ tooltip", status: "Moved",
    dom: async () => {
      await show();
      expect($("#cc-compose .askboss-glyph")?.getAttribute("title")).toBe("Gated actions require your approval.");
    } },
  { row: 7, today: "`Working` / `Still working · Ns` in the header", now: "Composer status, unchanged", status: "Kept",
    dom: () => { expect(src("app/ask-boss-hero.tsx")).toContain("`Still working · ${elapsed}s`"); },
    covered: "apps/web/tests/a-stalled-turn-is-not-the-previous-answer.test.ts" },
  { row: 8, today: "Textarea, auto-grow to 320px, ⌘/Ctrl+Enter builds, Enter = newline", now: "Unchanged", status: "Kept",
    dom: async () => { await show(); expect(tid("askboss-input")?.getAttribute("rows")).toBe("1"); },
    covered: "apps/web/tests/composer-fits-its-controls.test.ts" },
  { row: 9, today: "Placeholder (21 words)", now: "`Describe what to build…` + the sentence as tooltip", status: "Changed",
    dom: async () => {
      await show();
      expect(tid("askboss-input")?.getAttribute("placeholder")).toBe("Describe what to build…");
      expect(tid("askboss-input")?.getAttribute("title")).toBe("⌘/Ctrl+Enter builds it. Questions: Talk to BOSS.");
    } },
  { row: 10, today: "DefaultWorkflowPicker", now: "Composer bar; ▾ under 1024px", status: "Kept",
    dom: async () => { await show(); expect($("#cc-compose .askboss-bar [data-testid=\"askboss-default-workflow\"]")).not.toBeNull(); } },
  { row: 11, today: "DefaultAgentPicker", now: "Composer bar as Team ▾; ▾ under 1024px", status: "Kept",
    dom: async () => { await show(); expect($("#cc-compose .askboss-bar [data-testid=\"askboss-default-agents\"]")).not.toBeNull(); } },
  { row: 12, today: "`Build →` primary", now: "Unchanged", status: "Kept",
    dom: async () => { await show(); expect(txt('[data-testid="split-build"]')).toContain("Build →"); } },
  { row: 13, today: "`Talk to BOSS` (free)", now: "Unchanged, in the split menu", status: "Kept",
    dom: () => { expect(src("app/ask-boss-hero.tsx")).toContain('label: "Talk to BOSS", cost: "free"'); },
    covered: "apps/web/tests/discussing-an-idea.test.ts" },
  { row: 14, today: "`Research` (spends money; edition-gated)", now: "Unchanged", status: "Kept",
    dom: () => { expect(src("app/ask-boss-hero.tsx")).toContain('cost: "spends money"'); },
    covered: "apps/web/tests/composer-fits-its-controls.test.ts" },
  { row: 15, today: "`Need an example?` with 4 openers", now: "`Ideas` popover, same 4 openers", status: "Moved",
    dom: async () => {
      await show();
      expect($$(".askboss-opener")).toHaveLength(0);
      await click(tid("askboss-examples"));
      expect(tid("askboss-ideas")?.getAttribute("aria-label")).toBe("Need an example?");
      expect($$(".askboss-opener")).toHaveLength(4);
    } },
  { row: 16, today: "Research seed chips + `→ then press Research` / Community paused note", now: "`Ideas` popover", status: "Moved",
    dom: async () => {
      await show();
      await click(tid("askboss-examples"));
      expect($$(".askboss-seed")).toHaveLength(4);
      expect(txt(".askboss-research-hint")).toBe("→ then press Research");
    } },
  { row: 17, today: "16 templates: See all/Row, drift pause/resume, ←/→", now: "`Ideas` popover; inline when no run has ever happened", status: "Moved",
    dom: async () => {
      await show(freshView(), true);
      // Inline before the first run, without a press.
      expect(tid("askboss-ideas")?.getAttribute("role")).toBe("region");
      expect($$(".askboss-idea")).toHaveLength(16);
      expect(tid("askboss-grid-toggle")?.textContent).toBe("See all");
      expect($('[aria-label="Scroll left"]')).not.toBeNull();
      expect($('[aria-label="Scroll right"]')).not.toBeNull();
      expect($('[aria-label="Pause auto-scroll"], [aria-label="Resume auto-scroll"]')).not.toBeNull();
    } },
  { row: 18, today: "Template sheet (preview, Build this, Ask about it, …)", now: "Unchanged component, opened from `Ideas`", status: "Kept",
    dom: async () => {
      await show();
      await click(tid("askboss-examples"));
      await click($(".askboss-idea"));
      expect(tid("askboss-sheet")).not.toBeNull();
      expect(tid("askboss-sheet-build")).not.toBeNull();
      expect(tid("askboss-sheet-ask")).not.toBeNull();
    }, covered: "apps/web/tests/templates-are-a-gallery.test.ts" },
  { row: 19, today: "Turn output: status + elapsed, steps, error, Markdown answer", now: "The composer expands in place", status: "Kept",
    dom: () => {
      const hero = src("app/ask-boss-hero.tsx");
      for (const id of ["askboss-status", "askboss-steps", "askboss-error", "askboss-answer"]) expect(hero).toContain(`data-testid="${id}"`);
    }, covered: "apps/web/tests/a-stalled-turn-is-not-the-previous-answer.test.ts" },
  { row: 20, today: "Proposed actions head + buttons", now: "Head `BOSS can do this:`, old sentence as tooltip", status: "Kept",
    dom: () => {
      const hero = src("app/ask-boss-hero.tsx");
      expect(hero).toContain(">BOSS can do this:</p>");
      expect(hero).toContain('title="BOSS can do this for you — nothing runs until you choose."');
      expect(hero).toContain("data-testid={`askboss-action-${a.tool}`}");
    } },
  { row: 21, today: "Reset / foot links", now: "Unchanged", status: "Kept",
    dom: () => { expect(src("app/ask-boss-hero.tsx")).toContain("Ask something else"); } },
  { row: 22, today: "StartRefusal", now: "Unchanged", status: "Kept",
    dom: () => { expect(src("app/ask-boss-hero.tsx")).toContain("<StartRefusal refusal={refusal}"); } },
  { row: 23, today: "Delivery strip `Last 7 days`", now: "`THIS WEEK` head", status: "Moved",
    dom: async () => { await show(); expect(txt("#cc-week-h")).toBe("THIS WEEK"); expect($("#cc-week")?.getAttribute("data-testid")).toBe("cc-delivery"); } },
  { row: 24, today: "`N runs · X% passed` (code tasks!)", now: "`n runs · n passed` = workflow runs; builds in the tooltip", status: "Changed",
    covered: "engine/tests/test_the_command_center_view.py" },
  { row: 25, today: "`N jobs`", now: "Week tooltip `128 jobs`", status: "Moved", covered: "apps/web/tests/command-center-graphics.test.tsx" },
  { row: 26, today: "`N deployments`", now: "Week line `3 deploys`", status: "Kept", covered: "apps/web/tests/command-center-graphics.test.tsx" },
  { row: 27, today: "`$X spent` (llm_calls cost)", now: "Week line `$x` = money (the budget's measure)", status: "Changed",
    covered: "engine/tests/test_the_command_center_view.py" },
  { row: 28, today: "`N agent-hours`", now: "Week tooltip `n model hours`", status: "Changed", covered: "apps/web/tests/command-center-graphics.test.tsx" },
  { row: 29, today: "`N.NM tokens`", now: "Week tooltip", status: "Moved", covered: "apps/web/tests/command-center-graphics.test.tsx" },
  { row: 30, today: "Strip hidden when the API failed", now: "Last data plus `Can't reach BOSS`; never a confident zero", status: "Kept",
    dom: async () => {
      await showDown();
      expect(txt(".cc-status-text")).toBe("Can't reach BOSS");
      expect($('[data-testid="cc-week"]')?.children).toHaveLength(0);
      expect(txt('[data-testid="cc2-kpi-running"] .cc-num-n')).toBe("—");
    } },
  { row: 31, today: "KPI `N need you` (amber → #cc2-needs-you)", now: "Status number `NEED YOU`, same link, same count", status: "Kept",
    dom: async () => {
      await show();
      expect(tid("cc2-kpi-needs-you")?.getAttribute("href")).toBe("#cc2-needs-you");
      expect(txt('[data-testid="cc2-kpi-needs-you"] .cc-num-l')).toBe("NEED YOU");
    }, covered: "apps/web/tests/the-inbox-and-command-center-agree.test.tsx" },
  { row: 32, today: "KPI `N running` (→ #cc2-running)", now: "Status number `RUNNING`", status: "Kept",
    dom: async () => { await show(); expect(tid("cc2-kpi-running")?.getAttribute("href")).toBe("#cc2-running"); } },
  { row: 33, today: "KPI `N finished · 7d`", now: "Week `n passed` + bars; `DONE TODAY`", status: "Changed",
    dom: async () => { await show(); expect(txt('[data-testid="cc-kpi-done-today"] .cc-num-l')).toBe("DONE TODAY"); },
    covered: "engine/tests/test_the_command_center_view.py" },
  { row: 34, today: "KPI `N failed · 7d`", now: "Week red segments + `DONE TODAY` tooltip `Failed: n` + red caps", status: "Changed",
    dom: async () => { await show(); expect(tid("cc-kpi-done-today")?.getAttribute("title")).toBe("Runs that finished today. Failed: 1."); } },
  { row: 35, today: "Load error banner", now: "`Can't reach BOSS`, reason and time in the tooltip; last data dimmed", status: "Changed",
    dom: async () => {
      await showDown();
      expect(tid("cc2-load-error")?.textContent).toBe("Can't reach BOSS");
      expect(tid("cc2-load-error")?.getAttribute("title")).toContain("Postgres is not reachable.");
    }, covered: "apps/web/tests/command-center-bridge.test.tsx" },
  { row: 36, today: "What needs you head", now: "`NEEDS YOU n` + `All in Approvals ↗`", status: "Kept",
    dom: async () => {
      await show();
      expect(txt("#cc2-needs-you-h")).toBe("NEEDS YOU");
      expect(txt("#cc2-needs-you .cc-head-link")).toBe("All in Approvals ↗");
    } },
  { row: 37, today: "Stopped work first, shared AttentionCard row",
    now: "Same component; today's decisions first, then today's stopped, older stopped as one line (independent test: week-old failures sat above today's gates); decide_inline honoured",
    status: "Changed",
    dom: async () => {
      await show();
      const ids = $$("#cc2-needs-you [data-att-card]").map((c) => c.getAttribute("data-att-id"));
      expect(ids).toContain("run:wfr_ship:build");
      // The fixture's gate allows inline: it is approved in place here, as on Live.
      expect($('#cc2-needs-you [data-att-id="dec_gate"] [data-testid="ag-att-approve"]')).not.toBeNull();
    } },
  { row: 38, today: "Decision cards with server actions; Retry/Skip; Review ↗", now: "Same (shared card)", status: "Kept",
    dom: async () => { await show(); expect(tid("ag-att-answer-retry_step")).not.toBeNull(); },
    covered: "apps/web/tests/command-center-is-about-workflow-runs.test.tsx" },
  { row: 39, today: "Setup lines → ?area=setup&focus=", now: "Same", status: "Kept",
    dom: async () => {
      await show();
      const a = $('[data-testid="cc2-setup"] a')!;
      expect(a.getAttribute("href")).toBe("/?org=org_1&area=setup&focus=repository");
      expect(a.textContent).toBe("Setup: Connect a repository ↗");
      expect(a.getAttribute("title")).toBe("Fix in Repo and models");
    } },
  { row: 40, today: "Setup gaps not counted in need-you", now: "Same (attention_counts)", status: "Kept",
    dom: async () => { await show(); expect(txt("#cc2-needs-you .cc-head-count")).toBe("1"); },
    covered: "apps/api/tests/test_command_center_and_inbox_agree.py" },
  { row: 41, today: "Empty (20 words)", now: "`Nothing needs you` + tooltip", status: "Changed",
    dom: async () => {
      await show(freshView());
      expect(txt('[data-testid="cc2-needs-you-empty"]')).toBe("Nothing needs you");
      expect($('[data-testid="cc2-needs-you-empty"] [title]')?.getAttribute("title")).toBe("Gates, failed steps and setup gaps appear here.");
    } },
  { row: 42, today: "Expired-answerable line + `Review expired decisions`", now: "`n expired · still answerable ↗` + tooltip; same link", status: "Kept",
    dom: async () => {
      await show();
      const a = tid("cc2-expired-answerable")!;
      expect(a.textContent).toBe("3 expired · still answerable ↗");
      expect(a.getAttribute("href")).toBe(decisionsHref("expired", "org_1"));
      expect(a.getAttribute("title")).toBe("Nothing was applied. You can still answer them.");
    } },
  { row: 43, today: "Running now: MissionRow row", now: "`RUNNING`: MissionRow lane (full hops) + the day strip", status: "Kept + richer",
    dom: async () => {
      await show();
      expect($$("#cc2-running .cc-lane .ag-mission").length).toBeGreaterThan(0);
      expect($('#cc2-running [data-testid="cc-day-strip"]')).not.toBeNull();
      expect(txt("#cc-running-h")).toBe("RUNNING");
    } },
  { row: 44, today: "Running empty sentence with Workflow link", now: "`Nothing running` + `Start work ↗` + head `Workflow ↗`", status: "Changed",
    dom: async () => {
      await show(freshView());
      expect(txt('[data-testid="cc2-running-empty"]')).toBe("Nothing runningStart work ↗");
      const wf = $$("#cc2-running .cc-head-link").find((a) => a.textContent === "Workflow ↗")!;
      expect(wf.getAttribute("href")).toBe("/?org=org_1&area=delivery_workflows");
      await click($('[data-testid="cc2-running-empty"] button'));
      expect(document.activeElement?.getAttribute("data-testid")).toBe("askboss-input");
    } },
  { row: 45, today: "Recent outcomes: pill, title → run, sub (workflow · error/date)", now: "`DONE LATELY` chips; glyph; workflow/error in tooltip", status: "Moved",
    dom: async () => {
      await show();
      const rows = $$('[data-testid="cc2-recent-row"]');
      expect(rows.length).toBe(2);
      expect(rows[0].getAttribute("href")).toBe(runHref("wfr_170", "org_1"));
      expect(rows[0].getAttribute("title")).toBe("Audit · npm audit found 2 critical issues");
      expect(txt("#cc-done-h")).toBe("DONE LATELY");
    } },
  { row: 46, today: "Recent empty sentence", now: "`Nothing finished this week`", status: "Kept",
    dom: async () => { await show(freshView()); expect(txt('[data-testid="cc-done-empty"]')).toBe("Nothing finished this week"); } },
  { row: 47, today: "Help card 'What you can do here'", now: "Inline 3 rows before the first run; `?` popover otherwise", status: "Moved",
    dom: async () => {
      await show(freshView(), true);
      expect($('#cc-compose [data-testid="console-capabilities-card"]')).not.toBeNull();
      unmount();
      await show();
      expect($('[data-testid="console-capabilities-card"]')).toBeNull();
      await click(tid("cc-help"));
      expect($('.cc-help-pop [data-testid="console-capabilities-card"]')).not.toBeNull();
      expect(txt(".cc-help-pop")).toContain("not guaranteed");
    }, covered: "apps/web/tests/onboarding-help-card.test.tsx" },
  { row: 48, today: "15s poll, onChanged, 30s clock, in-flight guard", now: "10s usePolled + ETag; respects Paused and hidden", status: "Changed",
    covered: "apps/web/tests/command-center-bridge.test.tsx" },
  { row: 49, today: "Anchors #cc2-needs-you, #cc2-running, #cc2-recent", now: "Kept as ids", status: "Kept",
    dom: async () => { await show(); for (const id of ["cc2-needs-you", "cc2-running", "cc2-recent"]) expect(document.getElementById(id)).not.toBeNull(); } },
  { row: 50, today: "testids command-center, cc2-*, cc-delivery, console-capabilities-card, askboss-*", now: "All kept; new cc-* ids", status: "Kept",
    dom: async () => {
      await show();
      for (const id of ["command-center", "cc2-kpi-needs-you", "cc2-kpi-running", "cc2-setup", "cc2-expired-answerable",
                        "cc2-recent-row", "cc-delivery", "askboss-input", "askboss-examples", "askboss-default-workflow",
                        "cc-status", "cc-floor", "cc-today", "cc-day-strip", "cc-systems", "cc-dot-budget", "cc-week"]) {
        expect(tid(id), id).not.toBeNull();
      }
      unmount();
      await show(freshView(), true);
      for (const id of ["cc2-needs-you-empty", "cc2-running-empty", "console-capabilities-card"]) expect(tid(id), id).not.toBeNull();
      unmount();
      await showDown();
      expect(tid("cc2-load-error")).not.toBeNull();
    } },
  { row: 51, today: "E2E press askboss-examples → askboss-default-workflow", now: "`Ideas` carries the id; the picker stays visible", status: "Kept",
    dom: () => {
      const contract = readFileSync(resolve(REPO, "tests/e2e/harness/console_contract.py"), "utf8");
      expect(contract).toContain('Press("askboss-examples", "app/ask-boss-hero.tsx",');
      expect(contract).toContain('expect=_tid("askboss-default-workflow")');
    } },
  { row: 52, today: "GET /v1/console/command-center", now: "Kept (deprecated, tested)", status: "Kept",
    covered: "apps/api/tests/test_the_command_center_reads_workflow_runs.py" },
  { row: 53, today: "GET /v1/metrics/delivery", now: "Kept; called inside the view (memoised)", status: "Kept",
    covered: "apps/api/tests/test_the_command_center_view.py" },
  { row: 54, today: "Presence pill / Live chip / projects strip", now: "Untouched; no second copy here", status: "Kept",
    dom: async () => { await show(); expect($('[data-testid="command-center"] [data-testid="presence-pill"]')).toBeNull(); } },
  { row: 55, today: "'Live coordination' orb graph (fake data)", now: "The Floor, on real presence + acts", status: "Restored on real data",
    dom: async () => {
      await show();
      expect(txt("#cc-floor-h")).toBe("THE FLOOR");
      const map = $$("#cc-floor .cc-head-link").find((a) => a.textContent === "Open map ↗")!;
      expect(map.getAttribute("href")).toBe("/?org=org_1&area=workforce&view=organization");
    } },
];

beforeAll(() => {
  Object.defineProperty(window, "matchMedia", {
    configurable: true, writable: true,
    value: (q: string) => ({ matches: false, media: q, onchange: null, addEventListener: () => {},
                             removeEventListener: () => {}, addListener: () => {}, removeListener: () => {},
                             dispatchEvent: () => false }),
  });
});
beforeEach(() => {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440 });
  window.history.replaceState(null, "", "/?area=overview");
  vi.stubGlobal("fetch", vi.fn(async (url: string) => (String(url).startsWith("/api/delivery-workflows")
    ? { ok: true, status: 200, headers: new Headers(),
        json: async () => ({ items: [{ workflow_key: "ship_it", name: "Ship it", is_default: true }] }) }
    : { ok: false, status: 304, headers: new Headers(), json: async () => ({}) })));
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("the parity table holds (command-center spec §9)", () => {
  it("has every row, 1 to 55, once", () => {
    expect(ROWS.map((r) => r.row)).toEqual(Array.from({ length: 55 }, (_, i) => i + 1));
  });

  it("drops nothing operator-facing", () => {
    expect(ROWS.filter((r) => r.status === "Dropped")).toEqual([]);
  });

  it("every row is held by a check here or names the test that holds it, and that test exists", () => {
    const unheld = ROWS.filter((r) => !r.dom && !r.covered).map((r) => r.row);
    expect(unheld).toEqual([]);
    const missing = ROWS.filter((r) => r.covered && !existsSync(resolve(REPO, r.covered)))
      .map((r) => `${r.row}: ${r.covered}`);
    expect(missing).toEqual([]);
  });

  it.each(ROWS.filter((r) => r.dom).map((r) => [r.row, r.today, r]))("row %i — %s", async (_n, _t, r) => {
    await (r as Row).dom!();
  });
});

describe("the composer keeps the page's copy budget (§5.1)", () => {
  it("fresh install, Ideas inline: no visible run over six words outside the operator's text", async () => {
    await show(freshView(), true);
    expect(overBudget($("#cc-compose")!)).toEqual([]);
  });

  it("busy page with the Ideas popover open", async () => {
    await show();
    await click(tid("askboss-examples"));
    expect(overBudget($('[data-testid="command-center"]')!)).toEqual([]);
  });
});


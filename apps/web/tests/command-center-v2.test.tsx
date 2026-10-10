// @vitest-environment happy-dom
/**
 * The Command Center (cc v2), rendered and read: the hero, Needs you · In flight · Health, the
 * two shelves and the pulse, on the view's wire shape and the Factory's apps — and when the page
 * may move at all. Replaces command-center-bridge.test.tsx (the Floor / Today / day strip /
 * week page it tested is gone).
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";
import { apps, failed, freshView, gate, mission, pulse, quietView, view } from "./_helpers/command-center-view-fixture";
import { parseCommandCenterView, viewerZone, type CommandCenterView } from "../lib/command-center-view";
import { setConsoleLive } from "../lib/console-live";

const cleanUp = vi.fn(async (_f: FormData) => ({ ok: true, result: "Cleared 10 old questions" }));
vi.mock("../app/command-center/actions", () => ({ cleanUpOldAction: (f: FormData) => cleanUp(f) }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(), usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as { React?: typeof React }).React = React;

const { CommandCenter, motionOf, needsOrder } = await import("../app/command-center-ui");
const { AskBossHero } = await import("../app/ask-boss-hero");

type Wire = Record<string, unknown>;
const V = (w: Wire): CommandCenterView => parseCommandCenterView(w);
const page = (initial: CommandCenterView | null, over: Record<string, unknown> = {}) => (
  <CommandCenter projectId="p1" orgId="org_1" actor="ceo" initial={initial} tz={viewerZone()}
    composer={<AskBossHero projectId="p1" actor="ceo" orgId="org_1" deploymentAvailable={false} layout="hero" />}
    {...over} />
);
const text = (sel: string) => $(sel)?.textContent ?? "";
const tid = (id: string) => $(`[data-testid="${id}"]`);

let fetchMock: ReturnType<typeof vi.fn>;
function stubFetch(appsBody: Wire | null = apps()) {
  fetchMock = vi.fn(async (url: string) => {
    if (String(url).startsWith("/v1/factory/apps") && appsBody) {
      return { ok: true, status: 200, headers: new Headers({ etag: "\"a1\"" }), json: async () => appsBody } as unknown as Response;
    }
    return { ok: false, status: 304, headers: new Headers(), json: async () => ({}) } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
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
  stubMotion(false);
  setConsoleLive(true);
  try { window.localStorage.clear(); window.sessionStorage.clear(); } catch { /* none */ }
  stubFetch();
  cleanUp.mockClear();
});
afterEach(() => {
  unmount();
  vi.unstubAllGlobals();
  setConsoleLive(true);
});

describe("the hero", () => {
  it("opens help outside the console container and Close returns to the question button", async () => {
    await mount(page(V(view()), { help: <p>Real help content</p> }));
    const opener = tid("cc-help")!;
    await click(opener);
    const dialog = $('[role="dialog"][aria-label="What you can do here"]')!;
    expect(dialog.parentElement).toBe(document.body);
    expect(opener.getAttribute("aria-expanded")).toBe("true");
    expect(opener.getAttribute("aria-controls")).toBe(dialog.id);
    const close = dialog.querySelector<HTMLButtonElement>(".cc-help-close")!;
    expect(document.activeElement).toBe(close);
    await click(close);
    expect($('.cc-help-pop')).toBeNull();
    expect(document.activeElement).toBe(opener);
    expect(opener.getAttribute("aria-expanded")).toBe("false");
  });

  it("Escape returns to the actual Getting started opener", async () => {
    await mount(page(V(freshView()), { help: <p>Real help content</p> }));
    const opener = $('.cc-help-rows .ag-link')!;
    expect(opener.textContent).toBe("Getting started");
    expect(opener.getAttribute("aria-haspopup")).toBe("dialog");
    await click(opener);
    expect($('.cc-help-pop')).not.toBeNull();
    expect(opener.getAttribute("aria-expanded")).toBe("true");
    expect(opener.getAttribute("aria-controls")).toBe($('.cc-help-pop')!.id);
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect($('.cc-help-pop')).toBeNull();
    expect(document.activeElement).toBe(opener);
    expect(opener.getAttribute("aria-expanded")).toBe("false");
  });

  it("keeps inside interactions open and dismisses outside without stealing focus", async () => {
    await mount(page(V(view()), { help: <button type="button" data-testid="help-inner">Read more</button> }));
    await click(tid("cc-help"));
    const inner = tid("help-inner")!;
    await act(async () => inner.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true })));
    expect($('.cc-help-pop')).not.toBeNull();
    const outside = document.createElement("button");
    document.body.appendChild(outside);
    outside.focus();
    await act(async () => outside.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true })));
    expect($('.cc-help-pop')).toBeNull();
    expect(document.activeElement).toBe(outside);
  });

  it("leads with the pending decision and says when work last happened", async () => {
    await mount(page(V(view())));
    expect(tid("cc-next-action")?.getAttribute("data-kind")).toBe("decision");
    expect(text(".cc-headline")).toContain("Publish");
    expect(text('[data-testid="cc-status"]')).toMatch(/^BOSS connected/);
    // Something runs in this view: the eyebrow says so instead of a last-work age.
    expect(text('[data-testid="cc-last-work"]')).toBe("working now");
    // The server's sentence is still said, beside the systems word (never re-derived).
    expect(text('[data-testid="cc-status-text"]')).toBe("1 stopped");
    expect($('[data-testid="cc-status"]')?.getAttribute("data-key")).toBe("stopped");
  });

  it("service availability stays separate from the host system finding", async () => {
    const w = view();
    await mount(page(V({ ...w, systems: (w.systems as Wire[]).map((s) => s.key === "host" ? { ...s, tone: "red", state: "Struggling" } : s) })));
    expect(text('[data-testid="cc-status"]')).toMatch(/^BOSS connected/);
    expect(tid("cc-dot-host")?.getAttribute("title")).toContain("Struggling");
  });

  it("the composer is AskBossHero in the hero layout: pickers as chips, Talk to BOSS beside Build", async () => {
    await mount(page(V(view())));
    expect($('#cc-compose .askboss')?.getAttribute("data-layout")).toBe("hero");
    expect(tid("askboss-input")).not.toBeNull();
    expect(tid("askboss-talk")?.textContent).toBe("Talk to BOSS");
    expect(tid("split-build")?.textContent).toBe("Build →");
    expect(tid("askboss-add-existing")?.getAttribute("href")).toContain("compose=add");
    expect(tid("askboss-add-existing")?.getAttribute("href")).toContain("org=org_1");
  });

  it("the proof row counts the Factory's apps: built, PRs open, agents · teams, spend", async () => {
    await mount(page(V(view())));
    const proof = $$('[data-testid="cc-proof"] b').map((b) => b.textContent);
    expect(proof).toEqual(["5", "2", "34", "$13"]);
    expect(tid("cc-proof-apps")?.getAttribute("href")).toContain("area=code_tasks");
    expect(tid("cc-proof-spent")?.getAttribute("href")).toContain("area=budget");
  });

  it("the team: a tile per team with real faces; waiting on you is an amber ring, working glows", async () => {
    await mount(page(V(view({ attention: [gate(), failed()] }))));
    expect($$('[data-testid^="cc-tile-"]').length).toBe(4);
    expect($$('[data-testid="cc-tile-engineering"] .ag-avatar').length).toBe(2);
    // The gate is attributed to media_publisher: its face waits on you.
    expect($('[data-testid="cc-tile-media"] .cc-face-in')?.getAttribute("data-waiting")).toBe("true");
    expect($('[data-testid="cc-tile-engineering"] .cc-face-in[data-working="true"]')).not.toBeNull();
    expect(text('[data-testid="cc-team-foot"]')).toBe("2 working1 waiting on you4 ready");
    expect($('[data-testid="cc-tile-quality"] .cc-tile-name')?.getAttribute("href")).toContain("area=workforce");
    // A face opens its agent's live line, with Talk and its page.
    await click($('[data-testid="cc-tile-quality"] [data-testid="cc-face"]'));
    expect(tid("cc-facecard")?.textContent).toContain("Now: Testing");
    expect(tid("cc-face-talk")).not.toBeNull();
    expect(tid("cc-face-open")?.getAttribute("href")).toContain("role=qa");
    expect(tid("cc-open-map")?.getAttribute("href")).toContain("view=organization");
  });
});

describe("needs you", () => {
  it("each item is a sentence with its face, and the badge is counts.needs_you", async () => {
    await mount(page(V(view({ attention: [gate(), failed()] }))));
    expect(text('[data-testid="cc2-kpi-needs-you"]')).toBe("1");
    const rows = $$('[data-testid="cc-need"]');
    expect(rows.map((r) => r.getAttribute("data-kind"))).toEqual(["approval", "failed_step"]);
    expect(rows[0].textContent).toContain("“Publish” is waiting for you on Weekly video");
    expect(rows[0].querySelector(".ag-avatar")).not.toBeNull();
  });

  it("a plan waiting on you says so, with who drafted it", async () => {
    const plan = gate({ id: "dec_plan", decision_kind: "general", title: "Approve the plan?", role_key: "ceo_agent", run: null,
                        build: { run_id: "crun_a", app: "Interview Scorecard", where: "Plan", failing: [], retryable: false,
                                 plan_url: "/v1/code-tasks/crun_a" } });
    await mount(page(V(view({ attention: [plan] }))));
    const row = $$('[data-testid="cc-need"]')[0];
    expect(row.querySelector("b")?.textContent).toBe("Approve the plan · Interview Scorecard");
    expect(row.textContent).toMatch(/drafted it · waiting \d+[mhd]/);
    expect(row.querySelector('[data-testid="cc-need-review"]')?.className).toContain("primary");
  });

  it("a raised decision says its source, never a raw code", async () => {
    const d = gate({ id: "dec_r", decision_kind: "general", title: "Allow an OpenAI call without a reservation",
                     detail: "", source: "Spend guard", role_key: null, run: null });
    await mount(page(V(view({ attention: [d] }))));
    const row = $$('[data-testid="cc-need"]')[0];
    expect(row.textContent).toContain("Allow an OpenAI call without a reservation");
    expect(row.textContent).toMatch(/Spend guard · raised \d+[mhd] ago/);
    expect(row.querySelector(".cc-need-icon")?.textContent).toBe("$");
  });

  it("Review opens the shared card in place: the server's buttons, approve and reject inline", async () => {
    await mount(page(V(view({ attention: [gate({ decide_inline: true })] }))));
    expect(tid("ag-att-approve")).toBeNull();
    await click(tid("cc-need-review"));
    expect(tid("ag-att-approve")?.textContent).toContain("Publish it");
    expect(tid("cc-need-review")?.textContent).toBe("Close");
  });

  it("past three rows, `+n more` opens the rest in place", async () => {
    const many = [1, 2, 3, 4, 5].map((i) => gate({ id: `dec_${i}`, decision_id: `dec_${i}` }));
    await mount(page(V(view({ attention: many }))));
    expect($$('[data-testid="cc-need"]').length).toBe(3);
    expect(text('[data-testid="cc-att-more"]')).toBe("Show all 5");
    await click(tid("cc-att-more"));
    expect($$('[data-testid="cc-need"]').length).toBe(5);
  });

  it("stopped work from before today is one line; expired-but-answerable links to Approvals", async () => {
    await mount(page(V(view({ attention: [failed({ at: "2026-09-20T09:00:00Z" })],
                               counts: { agents: 7, working: 0, needs_you: 0, stopped: 1, setup: 0, running: 0, stalled: 0, expired_answerable: 4 } }))));
    expect($$('[data-testid="cc-need"]').length).toBe(0);
    expect(text('[data-testid="cc-stopped-earlier"]')).toBe("1 stopped earlier ↗");
    expect(text('[data-testid="cc2-expired-answerable"]')).toBe("4 expired · still answerable ↗");
    expect(tid("cc2-expired-answerable")?.getAttribute("href")).toContain("decision_filter=expired");
  });

  it("a budget that refuses at the door is the first row", async () => {
    await mount(page(V(view({ attention: [gate()], budget_refusal: {
      period: "month", cap_usd_micros: 1, actual_usd_micros: 2, resets_at_label: "", title: "Budget refused new work",
      detail: "$2 of $1 this month." } }))));
    expect($$('[data-testid="cc-need"]')[0].textContent).toContain("Budget refused new work");
  });

  it("a setup gap links to its row in Repo and models", async () => {
    const gap = { id: "ready:github", kind: "readiness", title: "Connect a repository", detail: "",
                  fix: { area: "setup", label: "Fix in Repo and models", focus: "repository" }, actions: [], answers: [],
                  decide_inline: false, at: null };
    await mount(page(V(view({ attention: [gap] }))));
    expect(tid("cc-need-fix")?.getAttribute("href")).toContain("focus=repository");
  });

  it("nothing waiting: one quiet line", async () => {
    await mount(page(V(quietView())));
    expect(text('[data-testid="cc2-needs-you-empty"]')).toContain("Nothing needs you");
  });

  it("the server's wire paints first, pressable; unreachable says so in the eyebrow", async () => {
    await mount(page(null, { initialWire: view({ attention: [gate()] }), initialError: null }));
    expect($(".cc-needs fieldset")?.hasAttribute("disabled")).toBe(false);
    unmount();
    await mount(page(null, { initialError: "Postgres is not reachable." }));
    expect(text('[data-testid="cc2-load-error"]')).toBe("Can't reach BOSS");
    expect($('[data-testid="command-center"]')?.getAttribute("data-reachable")).toBe("false");
  });
});

describe("in flight", () => {
  it("only what is moving, with its agent and step, linked to the run", async () => {
    await mount(page(V(view({ missions: [mission()] }))));
    const row = tid("cc-flight-row")!;
    expect(row.textContent).toContain("Ship it #182");
    expect(row.textContent).toContain("step 4/7");
    expect(row.getAttribute("href")).toContain("wfr=wfr_182");
    expect(row.querySelector(".ag-avatar")).not.toBeNull();
  });

  it("nothing moving: the radar, and a word to start", async () => {
    await mount(page(V(quietView())));
    expect(text('[data-testid="cc2-running-empty"]')).toContain("Nothing running right now");
    expect($(".cc-radar")).not.toBeNull();
  });

  it("stalled runs fold into one row with Resume or close; they are not counted running", async () => {
    const stalled = (id: string, s: string) => mission({ run_id: id, subject: s, state: "stalled", verb: "Stalled",
                                                         last_progress_at: "2026-09-12T17:36:00Z" });
    await mount(page(V(view({ missions: [stalled("w1", "Feature flag expiry tracker"), stalled("w2", "Subscription audit")],
                               counts: { agents: 7, working: 0, needs_you: 0, stopped: 0, setup: 0, running: 0, stalled: 2, expired_answerable: 0 } }))));
    expect(text('[data-testid="cc2-kpi-running"]')).toBe("0");
    expect(tid("cc2-running-empty")).not.toBeNull();
    expect(text('[data-testid="cc-stalled"] b')).toMatch(/^2 runs stalled \d+\+ days$/);
    expect(text('[data-testid="cc-stalled"] div span')).toContain("Feature flag expiry tracker · Subscription audit");
    expect(tid("cc-stalled-open")?.getAttribute("href")).toContain("area=delivery_workflows");
  });

  it("old stuck builds are one row; Clean up runs the doctor's safe fix and says what it did", async () => {
    await mount(page(V(view({ cleanup: { count: 10, since: "2026-08-09T11:55:00Z", ids: ["shepherd:a"],
                                         title: "10 old builds stuck since Aug 9", detail: "Shepherd can clear them safely" } }))));
    expect(text('[data-testid="cc-cleanup"] b')).toBe("10 old builds stuck since Aug 9");
    await click(tid("cc-cleanup-go"));
    expect(cleanUp).toHaveBeenCalledTimes(1);
    const form = cleanUp.mock.calls[0][0] as FormData;
    expect([form.get("project_id"), form.get("actor")]).toEqual(["p1", "ceo"]);
    expect(text('[data-testid="cc-cleanup"] b')).toBe("Cleared 10 old questions");
  });

  it("finished runs are chips that say their outcome and open the run", async () => {
    await mount(page(V(view())));
    const chips = $$('[data-testid="cc2-recent-row"]');
    expect(chips.map((c) => c.getAttribute("data-outcome"))).toEqual(["failed", "ok"]);
    expect(chips[0].getAttribute("title")).toContain("npm audit found 2 critical issues");
  });
});

describe("health", () => {
  it("money against the cap (subscription calls free), linked to Budget", async () => {
    await mount(page(V(view())));
    expect(text(".cc-gauge-text b")).toBe("$84.00 of $200");
    expect(text(".cc-gauge-text > span")).toBe("This month · subscription calls free");
    expect(tid("cc-budget-link")?.getAttribute("href")).toContain("area=budget");
  });

  it("seven systems, each a dot with the server's reason as its tooltip, linked with the org", async () => {
    await mount(page(V(view())));
    const dots = $$('[data-testid^="cc-dot-"]');
    expect(dots.length).toBe(7);
    expect(tid("cc-dot-models")?.getAttribute("title")).toBe("OK · Claude Code working · 212 calls today");
    expect(tid("cc-dot-models")?.getAttribute("href")).toBe("/?org=org_1&area=setup&focus=models");
    expect(tid("cc-dot-models")?.closest(".cc-sec")).not.toBeNull();
  });
});

describe("the shelves", () => {
  it("templates: one scrolling row of all sixteen with cover art; a card opens its page", async () => {
    await mount(page(V(view())));
    const cards = $$('[data-testid="cc-tpl"]');
    expect(cards.length).toBe(16);
    expect(cards[0].querySelector("svg")).not.toBeNull();
    expect(text('[data-testid="cc-templates-all"]')).toBe("All 16 ↗");
    await click(cards[0]);
    expect(window.location.search).toContain("template=");
    expect(tid("askboss-sheet")).not.toBeNull();
  });

  it("All 16 opens the templates on their own page; Back closes it", async () => {
    await mount(page(V(view())));
    await click(tid("cc-templates-all"));
    expect(window.location.search).toContain("templates=all");
    expect($$('[data-testid="cc-gallery"] [data-testid="cc-tpl"]').length).toBe(16);
    await click(tid("cc-gallery-back"));
    expect(tid("cc-gallery")).toBeNull();
  });

  it("apps: needs-you first, the Factory's state as a chip, template art or a monogram, faces, builds and cost", async () => {
    await mount(page(V(view())));
    const cards = $$('[data-testid="cc-app"]');
    expect(cards.length).toBe(5);
    expect(cards[0].textContent).toContain("Interview Scorecard");
    expect(cards[0].querySelector(".cc-app-state")?.textContent).toBe("Plan waiting on you");
    expect(cards[1].querySelector(".cc-app-state")?.textContent).toBe("PR #207 open");
    expect(cards[1].querySelector(".cc-mono")).not.toBeNull();   // no template: a monogram
    expect(cards[0].querySelector(".cc-mono")).toBeNull();       // Interview scorecard: its cover
    expect(cards[0].textContent).toContain("2 builds · $3.58");
    expect(cards[0].getAttribute("href")).toContain("build=crun_a");
  });

  it("an app whose latest attempt waits on its plan says so, and opens that attempt", async () => {
    const w = apps() as { apps: Wire[] };
    w.apps[1] = { ...w.apps[1], state: "needs_plan", word: "Needs you · plan",
                  attempts: [{ run_id: "crun_b2", state: "needs_plan" }, { run_id: "crun_b", state: "pr_open" }] };
    stubFetch(w);
    await mount(page(V(view())));
    const card = $$('[data-testid="cc-app"]').find((c) => c.textContent?.includes("Feature-Flag"))!;
    expect(card.querySelector(".cc-app-state")?.textContent).toBe("Plan waiting on you");
    expect(card.getAttribute("href")).toContain("build=crun_b2");
    expect(text('[data-testid="cc-apps-f-needs"]')).toBe("Needs you 2");
  });

  it("changing the filter takes the row back to its start", async () => {
    await mount(page(V(view())));
    const track = tid("cc-apps-rail")!;
    track.scrollLeft = 300;
    await click(tid("cc-apps-f-pr"));
    expect(tid("cc-apps-rail")!.scrollLeft).toBe(0);
  });

  it("each app card has its own Add a feature door, into the composer with the app chosen", async () => {
    await mount(page(V(view())));
    const add = $$('[data-testid="cc-app-add"]')[0];
    expect(add.textContent).toBe("＋ Add a feature");
    expect(add.getAttribute("href")).toContain("compose=add");
    expect(add.getAttribute("href")).toContain("app=crun_a");
    expect(add.getAttribute("href")).toContain("org=org_1");
  });

  it("filter chips: All / PR open / Needs you / Stopped, counted", async () => {
    await mount(page(V(view())));
    expect($$(".cc-f").map((f) => f.textContent)).toEqual(["All 5", "PR open 2", "Needs you 1", "Stopped 2"]);
    await click(tid("cc-apps-f-stopped"));
    expect($$('[data-testid="cc-app"]').length).toBe(2);
  });
});

describe("the pulse", () => {
  it("is the server's: a day per cell, the totals line and the five most active agents", async () => {
    await mount(page(V(view())));
    const p = pulse() as { days: unknown[]; total: number };
    expect($$('[data-testid="cc-heat"] i:not(.pad)').length).toBe(p.days.length);
    expect(text('[data-testid="cc-heat-foot"]')).toContain(`${p.total}agent steps`);
    expect(text('[data-testid="cc-heat-foot"]')).toContain("Busiest Sep 3 · 71 steps");
    expect(text('[data-testid="cc-heat-foot"]')).toContain("2PRs open");
    expect($$('[data-testid="cc-tops"] .cc-top-row').length).toBe(5);
  });

  it("latest: one line per app — who did what to which app, and a review's verdict", async () => {
    await mount(page(V(view())));
    const lines = $$('[data-testid="cc-ev"]').map((e) => e.textContent ?? "");
    expect(lines.length).toBe(4);
    expect(lines[1]).toMatch(/planned Interview Scorecard/);
    expect(lines[2]).toMatch(/reviewed Changelog Composer — asked for changes/);
    expect($$('[data-testid="cc-ev"]')[3].getAttribute("href")).toContain("wfr=wfr_182");
  });

  it("no work in 14 weeks is one quiet line, not an empty chart", async () => {
    await mount(page(V(freshView())));
    expect(tid("cc-pulse-empty")).not.toBeNull();
    expect(tid("cc-heat")).toBeNull();
  });
});

describe("re-test (independent tester)", () => {
  it("a fresh install leads with the existing composer; templates stay available and no empty pulse", async () => {
    stubFetch({ project_id: "p1", etag: "e", apps: [] });
    await mount(page(V(freshView())));
    expect(text(".cc-headline")).toBe("Describe your first task");
    expect(tid("cc-next-go")).toBeNull();
    expect(tid("cc-compose-disclosure")?.hasAttribute("open")).toBe(true);
    expect(tid("cc-pulse")).toBeNull();
    expect(tid("cc-templates")).not.toBeNull();
    expect(tid("askboss-ideas")).toBeNull();   // never inline in the composer any more
  });

  it("the Needs you number is decisions; other rows are said apart", async () => {
    await mount(page(V(view({ attention: [gate(), failed()] }))));
    expect(text('[data-testid="cc2-kpi-needs-you"]')).toBe("1");
    expect(text('[data-testid="cc-needs-more"]')).toBe("+1 other");
    expect(text('[data-testid="cc-stopped-chip"]')).toBe("1 stopped");
  });

  it("today's money is on the card, done today says its failures, this week opens by touch", async () => {
    await mount(page(V(view())));
    expect(text('[data-testid="cc-today-spend"]')).toBe("Today: $4.12 · $3.80 included");
    expect(text('[data-testid="cc-kpi-done-today"]')).toBe("4 done today · 1 failed");
    expect($('[data-testid="cc-week"]')?.tagName).toBe("DETAILS");
    expect(text('[data-testid="cc-week"] summary')).toBe("This week: 38 finished · 4 failed");
  });

  it("a face glows from the board's one working state (the server folds in live runs)", async () => {
    await mount(page(V(view())));
    // software_engineer is `working` on the board: it glows and moves; idle faces are still.
    expect($('[data-testid="cc-tile-engineering"] .cc-face-in[data-working="true"]')).not.toBeNull();
    expect($$('[data-testid="cc-tile-engineering"] .cc-face[data-live="true"]').length).toBe(1);
    expect($('[data-testid="cc-tile-leadership"] .cc-face[data-live="true"]')).toBeNull();
  });

  it("the shelf follows Needs you: a merge waiting on you anywhere in the app's lineage", async () => {
    const merge = gate({ id: "dec_m", decision_kind: "pull_request", title: "Merge PR #207?", role_key: "software_engineer",
                         run: null, build: { run_id: "crun_b_old", app: "Feature-Flag Expiry Tracker", where: "PR open", failing: [],
                                             retryable: false, pr_url: "https://github.com/a/b/pull/207" } });
    const w = apps() as { apps: Wire[] };
    w.apps[1] = { ...w.apps[1], run_ids: ["crun_b", "crun_b_old"] };
    stubFetch(w);
    await mount(page(V(view({ attention: [merge] }))));
    const card = $$('[data-testid="cc-app"]').find((c) => c.textContent?.includes("Feature-Flag"))!;
    expect(card.querySelector(".cc-app-state")?.textContent).toBe("Merge waiting on you");
    expect(card.getAttribute("href")).toContain("build=crun_b_old");
  });

  it("subscription calls are called free only when a subscription is connected", async () => {
    await mount(page(V(view({ budget: { ...(view().budget as Wire), subscription: false } }))));
    expect(text(".cc-gauge-text > span")).toBe("This month");
  });

  it("done today says its failures even when nothing finished", async () => {
    await mount(page(V(view({ today: { ...(view().today as Wire), runs_finished: 0, runs_failed: 2 } }))));
    expect(text('[data-testid="cc-kpi-done-today"]')).toBe("0 done today · 2 failed");
  });

  it("the latest feed says one move once", async () => {
    const { uniqueEvents } = await import("../app/command-center/pulse");
    const e = { roleKey: "qa", verb: "checked", subject: "X", verdict: null, at: "a", runId: "r", kind: "build" as const, url: null };
    expect(uniqueEvents([e, { ...e, at: "b" }, { ...e, subject: "Y" }]).length).toBe(2);
  });

  it("a face's card sits in the popover layer, and Talk opens the dock", async () => {
    await mount(page(V(view())));
    await click(tid("cc-face"));
    expect(tid("cc-facecard")?.parentElement).toBe(document.body);
    await click(tid("cc-face-talk"));
    expect(window.location.search).toContain("dock_role_key=");
    expect(window.location.search).toContain("agent=general");
  });

  it("the service line stays separate from a disk finding", async () => {
    const w = view();
    await mount(page(V({ ...w, systems: (w.systems as Wire[]).map((s) => s.key === "host" ? { ...s, tone: "amber", state: "Disk" } : s) })));
    expect(text('[data-testid="cc-status"]')).toMatch(/^BOSS connected/);
    expect(tid("cc-dot-host")?.getAttribute("title")).toContain("Disk");
  });

  it("an agent without a callsign is named once in its tooltip", async () => {
    const { whoLine } = await import("../app/command-center/hero");
    expect(whoLine("helm_x_unknown", "Helm x unknown")).toBe("Helm x unknown");
  });
});

describe("motion", () => {
  it("moves only when Live, visible, reachable, host fine and not asked for less", () => {
    const ok = { paused: false, hidden: false, reachable: true, hostCritical: false, reduced: false };
    expect(motionOf(ok)).toBe("full");
    expect(motionOf({ ...ok, reduced: true })).toBe("reduced");
    expect(motionOf({ ...ok, paused: true })).toBe("none");
    expect(motionOf({ ...ok, hidden: true })).toBe("none");
    expect(motionOf({ ...ok, reachable: false })).toBe("none");
    expect(motionOf({ ...ok, hostCritical: true })).toBe("none");
  });

  it("the page says its motion; reduced motion holds every face still", async () => {
    stubMotion(true);
    await mount(page(V(view())));
    expect($('[data-testid="command-center"]')?.getAttribute("data-motion")).toBe("reduced");
  });

  it("Paused: the chip, and no motion", async () => {
    setConsoleLive(false);
    await mount(page(V(view())));
    expect(tid("cc-paused")).not.toBeNull();
    expect($('[data-testid="command-center"]')?.getAttribute("data-motion")).toBe("none");
  });
});

describe("one read each, with ETags", () => {
  it("the view is seeded (no first fetch); the apps are read once through the shared poll", async () => {
    await mount(page(V(view())));
    await settle();
    const urls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(urls.filter((u) => u.startsWith("/v1/command-center/view")).length).toBe(0);
    expect(urls.filter((u) => u.startsWith("/v1/factory/apps")).length).toBe(1);
  });
});

describe("the order of Needs you", () => {
  it("refusal, decisions, what stopped today, the doctor's findings, then setup", () => {
    const v = V(view({ budget_refusal: { period: "month", cap_usd_micros: 1, actual_usd_micros: 2, resets_at_label: "",
                                          title: "Budget refused new work", detail: "" } }));
    const sys = { ...v.attention[0], id: "system:x", kind: "system_health" as const };
    const setup = { ...v.attention[0], id: "ready:x", kind: "readiness" as const };
    const { lead } = needsOrder(v, [setup, sys, ...v.attention]);
    expect(lead.map((a) => a.kind)).toEqual(["budget_refused", "approval", "failed_step", "system_health", "readiness"]);
  });
});

describe("the words on the page", () => {
  it.each([["busy", view({ missions: [mission()] })], ["quiet", quietView()], ["fresh", freshView()]])(
    "%s: no visible run over the copy budget", async (_name, w) => {
      await mount(page(V(w as Wire)));
      const root = $('[data-testid="command-center"]')!;
      // The templates' and apps' own words are user text; the page's words are budgeted —
      // except the approved look's own two lines, which are the design's (mockup m7).
      const APPROVED = ["Each one is a full brief your team can build today", "Start a , or pick a template below."];
      expect(overBudget(root).filter((t) => !APPROVED.includes(t))).toEqual([]);
    });
});

describe("the composer's replies name what started, never an id", () => {
  it("a run is called by what was asked for, as a link", async () => {
    const { namedRun } = await import("../app/ask-boss-hero");
    expect(namedRun("Build a lead-triage inbox that scores inbound and drafts replies")).toMatch(/^Lead-triage inbox that scores inbound.*…$/);
    expect(namedRun("Make the changelog composer.")).toBe("Changelog composer");
  });

  it("no reply in the hero writes a raw run id", async () => {
    const { readFileSync } = await import("node:fs");
    const { resolve } = await import("node:path");
    const src = readFileSync(resolve(__dirname, "../app/ask-boss-hero.tsx"), "utf8");
    expect(src).not.toMatch(/Run \\`\$\{runId\}\\`/);
    expect(src).not.toMatch(/Discovery run \\`\$\{/);
    expect(src).toContain("namedRun(directive)} run ↗");
  });
});

describe("a refusal's fix keeps the org (shared readiness panel)", () => {
  it("adds the console's org to the API's landing, once", async () => {
    const { withOrg } = await import("../app/workflow-readiness-ui");
    expect(withOrg("/?area=setup&focus=repository", "org_1")).toBe("/?org=org_1&area=setup&focus=repository");
    expect(withOrg("/?org=x&area=setup", "org_1")).toBe("/?org=x&area=setup");
    expect(withOrg("/?area=setup", null)).toBe("/?area=setup");
  });
});

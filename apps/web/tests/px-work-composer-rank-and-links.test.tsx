// @vitest-environment happy-dom
/**
 * Screen 05 (Work), the items the first pass left open (ACCEPTANCE 05-01..07, 05-18..23, 05-27,
 * 05-31, 05-33), on the engine's own shapes:
 *   - the New-work card: Feature · Bug · Ticket, Helm's plan read from GET …/work/plan for the kind
 *     (and re-read when it changes, before Start), ≈ time and ≈ cost from history, "change" picks
 *     another workflow and Start sends it;
 *   - Planned in Marshal's rank: Next up with the planner's face, the GitHub #12 chip, CI with its
 *     file, drag and Alt+↑/↓ (and ↑ ↓ buttons) re-rank through POST …/plan/{id}, optimistic with a
 *     rollback when BOSS refuses; a viewer cannot re-rank;
 *   - the Onboarding row: Setup, Discover → Ready → Backlog, Helm and Marshal, $1.20, its drawer;
 *   - Needs you: the Work tab's count is the header bar's, and "Pick a prod host" is in both.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { ProjectPage } from "../app/projects/project-page";
import {
  FX_DEPLOYMENTS, FX_HEALTH, FX_KNOWLEDGE, FX_ONBOARDING, FX_OVERVIEW, FX_SETTINGS, FX_WORK, FX_WORK_PLAN,
} from "../lib/project-experience-fixtures";
import { needsYouCards, parseOverview, parseWork, parseWorkPlan, reRank } from "../lib/project-experience";
import { $$, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "helpdesk-ai-demo";
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const type = async (el: Element | null, value: string) => {
  const input = el as HTMLInputElement;
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
};
const at = (q: string) => window.history.replaceState(null, "", `/?org=org_1&${q}`);

const BUG_PLAN = {
  ...FX_WORK_PLAN, kind: "bug",
  workflow: { key: "fast_fix", name: "Fast Fix / Hotfix", chosen: "mapped", why: "this project's workflow for bug fixes", href: "" },
  nodes: [{ key: "build", label: "Build", type: "todo" }, { key: "review", label: "Review", type: "todo" }, { key: "pr", label: "PR", type: "todo" },
          { key: "ship", label: "Staging", type: "you" }],
  estimate: { minutes: 30, minutes_basis: "typical", minutes_runs: 0, usd: null, usd_runs: 0 },
};
const MAJOR_PLAN = {
  ...FX_WORK_PLAN,
  workflow: { key: "major_feature", name: "Major Feature", chosen: "picked", why: "you picked it", href: "" },
  nodes: [{ key: "plan", label: "Plan", type: "agent", agent: { name: "Juno", role_key: "pm" } }, { key: "approve", label: "You approve", type: "you" },
          { key: "build", label: "Build", type: "todo" }, { key: "review", label: "Review", type: "todo" }, { key: "approve", label: "You approve", type: "you" },
          { key: "pr", label: "PR", type: "todo" }, { key: "ship", label: "Staging", type: "you" }],
  estimate: { minutes: 75, minutes_basis: "history", minutes_runs: 2, usd: 7.4, usd_runs: 2 },
};

/** The plan route as the engine runs it (plan.move): the item goes before `before`, else to the end
 *  of `lane`; the Work list then reads the new rank. */
function ranked() {
  const items = FX_WORK.items as unknown as { id: string; state: string; lane?: string }[];
  let order = items.filter((w) => w.state === "planned").map((w) => ({ id: w.id, lane: w.lane ?? "next" }));
  const move = (id: string, lane: string, before?: string) => {
    const me = { id, lane };
    order = order.filter((w) => w.id !== id);
    const at = before ? order.findIndex((w) => w.id === before) : order.map((w) => w.lane).lastIndexOf(lane) + 1;
    order.splice(at < 0 ? order.length : at, 0, me);
  };
  const work = () => {
    const byId = new Map(items.map((w) => [w.id, w]));
    const planned = order.map((w) => ({ ...byId.get(w.id)!, lane: w.lane }));
    return { items: [...items.filter((w) => w.state !== "planned"), ...planned] };
  };
  return { move, work };
}

function engine(over: (c: Call) => { status?: number; body: unknown } | undefined = () => undefined) {
  const rank = ranked();
  return stubFetch((c) => {
    const o = over(c);
    if (o) return o;
    const moved = c.method === "POST" && c.path.match(/^\/v1\/projects\/[^/]+\/plan\/([^/]+)$/);
    if (moved) {
      const b = c.body as { lane: string; before?: string };
      rank.move(moved[1], b.lane, b.before);
      return { body: { now: [], next: [], later: [] } };
    }
    if (c.method === "GET" && c.path === `/v1/projects/${P}/work`) return { body: rank.work() };
    if (c.method === "GET" && c.path === `/v1/projects/${P}/work/plan`) {
      const q = new URLSearchParams(c.search);
      if (q.get("workflow_key") === "major_feature") return { body: MAJOR_PLAN };
      return { body: q.get("kind") === "bug" ? BUG_PLAN : { ...FX_WORK_PLAN, kind: q.get("kind") } };
    }
    if (c.method !== "GET") return { body: { ok: true, run_id: "wfr_new" } };
    const m = c.path.match(/^\/v1\/projects\/([^/]+)\/(overview|work|knowledge|deployments|onboarding|settings|health|captain)$/);
    if (m) {
      const table: Record<string, unknown> = { overview: FX_OVERVIEW, work: FX_WORK, knowledge: FX_KNOWLEDGE, deployments: FX_DEPLOYMENTS,
                                                onboarding: { ...FX_ONBOARDING, state: "ready" }, settings: FX_SETTINGS, health: FX_HEALTH };
      return { body: table[m[2]] };
    }
    return undefined;
  });
}

async function openWork(role: "owner" | "viewer" = "owner") {
  at(`project_id=${P}&area=project&tab=work`);
  await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, role);
  await settle(10);
}
/** The Work tab's bar opens the New work modal (it replaced the one-line composer). */
async function openNewWork() {
  await press(tid("px-nw-bar-input"));
  await settle(6);
}
const strip = () => $$("[data-testid='px-compose-strip'] .px-fl-n").map((n) => n.textContent);
const planned = () => $$("[data-testid='px-work-planned'] [data-testid^='px-rank-wi_']").map((n) => n.getAttribute("data-testid")!.slice(8));

beforeEach(() => { window.history.replaceState(null, "", "/"); });
afterEach(() => { unmount(); });

describe("New work (05-01..07, 05-31)", () => {
  it("draws Helm's plan from the workflow's real steps with history estimates, and re-reads it for the kind before Start", async () => {
    const calls = engine();
    await openWork();
    expect(tid("px-composer")).toBeNull(); // the modal opens from the bar
    await openNewWork();
    expect(tid("px-composer")).not.toBeNull();
    expect(tid("px-compose-feature")!.getAttribute("aria-pressed")).toBe("true");
    expect(strip()).toEqual(["Plan", "You approve", "Build", "Review", "PR", "Staging"]);
    expect($$("[data-testid='px-compose-strip'] .px-fl-n")[1].getAttribute("data-t")).toBe("waiting");
    expect(tid("px-compose-est-min")!.textContent).toBe("≈ 25 min");
    expect(tid("px-compose-est-min")!.getAttribute("title")).toMatch(/last 3 runs/);
    expect(tid("px-compose-est-usd")!.textContent).toBe("≈ $3");
    expect(tid("px-compose-workflow")!.textContent).toContain("Minor Feature");
    // Bug: its own workflow, its own strip and a typical time — the plan updates before Start.
    await press(tid("px-compose-bug"));
    await settle(6);
    expect(calls.some((c) => c.path === `/v1/projects/${P}/work/plan` && new URLSearchParams(c.search).get("kind") === "bug")).toBe(true);
    expect(strip()).toEqual(["Build", "Review", "PR", "Staging"]);
    expect(tid("px-compose-est-min")!.getAttribute("title")).toMatch(/typical/);
    expect(tid("px-compose-est-usd")).toBeNull();
    expect(tid("px-compose-est-usd-none")!.textContent).toBe("cost after its first run");
    expect(tid("px-compose-workflow")!.textContent).toContain("Fast Fix / Hotfix");
    // Ticket: the box asks for a link.
    await press(tid("px-compose-ticket"));
    expect((tid("px-compose-text") as HTMLInputElement).placeholder).toBe("Paste a Jira key (LEND-142) or a Jira, Plane or GitHub issue link");
  });

  it("'change' picks another workflow, the plan redraws, and Start sends kind, words and the workflow", async () => {
    const calls = engine();
    await openWork();
    await openNewWork();
    await press(tid("px-compose-change"));
    expect(tid("px-compose-workflows")).not.toBeNull();
    expect(tid("px-compose-wf-minor_feature")!.getAttribute("aria-selected")).toBe("true");
    await press(tid("px-compose-wf-major_feature"));
    await settle(6);
    expect(tid("px-compose-workflows")).toBeNull();
    expect(strip()).toEqual(["Plan", "You approve", "Build", "Review", "You approve", "PR", "Staging"]);
    expect(tid("px-compose-est-min")!.textContent).toBe("≈ 75 min");
    expect(tid("px-compose-est-usd")!.textContent).toBe("≈ $7");
    await type(tid("px-compose-text"), "Let agents tag tickets by product");
    await press(tid("px-compose-start"));
    const sent = calls.find((c) => c.method === "POST" && c.path === `/v1/projects/${P}/work`);
    expect(sent?.body).toEqual({ kind: "feature", text: "Let agents tag tickets by product", workflow_key: "major_feature" });
    expect(tid("px-said")!.textContent).toMatch(/Started — Juno is planning it/);
    expect(tid("px-composer")).toBeNull(); // the modal closes on Start
    // A kind change goes back to that kind's own workflow.
    await openNewWork();
    await press(tid("px-compose-bug"));
    await settle(6);
    expect(tid("px-compose-workflow")!.textContent).toContain("Fast Fix");
  });

  it("a Ticket pasted as a link is sent as it is (BOSS keeps the issue link)", async () => {
    const calls = engine();
    await openWork();
    await openNewWork();
    await press(tid("px-compose-ticket"));
    await type(tid("px-compose-text"), "https://github.com/paramg/helpdesk-ai-demo/issues/31");
    await press(tid("px-compose-start"));
    expect(calls.find((c) => c.method === "POST" && c.path === `/v1/projects/${P}/work`)?.body)
      .toEqual({ kind: "ticket", text: "https://github.com/paramg/helpdesk-ai-demo/issues/31" });
  });
});

describe("Planned, in Marshal's rank (05-18..23, 05-33)", () => {
  it("shows Next up with the planner's face, the #12 issue chip, CI with its file, and From Roadmap", async () => {
    engine();
    await openWork();
    expect(planned()).toEqual(["wi_email", "wi_sla", "wi_tests", "wi_index"]);
    expect(tid("px-work-next-wi_email")!.textContent).toBe("Next up");
    expect(tid("px-work-next-wi_sla")).toBeNull();
    expect(tid("px-work-wi_email")!.querySelectorAll(".px-faces > *").length).toBe(1);
    const chip = tid("px-work-issue-wi_sla")!;
    expect(chip.textContent).toBe("#12");
    expect(chip.getAttribute("href")).toBe("https://github.com/paramg/helpdesk-ai-demo/issues/12");
    expect(chip.getAttribute("target")).toBe("_blank");
    expect(tid("px-work-tag-wi_sla")!.textContent).toBe("Bug");
    expect(tid("px-work-tag-wi_tests")!.textContent).toBe("CI");
    expect(tid("px-work-where-wi_tests")!.textContent).toBe("tests/test_notify.py");
    expect(tid("px-work-tag-wi_index")!.textContent).toBe("from Roadmap");
    expect(tid("px-work-wi_index")!.querySelector(".px-wk-ico")!.getAttribute("data-c")).toBe("violet");
    expect(tid("px-work-wi_tests")!.querySelector(".px-wk-ico")!.getAttribute("data-c")).toBe("red");
  });

  it("Start now sends the item's own body, with its issue link", async () => {
    const calls = engine();
    await openWork();
    await press(tid("px-work-start-wi_sla"));
    expect(calls.find((c) => c.method === "POST" && c.path === `/v1/projects/${P}/work`)?.body).toEqual({
      kind: "bug", text: "SLA timer ignores weekends. SLA timers keep counting through the weekend.", title: "SLA timer ignores weekends",
      source_ref: "rec:wi_sla", source_url: "https://github.com/paramg/helpdesk-ai-demo/issues/12" });
  });

  it("a drag re-ranks at once and saves through the plan route", async () => {
    const calls = engine();
    await openWork();
    const from = tid("px-rank-wi_tests")!;
    const to = tid("px-rank-wi_email")!;
    const data = new Map<string, string>();
    const dt = { setData: (k: string, v: string) => data.set(k, v), getData: (k: string) => data.get(k) ?? "", effectAllowed: "" };
    const fire = async (el: Element, name: string) => {
      const ev = new Event(name, { bubbles: true, cancelable: true }) as Event & { dataTransfer: unknown };
      Object.defineProperty(ev, "dataTransfer", { value: dt });
      await act(async () => { el.dispatchEvent(ev); });
    };
    expect(from.getAttribute("draggable")).toBe("true");
    await fire(from, "dragstart");
    await fire(to, "dragover");
    await fire(to, "drop");
    await settle(4);
    expect(calls.find((c) => c.method === "POST" && c.path === `/v1/projects/${P}/plan/wi_tests`)?.body).toEqual({ lane: "next", before: "wi_email" });
    expect(planned()).toEqual(["wi_tests", "wi_email", "wi_sla", "wi_index"]);
    expect(tid("px-said")!.textContent).toMatch(/Fix 6 failing tests is next up/);
  });

  it("the board's Planned column re-ranks by drag too", async () => {
    const calls = engine();
    await openWork();
    await press(tid("px-work-board"));
    const data = new Map<string, string>();
    const dt = { setData: (k: string, v: string) => data.set(k, v), getData: (k: string) => data.get(k) ?? "", effectAllowed: "" };
    const fire = async (el: Element, name: string) => {
      const ev = new Event(name, { bubbles: true, cancelable: true });
      Object.defineProperty(ev, "dataTransfer", { value: dt });
      await act(async () => { el.dispatchEvent(ev); });
    };
    await fire(tid("px-board-wi_index")!, "dragstart");
    await fire(tid("px-board-wi_sla")!, "dragover");
    await fire(tid("px-board-wi_sla")!, "drop");
    await settle(4);
    expect(calls.find((c) => c.method === "POST" && c.path === `/v1/projects/${P}/plan/wi_index`)?.body).toEqual({ lane: "next", before: "wi_sla" });
    expect($$("[aria-label='Planned'] [data-testid^='px-board-wi_']").map((n) => n.getAttribute("data-testid"))).toEqual(
      ["px-board-wi_email", "px-board-wi_index", "px-board-wi_sla", "px-board-wi_tests"]);
  });

  it("the keyboard does the same: ↓ and Alt+↑ move a row, across into Later and back", async () => {
    const calls = engine();
    await openWork();
    expect((tid("px-rank-up-wi_email") as HTMLButtonElement).disabled).toBe(true);
    await press(tid("px-rank-down-wi_tests"));
    expect(calls.filter((c) => c.method === "POST").at(-1)).toMatchObject({ path: `/v1/projects/${P}/plan/wi_tests`, body: { lane: "later" } });
    expect(planned()).toEqual(["wi_email", "wi_sla", "wi_index", "wi_tests"]);
    const row = tid("px-work-wi_tests")!;
    await act(async () => { tid("px-work-wi_tests")!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", altKey: true, bubbles: true })); });
    await settle(4);
    // Back up one: between the last of Next and the first of Later it is the end of Next again.
    const last = calls.filter((c) => c.method === "POST").at(-1)!;
    expect(last).toMatchObject({ path: `/v1/projects/${P}/plan/wi_tests`, body: { lane: "next" } });
    expect((last.body as { before?: string }).before).toBeUndefined();
    expect(planned()).toEqual(["wi_email", "wi_sla", "wi_tests", "wi_index"]);
    expect(row.getAttribute("aria-label")).toMatch(/Alt\+Up or Alt\+Down/);
  });

  it("a refused move puts the order back and says why", async () => {
    engine((c) => (c.method === "POST" && c.path.startsWith(`/v1/projects/${P}/plan/`) ? { status: 404, body: { detail: { error: "not_found", message: "Only an accepted roadmap item can move between Next and Later." } } } : undefined));
    await openWork();
    await press(tid("px-rank-up-wi_sla"));
    await settle(4);
    expect(planned()).toEqual(["wi_email", "wi_sla", "wi_tests", "wi_index"]);
    expect($$("[data-testid='px-said']").some((n) => n.textContent?.includes("Only an accepted roadmap item"))).toBe(true);
  });

  it("a viewer sees the rank but cannot change it", async () => {
    engine();
    await openWork("viewer");
    expect(planned()).toEqual(["wi_email", "wi_sla", "wi_tests", "wi_index"]);
    expect(tid("px-rank-up-wi_sla")).toBeNull();
    expect(tid("px-rank-wi_sla")!.getAttribute("draggable")).toBe("false");
  });

  it("re-ranks by position: lands in the lane of the row it goes before, or stays at the end of Next", () => {
    const items = parseWork(FX_WORK).items.filter((w) => w.state === "planned");
    expect(reRank(items, "wi_index", 0)).toMatchObject({ lane: "next", before: "wi_email" });
    expect(reRank(items, "wi_index", 3)).toBeNull();
    expect(reRank(items, "wi_email", 1)).toMatchObject({ lane: "next", before: "wi_tests" });
    const promoted = reRank([...items.slice(0, 3), { ...items[3], id: "wi_x" }, { ...items[3], id: "wi_y" }], "wi_y", 3)!;
    expect(promoted).toMatchObject({ lane: "next" });
    expect(promoted.before).toBeUndefined();
  });
});

describe("Onboarding is a row (05-27)", () => {
  it("Setup, its mini flow, Helm and Marshal, its cost, and it opens the onboarding page", async () => {
    engine();
    await openWork();
    const row = tid("px-work-onb_36")!;
    expect(tid("px-work-tag-onb_36")!.textContent).toBe("Setup");
    expect(row.textContent).toContain("$1.20");
    expect($$("[data-testid='px-work-flow-onb_36'] .px-fl-n").map((n) => n.textContent)).toEqual(["Discover", "Ready", "Backlog"]);
    expect(row.querySelectorAll(".px-faces > *").length).toBe(2);
    expect(row.querySelector(".px-wk-ico")!.getAttribute("data-c")).toBe("green");
    await press(tid("px-work-onb-onb_36"));
    expect(new URLSearchParams(window.location.search).get("tab")).toBe("onboarding");
  });
});

describe("Needs you is one list (header bar = Work tab)", () => {
  it("the Work tab's Needs-you count is the bar's, and Pick a prod host is in both", async () => {
    engine();
    await openWork();
    expect(tid("px-needs-bar")!.textContent).toContain("2 need you");
    expect(tid("px-work-f-needs_you")!.textContent).toContain("2");
    expect(tid("px-work-wi_visual")).not.toBeNull();
    expect(tid("px-work-needs-dec_prodhost")!.textContent).toContain("Pick a prod host");
    expect(tid("px-work-needs-dec_merge3")).toBeNull();
    await press(tid("px-work-needs-act-dec_prodhost"));
    expect(new URLSearchParams(window.location.search).get("drawer")).toBe("needs:dec_prodhost");
  });

  it("an engine card for every waiting row; an older engine's uncarded row gets one", () => {
    const ov = parseOverview(FX_OVERVIEW);
    const work = parseWork(FX_WORK).items;
    expect(needsYouCards(ov.needs_you, null, work).map((c) => c.id)).toEqual(["dec_merge3", "dec_prodhost"]);
    const uncarded = work.map((w) => (w.id === "wi_visual" ? { ...w, action_label: "Merge", links: { ...w.links, decision: "?area=decisions&decision=dec_x" } } : w));
    const cards = needsYouCards(ov.needs_you.filter((c) => c.id !== "dec_merge3"), null, uncarded);
    expect(cards.map((c) => [c.id, c.work_id || null])).toEqual([["dec_prodhost", null], ["work:wi_visual", "wi_visual"]]);
  });

  it("parses Helm's plan, and a missing field is empty, never a crash", () => {
    const p = parseWorkPlan(FX_WORK_PLAN);
    expect(p.workflow?.key).toBe("minor_feature");
    expect(p.nodes.map((n) => n.type)).toEqual(["agent", "you", "todo", "todo", "todo", "you"]);
    expect(p.estimate).toMatchObject({ minutes: 25, usd: 3 });
    expect(parseWorkPlan({}).nodes).toEqual([]);
    expect(parseWorkPlan(null).workflow).toBeNull();
  });
});

// @vitest-environment happy-dom
/**
 * Govern › Where people decide (agentic-factory wireframe 1), rendered from the API's own shapes
 * (GET /v1/decide, GET /v1/ambient/settings, POST /v1/decide/understand|apply, PUT
 * /v1/decide/rules/{kind}). The page is the ONE list: 7 cards + the fixed 8th, the one-line
 * summary with a link to the oldest open decision and to Agents at work, a card's popover that
 * saves, and "Change a rule in plain words" → a "Rule change" card → Apply N changes. Agents at
 * work holds each agent's alone-vs-waited card and 24 h lane.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import React, { act } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { $$, click, mountAs, settle, stubFetch, tid, type, unmount, type Call } from "./_helpers/multirepo-fixtures";

const { WherePeopleDecide } = await import("../app/govern/decide-tab");
const { AgentLanes } = await import("../app/agent-ops/agent-lanes");

afterEach(() => { unmount(); window.history.replaceState({}, "", "/"); });

const KINDS = [
  ["prod_deploy", "Production deploy", "rocket-launch"], ["merge", "Merge to the default branch", "git-merge"],
  ["spend", "Spend above a cap", "currency-dollar"], ["secrets", "Secrets and credentials", "key"],
  ["outside", "Talking to the outside", "megaphone"], ["delete", "Deleting data", "trash"],
  ["access", "Who can do what", "identification-card"],
] as const;
const rule = (over: Record<string, unknown> = {}) => ({ always_person: true, full_auto: false, decider: { seat: "ceo" }, backup: null,
  backup_after_minutes: 120, quiet: [], step_up: "", approvers: 1, ...over });
const BOARD = {
  cards: [
    ...KINDS.map(([key, title, icon], i) => ({
      key, title, icon, fixed: false, why: `why ${key}`, line: i === 0 ? "Always a person · quiet Fri 15:00 → Mon 08:00" : `line ${key}`,
      rule: rule(i === 0 ? { quiet: [{ from_day: "fri", from_time: "15:00", to_day: "mon", to_time: "08:00", tz: "Europe/London" }] }
        : key === "spend" ? { cap_usd: 500 } : key === "secrets" ? { step_up: "passkey" } : {}),
      here: rule(), decider: { kind: "seat", id: "ceo", label: "Owner seat", people: [{ id: "usr_p", name: "Param" }] },
      backup: null, scope: { per_project: key !== "secrets", projects: i === 0 ? ["northpeak"] : [] },
      count_24h: i === 0 ? 2 : 0, bars: [0, 1, 0, 0, 1, 0], agents_may: `may ${key}`, step_up: key === "secrets" ? "passkey" : "",
      delegate: { allowed: key === "merge" ? "release_branch" : "never", mine: 0 },
      trail: i === 0 ? [{ at: "2026-11-07T09:12:00Z", type: "decision", id: "dec_1", project_id: "p1", who: "usr_p", who_name: "Param",
                         status: "approved", what: "Deploy HelpDesk v1.4 to production?" }] : [],
      full_auto: "never",
    })),
    { key: "change_list", title: "Changing this list", icon: "lock-simple", fixed: true, rule: "Owner · passkey · always",
      line: "Owner · passkey · always", why: "So no agent can move the line it works under.",
      decider: { kind: "seat", id: "owner", label: "Owner", people: [] }, step_up: "passkey",
      last_change: { at: "2026-11-04T09:00:00Z", who: "Param", kind: "spend" }, agents_may: "Nothing" },
  ],
  people: [{ id: "usr_p", name: "Param", email: "p@x" }, { id: "usr_s", name: "Sarah", email: "s@x" }],
  backstop: { spend_usd: 500 },
  activity: { hours: 24, handled_by_agents: 142, asked_a_person: 6, waiting: 1,
              oldest: { id: "dec_9", project_id: "p1", question: "Merge PR #3?", at: "2026-11-07T08:00:00Z" } },
};
const SETTINGS = {
  settings: {}, envelope: { cap_usd_micros: 5e6, spent_usd_micros: 0, held_usd_micros: 0, available_usd_micros: 5e6, resets: "00:00" },
  stop: { stopped: false, scope: null, by: null, at: null, reason: "" },
  mode: { mode: "route", source: "console", pinned: false, by: null, at: null },
  review_builds: { install: false, project: null, classes: {}, effective: false, source: "install" },
};
const UNDERSTOOD = {
  status: "understood",
  edits: [{ kind: "prod_deploy", field: "quiet", scope: "install", title: "Production deploy",
            value: { from_day: "fri", from_time: "15:00", to_day: "mon", to_time: "08:00", tz: "Europe/London" },
            says: "Nothing goes to prod from Friday 15:00 to Monday 08:00." },
          { kind: "spend", field: "decider", scope: "install", title: "Spend above a cap",
            value: { person: "usr_s", name: "Sarah" }, says: "Sarah decides spend instead of you." }],
  already_allowed: [{ says: "Merging into staging/* is not the default branch: agents do it today.", kind: "merge" }],
  cannot: [], reply: "Two changes.",
  replay: [{ kind: "prod_deploy", field: "quiet", n: 3, words: "3 deploys would have waited until Mon 08:00" },
           { kind: "spend", field: "decider", n: 2, words: "2 spend decisions would have gone to Sarah" }],
};

function api(over?: (c: Call) => { status?: number; body: unknown } | undefined) {
  return stubFetch((c) => {
    const o = over?.(c);
    if (o) return o;
    if (c.path === "/v1/decide" && c.method === "GET") return { body: BOARD };
    if (c.path === "/v1/ambient/settings") return { body: SETTINGS };
    if (c.path === "/v1/decide/understand") return { body: UNDERSTOOD };
    if (c.path === "/v1/decide/apply") return { body: { applied: [] } };
    if (c.path.startsWith("/v1/decide/rules/")) return { body: { saved: {}, line: "x" } };
    if (c.path === "/v1/decide/activity") return { body: { summary: BOARD.activity, agents: [
      { role: "sre", alone: 9, waited: 1, lane: Array.from({ length: 24 }, (_, i) => [i % 3, i === 5 ? 1 : 0]) },
      { role: "finops_sentinel", alone: 0, waited: 0, lane: [] }] } };
    if (c.path === "/v1/ambient/autonomy") return { body: { classes: [{ task_class: "incident_response", title: "Incident", level: "A1", ceiling: "A3",
                                                                          owner_role: "sre", suggested_level: "A2" }], levels: [] } };
    return undefined;
  });
}
const sent = (calls: Call[], path: string | RegExp) =>
  calls.filter((c) => c.method !== "GET" && (typeof path === "string" ? c.path === path : path.test(c.path)));

async function open() {
  const calls = api();
  setConsoleLive(true);
  window.history.replaceState({}, "", "/?area=budget&tab=autonomy&org=o1");
  await mountAs(<WherePeopleDecide projectId="p1" />, "owner");
  await settle();
  return calls;
}

describe("Where people decide", () => {
  it("draws the header, the one line and the 7 + 1 cards from the list", async () => {
    await open();
    expect(document.querySelector("h1")?.textContent).toBe("Where people decide");
    expect(tid("wd-agents-state")?.textContent).toContain("Agents on");
    expect(tid("wd-stop-all")).not.toBeNull();
    expect(tid("wd-sum-agents")?.textContent).toBe("142");
    expect(tid("wd-sum-asked")?.textContent).toBe("6");
    expect(tid("wd-sum-waiting")?.textContent).toBe("1");
    expect(tid("wd-sum-oldest")?.getAttribute("href")).toContain("decision=dec_9");
    expect(tid("wd-what-agents-did")?.getAttribute("href")).toContain("area=agent_ops");
    const cards = $$('[data-testid="wd-card"]');
    expect(cards.map((c) => c.dataset.kind)).toEqual([...KINDS.map((k) => k[0]), "change_list"]);
    expect(cards[0].textContent).toContain("2 · 24h");
    expect(cards[0].textContent).toContain("Owner seat");
    expect(cards[3].textContent).toContain("passkey");
    expect(cards[7].textContent).toContain("fixed");
  });

  it("opens a card's popover with who decides, backup, scope, window, delegate, Full auto and the trail, and saves with the passkey", async () => {
    const calls = await open();
    await click($$('[data-testid="wd-card"]')[0]);
    const pop = tid("wd-pop")!;
    for (const words of ["Who decides", "Scope", "Quiet window", "Agents may", "On my behalf", "Full auto", "Never: irreversible"]) {
      expect(pop.textContent).toContain(words);
    }
    expect(pop.textContent).toContain("Fri 15:00 → Mon 08:00");
    expect(tid("wd-trail")?.querySelector("a")?.getAttribute("href")).toContain("decision=dec_1");
    expect(tid("wd-audit")?.getAttribute("href")).toContain("decide.rule_changed");
    expect(tid("wd-save")?.textContent).toContain("Save · passkey");
    const pick = tid("wd-pick-backup") as HTMLSelectElement;
    await act(async () => { pick.value = "p:usr_s"; pick.dispatchEvent(new Event("change", { bubbles: true })); });
    await settle();
    await click(tid("wd-save"));
    const put = sent(calls, "/v1/decide/rules/prod_deploy");
    expect(put).toHaveLength(1);
    expect(put[0].method).toBe("PUT");
    expect(put[0].body).toMatchObject({ project_id: "p1", scope: "install",
                                        rule: { backup: { person: "usr_s", name: "Sarah" }, quiet: [{ from_day: "fri" }] } });
  });

  it("says why a looser save was refused", async () => {
    api((c) => (c.path.startsWith("/v1/decide/rules/") ? { status: 422, body: { detail: { error: "stricter_only",
      message: "the cap can be lowered, never raised above the $500 backstop" } } } : undefined));
    setConsoleLive(true);
    await mountAs(<WherePeopleDecide projectId="p1" />, "owner");
    await settle();
    await click($$('[data-testid="wd-card"]')[2]);
    await click(tid("wd-save"));
    expect(tid("wd-refusal")?.textContent).toContain("never raised above");
  });

  it("turns plain words into a Rule change card with the 30-day replay, then applies N changes", async () => {
    const calls = await open();
    await type(tid("wd-words-input"), "Nothing goes to prod on Fridays after 3pm. Over $200 a day needs Sarah.");
    await click(tid("wd-words-read"));
    const understand = sent(calls, "/v1/decide/understand");
    expect(understand).toHaveLength(1);
    expect(understand[0].body).toMatchObject({ project_id: "p1" });
    const card = tid("wd-rule-change")!;
    expect(card.querySelector("b")?.textContent).toBe("Rule change");
    expect(document.body.textContent).not.toContain("Here's what I understood");
    expect($$('[data-testid="wd-edit"]')).toHaveLength(2);
    expect(tid("wd-already")?.textContent).toContain("Nothing to change.");
    expect(tid("wd-replay")?.textContent).toContain("3 deploys would have waited");
    expect(tid("wd-apply")?.textContent).toContain("Apply 2 changes");
    await click(tid("wd-edit-toggle"));
    await click($$('[data-testid="wd-edit-drop"]')[1]);
    expect(tid("wd-apply")?.textContent).toContain("Apply 1 change");
    await click(tid("wd-apply"));
    const apply = sent(calls, "/v1/decide/apply");
    expect(apply).toHaveLength(1);
    expect((apply[0].body as { edits: unknown[] }).edits).toEqual([
      { kind: "prod_deploy", field: "quiet", scope: "install", value: UNDERSTOOD.edits[0].value }]);
  });

  it("keeps the words when Helm cannot read them", async () => {
    api((c) => (c.path === "/v1/decide/understand" ? { body: { status: "no_model", edits: [], reply: "Helm could not read it just now." } } : undefined));
    setConsoleLive(true);
    await mountAs(<WherePeopleDecide projectId="p1" />, "owner");
    await settle();
    await type(tid("wd-words-input"), "something");
    await click(tid("wd-words-read"));
    expect(tid("wd-rule-change-fallback")?.textContent).toContain("could not read");
    expect((tid("wd-words-input") as HTMLTextAreaElement).value).toBe("something");
  });

  it("opens the kill switch's confirm from Stop all", async () => {
    await open();
    await click(tid("wd-stop-all"));
    expect(tid("aa-kill-confirm")).not.toBeNull();
  });
});

describe("Agents at work › each agent's 24 h", () => {
  it("shows each area agent's alone-vs-waited split, its lane and an earned raise", async () => {
    api();
    setConsoleLive(true);
    await mountAs(<AgentLanes projectId="p1" />, "owner");
    await settle();
    const cards = $$('[data-testid="wd-agent"]');
    expect(cards.map((c) => c.dataset.role)).toEqual(["sre", "finops_sentinel"]);
    expect(cards[0].textContent).toContain("9 alone");
    expect(cards[0].querySelectorAll(".wd-lane i")).toHaveLength(24);
    expect(cards[0].querySelector('[data-testid="wd-agent-raise"]')?.getAttribute("href")).toContain("tab=areas");
  });
});

describe("the page's type sizes", () => {
  it("uses the console's --fs tokens, never a literal font-size", () => {
    const css = readFileSync(join(__dirname, "../app/govern/decide.css"), "utf8");
    const sizes = [...css.matchAll(/font-size:\s*([^;}]+)/g)].map((m) => m[1].trim());
    expect(sizes.length).toBeGreaterThan(10);
    for (const s of sizes) expect(s).toMatch(/^var\(--fs-(page|title|body|row|ui|meta|tag|label)\)$/);
  });
});

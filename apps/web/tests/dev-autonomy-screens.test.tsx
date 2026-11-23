// @vitest-environment happy-dom
/**
 * DEV AUTONOMY's three screens (BOSSAPP-1441, approved wireframe 2026-11-23), rendered from the
 * API's own shapes (apps/api/app/routers/autonomy.py):
 *   Setup › Autonomy           one line + Change › to Govern › Where people decide
 *   Govern › Autonomy control  org default / this project (stricter only); the locked rows are
 *                              never controls; the API's 422 sentence is shown
 *   Work › Decided for you     latest first; Undo, Can't undo, the 409 reason; Looks good
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { $$, click, mountAs, settle, stubFetch, tid, type, unmount, type Call } from "./_helpers/multirepo-fixtures";

const { AutonomySetupRow } = await import("../app/autonomy/setup-row");
const { AutonomyControl } = await import("../app/autonomy/control");
const { DecidedForYou } = await import("../app/autonomy/decided");
const { parsePolicyView } = await import("../lib/autonomy");

afterEach(() => { unmount(); window.history.replaceState({}, "", "/"); });

const pol = (over: Record<string, unknown> = {}) => ({
  dev: "act", prod: "ask", unknown_remote: "prod", dev_topup_usd: { per_item: 1, per_day: 10 }, explicit_stops: "honour",
  source: "org", set_by: "usr_p", set_at: "2026-11-23T10:00:00Z", org_dev: null,
  line: "Development: agents act · Production: you decide", fixed: ["prod", "unknown_remote", "explicit_stops"],
  modes_available: ["act", "act_capped", "ask"], ...over,
});
const VIEW = { policy: pol(), org_default: pol(), install: { production: false } };

const DECIDED = {
  items: [
    { decision_id: "dec_3", question: "Merge PR #412 into develop?", agent: "release_manager", words: "Decided by Release manager under dev autonomy",
      class: "ask_prod_only", why: "not a prod branch", verdict: {}, action: "review_pull_request", decided_at: "2026-11-23T14:05:00Z",
      undo: "revert_merge", undo_unavailable: null, undone: false, reviewed: false, links: { workflow_run_id: "wfr_9" } },
    { decision_id: "dec_2", question: "Top up $0.80 on build 981?", agent: "finops_sentinel", words: "x", class: "act_capped",
      why: "within top-up", verdict: {}, action: "pay", decided_at: "2026-11-23T12:18:00Z", undo: null,
      undo_unavailable: "spend cannot be recalled; lower the top-up in the autonomy policy", undone: false, reviewed: true, links: {} },
  ],
  count: 2, unreviewed: 1,
};

const sent = (calls: Call[], path: string) => calls.filter((c) => c.method !== "GET" && c.path === path);

describe("Setup › Autonomy", () => {
  it("says the one line and links Change › to Govern › Where people decide, keeping the org", async () => {
    stubFetch((c) => (c.path === "/v1/autonomy/policy" ? { body: VIEW } : undefined));
    window.history.replaceState({}, "", "/?area=setup&org=o1");
    await mountAs(<AutonomySetupRow projectId="p1" />);
    await settle();
    expect(tid("su-autonomy-line")?.textContent).toContain("Development: agents act");
    expect(tid("su-autonomy-line")?.textContent).toContain("Production: you decide");
    const href = tid("su-autonomy-change")!.getAttribute("href")!;
    const q = new URLSearchParams(href.slice(href.indexOf("?") + 1));
    expect([q.get("area"), q.get("tab"), q.get("org")]).toEqual(["budget", "autonomy", "o1"]);
  });
});

describe("Govern › Autonomy control", () => {
  async function mount(view = VIEW, over?: (c: Call) => { status?: number; body: unknown } | undefined) {
    const calls = stubFetch((c) => over?.(c) ?? (c.method === "PUT" ? { body: { policy: pol() } } : undefined));
    setConsoleLive(true);
    let saved = 0;
    await mountAs(<AutonomyControl projectId="p1" view={parsePolicyView(view)} onSaved={() => { saved += 1; }} />);
    await settle();
    return { calls, saved: () => saved };
  }

  it("draws Development as a three-way choice and Production, Unknown remote, Explicit stops locked", async () => {
    await mount();
    expect($$('[data-testid="au-dev"] [role="radio"]').map((b) => b.textContent)).toEqual(["Agents act", "Act within cap", "Ask me"]);
    expect(tid("au-dev-act")?.getAttribute("aria-checked")).toBe("true");
    for (const [id, words] of [["au-lock-prod", "You decide"], ["au-lock-unknown", "Treated as production"], ["au-lock-stops", "Honoured"]]) {
      const lock = tid(id)!;
      expect(lock.textContent).toBe(words);
      // Never editable: no control of any kind inside a locked row.
      expect(lock.querySelector("button, input, select, textarea, [role='radio'], [contenteditable]")).toBeNull();
    }
    expect((tid("au-topup-item") as HTMLInputElement).value).toBe("1");
    expect((tid("au-topup-day") as HTMLInputElement).value).toBe("10");
    expect((tid("au-save") as HTMLButtonElement).disabled).toBe(true);
  });

  it("saves the org default through PUT /v1/autonomy/org-policy", async () => {
    const m = await mount();
    await click(tid("au-dev-ask"));
    await type(tid("au-topup-day"), "5");
    await click(tid("au-save"));
    const put = sent(m.calls, "/v1/autonomy/org-policy");
    expect(put).toHaveLength(1);
    expect(put[0].method).toBe("PUT");
    expect(put[0].body).toEqual({ project_id: "p1", policy: { dev: "ask", dev_topup_usd: { per_item: 1, per_day: 5 } } });
    expect(tid("au-said")?.textContent).toBe("Saved.");
    expect(m.saved()).toBe(1);
  });

  it("this project is stricter only: looser modes are greyed, a larger top-up is refused before it is sent", async () => {
    const org = pol({ dev: "act_capped", dev_topup_usd: { per_item: 2, per_day: 20 } });
    const m = await mount({ policy: pol({ dev: "act_capped", source: "project", org_dev: "act_capped", dev_topup_usd: { per_item: 2, per_day: 20 } }), org_default: org, install: { production: false } });
    expect(tid("au-scope-project")?.getAttribute("aria-pressed")).toBe("true");
    expect((tid("au-dev-act") as HTMLButtonElement).disabled).toBe(true);
    expect(tid("au-dev-act")?.getAttribute("title")).toContain("only be stricter");
    expect((tid("au-dev-ask") as HTMLButtonElement).disabled).toBe(false);
    await type(tid("au-topup-item"), "3");
    await click(tid("au-save"));
    expect(sent(m.calls, "/v1/autonomy/policy")).toHaveLength(0);
    expect(tid("au-said")?.textContent).toContain("may only lower it");
    await type(tid("au-topup-item"), "1.5");
    await click(tid("au-dev-ask"));
    await click(tid("au-save"));
    const put = sent(m.calls, "/v1/autonomy/policy");
    expect(put).toHaveLength(1);
    expect(put[0].body).toEqual({ project_id: "p1", policy: { dev: "ask", dev_topup_usd: { per_item: 1.5, per_day: 20 } } });
    expect(sent(m.calls, "/v1/autonomy/org-policy")).toHaveLength(0);
  });

  it("switching scope resets the inputs to that scope's values", async () => {
    const org = pol({ dev: "act", dev_topup_usd: { per_item: 2, per_day: 20 } });
    await mount({ policy: pol({ dev: "ask", source: "project", dev_topup_usd: { per_item: 1, per_day: 5 } }), org_default: org, install: { production: false } });
    expect((tid("au-topup-day") as HTMLInputElement).value).toBe("5");
    await type(tid("au-topup-day"), "4");
    await click(tid("au-scope-org"));
    expect((tid("au-topup-day") as HTMLInputElement).value).toBe("20");
    expect(tid("au-dev-act")?.getAttribute("aria-checked")).toBe("true");
  });

  it("says the API's own sentence when it refuses a looser project row (422)", async () => {
    const m = await mount(VIEW, (c) => (c.path === "/v1/autonomy/policy" && c.method === "PUT"
      ? { status: 422, body: { detail: { error: "stricter_only", message: "the org default is dev: ask; a project may only be stricter" } } } : undefined));
    await click(tid("au-scope-project"));
    await click(tid("au-dev-act_capped"));
    await click(tid("au-save"));
    expect(sent(m.calls, "/v1/autonomy/policy")).toHaveLength(1);
    expect(tid("au-said")?.textContent).toContain("a project may only be stricter");
    expect(m.saved()).toBe(0);
  });
});

describe("Work › Decided for you", () => {
  async function mount(over?: (c: Call) => { status?: number; body: unknown } | undefined) {
    const calls = stubFetch((c) => over?.(c) ?? (c.path === "/v1/autonomy/decided" ? { body: DECIDED }
      : c.path.endsWith("/reviewed") ? { body: { reviewed: true } } : c.path.endsWith("/undo") ? { body: { result: "reverted" } } : undefined));
    setConsoleLive(true);
    window.history.replaceState({}, "", "/?org=o1&project_id=p1");
    await mountAs(<DecidedForYou projectId="p1" />);
    await settle();
    return calls;
  }

  it("lists what agents decided, latest first, with the agent, the why, the time and Run ›", async () => {
    await mount();
    const rows = $$('[data-testid^="au-decided-dec_"][role="listitem"]');
    expect(rows.map((r) => r.dataset.testid)).toEqual(["au-decided-dec_3", "au-decided-dec_2"]);
    expect(tid("au-decided-dec_3-what")?.textContent).toBe("Merge PR #412 into develop");
    expect(rows[0].textContent).toContain("Release manager · dev autonomy");
    expect(rows[0].textContent).toContain("not a prod branch");
    expect(rows[0].querySelector("time")?.getAttribute("datetime")).toBe("2026-11-23T14:05:00Z");
    const run = tid("au-decided-dec_3-run")!.getAttribute("href")!;
    expect(run).toContain("area=delivery_workflows");
    expect(run).toContain("wfr=wfr_9");
    expect(tid("au-decided-dec_3-undo")?.textContent).toContain("Revert merge");
    expect(tid("au-decided-dec_2-noundo")?.textContent).toBe("Can't undo");
    expect(tid("au-decided-dec_2-noundo")?.getAttribute("title")).toContain("spend cannot be recalled");
    expect(tid("au-decided-dec_2-ok")?.getAttribute("aria-pressed")).toBe("true");
  });

  it("Undo posts to the decision, and a 409 says the API's reason on the row", async () => {
    const calls = await mount((c) => (c.path.endsWith("/undo") ? { status: 409, body: { detail: "Already undone." } } : undefined)
      ?? (c.path === "/v1/autonomy/decided" ? { body: DECIDED } : undefined));
    await click(tid("au-decided-dec_3-undo"));
    const post = sent(calls, "/v1/autonomy/decided/dec_3/undo");
    expect(post).toHaveLength(1);
    expect(post[0].body).toEqual({ project_id: "p1" });
    expect(tid("au-decided-dec_3-err")?.textContent).toBe("Already undone.");
  });

  it("Looks good posts reviewed and ticks the row", async () => {
    const calls = await mount();
    expect(tid("au-decided-dec_3-ok")?.getAttribute("aria-pressed")).toBe("false");
    await click(tid("au-decided-dec_3-ok"));
    expect(sent(calls, "/v1/autonomy/decided/dec_3/reviewed")).toHaveLength(1);
    expect(tid("au-decided-dec_3-ok")?.getAttribute("aria-pressed")).toBe("true");
  });

  it("an undone row offers neither Undo nor Looks good, and a failed read says so", async () => {
    const undone = { items: [{ ...DECIDED.items[0], undone: true }], count: 1, unreviewed: 1 };
    await mount((c) => (c.path === "/v1/autonomy/decided" ? { body: undone } : undefined));
    expect(tid("au-decided-dec_3-undo")).toBeNull();
    expect(tid("au-decided-dec_3-undone")?.textContent).toBe("Undone");
    expect((tid("au-decided-dec_3-ok") as HTMLButtonElement).disabled).toBe(true);
    unmount();
    await mount((c) => (c.path === "/v1/autonomy/decided" ? { status: 500, body: { detail: "boom" } } : undefined));
    expect(tid("au-decided-error")?.textContent).toContain("boom");
  });

  it("draws nothing when nothing was decided for you", async () => {
    await mount((c) => (c.path === "/v1/autonomy/decided" ? { body: { items: [], count: 0, unreviewed: 0 } } : undefined));
    expect(tid("au-decided")).toBeNull();
  });
});

describe("the screens' styles", () => {
  it("use --fs tokens only and never amber (amber is the one Needs-you signal)", () => {
    const css = readFileSync(join(__dirname, "../app/autonomy/autonomy.css"), "utf8");
    const sizes = [...css.matchAll(/font-size:\s*([^;}]+)/g)].map((m) => m[1].trim());
    expect(sizes.length).toBeGreaterThan(5);
    for (const s of sizes) expect(s).toMatch(/^var\(--fs-(page|title|body|row|ui|meta|tag|label)\)$/);
    expect(css.replace(/\/\*[\s\S]*?\*\//g, "")).not.toMatch(/amber|#[0-9a-f]{3,6}\b/i);
  });
});
